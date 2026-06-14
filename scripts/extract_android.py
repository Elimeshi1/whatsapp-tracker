#!/usr/bin/env python3
"""Extract the signals we diff from a decoded WhatsApp APK.

Three kinds of signal:

  strings       — the set of human-readable <string> VALUES. WhatsApp strips
                  resource *names* (apktool: APKTOOL_DUMMYVAL_0x<id>) and the
                  ids are reassigned every build, so we diff values, not names.
                  A value in the new build but not the old one is new UI text.

  string_areas  — for each string we can, the *real feature module* that uses
                  it. The dummy name embeds the resource id (0x7f12abcd); that
                  id appears as a constant in the bytecode wherever the string
                  is referenced. WhatsApp obfuscates most classes (X/…), but
                  ~4k classes keep readable paths (com/whatsapp/<module>/…), so
                  if a readable class references the id we can say exactly which
                  feature the string belongs to (e.g. companiondevice, payments,
                  registration). This is the concrete "where does this belong"
                  signal — derived from WhatsApp's own code layout, not guessed.

  components    — Activity / Service / Receiver / Provider names + permissions
                  from AndroidManifest.xml. Unlike the obfuscated internal code,
                  these stay readable (e.g. PasskeyPrologueConfirmationActivity),
                  so a new component is a strong, concrete "new feature" signal.

Usage: extract_android.py <decoded_dir> <out_json> [smali_dir]

If <smali_dir> (a baksmali/apktool source dump) is given, string_areas is built
by cross-referencing resource ids against the readable com/whatsapp classes.
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
RES_ID = re.compile(r"0x7f[0-9a-f]{6}")
# Generic second-level segments that don't add meaning as a label on their own.
_GENERIC_SEG = {"ui", "product", "app", "view", "views", "impl", "base", "core",
                "common", "data", "model", "models", "fragment", "fragments",
                "activity", "activities", "viewmodel", "util", "utils"}


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


def collect_values(path: Path):
    """Return (values_set, id_to_value) for the displayable <string> entries.

    id_to_value maps the resource id embedded in the dummy name (e.g.
    "0x7f120000") to its displayed value, so we can later tie a value to the
    code that references its id.
    """
    vals, id2val = set(), {}
    if not path.exists():
        return vals, id2val
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        print(f"WARN: could not parse {path}: {exc}", file=sys.stderr)
        return vals, id2val
    for el in root.findall("string"):
        text = clean("".join(el.itertext()))
        if len(text) >= 2 and has_letter(text):
            vals.add(text)
            m = RES_ID.search(el.get("name") or "")
            if m:
                id2val[m.group(0)] = text
    return vals, id2val


def _module_label(rel_parts) -> str:
    """Derive a feature label from a class path under com/whatsapp.

    rel_parts are the path segments after com/whatsapp, e.g.
    ("calling", "voipcalling", "Foo.smali"). Use the top module, plus a second
    segment when it adds meaning (not a generic 'ui'/'product'/… bucket).
    """
    dirs = [p for p in rel_parts[:-1]]  # drop the filename
    if not dirs:
        return rel_parts[0].rsplit(".", 1)[0]
    label = dirs[0]
    if len(dirs) > 1 and dirs[1] not in _GENERIC_SEG:
        label = f"{dirs[0]}/{dirs[1]}"
    return label


def build_string_areas(id2val: dict, smali_dir: Path) -> dict:
    """Map string VALUE -> real feature module by cross-referencing resource ids.

    Scans the readable com/whatsapp classes (the rest are obfuscated as X/…) for
    occurrences of each string's resource id and attributes the value to the
    module that references it most. Values referenced only by obfuscated code get
    no entry (they fall into "uncategorized" downstream).
    """
    base = smali_dir / "com" / "whatsapp"
    if not base.exists():
        print(f"WARN: no readable classes under {base}; skipping string_areas",
              file=sys.stderr)
        return {}
    wanted = set(id2val)
    id_modules = defaultdict(Counter)
    scanned = 0
    for f in base.rglob("*.smali"):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        ids = wanted.intersection(RES_ID.findall(txt))
        if not ids:
            continue
        label = _module_label(f.relative_to(base).parts)
        for rid in ids:
            id_modules[rid][label] += 1
    areas = {}
    for rid, counter in id_modules.items():
        val = id2val.get(rid)
        if val:
            areas[val] = counter.most_common(1)[0][0]
    print(f"==> scanned {scanned} readable classes; "
          f"{len(areas)}/{len(id2val)} strings tied to a feature module")
    return areas


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
    if len(sys.argv) not in (3, 4):
        print("usage: extract_android.py <decoded_dir> <out_json> [smali_dir]",
              file=sys.stderr)
        return 2
    decoded = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    smali_dir = Path(sys.argv[3]) if len(sys.argv) == 4 else None

    values, id2val = collect_values(decoded / "res" / "values" / "strings.xml")
    components, permissions = parse_manifest(decoded)
    version = read_version(decoded)
    string_areas = build_string_areas(id2val, smali_dir) if smali_dir else {}
    data = {
        "platform": "android",
        "package": "com.whatsapp",
        "version": version["versionName"],
        "versionCode": version["versionCode"],
        "strings": sorted(values),
        "string_areas": string_areas,
        "components": components,
        "permissions": permissions,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"==> android v{data['version']} ({data['versionCode']}): "
          f"{len(values)} texts, {len(string_areas)} localized to a module, "
          f"{len(components)} components, {len(permissions)} permissions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
