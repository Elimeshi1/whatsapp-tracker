#!/usr/bin/env python3
"""Compare freshly extracted string values against the baseline and write reports.

WhatsApp strips resource names, so we diff the *set of string values*: a value in
the new build but not the old one is new UI text (a new-feature hint); a value
that disappeared was removed. There is no reliable "changed" concept without
stable keys, so we report Added and Removed only.

For each platform present under ./incoming this:
  - diffs values against data/<platform>/latest.json
  - writes a Markdown report under reports/<platform>/ when something changed
  - updates the baseline (data/<platform>/latest.json) and a version snapshot
  - appends a one-line entry to CHANGELOG.md
  - emits a notify.json payload + GitHub Actions outputs (changed, summary)

Runs from the repo root inside the GitHub Actions "report" job.
"""
import difflib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCOMING = ROOT / "incoming"
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
CHANGELOG = ROOT / "CHANGELOG.md"


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARN: could not load {path}: {exc}", file=sys.stderr)
        return None


def as_values(strings) -> set:
    """Normalize a `strings` field to a set of values.

    Accepts the current list format, the legacy {name: value} dict format (older
    committed baselines), or None.
    """
    if isinstance(strings, dict):
        return {v for v in strings.values() if v}
    if isinstance(strings, list):
        return set(strings)
    return set()


_TAG = re.compile(r"<[^>]+>")
_PLACEHOLDER = re.compile(r"%\d*\$?[sd@]")
_NONWORD = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    """Normalize for similarity: drop tags, placeholders, punctuation, case."""
    s = _TAG.sub(" ", s)
    s = _PLACEHOLDER.sub(" ", s)
    s = _NONWORD.sub(" ", s.lower())
    return _WS.sub(" ", s).strip()


def classify_changes(added, removed):
    """Split a raw added/removed value diff into three buckets:

      new       — added values with no close match in `removed` (likely new
                  features / genuinely new UI text)
      reworded  — [old, new] pairs: an added value that is just a reworded
                  version of a removed one (same string, minor text change)
      removed   — removed values with no close match in `added`

    A pair counts as a reword when the normalized strings are very similar
    (ratio ≥ 0.85) or fairly similar with strong word overlap (ratio ≥ 0.6 and
    Jaccard ≥ 0.5). Greedy best-match; each removed value is used at most once.
    """
    rem = [(r, _norm(r), set(_norm(r).split())) for r in removed]
    used = set()
    new, reworded = [], []
    for a in added:
        na = _norm(a)
        ta = set(na.split())
        best, best_ratio = None, 0.0
        if na:
            for i, (_, nr, tr) in enumerate(rem):
                if i in used or not nr:
                    continue
                jac = len(ta & tr) / len(ta | tr) if (ta and tr) else 0.0
                # Cheap prune: skip clearly-unrelated pairs.
                if jac < 0.3 and abs(len(na) - len(nr)) > 0.4 * max(len(na), len(nr)):
                    continue
                ratio = difflib.SequenceMatcher(None, na, nr).ratio()
                if (ratio >= 0.85 or (ratio >= 0.6 and jac >= 0.5)) and ratio > best_ratio:
                    best, best_ratio = i, ratio
        if best is not None:
            used.add(best)
            old = removed[best]
            # Drop cosmetic-only pairs (differ only in punctuation / case / quotes /
            # placeholders) — they look identical and aren't a real change.
            if _norm(old) != _norm(a):
                reworded.append([old, a])
        else:
            new.append(a)
    removed_only = [r for i, r in enumerate(removed) if i not in used]
    return new, reworded, removed_only


def build_area_index(extract_path: Path):
    """Build a normalized-value -> module index from an extract's string_areas.

    Lets one platform reuse another's code-derived module labels by matching the
    *same English text*. Keyed on _norm (case/punctuation/placeholder-insensitive)
    so e.g. Android "%1$s" and macOS "%1$@" variants still match. Short/ambiguous
    strings are skipped to avoid mislabeling generic words like "OK"/"Done".
    """
    data = load_json(extract_path)
    idx = {}
    if not data:
        return idx
    for val, area in (data.get("string_areas") or {}).items():
        k = _norm(val)
        if len(k) >= 12 and k not in idx:
            idx[k] = area
    return idx


