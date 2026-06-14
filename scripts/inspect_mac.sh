#!/usr/bin/env bash
# Diagnostic: dump WhatsApp's custom "localite" localization format so we can
# write a parser. Temporary — removed once extract_mac is fixed.
set -uo pipefail   # no -e: broken pipes from head/xxd must not abort us

DMG="${1:?usage: inspect_mac.sh <dmg>}"
MOUNT="$(mktemp -d)"
hdiutil attach "$DMG" -nobrowse -quiet -mountpoint "$MOUNT"
trap 'hdiutil detach "$MOUNT" -quiet -force || true' EXIT

APP="$(find "$MOUNT" -maxdepth 1 -name '*.app' | head -1)"
VAL="$APP/Contents/Resources/en.lproj/Localizable.localite.values"
META="$APP/Contents/Resources/en.lproj/Localizable.localite.meta"

echo "===== .values: first 512 bytes (hex) ====="
xxd -l 512 "$VAL"
echo
echo "===== .values: bytes 512..1024 (hex) ====="
xxd -s 512 -l 512 "$VAL"
echo
echo "===== .values: strings -n 3 count ====="
LANG=C strings -n 3 "$VAL" | wc -l
echo "----- first 40 extracted values -----"
LANG=C strings -n 3 "$VAL" > /tmp/vals.txt
head -40 /tmp/vals.txt
echo
echo "===== .meta: hex around first non-zero byte ====="
# find first non-zero offset
OFF=$(LANG=C grep -aboP '[^\x00]' "$META" | head -1 | cut -d: -f1)
echo "first non-zero byte at offset: ${OFF:-none}"
if [ -n "${OFF:-}" ]; then xxd -s "$OFF" -l 512 "$META"; fi
echo
echo "===== .meta: strings -n 3 first 30 (keys?) ====="
LANG=C strings -n 3 "$META" | head -30
echo
echo "===== sanity: how many .values look like real UI text (have a space + letter) ====="
grep -cP '[A-Za-z].* ' /tmp/vals.txt
