#!/bin/bash
# Sign and notarize the macOS bundle + disk image so Gatekeeper opens it
# with a double-click (no right-click > Open dance).
#
# Usage:
#   bash scripts/notarize_macos.sh <path-to.dmg>
#     Full pipeline: sign dist/AudioConverter.app, rebuild the .dmg from
#     the signed bundle, submit to Apple, staple the ticket.
#   bash scripts/notarize_macos.sh --sign-only [bundle]
#   bash scripts/notarize_macos.sh --notarize-only <dmg>
#     Individual steps (used by build_dmg.sh so the image is always built
#     from an already-signed bundle — signing must come first).
#
# Required environment (CI secrets or local shell):
#   APPLE_SIGN_IDENTITY  e.g. "Developer ID Application: Jane Doe (AB12CD34EF)"
#   APPLE_ID             Apple ID email for notarization
#   APPLE_TEAM_ID        10-character team ID
#   APPLE_APP_PASSWORD   app-specific password (appleid.apple.com)
#
# Without credentials every mode prints why and exits 0, so local and
# unsigned CI builds keep working exactly as before.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

have_credentials() {
    [ -n "${APPLE_SIGN_IDENTITY:-}" ] && [ -n "${APPLE_ID:-}" ] \
    && [ -n "${APPLE_TEAM_ID:-}" ] && [ -n "${APPLE_APP_PASSWORD:-}" ]
}

sign_bundle() {
    local bundle="$1"
    echo "==> signing bundle (hardened runtime): $bundle"
    codesign --deep --force --options runtime \
        --entitlements "$ROOT/scripts/entitlements.plist" \
        --sign "$APPLE_SIGN_IDENTITY" "$bundle"
    codesign --verify --deep --strict "$bundle"
    echo "==> signature verified"
}

notarize_dmg() {
    local dmg="$1"
    echo "==> submitting for notarization (takes minutes): $dmg"
    xcrun notarytool submit "$dmg" \
        --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
        --password "$APPLE_APP_PASSWORD" --wait
    echo "==> stapling notarization ticket"
    xcrun stapler staple "$dmg"
    echo "==> notarized and stapled: $dmg"
}

if [ "${1:-}" = "--sign-only" ]; then
    BUNDLE="${2:-$ROOT/dist/AudioConverter.app}"
    if ! have_credentials; then
        echo "==> signing skipped: signing credentials not set"
        exit 0
    fi
    [ -d "$BUNDLE" ] || { echo "error: bundle missing at $BUNDLE" >&2; exit 1; }
    sign_bundle "$BUNDLE"
    exit 0
fi

if [ "${1:-}" = "--notarize-only" ]; then
    DMG="${2:-}"
    if ! have_credentials; then
        echo "==> notarization skipped: signing credentials not set"
        exit 0
    fi
    [ -n "$DMG" ] && [ -f "$DMG" ] || { echo "error: disk image missing: $DMG" >&2; exit 1; }
    notarize_dmg "$DMG"
    exit 0
fi

# Full pipeline: sign -> rebuild .dmg from the signed bundle -> notarize.
DMG="${1:-}"
[ -n "$DMG" ] || { echo "usage: $0 <path-to.dmg> [-- rebuilt from the signed bundle]" >&2; exit 1; }
if ! have_credentials; then
    echo "==> notarization skipped: signing credentials not set"
    echo "    (set APPLE_SIGN_IDENTITY, APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD)"
    exit 0
fi
[ -d "$ROOT/dist/AudioConverter.app" ] || { echo "error: run scripts/build_macos.sh first" >&2; exit 1; }
sign_bundle "$ROOT/dist/AudioConverter.app"
bash scripts/build_dmg.sh
FRESH="$(ls -t "$ROOT"/dist/AudioConverter-*.dmg | head -1)"
notarize_dmg "$FRESH"