def group_new_by_area(new_items, string_areas: dict, area_index: dict = None):
    """Group new strings by the real feature module that uses them.

    string_areas maps value -> module label (built in extract by cross-
    referencing resource ids against readable com/whatsapp classes — Android
    only). When a string has no own label (e.g. macOS, which has no code to
    cross-reference), fall back to area_index: the same module derived from the
    identical Android string. Anything still unmatched goes to "· uncategorized".
    Returns a list of {label, items} sorted by size, uncategorized last.
    """
    buckets = {}
    for v in new_items:
        label = (string_areas or {}).get(v)
        if not label and area_index:
            label = area_index.get(_norm(v))
        label = label or "· uncategorized"
        buckets.setdefault(label, []).append(v)
    groups = [{"label": k, "items": v} for k, v in buckets.items()]
    groups.sort(key=lambda g: (g["label"].startswith("·"), -len(g["items"]), g["label"]))
    return groups


def version_extra(platform: str, data: dict) -> str:
    if platform == "android" and data.get("versionCode"):
        return f"versionCode {data['versionCode']}"
    if platform == "mac" and data.get("build"):
        return f"build {data['build']}"
    return ""


def fmt_val(v: str, limit: int = 400) -> str:
    v = (v or "").replace("\n", "\\n")
    return v if len(v) <= limit else v[:limit] + " …"


def short_code(name: str) -> str:
    """Drop the com.whatsapp. prefix from a class/method name for readability."""
    return name[len("com.whatsapp."):] if name.startswith("com.whatsapp.") else name


def diff_code(methods_new: dict, methods_old: dict):
    """Diff the readable code surface (classes & methods) between two builds.

    This is the "function names" signal: WhatsApp obfuscates most code, but the
    readable com/whatsapp classes (extracted straight from the dex) are stable
    enough to diff, and a new readable class usually lands *before* any UI text
    does. Returns the cuts we report, or None when there's no methods extract
    for this platform (e.g. macOS):

      new_classes   — new top-level (non-synthetic) classes: the headline
                      signal (a new screen / manager / service / worker).
      removed_classes — top-level classes that disappeared.
      new_methods   — new readable methods on classes that *already existed*
                      (a capability added to an existing screen). Methods of
                      brand-new classes are omitted — the new class already
                      conveys them.

    Synthetic lambda/coroutine names (containing '$') are dropped as noise.
    """
    if not methods_new:
        return None
    new_c = set(methods_new.get("classes") or [])
    old_c = set((methods_old or {}).get("classes") or [])
    new_m = set(methods_new.get("methods") or [])
    old_m = set((methods_old or {}).get("methods") or [])
    added_c = new_c - old_c
    return {
        "new_classes": sorted(c for c in added_c if "$" not in c),
        "removed_classes": sorted(c for c in (old_c - new_c) if "$" not in c),
        "new_methods": sorted(
            m for m in (new_m - old_m)
            if "$" not in m and m.rsplit("#", 1)[0] not in added_c),
    }


