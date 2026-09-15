#!/bin/bash
# Build a double-click macOS installer (.dmg) for the Audio Converter.
#
# Usage:  bash scripts/build_dmg.sh
# Output: dist/AudioConverter-1.0.0.dmg
#
# For the person installing: open the .dmg, drag AudioConverter into
# Applications, eject, double-click the app. If macOS complains the app is
# from an unidentified developer, right-click it once and choose Open.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="$(grep -E "^APP_VERSION" src/app/__init__.py | sed "s/.*'\(.*\)'/\1/")"
BUNDLE="$ROOT/dist/AudioConverter"
DMG="$ROOT/dist/AudioConverter-${VERSION}.dmg"

if [ ! -x "$BUNDLE/AudioConverter" ]; then
    echo "==> bundle missing; building it first"
    bash scripts/build_macos.sh
fi

STAGE="$(mktemp -d /tmp/audio-converter-dmg-XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

cp -R "$BUNDLE" "$STAGE/AudioConverter"
ln -s /Applications "$STAGE/Applications"

cat > "$STAGE/How to install.txt" << 'EOF'
Audio Converter — install in 30 seconds
========================================

1. Drag "AudioConverter" onto "Applications".
2. Eject this disk image.
3. Open AudioConverter from Applications (or Spotlight).

First launch: macOS may say the app is from an unidentified developer
because it isn't Apple-notarized. Right-click the app once, choose Open,
then click Open again. You only do this once.

Everything the app needs (downloader included) is inside — no Terminal,
no Homebrew, nothing else to install. Your music saves to a folder you
pick inside the app, and your library lives on your Mac only.
EOF

rm -f "$DMG"
hdiutil create -volname "AudioConverter" -srcfolder "$STAGE" \
    -ov -format UDZO "$DMG" > /dev/null
echo "==> installer ready: $DMG"
ls -la "$DMG"
