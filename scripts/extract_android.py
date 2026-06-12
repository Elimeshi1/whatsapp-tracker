#!/usr/bin/env python3
"""Extract the set of human-readable string VALUES from a decoded WhatsApp APK.

WhatsApp strips resource *names* — apktool shows them as APKTOOL_DUMMYVAL_0x<id>,
and those numeric ids are reassigned between builds, so diffing by name is
meaningless (every string looks "changed"). Instead we diff the *set of string
values* — the WABetaInfo approach: a value present in the new build but not the
old one is new UI text, i.e. a hint at a new feature.

Usage: extract_android.py <decoded_dir> <out_json>
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def has_letter(s: str) -> bool:
    return any(ch.isalpha() for ch in s)


def collect_values(path: Path) -> set:
    """All human-readable <string> values from a res/values/strings.xml."""
    vals = set()
    if not path.exists():
        return vals
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        print(f"WARN: could not parse {path}: {exc}", file=sys.stderr)
        return vals
    for el in root.findall("string"):
        text = "".join(el.itertext()).strip()
        # Keep real text; drop format-only noise ("%s", "•", numbers, lone emoji).
        if len(text) >= 2 and has_letter(text):
            vals.add(text)
    return vals


def read_version(decoded_dir: Path) -> dict:
    """Pull versionName / versionCode from apktool.yml without a YAML dep."""
    info = {"versionName": None, "versionCode": None}
    yml = decoded_dir / "apktool.yml"
    if not yml.exists():
        return info
    text = yml.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"versionName:\s*['\"]?([^'\"\n]+)", text)
    if m:
        info["versionName"] = m.group(1).strip()
    m = re.search(r"versionCode:\s*['\"]?([^'\"\n]+)", text)
    if m:
        info["versionCode"] = m.group(1).strip()
    return info


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: extract_android.py <decoded_dir> <out_json>", file=sys.stderr)
        return 2
    decoded = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    values = collect_values(decoded / "res" / "values" / "strings.xml")
    version = read_version(decoded)
    data = {
        "platform": "android",
        "package": "com.whatsapp",
        "version": version["versionName"],
        "versionCode": version["versionCode"],
        "strings": sorted(values),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"==> android v{data['version']} ({data['versionCode']}): {len(values)} text values")
    return 0


if __name__ == "__main__":
    sys.exit(main())
