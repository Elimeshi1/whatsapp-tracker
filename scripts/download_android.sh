#!/usr/bin/env bash
# Downloads the WhatsApp (beta) APK for Android.
#
# Source priority:
#   1. If APK_URL is set (manual override, e.g. an APKMirror beta link), use it.
#   2. Otherwise fall back to APKPure's direct download endpoint.
#
# Usage: download_android.sh <out_dir>
set -euo pipefail

OUT_DIR="${1:-artifacts}"
mkdir -p "$OUT_DIR"
APK_PATH="$OUT_DIR/WhatsApp.apk"

UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

if [ -n "${APK_URL:-}" ]; then
  echo "==> Using manual APK_URL override"
  SRC="$APK_URL"
else
  echo "==> Using APKPure direct endpoint (no override supplied)"
  SRC="https://d.apkpure.com/b/APK/com.whatsapp?version=latest"
fi

echo "==> Downloading from: $SRC"
curl -fSL --retry 3 --retry-delay 5 -A "$UA" -o "$APK_PATH" "$SRC"

# Sanity check: an APK is a ZIP archive (magic bytes 'PK').
MAGIC=$(head -c 2 "$APK_PATH" | tr -d '\0')
if [ "$MAGIC" != "PK" ]; then
  echo "ERROR: downloaded file does not look like an APK/ZIP (magic='$MAGIC')." >&2
  echo "First bytes:" >&2
  head -c 200 "$APK_PATH" >&2 || true
  exit 1
fi

SIZE=$(wc -c < "$APK_PATH")
echo "==> Saved $APK_PATH ($SIZE bytes)"
