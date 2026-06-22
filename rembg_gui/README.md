# rembg GUI

A simple, local macOS desktop app that wraps **rembg** behind a graphical
interface so you can remove image **and video** backgrounds without using the
terminal. The window has two tabs: **Images** and **Video**.

- **Toolkit:** Python + PySide6 (Qt) — the most stable option here, with native
  drag-and-drop. (Tkinter was ruled out: your Homebrew Python 3.12 has no Tcl/Tk
  binding.)
- **Engine:** calls the rembg **Python API** directly (`new_session` + `remove`),
  not shell commands. Video frame extraction and encoding use **ffmpeg via
  subprocess**.
- **No admin rights required.** Everything runs inside your existing virtual
  environment.

## Images tab — features

- Add a single image, multiple images, or an entire folder (recursively).
- Drag & drop images or folders onto the list.
- Model dropdown: `u2net`, `u2netp`, `isnet-general-use`, `isnet-anime`
  (default: **isnet-general-use**).
- Alpha matting toggle with sliders:
  - Foreground threshold (0–255, default 240)
  - Background threshold (0–255, default 10)
  - Erode size (0–50, default 5)
- Choose an output folder, with optional **Preserve folder structure** for
  batch jobs.
- Process button, progress bar, live log window, and per-image error handling
  (one bad file does not stop the batch). Cancel button to stop early.
- Processing runs on a background thread, so the window never freezes.

## Video tab — features

Removes the background from every frame of a video and re-exports it.

- **Input:** select a single video file, or drag & drop one onto the list.
  Detected fps / duration / frame count / audio are shown after loading.
- **Model + alpha matting:** same controls as the Images tab.
- **Output Format** (radio buttons):
  - **Export as Video** — rebuilds a processed video with ffmpeg.
    - Preserves the **original fps** unless you tick *Override original FPS*.
    - **Preserves audio** (copied from the source).
    - **Codec:** *H.264 (.mp4)* or *ProRes 4444 (.mov)*.
      - ProRes 4444 **keeps transparency** (alpha channel).
      - H.264 has no alpha, so transparency is flattened onto black. Odd frame
        dimensions are auto-padded to even (libx264 requires even sizes).
    - Audio is re-encoded to AAC so export never fails on an exotic source
      audio codec (e.g. PCM in a QuickTime .mov).
  - **Export as PNG Frames** — saves every processed frame as a transparent PNG
    into `<output>/<videoname>_frames/`, plus a `metadata.txt`.
    - **FPS** field is recorded as *metadata only* (written to `metadata.txt`);
      no video is rebuilt.
    - **Numbering:** *Keep original numbering* or *Restart from 00001*.
- **Pipeline:** Step A extract frames (ffmpeg) → Step B remove background per
  frame (rembg) → Step C export. Each step has its own progress bar.
- Detailed log window and a Cancel button.

### About the FPS field (reconciling the two spec points)

The spec asked for an FPS field "only enabled in PNG mode", but also for an FPS
**override** in Video mode. Both are satisfied with one FPS spinbox:

- **PNG mode:** the spinbox is always enabled — it's the naming/metadata fps.
- **Video mode:** the spinbox is disabled by default (original fps is preserved)
  and becomes enabled only when you tick *Override original FPS*.

## Requirements

- **ffmpeg / ffprobe** must be installed (used for the Video tab). On macOS:
  `brew install ffmpeg` (no admin rights needed). Verified present on this
  machine at `/opt/homebrew/bin/ffmpeg`.

### Python environment

This app expects the virtual environment that already has rembg installed:

```
/Users/reneturek/Projects/GitHub/rembg/venv
```

PySide6 is the only extra dependency, and it is already installed in that venv.
If you ever recreate the venv, reinstall it with:

```bash
/Users/reneturek/Projects/GitHub/rembg/venv/bin/pip install PySide6
```

## How to run

**Option A — convenience script (recommended):**

```bash
cd /Users/reneturek/Projects/GitHub/rembg
chmod +x rembg_gui/run.sh    # first time only
./rembg_gui/run.sh
```

**Option B — directly with the venv Python:**

```bash
cd /Users/reneturek/Projects/GitHub/rembg
./venv/bin/python rembg_gui/app.py
```

**Option C — with the venv activated:**

```bash
cd /Users/reneturek/Projects/GitHub/rembg
source venv/bin/activate
python rembg_gui/app.py
```

> **First run of each model** downloads its ONNX weights (~170–180 MB) to
> `~/.u2net/`. This is automatic but needs an internet connection the first
> time. Later runs are offline.

## Typical workflow

1. Add images (button or drag & drop).
2. Pick a model (default `isnet-general-use` is a good general choice).
3. Optionally enable alpha matting and tweak the sliders for cleaner edges.
4. Click **Choose output folder…**.
5. (Batch) Tick **Preserve folder structure** to mirror input subfolders.
6. Click **Process**. Watch the progress bar and log. Outputs are saved as
   transparent `.png` files.

## Optional: build a double-clickable .app bundle

```bash
cd /Users/reneturek/Projects/GitHub/rembg
chmod +x rembg_gui/build_app.sh   # first time only
./rembg_gui/build_app.sh
open dist/rembg-GUI.app
```

This uses PyInstaller (installed on demand into the venv). The bundle is large
(~700 MB+) because it embeds PySide6, onnxruntime, numpy/scipy, etc. Models are
still fetched to `~/.u2net/` on first use, so the first run needs internet.

## Files

- `app.py` — the application (UI + rembg worker thread).
- `run.sh` — launches the app with the project venv.
- `build_app.sh` — optional macOS `.app` packaging via PyInstaller.
