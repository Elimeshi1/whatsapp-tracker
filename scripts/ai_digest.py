#!/usr/bin/env python3
"""Turn the raw diff in notify.json into a feature digest with an LLM.

Runs between diff_and_report.py and notify.py. diff_and_report.py stays the
source of truth for *what* changed (and already drops returning items via
seen.json); this stage only decides *what it means*: it groups the changed
items into features and gives each a short title and explanation.

Three guarantees drive the design:

  Nothing is missed.  Every changed item gets a short id (t1, c3, m7 …) and the
      model answers with ids only, never with the text itself. The code then
      checks coverage: ids the model left out are sent once more, and anything
      still missing lands in an "unclassified" group. The digest is rendered from
      the ids, so every item in the diff appears under some heading.

  Nothing is announced twice.  data/ai/ledger.json remembers every feature ever
      announced and every item already reported under one. An item already in
      the ledger (e.g. an Android string now arriving on Mac) is never sent to
      the model again — it's attached to its known feature as "now on <platform>".
      The model is also shown the recent known features, so new details of a
      feature it announced before come back as an *update* to that feature, not
      as a new one.

  Few tokens.  Only the diff goes in (never baselines), strings are stripped of
      markup and truncated, methods are grouped per class, Android + Mac items
      with the same text are sent once, the output is ids instead of text, the
      static prefix (instructions + known features) is shared across calls so
      OpenAI's prompt cache picks it up. The whole diff goes in one call
      (medium reasoning) so a topic is never split across two features.

Env:
  OPENAI_API_KEY   required; without it the stage is skipped (notify.py then
                   sends the plain diff, exactly as before)
  OPENAI_MODEL     default gpt-5.6-terra
  AI_LANGUAGE      language of titles/summaries, default Hebrew
  AI_DRY_RUN=1     print what would be sent, call nothing, write nothing

Stdlib only. Usage: ai_digest.py [notify.json]
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "ai" / "ledger.json"
API_URL = "https://api.openai.com/v1/responses"
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")
LANGUAGE = os.environ.get("AI_LANGUAGE", "Hebrew")
DRY = os.environ.get("AI_DRY_RUN") == "1"

# The whole diff goes in one call so the model sees the full picture and keeps
# each topic in one feature. Only a diff far beyond anything seen so far (the
# largest, 1,357 items, is ~92k chars) would be split.
BATCH_CHARS = 400000
KNOWN_FEATURES = 120      # most recent known features shown to the model
KNOWN_SUMMARY = 160       # chars of each known feature's description shown
TEXT_LIMIT = 220          # chars per string sent to the model
METHODS_PER_LINE = 12     # methods shown per class line

# id prefix → (notify.json bucket, human label for the prompt and the digest)
KINDS = {
    "t": ("new", "new text"),
    "w": ("reworded", "reworded text"),
    "r": ("removed", "removed text"),
    "c": ("new_components", "new screen/component"),
    "x": ("removed_components", "removed screen/component"),
    "p": ("new_permissions", "new permission"),
    "q": ("removed_permissions", "removed permission"),
    "k": ("new_classes", "new class"),
    "m": ("new_methods", "new methods on existing class"),
    "d": ("removed_classes", "removed class"),
}

INSTRUCTIONS = f"""You analyse diffs between WhatsApp beta builds (Android and Mac),
like an expert WhatsApp beta reporter (WABetaInfo style).
Input: a list of changed items, one per line: "<id> <platform> <kind> | <content>".
Strings are UI text ([module] = the app module using it); code names are
readable Java class/method names (A = Android, M = Mac).

First read the WHOLE list and understand the full picture, then group the items
into features — user-facing capabilities or product changes — and answer with
JSON only.

Rules:
- Put EVERY id in exactly one feature or in "noise". Never drop an id.
- One topic = one feature. Never split a topic across two features: its
  screens, classes, methods, UI texts, rewordings and removals all go together,
  even when they are far apart in the list. Before answering, check that no two
  of your features describe the same thing; merge them if they do.
- If items belong to a feature in KNOWN FEATURES, set "ref" to its id and
  "ref_title" to its exact title (both are checked): the items are new details
  of something already announced. Only link when it is truly the same feature.
  Otherwise "ref" and "ref_title" are "".
- "items" must list EVERY id of the feature — the answer is checked
  automatically and anything left out is flagged.
