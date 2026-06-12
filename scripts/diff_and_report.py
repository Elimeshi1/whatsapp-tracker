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


def version_extra(platform: str, data: dict) -> str:
    if platform == "android" and data.get("versionCode"):
        return f"versionCode {data['versionCode']}"
    if platform == "mac" and data.get("build"):
        return f"build {data['build']}"
    return ""


def fmt_val(v: str, limit: int = 400) -> str:
    v = (v or "").replace("\n", "\\n")
    return v if len(v) <= limit else v[:limit] + " …"


def render_report(platform: str, new_data: dict, added, removed, prev_version, initial: bool) -> str:
    ve = version_extra(platform, new_data)
    extra = f" ({ve})" if ve else ""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"# WhatsApp {platform.capitalize()} beta — v{new_data.get('version')}{extra}"
    lines = [title, ""]
    if prev_version and not initial:
        lines.append(f"_Compared against v{prev_version} · generated {now}_")
    else:
        lines.append(f"_Generated {now}_")
    lines.append("")

    if initial:
        lines.append(f"> Initial baseline captured: {len(as_values(new_data.get('strings')))} text values. "
                     "Future runs will diff against this.")
        lines.append("")
        return "\n".join(lines)

    if added:
        lines.append(f"## ➕ New texts ({len(added)})")
        lines.append("")
        lines += [f"- {fmt_val(v)}" for v in added]
        lines.append("")
    if removed:
        lines.append(f"## ➖ Removed texts ({len(removed)})")
        lines.append("")
        lines += [f"- {fmt_val(v)}" for v in removed]
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
    prev_version = (old_data or {}).get("version")

    old_set = as_values((old_data or {}).get("strings"))
    new_set = as_values(new_data.get("strings"))
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)

    version = new_data.get("version") or "unknown"
    has_changes = initial or bool(added) or bool(removed)

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
        render_report(platform, new_data, added, removed, prev_version, initial), encoding="utf-8")
    report_rel = report_path.relative_to(ROOT).as_posix()
    print(f"== {platform} v{version}: +{len(added)} / -{len(removed)} → {report_rel}")

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
        summary = f"{platform} v{version}: +{len(added)} / -{len(removed)} texts"
    return {
        "platform": platform,
        "version": version,
        "prev_version": prev_version,
        "version_extra": version_extra(platform, new_data),
        "summary": summary,
        "report": report_rel,
        "initial": initial,
        "counts": {"texts": len(new_set)},
        "added": added,
        "removed": removed,
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
