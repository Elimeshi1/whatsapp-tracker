#!/usr/bin/env python3
"""Send formatted change notifications via Telegram.

Reads notify.json (produced by diff_and_report.py) and, for each platform that
changed, sends a clear message showing exactly what was added / changed /
removed — the whole diff inline.

Enabled purely by presence of secrets (set as env vars):

  Telegram:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

If it isn't configured it prints a notice and exits 0 (never fails the run).
Set NOTIFY_DRY_RUN=1 to render to stdout instead of sending.

Stdlib only. Usage: notify.py [notify.json]
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 3900  # leave headroom under Telegram's 4096 cap
DRY = os.environ.get("NOTIFY_DRY_RUN") == "1"

PLATFORM_EMOJI = {"android": "🤖", "mac": "🍎"}


def trunc(v: str, limit: int = 200) -> str:
    v = (v or "").replace("\n", " ")
    return v if len(v) <= limit else v[:limit] + "…"


# ---------------------------------------------------------------- Telegram ---
# Uses Telegram's "Rich Messages" (Bot API 10.1, June 2026) via sendRichMessage
# with an HTML document: section headings, a divider, and collapsible <details>
# blocks for the added / changed / removed entries — the whole diff inline, no
# attachment. Falls back to a plain sendMessage if sendRichMessage ever errors.

RICH_CHAR_BUDGET = 30000   # under the 32768-char rich-message hard limit
RICH_BLOCK_BUDGET = 460    # under the 500-block hard limit


def esc(s: str) -> str:
    """Escape the only entities the rich HTML parser needs (& < >).

    Note: only &amp;/&lt;/&gt; survive Telegram's HTML parser. Other named
    entities (&rarr;, &nbsp; …) show up literally, so use real Unicode chars
    (→, ·, spaces) in the markup instead of entities.
    """
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def plain(s: str) -> str:
    """Strip HTML markup from a WhatsApp string so it reads cleanly in a message.

    WhatsApp values embed tags like <a href="…">…</a> and <highlight>…</highlight>;
    shown raw they're noise. Drop the tags, keep the text, collapse whitespace.
    """
    return _WS.sub(" ", _TAGS.sub("", s or "")).strip()


def reword_diff(old: str, new: str):
    """Split a reword into (context_prefix, old_fragment, new_fragment, suffix).

    Strips tags, then finds the shared leading and trailing words so we can show
    the whole sentence as context (which identifies *which* string changed) while
    pointing at the exact fragment that changed. e.g. for
    "… your number. More about usernames" → "… your number. Learn more" it returns
    prefix="… your number.", old="More about usernames", new="Learn more", suffix="".
    """
    o, n = plain(old), plain(new)
    i = 0
    while i < len(o) and i < len(n) and o[i] == n[i]:
        i += 1
    while i > 0 and o[i - 1] != " ":            # back up to a word boundary
        i -= 1
    j = 0
    while j < len(o) - i and j < len(n) - i and o[-1 - j] == n[-1 - j]:
        j += 1
    while j > 0 and o[len(o) - j] != " ":       # forward to a word boundary
        j -= 1
    prefix = o[:i].strip()
    suffix = (o[len(o) - j:].strip() if j > 0 else "")
    o_mid = (o[i:len(o) - j] if j > 0 else o[i:]).strip()
    n_mid = (n[i:len(n) - j] if j > 0 else n[i:]).strip()
    return prefix, o_mid, n_mid, suffix


def reword_html(old: str, new: str, limit: int = 220) -> str:
    """Render a reword as: context with the new fragment bold + what it replaced.

    Shows enough of the sentence to tell entries apart, the new wording in bold,
    and "(was: …)" so the change is unambiguous. Falls back to a plain
    old → new when there's no shared context to anchor on.
    """
    prefix, o_mid, n_mid, suffix = reword_diff(old, new)
    if not prefix and not suffix:               # nothing shared — show both fully
        return f"{esc(trunc(o_mid, 120))} → <b>{esc(trunc(n_mid, 140))}</b>"
    ctx = " ".join(p for p in (esc(trunc(prefix, limit)), f"<b>{esc(n_mid)}</b>", esc(suffix)) if p.strip())
    return f"{ctx}  (was: {esc(trunc(o_mid, 80))})"


def short_component(name: str) -> str:
    """Drop the com.whatsapp. prefix from a component/permission for readability."""
    for p in ("com.whatsapp.", "android.permission.", "com.whatsapp"):
        if name.startswith(p):
            return name[len(p):].lstrip(".")
    return name


def _detail(summary_label: str, items_html: str) -> str:
    # Always collapsed — no section is open by default.
    return f"<details><summary><b>{summary_label}</b></summary><ul>{items_html}</ul></details>"


# Grammatical / filler words ignored when auto-clustering texts by shared word.
_STOP = set("""the a an and or to of for is are be been will would can could should you your yours this that
these those it its from as at by was were if but so no not yes when then we our us they them their he she his
her i me my mine do does did have has had get got now more all any some one only just please try again tap go
see use used make sure need want let via with about into than too also new whatsapp couldn didn doesn won isn
aren wasn don wouldn shouldn hasn haven wont cant what which who how why here there add set onto your
href http https www html font face span learn more about back next done okay""".split())


def _stem(w: str) -> str:
    """Crude singularizer so message/messages cluster together."""
    if len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _word_list(s: str):
    return [w for w in re.findall(r"[a-z]+", s.lower()) if len(w) >= 4 and w not in _STOP]


def auto_group(values, min_size=3):
    """Cluster values automatically by their most frequent shared content word.

    Labels emerge from the data (no predefined list): each value is attached to
    the most common (singularized) word it contains; words shared by < min_size
    values fall to "Other". Returns [(label, [values...]), ...], largest first.
    """
    docs = [(v, _word_list(v)) for v in values]
    freq = Counter()
    for _, ws in docs:
        freq.update({_stem(w) for w in ws})
    assigned = [False] * len(docs)
    groups = []
    for stem, count in freq.most_common():
        if count < min_size:
            break
        members = [i for i, (_, ws) in enumerate(docs)
                   if not assigned[i] and stem in {_stem(w) for w in ws}]
        if len(members) >= min_size:
            for i in members:
                assigned[i] = True
            # Label with the most common surface form mapping to this stem.
            surface = Counter(w for i in members for w in docs[i][1] if _stem(w) == stem)
            groups.append((surface.most_common(1)[0][0].capitalize(),
                           [docs[i][0] for i in members]))
    other = [v for i, (v, _) in enumerate(docs) if not assigned[i]]
    if other:
        groups.append(("Other", other))
    return groups


def group_components(comps):
    """Group component class names by their first package segment (file-based)."""
    groups = {}
    for c in comps:
        seg = c.replace("com.whatsapp.", "").split(".")[0] or "(root)"
        groups.setdefault(seg, []).append(c)
    return sorted(groups.items())


def new_text_sections(run: dict):
    """Return [(label, [values...]), ...] for the new texts.

    Prefers the real feature modules from new_groups (built in extract by tying
    each string's resource id to the readable class that uses it). Strings with
    no readable owner ("· uncategorized") are still split by their shared word so
    they keep some structure — but labeled "other · …" so it's clear they're a
    heuristic, not WhatsApp's own module. Falls back to pure word-clustering when
    new_groups is absent (e.g. macOS, which has no code cross-reference).
    """
    groups = run.get("new_groups")
    out = []
    if groups:
        for g in groups:
            if g["label"].startswith("·"):
                for sub, items in auto_group(g["items"]):
                    out.append(("other" if sub == "Other" else f"other · {sub.lower()}", items))
            else:
                out.append((g["label"], g["items"]))
    else:
        out = list(auto_group(run.get("new", [])))
    return out


def _run_header(run: dict) -> str:
    emoji = PLATFORM_EMOJI.get(run["platform"], "📱")
    ver = esc(str(run["version"]))
    extra = f" · {esc(run['version_extra'])}" if run.get("version_extra") else ""
    prev = run.get("prev_version")
    head = f"<h3>{emoji} WhatsApp {run['platform'].capitalize()} beta</h3>"
    if prev and not run.get("initial"):
        head += f"<p>{esc(str(prev))} → <b>{ver}</b>{extra}</p>"
    else:
        head += f"<p><b>{ver}</b>{extra}</p>"
    return head


def _summary_line(run: dict) -> str:
    nc, nt = len(run.get("new_components", [])), len(run.get("new", []))
    rw, rm = len(run.get("reworded", [])), len(run.get("removed", []))
    parts = []
    if nc:
        parts.append(f"🧩 <b>{nc} new screens</b>")
    parts.append(f"🆕 <b>{nt} new texts</b>")
    if rw:
        parts.append(f"✏️ {rw} reworded")
    if rm:
        parts.append(f"➖ {rm} removed")
    return "<p>" + "  ·  ".join(parts) + "</p>"


def _visible_screens(run: dict) -> str:
    """New screens/permissions shown inline (not collapsed), grouped by package."""
    out = ""
    nc = run.get("new_components", [])
    if nc:
        out += f"<p><b>🧩 New screens / features ({len(nc)})</b></p><ul>"
        for pkg, items in group_components(nc):
            names = ", ".join(short_component(c).split(".")[-1] for c in items)
            out += f"<li><b>{esc(pkg)}</b> — {esc(names)}</li>"
        out += "</ul>"
    np = run.get("new_permissions", [])
    if np:
        lis = "".join(f"<li><code>{esc(short_component(p))}</code></li>" for p in np)
        out += f"<p><b>🔐 New permissions ({len(np)})</b></p><ul>{lis}</ul>"
    return out


def _run_sections(run: dict):
    """Collapsed (label, [<li> html, ...]) sections: new texts in automatic
    word-clusters, then reworded and removed."""
    secs = []
    for label, items in new_text_sections(run):
        secs.append((f"🆕 {label}", [f"<li>{esc(trunc(plain(v), 260))}</li>" for v in items]))
    rew = run.get("reworded", [])
    if rew:
        secs.append(("✏️ Reworded — minor text changes",
                     [f"<li>{reword_html(p[0], p[1])}</li>" for p in rew]))
    rem = run.get("removed", [])
    if rem:
        secs.append(("➖ Removed texts", [f"<li>{esc(trunc(plain(v), 180))}</li>" for v in rem]))
    rmc = run.get("removed_components", [])
    if rmc:
        secs.append(("➖ Removed screens / features",
                     [f"<li><code>{esc(short_component(c))}</code></li>" for c in rmc]))
    return secs


def rich_messages(run: dict, generated: str):
    """Paginate one run into one or more rich HTML docs within Telegram limits.

    New screens are shown inline (always visible); the topic-grouped text lists
    are collapsed. No section is open by default and there are no per-section
    caps — sections are split across messages only if one would exceed limits.
    """
    if run.get("initial"):
        texts = run.get("counts", {}).get("texts", 0)
        return [_run_header(run) + f"<p><i>Initial baseline captured</i> ({texts} texts).</p>"]

    header = _run_header(run) + "<hr/>" + _summary_line(run) + _visible_screens(run)
    footer = f"<footer>WhatsApp beta tracker · {esc(generated)}</footer>" if generated else ""
    messages, cur, blocks = [], header, 4

    for label, items in _run_sections(run):
        idx, first = 0, True
        while idx < len(items):
            base = len(cur) + len(footer) + 80
            chunk, clen = [], len(label) + 40
            while idx < len(items):
                it = items[idx]
                if base + clen + len(it) > RICH_CHAR_BUDGET or blocks + len(chunk) + 2 > RICH_BLOCK_BUDGET:
                    break
                chunk.append(it)
                clen += len(it)
                idx += 1
            if not chunk:                       # current message is full → flush
                messages.append(cur + footer)
                cur, blocks = "", 0
                continue
            summary_label = f"{label} ({len(items)})" if first else f"{label} (cont.)"
            cur += _detail(summary_label, "".join(chunk))
            blocks += len(chunk) + 2
            first = False
    if cur:
        messages.append(cur + footer)
    return messages or [header + footer]


def basic_html(run: dict) -> str:
    """sendMessage-compatible HTML (fallback if sendRichMessage is unavailable)."""
    emoji = PLATFORM_EMOJI.get(run["platform"], "📱")
    prev = f"{esc(str(run['prev_version']))} → " if run.get("prev_version") else ""
    lines = [f"{emoji} <b>WhatsApp {run['platform'].capitalize()} beta</b> "
             f"{prev}<b>{esc(str(run['version']))}</b>"]
    if run.get("initial"):
        texts = run.get("counts", {}).get("texts", 0)
        lines.append(f"<i>Initial baseline captured</i> ({texts} texts).")
        return "\n".join(lines)
    nc = run.get("new_components", [])
    lines.append(f"\n🧩 <b>{len(nc)} new screens</b> · 🆕 <b>{len(run.get('new', []))} new texts</b> · "
                 f"✏️ {len(run.get('reworded', []))} reworded · ➖ {len(run.get('removed', []))} removed")
    if nc:
        lines.append("\n<b>🧩 New screens:</b>")
        for pkg, items in group_components(nc):
            names = ", ".join(short_component(c).split(".")[-1] for c in items)
            lines.append(f"• <b>{esc(pkg)}</b> — {esc(names)}")
    for label, items in new_text_sections(run)[:6]:
        lines.append(f"\n<b>🆕 {esc(label)}:</b>")
        lines += [f"• {esc(trunc(plain(v)))}" for v in items[:8]]
    rew = run.get("reworded", [])
    if rew:
        lines.append("\n<b>✏️ Reworded:</b>")
        for p in rew[:8]:
            lines.append(f"• {reword_html(p[0], p[1], limit=140)}")
    return "\n".join(lines)[:TG_LIMIT]


def tg_post(token: str, method: str, data: bytes, headers: dict):
    url = TG_API.format(token=token, method=method)
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def tg_send_rich(token: str, chat_id: str, html_doc: str):
    rich = {"html": html_doc}
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "rich_message": json.dumps(rich, ensure_ascii=False),
    }).encode()
    return tg_post(token, "sendRichMessage", data,
                   {"Content-Type": "application/x-www-form-urlencoded"})


def tg_send_message(token: str, chat_id: str, text: str, parse_mode: str = "HTML"):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": parse_mode, "disable_web_page_preview": "true",
    }).encode()
    return tg_post(token, "sendMessage", data,
                   {"Content-Type": "application/x-www-form-urlencoded"})


def send_telegram(payload: dict):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "DRY")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "DRY")
    generated = payload.get("generated", "")
    for run in payload["runs"]:
        docs = rich_messages(run, generated)
        if DRY:
            for i, doc in enumerate(docs, 1):
                print(f"\n----- TELEGRAM rich msg {i}/{len(docs)} ({run['platform']}) -----\n{doc}")
            continue
        try:
            for doc in docs:
                tg_send_rich(token, chat_id, doc)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, still notify
            print(f"notify: sendRichMessage failed ({exc}); using sendMessage", file=sys.stderr)
            tg_send_message(token, chat_id, basic_html(run), parse_mode="HTML")
    print("Telegram: sent" if not DRY else "Telegram: dry-run rendered")


# -------------------------------------------------------------------- main ---

def main() -> int:
    try:  # keep emoji-safe stdout on Windows consoles too
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "notify.json"
    if not path.exists():
        print("notify: no notify.json, nothing to do")
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("changed") or not payload.get("runs"):
        print("notify: no changes, nothing to send")
        return 0

    tg_ok = bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))

    if not tg_ok and not DRY:
        print("notify: Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID). Skipping.")
        return 0

    try:
        send_telegram(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"notify: Telegram failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
