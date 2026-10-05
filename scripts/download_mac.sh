#!/usr/bin/env bash
# Downloads the newest WhatsApp native macOS build.
#
# The official endpoint 302-redirects to a CDN .dmg whose filename carries the
# version, e.g. .../WhatsApp-2.26.23.19.dmg
#
# Both the Beta and the Release channel are checked and the newer build wins:
# the Beta channel has stalled for weeks at a time (stuck on 2.26.35.23 while
# Release had moved on to 2.26.40.x), so trusting it alone freezes the tracker.
#
# Usage: download_mac.sh <out_dir>
set -euo pipefail

OUT_DIR="${1:-artifacts}"
mkdir -p "$OUT_DIR"
DMG_PATH="$OUT_DIR/WhatsApp-mac.dmg"

BASE="https://web.whatsapp.com/desktop/mac_native/release/"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

version_of() {
  echo "$1" | grep -oE 'WhatsApp-[0-9]+(\.[0-9]+)+\.dmg' | grep -oE '[0-9]+(\.[0-9]+)+' | head -1 || true
}

# True when dotted version $1 is strictly newer than $2.
newer() {
  python3 -c 'import sys; v=lambda s: [int(n) for n in s.split(".")]; sys.exit(0 if v(sys.argv[1]) > v(sys.argv[2]) else 1)' "$1" "$2"
}

# Resolve each channel's redirect target without downloading the DMG.
BEST_URL=""; BEST_VER=""
for CHANNEL in Beta Release; do
  TARGET=$(curl -sS --retry 3 --retry-delay 5 -A "$UA" -o /dev/null -w '%{redirect_url}' \
    "$BASE?configuration=$CHANNEL" || true)
  VER=$(version_of "$TARGET")
  echo "==> $CHANNEL channel: ${VER:-unknown}"
  [ -n "$VER" ] || continue
  if [ -z "$BEST_VER" ] || newer "$VER" "$BEST_VER"; then
    BEST_URL="$TARGET"; BEST_VER="$VER"
  fi
done

# Fall back to the Beta endpoint itself if neither redirect could be read.
URL="${BEST_URL:-$BASE?configuration=Beta}"

echo "==> Downloading WhatsApp Mac from: $URL"
# -w writes the final (post-redirect) URL so we can recover the version.
EFFECTIVE=$(curl -fSL --retry 3 --retry-delay 5 -A "$UA" -o "$DMG_PATH" -w '%{url_effective}' "$URL")
echo "==> Final URL: $EFFECTIVE"

VERSION=$(version_of "$EFFECTIVE")
if [ -n "$VERSION" ]; then
  echo "$VERSION" > "$OUT_DIR/mac-version.txt"
  echo "==> Detected version from URL: $VERSION"
fi

SIZE=$(wc -c < "$DMG_PATH")
echo "==> Saved $DMG_PATH ($SIZE bytes)"
