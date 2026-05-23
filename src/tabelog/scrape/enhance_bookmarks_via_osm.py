"""One-shot upgrade pass: walk every category=attraction entry in
data/user/bookmarks.json, reverse-geocode by (lat, lon) against OSM
Nominatim, and overlay multilingual names from OSM's namedetails. Where
OSM has a value, we trust it over the existing MT/curated value (OSM
tags are community-curated by humans; MT often produces literal
common-noun translations for proper names — "新世界 -> new world").
Fields not covered by OSM keep whatever they had before.

Strategy per field:
  name_en  ← namedetails['name:en']  or extratags['int_name']
  name_sc  ← namedetails['name:zh-Hans'] or 'name:zh-CN' or 'name:zh'
            (only when the candidate is plausibly Simplified; the bare
             'name:zh' tag is often Traditional in Japan / Taiwan)
  name_tc  ← namedetails['name:zh-Hant'] or 'name:zh-TW' or 'name:zh-HK'
  name_jp  ← namedetails['name:ja']   or top-level 'name' (since the
            place is in Japan, the primary OSM name is JP)
  name_src ← never touched (it's the user's / migration's original input)

A reverse-geocode hit is only accepted if the resolved point sits
within ~250m of the input coord and the OSM JP name shares at least
one character with the existing name_jp — guards against hitting a
nearby road instead of the landmark itself.

Run:  uv run python src/tabelog/scrape/enhance_bookmarks_via_osm.py
"""

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx

from tabelog.paths import BOOKMARKS_JSON


NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "tabelog-personal-map/0.1 (one-shot bookmark name enhancement)"
PER_CALL_DELAY_S = 1.1   # Nominatim asks for ≤1 req/s
TIMEOUT_S = 15.0
MAX_DRIFT_M = 250        # reject hits this far from the input coord


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def reverse_geocode(client: httpx.Client, lat: float, lon: float) -> dict | None:
    params = {
        "format": "jsonv2",
        "lat": f"{lat:.7f}",
        "lon": f"{lon:.7f}",
        "zoom": "18",
        "accept-language": "zh,en,ja",
        "namedetails": "1",
        "extratags": "1",
    }
    r = client.get(NOMINATIM, params=params, timeout=TIMEOUT_S)
    if r.status_code != 200:
        return None
    j = r.json()
    if not isinstance(j, dict) or "lat" not in j:
        return None
    return j


def pick_zh_simplified(nd: dict) -> str:
    """Return a name that's likely Simplified, falling back through
    Hans/CN/HK in order. The bare 'name:zh' tag is ambiguous (often
    Traditional in Japan), so it goes last and we accept whatever it
    has rather than guess."""
    for key in ("name:zh-Hans", "name:zh-CN"):
        v = (nd.get(key) or "").strip()
        if v:
            return v
    return (nd.get("name:zh") or "").strip()


def pick_zh_traditional(nd: dict) -> str:
    for key in ("name:zh-Hant", "name:zh-TW", "name:zh-HK"):
        v = (nd.get(key) or "").strip()
        if v:
            return v
    return ""


