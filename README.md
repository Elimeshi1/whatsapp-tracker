# WhatsApp Beta Tracker

Automatically watches the **WhatsApp beta** builds for **Android** and **macOS**,
extracts their texts and feature toggles, and reports what changed between
versions — so you can see what's new before it ships.

It runs entirely on **GitHub Actions** (no server needed) on a schedule, and
opens a GitHub **issue** whenever something changes.

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
                                             └─ open an issue if anything changed
```

The committed `data/<platform>/latest.json` is the baseline; the next run diffs
against it. Git history therefore doubles as a full version-by-version archive.

## Output

- **`reports/<platform>/<date>_v<version>.md`** — the organized "what's new":
  Added / Changed / Removed texts and flags.
- **`CHANGELOG.md`** — one line per run, newest first.
- **`data/<platform>/latest.json`** — current baseline.
- **`data/<platform>/snapshots/<version>.json`** — per-version archive.
- A **GitHub issue** per run that had changes (your notification).

## Setup

1. Create a new GitHub repo and push this folder (see commands below).
2. In the repo: **Settings → Actions → General → Workflow permissions** →
   enable **Read and write permissions** (lets the workflow commit + open issues).
3. The schedule starts automatically. To run on demand:
   **Actions → WhatsApp Beta Tracker → Run workflow**.

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
```

## Tuning

- **Frequency:** edit the `cron` in `.github/workflows/track.yml`.
- **apktool version:** pinned in the workflow; bump the release URL to update.
- **macOS cost:** the macOS job uses GitHub's macOS runners. On a **public**
  repo, Actions minutes are free. On a private repo, macOS minutes bill at 10×,
  so consider keeping this repo public or reducing the schedule frequency.
