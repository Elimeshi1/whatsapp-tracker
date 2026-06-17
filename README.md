# WhatsApp Beta Tracker

Watches the **WhatsApp beta** builds for **Android** and **macOS**, and reports
what changed between versions. It runs entirely on **GitHub Actions** — no server
— on a schedule, commits each diff back to the repo, and sends a clean,
collapsible notification to **Telegram** whenever something changes.

Each change is classified so real new features stand out from noise:

- **🆕 New** — text (or a class/screen) with no match in the previous build
- **✏️ Reworded** — an existing string with a minor text change
  (e.g. *"Failed to login"* → *"Couldn't log in"*)
- **➖ Removed** — text, screen, or class that's gone

## Quick start

1. **Fork / push** this repo to your own GitHub account.
2. **Enable write access for Actions:** repo **Settings → Actions → General →
   Workflow permissions → Read and write permissions**. This lets the workflow
   commit each diff back.
3. **Add Telegram secrets** (see [Telegram](#telegram) below) so you get notified.
4. Done. The tracker runs every 3 hours automatically. To run it now:
   **Actions → WhatsApp Beta Tracker → Run workflow**.

That's the whole setup. Everything else below is reference.

## Telegram

Under **Settings → Secrets and variables → Actions → New repository secret**, add:

| Secret | Value |
|--------|-------|
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) (`/newbot`) |
| `TELEGRAM_CHAT_ID` | Your chat id (a number; group/channel ids start with `-`) |

To get your chat id, message [@GetChatID_IL_BOT](https://t.me/GetChatID_IL_BOT).

If the secrets aren't set, the tracker still records changes in the repo — it
just sends nothing.

**What a message looks like:** a header and a one-line summary
(*🧩 new screens · 🧬 new classes · 🆕 new texts · …*), then collapsible
sections — **closed by default, tap to open**:

- **UI-text changes** first, grouped by feature module (🆕 New, ✏️ Reworded,
  ➖ Removed).
- A divider, then **code-surface changes** (🧬 new classes, new methods,
  removed classes).

The whole diff is inline (no attached file), split across multiple messages only
for very large updates.

## What it tracks

| Platform | Source | What's extracted |
|----------|--------|------------------|
| **Android** | WhatsApp's self-hosted APK (or a manual APK URL you pass in) | UI strings, each tagged with the feature module that uses it; manifest components & permissions; readable class & method names (the "code surface") |
| **macOS** | Official beta endpoint (`.dmg`) | English UI strings from WhatsApp's localization blobs, plus InfoPlist / permission text |

### Running a specific Android beta

The default source is WhatsApp's own self-hosted APK
(`whatsapp.com/android/current/WhatsApp.apk`) — the one source that works
reliably from GitHub's datacenter IPs (APKPure / APKMirror return 403 there). It
tracks WhatsApp's latest distributed build, usually at or near the current beta.

To track a *specific* beta build, grab its direct CDN APK link and run the
workflow manually with the **`apk_url`** input filled in.

## Output

Every run that finds changes produces:

- `reports/<platform>/<date>_v<version>.md` — the organized "what's new".
- `CHANGELOG.md` — one line per run, newest first.
- `data/<platform>/latest.json` — the current baseline (next run diffs against it).
- `data/<platform>/snapshots/<version>.json` — a per-version archive.
- A Telegram message.

Git history doubles as a full version-by-version archive.

## How it works

```
schedule (every 3h)
 ├─ android job:  download APK → extract strings, components, code surface → JSON
 ├─ macos  job:   download DMG → extract strings → JSON
 └─ report job:   diff each platform vs its baseline
                  ├─ write report + update baseline + snapshot
                  ├─ append CHANGELOG.md, commit back to the repo
                  └─ notify Telegram if anything changed
```

The committed `data/<platform>/latest.json` is the baseline the next run diffs
against, so git history doubles as a full version-by-version archive.

### Project layout

Each stage is one script; they communicate only through JSON files, so any stage
can be run and tested on its own.

| File | Stage | Responsibility |
|------|-------|----------------|
| `.github/workflows/track.yml` | orchestration | the schedule + the three jobs (android, macos, report) |
| `scripts/download_android.sh` | fetch | download the APK (or a manual `apk_url`) |
| `scripts/download_mac.sh` | fetch | download the macOS beta `.dmg` |
| `scripts/extract_android.py` | extract | APK → strings (tagged by module), components, permissions |
| `scripts/extract_methods.py` | extract | APK dex → readable class & method names (the code surface) |
| `scripts/extract_mac.py` | extract | `.dmg` → English UI strings |
| `scripts/diff_and_report.py` | diff | compare each extract vs its baseline → report + `notify.json` |
| `scripts/notify.py` | notify | render `notify.json` into the Telegram message and send it |
| `data/<platform>/` | state | committed baselines + per-version snapshots |
| `reports/`, `CHANGELOG.md` | output | human-readable history |

## Running locally (optional)

```bash
# Android — needs Java + apktool.jar + baksmali.jar (versions pinned in the workflow)
bash scripts/download_android.sh artifacts
java -jar apktool.jar d -s -f -o decoded artifacts/WhatsApp.apk
# (optional) disassemble code so strings can be tagged with their feature module:
for dex in decoded/classes*.dex; do java -jar baksmali.jar d "$dex" -o smali; done
python3 scripts/extract_android.py decoded artifacts/android-extract.json smali
python3 scripts/extract_methods.py artifacts/WhatsApp.apk artifacts/android-methods.json

# macOS — run on a Mac
bash scripts/download_mac.sh artifacts
python3 scripts/extract_mac.py artifacts/WhatsApp-mac.dmg artifacts/mac-extract.json

# Diff (expects incoming/<platform>-extract/<platform>-extract.json)
mkdir -p incoming/android-extract && cp artifacts/android-extract.json incoming/android-extract/
python3 scripts/diff_and_report.py

# Preview the Telegram message without sending it
NOTIFY_DRY_RUN=1 python3 scripts/notify.py
```

## Tuning

- **Frequency:** edit the `cron` in `.github/workflows/track.yml`.
- **Tool versions:** apktool / baksmali release URLs are pinned in the workflow.
- **macOS cost:** the macOS job uses GitHub's macOS runners (billed at 10× on
  private repos). Keep the repo public, or reduce the schedule, to control cost.

## Under the hood

Details on *why* the extraction works the way it does — not needed to run it.

**Why diff string *values*, not keys.** WhatsApp strips resource names (apktool
shows them as `APKTOOL_DUMMYVAL_0x…`) and reassigns the numeric ids every build,
so diffing by name is meaningless — every string would look changed. Instead we
diff the *set of string values*: a value in the new build but not the old one is
new UI text. This is the same signal trackers like WABetaInfo watch.

**Tagging each Android string with its feature module.** A string isn't just
dumped in a flat list — it's attributed to the real feature module it lives in
(`companiondevice`, `registration`, `payments/indiaupi`, …). The dummy resource
name embeds the resource id (`0x7f12abcd`), and that id appears as a constant in
the bytecode. Most classes are obfuscated (`X/…`), but ~4k keep readable
`com/whatsapp/<module>/…` paths, so when a readable class references the id we
know which feature owns the string. Strings referenced only by obfuscated code
fall under `other …`.

**macOS reuses Android's labels.** The Mac app stores UI text in opaque
hash-keyed blobs with no namespaces, and its code isn't practically
cross-referenceable. But WhatsApp ships the *same* English text on both
platforms, so a Mac string that matches an Android one inherits that module
(~15% of Mac strings); the rest are word-clustered.

**The code surface (the "function names" signal).** Beyond UI text, Android
tracks WhatsApp's own readable class and method names. A new readable
`com/whatsapp/<module>/…` class usually lands *before* any UI text does — the
earliest concrete new-feature hint (e.g. the `companiondevice.Passkey…` classes
revealed passkey sign-in; `StreamingDownloadEngine`, a new media engine).
`extract_methods.py` reads this straight from the APK's dex method table — no
apktool/baksmali needed. We report two cuts, dropping obfuscated and synthetic
(`$lambda` / coroutine) noise: **new top-level classes** (the headline) and
**new methods on classes that already existed** (a capability added to an
existing screen). This baseline lives in `data/<platform>/methods.json`, separate
from `latest.json` so that stays small.

## Disclaimer

This is an independent, unofficial project. It is **not affiliated with,
endorsed by, or connected to WhatsApp LLC or Meta**. "WhatsApp" is a trademark of
its respective owner.

**What it does — and doesn't do.** It downloads WhatsApp's *publicly distributed*
release builds (the same files anyone can download from whatsapp.com) and reads
them to produce a changelog: it extracts and compares **metadata only** — string
values, manifest entries, and the readable names of classes and methods. It does
**not** modify, patch, repackage, or redistribute the app; it stores no WhatsApp
code or app binaries in this repo, only the small JSON/text diffs it derives from
them; and it never touches WhatsApp's servers, accounts, or any user data.
Everything runs against a local copy of a public build.

This project is provided for **educational and research purposes only**. Do not
redistribute WhatsApp's APK/DMG or any assets extracted from them.

## License

The source code in this repository is released under the [MIT License](LICENSE).
This covers only the project's own code — not WhatsApp, its trademarks, or
anything extracted from its builds.
