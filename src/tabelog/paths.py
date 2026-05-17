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

# User-curated state edited via the map UI
FAVORITES_JSON = USER_DIR / "favorites.json"
BLACKLIST_JSON = USER_DIR / "blacklist.json"

# Rendered artifact — committed to docs/ so GitHub Pages picks it up.
MAP_HTML = DOCS_DIR / "index.html"
GEOCODE_CACHE = CACHE_DIR / "geocode_cache.json"
ATTRACTIONS_CSV = DATA / "attractions.csv"


def _ensure_dirs() -> None:
    """Create data/* subdirs so scripts that write to them don't trip on
    missing parents."""
    for d in (
        OMAKASE_DIR, TABELOG_DIR, USER_DIR, OUTPUT_DIR,
        CACHE_DIR, INTERMEDIATE_DIR, DOCS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
