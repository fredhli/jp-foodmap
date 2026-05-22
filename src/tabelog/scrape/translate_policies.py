"""Translate every restaurant's Japanese reservation policy
(`reservation_policy` in tabelog.csv) to English via Google Translate and
stash to data/tabelog/policy_en.json keyed by detail_url.

Going JA -> EN directly (instead of via the Chinese machine-translation
Tabelog ships) avoids two-hop translation drift and lets the dedupe
cache hit harder — the Japanese policies are heavy on boilerplate
("ネット予約ご利用可能", the long generic Tabelog-Award disclaimer,
etc.), so the same source string lands on dozens of detail_urls and is
translated exactly once per run.

The script is resumable: each successful translation is kept on disk and
re-running picks up only the still-untranslated unique strings. Progress
is checkpointed every CHECKPOINT_EVERY texts and on Ctrl-C, so an
interrupted run never loses more than a handful of recent calls.

Once you've translated as much as you want (full corpus or partial),
re-run map.py — it bakes docs/data/popups-en.json from this file. URLs
without an English entry fall back to the Chinese policy so the popup
never goes blank in the meantime.

Run:
    uv run python src/tabelog/scrape/translate_policies.py
"""

from __future__ import annotations

import csv
import json
import signal
import sys
import time
from pathlib import Path

from deep_translator import GoogleTranslator

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from tabelog.paths import POLICY_EN_JSON, TABELOG_CSV

# Save to disk every N successful translations. Small enough that
# Ctrl-C never loses more than ~3-4s of work; large enough that we're
# not hammering the file system between calls.
CHECKPOINT_EVERY = 25

# Idle delay between requests. The free Google web endpoint is
# unmetered but rate-limits aggressive callers; 0.15s ≈ 6-7 req/s,
# which has held up across multi-thousand-row runs in practice.
SLEEP_BETWEEN = 0.15

# Google Translate caps a single request near 5,000 chars. Tabelog
# policies are usually short, but truncate defensively so a stray long
# one doesn't blow the whole batch.
MAX_LEN = 4900

SRC_LANG = "ja"
TGT_LANG = "en"

# CSV column that holds the Japanese reservation policy. The Chinese
# column (reservation_policy_chinese) is what the bottom sheet shows in
# zh-CN / zh-TW mode; we ignore it here.
POLICY_FIELD = "reservation_policy"


def load_existing() -> dict[str, str]:
    if POLICY_EN_JSON.exists():
        try:
            return json.loads(POLICY_EN_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"{POLICY_EN_JSON} is not valid JSON ({e}); refusing to "
                f"overwrite. Inspect and either fix or delete it."
            )
    POLICY_EN_JSON.parent.mkdir(parents=True, exist_ok=True)
    return {}


def save_atomic(translations: dict[str, str]) -> None:
    """Write via a tmp file + rename so an interrupted save never leaves
    a half-written JSON on disk."""
    tmp = POLICY_EN_JSON.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(translations, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(POLICY_EN_JSON)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    translations = load_existing()
    print(f"loaded {len(translations)} existing translations from "
          f"{POLICY_EN_JSON}")

    # First pass: index the CSV by Japanese policy text. Translate each
    # unique source string once, then fan the result out to every
    # detail_url that shares it.
    policy_to_urls: dict[str, list[str]] = {}
    url_to_policy: dict[str, str] = {}
    skipped_no_ja = 0
    with TABELOG_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            url = (row.get("detail_url") or "").strip()
            policy = (row.get(POLICY_FIELD) or "").strip()
            if not url:
                continue
            if not policy:
                # No Japanese source — the popup will fall back to the
                # Chinese version at render time.
                skipped_no_ja += 1
                continue
            url_to_policy[url] = policy
            policy_to_urls.setdefault(policy, []).append(url)

    if skipped_no_ja:
        print(f"  ({skipped_no_ja} rows have no Japanese policy; falling "
              f"back to Chinese in popups-en.json)")

    # Seed text -> en cache from translations already on disk. Any
    # existing URL whose Japanese policy is still in the CSV becomes a
    # free cache entry, no API call needed.
    text_cache: dict[str, str] = {}
    for url, en in translations.items():
        if not en or not en.strip():
            continue
        policy = url_to_policy.get(url)
        if policy and policy not in text_cache:
            text_cache[policy] = en

    # Apply pre-existing cache hits to URLs the seed didn't cover (e.g.
    # a freshly scraped restaurant that shares boilerplate with one we
    # translated weeks ago). These are pure JSON writes — no API calls.
    free_hits = 0
    for policy, urls in policy_to_urls.items():
        en = text_cache.get(policy)
        if not en:
            continue
        for url in urls:
            if not translations.get(url, "").strip():
                translations[url] = en
                free_hits += 1
    if free_hits:
        save_atomic(translations)
        print(f"cache fan-out: {free_hits} URLs filled from existing "
              f"translations (no API calls)")

    # Unique source strings that still need translating.
    pending_texts = [p for p in policy_to_urls if p not in text_cache]
    total_urls_pending = sum(len(policy_to_urls[p]) for p in pending_texts)
    dedupe_ratio = total_urls_pending / max(len(pending_texts), 1)
    print(f"{len(pending_texts)} unique JA policies still need translation, "
          f"covering {total_urls_pending} restaurants "
          f"(dedupe: {dedupe_ratio:.1f}x — each API call covers ~{dedupe_ratio:.1f} URLs)")
    if not pending_texts:
        return

    # Translate the most-shared policies first — if a run gets interrupted
    # early, we want to have covered the high-fanout boilerplate already.
    pending_texts.sort(key=lambda p: -len(policy_to_urls[p]))

    interrupted = {"flag": False}

    def _on_sigint(sig, frame):
        if interrupted["flag"]:
            raise KeyboardInterrupt
        interrupted["flag"] = True
        print("\ninterrupt received; finishing current row, then saving...",
              flush=True)

    signal.signal(signal.SIGINT, _on_sigint)

    translator = GoogleTranslator(source=SRC_LANG, target=TGT_LANG)
    new = 0       # unique source texts translated this run
    fanout = 0    # URLs filled as a side effect
    fails = 0
    start = time.monotonic()

    for i, policy in enumerate(pending_texts, 1):
        if interrupted["flag"]:
            break
        urls_for_this = policy_to_urls[policy]
        try:
            en = translator.translate(policy[:MAX_LEN]) or ""
        except Exception as e:
            fails += 1
            print(f"  [{i}/{len(pending_texts)}] FAIL ({len(urls_for_this)} urls): "
                  f"{e}", flush=True)
            continue
        text_cache[policy] = en
        for url in urls_for_this:
            translations[url] = en
        new += 1
        fanout += len(urls_for_this)
        if new % CHECKPOINT_EVERY == 0:
            save_atomic(translations)
            elapsed = time.monotonic() - start
            rate = new / elapsed if elapsed > 0 else 0
            eta = (len(pending_texts) - i) / rate if rate > 0 else 0
            print(f"  [{i}/{len(pending_texts)}] +{new} unique texts -> "
                  f"{fanout} URLs filled, {fails} failed; "
                  f"{rate:.1f} texts/s, eta {eta/60:.1f} min", flush=True)
        time.sleep(SLEEP_BETWEEN)

    save_atomic(translations)
    elapsed = time.monotonic() - start
    print(f"\ndone: +{new} unique JA texts translated -> {fanout} URLs filled "
          f"in {elapsed/60:.1f} min ({fails} failed); "
          f"{len(translations)} total in {POLICY_EN_JSON}")


if __name__ == "__main__":
    main()
