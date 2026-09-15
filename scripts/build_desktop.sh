#!/usr/bin/env bash
# Build the AudioConverter desktop app for macOS or Linux.
#   macOS  -> dist/AudioConverter.app
#   Linux  -> dist/AudioConverter/
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=${PYTHON:-python3}
VENV=".venv-desktop"

echo "==> Creating build venv at $VENV (${PYTHON})"
"$PYTHON" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> Installing app dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt -r requirements-desktop.txt
pip install --quiet pyinstaller

echo "==> Building with PyInstaller"
pyinstaller --noconfirm --clean desktop.spec

echo
echo "Build complete. Artifact:"
find dist -maxdepth 1 -name 'AudioConverter*' -print