def main() -> None:
    if not BOOKMARKS_JSON.exists():
        raise SystemExit(f"missing {BOOKMARKS_JSON}")
    entries: list[dict] = json.loads(BOOKMARKS_JSON.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise SystemExit("bookmarks.json is not a JSON array")

    targets = [(i, e) for i, e in enumerate(entries)
               if isinstance(e, dict) and e.get("category") == "attraction"
               and "name_src" in e]   # skip legacy `name`-only entries
    print(f"[osm] {len(targets)} attraction entries to enhance "
          f"(of {len(entries)} total in bookmarks.json)")
    print(f"[osm] rate-limited to ~1 req/s; expect ~{len(targets)} seconds")
    print()

    stats = {"hit": 0, "skipped_drift": 0, "skipped_mismatch": 0,
             "skipped_noname": 0, "no_change": 0, "err": 0}
    field_updates = {"name_en": 0, "name_sc": 0, "name_tc": 0, "name_jp": 0}

    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers) as client:
        for n, (idx, e) in enumerate(targets, start=1):
            lat, lon = float(e["lat"]), float(e["lon"])
            label = e.get("name_jp") or e.get("name_src") or "?"
            prefix = f"  [{n:3d}/{len(targets)}] {label}"
            try:
                j = reverse_geocode(client, lat, lon)
            except Exception as exc:   # noqa: BLE001
                print(f"{prefix}  ERROR {exc}")
                stats["err"] += 1
                time.sleep(PER_CALL_DELAY_S)
                continue
            if not j:
                print(f"{prefix}  no result")
                stats["err"] += 1
                time.sleep(PER_CALL_DELAY_S)
                continue

            try:
                hit_lat = float(j.get("lat"))
                hit_lon = float(j.get("lon"))
            except (TypeError, ValueError):
                hit_lat = hit_lon = None
            if hit_lat is None:
                print(f"{prefix}  hit has no coords")
                stats["err"] += 1
                time.sleep(PER_CALL_DELAY_S)
                continue

            drift = haversine_m(lat, lon, hit_lat, hit_lon)
            if drift > MAX_DRIFT_M:
                print(f"{prefix}  SKIP (drift {drift:.0f}m > {MAX_DRIFT_M}m)")
                stats["skipped_drift"] += 1
                time.sleep(PER_CALL_DELAY_S)
                continue

            nd = j.get("namedetails") or {}
            extra = j.get("extratags") or {}
            osm_ja = (nd.get("name:ja") or j.get("name") or "").strip()
            if not osm_ja:
                print(f"{prefix}  SKIP (no name on OSM hit)")
                stats["skipped_noname"] += 1
                time.sleep(PER_CALL_DELAY_S)
                continue

            # Sanity check: OSM JP name should share at least one CJK
            # ideograph with our existing name_jp. Pure-kana places are
            # tricky (アメリカ村 vs American village) so we relax to "or
            # length-2 substring either direction" for those.
            cur_jp = (e.get("name_jp") or "").strip()
            if cur_jp:
                shared = set(cur_jp) & set(osm_ja)
                substr = (len(cur_jp) >= 2 and len(osm_ja) >= 2 and
                          (cur_jp[:2] in osm_ja or osm_ja[:2] in cur_jp))
                if not (shared or substr):
                    print(f"{prefix}  SKIP (name mismatch: "
                          f"existing={cur_jp!r} osm={osm_ja!r})")
                    stats["skipped_mismatch"] += 1
                    time.sleep(PER_CALL_DELAY_S)
                    continue

            osm_en = (nd.get("name:en") or extra.get("int_name") or "").strip()
            osm_sc = pick_zh_simplified(nd)
            osm_tc = pick_zh_traditional(nd)

            updates: list[tuple[str, str, str]] = []   # (field, before, after)
            for field, new_val in (("name_en", osm_en),
                                   ("name_sc", osm_sc),
                                   ("name_tc", osm_tc),
                                   ("name_jp", osm_ja)):
                if not new_val:
                    continue
                old_val = (e.get(field) or "").strip()
                if old_val == new_val:
                    continue
                updates.append((field, old_val, new_val))

            if not updates:
                print(f"{prefix}  ok (no field improved)")
                stats["no_change"] += 1
                stats["hit"] += 1
                time.sleep(PER_CALL_DELAY_S)
                continue

            print(f"{prefix}  HIT")
            for field, old, new in updates:
                print(f"      {field}: {old!r}  ->  {new!r}")
                e[field] = new
                field_updates[field] += 1
            entries[idx] = e
            stats["hit"] += 1

            time.sleep(PER_CALL_DELAY_S)

    BOOKMARKS_JSON.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"[osm] wrote {len(entries)} entries back to {BOOKMARKS_JSON}")
    print(f"[osm] summary: {stats}")
    print(f"[osm] field updates: {field_updates}")


if __name__ == "__main__":
    main()
