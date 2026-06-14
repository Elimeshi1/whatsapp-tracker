#!/usr/bin/env bash
# Diagnostic: mount a WhatsApp macOS .dmg and inventory every file that could
# carry UI strings, so we can see WHERE the modern (Catalyst) app keeps them.
# Temporary — used to fix extract_mac.py, then removed.
set -euo pipefail

DMG="${1:?usage: inspect_mac.sh <dmg>}"
MOUNT="$(mktemp -d)"
hdiutil attach "$DMG" -nobrowse -quiet -mountpoint "$MOUNT"
trap 'hdiutil detach "$MOUNT" -quiet -force || true' EXIT

APP="$(find "$MOUNT" -maxdepth 1 -name '*.app' | head -1)"
echo "APP = $APP"
echo

echo "===== top-level Contents ====="
ls -la "$APP/Contents" || true
echo

echo "===== *.lproj dirs (anywhere) ====="
find "$APP" -name '*.lproj' -type d | sed "s#$APP#<APP>#" | sort | head -60
echo

echo "===== en/Base lproj contents ====="
for d in $(find "$APP" -type d \( -name 'en.lproj' -o -name 'Base.lproj' -o -name 'en_US.lproj' \)); do
  echo "--- $d (rel: ${d#$APP}) ---"
  ls -la "$d"
done
echo

echo "===== all .strings / .stringsdict / .loctable (size, rel path) ====="
find "$APP" \( -name '*.strings' -o -name '*.stringsdict' -o -name '*.loctable' \) \
  -exec stat -f '%z  %N' {} \; 2>/dev/null | sed "s#$APP#<APP>#" | sort -rn | head -80
echo

echo "===== biggest .loctable: entry counts per locale ====="
BIG="$(find "$APP" -name '*.loctable' -exec stat -f '%z %N' {} \; 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
if [ -n "${BIG:-}" ]; then
  echo "file: ${BIG#$APP}"
  plutil -convert json -o - "$BIG" 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
if isinstance(d,dict):
    for loc,tbl in list(d.items())[:8]:
        n=len(tbl) if isinstance(tbl,dict) else "?"
        print(f"  {loc}: {n} entries")
' || echo "  (plutil/parse failed)"
fi
echo

echo "===== Assets.car present? (compiled, may hold text) ====="
find "$APP" -name 'Assets.car' | sed "s#$APP#<APP>#" | head
echo

echo "===== Frameworks with their own Resources/*.lproj ====="
find "$APP/Contents/Frameworks" -name '*.lproj' -type d 2>/dev/null | sed "s#$APP#<APP>#" | sort | head -40 || true
