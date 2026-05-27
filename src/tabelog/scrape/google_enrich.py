"""
Google Places enrichment for the Tabelog corpus (Phase 1, static).

For every restaurant in tabelog.csv this resolves the matching Google Place
and pulls: precise POI coordinates, English + Japanese formatted address,
structured address components (both languages), place types, place_id, and a
Google Maps deep link. Results land in data/tabelog/google_places.csv — a
SEPARATE ledger keyed by detail_url. It never touches tabelog.csv.

Accuracy over recall. A restaurant we can't confidently match is left
'unmatched' rather than pinned to the wrong place. Each Google candidate runs
four independent gates:

  1. Distance      — Google's pin vs the known GSI coordinate (haversine).
  2. Name          — NFKC-normalised fuzzy similarity (substring => strong).
  3. Prefecture    — parsed from the address; a mismatch hard-drops the
                     candidate (a different prefecture is never the right place).
  4. City / ward   — softer regional agreement.

Only clear winners are auto-'accepted'. Borderline candidates are flagged
'review' for a human pass; nothing questionable is silently accepted.

Matching runs in Japanese (languageCode=ja) so Google's displayName comes back
in kanji/kana and lines up with the Tabelog name. The English address is then
fetched per matched place via a cheap Place Details call (languageCode=en).

Cost model (Places API New — per-1000 price, monthly free cap):
  - Text Search Pro          $32/1k, 5,000 free/mo   — the match call (ja)
  - Place Details Essentials  $5/1k, 10,000 free/mo  — the en-address call
Keep each calendar month under ~4,800 new restaurants and both stay inside the
free caps => $0. 9,810 rows split across two months (late-May + June). Minor
retry overage bills at $32/1k — a few dollars at most.

Prerequisites:
  - Enable "Places API (New)" on your Google Cloud project.
  - Create an API key. Set a daily quota cap + a billing budget alert as a
    safety net (cheap insurance against a runaway loop or a leaked key).
  - PowerShell:  $env:GOOGLE_MAPS_API_KEY = "AIza..."

Usage:
  # 1. Validate on one prefecture first (free, a few minutes):
  uv run python src/tabelog/scrape/google_enrich.py --region tottori

  # 2. Open google_places.csv. Check the 'accepted' rows are right and skim
  #    'review'/'unmatched'. If the thresholds need nudging, edit the
  #    constants below and re-decide for free:
  uv run python src/tabelog/scrape/google_enrich.py --rescore

  # 3. A month's batch — one or more region slugs (--region accepts several).
  #    Keep each calendar month's new rows under ~4,800 to stay $0:
  uv run python src/tabelog/scrape/google_enrich.py --region osaka hyogo okayama tottori

  # 4. The rest, next calendar month (no --region => whatever's still undone):
  uv run python src/tabelog/scrape/google_enrich.py --limit 4800

  # Other:
  uv run python src/tabelog/scrape/google_enrich.py --dry-run
  uv run python src/tabelog/scrape/google_enrich.py --retry-unmatched
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
import pykakasi

from tabelog.paths import GOOGLE_PLACES_CACHE, GOOGLE_PLACES_CSV, TABELOG_CSV

# --- API endpoints + field masks -------------------------------------------

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# Every field here is Pro-tier or lower, so the search bills once at the Text
# Search Pro SKU. languageCode is 'ja' on this call (see module docstring).
SEARCH_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.location",
    "places.formattedAddress",
    "places.shortFormattedAddress",
    "places.addressComponents",
    "places.types",
    "places.primaryType",
    "places.businessStatus",
    "places.googleMapsUri",
    "places.plusCode",
])
# formattedAddress + addressComponents only => Essentials SKU. Used purely to
# fetch the English address for a place we've already matched.
DETAILS_FIELD_MASK = "id,formattedAddress,addressComponents"

# --- matching thresholds (tune during the single-region validation run) -----

SEARCH_RADIUS_M = 1500.0    # locationBias circle around the GSI coordinate
ACCEPT_DIST_M = 400.0       # auto-accept only if Google's pin is this close
REVIEW_DIST_M = 1200.0      # beyond this, never accept (likely a different place)
ACCEPT_NAME_SIM = 0.72      # auto-accept name-similarity floor
STRONG_NAME_SIM = 0.92      # a near-exact name relaxes the city/distance checks
REVIEW_NAME_SIM = 0.45      # below this, treat as a non-match
STRONG_NAME_DIST_M = 600.0  # distance allowance on the strong-name accept path
REVIEW_STRONG_NAME_SIM = 0.85  # near-exact name + same city => at least review,
#                                however far the pin (GSI likely mis-geocoded)
REVIEW_NEAR_DIST_M = 100.0     # essentially-coincident pin + same city => review
ACCEPT_NEAR_DIST_M = 100.0  # within this, accept on location alone — Google's pin
#                             sits essentially on our geocoded address point
ADDR_MATCH_DIST_M = 2000.0  # within this AND same 町 + same banchi number => accept
#                             (manual review of the first ~224 'review' rows came
#                             back 224/224 correct, so these gates were too strict)
MAX_RESULTS = 5
DO_FALLBACK_QUERY = True     # retry with name + pref/city if the full query whiffs

DEFAULT_QPS = 8.0
CHECKPOINT_EVERY = 50

# Per-1000 SKU prices, used only for the end-of-run worst-case cost print.
PRICE_SEARCH_PRO = 32.0 / 1000
PRICE_DETAILS_ESS = 5.0 / 1000

# Statuses that are "done" — never re-fetched on a later run. 'rejected' is a
# human verdict from review_google.py, so it's terminal too (a re-run must not
# re-fetch and trample it).
TERMINAL = ("accepted", "review", "unmatched", "rejected")

# Result columns the matcher fills (meta columns are added separately).
RESULT_KEYS = [
    "status", "confidence", "notes", "candidate_count",
    "place_id", "g_name", "g_lat", "g_lon", "dist_m", "name_sim",
    "pref_match", "city_match",
    "g_address_en", "g_address_ja",
    "g_address_components_ja", "g_address_components_en",
    "g_types", "g_primary_type", "g_business_status", "g_maps_url", "g_plus_code",
]

FIELDNAMES = [
    "detail_url", "region", "tabelog_name", "tabelog_address", "gsi_lat", "gsi_lon",
    "status", "confidence", "place_id", "g_name", "name_sim", "dist_m",
    "pref_match", "city_match",
    "g_lat", "g_lon", "g_address_en", "g_address_ja",
    "g_types", "g_primary_type", "g_business_status", "g_maps_url", "g_plus_code",
    "g_address_components_ja", "g_address_components_en",
    "candidate_count", "notes", "fetched_at", "batch",
]


# --- small helpers -----------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


_NAME_STRIP_RE = re.compile(r"[（）()【】\[\]「」『』〈〉・,，.。･\-—–_/|~〜!！?？'\"’“”　 ]+")

_KKS = pykakasi.kakasi()


def norm_name(s: str) -> str:
    return _NAME_STRIP_RE.sub("", nfkc(s).lower())


def _romaji(s: str) -> str:
    """Stripped Hepburn romaji. Bridges 假名 vs ローマ字 spellings, so e.g.
    'ファロ トラットリア' and 'FARO trattoria' compare as the same shop."""
    if not s:
        return ""
    try:
        roma = "".join(p.get("hepburn", "") for p in _KKS.convert(s))
    except Exception:
        roma = s
    return _NAME_STRIP_RE.sub("", roma.lower())


def _sim_pair(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # Google often tacks on a branch marker (本店 / 渋谷店) or a category prefix
    # (海鮮丼 / お食事処). Treat containment of the shorter inside the longer as
    # a strong — but not perfect — match.
    if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
        return max(ratio, 0.9)
    return ratio


def name_similarity(tabelog_name: str, google_name: str) -> float:
    """Max of the raw (kana/kanji) similarity and the romaji-transliterated
    similarity, so names differing only by script still line up."""
    raw = _sim_pair(norm_name(tabelog_name), norm_name(google_name))
    roma = _sim_pair(_romaji(tabelog_name), _romaji(google_name))
    return max(raw, roma)


def parse_pref_city(addr: str) -> tuple[str, list[str]]:
    """Pull the prefecture and municipality token(s) off the head of a Tabelog
    address. Returns (prefecture, [city...]). For 郡 (rural district) addresses
    the contained 町/村 is captured too, so the match still has something the
    Google locality can line up with."""
    a = nfkc(addr).strip()
    pref = ""
    m = re.match(r"^\s*(.+?[都道府県])", a)
    if m:
        pref = m.group(1)
    rest = a[len(pref):]
    cities: list[str] = []
    m1 = re.match(r"^(.+?[市区郡町村])", rest)
    if m1:
        cities.append(m1.group(1))
        if m1.group(1).endswith("郡"):
            m2 = re.match(r"^(.+?[町村])", rest[len(m1.group(1)):])
            if m2:
                cities.append(m2.group(1))
    return pref, cities


def google_pref_cities(components) -> tuple[str, list[str]]:
    pref = ""
    cities: list[str] = []
    for c in components or []:
        types = c.get("types", []) or []
        txt = nfkc(c.get("longText") or c.get("shortText") or "")
        if "administrative_area_level_1" in types:
            pref = txt
        if any(t in types for t in
               ("locality", "administrative_area_level_2", "sublocality_level_1")):
            if txt:
                cities.append(txt)
    return pref, cities


def _loose_eq(a: str, b: str) -> bool:
    a, b = nfkc(a), nfkc(b)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def pref_matches(tb_pref: str, g_pref: str) -> bool:
    return _loose_eq(tb_pref, g_pref)


def city_matches(tb_cities: list[str], g_cities: list[str]) -> bool:
    return any(_loose_eq(t, g) for t in tb_cities for g in g_cities)


_POSTAL_RE = re.compile(r"〒?\s*\d{3}[-－]?\d{4}")
_WARD_RE = re.compile(r"[市区郡]")
_CHO_RE = re.compile(r"(?:丁目|町)")
_NUMRUN_RE = re.compile(r"\d+(?:[-－]\d+)*")


def addr_tail(addr: str) -> tuple[str, str]:
    """Reduce a Japanese address to (town_token, banchi_digits) for cross-source
    comparison: NFKC (full-width digits -> half), postal code and admin prefix
    (up to the last 市/区/郡) stripped, hyphens dropped from the number.

    Tabelog '…東山区問屋通五条下ル上人町433' and Google '〒605-0903 …東山区上人町４３３'
    both reduce to town '…上人町' + number '433'."""
    s = _POSTAL_RE.sub("", nfkc(addr))
    s = re.sub(r"\s+", "", s)
    wards = list(_WARD_RE.finditer(s))
    if wards:
        s = s[wards[-1].end():]
    nums = _NUMRUN_RE.findall(s)
    num = re.sub(r"[-－]", "", nums[-1]) if nums else ""
    town = ""
    last = None
    for m in _CHO_RE.finditer(s):
        last = m
    if last:
        start = 0  # walk back to the previous digit (or string start)
        for j in range(last.start() - 1, -1, -1):
            if s[j].isdigit():
                start = j + 1
                break
        town = s[start:last.end()]
    return town, num


def addr_tokens_match(tb_addr: str, g_addr: str) -> bool:
    """True when both addresses carry the same 町/丁目 token (suffix match,
    >=3 chars, so Kyoto's street-prefix style still lines up) AND the same
    banchi digits. Two independent signals — strong enough to accept within a
    couple of km even when the names don't fuzzy-match."""
    t_town, t_num = addr_tail(tb_addr)
    g_town, g_num = addr_tail(g_addr)
    if not t_num or t_num != g_num:
        return False
    if not t_town or not g_town:
        return False
    short, long = sorted((t_town, g_town), key=len)
    return len(short) >= 3 and long.endswith(short)


def _slim_components(comps) -> list[dict]:
    return [{"t": c.get("longText") or c.get("shortText"),
             "types": c.get("types", [])} for c in (comps or [])]


def _blank_result() -> dict:
    return {k: "" for k in RESULT_KEYS}


# --- matching ----------------------------------------------------------------

def _composite(sim, dist, pref_match, city_match) -> float:
    dist_score = 1.0 if dist is None else max(0.0, 1 - dist / REVIEW_DIST_M)
    region_score = 0.5 * float(pref_match) + 0.5 * float(city_match)
    return 0.5 * sim + 0.3 * dist_score + 0.2 * region_score


def _candidate_fields(c: dict) -> dict:
    loc = c.get("location") or {}
    return {
        "place_id": c.get("id", ""),
        "g_name": (c.get("displayName") or {}).get("text", ""),
        "g_lat": loc.get("latitude", ""),
        "g_lon": loc.get("longitude", ""),
        "g_address_ja": c.get("formattedAddress", ""),
        "g_address_components_ja": json.dumps(
            _slim_components(c.get("addressComponents")), ensure_ascii=False),
        "g_types": "|".join(c.get("types", []) or []),
        "g_primary_type": c.get("primaryType", ""),
        "g_business_status": c.get("businessStatus", ""),
        "g_maps_url": c.get("googleMapsUri", ""),
        "g_plus_code": (c.get("plusCode") or {}).get("globalCode", ""),
    }


def evaluate(row: dict, candidates: list[dict]) -> dict:
    """Score the candidates against one Tabelog row and decide accepted /
    review / unmatched. Returns a partial result dict (merge over _blank_result)."""
    tb_name = row.get("name", "")
    tb_pref, tb_cities = parse_pref_city(row.get("address", ""))
    gsi_lat, gsi_lon = _to_float(row.get("lat")), _to_float(row.get("lon"))

    scored = []
    for c in candidates:
        loc = c.get("location") or {}
        clat, clon = loc.get("latitude"), loc.get("longitude")
        g_name = (c.get("displayName") or {}).get("text", "")
        g_pref, g_cities = google_pref_cities(c.get("addressComponents"))
        sim = name_similarity(tb_name, g_name)
        dist = (haversine_m(gsi_lat, gsi_lon, clat, clon)
                if None not in (gsi_lat, gsi_lon, clat, clon) else None)
        scored.append({
            "c": c, "sim": sim, "dist": dist, "g_pref": g_pref,
            "pref_match": pref_matches(tb_pref, g_pref),
            "city_match": city_matches(tb_cities, g_cities),
        })

    if not scored:
        return {"status": "unmatched", "confidence": 0.0,
                "notes": "no candidates returned", "candidate_count": 0}

    # Hard prefecture gate: drop candidates whose (known) prefecture disagrees.
    pool = [s for s in scored if (not tb_pref or not s["g_pref"] or s["pref_match"])]
    if not pool:
        top = max(scored, key=lambda s: s["sim"])
        return {"status": "unmatched", "confidence": 0.0,
                "candidate_count": len(candidates),
                "g_name": top["c"].get("displayName", {}).get("text", ""),
                "notes": f"all {len(scored)} candidate(s) in wrong prefecture "
                         f"(tabelog={tb_pref or '?'}, google={top['g_pref'] or '?'})"}

    for s in pool:
        s["conf"] = _composite(s["sim"], s["dist"], s["pref_match"], s["city_match"])
    best = max(pool, key=lambda s: s["conf"])

    sim, dist = best["sim"], best["dist"]
    pm, cm = best["pref_match"], best["city_match"]
    near = dist is not None and dist <= ACCEPT_DIST_M
    within_review = dist is None or dist <= REVIEW_DIST_M
    # Same 町 + banchi number on both sides — a strong location signal that
    # doesn't depend on the names fuzzy-matching.
    addr_match = addr_tokens_match(
        row.get("address", ""), best["c"].get("formattedAddress", "")
    )

    accept = (
        (pm and cm and near and sim >= ACCEPT_NAME_SIM)
        or (pm and sim >= STRONG_NAME_SIM
            and (dist is None or dist <= STRONG_NAME_DIST_M))
        # Within 100 m the pin is essentially on our address point — accept on
        # location alone (manual review of these came back 224/224 correct).
        or (pm and dist is not None and dist <= ACCEPT_NEAR_DIST_M)
        # Same town + banchi number within a couple of km — accept regardless
        # of name (handles 全角 digits, Kyoto street-prefix addresses, etc.).
        or (pm and dist is not None and dist <= ADDR_MATCH_DIST_M and addr_match)
    )
    # Never silently drop a strong candidate — route it to review (a human
    # confirms) when the name is a near-exact same-city match (a large offset
    # is then evidence GSI mis-geocoded, not that Google is wrong) or when the
    # pin is essentially coincident with ours.
    review = (not accept) and (
        (pm and within_review and sim >= REVIEW_NAME_SIM)
        or (pm and cm and sim >= REVIEW_STRONG_NAME_SIM)
        or (pm and cm and dist is not None and dist <= REVIEW_NEAR_DIST_M)
    )
    status = "accepted" if accept else ("review" if review else "unmatched")

    note = (f"sim={sim:.2f} dist={'NA' if dist is None else round(dist)}m "
            f"pref={pm} city={cm} addr={addr_match} cands={len(candidates)}")

    if status == "unmatched":
        # Keep the diagnostic name but no place_id => no en-address call, no pin.
        return {"status": status, "confidence": round(best["conf"], 3),
                "candidate_count": len(candidates),
                "g_name": _candidate_fields(best["c"])["g_name"],
                "notes": "below threshold: " + note}

    out = {
        "status": status, "confidence": round(best["conf"], 3),
        "candidate_count": len(candidates), "notes": note,
        "name_sim": round(sim, 3),
        "dist_m": "" if dist is None else round(dist),
        "pref_match": pm, "city_match": cm,
    }
    out.update(_candidate_fields(best["c"]))
    return out


# --- HTTP --------------------------------------------------------------------

def _request(client, method, url, *, headers=None, params=None, json_body=None,
             max_retries=4):
    """One request with backoff on transient failures. Returns parsed JSON, or
    None on a permanent 4xx or after exhausting retries."""
    for attempt in range(max_retries):
        try:
            resp = client.request(method, url, headers=headers, params=params,
                                   json=json_body, timeout=30.0)
        except Exception as e:
            wait = 2 ** attempt
            print(f"    network error ({e!r}); retry in {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            print(f"    HTTP {resp.status_code}; backoff {wait}s")
            time.sleep(wait)
            continue
        print(f"    HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    return None


def search_place(query, center, key, client) -> list[dict] | None:
    """Text Search (ja). Returns the candidate list, [] for a clean no-result,
    or None on API error (so the caller can mark the row 'error' and retry)."""
    body = {
        "textQuery": query,
        "languageCode": "ja",
        "regionCode": "JP",
        "maxResultCount": MAX_RESULTS,
    }
    if center:
        body["locationBias"] = {"circle": {
            "center": {"latitude": center[0], "longitude": center[1]},
            "radius": SEARCH_RADIUS_M,
        }}
    data = _request(client, "POST", SEARCH_URL, json_body=body, headers={
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": SEARCH_FIELD_MASK,
    })
    if data is None:
        return None
    return data.get("places", []) or []


def fetch_en_address(place_id, key, client) -> dict:
    data = _request(client, "GET", DETAILS_URL.format(place_id=place_id),
                    params={"languageCode": "en", "regionCode": "JP"}, headers={
                        "X-Goog-Api-Key": key,
                        "X-Goog-FieldMask": DETAILS_FIELD_MASK,
                    })
    return data or {}


def build_query(row: dict) -> str:
    return f"{(row.get('name') or '').strip()} {(row.get('address') or '').strip()}".strip()


def build_fallback_query(row: dict) -> str:
    name = (row.get("name") or "").strip()
    pref, cities = parse_pref_city(row.get("address") or "")
    loc = pref + (cities[0] if cities else "")
    return f"{name} {loc}".strip() if loc else ""


# --- ledger I/O --------------------------------------------------------------

def load_tabelog_rows(regions: list[str] | None = None) -> list[dict]:
    with open(TABELOG_CSV, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if regions:
        want = set(regions)
        rows = [r for r in rows if r.get("region") in want]
    return rows


def load_results() -> dict[str, dict]:
    if not GOOGLE_PLACES_CSV.exists():
        return {}
    with open(GOOGLE_PLACES_CSV, encoding="utf-8-sig", newline="") as f:
        return {r["detail_url"]: r for r in csv.DictReader(f)}


def write_results(results: dict[str, dict]) -> None:
    GOOGLE_PLACES_CSV.parent.mkdir(parents=True, exist_ok=True)
    order = {"accepted": 0, "review": 1, "unmatched": 2, "error": 3}
    rows = sorted(results.values(), key=lambda r: (
        r.get("region", ""), order.get(r.get("status", ""), 9), r.get("detail_url", "")))
    with open(GOOGLE_PLACES_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDNAMES})


def load_cache() -> dict:
    if GOOGLE_PLACES_CACHE.exists():
        try:
            return json.loads(GOOGLE_PLACES_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    GOOGLE_PLACES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    GOOGLE_PLACES_CACHE.write_text(json.dumps(cache, ensure_ascii=False),
                                   encoding="utf-8")


def make_meta(row: dict) -> dict:
    return {
        "detail_url": row.get("detail_url", ""),
        "region": row.get("region", ""),
        "tabelog_name": row.get("name", ""),
        "tabelog_address": row.get("address", ""),
        "gsi_lat": row.get("lat", ""),
        "gsi_lon": row.get("lon", ""),
        "fetched_at": _now_iso(),
        "batch": _now_month(),
    }


def _apply_en_address(res: dict, det: dict) -> None:
    res["g_address_en"] = det.get("formattedAddress", "")
    res["g_address_components_en"] = json.dumps(
        _slim_components(det.get("addressComponents")), ensure_ascii=False)


# --- run modes ---------------------------------------------------------------

def rescore() -> None:
    """Re-decide every cached restaurant against the current thresholds. No API
    calls — the English address already paid for is carried straight through."""
    cache = load_cache()
    rows_by_url = {r["detail_url"]: r for r in load_tabelog_rows()}
    results = load_results()
    n = 0
    for url, raw in cache.items():
        row = rows_by_url.get(url)
        if not row:
            continue
        # Re-scoring only re-opens the uncertain rows. 'accepted' can't get
        # worse under looser thresholds, and 'rejected' is a human verdict —
        # leave both as they are.
        if results.get(url, {}).get("status") in ("accepted", "rejected"):
            continue
        res = {**_blank_result(), **evaluate(row, raw.get("search") or [])}
        if res["status"] in ("accepted", "review"):
            _apply_en_address(res, raw.get("details_en") or {})
        merged = {**make_meta(row), **res}
        if url in results:  # preserve when it was actually fetched
            merged["fetched_at"] = results[url].get("fetched_at", merged["fetched_at"])
            merged["batch"] = results[url].get("batch", merged["batch"])
        results[url] = merged
        n += 1
    write_results(results)
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r.get("status", "")] = counts.get(r.get("status", ""), 0) + 1
    print(f"re-scored {n} cached restaurants (no API calls). totals: {counts}")
    print(f"output: {GOOGLE_PLACES_CSV}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Google Places enrichment for the Tabelog corpus (Phase 1).")
    ap.add_argument("--region", nargs="+", metavar="SLUG",
                    help="only process these region slug(s), "
                         "e.g. --region osaka hyogo okayama tottori")
    ap.add_argument("--limit", type=int, default=4800,
                    help="max NEW restaurants to process this run (monthly batch)")
    ap.add_argument("--qps", type=float, default=DEFAULT_QPS,
                    help="request throttle (requests/second)")
    ap.add_argument("--retry-unmatched", action="store_true",
                    help="also re-query rows previously left 'unmatched'")
    ap.add_argument("--rescore", action="store_true",
                    help="re-decide matches from the response cache; no API calls")
    ap.add_argument("--dry-run", action="store_true",
                    help="show which rows would be processed and exit")
    args = ap.parse_args()

    if args.rescore:
        rescore()
        return

    rows = load_tabelog_rows(args.region)
    results = load_results()
    cache = load_cache()

    # 'error' rows are never in done => they auto-retry on the next run.
    # --retry-unmatched re-opens 'unmatched' too, but never 'rejected'
    # (that's a human verdict) or 'accepted'/'review'.
    terminal = ("accepted", "review", "rejected") if args.retry_unmatched else TERMINAL
    done = {u for u, r in results.items() if r.get("status") in terminal}
    todo = [r for r in rows if r.get("detail_url") not in done][:args.limit]

    print(f"corpus: {len(rows)} rows"
          + (f" (regions={','.join(args.region)})" if args.region else "")
          + f" | already done: {len(done)} | this run: {len(todo)} (limit {args.limit})")
    if args.dry_run:
        for r in todo[:20]:
            print("  would do:", r.get("name"), "|", r.get("address"))
        if len(todo) > 20:
            print(f"  ... and {len(todo) - 20} more")
        return
    if not todo:
        print("nothing to do.")
        return

    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        print('ERROR: set GOOGLE_MAPS_API_KEY first  '
              '(PowerShell:  $env:GOOGLE_MAPS_API_KEY = "AIza...")')
        sys.exit(1)

    sleep_s = 1.0 / args.qps if args.qps > 0 else 0.0
    n_search = n_fallback = n_details = 0
    counts = {"accepted": 0, "review": 0, "unmatched": 0, "error": 0}

    with httpx.Client(headers={"User-Agent": "jpfoodmap-google-enrich/0.1"}) as client:
        for i, row in enumerate(todo, 1):
            url = row.get("detail_url", "")
            la, lo = _to_float(row.get("lat")), _to_float(row.get("lon"))
            center = (la, lo) if (la is not None and lo is not None) else None

            query = build_query(row)
            cands = search_place(query, center, key, client)
            n_search += 1
            time.sleep(sleep_s)

            if cands is None:
                res = {**_blank_result(), "status": "error",
                       "notes": "search API error"}
            else:
                if not cands and DO_FALLBACK_QUERY:
                    fq = build_fallback_query(row)
                    if fq and fq != query:
                        cands = search_place(fq, center, key, client) or []
                        n_search += 1
                        n_fallback += 1
                        time.sleep(sleep_s)
                res = {**_blank_result(), **evaluate(row, cands or [])}
                cache[url] = {"query": query, "search": cands or []}
                if res["status"] in ("accepted", "review") and res.get("place_id"):
                    det = fetch_en_address(res["place_id"], key, client)
                    n_details += 1
                    time.sleep(sleep_s)
                    if det:
                        _apply_en_address(res, det)
                        cache[url]["details_en"] = det

            counts[res["status"]] = counts.get(res["status"], 0) + 1
            results[url] = {**make_meta(row), **res}

            if i % CHECKPOINT_EVERY == 0:
                write_results(results)
                save_cache(cache)
                print(f"  [{i}/{len(todo)}] checkpoint — {counts}")

    write_results(results)
    save_cache(cache)
    worst = n_search * PRICE_SEARCH_PRO + n_details * PRICE_DETAILS_ESS
    print("\ndone.")
    print(f"  processed {len(todo)} | results: {counts}")
    print(f"  api calls: {n_search} text-search ({n_fallback} fallback) "
          f"+ {n_details} place-details")
    print(f"  cost: $0 while under this month's free caps "
          f"(5,000 search / 10,000 details).")
    print(f"        worst case if ALL were billable = ${worst:.2f}")
    print(f"  output: {GOOGLE_PLACES_CSV}")


if __name__ == "__main__":
    main()
