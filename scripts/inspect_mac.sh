#!/usr/bin/env bash
# Diagnostic: do the macOS string files carry meaningful (namespaced) KEYS we
# could group by? Inspect a non-English Localizable.strings and the localite
# .meta. Temporary — removed after.
set -uo pipefail

DMG="${1:?usage: inspect_mac.sh <dmg>}"
MOUNT="$(mktemp -d)"
hdiutil attach "$DMG" -nobrowse -quiet -mountpoint "$MOUNT"
trap 'hdiutil detach "$MOUNT" -quiet -force || true' EXIT

APP="$(find "$MOUNT" -maxdepth 1 -name '*.app' | head -1)"
RES="$APP/Contents/Resources"

echo "===== de.lproj/Localizable.strings : first 40 lines (key = value format?) ====="
DE="$RES/de.lproj/Localizable.strings"
if [ -f "$DE" ]; then
  # could be binary plist or text; try plutil then raw
  plutil -p "$DE" 2>/dev/null | head -40 || head -40 "$DE"
  echo "...."
  echo "entries (de): $(plutil -convert json -o - "$DE" 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo '?')"
fi
echo

echo "===== he.lproj/Localizable.strings : 20 sample KEYS only ====="
HE="$RES/he.lproj/Localizable.strings"
if [ -f "$HE" ]; then
  plutil -convert json -o - "$HE" 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
ks=list(d.keys())
print("total keys:", len(ks))
for k in ks[:20]: print("  KEY:", repr(k[:80]))
' 2>/dev/null || echo "(parse failed)"
fi
echo

echo "===== localite .meta : size, hex of first non-zero region, strings ====="
META="$RES/en.lproj/Localizable.localite.meta"
ls -la "$META"
python3 - "$META" <<'PY'
import sys
b=open(sys.argv[1],"rb").read()
nz=next((i for i,x in enumerate(b) if x),None)
print("first non-zero offset:",nz,"of",len(b))
seg=b[nz:nz+160] if nz is not None else b[:160]
print("hex:",seg.hex())
# any ascii-ish runs?
import re
runs=re.findall(rb'[ -~]{4,}',b)
print("ascii runs >=4 chars:",len(runs))
for r in runs[:15]: print("  RUN:",r[:60])
PY
echo

echo "===== do localite value-count and de-strings key-count match? ====="
VAL="$RES/en.lproj/Localizable.localite.values"
echo "localite values (strings -n 2): $(LANG=C strings -n 2 "$VAL" | wc -l)"
