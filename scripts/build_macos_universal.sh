#!/bin/bash
# Build a universal (arm64 + x86_64) macOS bundle.
#
# Cross-arch PyInstaller needs each arch's Python. On Apple Silicon we
# use `arch -x86_64` to run an Intel Python under Rosetta; on Intel
# hosts we use `arch -arm64` to invoke an arm64 Python (e.g. from
# Homebrew at /opt/homebrew/opt/python@3.12/bin/python3).
#
# Usage:  bash scripts/build_macos_universal.sh
# Output: dist/AudioConverter.app  (universal Mach-O)
#
# Notes:
# - Set HOMEBREW_PYTHON_X86 if your x86_64 Python lives outside
#   /usr/local/bin/python3 or /Library/Frameworks/Python.framework.
# - Set HOMEBREW_PYTHON_ARM64 if your arm64 Python lives outside
#   /opt/homebrew/opt/python@3.12/bin/python3.
# - Both Python interpreters need `pyinstaller` installed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Detect host arch — we build the *other* arch under Rosetta/arch(1).
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    arm64)  ARM_BIN=""; X86_BIN="${HOMEBREW_PYTHON_X86:-/usr/local/bin/python3}" ;;
    x86_64) ARM_BIN="${HOMEBREW_PYTHON_ARM64:-/opt/homebrew/opt/python@3.12/bin/python3}"; X86_BIN="" ;;
    *)
        echo "error: unsupported host arch: $HOST_ARCH" >&2
        exit 1 ;;
esac
OTHER_ARCH="x86_64"
[[ "$HOST_ARCH" == "x86_64" ]] && OTHER_ARCH="arm64"

mkdir -p dist_universal build_universal
rm -rf build_universal/* dist_universal/*

build_one() {
    local arch="$1"
    local py="$2"
    echo "==> building for $arch using $py"
    if [ ! -x "$py" ]; then
        echo "error: $py not found" >&2
        exit 1
    fi
    local venv="build_universal/venv-$arch"
    "$py" -m venv "$venv"
    "$venv/bin/pip" install --quiet --upgrade pip
    "$venv/bin/pip" install --quiet -r requirements.txt pyinstaller
    bash scripts/build_macos.sh  # runs pytest + pyinstaller
    # Move the built .app out of dist/ to a per-arch location.
    rm -rf "dist_universal/AudioConverter-$arch.app"
    cp -R dist/AudioConverter.app "dist_universal/AudioConverter-$arch.app"
}

if [ -n "$ARM_BIN" ]; then
    build_one arm64 "$ARM_BIN"
fi
if [ -n "$X86_BIN" ]; then
    build_one x86_64 "$X86_BIN"
fi

# Locate the Mach-O binaries inside each arch's .app.
HOST_APP="$ROOT/dist_universal/AudioConverter-$HOST_ARCH.app"
OTHER_APP="$ROOT/dist_universal/AudioConverter-$OTHER_ARCH.app"
HOST_BIN="$HOST_APP/Contents/MacOS/AudioConverter"
OTHER_BIN="$OTHER_APP/Contents/MacOS/AudioConverter"
if [ ! -x "$HOST_BIN" ] || [ ! -x "$OTHER_BIN" ]; then
    echo "error: one of the arch builds failed — see $HOST_APP / $OTHER_APP" >&2
    exit 1
fi

# lipo the two binaries into one universal.
echo "==> lipo -create $HOST_BIN $OTHER_BIN"
lipo -create -arch "$HOST_ARCH" "$HOST_BIN" -arch "$OTHER_ARCH" "$OTHER_BIN" \
    -output "$HOST_BIN"
file "$HOST_BIN"
echo "==> universal build ready at $HOST_APP"
echo "    Run bash scripts/build_dmg.sh for the installer disk image."