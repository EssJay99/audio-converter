#!/bin/bash
# Build a distributable macOS bundle for the Audio Converter desktop app.
#
# Usage:  bash scripts/build_macos.sh
# Output: dist/AudioConverter/AudioConverter
#
# Install:  1. `brew install ffmpeg` (once; conversions need it)
#           2. Copy dist/AudioConverter to /Applications
#           3. Double-click AudioConverter (first launch: right-click > Open
#              to clear Gatekeeper, as the bundle is not notarized)
#           4. Optional login start: copy scripts/com.audioconverter.app.plist
#              to ~/Library/LaunchAgents and run `launchctl load` on it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
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
if [ ! -x build_helpers/ffmpeg ]; then
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
"$PY" -m PyInstaller audio-converter.spec \
    --noconfirm --clean --distpath "$ROOT/dist" --workpath "$ROOT/build"

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
