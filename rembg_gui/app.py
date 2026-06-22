#!/usr/bin/env python3
"""
rembg GUI — a simple macOS desktop app to remove image backgrounds.

Wraps the rembg *Python API* (no shell calls) and is meant to run inside the
virtual environment at /Users/reneturek/Projects/GitHub/rembg/venv.

Run:
    /Users/reneturek/Projects/GitHub/rembg/venv/bin/python rembg_gui/app.py
"""

import os
import sys
import traceback
from dataclasses import dataclass

from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
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
    QSlider,
    QVBoxLayout,
    QWidget,
)

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

# Models exposed in the dropdown (label is what the user sees; value is the
# rembg model name passed to new_session()).
MODELS = ["u2net", "u2netp", "isnet-general-use", "isnet-anime"]
DEFAULT_MODEL = "isnet-general-use"

# File extensions we treat as processable images.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}


@dataclass(frozen=True)
class InputItem:
    """A single image to process.

    `path` is the absolute source file. `root` is the directory used as the
    base when 'Preserve folder structure' is enabled — the output mirrors the
    relative path of `path` under `root`.
    """

    path: str
    root: str


# ----------------------------------------------------------------------------
# Worker — runs rembg off the UI thread so the window never freezes
# ----------------------------------------------------------------------------


class Worker(QObject):
    progress = Signal(int, int)        # done, total
    log = Signal(str)                  # human-readable log line
    finished = Signal(int, int)        # succeeded, failed

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
        """Compute the destination .png path for a given input item."""
        if self.preserve_structure:
            rel = os.path.relpath(item.path, item.root)
            rel = os.path.splitext(rel)[0] + ".png"
            dest = os.path.join(self.output_dir, rel)
        else:
            name = os.path.splitext(os.path.basename(item.path))[0] + ".png"
            dest = os.path.join(self.output_dir, name)
        return dest

    @Slot()
    def run(self):
        succeeded = 0
        failed = 0
        total = len(self.items)

        # Import rembg here (inside the worker thread) so any heavy import or
        # model load does not block the UI from showing up.
        try:
            self.log.emit(f"Loading model '{self.model}' …")
            from rembg import new_session, remove
            from PIL import Image
            session = new_session(self.model)
            self.log.emit("Model ready. Starting …")
        except Exception as exc:  # pragma: no cover - environment dependent
            self.log.emit(f"FATAL: could not initialize rembg: {exc}")
            self.log.emit(traceback.format_exc())
            self.finished.emit(0, total)
            return

        for i, item in enumerate(self.items, start=1):
            if self._cancel:
                self.log.emit("Cancelled by user.")
                break
            try:
                dest = self._output_path(item)
                os.makedirs(os.path.dirname(dest), exist_ok=True)

                with Image.open(item.path) as img:
                    img.load()
                    result = remove(
                        img,
                        session=session,
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


# ----------------------------------------------------------------------------
# Drag-and-drop enabled list
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


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("rembg — Background Remover")
        self.resize(900, 680)

        self.items: dict[str, InputItem] = {}   # path -> InputItem (dedup)
        self.output_dir: str | None = None
        self.thread: QThread | None = None
        self.worker: Worker | None = None

        self._build_ui()

    # -- UI construction ----------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        root = QHBoxLayout(central)

        # Left column: inputs + file list
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

        hint = QLabel("Tip: you can also drag & drop images or folders here.")
        hint.setStyleSheet("color: gray;")
        in_layout.addWidget(hint)

        left.addWidget(in_box, 3)

        # Log window
        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        log_layout.addWidget(self.log_view)
        left.addWidget(log_box, 2)

        # Right column: options
        right = QVBoxLayout()
        root.addLayout(right, 2)

        # Model
        model_box = QGroupBox("Model")
        model_layout = QVBoxLayout(model_box)
        self.model_combo = QComboBox()
        self.model_combo.addItems(MODELS)
        self.model_combo.setCurrentText(DEFAULT_MODEL)
        model_layout.addWidget(self.model_combo)
        right.addWidget(model_box)

        # Alpha matting
        am_box = QGroupBox("Alpha matting")
        am_layout = QVBoxLayout(am_box)
        self.am_check = QCheckBox("Enable alpha matting")
        self.am_check.toggled.connect(self._toggle_alpha)
        am_layout.addWidget(self.am_check)

        self.am_controls = QWidget()
        grid = QGridLayout(self.am_controls)
        self.s_fg, self.l_fg = self._make_slider(0, 255, 240)
        self.s_bg, self.l_bg = self._make_slider(0, 255, 10)
        self.s_erode, self.l_erode = self._make_slider(0, 50, 5)
        grid.addWidget(QLabel("Foreground threshold"), 0, 0)
        grid.addWidget(self.s_fg, 0, 1)
        grid.addWidget(self.l_fg, 0, 2)
        grid.addWidget(QLabel("Background threshold"), 1, 0)
        grid.addWidget(self.s_bg, 1, 1)
        grid.addWidget(self.l_bg, 1, 2)
        grid.addWidget(QLabel("Erode size"), 2, 0)
        grid.addWidget(self.s_erode, 2, 1)
        grid.addWidget(self.l_erode, 2, 2)
        am_layout.addWidget(self.am_controls)
        self.am_controls.setEnabled(False)
        right.addWidget(am_box)

        # Output
        out_box = QGroupBox("Output")
        out_layout = QVBoxLayout(out_box)
        out_row = QHBoxLayout()
        b_out = QPushButton("Choose output folder…")
        b_out.clicked.connect(self.choose_output)
        out_row.addWidget(b_out)
        out_layout.addLayout(out_row)
        self.out_label = QLabel("No output folder selected")
        self.out_label.setWordWrap(True)
        self.out_label.setStyleSheet("color: gray;")
        out_layout.addWidget(self.out_label)
        self.preserve_check = QCheckBox("Preserve folder structure (batch)")
        out_layout.addWidget(self.preserve_check)
        right.addWidget(out_box)

        right.addStretch(1)

        # Process + progress
        self.process_btn = QPushButton("Process")
        self.process_btn.setMinimumHeight(40)
        self.process_btn.clicked.connect(self.start_processing)
        right.addWidget(self.process_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_processing)
        right.addWidget(self.cancel_btn)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        right.addWidget(self.progress)

        self.setCentralWidget(central)

    def _make_slider(self, lo, hi, default):
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(default)
        label = QLabel(str(default))
        label.setMinimumWidth(32)
        s.valueChanged.connect(lambda v, lbl=label: lbl.setText(str(v)))
        return s, label

    def _toggle_alpha(self, on):
        self.am_controls.setEnabled(on)

    # -- Input handling -----------------------------------------------------

    def _add_paths(self, paths, root_for_files=None):
        """Add a mix of files and directories. Directories are walked."""
        added = 0
        for p in paths:
            if not p:
                continue
            if os.path.isdir(p):
                base = os.path.dirname(os.path.normpath(p))  # parent of folder
                for dirpath, _dirs, files in os.walk(p):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                            full = os.path.join(dirpath, f)
                            added += self._add_one(full, base)
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
        item = QListWidgetItem(path)
        self.list.addItem(item)
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
            path = item.text()
            self.items.pop(path, None)
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

    # -- Processing ---------------------------------------------------------

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

        items = list(self.items.values())

        self.thread = QThread()
        self.worker = Worker(
            items=items,
            model=self.model_combo.currentText(),
            output_dir=self.output_dir,
            preserve_structure=self.preserve_check.isChecked(),
            alpha_matting=self.am_check.isChecked(),
            fg=self.s_fg.value(),
            bg=self.s_bg.value(),
            erode=self.s_erode.value(),
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self.on_log)
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

    @Slot(str)
    def on_log(self, line):
        self.log_view.appendPlainText(line)

    @Slot(int, int)
    def on_finished(self, succeeded, failed):
        self.log_view.appendPlainText(
            f"──────── done: {succeeded} ok, {failed} failed ────────")
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread = None
        self.worker = None
        self.process_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("rembg GUI")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
