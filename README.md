# WhatsApp Beta Tracker

Automatically watches the **WhatsApp beta** builds for **Android** and **macOS**,
extracts their UI texts, and reports what changed between versions — **classifying
each change** so real new features stand out from noise:

- **🆕 New** — text with no close match in the previous build (likely new features)
- **✏️ Reworded** — an existing string with a minor text change (e.g. "Failed to
  login" → "Couldn't log in") — surfaced separately so it doesn't look new
- **➖ Removed** — text that's gone

It runs entirely on **GitHub Actions** (no server needed) on a schedule, and
pushes a clean, formatted notification to **Telegram and/or email** whenever
something changes — laid out inline and readable.

## What it tracks

| Platform | Source | Extracted |
|----------|--------|-----------|
| Android  | WhatsApp's self-hosted APK (or a manual APK URL you pass in) | the set of human-readable `strings.xml` values |
| macOS    | Official beta endpoint `?configuration=Beta` → `.dmg` | string values from English/Base `*.strings` and `*.loctable` |

**Why values, not keys?** WhatsApp strips resource *names* (apktool shows them as
`APKTOOL_DUMMYVAL_0x…`) and the numeric ids are reassigned every build, so
diffing by name is meaningless — every string looks "changed". Instead we diff
the **set of string values**: a value present in the new build but not the old
one is new UI text — a hint at a new feature. This is the same signal trackers
like WABetaInfo watch.

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
  the new / reworded / removed texts for that version.
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

You'll get one message per changed platform. Messages use Telegram's
**Rich Messages** (`sendRichMessage`, Bot API 10.1) — an HTML document where the
**🧩 new screens/features are shown inline** (always visible), grouped by their
class package (`companiondevice`, `offload`, …). The new texts are
**auto-clustered by their most common shared word** (labels emerge from the data
— no predefined topic list) inside **collapsed `<details>` sections**, along with
✏️ Reworded and ➖ Removed. Nothing is expanded by default; tap to open.
Cosmetic-only changes (punctuation/case) are dropped, not shown as reworded. The
whole diff is inline (no file), split across multiple messages only for very
large updates. If
`sendRichMessage` is ever unavailable it falls back to a plain `sendMessage`.
The bot must be an admin of the target channel to post (for a private DM, use
your own user id as `TELEGRAM_CHAT_ID`).

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

The default Android source is **WhatsApp's own self-hosted APK**
(`whatsapp.com/android/current/WhatsApp.apk`) — the one source that reliably
works from GitHub's datacenter IPs (APKPure/APKMirror return HTTP 403 there).
It tracks WhatsApp's latest distributed build, which is usually at or near the
current beta.

When you want a *specific* beta-channel APK, grab its direct APK link (e.g. from
APKMirror) and run the workflow manually with the **`apk_url`** input filled in —
the Android job will use that instead. Note that APKMirror links are usually
Cloudflare-gated, so you may need a direct CDN link rather than the page URL.

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
