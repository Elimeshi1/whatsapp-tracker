# WhatsApp Beta Tracker

Automatically watches the **WhatsApp beta** builds for **Android** and **macOS**,
extracts their UI texts, and reports what changed between versions — **classifying
each change** so real new features stand out from noise:

- **🆕 New** — text with no close match in the previous build (likely new features)
- **✏️ Reworded** — an existing string with a minor text change (e.g. "Failed to
  login" → "Couldn't log in") — surfaced separately so it doesn't look new
- **➖ Removed** — text that's gone

It runs entirely on **GitHub Actions** (no server needed) on a schedule, and
pushes a clean, formatted notification to **Telegram** whenever something
changes — laid out inline and readable.

## What it tracks

| Platform | Source | Extracted |
|----------|--------|-----------|
| Android  | WhatsApp's self-hosted APK (or a manual APK URL you pass in) | the set of human-readable `strings.xml` values, **each tied to the WhatsApp feature module that uses it** (see below), plus manifest components/permissions |
| macOS    | Official beta endpoint `?configuration=Beta` → `.dmg` | English UI values from WhatsApp's custom `*.localite.values` blobs (the main bundle keeps no standard English `Localizable.strings`), plus `*.strings`/`*.loctable` for InfoPlist/permission text |

### Where each Android string belongs

New texts aren't just dumped in a flat list — each is attributed to the **real
feature module** it lives in (e.g. `companiondevice`, `registration`,
`payments/indiaupi`, `offload`). WhatsApp strips resource *names*, but the dummy
name embeds the resource id (`0x7f12abcd`), and that id appears as a constant in
the bytecode wherever the string is used. Most classes are obfuscated (`X/…`),
but ~4k keep readable paths (`com/whatsapp/<module>/…`), so when a readable class
references the id we know exactly which feature the string belongs to. This is
WhatsApp's *own* code layout — not a guessed topic. Strings referenced only by
obfuscated code fall under `other …` (still split by shared word for structure).

**macOS reuses these labels.** The Mac app has no groupable structure of its own
— it stores UI text in opaque hash-keyed blobs (keys like `1aIk6g`, no
namespaces), and its code isn't practically cross-referenceable. But WhatsApp
ships the *same* English text on both platforms, so a Mac string that matches an
Android one (placeholder-insensitive) inherits that Android module. In practice
~15% of Mac strings map to a real module this way; the rest are word-clustered.

**Why values, not keys?** WhatsApp strips resource *names* (apktool shows them as
`APKTOOL_DUMMYVAL_0x…`) and the numeric ids are reassigned every build, so
diffing by name is meaningless — every string looks "changed". Instead we diff
the **set of string values**: a value present in the new build but not the old
one is new UI text — a hint at a new feature. This is the same signal trackers
like WABetaInfo watch.

## How it works

```
schedule (every 6h) ─┬─ android job (ubuntu): download APK → apktool decode (resources)
                     │                        + baksmali (code) → extract JSON
                     └─ macos  job (macos):  download DMG → mount → extract JSON
                              │
                              └─ report job: diff vs data/<platform>/latest.json
                                             ├─ write reports/<platform>/<date>_v<ver>.md
                                             ├─ update baselines + version snapshots
                                             ├─ append CHANGELOG.md
                                             ├─ commit back to the repo
                                             └─ notify Telegram if changed
```

The committed `data/<platform>/latest.json` is the baseline; the next run diffs
against it. Git history therefore doubles as a full version-by-version archive.

## Output

- **`reports/<platform>/<date>_v<version>.md`** — the organized "what's new":
  the new / reworded / removed texts for that version.
- **`CHANGELOG.md`** — one line per run, newest first.
- **`data/<platform>/latest.json`** — current baseline.
- **`data/<platform>/snapshots/<version>.json`** — per-version archive.
- A **Telegram message** per run that had changes (see below).

## Setup

1. Create a new GitHub repo and push this folder (see commands below).
2. In the repo: **Settings → Actions → General → Workflow permissions** →
   enable **Read and write permissions** (lets the workflow commit results).
3. Configure Telegram notifications (next section).
4. The schedule starts automatically. To run on demand:
   **Actions → WhatsApp Beta Tracker → Run workflow**.

## Notifications

Set these under **Settings → Secrets and variables → Actions → New repository
secret**. Telegram turns on only when its secrets are present. If they're not
set, the run still records changes in the repo but sends nothing.

### Telegram

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
**grouped by the WhatsApp feature module that actually uses each string**
(from the code cross-reference above — `companiondevice`, `registration`,
`payments/indiaupi`, …; strings with no readable owner fall under `other …`)
inside **collapsed `<details>` sections**, along with
✏️ Reworded and ➖ Removed. Nothing is expanded by default; tap to open.
Cosmetic-only changes (punctuation/case) are dropped, not shown as reworded. The
whole diff is inline (no file), split across multiple messages only for very
large updates. If
`sendRichMessage` is ever unavailable it falls back to a plain `sendMessage`.
The bot must be an admin of the target channel to post (for a private DM, use
your own user id as `TELEGRAM_CHAT_ID`).

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
# Android (needs Java + apktool.jar + baksmali.jar — versions pinned in the workflow)
bash scripts/download_android.sh artifacts
java -jar apktool.jar d -s -f -o decoded artifacts/WhatsApp.apk
# Disassemble code so strings can be tied to their feature module (optional but
# recommended — omit the smali arg to skip module labels):
for dex in decoded/classes*.dex; do java -jar baksmali.jar d "$dex" -o smali; done
python3 scripts/extract_android.py decoded artifacts/android-extract.json smali

# macOS (run on a Mac)
bash scripts/download_mac.sh artifacts
python3 scripts/extract_mac.py artifacts/WhatsApp-mac.dmg artifacts/mac-extract.json

# Diff (expects incoming/<platform>-extract/<platform>-extract.json)
mkdir -p incoming/android-extract && cp artifacts/android-extract.json incoming/android-extract/
python3 scripts/diff_and_report.py

# Preview the notification without sending (renders the Telegram message)
NOTIFY_DRY_RUN=1 python3 scripts/notify.py
```

## Tuning

- **Frequency:** edit the `cron` in `.github/workflows/track.yml`.
- **apktool version:** pinned in the workflow; bump the release URL to update.
- **macOS cost:** the macOS job uses GitHub's macOS runners. On a **public**
  repo, Actions minutes are free. On a private repo, macOS minutes bill at 10×,
  so consider keeping this repo public or reducing the schedule frequency.