- "noise": ONLY items with no product meaning at all — refactors, generic/infra
  code, analytics, generic errors and plurals, typo-level wording fixes. Any
  item hinting at something a user could see or do belongs to a feature; when
  unsure, make it a low-importance feature rather than noise.
- "title": a clear, specific feature name (not generic like "chat improvements").
- "summary": a detailed explanation, 2-5 sentences, for someone who has not
  seen the texts: what the feature is, how it works for the user (where it
  appears, what they can do, options, limits, who can use it), and anything
  notable the texts reveal (e.g. a feature being removed or replaced, a new
  setting, a privacy aspect, a specific country). For a "ref" update, explain
  what is new about the known feature in this build. Base everything on the
  items; don't invent details they don't support.
- "status": what this really is, using what you know about WhatsApp as it is
  publicly available today plus the evidence in the items:
    "new_unreleased" — a genuinely new feature, still in development and not yet
                       available in the public app (new screens/classes, a set
                       of new strings describing something WhatsApp doesn't have)
    "existing_change" — a change, extension or rollout step of a feature that
                       already exists in the app
    "removal"        — a feature being removed or replaced
    "text_only"      — only text: rewording, strings that come and go between
                       builds, generic/duplicated strings, or a few lines with no
                       real feature behind them
- "status_reason": one sentence on why you chose that status (what in the items
  or in WhatsApp's current app supports it). Be honest when unsure.
- "importance": high = new user-facing feature, medium = notable change to an
  existing one, low = minor.
- Write title and summary in {LANGUAGE}. Keep product names (Meta AI, Pix,
  Passkey …) as they are."""

SCHEMA = {
    "type": "object",
    "properties": {
        "features": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "ref_title": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "status": {"type": "string", "enum": ["new_unreleased", "existing_change",
                                                          "removal", "text_only"]},
                    "status_reason": {"type": "string"},
                    "importance": {"type": "string", "enum": ["high", "medium", "low"]},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["ref", "ref_title", "title", "summary", "status", "status_reason",
                             "importance", "items"],
                "additionalProperties": False,
            },
        },
        "noise": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["features", "noise"],
    "additionalProperties": False,
}

_TAG = re.compile(r"<[^>]+>")
_PLACEHOLDER = re.compile(r"%\d*\$?[sd@]")
_NONWORD = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def plain(s: str) -> str:
    return _WS.sub(" ", _TAG.sub("", s or "")).strip()


def short(s: str, limit: int = TEXT_LIMIT) -> str:
    s = plain(s)
    return s if len(s) <= limit else s[:limit] + "…"


def short_code(name: str) -> str:
    for p in ("com.whatsapp.", "android.permission."):
        if name.startswith(p):
            return name[len(p):]
    return name


def norm(s: str) -> str:
    """Platform-neutral form: Android %1$s and Mac %1$@ variants match."""
    s = _PLACEHOLDER.sub(" ", _TAG.sub(" ", s or ""))
    return _WS.sub(" ", _NONWORD.sub(" ", s.lower())).strip()


def item_key(kind: str, raw) -> str:
    """Stable ledger key for one changed item (same text on both platforms → same key)."""
    if kind == "w":
        raw = raw[1]                         # a reword is identified by its new text
    if kind in ("t", "w", "r"):
        body = norm(raw)
    else:
        body = str(raw)
    return hashlib.sha1(f"{kind}:{body}".encode()).hexdigest()[:16]


# ------------------------------------------------------------------ ledger ---

def load_ledger() -> dict:
    try:
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("features", {})
    data.setdefault("items", {})
    data.setdefault("next_id", 1)
    return data


def save_ledger(ledger: dict):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=1, sort_keys=True),
                      encoding="utf-8")


def known_features_block(ledger: dict, extra: list) -> str:
    feats = sorted(ledger["features"].items(),
                   key=lambda kv: kv[1].get("last_seen", ""), reverse=True)[:KNOWN_FEATURES]
    # Oldest first so the list grows at the end: keeps the cached prefix stable.
    lines = [f"{fid} | {f['title']} | {short(f.get('summary', ''), KNOWN_SUMMARY)}"
             for fid, f in reversed(feats)]
    lines += [f"{fid} | {title} | {short(summary, KNOWN_SUMMARY)}" for fid, title, summary in extra]
    return ("KNOWN FEATURES (id | title | description):\n"
            + ("\n".join(lines) if lines else "(none yet)"))


# ------------------------------------------------------------------- items ---

def collect_items(runs: list, ledger: dict):
    """Flatten every run's diff into id'd items, merging identical cross-platform ones.

    Returns (items, carried): items is {id: item} to send to the model; carried
    maps known feature id → items already reported under it (no model needed).
    """
    by_key, carried = {}, {}
    counters = {k: 0 for k in KINDS}
    for run in runs:
        plat = run["platform"]
        # Code-derived module labels (Android, and Mac strings matched to Android).
        areas = {v: g["label"] for g in run.get("new_groups") or []
                 if not g["label"].startswith("·") for v in g["items"]}
        entries = []
        for kind, (bucket, _) in KINDS.items():
            if kind == "m":
                # Group methods per class: one id (and one line) per class.
                per_class = {}
                for m in run.get(bucket) or []:
                    cls, _, meth = m.partition("#")
                    per_class.setdefault(cls, []).append(meth)
                entries += [("m", (cls, meths)) for cls, meths in sorted(per_class.items())]
            else:
                entries += [(kind, v) for v in run.get(bucket) or []]
        for kind, raw in entries:
            key = item_key(kind, raw if kind != "m" else raw[0] + "#" + ",".join(raw[1]))
            if key in by_key:                       # same text on the other platform
                if plat not in by_key[key]["platforms"]:
                    by_key[key]["platforms"].append(plat)
                continue
            item = {"kind": kind, "raw": raw, "key": key, "platforms": [plat],
                    "area": areas.get(raw) if kind == "t" else None}
            fid = ledger["items"].get(key)
            if fid == "noise":                      # judged noise before; not news now
                by_key[key] = item
                continue
            if fid and fid in ledger["features"]:
                carried.setdefault(fid, []).append(item)
                by_key[key] = item
                continue
            counters[kind] += 1
            item["id"] = f"{kind}{counters[kind]}"
            by_key[key] = item
    items = {it["id"]: it for it in by_key.values() if "id" in it}
    return items, carried


def item_line(it: dict) -> str:
    kind, raw = it["kind"], it["raw"]
    plats = "+".join("A" if p == "android" else "M" for p in it["platforms"])
    label = KINDS[kind][1]
    if kind == "w":
        content = f"{short(raw[0], 120)} → {short(raw[1], 160)}"
    elif kind in ("t", "r"):
        content = short(raw)
        if it.get("area"):
            content = f"[{it['area']}] {content}"
    elif kind == "m":
        cls, meths = raw
        more = f" (+{len(meths) - METHODS_PER_LINE})" if len(meths) > METHODS_PER_LINE else ""
        content = f"{short_code(cls)}: {', '.join(meths[:METHODS_PER_LINE])}{more}"
    else:
        content = short_code(raw)
    return f"{it['id']} {plats} {label} | {content}"


def item_sort_key(it: dict):
    """Related items next to each other, so batches split along feature lines."""
    kind, raw = it["kind"], it["raw"]
    if kind in ("t", "w", "r"):
        topic = it.get("area") or "~"
    else:
        name = short_code(raw[0] if kind == "m" else raw)
        topic = name.split(".")[0]
    return (topic, "twckpmrxqd".index(kind), int(it["id"][1:]))


def batches(items: dict):
    lines = [item_line(it) for it in sorted(items.values(), key=item_sort_key)]
    batch, size = [], 0
    for line in lines:
        if batch and size + len(line) > BATCH_CHARS:
            yield batch
            batch, size = [], 0
        batch.append(line)
        size += len(line) + 1
    if batch:
        yield batch


# ---------------------------------------------------------------- OpenAI ---

USAGE = {"calls": 0, "input": 0, "cached": 0, "output": 0}


def call_model(known: str, lines: list, note: str = "") -> dict:
    body = {
        "model": MODEL,
        "instructions": INSTRUCTIONS,
        # Known features first: identical across this run's calls → cache hits.
        "input": known + ("\n\n" + note if note else "") + "\n\nCHANGED ITEMS:\n" + "\n".join(lines),
        "reasoning": {"effort": "medium"},
        "prompt_cache_key": "wa-tracker-digest",
        "max_output_tokens": 64000,
        "text": {"format": {"type": "json_schema", "name": "digest",
                            "schema": SCHEMA, "strict": True}},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                 "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * 2 ** attempt)
                continue
            raise RuntimeError(f"OpenAI HTTP {exc.code}: {exc.read().decode()[:500]}")
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(5 * 2 ** attempt)
                continue
            raise
    usage = data.get("usage") or {}
    USAGE["calls"] += 1
    USAGE["input"] += usage.get("input_tokens", 0)
    USAGE["cached"] += (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    USAGE["output"] += usage.get("output_tokens", 0)
    if data.get("status") != "completed":
        raise RuntimeError(f"OpenAI response {data.get('status')}: "
                           f"{data.get('incomplete_details') or data.get('error')}")
    text = "".join(c.get("text", "") for o in data.get("output", []) if o.get("type") == "message"
                   for c in o.get("content", []) if c.get("type") == "output_text")
    return json.loads(text)


# ---------------------------------------------------------------- digest ---

RETRY_CHUNK = 200         # items per follow-up call for ids the model skipped
RETRY_ROUNDS = 3

RETRY_NOTE = """These items were left out of the analysis of this build. Assign
each to the feature it belongs to. Strongly prefer the features in KNOWN FEATURES
(those with N ids were found in this same build): set "ref" to that id and
"ref_title" to its title. Create a new feature only for a topic none of them
covers."""


def classify(items: dict, ledger: dict) -> tuple:
    """Run the model over all items. Returns (features, noise_ids, unclassified_ids).

    features: [{ref, title, summary, status, …, items: [ids]}], each id placed
    at most once. Coverage is enforced here, not trusted to the model: whatever
    it leaves out is sent again in small chunks, alongside the features already
    found in this build, until everything is placed (or a round makes no
    progress).
    """
    features, noise, placed = [], [], set()
    this_run = []                        # (N id, title, summary) found in this build
    by_tmp, by_ref = {}, {}

    def resolve(ref: str, ref_title: str) -> str:
        """Check the model's ref against the title it copied; fix or drop a wrong one."""
        want = norm(ref_title)
        if ref in by_tmp:
            if not want or norm(by_tmp[ref]["title"]) == want:
                return ref
        elif ref in ledger["features"]:
            if not want or norm(ledger["features"][ref]["title"]) == want:
                return ref
        if not want:
            return ""
        for k, v in by_tmp.items():
            if norm(v["title"]) == want:
                return k
        for k, v in ledger["features"].items():
            if norm(v["title"]) == want:
                return k
        return ""                        # no such feature → treat as new

    def absorb(result, valid):
        for f in result.get("features", []):
            ids = [i for i in f.get("items", []) if i in valid and i not in placed]
            if not ids:
                continue
            placed.update(ids)
            ref = resolve(f.get("ref") or "", f.get("ref_title") or "")
            if not ref:                  # same new topic twice → one feature
                tkey = norm(f["title"])
                ref = next((k for k, v in by_tmp.items() if norm(v["title"]) == tkey), "")
            if ref in by_tmp:
                by_tmp[ref]["items"] += ids
                continue
            if ref in by_ref:            # same known feature again
                by_ref[ref]["items"] += ids
                continue
            feat = {"ref": ref, "title": f["title"].strip(), "summary": f["summary"].strip(),
                    "status": f.get("status", "text_only"),
                    "status_reason": f.get("status_reason", "").strip(),
                    "importance": f.get("importance", "low"), "items": ids}
            features.append(feat)
            if ref:
                by_ref[ref] = feat
            else:
                tmp = f"N{len(by_tmp) + 1}"
                by_tmp[tmp] = feat
                this_run.append((tmp, feat["title"], feat["summary"]))
        for i in result.get("noise", []):
            if i in valid and i not in placed:
                placed.add(i)
                noise.append(i)

    for lines in batches(items):
        valid = {ln.split(" ", 1)[0] for ln in lines}
        absorb(call_model(known_features_block(ledger, this_run), lines), valid)

    for _ in range(RETRY_ROUNDS):
        missing = [i for i in items if i not in placed]
        if not missing:
            break
        print(f"ai: model skipped {len(missing)} item(s); sending them again")
        before = len(placed)
        for n in range(0, len(missing), RETRY_CHUNK):
            chunk = missing[n:n + RETRY_CHUNK]
            try:
                absorb(call_model(known_features_block(ledger, this_run),
                                  [item_line(items[i]) for i in chunk], note=RETRY_NOTE),
                       set(chunk))
            except Exception as exc:  # noqa: BLE001 — the rest of the digest is still good
                print(f"ai: retry failed: {exc}", file=sys.stderr)
        if len(placed) == before:
            break
    unclassified = [i for i in items if i not in placed]
    return features, noise, unclassified


def render_item(it: dict) -> dict:
    """What notify.py needs to display one item (text, not ids)."""
    kind, raw = it["kind"], it["raw"]
    if kind == "m":
        raw = f"{short_code(raw[0])}: " + ", ".join(raw[1])
    elif kind == "w":
        raw = list(raw)
    elif kind not in ("t", "r"):
        raw = short_code(raw)
    return {"kind": kind, "label": KINDS[kind][1], "value": raw, "platforms": it["platforms"]}


def build_digest(runs: list, ledger: dict) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    versions = {r["platform"]: str(r["version"]) for r in runs}
    items, carried = collect_items(runs, ledger)
    print(f"ai: {len(items)} item(s) for the model, "
          f"{sum(len(v) for v in carried.values())} already reported under known features")

    if DRY:
        for lines in batches(items):
            print(known_features_block(ledger, []) + "\n\nCHANGED ITEMS:\n" + "\n".join(lines))
        return {}

    features, noise, unclassified = classify(items, ledger) if items else ([], [], [])

    out_features = []
    for f in features:
        fid = f["ref"]
        is_update = bool(fid)
        if not fid:
            fid = f"F{ledger['next_id']}"
            ledger["next_id"] += 1
            ledger["features"][fid] = {"title": f["title"], "summary": f["summary"],
                                       "first_seen": now, "versions": {}}
        entry = ledger["features"][fid]
        entry["last_seen"] = now
        for i in f["items"]:
            ledger["items"][items[i]["key"]] = fid
            for p in items[i]["platforms"]:
                entry["versions"].setdefault(p, versions.get(p))
        out_features.append({
            "id": fid, "update": is_update,
            "title": f["title"],
            "summary": f["summary"], "importance": f["importance"],
            "status": f["status"], "status_reason": f["status_reason"],
            "items": [render_item(items[i]) for i in f["items"]],
        })

    # Items already reported under a known feature that show up again — almost
    # always the other platform catching up. Rendered without a model call.
    platform_arrivals = []
    for fid, its in carried.items():
        entry = ledger["features"][fid]
        newly = sorted({p for it in its for p in it["platforms"]} - set(entry["versions"]))
        for p in newly:
            entry["versions"][p] = versions.get(p)
        if newly:
            entry["last_seen"] = now
            platform_arrivals.append({"id": fid, "title": entry["title"], "platforms": newly,
                                      "count": len(its)})

    # Noise is recorded too, so it is never sent to the model again.
    for i in noise:
        ledger["items"].setdefault(items[i]["key"], "noise")
    # Genuinely new, unreleased features first; "just text" last.
    rank = {"high": 0, "medium": 1, "low": 2}
    srank = {"new_unreleased": 0, "existing_change": 1, "removal": 2, "text_only": 3}
    out_features.sort(key=lambda f: (srank.get(f["status"], 4), f["update"],
                                     rank.get(f["importance"], 3)))
    return {
        "model": MODEL,
        "features": out_features,
        "platform_arrivals": platform_arrivals,
        "noise": [render_item(items[i]) for i in noise],
        "unclassified": [render_item(items[i]) for i in unclassified],
        "usage": dict(USAGE),
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "notify.json"
    if not path.exists():
        print("ai: no notify.json, nothing to do")
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    runs = [r for r in payload.get("runs", []) if not r.get("initial")]
    if not payload.get("changed") or not runs:
        print("ai: no changes, nothing to analyse")
        return 0
    if not os.environ.get("OPENAI_API_KEY") and not DRY:
        print("ai: OPENAI_API_KEY not set — skipping, plain diff will be sent")
        return 0

    ledger = load_ledger()
    try:
        digest = build_digest(runs, ledger)
    except Exception as exc:  # noqa: BLE001 — never block the plain notification
        print(f"ai: digest failed ({exc}); plain diff will be sent", file=sys.stderr)
        return 0
    if DRY:
        return 0

    # The ledger is saved only after a complete digest, so a failed run never
    # marks items as reported that nobody was told about.
    save_ledger(ledger)
    payload["ai"] = digest
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    u = digest["usage"]
    print(f"ai: {len(digest['features'])} feature(s), {len(digest['noise'])} noise, "
          f"{len(digest['unclassified'])} unclassified · {u['calls']} call(s), "
          f"{u['input']} in ({u['cached']} cached) / {u['output']} out tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
