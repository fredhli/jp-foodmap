"""One-shot rescue: re-geocode the 14 known cross-prefecture errors in
favorites_builtin.json identified by audit_favorites_builtin_coords.py.

Per entry:
  1. Skip if the existing coord already lands in the expected prefecture
     (so re-runs after a partial success are safe).
  2. Retry Nominatim with "{prefecture_jp} {name_src}" — the prefix
     disambiguates same-named places elsewhere in Japan (and abroad).
     Reverse-check each candidate; take the first one inside the
     expected prefecture.
  3. Fall back to ja-wiki -> Wikidata P625 (coordinate location), also
     reverse-checked.
  4. If both miss, leave the entry untouched and log for hand-fix.

The rescue list is hardcoded — this is the audit-driven follow-up, not a
general tool. Re-running is safe.

Run:  uv run python src/tabelog/scrape/rescue_misplaced_favorites.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx

from tabelog.paths import FAVORITES_BUILTIN_JSON


USER_AGENT = "tabelog-personal-map/0.1 (favorites_builtin rescue)"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REV = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_DELAY_S = 1.1
WIKI_DELAY_S = 0.5
TIMEOUT_S = 20.0

# Must mirror audit_favorites_builtin_coords.REGION_TO_PREF
REGION_TO_PREF = {
    "hokkaido":  "北海道",   "aomori":    "青森県",  "iwate":     "岩手県",
    "miyagi":    "宮城県",   "akita":     "秋田県",  "yamagata":  "山形県",
    "fukushima": "福島県",   "ibaraki":   "茨城県",  "tochigi":   "栃木県",
    "gunma":     "群馬県",   "saitama":   "埼玉県",  "chiba":     "千葉県",
    "tokyo":     "東京都",   "kanagawa":  "神奈川県", "niigata":   "新潟県",
    "toyama":    "富山県",   "ishikawa":  "石川県",  "fukui":     "福井県",
    "yamanashi": "山梨県",   "nagano":    "長野県",  "gifu":      "岐阜県",
    "shizuoka":  "静岡県",   "aichi":     "愛知県",  "mie":       "三重県",
    "shiga":     "滋賀県",   "kyoto":     "京都府",  "osaka":     "大阪府",
    "hyogo":     "兵庫県",   "nara":      "奈良県",  "wakayama":  "和歌山県",
    "tottori":   "鳥取県",   "shimane":   "島根県",  "okayama":   "岡山県",
    "hiroshima": "広島県",   "yamaguchi": "山口県",  "tokushima": "徳島県",
    "kagawa":    "香川県",   "ehime":     "愛媛県",  "kochi":     "高知県",
    "fukuoka":   "福岡県",   "saga":      "佐賀県",  "nagasaki":  "長崎県",
    "kumamoto":  "熊本県",   "oita":      "大分県",  "miyazaki":  "宮崎県",
    "kagoshima": "鹿児島県", "okinawa":   "沖縄県",
}

# Cross-prefecture errors from the audit run on 2026-05-23.
# Second batch: the "(none)" cases turned out to be silently hitting Tokyo
# (zoom=10 reverse for 東京 23-ward addresses returns city only, with no
# state field — the audit's matcher missed them on the first pass).
RESCUE_IDS = [
    "fb-hokkaido-sapporo-tokeidai",   # actual: 栃木県
    "fb-kanagawa-hakone-onsen",       # actual: 島根県
    "fb-aichi-osu-shotengai",         # actual: 大阪府
    "fb-mie-ise-jingu",               # actual: 高知県
    "fb-mie-meoto-iwa",               # actual: 福井県
    "fb-mie-kumano-kodo",             # actual: 大阪府
    "fb-wakayama-nachi-falls",        # actual: 栃木県
    "fb-ishikawa-omicho-market",      # actual: 滋賀県
    "fb-iwate-ryusendo",              # actual: 吉林省 (CN)
    "fb-akita-oga-peninsula",         # actual: 全北 (KR)
    "fb-yamagata-yamadera",           # actual: 長野県
    "fb-fukushima-tsurugajo",         # actual: 宮城県
    "fb-shimane-iwami-ginzan",        # actual: 鳥取県
    "fb-kumamoto-kusasenri",          # actual: 黒竜江省 (CN)
    "fb-kanagawa-komachi-dori",       # silent Tokyo hit (江東区)
    "fb-okinawa-kokusai-dori",        # silent Tokyo hit (台東区)
    "fb-fukui-tojinbo",               # silent Tokyo hit (台東区)
    "fb-yamagata-zao-juhyo",          # silent Tokyo hit (文京区)
    "fb-okayama-korakuen",            # silent Tokyo hit (文京区, name collision)
]


def nominatim_search(client: httpx.Client, q: str, limit: int = 5
                    ) -> list[tuple[float, float]]:
    """Japan-bounded search. Returns up to `limit` (lat, lon) candidates."""
    params = {
        "format": "jsonv2",
        "limit": str(limit),
        "addressdetails": "0",
        "viewbox": "122,46,154,24",
        "bounded": "1",
        "accept-language": "ja",
        "q": q,
    }
    r = client.get(NOMINATIM_SEARCH, params=params, timeout=TIMEOUT_S)
    if r.status_code != 200:
        return []
    arr = r.json()
    if not isinstance(arr, list):
        return []
    out: list[tuple[float, float]] = []
    for it in arr:
        try:
            out.append((float(it["lat"]), float(it["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def reverse_pref_check(client: httpx.Client, lat: float, lon: float,
                       expected_pref: str) -> bool:
    """True if (lat, lon) reverse-geocodes inside the expected prefecture."""
    params = {
        "format": "jsonv2",
        "lat": f"{lat:.7f}",
        "lon": f"{lon:.7f}",
        "zoom": "10",
        "accept-language": "ja",
        "addressdetails": "1",
    }
    r = client.get(NOMINATIM_REV, params=params, timeout=TIMEOUT_S)
    if r.status_code != 200:
        return False
    j = r.json()
    if not isinstance(j, dict):
        return False
    addr = j.get("address") or {}
    actual = (addr.get("state") or "").strip()
    display = (j.get("display_name") or "").strip()
    return actual == expected_pref or expected_pref in display


def wiki_to_qid(client: httpx.Client, title: str, lang: str = "ja"
               ) -> str | None:
    url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {
        "action": "query", "titles": title, "prop": "pageprops",
        "redirects": "1", "formatversion": "2", "format": "json",
    }
    r = client.get(url, params=params, timeout=TIMEOUT_S)
    if r.status_code != 200:
        return None
    pages = (r.json().get("query") or {}).get("pages") or []
    if not pages:
        return None
    page = pages[0]
    if page.get("missing"):
        return None
    pp = page.get("pageprops") or {}
    if "disambiguation" in pp:
        return None
    return pp.get("wikibase_item")


def wikidata_coord(client: httpx.Client, qid: str
                  ) -> tuple[float, float] | None:
    """P625 = coordinate location. Returns (lat, lon) or None."""
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    r = client.get(url, timeout=TIMEOUT_S)
    if r.status_code != 200:
        return None
    j = r.json()
    entity = ((j.get("entities") or {}).get(qid) or {})
    claims = (entity.get("claims") or {}).get("P625") or []
    if not claims:
        return None
    value = (((claims[0].get("mainsnak") or {}).get("datavalue") or {})
             .get("value") or {})
    try:
        return float(value["latitude"]), float(value["longitude"])
    except (KeyError, TypeError, ValueError):
        return None


def save(entries: list[dict]) -> None:
    FAVORITES_BUILTIN_JSON.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    entries: list[dict] = json.loads(
        FAVORITES_BUILTIN_JSON.read_text(encoding="utf-8"))
    by_id = {e.get("id"): (i, e) for i, e in enumerate(entries)
             if isinstance(e, dict)}

    stats = {"nominatim_fix": 0, "wikidata_fix": 0,
             "still_broken": 0, "skipped_clean": 0, "missing": 0}
    headers = {"User-Agent": USER_AGENT}

    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for n, eid in enumerate(RESCUE_IDS, start=1):
            if eid not in by_id:
                print(f"  [{n:2d}/{len(RESCUE_IDS)}] {eid}: NOT FOUND")
                stats["missing"] += 1
                continue
            idx, e = by_id[eid]
            region = eid.split("-", 2)[1]
            expected = REGION_TO_PREF[region]
            name = (e.get("name_src") or "?").strip()
            cur_lat = float(e.get("lat", 0))
            cur_lon = float(e.get("lon", 0))
            prefix = f"  [{n:2d}/{len(RESCUE_IDS)}] {eid}  ({name}, want {expected})"

            # Skip if current coord already lands in the expected prefecture
            # (idempotent re-runs).
            if reverse_pref_check(client, cur_lat, cur_lon, expected):
                print(f"{prefix}  SKIP (already correct)")
                stats["skipped_clean"] += 1
                time.sleep(NOMINATIM_DELAY_S)
                continue
            time.sleep(NOMINATIM_DELAY_S)

            # Step 1: Nominatim with prefecture-prefix disambiguator.
            new_coord = None
            q = f"{expected} {name}"
            candidates = nominatim_search(client, q, limit=5)
            time.sleep(NOMINATIM_DELAY_S)
            for (cl, co) in candidates:
                if reverse_pref_check(client, cl, co, expected):
                    new_coord = (cl, co)
                    break
                time.sleep(NOMINATIM_DELAY_S)

            if new_coord:
                e["lat"], e["lon"] = new_coord
                entries[idx] = e
                save(entries)
                print(f"{prefix}  NOM HIT  "
                      f"({cur_lat:.5f},{cur_lon:.5f}) -> "
                      f"({new_coord[0]:.5f},{new_coord[1]:.5f})")
                stats["nominatim_fix"] += 1
                continue

            # Step 2: Wikidata P625 fallback.
            qid = wiki_to_qid(client, name, "ja")
            time.sleep(WIKI_DELAY_S)
            if not qid:
                print(f"{prefix}  STILL BROKEN — wiki no qid for {name!r}")
                stats["still_broken"] += 1
                continue

            wd = wikidata_coord(client, qid)
            time.sleep(WIKI_DELAY_S)
            if not wd:
                print(f"{prefix}  STILL BROKEN — {qid} has no P625")
                stats["still_broken"] += 1
                continue

            if reverse_pref_check(client, wd[0], wd[1], expected):
                e["lat"], e["lon"] = wd
                entries[idx] = e
                save(entries)
                print(f"{prefix}  WIKI HIT {qid}  "
                      f"({cur_lat:.5f},{cur_lon:.5f}) -> "
                      f"({wd[0]:.5f},{wd[1]:.5f})")
                stats["wikidata_fix"] += 1
                time.sleep(NOMINATIM_DELAY_S)
                continue
            time.sleep(NOMINATIM_DELAY_S)

            print(f"{prefix}  STILL BROKEN — "
                  f"wiki coord {wd} also not in {expected}")
            stats["still_broken"] += 1

    print()
    print(f"[rescue] stats: {stats}")


if __name__ == "__main__":
    main()
