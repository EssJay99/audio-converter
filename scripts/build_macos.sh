#!/bin/bash
# Build a distributable macOS bundle for the Audio Converter desktop app.
#
# Usage:  bash scripts/build_macos.sh
# Output: dist/AudioConverter/AudioConverter
#
# Install:  1. `brew install ffmpeg` (once; conversions need it)
#           2. Copy dist/AudioConverter to /Applications
#           3. Double-click AudioConverter (notarized builds open directly;
#              unsigned builds: right-click > Open once to clear Gatekeeper)
#           4. Optional login start: copy scripts/com.audioconverter.app.plist
#              to ~/Library/LaunchAgents and run `launchctl load` on it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# PYTHON_BIN overrides the interpreter (universal builds point at a
# universal2 Python); UNIVERSAL=1 fetches both ffmpeg arches and tells
# PyInstaller to emit a universal2 binary (requires that interpreter
# to be universal2 itself — plain arm64/x86_64 Pythons can't do this).
PY="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
    echo "error: $PY not found; create the venv first" >&2
    exit 1
fi

mkdir -p build_helpers

# Standalone yt-dlp binary (no Python needed at runtime).
if [ ! -x build_helpers/yt-dlp ]; then
    echo "==> downloading yt-dlp_macos"
    curl -sL --max-time 120 -o build_helpers/yt-dlp \
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
    chmod +x build_helpers/yt-dlp
else
    echo "==> yt-dlp helper already present"
fi

# Static ffmpeg binary so users never touch the command line.
# (ffprobe is not needed: the app parses `ffmpeg -i` output instead.)
# UNIVERSAL=1 fetches both arches as ffmpeg-arm64/ffmpeg-x86_64 (the app
# picks at runtime); otherwise a single host-arch `ffmpeg` is fetched.
if [ "${UNIVERSAL:-0}" = "1" ]; then
    if [ ! -x build_helpers/ffmpeg-arm64 ]; then
        echo "==> downloading static ffmpeg (arm64)"
        curl -sL --max-time 180 -o build_helpers/ffmpeg-arm64.zip \
            "https://www.osxexperts.net/ffmpeg80arm.zip"
        python3 -c "import zipfile; zipfile.ZipFile('build_helpers/ffmpeg-arm64.zip').extract('ffmpeg', 'build_helpers/tmp_ff_arm')"
        mv build_helpers/tmp_ff_arm/ffmpeg build_helpers/ffmpeg-arm64
        rm -rf build_helpers/tmp_ff_arm build_helpers/ffmpeg-arm64.zip
        chmod +x build_helpers/ffmpeg-arm64
        file build_helpers/ffmpeg-arm64
    else
        echo "==> ffmpeg arm64 helper already present"
    fi
    if [ ! -x build_helpers/ffmpeg-x86_64 ]; then
        echo "==> downloading static ffmpeg (x86_64)"
        curl -sL --max-time 180 -o build_helpers/ffmpeg-x86_64.zip \
            "https://evermeet.cx/ffmpeg/ffmpeg-9.0.1.zip"
        python3 -c "import zipfile; zipfile.ZipFile('build_helpers/ffmpeg-x86_64.zip').extract('ffmpeg', 'build_helpers/tmp_ff_x86')"
        mv build_helpers/tmp_ff_x86/ffmpeg build_helpers/ffmpeg-x86_64
        rm -rf build_helpers/tmp_ff_x86 build_helpers/ffmpeg-x86_64.zip
        chmod +x build_helpers/ffmpeg-x86_64
        file build_helpers/ffmpeg-x86_64
    else
        echo "==> ffmpeg x86_64 helper already present"
    fi
elif [ ! -x build_helpers/ffmpeg ]; then
    echo "==> downloading static ffmpeg for $(uname -m)"
    if [ "$(uname -m)" = "arm64" ]; then
        curl -sL --max-time 180 -o build_helpers/ffmpeg.zip \
            "https://www.osxexperts.net/ffmpeg80arm.zip"
    else
        curl -sL --max-time 180 -o build_helpers/ffmpeg.zip \
            "https://evermeet.cx/ffmpeg/ffmpeg-9.0.1.zip"
    fi
    python3 -c "import zipfile; zipfile.ZipFile('build_helpers/ffmpeg.zip').extract('ffmpeg', 'build_helpers/tmp_ff')"
    mv build_helpers/tmp_ff/ffmpeg build_helpers/ffmpeg
    rm -rf build_helpers/tmp_ff build_helpers/ffmpeg.zip
    chmod +x build_helpers/ffmpeg
    file build_helpers/ffmpeg
else
    echo "==> ffmpeg helper already present"
fi

echo "==> running test suite before packaging"
"$PY" -m pytest tests/ -q

echo "==> building with PyInstaller"
if [ "${UNIVERSAL:-0}" = "1" ]; then
    "$PY" -m PyInstaller audio-converter.spec \
        --noconfirm --clean --target-arch universal2 \
        --distpath "$ROOT/dist" --workpath "$ROOT/build"
else
    "$PY" -m PyInstaller audio-converter.spec \
        --noconfirm --clean --distpath "$ROOT/dist" --workpath "$ROOT/build"
fi

BIN="$ROOT/dist/AudioConverter/AudioConverter"
APP_BIN="$ROOT/dist/AudioConverter.app/Contents/MacOS/AudioConverter"
if [ ! -x "$APP_BIN" ]; then
    echo "error: bundle binary missing at $APP_BIN" >&2
    exit 1
fi

echo "==> smoke-testing the bundle (no window opened)"
SELFTEST_DB="$(mktemp -u /tmp/audio-converter-selftest-XXXXXX.db)"
AUDIO_CONVERTER_DB_PATH="$SELFTEST_DB" \
AUDIO_CONVERTER_SECRET_KEY="selftest" \
    "$APP_BIN" --self-test
rm -f "$SELFTEST_DB"* 2>/dev/null || true

echo "==> build OK:"
echo "    folder: $BIN"
echo "    app:    $APP_BIN"
echo "    Run bash scripts/build_dmg.sh for the installer disk image."
