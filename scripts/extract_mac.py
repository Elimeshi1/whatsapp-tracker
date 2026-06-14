#!/usr/bin/env python3
"""Mount a WhatsApp macOS .dmg and extract localized strings.

Runs on a macOS runner (needs hdiutil + plutil). Collects every English/Base
.strings and .loctable file inside the app bundle plus the bundle version, and
flattens them into a single {key: value} map keyed by "<file>::<key>".

Usage: extract_mac.py <dmg_path> <out_json>
"""
import json
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd: list) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def plutil_to_obj(path: Path):
    """Convert a .strings / .loctable / .plist to a Python object via plutil."""
    try:
        out = run(["plutil", "-convert", "json", "-o", "-", str(path)])
        return json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"WARN: plutil failed for {path}: {exc}", file=sys.stderr)
        return None


def has_letter(s: str) -> bool:
    return any(ch.isalpha() for ch in s)


# Bidi control marks WhatsApp wraps every value with (U+200E LRM etc.); strip so
# values compare/display cleanly.
_BIDI = dict.fromkeys(
    map(ord, "‎‏‪‫‬‭‮⁦⁧⁨⁩"),
    None,
)


def clean(text: str) -> str:
    t = text.translate(_BIDI).strip().replace("\\'", "'").replace('\\"', '"')
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1].strip()
    return t


def collect_localite(path: Path, out: set):
    """Extract values from WhatsApp's custom `.localite.values` blob.

    Format: a zero-padded header, then UTF-8 string values each terminated by a
    NUL byte (and prefixed with a U+200E mark). This holds the *English source*
    UI — the main app keeps no standard en Localizable.strings, so this is where
    almost all texts live. Keys live in the sibling `.meta`, which we don't need
    (we diff values).
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        print(f"WARN: could not read {path}: {exc}", file=sys.stderr)
        return
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        try:
            t = clean(chunk.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        if len(t) >= 2 and has_letter(t):
            out.add(t)


def collect_values(obj, out: set, prefer_locales=("en", "Base", "en_US")):
    """Collect human-readable string VALUES from one parsed file into `out`.

    We diff the set of values (not keys), to stay consistent with Android where
    keys are unusable. .strings -> {key: value}; .loctable -> {locale: {k: v}}.
    """
    if not isinstance(obj, dict):
        return
    # .loctable: values are dicts-per-locale — pick a preferred English locale.
    if obj and all(isinstance(v, dict) for v in obj.values()):
        locale = next((l for l in prefer_locales if l in obj), None) or next(iter(obj))
        table = obj.get(locale, {})
    else:
        table = obj
    for v in table.values():
        if isinstance(v, str):
            t = clean(v)
            if len(t) >= 2 and has_letter(t):
                out.add(t)


def find_app(mount: Path) -> Path:
    apps = list(mount.glob("*.app"))
    if not apps:
        raise SystemExit(f"No .app found in {mount}")
    return apps[0]


def bundle_version(app: Path) -> dict:
    info = app / "Contents" / "Info.plist"
    if not info.exists():
        return {"version": None, "build": None}
    try:
        with open(info, "rb") as fh:
            pl = plistlib.load(fh)
        return {
            "version": pl.get("CFBundleShortVersionString"),
            "build": pl.get("CFBundleVersion"),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: could not read Info.plist: {exc}", file=sys.stderr)
        return {"version": None, "build": None}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: extract_mac.py <dmg_path> <out_json>", file=sys.stderr)
        return 2
    dmg = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    mount = Path(tempfile.mkdtemp(prefix="wamount-"))
    strings: set = set()
    meta = {"version": None, "build": None}
    try:
        run(["hdiutil", "attach", str(dmg), "-nobrowse", "-quiet", "-mountpoint", str(mount)])
        app = find_app(mount)
        meta = bundle_version(app)
        resources = app / "Contents" / "Resources"

        en_lprojs = ("en.lproj", "Base.lproj", "en_US.lproj")

        # The bulk of the English UI lives in WhatsApp's custom `.localite.values`
        # blobs (the main bundle + frameworks like SharedModules) — English has
        # no standard Localizable.strings. Parse every English-locale one.
        localite = [f for f in app.rglob("*.localite.values")
                    if f.parent.name in en_lprojs]
        for f in localite:
            collect_localite(f, strings)

        # Standard .strings / .loctable still carry InfoPlist permission text and
        # extension strings — keep them too.
        candidates = []
        for d in en_lprojs:
            candidates += list((resources / d).glob("*.strings"))
        candidates += list(app.rglob("*.loctable"))

        seen = set()
        for f in candidates:
            if f in seen or not f.exists():
                continue
            seen.add(f)
            collect_values(plutil_to_obj(f), strings)
        print(f"==> sources: {len(localite)} localite blob(s) + "
              f"{len(seen)} .strings/.loctable files")
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-quiet", "-force"],
                       capture_output=True, text=True)

    data = {
        "platform": "mac",
        "schema": 2,  # bumped when extraction changes; mismatch = baseline reset
        "version": meta["version"],
        "build": meta["build"],
        "strings": sorted(strings),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"==> mac v{data['version']} (build {data['build']}): {len(strings)} text values")
    return 0


if __name__ == "__main__":
    sys.exit(main())
