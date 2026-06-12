#!/usr/bin/env python3
"""Extract the signals we diff from a decoded WhatsApp APK.

Two kinds of signal:

  strings     — the set of human-readable <string> VALUES. WhatsApp strips
                resource *names* (apktool: APKTOOL_DUMMYVAL_0x<id>) and the ids
                are reassigned every build, so we diff values, not names. A
                value in the new build but not the old one is new UI text.

  components  — Activity / Service / Receiver / Provider names + permissions
                from AndroidManifest.xml. Unlike the obfuscated internal code,
                these stay readable (e.g. PasskeyPrologueConfirmationActivity),
                so a new component is a strong, concrete "new feature" signal.

Usage: extract_android.py <decoded_dir> <out_json>
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def has_letter(s: str) -> bool:
    return any(ch.isalpha() for ch in s)


def clean(text: str) -> str:
    """Normalize a string value to its displayed text.

    Android wraps values in "..." to preserve whitespace, and escapes ' and ".
    Strip those so the same displayed text compares equal across builds.
    """
    t = text.strip().replace("\\'", "'").replace('\\"', '"')
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1].strip()
    return t


def collect_values(path: Path) -> set:
    vals = set()
    if not path.exists():
        return vals
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        print(f"WARN: could not parse {path}: {exc}", file=sys.stderr)
        return vals
    for el in root.findall("string"):
        text = clean("".join(el.itertext()))
        if len(text) >= 2 and has_letter(text):
            vals.add(text)
    return vals


def parse_manifest(decoded_dir: Path):
    """Return (components, permissions) as sorted lists of names."""
    comps, perms = set(), set()
    mf = decoded_dir / "AndroidManifest.xml"
    if not mf.exists():
        return [], []
    try:
        root = ET.parse(mf).getroot()
    except ET.ParseError as exc:
        print(f"WARN: could not parse manifest: {exc}", file=sys.stderr)
        return [], []
    for el in root.findall("uses-permission") + root.findall("uses-permission-sdk-23"):
        name = el.get(ANDROID_NS + "name")
        if name:
            perms.add(name)
    app = root.find("application")
    if app is not None:
        for tag in ("activity", "activity-alias", "service", "receiver", "provider"):
            for el in app.findall(tag):
                name = el.get(ANDROID_NS + "name")
                if name:
                    comps.add(name)
    return sorted(comps), sorted(perms)


def read_version(decoded_dir: Path) -> dict:
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
    components, permissions = parse_manifest(decoded)
    version = read_version(decoded)
    data = {
        "platform": "android",
        "package": "com.whatsapp",
        "version": version["versionName"],
        "versionCode": version["versionCode"],
        "strings": sorted(values),
        "components": components,
        "permissions": permissions,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"==> android v{data['version']} ({data['versionCode']}): "
          f"{len(values)} texts, {len(components)} components, {len(permissions)} permissions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
