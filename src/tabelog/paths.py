"""Central data/output paths. Every script imports from here so the
on-disk layout is described in one place."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA = PROJECT_ROOT / "data"
OMAKASE_DIR = DATA / "omakase"
TABELOG_DIR = DATA / "tabelog"
USER_DIR = DATA / "user"
OUTPUT_DIR = DATA / "output"
CACHE_DIR = DATA / "cache"
INTERMEDIATE_DIR = DATA / "intermediate"
ARCHIVE_DIR = DATA / "archive"

# GitHub Pages serves from this directory.
DOCS_DIR = PROJECT_ROOT / "docs"

PROFILE_DIR = PROJECT_ROOT / ".chrome_profile"

# Omakase pipeline (omakase.in)
RESTAURANTS_CSV = OMAKASE_DIR / "restaurants.csv"
RESTAURANTS_ALL_CSV = OMAKASE_DIR / "restaurants_all.csv"
BOOKABLE_CSV = OMAKASE_DIR / "bookable.csv"

# Tabelog pipeline (tabelog.com) — unified across regions; each row carries
# a `region` column tagging which Tabelog list it was scraped from.
TABELOG_CSV = TABELOG_DIR / "tabelog.csv"
# Side store for English translations of reservation_policy_chinese, keyed
# by Tabelog detail URL. Populated by scrape/translate_policies.py, read by
# map.py when baking docs/data/popups-en.json. Separate from tabelog.csv so
# scraping / geocoding rewrites don't trample the translations.
POLICY_EN_JSON = TABELOG_DIR / "policy_en.json"

# User-curated state edited via the map UI
FAVORITES_JSON = USER_DIR / "favorites.json"
BLACKLIST_JSON = USER_DIR / "blacklist.json"
# Pinned places (right-click → 加入收藏). Distinct from FAVORITES_JSON which
# stores Tabelog restaurant URLs; this one stores user-named map pins.
BOOKMARKS_JSON = USER_DIR / "bookmarks.json"

# Rendered artifacts — committed to docs/ so GitHub Pages picks them up.
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
# Service worker — caches the big JSON / GeoJSON / tile assets so repeat
# visits skip the network round-trip. Built fresh per run with a version
# stamp so an older SW can't keep serving stale data after a redeploy.
SW_JS = DOCS_DIR / "sw.js"
GEOCODE_CACHE = CACHE_DIR / "geocode_cache.json"
ATTRACTIONS_CSV = DATA / "attractions.csv"

# UI translation tables. en.json is hand-edited Chinese-to-English
# {cn_run: en_text} pairs covering every CJK run that appears as a text
# node on the rendered page. map.py loads it at build time, intersects
# with the actual runs in docs/index.html, and ships the result inline
# as TEXT_EN_MAP. Runs without an entry stay in Chinese at runtime.
I18N_DIR = DATA / "i18n"
I18N_EN_JSON = I18N_DIR / "en.json"


def _ensure_dirs() -> None:
    """Create data/* subdirs so scripts that write to them don't trip on
    missing parents."""
    for d in (
        OMAKASE_DIR, TABELOG_DIR, USER_DIR, OUTPUT_DIR,
        CACHE_DIR, INTERMEDIATE_DIR, DOCS_DIR, DOCS_DATA_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
