#!/usr/bin/env bash
# Diagnostic: dump the structure of WhatsApp's custom "localite" localization
# files so we can write a parser. Temporary — removed once extract_mac is fixed.
set -euo pipefail

DMG="${1:?usage: inspect_mac.sh <dmg>}"
MOUNT="$(mktemp -d)"
hdiutil attach "$DMG" -nobrowse -quiet -mountpoint "$MOUNT"
trap 'hdiutil detach "$MOUNT" -quiet -force || true' EXIT

APP="$(find "$MOUNT" -maxdepth 1 -name '*.app' | head -1)"
VAL="$APP/Contents/Resources/en.lproj/Localizable.localite.values"
META="$APP/Contents/Resources/en.lproj/Localizable.localite.meta"

echo "===== sizes ====="
ls -la "$VAL" "$META"
echo

echo "===== .meta: first 256 bytes (hex) ====="
xxd "$META" | head -16
echo
echo "===== .meta: try plutil ====="
plutil -p "$META" 2>&1 | head -20 || echo "(not a plist)"
echo

echo "===== .values: first 512 bytes (hex) ====="
xxd "$VAL" | head -32
echo
echo "===== .values: try plutil ====="
plutil -p "$VAL" 2>&1 | head -8 || echo "(not a plist)"
echo
echo "===== .values: printable string count (strings -n 3) ====="
LANG=C strings -n 3 "$VAL" | wc -l
echo "----- first 30 extracted strings -----"
LANG=C strings -n 3 "$VAL" | head -30
echo
echo "===== .meta: printable strings (keys?) first 20 ====="
LANG=C strings -n 3 "$META" | head -20
