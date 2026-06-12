#!/usr/bin/env python3
"""Send formatted change notifications via Telegram and/or email.

Reads notify.json (produced by diff_and_report.py) and, for each platform that
changed, sends a clear message showing exactly what was added / changed /
removed, with the full Markdown report attached.

Channels are enabled purely by presence of secrets (set as env vars):

  Telegram:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  Email:     MAIL_USERNAME, MAIL_PASSWORD, MAIL_TO
             (optional: MAIL_HOST=smtp.gmail.com, MAIL_PORT=465, MAIL_FROM)

If no channel is configured it prints a notice and exits 0 (never fails the run).
Set NOTIFY_DRY_RUN=1 to render to stdout/files instead of sending.

Stdlib only. Usage: notify.py [notify.json]
"""
import html
import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from email.message import EmailMessage
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

RICH_MAX_ITEMS = 60       # per category — keeps under the 500-block limit
RICH_CHAR_BUDGET = 30000  # under the 32768-char rich-message hard limit


def esc(s: str) -> str:
    """Escape the only entities the rich HTML parser needs (& < >)."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _detail_block(emoji_label: str, items_html: str, count: int, open_: bool) -> str:
    attr = " open" if open_ else ""
    return (f"<details{attr}><summary><b>{emoji_label} ({count})</b></summary>"
            f"<ul>{items_html}</ul></details>")


def rich_html(run: dict, generated: str) -> str:
    """Build the rich-message HTML document for one platform run."""
    emoji = PLATFORM_EMOJI.get(run["platform"], "📱")
    ver = esc(str(run["version"]))
    extra = f" · {esc(run['version_extra'])}" if run.get("version_extra") else ""
    prev = run.get("prev_version")

    head = [f"<h3>{emoji} WhatsApp {run['platform'].capitalize()} beta</h3>"]
    if prev and not run.get("initial"):
        head.append(f"<p>{esc(str(prev))} &rarr; <b>{ver}</b>{extra}</p>")
    else:
        head.append(f"<p><b>{ver}</b>{extra}</p>")

    if run.get("initial"):
        counts = ", ".join(f"{v} {esc(k)}" for k, v in run.get("counts", {}).items())
        head.append(f"<p><i>Initial baseline captured</i> ({esc(counts)}).</p>")
        return "".join(head)

    added = run.get("added", [])
    removed = run.get("removed", [])

    def li_values(values):
        out = []
        for i, v in enumerate(values):
            if i >= RICH_MAX_ITEMS:
                out.append(f"<li>… +{len(values) - i} more</li>")
                break
            out.append(f"<li>{esc(trunc(v, 200))}</li>")
        return "".join(out)

    def build(with_details: bool) -> str:
        parts = list(head)
        parts.append("<hr/>")
        parts.append(f"<p>➕ <b>{len(added)} new</b> &nbsp;&nbsp; ➖ {len(removed)} removed</p>")
        if with_details:
            if added:
                parts.append(_detail_block("➕ New texts", li_values(added), len(added), open_=True))
            if removed:
                parts.append(_detail_block("➖ Removed texts", li_values(removed), len(removed), open_=False))
        if generated:
            parts.append(f"<footer>WhatsApp beta tracker · {esc(generated)}</footer>")
        return "".join(parts)

    doc = build(with_details=True)
    if len(doc) > RICH_CHAR_BUDGET:
        # Large update: section summaries only, to stay within the char limit.
        doc = build(with_details=False)
        doc += "<p><i>Large update — open the full report in the repository.</i></p>"
    return doc


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
    added = run.get("added", [])
    removed = run.get("removed", [])
    lines.append(f"\n➕ <b>{len(added)} new</b> · ➖ {len(removed)} removed")
    for v in added[:40]:
        lines.append(f"➕ {esc(trunc(v))}")
    for v in removed[:15]:
        lines.append(f"➖ {esc(trunc(v))}")
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
        doc = rich_html(run, generated)
        if DRY:
            print(f"\n----- TELEGRAM rich HTML ({run['platform']}) -----\n{doc}")
            continue
        try:
            tg_send_rich(token, chat_id, doc)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, still notify
            print(f"notify: sendRichMessage failed ({exc}); using sendMessage", file=sys.stderr)
            tg_send_message(token, chat_id, basic_html(run), parse_mode="HTML")
    print("Telegram: sent" if not DRY else "Telegram: dry-run rendered")


# ------------------------------------------------------------------- Email ---

def html_run(run: dict) -> str:
    emoji = PLATFORM_EMOJI.get(run["platform"], "📱")
    ve = f" &middot; {html.escape(run['version_extra'])}" if run.get("version_extra") else ""
    out = [f"<h2 style='margin:18px 0 6px'>{emoji} WhatsApp {run['platform'].capitalize()} "
           f"beta v{html.escape(str(run['version']))}{ve}</h2>"]
    prev = run.get("prev_version")
    if prev and not run.get("initial"):
        out.append(f"<p style='color:#5f6368;margin:0 0 8px'>compared against v{html.escape(str(prev))}</p>")
    if run.get("initial"):
        texts = run.get("counts", {}).get("texts", 0)
        out.append(f"<p><i>Initial baseline captured</i> ({texts} texts).</p>")
        return "\n".join(out)

    added = run.get("added", [])
    removed = run.get("removed", [])
    if added:
        out.append(f"<p style='margin:10px 0 4px;color:#137333'><b>➕ New texts ({len(added)})</b></p><ul style='margin:0'>")
        out += [f"<li>{html.escape(trunc(v, 400))}</li>" for v in added]
        out.append("</ul>")
    if removed:
        out.append(f"<p style='margin:10px 0 4px;color:#c5221f'><b>➖ Removed texts ({len(removed)})</b></p><ul style='margin:0'>")
        out += [f"<li>{html.escape(trunc(v, 400))}</li>" for v in removed]
        out.append("</ul>")
    return "\n".join(out)


def build_email_html(payload: dict) -> str:
    body = "\n".join(html_run(r) for r in payload["runs"])
    return (f"<div style='font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
            f"font-size:14px;color:#202124;max-width:720px'>"
            f"<p style='color:#5f6368'>WhatsApp beta tracker · {html.escape(payload['generated'])}</p>"
            f"{body}"
            f"<hr style='margin-top:24px;border:none;border-top:1px solid #eee'>"
            f"<p style='color:#9aa0a6;font-size:12px'>Full reports are attached as Markdown.</p></div>")


def send_email(payload: dict):
    user = os.environ.get("MAIL_USERNAME", "dry@example.com")
    password = os.environ.get("MAIL_PASSWORD", "DRY")
    to_addr = os.environ.get("MAIL_TO", "dry@example.com")
    host = os.environ.get("MAIL_HOST", "smtp.gmail.com")
    port = int(os.environ.get("MAIL_PORT", "465"))
    from_addr = os.environ.get("MAIL_FROM", user)

    subject = "WhatsApp beta changes: " + "; ".join(
        f"{r['platform']} v{r['version']}" for r in payload["runs"])

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content("This is an HTML email. See the changes in an HTML-capable client.")
    msg.add_alternative(build_email_html(payload), subtype="html")

    for run in payload["runs"]:
        report = ROOT / run["report"]
        if report.exists():
            msg.add_attachment(report.read_bytes(), maintype="text", subtype="markdown",
                               filename=report.name)

    if DRY:
        out = ROOT / "notify_email_preview.html"
        out.write_text(build_email_html(payload), encoding="utf-8")
        print(f"\n----- EMAIL -----\nSubject: {subject}\nTo: {to_addr}\nHTML preview -> {out}")
        return

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
    print("Email: sent")


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
    mail_ok = all(os.environ.get(k) for k in ("MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_TO"))

    if not (tg_ok or mail_ok) and not DRY:
        print("notify: no channel configured (set TELEGRAM_* and/or MAIL_* secrets). Skipping.")
        return 0

    if tg_ok or DRY:
        try:
            send_telegram(payload)
        except Exception as exc:  # noqa: BLE001
            print(f"notify: Telegram failed: {exc}", file=sys.stderr)
    if mail_ok or DRY:
        try:
            send_email(payload)
        except Exception as exc:  # noqa: BLE001
            print(f"notify: email failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
