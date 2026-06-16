#!/usr/bin/env python3
"""Extract the readable class & method *names* WhatsApp's own code exposes.

This is the "function names" signal: a new readable class or method in
com/whatsapp is a strong, concrete new-feature hint — it usually lands before
any UI text does, and (unlike WhatsApp's obfuscated X/… classes) the readable
names are stable enough across builds to diff.

How it works: we read the method table straight out of the APK's classes*.dex
files (no apktool/baksmali needed — those slow Java steps stay only for the
string→module mapping). The DEX method table lists every (class, method) the
build references; we keep only the ones whose class lives under com/whatsapp and
whose class- and method-names look human-written rather than obfuscated.

What "readable" means here:
  class   — simple name starts uppercase, >= 4 chars, has a lowercase letter
            (CamelCase like PasskeyEnrollmentActivity) — excludes X/, A01, Bk.
  method  — camelCase / snake_case, >= 4 chars (onCreate, startEnrollment) —
            excludes obfuscated a(), b0(), Bk(). Constructors are skipped.

Usage: extract_methods.py <apk_path> <out_json> [version] [versionCode]
"""
import json
import re
import sys
import zipfile
from struct import unpack_from


# ----------------------------------------------------------------- DEX parse ---
def _uleb128(buf, off):
    """Decode an unsigned LEB128 at off; return (value, new_off)."""
    result = shift = 0
    while True:
        b = buf[off]
        off += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, off
        shift += 7


def _read_string(buf, data_off):
    """Read one MUTF-8 string entry: ULEB128 utf16-len, then bytes to NUL."""
    _, off = _uleb128(buf, data_off)          # utf16 length — skip, we read to NUL
    end = buf.index(b"\x00", off)
    return buf[off:end].decode("utf-8", "replace")


def dex_methods(buf):
    """Yield (class_descriptor, method_name) for every method id in one dex."""
    if buf[:4] != b"dex\n":
        return
    (string_ids_size, string_ids_off,
     type_ids_size, type_ids_off) = unpack_from("<IIII", buf, 0x38)[:4]
    method_ids_size, method_ids_off = unpack_from("<II", buf, 0x58)

    # string_ids[i] -> data offset; resolve lazily to avoid decoding all strings.
    str_offs = unpack_from(f"<{string_ids_size}I", buf, string_ids_off)
    str_cache = {}

    def s(idx):
        v = str_cache.get(idx)
        if v is None:
            v = str_cache[idx] = _read_string(buf, str_offs[idx])
        return v

    # type_ids[i] -> string idx of the type descriptor.
    type_str_idx = unpack_from(f"<{type_ids_size}I", buf, type_ids_off)

    # method_id = (ushort class_type_idx, ushort proto_idx, uint name_str_idx)
    for i in range(method_ids_size):
        class_idx, _proto, name_idx = unpack_from("<HHI", buf, method_ids_off + i * 8)
        cls = s(type_str_idx[class_idx])
        if not cls.startswith("Lcom/whatsapp/"):
            continue
        yield cls, s(name_idx)


# ------------------------------------------------------------- readability ---
_CLASS_OK = re.compile(r"^[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*$")
_METHOD_OK = re.compile(r"^[a-z][A-Za-z0-9_]*$")
_LIFECYCLE = {"onCreate", "onResume", "onPause", "onStart", "onStop", "onDestroy"}


def simple_class(descriptor: str) -> str:
    """Lcom/whatsapp/foo/BarActivity; -> BarActivity (last path segment)."""
    inner = descriptor[1:-1]                   # strip L … ;
    inner = inner.split("$", 1)[0]             # drop inner-class suffix
    return inner.rsplit("/", 1)[-1]


def readable_class(descriptor: str) -> bool:
    return bool(_CLASS_OK.match(simple_class(descriptor)))


def readable_method(name: str) -> bool:
    if name in _LIFECYCLE:
        return True
    # camelCase/snake_case, long enough, and not all-lowercase-no-underscore
    # (which is the shape obfuscation also produces, e.g. "abcd").
    if len(name) < 4 or not _METHOD_OK.match(name):
        return False
    # Drop JNI/type-signature dispatch names (jnidispatchIIII…OOOO, etc.):
    # machine-generated, never a real feature signal. The capital-run check also
    # catches other encoded-signature names.
    if name.startswith("jnidispatch") or re.search(r"[A-Z]{6,}", name):
        return False
    return any(c.isupper() for c in name) or "_" in name


def pretty_method(descriptor: str, name: str) -> str:
    """Lcom/whatsapp/foo/Bar;, doThing -> com.whatsapp.foo.Bar#doThing"""
    return descriptor[1:-1].replace("/", ".") + "#" + name


def main() -> int:
    if len(sys.argv) not in (3, 4, 5):
        print("usage: extract_methods.py <apk> <out_json> [version] [versionCode]",
              file=sys.stderr)
        return 2
    apk, out_path = sys.argv[1], sys.argv[2]
    version = sys.argv[3] if len(sys.argv) > 3 else None
    version_code = sys.argv[4] if len(sys.argv) > 4 else None

    classes, methods = set(), set()
    with zipfile.ZipFile(apk) as z:
        dex_names = [n for n in z.namelist()
                     if re.fullmatch(r"classes\d*\.dex", n)]
        for name in dex_names:
            buf = z.read(name)
            for cls, mname in dex_methods(buf):
                if not readable_class(cls):
                    continue
                classes.add(cls[1:-1].replace("/", "."))
                if readable_method(mname):
                    methods.add(pretty_method(cls, mname))
    print(f"==> scanned {len(dex_names)} dex: "
          f"{len(classes)} readable classes, {len(methods)} readable methods")

    data = {
        "platform": "android",
        "kind": "methods",
        "schema": 1,
        "version": version,
        "versionCode": version_code,
        "classes": sorted(classes),
        "methods": sorted(methods),
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
