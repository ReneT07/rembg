#!/bin/bash
# Launch the rembg GUI using the project virtual environment.
# Works regardless of the current directory.
set -e

PROJECT_DIR="/Users/reneturek/Projects/GitHub/rembg"
VENV_PY="$PROJECT_DIR/venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "Virtual environment not found at $PROJECT_DIR/venv" >&2
  echo "Create it first (see README.md)." >&2
  exit 1
fi

# Run from the project root so 'rembg_gui' is importable.
cd "$PROJECT_DIR"
exec "$VENV_PY" rembg_gui/app.py "$@"
