#!/usr/bin/env python3
"""Build-contract checker (M-056).

    uv run python scripts/verify_build.py
    uv run python scripts/verify_build.py --build-log path/to/build.log
    uv run python scripts/verify_build.py --update-baseline   # after a real corpus change

Reads the build OUTPUT (docs/data/*.json, docs/index.html, docs/sw.js) and
asserts the invariants the front end silently assumes. Exits non-zero with a
readable reason on the first violated one. Nothing here touches the network,
the Worker, or any user state — it only reads files.

Why each check exists (all from the 2026-09-05 audit):

  popups parity   the bottom sheet indexes popup arrays by slot number, and
                  which of the four files it loads depends on the UI language.
                  A key present in one variant but not another = an empty card
                  in that language only. The check is "the four agree with each
                  other", never "each row has exactly N slots" — the design
                  work adds slots, and a hard-coded N would fail the day it
                  lands.
  restaurants     row count vs the last known-good value (a silent 20% drop is
                  what M-019 / M-094 produce), unique detail_url (the sync
                  layer keys favorites by it), coordinates inside Japan.
  localStorage    every key name the deployed page must keep reading. Renaming
                  one is the cardinal-rule violation in CLAUDE.md: existing
                  users lose their favorites / bookmarks on the next refresh.
  sw.js           ALL five popup variants must stay in CURRENT_VERSIONED_URLS
                  (activate's GC allowlist) or a visitor who switched language
                  loses the copy they are still using (M-062).
  manifest        id / start_url / scope are the install identity (M-145).
                  Changing `id` orphans every home-screen icon already out
                  there and makes a re-install a second, separate app.
  about           the 关于本站 sheet must carry the real APP_VERSION and
                  DATA_SCRAPED_AT, not an unsubstituted placeholder (M-119).
  i18n            missing EN/JA translation counts must not grow past the
                  2026-09-05 baseline — every new Chinese UI string has to be
                  added to data/i18n/{en,ja}.json in the same change.

The moving threshold (last good restaurants.json row count) lives in
scripts/verify_baseline.json so this file stays declarative.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from tabelog.paths import (  # noqa: E402
    BUILD_REPORT_JSON,
    DOCS_DIR,
    MAP_HTML,
    POPUPS_EN_JSON,
    POPUPS_JA_JSON,
    POPUPS_JSON,
    POPUPS_TW_JSON,
    RESTAURANTS_JSON,
    SW_JS,
    TABELOG_CSV,
    VERIFY_BASELINE_JSON,
    atomic_write_json,
)

# --- constants ---------------------------------------------------------------

# Japan bounding box, generous at the edges (Yonaguni 122.9E / Minamitorishima
# 153.99E / Okinotorishima 20.4N / Benten-jima 45.55N).
JAPAN_BBOX = (20.0, 46.2, 122.5, 154.5)  # lat_min, lat_max, lon_min, lon_max

# How far restaurants.json may shrink versus the recorded baseline before the
# build is considered broken.
MAX_SHRINK_PCT = 5.0

# The release this tree is supposed to be. Kept here (not read blindly from
# map.py) so that forgetting to bump APP_VERSION fails the gate instead of
# silently shipping the previous version number in the 关于本站 sheet.
# Bump this, map.py APP_VERSION, CHANGELOG.md and the git tag together.
EXPECTED_APP_VERSION = "2.1.0"

# map.py is the single source of both build-time facts the About sheet states.
# Parsed as text rather than imported: importing map.py runs the whole render
# module (folium, the corpus, the i18n tables) as a side effect.
MAP_PY = REPO / "src" / "tabelog" / "scrape" / "map.py"
_APP_VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"([^"]+)"', re.M)
_SCRAPED_AT_RE = re.compile(r'^DATA_SCRAPED_AT\s*=\s*"([^"]+)"', re.M)


def _read_map_py_stamps() -> tuple[str | None, str | None]:
    """(APP_VERSION, DATA_SCRAPED_AT) as literals in map.py, or (None, None)."""
    try:
        src = MAP_PY.read_text(encoding="utf-8")
    except OSError:
        return None, None
    v = _APP_VERSION_RE.search(src)
    d = _SCRAPED_AT_RE.search(src)
    return (v.group(1) if v else None, d.group(1) if d else None)

# CLAUDE.md "Backwards compatibility": these key names are load-bearing for
# every deployed browser. The page must still mention every one of them.
REQUIRED_LOCALSTORAGE_KEYS = [
    "omakase_state_cache_v2",
    "tabelog.auth",
    "tabelog.bookmarks",
    "tabelog.filterState",
    "tabelog.syncBase",
    "tabelog.lang",
    "tabelog.mapView",
    "tabelog.showAttractions",
    "tabelog.showTransit",
    "tabelog.showBookmarks",
]

# All five versioned data URLs the service worker must keep in its GC
# allowlist. The four popups variants are the ones that matter (M-062).
REQUIRED_SW_VERSIONED = [
    "data/restaurants.json",
    "data/popups.json",
    "data/popups-tw.json",
    "data/popups-en.json",
    "data/popups-ja.json",
]

# Baseline from audit_outputs/impl-2026-09-05/m1/baseline-build.log (the build
# taken at the start of the 2.0.0 work, before any of the milestone edits).
# These are CEILINGS, not targets: adding a Chinese UI string without adding
# its en.json / ja.json entry pushes the count up and fails the build.
# (Fallback only — used when the check has just a count, e.g. --build-log.
# The set-based comparison against scripts/verify_baseline.json is preferred.)
BASELINE_MISSING_EN = 514
BASELINE_MISSING_JA = 515

POPUP_FILES = {
    "popups.json": POPUPS_JSON,
    "popups-tw.json": POPUPS_TW_JSON,
    "popups-en.json": POPUPS_EN_JSON,
    "popups-ja.json": POPUPS_JA_JSON,
}


# --- plumbing ----------------------------------------------------------------

class Failure(Exception):
    pass


_failures: list[str] = []
_notes: list[str] = []


def fail(check: str, message: str) -> None:
    _failures.append(f"[{check}] {message}")
    print(f"  FAIL  {check}: {message}")


def ok(check: str, message: str) -> None:
    print(f"  ok    {check}: {message}")


def note(message: str) -> None:
    _notes.append(message)
    print(f"  note  {message}")


def load_json(path: Path) -> object:
    if not path.exists():
        raise Failure(f"{path} does not exist — run map.py first")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Failure(f"{path} is not valid JSON: {e}")


# --- checks ------------------------------------------------------------------

def check_popup_parity() -> None:
    """The four popup variants must describe the same restaurants with the
    same slot count per restaurant. Asserted pairwise against popups.json,
    never against a hard-coded slot number."""
    loaded: dict[str, dict] = {}
    for name, path in POPUP_FILES.items():
        data = load_json(path)
        if not isinstance(data, dict):
            fail("popups", f"{name} is not a JSON object")
            return
        loaded[name] = data

    ref_name = "popups.json"
    ref = loaded[ref_name]
    ref_keys = set(ref)
    for name, data in loaded.items():
        if name == ref_name:
            continue
        keys = set(data)
        if keys != ref_keys:
            only_ref = sorted(ref_keys - keys)[:5]
            only_this = sorted(keys - ref_keys)[:5]
            fail(
                "popups",
                f"{name} key set differs from {ref_name}: "
                f"{len(ref_keys - keys)} missing here (e.g. {only_ref}), "
                f"{len(keys - ref_keys)} extra here (e.g. {only_this}). "
                f"The bottom sheet would render an empty card in that "
                f"language for every missing URL.",
            )
            return

    slot_counts: set[int] = set()
    mismatches: list[str] = []
    for url, arr in ref.items():
        n = len(arr) if isinstance(arr, list) else -1
        slot_counts.add(n)
        for name, data in loaded.items():
            if name == ref_name:
                continue
            other = data[url]
            if not isinstance(other, list) or len(other) != n:
                mismatches.append(
                    f"{url}: {ref_name} has {n} slots, {name} has "
                    f"{len(other) if isinstance(other, list) else 'not-a-list'}"
                )
                if len(mismatches) >= 5:
                    break
        if mismatches:
            break
    if mismatches:
        fail("popups", "slot count differs between variants: " + "; ".join(mismatches))
        return
    if -1 in slot_counts:
        fail("popups", f"{ref_name} contains a value that is not a list")
        return
    ok(
        "popups",
        f"4 variants agree on {len(ref_keys):,} URLs, "
        f"{sorted(slot_counts)} slots per entry (count not asserted, only parity)",
    )


def check_popup_slots() -> None:
    """M-029 / M-030 — the shape of the three slots the 2.0.0 card reads.

    Deliberately NOT a slot-count assertion (check_popup_parity owns that,
    and only as "the four agree"). These are per-value contracts:

      p[9]   None, or a dict whose keys are a subset of {b, lead, cancel,
             note}; b ∈ {0,1,2} and IDENTICAL across the four variants (it
             is the bookability verdict, read off the Japanese original, so
             a language-dependent b would mean the card told an EN reader
             the opposite of what it told a JA reader); lead / cancel / note
             non-empty strings. b must cover ≥99% of rows — the corpus has
             43 rows whose policy text does not start with an enumeration
             word, and a sudden drop means the head-word list broke.
      p[10]  None, or a non-empty string that is not the literal '-'
             (7,926 rows have a non-empty `holiday`, 2,578 of them are '-';
             rendering those would put "休 -" on a quarter of the cards).
      p[11]  None, or a non-negative int (metres to the station).
    """
    variants: dict[str, dict] = {}
    for name, path in POPUP_FILES.items():
        data = load_json(path)
        if not isinstance(data, dict):
            fail("popup-slots", f"{name} is not a JSON object")
            return
        variants[name] = data

    ref = variants["popups.json"]
    if not ref:
        fail("popup-slots", "popups.json is empty")
        return
    if len(next(iter(ref.values()))) <= 11:
        fail(
            "popup-slots",
            "popup arrays have no slot 9/10/11 — map.py did not append the "
            "policy struct / holiday / station distance (M-029, M-030).",
        )
        return

    allowed = {"b", "lead", "cancel", "note"}
    problems: list[str] = []
    b_hits = 0
    holiday_hits = 0
    station_hits = 0

    def _slot(arr, i):
        return arr[i] if isinstance(arr, list) and len(arr) > i else None

    for url, arr in ref.items():
        if len(problems) >= 5:
            break
        # --- p[9] ---------------------------------------------------------
        bs = []
        for name, data in variants.items():
            pol = _slot(data.get(url), 9)
            if pol is None:
                bs.append(None)
                continue
            if not isinstance(pol, dict):
                problems.append(f"{url} [{name}] p[9] is {type(pol).__name__}, not dict/None")
                break
            extra = set(pol) - allowed
            if extra:
                problems.append(f"{url} [{name}] p[9] has unknown keys {sorted(extra)}")
                break
            b = pol.get("b")
            if "b" in pol and b not in (0, 1, 2):
                problems.append(f"{url} [{name}] p[9].b is {b!r}, expected 0/1/2")
                break
            bad = [
                k for k in ("lead", "cancel", "note")
                if k in pol and (not isinstance(pol[k], str) or not pol[k].strip())
            ]
            if bad:
                problems.append(f"{url} [{name}] p[9] has empty/non-str {bad}")
                break
            bs.append(b)
        else:
            if len(set(bs)) > 1:
                problems.append(
                    f"{url} p[9].b differs between variants: "
                    f"{dict(zip(variants, bs))} — the verdict comes from the "
                    f"Japanese original and must be the same in all four"
                )
            elif bs and bs[0] is not None:
                b_hits += 1
        # --- p[10] / p[11] ------------------------------------------------
        hol = _slot(arr, 10)
        if hol is not None:
            if not isinstance(hol, str) or not hol.strip() or hol.strip() == "-":
                problems.append(f"{url} p[10] is {hol!r} (expected a real closing-day string or None)")
            else:
                holiday_hits += 1
        stm = _slot(arr, 11)
        if stm is not None:
            if not isinstance(stm, int) or isinstance(stm, bool) or stm < 0:
                problems.append(f"{url} p[11] is {stm!r} (expected a non-negative int or None)")
            else:
                station_hits += 1

    if problems:
        fail("popup-slots", "; ".join(problems))
        return

    total = len(ref)
    pct = 100.0 * b_hits / total if total else 0.0
    if pct < 99.0:
        fail(
            "popup-slots",
            f"p[9].b covers only {b_hits:,}/{total:,} ({pct:.1f}%), expected "
            f">=99%. policy_b()'s head-word list (完全予約制 / 予約不可 / 予約可) "
            f"no longer matches the corpus.",
        )
    else:
        ok("popup-slots", f"p[9].b on {b_hits:,}/{total:,} rows ({pct:.1f}%), same in all 4 variants")

    if not 5000 <= holiday_hits <= 5700:
        fail(
            "popup-slots",
            f"p[10] (closing days) present on {holiday_hits:,} rows, expected "
            f"5,000-5,700. Either the day-token filter stopped rejecting the "
            f"2,578 '-' rows, or it started rejecting real ones.",
        )
    else:
        ok("popup-slots", f"p[10] closing days on {holiday_hits:,} rows")
    ok("popup-slots", f"p[11] station distance on {station_hits:,} rows")


def check_restaurant_fields() -> None:
    """M-023 / B1 — the two fields the region filter and the result list
    read off every restaurants.json row, plus the PREFS table they are
    index-aligned with."""
    data = load_json(RESTAURANTS_JSON)
    if not isinstance(data, list):
        fail("row-fields", "restaurants.json is not a JSON array")
        return

    seen_pref: set[int] = set()
    bad_pref: list[str] = []
    bad_st: list[str] = []
    pref_hits = st_hits = 0
    for r in data:
        if not isinstance(r, dict):
            continue
        if "pref" in r:
            p = r["pref"]
            if not isinstance(p, int) or isinstance(p, bool) or not 0 <= p <= 46:
                if len(bad_pref) < 5:
                    bad_pref.append(f"{r.get('detail_url')}: pref={p!r}")
            else:
                pref_hits += 1
                seen_pref.add(p)
        if "st" in r:
            s = r["st"]
            if not isinstance(s, str) or not s.strip():
                if len(bad_st) < 5:
                    bad_st.append(f"{r.get('detail_url')}: st={s!r}")
            else:
                st_hits += 1
    if bad_pref:
        fail("row-fields", "pref must be an int 0-46: " + "; ".join(bad_pref))
    if bad_st:
        fail("row-fields", "st must be a non-empty string: " + "; ".join(bad_st))

    total = len(data)
    pct = 100.0 * pref_hits / total if total else 0.0
    if pct < 99.0:
        fail(
            "row-fields",
            f"pref covers only {pref_hits:,}/{total:,} ({pct:.1f}%), expected "
            f">=99%. prefecture_index() stopped matching addresses — the "
            f"region filter would silently hide those rows.",
        )
    elif len(seen_pref) != 47:
        fail(
            "row-fields",
            f"only {len(seen_pref)}/47 prefectures appear in the payload "
            f"(missing indices {sorted(set(range(47)) - seen_pref)}). The "
            f"region <select> would offer an empty option.",
        )
    else:
        ok("row-fields", f"pref on {pref_hits:,}/{total:,} rows, all 47 prefectures present")
    ok("row-fields", f"st (station) on {st_hits:,}/{total:,} rows")

    if not MAP_HTML.exists():
        fail("row-fields", f"{MAP_HTML} does not exist — run map.py first")
        return
    html = MAP_HTML.read_text(encoding="utf-8")
    hits = re.findall(r"var PREFS\s*=\s*(\[.*?\]);", html, re.S)
    if len(hits) != 1:
        fail(
            "row-fields",
            f"expected exactly one `var PREFS = [...]` in docs/index.html, "
            f"found {len(hits)}",
        )
        return
    try:
        prefs = json.loads(hits[0])
    except json.JSONDecodeError as e:
        fail("row-fields", f"the inlined PREFS table is not valid JSON: {e}")
        return
    if not isinstance(prefs, list) or len(prefs) != 47:
        fail("row-fields", f"PREFS has {len(prefs) if isinstance(prefs, list) else '?'} entries, expected 47")
        return
    missing_keys = [
        i for i, p in enumerate(prefs)
        if not isinstance(p, dict) or not {"ja", "sc", "tc", "en", "n"} <= set(p)
    ]
    if missing_keys:
        fail("row-fields", f"PREFS entries missing ja/sc/tc/en/n at indices {missing_keys[:5]}")
        return
    ok("row-fields", "PREFS: 47 entries inlined, each with ja/sc/tc/en/n")


def check_restaurants(baseline: dict, update_baseline: bool) -> None:
    data = load_json(RESTAURANTS_JSON)
    if not isinstance(data, list):
        fail("restaurants", "restaurants.json is not a JSON array")
        return
    n = len(data)

    prev = baseline.get("restaurants_rows")
    if prev is None:
        note(
            f"no restaurants_rows baseline recorded yet — seeding with {n}. "
            f"Re-run with --update-baseline after an intentional corpus change."
        )
        baseline["restaurants_rows"] = n
    else:
        floor = prev * (1.0 - MAX_SHRINK_PCT / 100.0)
        if n < floor:
            fail(
                "restaurants",
                f"row count fell to {n:,} from a baseline of {prev:,} "
                f"({100.0 * (prev - n) / prev:.1f}% down, limit "
                f"{MAX_SHRINK_PCT:g}%). Check docs/data/dropped.json: rows "
                f"lose their marker when the address is blank (M-019) or "
                f"geocoding failed (M-094). If the shrink is intentional, "
                f"re-run with --update-baseline.",
            )
        else:
            ok("restaurants", f"{n:,} rows (baseline {prev:,})")
        if update_baseline:
            baseline["restaurants_rows"] = n

    urls = [r.get("detail_url") for r in data if isinstance(r, dict)]
    missing_url = sum(1 for u in urls if not u)
    if missing_url:
        fail("restaurants", f"{missing_url} rows have no detail_url")
    dupes = len(urls) - len(set(urls))
    if dupes:
        seen: set[str] = set()
        example = next((u for u in urls if u in seen or seen.add(u)), None)
        fail(
            "restaurants",
            f"{dupes} duplicate detail_url values (e.g. {example!r}). "
            f"Favorites are keyed by detail_url, so a duplicate makes one "
            f"star toggle two markers.",
        )
    elif not missing_url:
        ok("restaurants", f"{len(urls):,} detail_url values, all unique")

    lat_min, lat_max, lon_min, lon_max = JAPAN_BBOX
    out_of_box = []
    for r in data:
        if not isinstance(r, dict):
            continue
        lat, lon = r.get("lat"), r.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            out_of_box.append((r.get("name"), lat, lon))
        elif not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            out_of_box.append((r.get("name"), lat, lon))
        if len(out_of_box) >= 5:
            break
    if out_of_box:
        fail(
            "restaurants",
            f"coordinates outside the Japan bbox {JAPAN_BBOX}: {out_of_box}",
        )
    else:
        ok("restaurants", "all coordinates inside the Japan bbox")


def check_localstorage_keys() -> None:
    if not MAP_HTML.exists():
        fail("storage-keys", f"{MAP_HTML} does not exist — run map.py first")
        return
    html = MAP_HTML.read_text(encoding="utf-8")
    missing = [k for k in REQUIRED_LOCALSTORAGE_KEYS if k not in html]
    if missing:
        fail(
            "storage-keys",
            f"{len(missing)} localStorage key name(s) no longer appear in "
            f"docs/index.html: {missing}. Renaming one orphans every existing "
            f"user's saved state — see CLAUDE.md 'Backwards compatibility'. "
            f"If the rename is genuinely required, ship a read-old/write-new "
            f"migration and update this list in the same commit.",
        )
    else:
        ok("storage-keys", f"all {len(REQUIRED_LOCALSTORAGE_KEYS)} key names present")


# M-031 / E2. Sub-collections ride inside the existing `bookmarks` array as
# coordinate-less {category:'meta'} entries rather than as a new localStorage
# key or a new top-level KV field. Four load-bearing properties of that
# decision, each of which is a silent data-loss bug if it ever goes away:
#
#   1. renderBookmark bails on anything without numeric lat/lon, so metadata
#      entries paint no marker. Drop the guard and every list row becomes a
#      pin at (undefined, undefined).
#   2. rebuildHiddenIds still matches ONLY category 'hidden' + an 'fb-' id.
#      Widen it to 'meta' and list rows start tombstoning built-in landmarks.
#   3. buildBody still sends favorites as Array.from(state.fav) — a plain
#      string array. The Worker replaces the blob wholesale, so a richer
#      shape here would be written back by new clients and misread by old.
#   4. 'meta' is actually spelled somewhere, i.e. the feature is still built
#      in and this check is testing the live page rather than passing by
#      accident.
#
# Regex existence assertions on purpose — line numbers in a 15k-line
# generator are worthless, and the whole JS lands on two enormous lines.
SUBCOLLECTION_CONTRACTS: list[tuple[str, str, str]] = [
    (
        "renderBookmark coordinate guard",
        r"typeof\s+bm\.lat\s*!==\s*'number'\s*\|\|\s*typeof\s+bm\.lon\s*!==\s*'number'",
        "metadata entries would be rendered as markers at undefined coords",
    ),
    (
        "rebuildHiddenIds is 'hidden'-only",
        r"bm\.category\s*===\s*'hidden'\s*&&\s*typeof\s+bm\.id\s*===\s*'string'",
        "widening the tombstone test past 'hidden' makes sub-collection rows "
        "hide built-in landmarks",
    ),
    (
        "buildBody favorites stay a plain string array",
        r"favorites:\s*Array\.from\(state\.fav\)",
        "the KV blob's favorites field must stay a flat string array — see "
        "CLAUDE.md 'Never change the KV blob schema breakingly'",
    ),
    (
        "sub-collection entries are category 'meta'",
        r"category:\s*'meta'",
        "sub-collections are gone, or moved off the 'meta' category they "
        "share with nothing else",
    ),
]


def check_subcollections() -> None:
    if not MAP_HTML.exists():
        fail("subcollections", f"{MAP_HTML} does not exist — run map.py first")
        return
    html = MAP_HTML.read_text(encoding="utf-8")
    broken = [
        f"{name} ({why})"
        for name, pattern, why in SUBCOLLECTION_CONTRACTS
        if not re.search(pattern, html)
    ]
    if broken:
        fail(
            "subcollections",
            f"{len(broken)} sub-collection invariant(s) no longer hold in "
            f"docs/index.html: {broken}",
        )
        return
    # And the two categories must stay distinct: 'meta' must never be spelled
    # as a hidden-tombstone category anywhere.
    if re.search(r"category\s*:\s*'hidden'\s*,\s*kind\s*:", html):
        fail(
            "subcollections",
            "a sub-collection entry is being written with category 'hidden' — "
            "that is the built-in landmark tombstone category, not this one",
        )
        return
    ok("subcollections", f"all {len(SUBCOLLECTION_CONTRACTS)} E2 invariants hold")


def check_service_worker() -> None:
    if not SW_JS.exists():
        fail("sw", f"{SW_JS} does not exist — run map.py first")
        return
    js = SW_JS.read_text(encoding="utf-8")
    m = re.search(r"CURRENT_VERSIONED_URLS\s*=\s*(\[[^\]]*\])", js)
    if not m:
        fail("sw", "CURRENT_VERSIONED_URLS not found in docs/sw.js")
        return
    try:
        urls = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        fail("sw", f"CURRENT_VERSIONED_URLS is not a JSON array: {e}")
        return
    bare = {u.split("?", 1)[0] for u in urls if isinstance(u, str)}
    missing = [u for u in REQUIRED_SW_VERSIONED if u not in bare]
    if missing:
        fail(
            "sw",
            f"CURRENT_VERSIONED_URLS is missing {missing}. activate() deletes "
            f"every cached entry not on this list, so a visitor whose UI "
            f"language uses a dropped variant loses it mid-session (M-062).",
        )
        return
    n_popups = sum(1 for u in bare if u.startswith("data/popups"))
    if n_popups != 4:
        fail("sw", f"expected 4 popups variants in the allowlist, found {n_popups}")
        return
    ok("sw", f"{len(bare)} versioned URLs incl. all 4 popups variants")


def check_manifest_identity() -> None:
    """M-145: the install identity is frozen.

    `id` is what the browser keys an installed app by. Change it and every
    existing home-screen / desktop icon points at an app that no longer
    exists, while a re-install lands as a SECOND app — the install-side
    equivalent of renaming a localStorage key. `start_url` / `scope` are the
    same story for what the installed window is allowed to navigate to.
    Shortcuts (M-145) may be added freely; they must stay inside the scope.
    """
    path = DOCS_DIR / "manifest.webmanifest"
    if not path.exists():
        fail("manifest", f"{path} does not exist")
        return
    try:
        man = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail("manifest", f"manifest.webmanifest is not valid JSON: {e}")
        return
    for key in ("id", "start_url", "scope"):
        if man.get(key) != "/":
            fail(
                "manifest",
                f'{key} is {man.get(key)!r}, expected "/". Changing it orphans '
                f"every already-installed copy of the app.",
            )
            return
    shortcuts = man.get("shortcuts") or []
    if not isinstance(shortcuts, list):
        fail("manifest", "shortcuts must be a list")
        return
    for s in shortcuts:
        if not isinstance(s, dict) or not s.get("name") or not s.get("url"):
            fail("manifest", f"shortcut entry missing name/url: {s!r}")
            return
        if not str(s["url"]).startswith("/"):
            fail("manifest", f"shortcut url {s['url']!r} is outside the scope")
            return
    ok("manifest", f'id/start_url/scope = "/", {len(shortcuts)} shortcut(s)')


def check_about_stamps() -> None:
    """M-119: the 关于本站 sheet has to actually carry the two build facts.

    APP_VERSION and DATA_SCRAPED_AT are substituted into ONBOARD_HTML at
    import time in map.py. If either placeholder ever stops being replaced
    the page ships a literal `v__APP_VERSION__`, which no test elsewhere
    would notice.
    """
    if not MAP_HTML.exists():
        fail("about", f"{MAP_HTML} does not exist — run map.py first")
        return
    app_version, scraped_at = _read_map_py_stamps()
    if app_version is None or scraped_at is None:
        fail("about", "could not read APP_VERSION / DATA_SCRAPED_AT out of "
                      "src/tabelog/scrape/map.py")
        return
    if app_version != EXPECTED_APP_VERSION:
        fail(
            "about",
            f"map.py APP_VERSION is {app_version!r}, expected "
            f"{EXPECTED_APP_VERSION!r} — bump one of the two (map.py, "
            f"scripts/verify_build.py EXPECTED_APP_VERSION) and CHANGELOG.md "
            f"together.",
        )
        return
    html = MAP_HTML.read_text(encoding="utf-8")
    missing = [s for s in (f"v{app_version}", scraped_at) if s not in html]
    if missing:
        fail(
            "about",
            f"docs/index.html does not mention {missing} — the 关于本站 sheet "
            f"lost its version / scrape-date stamp (map.py APP_VERSION / "
            f"DATA_SCRAPED_AT).",
        )
        return
    if "__APP_VERSION__" in html or "__DATA_SCRAPED_AT__" in html:
        fail("about", "an unsubstituted __APP_VERSION__/__DATA_SCRAPED_AT__ "
                      "placeholder reached docs/index.html")
        return
    ok("about", f"version v{app_version} + scrape date {scraped_at} present")


def check_i18n(build_log: Path | None, baseline: dict, update_baseline: bool) -> None:
    """Untranslated UI strings must not grow past the 2026-09-05 baseline.

    The counts cannot be recomputed from docs/index.html: the rendered page
    already carries the injected TEXT_*_MAP tables, whose Traditional variants
    are themselves CJK runs and inflate the number (768 instead of 525 on the
    2026-09-05 build). So read them from the build report map.py writes, or
    from a saved build log.

    When the build report is available the check is set-based, not count-based:
    it names the exact runs that are missing today and were not missing at the
    baseline, which is the actionable form ("you added this string, translate
    it"). A log only carries the counts, so that path falls back to comparing
    numbers.
    """
    missing_en = missing_ja = None
    runs_en: list[str] | None = None
    runs_ja: list[str] | None = None
    source = ""
    if build_log is not None:
        if not build_log.exists():
            fail("i18n", f"--build-log {build_log} does not exist")
            return
        text = build_log.read_text(encoding="utf-8", errors="replace")
        m_en = re.search(r"missing EN translations:\s*(\d+)", text)
        m_ja = re.search(r"missing JA translations:\s*(\d+)", text)
        missing_en = int(m_en.group(1)) if m_en else 0
        missing_ja = int(m_ja.group(1)) if m_ja else 0
        source = f"from {build_log.name}"
    elif BUILD_REPORT_JSON.exists():
        report = load_json(BUILD_REPORT_JSON)
        if not isinstance(report, dict):
            fail("i18n", f"{BUILD_REPORT_JSON} is not a JSON object")
            return
        missing_en = int(report.get("missing_en_count", 0))
        missing_ja = int(report.get("missing_ja_count", 0))
        runs_en = list(report.get("missing_en") or [])
        runs_ja = list(report.get("missing_ja") or [])
        source = f"from {BUILD_REPORT_JSON.name} ({report.get('generated_at')})"
    else:
        note(
            f"i18n check skipped: no {BUILD_REPORT_JSON.name} (run map.py, or "
            f"pass --build-log path/to/build.log)"
        )
        return

    base_en = baseline.get("missing_en")
    base_ja = baseline.get("missing_ja")
    if runs_en is not None and isinstance(base_en, list) and isinstance(base_ja, list):
        new_en = sorted(set(runs_en) - set(base_en))
        new_ja = sorted(set(runs_ja) - set(base_ja))
        if update_baseline:
            baseline["missing_en"] = sorted(runs_en)
            baseline["missing_ja"] = sorted(runs_ja)
            ok("i18n", f"baseline updated to EN {len(runs_en)} / JA {len(runs_ja)} runs")
            return
        if new_en or new_ja:
            fail(
                "i18n",
                f"{len(new_en)} EN / {len(new_ja)} JA Chinese UI string(s) on the "
                f"page have no translation entry and were not there at the "
                f"baseline ({source}). Add each to data/i18n/en.json AND "
                f"data/i18n/ja.json, then rebuild. New EN runs: "
                f"{new_en[:20]}{' ...' if len(new_en) > 20 else ''}; "
                f"new JA runs: {new_ja[:20]}{' ...' if len(new_ja) > 20 else ''}",
            )
        else:
            ok(
                "i18n",
                f"no new untranslated UI strings (EN {missing_en} / JA "
                f"{missing_ja} runs, all on the 2026-09-05 baseline list; "
                f"{source})",
            )
        return

    problems = []
    if missing_en > BASELINE_MISSING_EN:
        problems.append(f"EN {missing_en} > baseline {BASELINE_MISSING_EN}")
    if missing_ja > BASELINE_MISSING_JA:
        problems.append(f"JA {missing_ja} > baseline {BASELINE_MISSING_JA}")
    if problems:
        fail(
            "i18n",
            f"untranslated UI strings grew ({'; '.join(problems)}, {source}). "
            f"Every new Chinese string needs an entry in data/i18n/en.json AND "
            f"data/i18n/ja.json; the build log lists the first 30 missing runs.",
        )
    else:
        ok(
            "i18n",
            f"missing EN {missing_en} <= {BASELINE_MISSING_EN}, "
            f"missing JA {missing_ja} <= {BASELINE_MISSING_JA} ({source})",
        )


def check_csv_consistency() -> None:
    """Optional: only runs when the (gitignored) master CSV is present."""
    if not TABELOG_CSV.exists():
        note(f"{TABELOG_CSV.name} not present — skipping CSV cross-check "
             f"(expected on a fresh clone)")
        return
    import csv

    with TABELOG_CSV.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    with_addr = sum(1 for r in rows if (r.get("address") or "").strip())
    data = load_json(RESTAURANTS_JSON)
    published = len(data) if isinstance(data, list) else 0
    dropped = with_addr - published
    if dropped < 0:
        fail(
            "csv",
            f"restaurants.json has {published:,} rows but the CSV only has "
            f"{with_addr:,} with an address — the payload is stale or the CSV "
            f"was truncated.",
        )
        return
    limit = max(10, round(len(rows) * 0.2 / 100))
    if dropped > limit:
        fail(
            "csv",
            f"{dropped} CSV rows with an address are missing from "
            f"restaurants.json (limit {limit}); see docs/data/dropped.json",
        )
    else:
        ok("csv", f"{len(rows):,} CSV rows, {with_addr:,} with an address, "
                  f"{published:,} published, {dropped} dropped (limit {limit})")


# --- main --------------------------------------------------------------------

def load_baseline() -> dict:
    if VERIFY_BASELINE_JSON.exists():
        try:
            data = json.loads(VERIFY_BASELINE_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        note(f"{VERIFY_BASELINE_JSON.name} unreadable — treating as empty")
    return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-log", type=Path, default=None,
                    help="read the missing EN/JA counts from a saved map.py "
                         "log instead of recomputing them from docs/index.html")
    ap.add_argument("--update-baseline", action="store_true",
                    help="record the current restaurants.json row count as the "
                         "new baseline (use after an intentional corpus change)")
    args = ap.parse_args(argv)

    print(f"verify_build: checking {DOCS_DIR}")
    baseline = load_baseline()
    baseline_before = dict(baseline)

    try:
        check_popup_parity()
        check_popup_slots()          # M-029 / M-030
        check_restaurants(baseline, args.update_baseline)
        check_restaurant_fields()    # M-023 / B1
        check_localstorage_keys()
        check_subcollections()       # M-031 / E2
        check_service_worker()
        check_manifest_identity()    # M-145
        check_about_stamps()         # M-119
        check_i18n(args.build_log, baseline, args.update_baseline)
        check_csv_consistency()
    except Failure as e:
        fail("fatal", str(e))

    if baseline != baseline_before:
        baseline["updated_at"] = __import__("time").strftime(
            "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()
        )
        atomic_write_json(VERIFY_BASELINE_JSON, baseline, indent=2)
        print(f"  wrote {VERIFY_BASELINE_JSON}")

    print()
    if _failures:
        print(f"verify_build: FAILED ({len(_failures)} problem(s))")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("verify_build: all contracts hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
