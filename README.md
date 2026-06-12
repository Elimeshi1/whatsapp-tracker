# WhatsApp Beta Tracker

Automatically watches the **WhatsApp beta** builds for **Android** and **macOS**,
extracts their texts and feature toggles, and reports what changed between
versions — so you can see what's new before it ships.

It runs entirely on **GitHub Actions** (no server needed) on a schedule, and
pushes a clean, formatted notification to **Telegram and/or email** whenever
something changes — showing exactly what was added, changed, or removed, with
the full report attached.

## What it tracks

| Platform | Source | Extracted |
|----------|--------|-----------|
| Android  | APKPure direct download (or a manual APK URL you pass in) | `strings.xml`, `bools.xml`, `integers.xml` |
| macOS    | Official beta endpoint `?configuration=Beta` → `.dmg` | every English/Base `*.strings` and `*.loctable` |

- **Texts** are the UI strings — new features almost always introduce new strings.
- **Feature toggles** (`bools`) and **tunables** (`integers`) are the gates that
  flip new behaviour on. Together these are the same signals trackers like
  WABetaInfo watch.

## How it works

```
schedule (every 6h) ─┬─ android job (ubuntu): download APK → apktool decode → extract JSON
                     └─ macos  job (macos):  download DMG → mount → extract JSON
                              │
                              └─ report job: diff vs data/<platform>/latest.json
                                             ├─ write reports/<platform>/<date>_v<ver>.md
                                             ├─ update baselines + version snapshots
                                             ├─ append CHANGELOG.md
                                             ├─ commit back to the repo
                                             └─ notify Telegram / email if changed
```

The committed `data/<platform>/latest.json` is the baseline; the next run diffs
against it. Git history therefore doubles as a full version-by-version archive.

## Output

- **`reports/<platform>/<date>_v<version>.md`** — the organized "what's new":
  Added / Changed / Removed texts and flags.
- **`CHANGELOG.md`** — one line per run, newest first.
- **`data/<platform>/latest.json`** — current baseline.
- **`data/<platform>/snapshots/<version>.json`** — per-version archive.
- A **Telegram message** and/or **email** per run that had changes (see below).

## Setup

1. Create a new GitHub repo and push this folder (see commands below).
2. In the repo: **Settings → Actions → General → Workflow permissions** →
   enable **Read and write permissions** (lets the workflow commit results).
3. Configure at least one notification channel (next section).
4. The schedule starts automatically. To run on demand:
   **Actions → WhatsApp Beta Tracker → Run workflow**.

## Notifications

Set these under **Settings → Secrets and variables → Actions → New repository
secret**. Configure Telegram, email, or both — a channel turns on only when its
secrets are present. If neither is set, the run still records changes in the
repo but sends nothing.

### Telegram (recommended — instant phone push, rich formatting)

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts → copy
   the **bot token**.
2. Send any message to your new bot (so it's allowed to message you back).
3. Get your **chat id**: open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read
   `result[].message.chat.id` (a number; for groups it starts with `-`).
4. Add secrets:
   - `TELEGRAM_BOT_TOKEN` = the bot token
   - `TELEGRAM_CHAT_ID` = your chat id

You'll get one message per changed platform with the diff, plus the full report
attached as a Markdown file.

### Email (Gmail)

1. Enable 2-Step Verification on your Google account, then create an
   **App Password** (Google Account → Security → App passwords).
2. Add secrets:
   - `MAIL_USERNAME` = your Gmail address
   - `MAIL_PASSWORD` = the 16-char app password (not your normal password)
   - `MAIL_TO` = where to send (can be the same address)
   - *(optional)* `MAIL_FROM`, `MAIL_HOST` (default `smtp.gmail.com`),
     `MAIL_PORT` (default `465`)

You'll get one HTML email per run summarizing every changed platform, with the
reports attached. For a non-Gmail provider, set `MAIL_HOST`/`MAIL_PORT`
accordingly (port `465` = SSL, anything else = STARTTLS).

### Getting the *exact* latest Android beta

APKPure is fully automated but can lag a day or two behind the newest beta.
When you want a specific build, grab its direct APK link (e.g. from APKMirror)
and run the workflow manually with the **`apk_url`** input filled in — the
Android job will use that instead.

## Running locally (optional)

```bash
# Android (needs Java + apktool.jar — same version pinned in the workflow)
bash scripts/download_android.sh artifacts
java -jar apktool.jar d -s -f -o decoded artifacts/WhatsApp.apk
python3 scripts/extract_android.py decoded artifacts/android-extract.json

# macOS (run on a Mac)
bash scripts/download_mac.sh artifacts
python3 scripts/extract_mac.py artifacts/WhatsApp-mac.dmg artifacts/mac-extract.json

# Diff (expects incoming/<platform>-extract/<platform>-extract.json)
mkdir -p incoming/android-extract && cp artifacts/android-extract.json incoming/android-extract/
python3 scripts/diff_and_report.py

# Preview notifications without sending (renders Telegram text + email HTML)
NOTIFY_DRY_RUN=1 python3 scripts/notify.py
```

## Tuning

- **Frequency:** edit the `cron` in `.github/workflows/track.yml`.
- **apktool version:** pinned in the workflow; bump the release URL to update.
- **macOS cost:** the macOS job uses GitHub's macOS runners. On a **public**
  repo, Actions minutes are free. On a private repo, macOS minutes bill at 10×,
  so consider keeping this repo public or reducing the schedule frequency.
