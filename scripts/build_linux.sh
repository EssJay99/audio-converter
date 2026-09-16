#!/bin/bash
# Build a Linux bundle + easy-install tarball for the Audio Converter.
# Run ON Linux (or in CI):  bash scripts/build_linux.sh
# Output: dist/AudioConverter-1.0.0-linux.tar.gz
#
# For the person installing: extract the tarball, run ./install.sh
# (no sudo needed), then launch "Audio Converter" from the app menu.
# One optional system package enables the embedded window instead of a
# browser tab:  sudo apt install gir1.2-webkit2-4.1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="$(grep -E "^APP_VERSION" src/app/__init__.py | sed "s/.*'\(.*\)'/\1/")"
PY="${PYTHON:-python3}"

if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
    echo "==> installing pyinstaller"
    "$PY" -m pip install --quiet pyinstaller
fi

mkdir -p build_helpers

if [ ! -x build_helpers/yt-dlp ]; then
    echo "==> downloading yt-dlp_linux"
    curl -sL --max-time 120 -o build_helpers/yt-dlp \
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux"
    chmod +x build_helpers/yt-dlp
fi

if [ ! -x build_helpers/ffmpeg ]; then
    echo "==> downloading static ffmpeg (x86_64)"
    curl -sL --max-time 180 -o build_helpers/ffmpeg.tar.xz \
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    rm -rf build_helpers/ffextract
    mkdir -p build_helpers/ffextract
    tar -xJf build_helpers/ffmpeg.tar.xz -C build_helpers/ffextract
    FFMPEG_BIN="$(find build_helpers/ffextract -name ffmpeg -type f | head -1)"
    if [ -z "$FFMPEG_BIN" ]; then
        echo "error: ffmpeg binary not found in archive" >&2
        exit 1
    fi
    mv "$FFMPEG_BIN" build_helpers/ffmpeg
    rm -rf build_helpers/ffextract build_helpers/ffmpeg.tar.xz
    chmod +x build_helpers/ffmpeg
fi

echo "==> building with PyInstaller"
"$PY" -m PyInstaller audio-converter.spec \
    --noconfirm --clean --distpath "$ROOT/dist" --workpath "$ROOT/build"

BIN="$ROOT/dist/AudioConverter/AudioConverter"
if [ ! -x "$BIN" ]; then
    echo "error: bundle binary missing at $BIN" >&2
    exit 1
fi

echo "==> smoke-testing the bundle (no window opened)"
SELFTEST_DB="$(mktemp -u /tmp/audio-converter-selftest-XXXXXX.db)"
AUDIO_CONVERTER_DB_PATH="$SELFTEST_DB" \
AUDIO_CONVERTER_SECRET_KEY="selftest" \
    "$BIN" --self-test
rm -f "$SELFTEST_DB"* 2>/dev/null || true

echo "==> staging installable tarball"
STAGE="$ROOT/dist/AudioConverter-${VERSION}-linux"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$ROOT/dist/AudioConverter" "$STAGE/AudioConverter"

cat > "$STAGE/AudioConverter.desktop" << 'EOF'
[Desktop Entry]
Name=Audio Converter
Comment=YouTube/SoundCloud to lossless audio
Exec=INSTALL_PREFIX/AudioConverter/AudioConverter
Type=Application
Categories=AudioVideo;Audio;
Terminal=false
StartupWMClass=AudioConverter
EOF

cat > "$STAGE/install.sh" << 'EOF'
#!/bin/bash
# Install Audio Converter for the current user (no sudo needed).
set -euo pipefail
cd "$(dirname "$0")"
PREFIX="$HOME/.local/share"
mkdir -p "$PREFIX" "$HOME/.local/share/applications"
rm -rf "$PREFIX/AudioConverter"
cp -R AudioConverter "$PREFIX/AudioConverter"
sed "s|INSTALL_PREFIX|$PREFIX|" AudioConverter.desktop > "$HOME/.local/share/applications/audioconverter.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Installed. Launch 'Audio Converter' from your app menu."
EOF
chmod +x "$STAGE/install.sh"

cat > "$STAGE/How to install.txt" << 'EOF'
Audio Converter — install in 30 seconds (Linux)
================================================

1. Extract this archive anywhere.
2. Double-click install.sh (or run ./install.sh in a terminal).
3. Launch "Audio Converter" from your app menu.

Everything the app needs (downloader included) is inside. The only
optional extra: for the embedded app window instead of a browser tab,
install the system web view once with:
    sudo apt install gir1.2-webkit2-4.1
Without it the app still works — it simply opens in your browser.
Your music saves to a folder you pick, and your library stays on
your machine only.
EOF

tar -czf "$STAGE.tar.gz" -C "$ROOT/dist" "$(basename "$STAGE")"
echo "==> installer ready: $STAGE.tar.gz"
ls -la "$STAGE.tar.gz"
