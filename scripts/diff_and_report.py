#!/usr/bin/env python3
"""Compare freshly extracted data against the committed baseline and write reports.

For each platform present under ./incoming, this:
  - diffs strings / bools / integers against data/<platform>/latest.json
  - writes a Markdown report under reports/<platform>/ when something changed
  - updates the baseline (data/<platform>/latest.json) and a version snapshot
  - appends a one-line entry to CHANGELOG.md
  - emits GitHub Actions outputs:  changed=true|false  and  summary=<text>

Designed to run from the repo root inside the GitHub Actions "report" job.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCOMING = ROOT / "incoming"
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
CHANGELOG = ROOT / "CHANGELOG.md"

# Which dict-sections each platform contributes, in display order.
SECTIONS = {
    "android": [("strings", "Texts (strings)"), ("bools", "Feature toggles (bools)"),
                ("integers", "Tunables (integers)")],
    "mac": [("strings", "Texts (strings)")],
}


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARN: could not load {path}: {exc}", file=sys.stderr)
        return None


def diff_maps(old: dict, new: dict):
    """Return (added, removed, changed) for two {key: value} maps."""
    old = old or {}
    new = new or {}
    old_keys, new_keys = set(old), set(new)
    added = {k: new[k] for k in sorted(new_keys - old_keys)}
    removed = {k: old[k] for k in sorted(old_keys - new_keys)}
    changed = {k: (old[k], new[k]) for k in sorted(old_keys & new_keys) if old[k] != new[k]}
    return added, removed, changed


def fmt_val(v: str, limit: int = 300) -> str:
    v = (v or "").replace("\n", "\\n")
    return v if len(v) <= limit else v[:limit] + " …"


def render_report(platform: str, new_data: dict, diffs: dict, initial: bool) -> str:
    version = new_data.get("version")
    extra = ""
    if platform == "android" and new_data.get("versionCode"):
        extra = f" (versionCode {new_data['versionCode']})"
    if platform == "mac" and new_data.get("build"):
        extra = f" (build {new_data['build']})"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [f"# WhatsApp {platform.capitalize()} beta — v{version}{extra}", "", f"_Generated {now}_", ""]
    if initial:
        lines += ["> Initial baseline captured. Future runs will diff against this.", ""]
        for key, title in SECTIONS[platform]:
            lines.append(f"- **{title}:** {len(new_data.get(key, {}))} entries")
        lines.append("")
        return "\n".join(lines)

    for key, title in SECTIONS[platform]:
        added, removed, changed = diffs[key]
        if not (added or removed or changed):
            continue
        lines.append(f"## {title}")
        lines.append("")
        if added:
            lines.append(f"### ➕ Added ({len(added)})")
            lines.append("")
            for k, v in added.items():
                lines.append(f"- `{k}` = {fmt_val(v)}")
            lines.append("")
        if changed:
            lines.append(f"### ✏️ Changed ({len(changed)})")
            lines.append("")
            for k, (old, new) in changed.items():
                lines.append(f"- `{k}`")
                lines.append(f"  - before: {fmt_val(old)}")
                lines.append(f"  - after:  {fmt_val(new)}")
            lines.append("")
        if removed:
            lines.append(f"### ➖ Removed ({len(removed)})")
            lines.append("")
            for k, v in removed.items():
                lines.append(f"- `{k}` = {fmt_val(v)}")
            lines.append("")
    return "\n".join(lines)


def process_platform(platform: str, extract_path: Path):
    new_data = load_json(extract_path)
    if not new_data:
        print(f"== {platform}: no extract data, skipping")
        return None

    baseline_path = DATA / platform / "latest.json"
    old_data = load_json(baseline_path)
    initial = old_data is None

    diffs = {}
    total_changes = 0
    for key, _ in SECTIONS[platform]:
        added, removed, changed = diff_maps((old_data or {}).get(key), new_data.get(key))
        diffs[key] = (added, removed, changed)
        total_changes += len(added) + len(removed) + len(changed)

    version = new_data.get("version") or "unknown"
    has_changes = initial or total_changes > 0

    if not has_changes:
        print(f"== {platform} v{version}: no changes")
        # Still refresh baseline (e.g. version bump with identical content).
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2, sort_keys=True),
                                 encoding="utf-8")
        return None

    # Write report.
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_dir = REPORTS / platform
    report_dir.mkdir(parents=True, exist_ok=True)
    safe_ver = str(version).replace("/", "_")
    report_path = report_dir / f"{date}_v{safe_ver}.md"
    report_path.write_text(render_report(platform, new_data, diffs, initial), encoding="utf-8")
    report_rel = report_path.relative_to(ROOT).as_posix()
    print(f"== {platform} v{version}: wrote {report_rel}")

    # Update baseline + keep a version snapshot.
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(new_data, ensure_ascii=False, indent=2, sort_keys=True)
    baseline_path.write_text(payload, encoding="utf-8")
    snap_dir = DATA / platform / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / f"{safe_ver}.json").write_text(payload, encoding="utf-8")

    # Build a compact summary.
    if initial:
        summary = f"{platform} v{version}: initial baseline"
    else:
        parts = []
        for key, title in SECTIONS[platform]:
            a, r, c = diffs[key]
            if a or r or c:
                parts.append(f"{title.split(' (')[0].lower()} +{len(a)}/~{len(c)}/-{len(r)}")
        summary = f"{platform} v{version}: " + ", ".join(parts)
    return {
        "platform": platform,
        "version": version,
        "summary": summary,
        "report": report_rel,
        "initial": initial,
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
    # Multi-line safe.
    with open(out, "a", encoding="utf-8") as fh:
        delim = f"__EOF_{name}__"
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def main() -> int:
    results = []
    for platform in ("android", "mac"):
        extract_path = INCOMING / f"{platform}-extract" / f"{platform}-extract.json"
        res = process_platform(platform, extract_path)
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

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("## WhatsApp tracker run\n\n" + summary + "\n")
    print("\n=== SUMMARY ===\n" + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
