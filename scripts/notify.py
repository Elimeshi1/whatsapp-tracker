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
import uuid
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
# Uses MarkdownV2 with collapsible (expandable) blockquotes — Telegram's
# document-grade formatting. Details per section are tucked into an expandable
# quote so the message is compact but fully inspectable with one tap.

_MDV2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!\\")


def mdv2(s: str) -> str:
    """Escape arbitrary text for Telegram MarkdownV2."""
    return "".join("\\" + ch if ch in _MDV2_SPECIAL else ch for ch in (s or ""))


def mdcode(s: str) -> str:
    """Render text as an inline code span (only ` and \\ need escaping)."""
    s = (s or "").replace("\\", "\\\\").replace("`", "\\`")
    return f"`{s}`"


def expandable_quote(lines: list) -> str:
    """Wrap already-escaped single-line strings in an expandable blockquote."""
    if not lines:
        return ""
    if len(lines) == 1:
        return f"**>{lines[0]}||"
    middle = [f">{l}" for l in lines[1:-1]]
    return "\n".join([f"**>{lines[0]}", *middle, f">{lines[-1]}||"])


def tg_render(run: dict) -> str:
    """Build a MarkdownV2 message for one platform run."""
    emoji = PLATFORM_EMOJI.get(run["platform"], "📱")
    head = f"{emoji} *WhatsApp {run['platform'].capitalize()} beta* v{mdv2(str(run['version']))}"
    if run.get("version_extra"):
        head += f"\n_{mdv2(run['version_extra'])}_"
    if run.get("initial"):
        counts = ", ".join(f"{v} {k}" for k, v in run.get("counts", {}).items())
        return head + f"\n\n_Initial baseline captured_ \\({mdv2(counts)}\\)\\."

    def build(with_details: bool) -> str:
        parts = [head, ""]
        for sec in run["sections"]:
            a, c, r = sec["added"], sec["changed"], sec["removed"]
            parts.append(f"*{mdv2(sec['title'])}* — ➕{len(a)} ✏️{len(c)} ➖{len(r)}")
            if with_details:
                detail = []
                for k, v in a.items():
                    detail.append(f"➕ {mdcode(k)}: {mdv2(trunc(v))}")
                for k, (old, new) in c.items():
                    detail.append(f"✏️ {mdcode(k)}: {mdv2(trunc(old, 80))} → {mdv2(trunc(new))}")
                for k in r:
                    detail.append(f"➖ {mdcode(k)}")
                if detail:
                    parts.append(expandable_quote(detail))
            parts.append("")
        return "\n".join(parts).rstrip()

    text = build(with_details=True)
    if len(text) > TG_LIMIT:
        # Re-render compact to avoid truncating mid-entity (breaks MarkdownV2).
        text = build(with_details=False) + "\n\n_Full details in the attached report_\\."
    return text


def tg_post(token: str, method: str, data: bytes, headers: dict):
    url = TG_API.format(token=token, method=method)
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def tg_send_message(token: str, chat_id: str, text: str):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "MarkdownV2", "disable_web_page_preview": "true",
    }).encode()
    return tg_post(token, "sendMessage", data,
                   {"Content-Type": "application/x-www-form-urlencoded"})


def tg_send_document(token: str, chat_id: str, filepath: Path, caption: str = ""):
    boundary = "----wa" + uuid.uuid4().hex
    body = bytearray()

    def field(name, value):
        body.extend((f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n").encode())

    field("chat_id", chat_id)
    if caption:
        field("caption", caption)  # plain text caption (no parse_mode)
    body.extend((f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="document"; filename="{filepath.name}"\r\n'
                 f"Content-Type: text/markdown\r\n\r\n").encode())
    body.extend(filepath.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    return tg_post(token, "sendDocument", bytes(body),
                   {"Content-Type": f"multipart/form-data; boundary={boundary}"})


def send_telegram(payload: dict):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "DRY")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "DRY")
    for run in payload["runs"]:
        text = tg_render(run)
        if DRY:
            print(f"\n----- TELEGRAM ({run['platform']}) -----\n{text}\n[attach: {run['report']}]")
            continue
        tg_send_message(token, chat_id, text)
        report = ROOT / run["report"]
        if report.exists() and not run.get("initial"):
            tg_send_document(token, chat_id, report,
                             caption=f"Full report · {run['platform']} v{run['version']}")
    print("Telegram: sent" if not DRY else "Telegram: dry-run rendered")


# ------------------------------------------------------------------- Email ---

def html_run(run: dict) -> str:
    emoji = PLATFORM_EMOJI.get(run["platform"], "📱")
    ve = f" &middot; {html.escape(run['version_extra'])}" if run.get("version_extra") else ""
    out = [f"<h2 style='margin:18px 0 6px'>{emoji} WhatsApp {run['platform'].capitalize()} "
           f"beta v{html.escape(str(run['version']))}{ve}</h2>"]
    if run.get("initial"):
        counts = ", ".join(f"{v} {k}" for k, v in run.get("counts", {}).items())
        out.append(f"<p><i>Initial baseline captured</i> ({html.escape(counts)}).</p>")
        return "\n".join(out)

    for sec in run["sections"]:
        out.append(f"<h3 style='margin:14px 0 4px'>{html.escape(sec['title'])}</h3>")
        if sec["added"]:
            out.append("<p style='margin:4px 0;color:#137333'><b>➕ Added</b></p><ul style='margin:0'>")
            for k, v in sec["added"].items():
                out.append(f"<li><code>{html.escape(k)}</code> — {html.escape(trunc(v, 400))}</li>")
            out.append("</ul>")
        if sec["changed"]:
            out.append("<p style='margin:4px 0;color:#b06000'><b>✏️ Changed</b></p><ul style='margin:0'>")
            for k, (old, new) in sec["changed"].items():
                out.append(f"<li><code>{html.escape(k)}</code><br>"
                           f"<span style='color:#888'>before:</span> {html.escape(trunc(old, 400))}<br>"
                           f"<span style='color:#888'>after:</span> {html.escape(trunc(new, 400))}</li>")
            out.append("</ul>")
        if sec["removed"]:
            out.append("<p style='margin:4px 0;color:#c5221f'><b>➖ Removed</b></p><ul style='margin:0'>")
            for k, v in sec["removed"].items():
                out.append(f"<li><code>{html.escape(k)}</code> — {html.escape(trunc(v, 400))}</li>")
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
