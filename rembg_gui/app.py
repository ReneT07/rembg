#!/usr/bin/env python3
"""
rembg GUI — a simple macOS desktop app to remove image/video backgrounds.

Two tabs:
  • Images — batch-remove backgrounds from images.
  • Video  — extract frames with ffmpeg, remove backgrounds with rembg, then
             export either a processed video (H.264 / ProRes) or PNG frames.

Wraps the rembg *Python API* (no shell calls for rembg) and calls **ffmpeg via
subprocess** for video work. Meant to run inside the virtual environment at
/Users/reneturek/Projects/GitHub/rembg/venv.

Run:
    /Users/reneturek/Projects/GitHub/rembg/venv/bin/python rembg_gui/app.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass

from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

MODELS = ["u2net", "u2netp", "isnet-general-use", "isnet-anime"]
DEFAULT_MODEL = "isnet-general-use"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg",
              ".wmv", ".flv"}

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


@dataclass(frozen=True)
class InputItem:
    """A single image to process (for the Images tab)."""

    path: str
    root: str


# ----------------------------------------------------------------------------
# Shared widgets / helpers
# ----------------------------------------------------------------------------


class DropList(QListWidget):
    """A QListWidget that accepts dropped files and folders."""

    files_dropped = Signal(list)  # list[str] of dropped paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.ExtendedSelection)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls()]
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


def make_slider(lo, hi, default):
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(default)
    label = QLabel(str(default))
    label.setMinimumWidth(32)
    s.valueChanged.connect(lambda v, lbl=label: lbl.setText(str(v)))
    return s, label


def build_alpha_group():
    """Return (groupbox, controls_widget, check, (s_fg,s_bg,s_erode))."""
    box = QGroupBox("Alpha matting")
    layout = QVBoxLayout(box)
    check = QCheckBox("Enable alpha matting")
    layout.addWidget(check)

    controls = QWidget()
    grid = QGridLayout(controls)
    s_fg, l_fg = make_slider(0, 255, 240)
    s_bg, l_bg = make_slider(0, 255, 10)
    s_erode, l_erode = make_slider(0, 50, 5)
    grid.addWidget(QLabel("Foreground threshold"), 0, 0)
    grid.addWidget(s_fg, 0, 1)
    grid.addWidget(l_fg, 0, 2)
    grid.addWidget(QLabel("Background threshold"), 1, 0)
    grid.addWidget(s_bg, 1, 1)
    grid.addWidget(l_bg, 1, 2)
    grid.addWidget(QLabel("Erode size"), 2, 0)
    grid.addWidget(s_erode, 2, 1)
    grid.addWidget(l_erode, 2, 2)
    layout.addWidget(controls)
    controls.setEnabled(False)
    check.toggled.connect(controls.setEnabled)
    return box, controls, check, (s_fg, s_bg, s_erode)


# ============================================================================
# IMAGES TAB
# ============================================================================


class ImageWorker(QObject):
    progress = Signal(int, int)
    current = Signal(int, int, str)   # index, total, filename (before each image)
    log = Signal(str)
    finished = Signal(int, int)

    def __init__(self, items, model, output_dir, preserve_structure,
                 alpha_matting, fg, bg, erode):
        super().__init__()
        self.items = items
        self.model = model
        self.output_dir = output_dir
        self.preserve_structure = preserve_structure
        self.alpha_matting = alpha_matting
        self.fg = fg
        self.bg = bg
        self.erode = erode
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _output_path(self, item: InputItem) -> str:
        if self.preserve_structure:
            rel = os.path.relpath(item.path, item.root)
            rel = os.path.splitext(rel)[0] + ".png"
            return os.path.join(self.output_dir, rel)
        name = os.path.splitext(os.path.basename(item.path))[0] + ".png"
        return os.path.join(self.output_dir, name)

    @Slot()
    def run(self):
        succeeded = failed = 0
        total = len(self.items)
        try:
            self.log.emit(f"Loading model '{self.model}' …")
            from rembg import new_session, remove
            from PIL import Image
            session = new_session(self.model)
            self.log.emit("Model ready. Starting …")
        except Exception as exc:
            self.log.emit(f"FATAL: could not initialize rembg: {exc}")
            self.log.emit(traceback.format_exc())
            self.finished.emit(0, total)
            return

        for i, item in enumerate(self.items, start=1):
            if self._cancel:
                self.log.emit("Cancelled by user.")
                break
            self.current.emit(i, total, os.path.basename(item.path))
            try:
                dest = self._output_path(item)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with Image.open(item.path) as img:
                    img.load()
                    result = remove(
                        img, session=session,
                        alpha_matting=self.alpha_matting,
                        alpha_matting_foreground_threshold=self.fg,
                        alpha_matting_background_threshold=self.bg,
                        alpha_matting_erode_size=self.erode,
                    )
                    result.save(dest)
                succeeded += 1
                self.log.emit(f"[OK]   {os.path.basename(item.path)} → {dest}")
            except Exception as exc:
                failed += 1
                self.log.emit(f"[FAIL] {os.path.basename(item.path)} : {exc}")
            finally:
                self.progress.emit(i, total)

        self.finished.emit(succeeded, failed)


class ImageTab(QWidget):
    def __init__(self):
        super().__init__()
        self.items: dict[str, InputItem] = {}
        self.output_dir: str | None = None
        self.thread: QThread | None = None
        self.worker: ImageWorker | None = None
        self._build_ui()

    def _build_ui(self):
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        root.addLayout(left, 3)

        in_box = QGroupBox("Input images")
        in_layout = QVBoxLayout(in_box)
        btn_row = QHBoxLayout()
        b_single = QPushButton("Add image…")
        b_multi = QPushButton("Add images…")
        b_folder = QPushButton("Add folder…")
        b_single.clicked.connect(self.add_single)
        b_multi.clicked.connect(self.add_multiple)
        b_folder.clicked.connect(self.add_folder)
        for b in (b_single, b_multi, b_folder):
            btn_row.addWidget(b)
        in_layout.addLayout(btn_row)

        self.list = DropList()
        self.list.files_dropped.connect(self.on_files_dropped)
        in_layout.addWidget(self.list)

        list_btns = QHBoxLayout()
        b_remove = QPushButton("Remove selected")
        b_clear = QPushButton("Clear all")
        b_remove.clicked.connect(self.remove_selected)
        b_clear.clicked.connect(self.clear_all)
        list_btns.addWidget(b_remove)
        list_btns.addWidget(b_clear)
        list_btns.addStretch(1)
        self.count_label = QLabel("0 images")
        list_btns.addWidget(self.count_label)
        in_layout.addLayout(list_btns)

        hint = QLabel("Tip: drag & drop images or folders here.")
        hint.setStyleSheet("color: gray;")
        in_layout.addWidget(hint)
        left.addWidget(in_box, 3)

        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        log_layout.addWidget(self.log_view)
        left.addWidget(log_box, 2)

        right = QVBoxLayout()
        root.addLayout(right, 2)

        model_box = QGroupBox("Model")
        model_layout = QVBoxLayout(model_box)
        self.model_combo = QComboBox()
        self.model_combo.addItems(MODELS)
        self.model_combo.setCurrentText(DEFAULT_MODEL)
        model_layout.addWidget(self.model_combo)
        right.addWidget(model_box)

        am_box, self.am_controls, self.am_check, sliders = build_alpha_group()
        self.s_fg, self.s_bg, self.s_erode = sliders
        right.addWidget(am_box)

        out_box = QGroupBox("Output")
        out_layout = QVBoxLayout(out_box)
        b_out = QPushButton("Choose output folder…")
        b_out.clicked.connect(self.choose_output)
        out_layout.addWidget(b_out)
        self.out_label = QLabel("No output folder selected")
        self.out_label.setWordWrap(True)
        self.out_label.setStyleSheet("color: gray;")
        out_layout.addWidget(self.out_label)
        self.preserve_check = QCheckBox("Preserve folder structure (batch)")
        out_layout.addWidget(self.preserve_check)
        right.addWidget(out_box)

        right.addStretch(1)
        self.process_btn = QPushButton("Process")
        self.process_btn.setMinimumHeight(40)
        self.process_btn.clicked.connect(self.start_processing)
        right.addWidget(self.process_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_processing)
        right.addWidget(self.cancel_btn)
        self.status_label = QLabel("Idle.")
        self.status_label.setStyleSheet("color: gray;")
        right.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m")
        right.addWidget(self.progress)

    # input handling
    def _add_paths(self, paths, root_for_files=None):
        added = 0
        for p in paths:
            if not p:
                continue
            if os.path.isdir(p):
                base = os.path.dirname(os.path.normpath(p))
                for dirpath, _dirs, files in os.walk(p):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                            added += self._add_one(os.path.join(dirpath, f), base)
            elif os.path.isfile(p):
                if os.path.splitext(p)[1].lower() in IMAGE_EXTS:
                    root = root_for_files or os.path.dirname(os.path.abspath(p))
                    added += self._add_one(os.path.abspath(p), root)
        self._refresh_count()
        if added:
            self.log_view.appendPlainText(f"Added {added} image(s).")

    def _add_one(self, path, root) -> int:
        if path in self.items:
            return 0
        self.items[path] = InputItem(path=path, root=root)
        self.list.addItem(QListWidgetItem(path))
        return 1

    @Slot()
    def add_single(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.gif)")
        if path:
            self._add_paths([path])

    @Slot()
    def add_multiple(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select images", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.gif)")
        if paths:
            self._add_paths(paths)

    @Slot()
    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder")
        if folder:
            self._add_paths([folder])

    @Slot(list)
    def on_files_dropped(self, paths):
        self._add_paths(paths)

    @Slot()
    def remove_selected(self):
        for item in self.list.selectedItems():
            self.items.pop(item.text(), None)
            self.list.takeItem(self.list.row(item))
        self._refresh_count()

    @Slot()
    def clear_all(self):
        self.items.clear()
        self.list.clear()
        self._refresh_count()

    def _refresh_count(self):
        self.count_label.setText(f"{len(self.items)} images")

    @Slot()
    def choose_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self.output_dir = folder
            self.out_label.setText(folder)
            self.out_label.setStyleSheet("color: black;")

    # processing
    @Slot()
    def start_processing(self):
        if not self.items:
            QMessageBox.warning(self, "No input", "Add at least one image first.")
            return
        if not self.output_dir:
            QMessageBox.warning(self, "No output", "Choose an output folder first.")
            return
        self.process_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setValue(0)
        self.log_view.appendPlainText("──────── starting ────────")

        self.thread = QThread()
        self.worker = ImageWorker(
            items=list(self.items.values()),
            model=self.model_combo.currentText(),
            output_dir=self.output_dir,
            preserve_structure=self.preserve_check.isChecked(),
            alpha_matting=self.am_check.isChecked(),
            fg=self.s_fg.value(), bg=self.s_bg.value(), erode=self.s_erode.value(),
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.current.connect(self.on_current)
        self.worker.log.connect(self.log_view.appendPlainText)
        self.worker.finished.connect(self.on_finished)
        self.thread.start()

    @Slot()
    def cancel_processing(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)

    @Slot(int, int)
    def on_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    @Slot(int, int, str)
    def on_current(self, i, total, name):
        self.status_label.setStyleSheet("color: black;")
        self.status_label.setText(f"Processing {i}/{total}: {name}")

    @Slot(int, int)
    def on_finished(self, succeeded, failed):
        self.log_view.appendPlainText(
            f"──────── done: {succeeded} ok, {failed} failed ────────")
        self.status_label.setText(f"Done — {succeeded} ok, {failed} failed.")
        self.status_label.setStyleSheet("color: gray;")
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread = self.worker = None
        self.process_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)


# ============================================================================
# VIDEO TAB
# ============================================================================


def probe_video(path):
    """Return dict(fps, duration, has_audio, frames_est) using ffprobe."""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    data = json.loads(out.stdout)
    fps = 30.0
    has_audio = False
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and "r_frame_rate" in s:
            num, _, den = s["r_frame_rate"].partition("/")
            try:
                den = float(den) if den else 1.0
                if den:
                    fps = float(num) / den
            except ValueError:
                pass
        elif s.get("codec_type") == "audio":
            has_audio = True
    duration = float(data.get("format", {}).get("duration", 0) or 0)
    frames_est = int(round(duration * fps)) if duration and fps else 0
    return {"fps": fps, "duration": duration,
            "has_audio": has_audio, "frames_est": frames_est}


class VideoWorker(QObject):
    extract_progress = Signal(int, int)
    process_progress = Signal(int, int)
    export_progress = Signal(int, int)
    log = Signal(str)
    finished = Signal(bool, str)   # success, output_location

    def __init__(self, video_path, info, model, alpha_matting, fg, bg, erode,
                 mode, output_dir, codec, override_fps, fps_value,
                 png_numbering):
        super().__init__()
        self.video_path = video_path
        self.info = info
        self.model = model
        self.alpha_matting = alpha_matting
        self.fg, self.bg, self.erode = fg, bg, erode
        self.mode = mode                # "video" | "png"
        self.output_dir = output_dir
        self.codec = codec              # "h264" | "prores"
        self.override_fps = override_fps
        self.fps_value = fps_value
        self.png_numbering = png_numbering   # "keep" | "restart"
        self._cancel = False
        self._proc = None
        self._tmp = None

    def cancel(self):
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    # -- ffmpeg helper with progress parsing --------------------------------
    def _run_ffmpeg(self, cmd, total, progress_signal, stage):
        self.log.emit(f"$ {' '.join(self._shorten(c) for c in cmd)}")
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1)
        tail = []
        for line in self._proc.stdout:
            line = line.rstrip()
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)
            m = re.match(r"frame=\s*(\d+)", line)
            if m and total:
                progress_signal.emit(min(int(m.group(1)), total), total)
        self._proc.wait()
        if self._proc.returncode != 0 and not self._cancel:
            self.log.emit(f"ffmpeg ({stage}) failed:")
            for t in tail[-15:]:
                self.log.emit("  " + t)
            raise RuntimeError(f"ffmpeg {stage} exited {self._proc.returncode}")
        self._proc = None

    @staticmethod
    def _shorten(c):
        return os.path.basename(c) if os.path.sep in c and os.path.exists(c) else c

    @Slot()
    def run(self):
        try:
            self._tmp = tempfile.mkdtemp(prefix="rembg_vid_")
            extract_dir = os.path.join(self._tmp, "extract")
            processed_dir = os.path.join(self._tmp, "processed")
            os.makedirs(extract_dir)
            os.makedirs(processed_dir)

            self._extract(extract_dir)
            if self._cancel:
                return self._cancel_finish()

            n = self._process(extract_dir, processed_dir)
            if self._cancel:
                return self._cancel_finish()
            if n == 0:
                raise RuntimeError("No frames were extracted/processed.")

            if self.mode == "video":
                location = self._export_video(processed_dir, n)
            else:
                location = self._export_png(processed_dir, n)

            self.finished.emit(True, location)
        except Exception as exc:
            self.log.emit(f"ERROR: {exc}")
            self.log.emit(traceback.format_exc())
            self.finished.emit(False, "")
        finally:
            if self._tmp and os.path.isdir(self._tmp):
                shutil.rmtree(self._tmp, ignore_errors=True)

    def _cancel_finish(self):
        self.log.emit("Cancelled by user.")
        self.finished.emit(False, "")

    # -- Step A: extract ----------------------------------------------------
    def _extract(self, extract_dir):
        total = self.info.get("frames_est", 0)
        self.log.emit("── Step A: extracting frames with ffmpeg …")
        cmd = [FFMPEG, "-y", "-i", self.video_path,
               "-start_number", "1",
               os.path.join(extract_dir, "frame_%06d.png"),
               "-progress", "pipe:1", "-nostats", "-loglevel", "error"]
        self._run_ffmpeg(cmd, total, self.extract_progress, "extract")
        count = len(self._frames(extract_dir))
        # make sure the bar reads 100%
        self.extract_progress.emit(count or 1, count or 1)
        self.log.emit(f"   extracted {count} frame(s).")

    @staticmethod
    def _frames(folder):
        return sorted(f for f in os.listdir(folder) if f.lower().endswith(".png"))

    # -- Step B: rembg ------------------------------------------------------
    def _process(self, extract_dir, processed_dir):
        frames = self._frames(extract_dir)
        total = len(frames)
        self.log.emit(f"── Step B: removing background from {total} frame(s) "
                      f"with '{self.model}' …")
        from rembg import new_session, remove
        from PIL import Image
        session = new_session(self.model)
        done = 0
        for i, fname in enumerate(frames, start=1):
            if self._cancel:
                break
            src = os.path.join(extract_dir, fname)
            dst = os.path.join(processed_dir, fname)
            try:
                with Image.open(src) as img:
                    img.load()
                    result = remove(
                        img, session=session,
                        alpha_matting=self.alpha_matting,
                        alpha_matting_foreground_threshold=self.fg,
                        alpha_matting_background_threshold=self.bg,
                        alpha_matting_erode_size=self.erode,
                    )
                    result.save(dst)
                done += 1
            except Exception as exc:
                self.log.emit(f"   [FAIL] {fname}: {exc}")
            self.process_progress.emit(i, total)
        self.log.emit(f"   processed {done}/{total} frame(s).")
        return done

    # -- Step C: export video ----------------------------------------------
    def _export_video(self, processed_dir, n):
        fps = self.fps_value if self.override_fps else self.info["fps"]
        stem = os.path.splitext(os.path.basename(self.video_path))[0]
        has_audio = self.info["has_audio"]

        if self.codec == "prores":
            ext, vargs = ".mov", ["-c:v", "prores_ks", "-profile:v", "4444",
                                  "-pix_fmt", "yuva444p10le"]
            self.log.emit("   ProRes 4444 selected — transparency is preserved.")
        else:
            # libx264 + yuv420p requires EVEN width/height; many real videos
            # (phone clips, screen recordings) have odd dimensions and would
            # otherwise fail with "width/height not divisible by 2". Pad to the
            # next even size so encoding always succeeds.
            ext, vargs = ".mp4", [
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            ]
            self.log.emit("   H.264 selected — transparency is flattened onto "
                          "black (H.264 has no alpha).")

        out_path = os.path.join(self.output_dir, f"{stem}_nobg{ext}")
        self.log.emit(f"── Step C: rebuilding video at {fps:g} fps → {out_path}")

        cmd = [FFMPEG, "-y", "-framerate", f"{fps}",
               "-i", os.path.join(processed_dir, "frame_%06d.png")]
        if has_audio:
            # Re-encode audio to AAC rather than '-c:a copy': copy fails when the
            # source audio codec is incompatible with the target container
            # (e.g. PCM from a .mov into an .mp4). AAC works in both .mp4 and
            # .mov and still preserves the audio content.
            cmd += ["-i", self.video_path, "-map", "0:v:0", "-map", "1:a:0",
                    "-c:a", "aac", "-b:a", "192k", "-shortest"]
            if self.override_fps:
                self.log.emit("   note: FPS overridden — audio may drift out of "
                              "sync relative to the new frame rate.")
        cmd += vargs + [out_path, "-progress", "pipe:1", "-nostats",
                        "-loglevel", "error"]
        self._run_ffmpeg(cmd, n, self.export_progress, "encode")
        self.export_progress.emit(n, n)
        self.log.emit("   done.")
        return out_path

    # -- Step C: export PNG frames -----------------------------------------
    def _export_png(self, processed_dir, n):
        stem = os.path.splitext(os.path.basename(self.video_path))[0]
        out_folder = os.path.join(self.output_dir, f"{stem}_frames")
        os.makedirs(out_folder, exist_ok=True)
        self.log.emit(f"── Step C: writing PNG frames → {out_folder}")

        frames = self._frames(processed_dir)
        total = len(frames)
        for i, fname in enumerate(frames, start=1):
            if self._cancel:
                break
            if self.png_numbering == "restart":
                out_name = f"frame_{i:05d}.png"
            else:  # keep original numbering from extraction
                out_name = fname
            shutil.copy2(os.path.join(processed_dir, fname),
                         os.path.join(out_folder, out_name))
            self.export_progress.emit(i, total)

        # FPS is recorded as metadata only (no video is built).
        meta = os.path.join(out_folder, "metadata.txt")
        with open(meta, "w") as fh:
            fh.write(f"source_video: {os.path.basename(self.video_path)}\n")
            fh.write(f"frame_count: {total}\n")
            fh.write(f"fps_metadata: {self.fps_value}\n")
            fh.write(f"numbering: {self.png_numbering}\n")
            fh.write("note: fps is metadata only; no video was rebuilt.\n")
        self.export_progress.emit(total, total)
        self.log.emit(f"   wrote {total} PNG(s) + metadata.txt "
                      f"(fps metadata = {self.fps_value}).")
        return out_folder


class VideoTab(QWidget):
    def __init__(self):
        super().__init__()
        self.video_path: str | None = None
        self.info: dict | None = None
        self.output_dir: str | None = None
        self.thread: QThread | None = None
        self.worker: VideoWorker | None = None
        self._build_ui()
        self._update_enabled()

    def _build_ui(self):
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        root.addLayout(left, 3)

        in_box = QGroupBox("Input video")
        in_layout = QVBoxLayout(in_box)
        b_select = QPushButton("Select video…")
        b_select.clicked.connect(self.select_video)
        in_layout.addWidget(b_select)
        self.list = DropList()
        self.list.files_dropped.connect(self.on_files_dropped)
        self.list.setMaximumHeight(70)
        in_layout.addWidget(self.list)
        self.info_label = QLabel("No video selected.")
        self.info_label.setStyleSheet("color: gray;")
        in_layout.addWidget(self.info_label)
        hint = QLabel("Tip: drag & drop a single video file here.")
        hint.setStyleSheet("color: gray;")
        in_layout.addWidget(hint)
        left.addWidget(in_box)

        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        log_layout.addWidget(self.log_view)
        left.addWidget(log_box, 1)

        right = QVBoxLayout()
        root.addLayout(right, 2)

        # model
        model_box = QGroupBox("Model")
        ml = QVBoxLayout(model_box)
        self.model_combo = QComboBox()
        self.model_combo.addItems(MODELS)
        self.model_combo.setCurrentText(DEFAULT_MODEL)
        ml.addWidget(self.model_combo)
        right.addWidget(model_box)

        # alpha
        am_box, self.am_controls, self.am_check, sliders = build_alpha_group()
        self.s_fg, self.s_bg, self.s_erode = sliders
        right.addWidget(am_box)

        # output format
        fmt_box = QGroupBox("Output Format")
        fl = QVBoxLayout(fmt_box)
        self.radio_video = QRadioButton("Export as Video")
        self.radio_png = QRadioButton("Export as PNG Frames")
        self.radio_video.setChecked(True)
        self.fmt_group = QButtonGroup(self)
        self.fmt_group.addButton(self.radio_video)
        self.fmt_group.addButton(self.radio_png)
        self.radio_video.toggled.connect(self._update_enabled)
        fl.addWidget(self.radio_video)
        fl.addWidget(self.radio_png)

        # video-only options
        self.video_opts = QWidget()
        vo = QGridLayout(self.video_opts)
        vo.setContentsMargins(20, 0, 0, 0)
        vo.addWidget(QLabel("Codec:"), 0, 0)
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["H.264 (.mp4)", "ProRes 4444 (.mov, keeps alpha)"])
        vo.addWidget(self.codec_combo, 0, 1)
        self.override_check = QCheckBox("Override original FPS")
        self.override_check.toggled.connect(self._update_enabled)
        vo.addWidget(self.override_check, 1, 0, 1, 2)
        fl.addWidget(self.video_opts)

        # png-only options
        self.png_opts = QWidget()
        po = QGridLayout(self.png_opts)
        po.setContentsMargins(20, 0, 0, 0)
        po.addWidget(QLabel("Numbering:"), 0, 0)
        self.numbering_combo = QComboBox()
        self.numbering_combo.addItems(
            ["Keep original numbering", "Restart from 00001"])
        po.addWidget(self.numbering_combo, 0, 1)
        fl.addWidget(self.png_opts)

        # shared FPS row
        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("FPS:"))
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 240)
        self.fps_spin.setValue(30)
        fps_row.addWidget(self.fps_spin)
        fps_row.addStretch(1)
        fl.addLayout(fps_row)
        right.addWidget(fmt_box)

        # output folder
        out_box = QGroupBox("Output folder")
        ol = QVBoxLayout(out_box)
        b_out = QPushButton("Choose output folder…")
        b_out.clicked.connect(self.choose_output)
        ol.addWidget(b_out)
        self.out_label = QLabel("No output folder selected")
        self.out_label.setWordWrap(True)
        self.out_label.setStyleSheet("color: gray;")
        ol.addWidget(self.out_label)
        right.addWidget(out_box)

        right.addStretch(1)

        # progress bars
        prog_box = QGroupBox("Progress")
        pl = QGridLayout(prog_box)
        self.p_extract = QProgressBar()
        self.p_process = QProgressBar()
        self.p_export = QProgressBar()
        pl.addWidget(QLabel("1. Extract"), 0, 0)
        pl.addWidget(self.p_extract, 0, 1)
        pl.addWidget(QLabel("2. Remove bg"), 1, 0)
        pl.addWidget(self.p_process, 1, 1)
        pl.addWidget(QLabel("3. Export"), 2, 0)
        pl.addWidget(self.p_export, 2, 1)
        right.addWidget(prog_box)

        self.process_btn = QPushButton("Process")
        self.process_btn.setMinimumHeight(40)
        self.process_btn.clicked.connect(self.start_processing)
        right.addWidget(self.process_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_processing)
        right.addWidget(self.cancel_btn)

    # -- enable/disable logic ----------------------------------------------
    def _update_enabled(self, *_):
        is_video = self.radio_video.isChecked()
        self.video_opts.setEnabled(is_video)
        self.png_opts.setEnabled(not is_video)
        # FPS spinbox: always for PNG (metadata); for video only when overriding.
        if is_video:
            self.fps_spin.setEnabled(self.override_check.isChecked())
        else:
            self.fps_spin.setEnabled(True)

    # -- input handling -----------------------------------------------------
    def select_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select video", "",
            "Videos (*.mp4 *.mov *.m4v *.avi *.mkv *.webm *.mpg *.mpeg *.wmv *.flv)")
        if path:
            self._set_video(path)

    @Slot(list)
    def on_files_dropped(self, paths):
        for p in paths:
            if os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTS:
                self._set_video(p)
                return
        self.log_view.appendPlainText("Dropped item is not a supported video.")

    def _set_video(self, path):
        self.video_path = path
        self.list.clear()
        self.list.addItem(QListWidgetItem(path))
        try:
            self.info = probe_video(path)
            self.info_label.setStyleSheet("color: black;")
            self.info_label.setText(
                f"{self.info['fps']:g} fps · {self.info['duration']:.1f}s · "
                f"~{self.info['frames_est']} frames · "
                f"audio: {'yes' if self.info['has_audio'] else 'no'}")
            self.fps_spin.setValue(max(1, min(240, round(self.info['fps']))))
            self.log_view.appendPlainText(f"Loaded: {path}")
        except Exception as exc:
            self.info = None
            self.info_label.setText(f"Could not probe video: {exc}")
            self.info_label.setStyleSheet("color: #b00;")

    def choose_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self.output_dir = folder
            self.out_label.setText(folder)
            self.out_label.setStyleSheet("color: black;")

    # -- processing ---------------------------------------------------------
    def start_processing(self):
        if not self.video_path or not self.info:
            QMessageBox.warning(self, "No video", "Select a valid video first.")
            return
        if not self.output_dir:
            QMessageBox.warning(self, "No output", "Choose an output folder first.")
            return

        self.process_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        for bar in (self.p_extract, self.p_process, self.p_export):
            bar.setValue(0)
        self.log_view.appendPlainText("──────── starting ────────")

        mode = "video" if self.radio_video.isChecked() else "png"
        codec = "prores" if self.codec_combo.currentIndex() == 1 else "h264"
        numbering = "restart" if self.numbering_combo.currentIndex() == 1 else "keep"

        self.thread = QThread()
        self.worker = VideoWorker(
            video_path=self.video_path, info=self.info,
            model=self.model_combo.currentText(),
            alpha_matting=self.am_check.isChecked(),
            fg=self.s_fg.value(), bg=self.s_bg.value(), erode=self.s_erode.value(),
            mode=mode, output_dir=self.output_dir, codec=codec,
            override_fps=self.override_check.isChecked(),
            fps_value=self.fps_spin.value(), png_numbering=numbering,
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.extract_progress.connect(
            lambda d, t: self._set_bar(self.p_extract, d, t))
        self.worker.process_progress.connect(
            lambda d, t: self._set_bar(self.p_process, d, t))
        self.worker.export_progress.connect(
            lambda d, t: self._set_bar(self.p_export, d, t))
        self.worker.log.connect(self.log_view.appendPlainText)
        self.worker.finished.connect(self.on_finished)
        self.thread.start()

    def _set_bar(self, bar, done, total):
        bar.setMaximum(max(1, total))
        bar.setValue(done)

    def cancel_processing(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)

    @Slot(bool, str)
    def on_finished(self, success, location):
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread = self.worker = None
        self.process_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if success:
            self.log_view.appendPlainText(f"──────── DONE → {location} ────────")
            box = QMessageBox(self)
            box.setWindowTitle("Finished")
            box.setText(f"Output saved to:\n{location}")
            reveal = box.addButton("Reveal in Finder", QMessageBox.AcceptRole)
            box.addButton("OK", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is reveal:
                target = location if os.path.isdir(location) else os.path.dirname(location)
                subprocess.run(["open", "-R", location] if os.path.exists(location)
                               else ["open", target])
        else:
            self.log_view.appendPlainText("──────── stopped (see log) ────────")


# ============================================================================
# MAIN WINDOW
# ============================================================================


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("rembg — Background Remover")
        self.resize(960, 720)
        tabs = QTabWidget()
        tabs.addTab(ImageTab(), "Images")
        tabs.addTab(VideoTab(), "Video")
        self.setCentralWidget(tabs)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("rembg GUI")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