def render_report(platform, new_data, prev_version, initial: bool, d: dict) -> str:
    ve = version_extra(platform, new_data)
    extra = f" ({ve})" if ve else ""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# WhatsApp {platform.capitalize()} beta — v{new_data.get('version')}{extra}", ""]
    if prev_version and not initial:
        lines.append(f"_Compared against v{prev_version} · generated {now}_")
    else:
        lines.append(f"_Generated {now}_")
    lines.append("")

    if initial:
        lines.append(f"> Initial baseline captured: {len(as_values(new_data.get('strings')))} text values, "
                     f"{len(new_data.get('components') or [])} components. "
                     "Future runs will diff against this.")
        lines.append("")
        return "\n".join(lines)

    def section(heading, items, fmt):
        if not items:
            return
        lines.append(f"## {heading} ({len(items)})")
        lines.append("")
        lines.extend(f"- {fmt(i)}" for i in items)
        lines.append("")

    code = lambda x: f"`{x}`"
    section("🧩 New screens / features", d["new_components"], code)
    section("🔐 New permissions", d["new_permissions"], code)

    # New texts, grouped by the real feature module that uses them.
    groups = d.get("new_groups")
    if groups:
        lines.append(f"## 🆕 New texts — possible new features ({len(d['new'])})")
        lines.append("")
        for g in groups:
            label = "uncategorized" if g["label"].startswith("·") else g["label"]
            lines.append(f"### `{label}` ({len(g['items'])})")
            lines.append("")
            lines.extend(f"- {fmt_val(i)}" for i in g["items"])
            lines.append("")
    else:
        section("🆕 New texts — possible new features", d["new"], fmt_val)

    section("✏️ Reworded — existing text, minor changes", d["reworded"],
            lambda p: f"{fmt_val(p[0])}  →  {fmt_val(p[1])}")
    section("➖ Removed texts", d["removed"], fmt_val)
    section("➖ Removed screens / features", d["removed_components"], code)

    # Code-surface signal: readable class/method names from the dex.
    cn = lambda x: f"`{short_code(x)}`"
    section("🧬 New classes / features — code surface", d.get("new_classes", []), cn)
    section("🧬 New methods on existing screens — code surface", d.get("new_methods", []), cn)
    section("➖ Removed classes — code surface", d.get("removed_classes", []), cn)
    return "\n".join(lines)


def process_platform(platform: str, extract_path: Path, area_index: dict = None):
    new_data = load_json(extract_path)
    if not new_data:
        print(f"== {platform}: no extract data, skipping")
        return None

    baseline_path = DATA / platform / "latest.json"
    old_data = load_json(baseline_path)
    # A schema change means the extractor now captures a different (usually much
    # larger) set of strings — diffing against the old baseline would report
    # thousands of bogus "new" texts. Treat it as a fresh baseline instead.
    schema_changed = (old_data is not None
                      and old_data.get("schema") != new_data.get("schema"))
    if schema_changed:
        print(f"== {platform}: extract schema changed "
              f"({old_data.get('schema')} → {new_data.get('schema')}); resetting baseline")
    initial = old_data is None or schema_changed
    prev_version = (old_data or {}).get("version")

    old_set = as_values((old_data or {}).get("strings"))
    new_set = as_values(new_data.get("strings"))
    old_comp = as_values((old_data or {}).get("components"))
    new_comp = as_values(new_data.get("components"))
    old_perm = as_values((old_data or {}).get("permissions"))
    new_perm = as_values(new_data.get("permissions"))

    version = new_data.get("version") or "unknown"
    # Pull areas out for labeling, but don't persist them in the committed
    # baseline/snapshot (5k+ entries; only needed transiently for this diff).
    string_areas = new_data.pop("string_areas", None) or {}

    # Code-surface signal (Android only): readable class/method names pulled
    # straight from the dex by extract_methods.py. Diffed against its own
    # baseline, kept separate from latest.json so that file stays small.
    methods_new = load_json(INCOMING / f"{platform}-methods" / f"{platform}-methods.json")
    methods_baseline = DATA / platform / "methods.json"
    code_initial = methods_new is not None and not methods_baseline.exists()
    code_diff = diff_code(methods_new, load_json(methods_baseline))
    if methods_new is not None:
        methods_baseline.parent.mkdir(parents=True, exist_ok=True)
        methods_baseline.write_text(
            json.dumps(methods_new, ensure_ascii=False, indent=2), encoding="utf-8")

    empty_code = {"new_classes": [], "new_methods": [], "removed_classes": []}
    if initial:
        d = {"new": [], "new_groups": [], "reworded": [], "removed": [],
             "new_components": [], "removed_components": [], "new_permissions": [],
             "removed_permissions": [], **empty_code}
        has_changes = True
    else:
        new_items, reworded, removed_only = classify_changes(
            sorted(new_set - old_set), sorted(old_set - new_set))
        d = {
            "new": new_items,
            "new_groups": group_new_by_area(new_items, string_areas, area_index),
            "reworded": reworded,
            "removed": removed_only,
            "new_components": sorted(new_comp - old_comp),
            "removed_components": sorted(old_comp - new_comp),
            "new_permissions": sorted(new_perm - old_perm),
            "removed_permissions": sorted(old_perm - new_perm),
            **empty_code,
        }
        # Skip code on the feature's first run (no methods baseline yet) — the
        # whole surface would otherwise look "new".
        if code_diff and not code_initial:
            d.update(code_diff)
        has_changes = any(d.values())

    if not has_changes:
        print(f"== {platform} v{version}: no changes")
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return None

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe_ver = str(version).replace("/", "_")
    report_dir = REPORTS / platform
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{date}_v{safe_ver}.md"
    report_path.write_text(
        render_report(platform, new_data, prev_version, initial, d), encoding="utf-8")
    report_rel = report_path.relative_to(ROOT).as_posix()
    print(f"== {platform} v{version}: {len(d['new_components'])} new screens / "
          f"{len(d['new'])} new texts / {len(d['reworded'])} reworded / "
          f"{len(d['removed'])} removed → {report_rel}")

    # Update baseline + version snapshot.
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(new_data, ensure_ascii=False, indent=2)
    baseline_path.write_text(payload, encoding="utf-8")
    snap_dir = DATA / platform / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / f"{safe_ver}.json").write_text(payload, encoding="utf-8")

    if initial:
        summary = f"{platform} v{version}: initial baseline ({len(new_set)} texts)"
    else:
        parts = []
        if d["new_components"]:
            parts.append(f"{len(d['new_components'])} new screens")
        if d["new_classes"]:
            parts.append(f"{len(d['new_classes'])} new classes")
        parts.append(f"{len(d['new'])} new texts")
        if d["reworded"]:
            parts.append(f"{len(d['reworded'])} reworded")
        if d["removed"]:
            parts.append(f"{len(d['removed'])} removed")
        summary = f"{platform} v{version}: " + ", ".join(parts)
    return {
        "platform": platform,
        "version": version,
        "prev_version": prev_version,
        "version_extra": version_extra(platform, new_data),
        "summary": summary,
        "report": report_rel,
        "initial": initial,
        "counts": {"texts": len(new_set)},
        **d,
    }


