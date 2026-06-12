#!/usr/bin/env bash
# Downloads the WhatsApp native macOS *beta* build.
#
# The official endpoint 302-redirects to a CDN .dmg whose filename carries the
# version, e.g. .../WhatsApp-2.26.23.19.dmg
#
# Usage: download_mac.sh <out_dir>
set -euo pipefail

OUT_DIR="${1:-artifacts}"
mkdir -p "$OUT_DIR"
DMG_PATH="$OUT_DIR/WhatsApp-mac.dmg"

URL="https://web.whatsapp.com/desktop/mac_native/release/?configuration=Beta"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

echo "==> Downloading WhatsApp Mac beta from: $URL"
# -w writes the final (post-redirect) URL so we can recover the version.
EFFECTIVE=$(curl -fSL --retry 3 --retry-delay 5 -A "$UA" -o "$DMG_PATH" -w '%{url_effective}' "$URL")
echo "==> Final URL: $EFFECTIVE"

VERSION=$(echo "$EFFECTIVE" | grep -oE 'WhatsApp-[0-9]+(\.[0-9]+)+\.dmg' | grep -oE '[0-9]+(\.[0-9]+)+' | head -1 || true)
if [ -n "$VERSION" ]; then
  echo "$VERSION" > "$OUT_DIR/mac-version.txt"
  echo "==> Detected version from URL: $VERSION"
fi

SIZE=$(wc -c < "$DMG_PATH")
echo "==> Saved $DMG_PATH ($SIZE bytes)"
