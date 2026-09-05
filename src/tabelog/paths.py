"""Central data/output paths. Every script imports from here so the
on-disk layout is described in one place."""

from pathlib import Path

# M-020: atomic write helpers live in tabelog/atomic.py; re-exported here so
# a script only ever needs one import line for "where do I write, and how".
from tabelog.atomic import (  # noqa: F401
    atomic_write_bytes,
    atomic_write_csv,
    atomic_write_json,
    atomic_write_text,
    backup_file,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA = PROJECT_ROOT / "data"
OMAKASE_DIR = DATA / "omakase"
TABELOG_DIR = DATA / "tabelog"
USER_DIR = DATA / "user"
OUTPUT_DIR = DATA / "output"
CACHE_DIR = DATA / "cache"
INTERMEDIATE_DIR = DATA / "intermediate"
ARCHIVE_DIR = DATA / "archive"

# Cloudflare Pages serves from this directory.
DOCS_DIR = PROJECT_ROOT / "docs"

PROFILE_DIR = PROJECT_ROOT / ".chrome_profile"

# Omakase pipeline (omakase.in)
RESTAURANTS_CSV = OMAKASE_DIR / "restaurants.csv"
RESTAURANTS_ALL_CSV = OMAKASE_DIR / "restaurants_all.csv"
BOOKABLE_CSV = OMAKASE_DIR / "bookable.csv"

# Tabelog pipeline (tabelog.com) — unified across regions; each row carries
# a `region` column tagging which Tabelog list it was scraped from.
TABELOG_CSV = TABELOG_DIR / "tabelog.csv"
# M-020: one-generation backup of the master CSV, written by scrape_all.py
# right before it replaces TABELOG_CSV. The master is gitignored, so without
# this a bad scrape has no undo path. Dropbox file history is the second net.
TABELOG_CSV_PREV = TABELOG_DIR / "tabelog.csv.prev"
# M-019: rows scrape_all.py refused to merge into the master CSV (detail page
# came back without an address, so keeping the new row would have blanked a
# good old row). Append-only JSONL, one record per rejected row, so a failed
# re-scrape leaves a readable trail instead of just a log line.
SCRAPE_FAILED_LEDGER = TABELOG_DIR / "scrape_failed.jsonl"
# Side store for English translations of reservation_policy_chinese, keyed
# by Tabelog detail URL. Populated by scrape/translate_policies.py, read by
# map.py when baking docs/data/popups-en.json. Separate from tabelog.csv so
# scraping / geocoding rewrites don't trample the translations.
POLICY_EN_JSON = TABELOG_DIR / "policy_en.json"
# Google Places enrichment (Phase 1, static). A separate ledger keyed by
# detail_url so the Google-sourced coords / bilingual address / types never
# trample the scraped CSV until we explicitly decide to merge. Built by
# scrape/google_enrich.py; one row per restaurant with a match status
# (accepted / review / unmatched / error).
GOOGLE_PLACES_CSV = TABELOG_DIR / "google_places.csv"
# Raw Google API responses keyed by detail_url, so match decisions can be
# re-scored (google_enrich.py --rescore) after tuning thresholds without
# spending any more quota.
GOOGLE_PLACES_CACHE = CACHE_DIR / "google_places_cache.json"
# Local human-review surface for google_enrich.py 'review' matches (not
# deployed). review_google.py emits the HTML; the browser downloads decisions
# which review_google.py --apply writes back into GOOGLE_PLACES_CSV.
GOOGLE_REVIEW_HTML = TABELOG_DIR / "google_review.html"

# User-curated state edited via the map UI
FAVORITES_JSON = USER_DIR / "favorites.json"
BLACKLIST_JSON = USER_DIR / "blacklist.json"
# Pinned places (right-click → 加入收藏). Distinct from FAVORITES_JSON which
# stores Tabelog restaurant URLs; this one stores user-named map pins.
BOOKMARKS_JSON = USER_DIR / "bookmarks.json"

# Rendered artifacts — committed to docs/ so Cloudflare Pages picks them up.
# Core fields (lat/lon, filterable bits, tooltip) fetched on boot; popup HTML
# split out so it can be fetched lazily on first marker click.
MAP_HTML = DOCS_DIR / "index.html"
DOCS_DATA_DIR = DOCS_DIR / "data"
RESTAURANTS_JSON = DOCS_DATA_DIR / "restaurants.json"
POPUPS_JSON = DOCS_DATA_DIR / "popups.json"
# Traditional Chinese variant of popups.json — same structure, with the
# Chinese policy and award-ribbon fields run through OpenCC s2t. Fetched
# in place of popups.json when activeLang === 'zh-TW'.
POPUPS_TW_JSON = DOCS_DATA_DIR / "popups-tw.json"
# English variant. Same structure as popups.json with the policy field
# overlaid from data/tabelog/policy_en.json (built by the translation
# pass). Restaurants the translator hasn't covered yet keep the Chinese
# policy as a fallback so the popup never goes blank.
POPUPS_EN_JSON = DOCS_DATA_DIR / "popups-en.json"
# Japanese variant. Same structure as popups.json with the policy field
# overlaid from the original `reservation_policy` column of tabelog.csv —
# i.e. the literal Japanese text Tabelog publishes. Restaurants whose CSV
# row has no policy fall back to the Chinese popup row.
POPUPS_JA_JSON = DOCS_DATA_DIR / "popups-ja.json"
# Service worker — caches the big JSON / GeoJSON / tile assets so repeat
# visits skip the network round-trip. Built fresh per run with a version
# stamp so an older SW can't keep serving stale data after a redeploy.
SW_JS = DOCS_DIR / "sw.js"
# M-094: rows that had an address but never made it into restaurants.json
# (geocoding found nothing). Written next to restaurants.json every build so
# the omission is visible instead of scrolling past in the build log.
DROPPED_JSON = DOCS_DATA_DIR / "dropped.json"
GEOCODE_CACHE = CACHE_DIR / "geocode_cache.json"
# Repo-shipped landmark set: always rendered on the map regardless of
# whether the visitor has Gist sync configured, so friends opening the
# deployed page get a populated map out of the box. Same i18n schema as
# bookmarks (name_src/sc/tc/jp/en + emoji + lat/lon + category). Edit the
# JSON directly; there's no separate build step.
FAVORITES_BUILTIN_JSON = DATA / "favorites_builtin.json"

# UI translation tables. en.json is hand-edited Chinese-to-English
# {cn_run: en_text} pairs covering every CJK run that appears as a text
# node on the rendered page. map.py loads it at build time, intersects
# with the actual runs in docs/index.html, and ships the result inline
# as TEXT_EN_MAP. Runs without an entry stay in Chinese at runtime.
I18N_DIR = DATA / "i18n"
I18N_EN_JSON = I18N_DIR / "en.json"
# Japanese variant of the UI translation table. Same shape as en.json,
# {cn_run: ja_text}. Place names use natural Japanese forms (東京タワー,
# 渋谷スクランブル交差点) rather than re-romanising.
I18N_JA_JSON = I18N_DIR / "ja.json"

# M-056: build-contract checker (scripts/verify_build.py) and the small JSON
# it keeps its moving thresholds in (last known-good restaurants.json row
# count, so "the corpus silently shrank by 20%" fails the build).
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
VERIFY_BASELINE_JSON = SCRIPTS_DIR / "verify_baseline.json"
# M-056: the repo's test suites. tests/pipeline + tests/compat + the smoke
# run live here; tests/worker and tests/sync are owned by the worker / sync
# work. See tests/README.md.
TESTS_DIR = PROJECT_ROOT / "tests"
# M-056: machine-readable summary of the last map.py run (row counts, popup
# slot count, missing-translation counts). Written every build, read by
# scripts/verify_build.py so the contract check doesn't have to scrape stdout.
# Lives under data/output/ because it describes a local build, not the site.
BUILD_REPORT_JSON = OUTPUT_DIR / "build_report.json"

# In-page help content directory. Currently unused — was authored for the
# old Gist sync flow, which got replaced by Google OAuth. Kept around as a
# parking spot for future help docs.
HELP_DIR = DOCS_DIR / "help"


def _ensure_dirs() -> None:
    """Create data/* subdirs so scripts that write to them don't trip on
    missing parents."""
    for d in (
        OMAKASE_DIR, TABELOG_DIR, USER_DIR, OUTPUT_DIR,
        CACHE_DIR, INTERMEDIATE_DIR, DOCS_DIR, DOCS_DATA_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
