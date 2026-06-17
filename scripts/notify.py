#!/usr/bin/env python3
"""Send formatted change notifications via Telegram.

Reads notify.json (produced by diff_and_report.py) and, for each platform that
changed, sends a clear message showing exactly what was added / changed /
removed — the whole diff inline.

Enabled purely by presence of secrets (set as env vars):

  Telegram:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

If it isn't configured it prints a notice and exits 0 (never fails the run).
Set NOTIFY_DRY_RUN=1 to render to stdout instead of sending.

Stdlib only. Usage:
  notify.py [notify.json]        send the change notification(s)
  notify.py --alert <html>       send a one-off alert (CI failure handler)
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
    ncl = len(run.get("new_classes", []))
    parts = []
    if nc:
        parts.append(f"🧩 <b>{nc} new screens</b>")
    if ncl:
        parts.append(f"🧬 <b>{ncl} new classes</b>")
    parts.append(f"🆕 <b>{nt} new texts</b>")
    if rw:
        parts.append(f"✏️ {rw} reworded")
    if rm:
        parts.append(f"➖ {rm} removed")
    return "<p>" + "  ·  ".join(parts) + "</p>"


# Clear visual break between the UI-text changes and the code-surface changes.
CODE_DIVIDER = "<hr/><h3>🧬 Code changes — classes &amp; methods</h3>"


def _grouped_items(names, strip=lambda c: c):
    """Render names grouped by first package segment into <li> rows, one per
    package: "<b>module</b> — Foo, Bar". Keeps long class/screen lists scannable."""
    items = []
    for pkg, members in group_components(names):
        leaves = ", ".join(strip(c).split(".")[-1] for c in members)
        items.append(f"<li><b>{esc(pkg)}</b> — {esc(leaves)}</li>")
    return items


def _string_sections(run: dict):
    """(label, [<li> …]) sections for the UI-text changes (shown first).

    Labels carry their own count; new texts are split into the automatic
    word/module clusters."""
    secs = []
    for label, items in new_text_sections(run):
        secs.append((f"🆕 {label} ({len(items)})",
                     [f"<li>{esc(trunc(plain(v), 260))}</li>" for v in items]))
    rew = run.get("reworded", [])
    if rew:
        secs.append((f"✏️ Reworded — minor text changes ({len(rew)})",
                     [f"<li>{reword_html(p[0], p[1])}</li>" for p in rew]))
    rem = run.get("removed", [])
    if rem:
        secs.append((f"➖ Removed texts ({len(rem)})",
                     [f"<li>{esc(trunc(plain(v), 180))}</li>" for v in rem]))
    return secs


def _code_sections(run: dict):
    """(label, [<li> …]) sections for the code-surface changes (shown below the
    divider): new screens, classes, methods, permissions, then removals."""
    secs = []
    nc = run.get("new_components", [])
    if nc:
        secs.append((f"🧩 New screens / features ({len(nc)})",
                     _grouped_items(nc, short_component)))
    ncl = run.get("new_classes", [])
    if ncl:
        secs.append((f"🧬 New classes / features ({len(ncl)})", _grouped_items(ncl)))
    nm = run.get("new_methods", [])
    if nm:
        secs.append((f"🧬 New methods on existing screens ({len(nm)})",
                     [f"<li><code>{esc(short_component(m))}</code></li>" for m in nm]))
    np = run.get("new_permissions", [])
    if np:
        secs.append((f"🔐 New permissions ({len(np)})",
                     [f"<li><code>{esc(short_component(p))}</code></li>" for p in np]))
    rmc = run.get("removed_components", [])
    if rmc:
        secs.append((f"➖ Removed screens / features ({len(rmc)})",
                     [f"<li><code>{esc(short_component(c))}</code></li>" for c in rmc]))
    rcl = run.get("removed_classes", [])
    if rcl:
        secs.append((f"➖ Removed classes ({len(rcl)})",
                     [f"<li><code>{esc(short_component(c))}</code></li>" for c in rcl]))
    return secs


def _renderables(run: dict):
    """The ordered blocks of one message, before pagination.

    Each is ("details", label, items) — a collapsed section — or ("raw", html),
    emitted verbatim. Layout: UI-text sections first, then a divider, then the
    code-surface sections.
    """
    blocks = [("details", lbl, items) for lbl, items in _string_sections(run)]
    code = _code_sections(run)
    if code:
        blocks.append(("raw", CODE_DIVIDER))
        blocks += [("details", lbl, items) for lbl, items in code]
    return blocks


def _fit_chunk(items, start, base_len, label, blocks):
    """Greedily take items[start:] that still fit one message.

    Returns (chunk, next_index). Stops at whichever Telegram limit is hit first:
    the per-message character budget or the block-count budget. An empty chunk
    means not even one more item fits — the caller should flush and retry.
    """
    chunk, clen, idx = [], len(label) + 40, start
    while idx < len(items):
        item = items[idx]
        if (base_len + clen + len(item) > RICH_CHAR_BUDGET
                or blocks + len(chunk) + 2 > RICH_BLOCK_BUDGET):
            break
        chunk.append(item)
        clen += len(item)
        idx += 1
    return chunk, idx


def rich_messages(run: dict, generated: str):
    """Paginate one run into one or more rich HTML docs within Telegram limits.

    A visible header + at-a-glance summary line, then every section as a collapsed
    <details> (closed by default, tap to open). Sections are split across messages
    only if one would exceed Telegram's char/block limits; a continued section is
    re-labelled "(cont.)".
    """
    if run.get("initial"):
        texts = run.get("counts", {}).get("texts", 0)
        return [_run_header(run) + f"<p><i>Initial baseline captured</i> ({texts} texts).</p>"]

    header = _run_header(run) + "<hr/>" + _summary_line(run)
    footer = f"<footer>WhatsApp beta tracker · {esc(generated)}</footer>" if generated else ""

    messages = []
    cur, blocks = header, 4

    def flush():
        nonlocal cur, blocks
        messages.append(cur + footer)
        cur, blocks = "", 0

    for kind, *rest in _renderables(run):
        if kind == "raw":
            html = rest[0]
            if len(cur) + len(html) + len(footer) + 80 > RICH_CHAR_BUDGET:
                flush()
            cur += html
            blocks += 1
            continue
        label, items = rest
        idx, first = 0, True
        while idx < len(items):
            base = len(cur) + len(footer) + 80
            chunk, idx = _fit_chunk(items, idx, base, label, blocks)
            if not chunk:                       # message full → flush and retry
                flush()
                continue
            cur += _detail(label if first else f"{label} (cont.)", "".join(chunk))
            blocks += len(chunk) + 2
            first = False
    if cur:
        flush()
    return messages or [header + footer]


def _blockquote_section(label: str, items: list) -> str:
    """A section as an expandable (collapsed-by-default) blockquote for the
    plain sendMessage fallback: bold heading + <blockquote expandable> body.
    Converts the rich <li> rows into bullet lines (sendMessage HTML has no <ul>)."""
    body = "".join(items).replace("<li>", "• ").replace("</li>", "\n").rstrip("\n")
    return f"<b>{esc_label(label)}</b>\n<blockquote expandable>{body}</blockquote>"


def esc_label(label: str) -> str:
    # Labels are our own text (with emoji); only the HTML-significant chars matter.
    return label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def basic_html(run: dict) -> str:
    """sendMessage-compatible HTML (fallback if sendRichMessage is unavailable).

    Same layout as the rich version — summary, UI-text sections, a divider, then
    code sections — but each section is an expandable blockquote (collapsed by
    default), which plain sendMessage supports."""
    emoji = PLATFORM_EMOJI.get(run["platform"], "📱")
    prev = f"{esc(str(run['prev_version']))} → " if run.get("prev_version") else ""
    lines = [f"{emoji} <b>WhatsApp {run['platform'].capitalize()} beta</b> "
             f"{prev}<b>{esc(str(run['version']))}</b>"]
    if run.get("initial"):
        texts = run.get("counts", {}).get("texts", 0)
        lines.append(f"<i>Initial baseline captured</i> ({texts} texts).")
        return "\n".join(lines)

    nc, ncl = len(run.get("new_components", [])), len(run.get("new_classes", []))
    summ = []
    if nc:
        summ.append(f"🧩 <b>{nc} new screens</b>")
    if ncl:
        summ.append(f"🧬 <b>{ncl} new classes</b>")
    summ.append(f"🆕 <b>{len(run.get('new', []))} new texts</b>")
    if run.get("reworded"):
        summ.append(f"✏️ {len(run['reworded'])} reworded")
    if run.get("removed"):
        summ.append(f"➖ {len(run['removed'])} removed")
    lines.append("\n" + "  ·  ".join(summ))

    for label, items in _string_sections(run):
        lines.append("\n" + _blockquote_section(label, items))
    code = _code_sections(run)
    if code:
        lines.append("\n➖➖➖➖➖  🧬 <b>Code changes</b>  ➖➖➖➖➖")
        for label, items in code:
            lines.append("\n" + _blockquote_section(label, items))
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


def send_alert(text: str):
    """Send a one-off plain HTML alert (used by CI when a step fails).

    Best-effort and never raises: if Telegram isn't configured it just prints a
    notice, so it can't itself fail the failure-handling step that calls it.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if DRY:
        print(f"[alert dry-run] {text}")
        return
    if not (token and chat_id):
        print("notify: Telegram not configured; alert skipped")
        return
    try:
        tg_send_message(token, chat_id, text, parse_mode="HTML")
        print("Telegram: alert sent")
    except Exception as exc:  # noqa: BLE001 — never let the alert fail the job
        print(f"notify: alert send failed: {exc}", file=sys.stderr)


# -------------------------------------------------------------------- main ---

def main() -> int:
    try:  # keep emoji-safe stdout on Windows consoles too
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    # Alert mode: `notify.py --alert <html message>` sends one plain message.
    # Used by the workflow's failure handler; unrelated to notify.json.
    if len(sys.argv) > 1 and sys.argv[1] == "--alert":
        send_alert(" ".join(sys.argv[2:]) or "⚠️ WhatsApp tracker: a run failed.")
        return 0

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
