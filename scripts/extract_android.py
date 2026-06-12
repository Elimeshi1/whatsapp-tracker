#!/usr/bin/env python3
"""Extract strings, bools and integers from an apktool-decoded WhatsApp APK.

These are the highest-signal resources for tracking "what's new":
  - res/values/strings.xml   -> UI texts (new features usually add new strings)
  - res/values/bools.xml     -> feature toggles (gates that flip new behaviour on)
  - res/values/integers.xml  -> tunables / thresholds

Usage: extract_android.py <decoded_dir> <out_json>
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_value_file(path: Path, tag: str) -> dict:
    """Parse a res/values/*.xml file, returning {name: text} for the given tag."""
    out = {}
    if not path.exists():
        return out
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        print(f"WARN: could not parse {path}: {exc}", file=sys.stderr)
        return out
    for el in root.findall(tag):
        name = el.get("name")
        if not name:
            continue
        # Join text + tails so formatted strings with inner tags survive.
        text = "".join(el.itertext()).strip()
        out[name] = text
    return out


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
    values = decoded / "res" / "values"

    version = read_version(decoded)
    data = {
        "platform": "android",
        "package": "com.whatsapp",
        "version": version["versionName"],
        "versionCode": version["versionCode"],
        "strings": parse_value_file(values / "strings.xml", "string"),
        "bools": parse_value_file(values / "bools.xml", "bool"),
        "integers": parse_value_file(values / "integers.xml", "integer"),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(
        f"==> android v{data['version']} ({data['versionCode']}): "
        f"{len(data['strings'])} strings, {len(data['bools'])} bools, {len(data['integers'])} integers"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
