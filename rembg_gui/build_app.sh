#!/bin/bash
# Optional: package the rembg GUI as a standalone macOS .app bundle using
# PyInstaller. Produces dist/rembg-GUI.app — double-clickable, no terminal.
#
# NOTE: the resulting bundle is large (~700 MB+) because it embeds PySide6,
# onnxruntime, numpy/scipy, etc. Models are still downloaded on first use to
# ~/.u2net, so the machine running the .app needs internet access the first
# time each model is used.
#
# ffmpeg/ffprobe are NOT bundled — the Video tab calls them from PATH, so the
# target machine still needs `brew install ffmpeg`.
#
# Usage:
#   ./rembg_gui/build_app.sh
set -e

PROJECT_DIR="/Users/reneturek/Projects/GitHub/rembg"
VENV_PY="$PROJECT_DIR/venv/bin/python"

cd "$PROJECT_DIR"

# PyInstaller is only needed for packaging — install it into the venv on demand.
"$VENV_PY" -m pip install --upgrade pyinstaller >/dev/null

# Build. --collect-all pulls in data files / hidden imports that these
# packages load dynamically (model metadata, native libs, etc.).
"$VENV_PY" -m PyInstaller \
  --name "rembg-GUI" \
  --windowed \
  --noconfirm \
  --clean \
  --collect-all rembg \
  --collect-all onnxruntime \
  --collect-all pymatting \
  --collect-all skimage \
  --collect-all scipy \
  --collect-all numba \
  --collect-all llvmlite \
  rembg_gui/app.py

echo
echo "Done. App bundle is at: $PROJECT_DIR/dist/rembg-GUI.app"
echo "Launch it with: open dist/rembg-GUI.app"
