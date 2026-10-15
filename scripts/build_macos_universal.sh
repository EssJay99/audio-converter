#!/bin/bash
# Build a universal2 (arm64 + x86_64) macOS bundle in ONE PyInstaller pass.
#
# This works only with a universal2 Python interpreter (e.g. a python.org
# macOS installer, whose framework builds ship both arches): PyInstaller
# cannot cross-compile from a single-arch interpreter. Both ffmpeg arches
# are bundled beside the app and picked at runtime by platform.machine().
#
# Usage:
#   PYTHON_BIN=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
#       bash scripts/build_macos_universal.sh
#   # or rely on ./.venv-universal (created below from that interpreter)
#
# Output: dist/AudioConverter.app  (universal Mach-O, lipo-verified)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY_CANDIDATES=(
    "${PYTHON_BIN:-}"
    "$ROOT/.venv-universal/bin/python"
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
)
PY=""
for candidate in "${PY_CANDIDATES[@]}"; do
    [ -n "$candidate" ] || continue
    if [ -x "$candidate" ]; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "error: no universal2-capable Python found." >&2
    echo "Install a python.org macOS build (universal2) or set PYTHON_BIN." >&2
    exit 1
fi
REAL_PY="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$PY" 2>/dev/null || echo "$PY")"
if ! lipo -archs "$REAL_PY" 2>/dev/null | grep -q "x86_64" || \
   ! lipo -archs "$REAL_PY" 2>/dev/null | grep -q "arm64"; then
    echo "error: $PY is not universal2 (lipo: $(lipo -archs "$REAL_PY" 2>/dev/null || echo unknown))." >&2
    echo "PyInstaller --target-arch universal2 needs a universal2 interpreter" >&2
    echo "(python.org macOS installers qualify; Homebrew Pythons do not)." >&2
    exit 1
fi
echo "==> universal2 interpreter: $PY"

if [ ! -x "$ROOT/.venv-universal/bin/python" ]; then
    echo "==> creating .venv-universal"
    "$PY" -m venv "$ROOT/.venv-universal"
    "$ROOT/.venv-universal/bin/pip" install --quiet --upgrade pip
    "$ROOT/.venv-universal/bin/pip" install --quiet \
        -r requirements.txt -r requirements-desktop.txt pyinstaller pytest
fi

# Upstream yt-dlp_macos must itself be fat, or Intel Macs get a broken helper.
if [ -x build_helpers/yt-dlp ]; then
    if ! lipo -archs build_helpers/yt-dlp 2>/dev/null | grep -q "x86_64" || \
       ! lipo -archs build_helpers/yt-dlp 2>/dev/null | grep -q "arm64"; then
        echo "==> cached yt-dlp is single-arch; re-downloading"
        rm -f build_helpers/yt-dlp
    fi
fi

PYTHON_BIN="$ROOT/.venv-universal/bin/python" UNIVERSAL=1 \
    bash scripts/build_macos.sh

APP_BIN="$ROOT/dist/AudioConverter.app/Contents/MacOS/AudioConverter"
echo "==> verifying universal binary"
ARCHS="$(lipo -archs "$APP_BIN")"
echo "    $APP_BIN: $ARCHS"
if [[ "$ARCHS" != *"x86_64"* || "$ARCHS" != *"arm64"* ]]; then
    echo "error: bundle is not universal2 ($ARCHS)" >&2
    exit 1
fi
for helper in ffmpeg-arm64 ffmpeg-x86_64; do
    if [ ! -x "build_helpers/$helper" ]; then
        echo "error: missing build_helpers/$helper" >&2
        exit 1
    fi
done
echo "==> universal build OK; run bash scripts/build_dmg.sh for the disk image."