def append_changelog(results):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bullets = [f"- {r['summary']} — [report]({r['report']})" for r in results]
    new_block = f"## {date}\n\n" + "\n".join(bullets) + "\n\n"
    title = "# Changelog\n"
    existing = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else title + "\n"
    body = existing[len(title):].lstrip("\n") if existing.startswith(title) else existing
    CHANGELOG.write_text(title + "\n" + new_block + body, encoding="utf-8")


def set_output(name: str, value: str):
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        print(f"(local) {name}={value}")
        return
    with open(out, "a", encoding="utf-8") as fh:
        delim = f"__EOF_{name}__"
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def main() -> int:
    try:  # emoji/arrow-safe stdout on Windows consoles too
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    # Android strings carry code-derived module labels; reuse them to label
    # identical macOS strings (which have no module structure of their own).
    area_index = build_area_index(INCOMING / "android-extract" / "android-extract.json")
    if area_index:
        print(f"== cross-platform module index: {len(area_index)} labeled strings")

    results = []
    for platform in ("android", "mac"):
        extract_path = INCOMING / f"{platform}-extract" / f"{platform}-extract.json"
        res = process_platform(platform, extract_path, area_index)
        if res:
            results.append(res)

    changed = bool(results)
    set_output("changed", "true" if changed else "false")
    if results:
        summary = "\n".join(f"- {r['summary']}" for r in results)
        append_changelog(results)
    else:
        summary = "No changes detected."
    set_output("summary", summary)

    (ROOT / "notify.json").write_text(
        json.dumps({
            "changed": changed,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "runs": results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("## WhatsApp tracker run\n\n" + summary + "\n")
    print("\n=== SUMMARY ===\n" + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
