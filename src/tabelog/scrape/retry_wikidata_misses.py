"""Retry pass for bookmarks.json entries whose four translation fields
are still empty. Tuned for Wikipedia's rate limit:
  - Only ja.wikipedia (cross-language 429s share an IP bucket; querying
    en + zh too just burns the budget without adding hits — these are
    all Japanese place names anyway).
  - 2s base delay between entries.
  - On 429, sleep 60s and retry up to 3 times before giving up.

Run:  uv run python src/tabelog/scrape/retry_wikidata_misses.py
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
from opencc import OpenCC

from tabelog.paths import BOOKMARKS_JSON


USER_AGENT = "tabelog-personal-map/0.1 (Wikidata retry pass)"
BASE_DELAY_S = 2.0
TIMEOUT_S = 20.0
MAX_DRIFT_M = 5_000
THROTTLE_BACKOFF_S = 60.0
THROTTLE_MAX_RETRIES = 3

_s2t = OpenCC("s2t")
_t2s = OpenCC("t2s")


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000.0
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1); dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlam/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def get_with_throttle_backoff(client, url, params=None):
    """GET with automatic 429 backoff. Returns response or None on
    persistent failure."""
    for attempt in range(THROTTLE_MAX_RETRIES):
        r = client.get(url, params=params, timeout=TIMEOUT_S)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", THROTTLE_BACKOFF_S))
            print(f"      .. throttled (attempt {attempt + 1}/"
                  f"{THROTTLE_MAX_RETRIES}); sleeping {wait:.0f}s")
            time.sleep(wait)
            continue
        return r
    return None


def wikipedia_to_qid(client, title):
    """ja.wikipedia title -> Wikidata Q-ID (and coords if any)."""
    url = "https://ja.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "titles": title,
        "prop": "pageprops|coordinates",
        "redirects": "1",
        "formatversion": "2",
        "format": "json",
    }
    r = get_with_throttle_backoff(client, url, params)
    if r is None or r.status_code != 200:
        return None
    j = r.json()
    if "error" in j:
        print(f"      !! API error: {j['error']}")
        return None
    pages = j.get("query", {}).get("pages") or []
    if not pages:
        return None
    page = pages[0]
    if page.get("missing"):
        return None
    pp = page.get("pageprops") or {}
    if "disambiguation" in pp:
        print("      .. disambiguation page")
        return None
    qid = pp.get("wikibase_item")
    if not qid:
        return None
    coords = page.get("coordinates") or []
    if coords:
        return qid, coords[0].get("lat"), coords[0].get("lon")
    return qid, None, None


def wikidata_labels(client, qid):
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    r = get_with_throttle_backoff(client, url)
    if r is None or r.status_code != 200:
        return {}
    j = r.json()
    entity = (j.get("entities") or {}).get(qid) or {}
    raw = entity.get("labels") or {}
    return {lang: (info.get("value") or "") for lang, info in raw.items()}


def pick_chinese(labels):
    sc = labels.get("zh-hans") or labels.get("zh-cn") or ""
    tc = (labels.get("zh-hant") or labels.get("zh-tw")
          or labels.get("zh-hk") or "")
    if not sc and tc:
        sc = _t2s.convert(tc)
    if not tc and sc:
        tc = _s2t.convert(sc)
    if not sc and not tc:
        zh = labels.get("zh") or ""
        if zh:
            sc = _t2s.convert(zh)
            tc = _s2t.convert(sc)
    return sc, tc


def main():
    entries = json.loads(BOOKMARKS_JSON.read_text(encoding="utf-8"))

    targets = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict): continue
        if e.get("category") != "attraction": continue
        if "name_src" not in e: continue
        if any(e.get(f) for f in ("name_sc", "name_tc", "name_jp", "name_en")):
            continue
        targets.append((i, e))

    print(f"[retry-v2] {len(targets)} still-empty entries")
    print(f"[retry-v2] ~{len(targets) * BASE_DELAY_S * 2:.0f}s under "
          f"clean conditions; longer if throttled")
    print()

    stats = {"hit": 0, "miss": 0, "drift_reject": 0}
    still_misses = []

    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for n, (idx, e) in enumerate(targets, start=1):
            name = e["name_src"].strip()
            prefix = f"  [{n:3d}/{len(targets)}] {name}"

            try:
                hit = wikipedia_to_qid(client, name)
            except Exception as exc:   # noqa: BLE001
                print(f"{prefix}  ERR: {exc}")
                still_misses.append((idx, name))
                time.sleep(BASE_DELAY_S)
                continue

            if not hit:
                print(f"{prefix}  MISS")
                stats["miss"] += 1
                still_misses.append((idx, name))
                time.sleep(BASE_DELAY_S)
                continue

            qid, hit_lat, hit_lon = hit
            if hit_lat is not None and hit_lon is not None:
                drift = haversine_m(float(e["lat"]), float(e["lon"]),
                                    hit_lat, hit_lon)
                if drift > MAX_DRIFT_M:
                    print(f"{prefix}  REJECT {qid} (drift {drift:.0f}m)")
                    stats["drift_reject"] += 1
                    still_misses.append((idx, name))
                    time.sleep(BASE_DELAY_S)
                    continue

            time.sleep(BASE_DELAY_S)
            labels = wikidata_labels(client, qid)
            if not labels:
                print(f"{prefix}  ERR (empty labels for {qid})")
                still_misses.append((idx, name))
                time.sleep(BASE_DELAY_S)
                continue

            sc, tc = pick_chinese(labels)
            jp = labels.get("ja") or ""
            en = labels.get("en") or ""

            e["name_sc"] = sc
            e["name_tc"] = tc
            e["name_jp"] = jp
            e["name_en"] = en

            print(f"{prefix}  HIT {qid}")
            print(f"      sc={sc!r} tc={tc!r} jp={jp!r} en={en!r}")
            stats["hit"] += 1
            time.sleep(BASE_DELAY_S)

    BOOKMARKS_JSON.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print()
    print(f"[retry-v2] stats: {stats}")
    if still_misses:
        print(f"[retry-v2] {len(still_misses)} entries still need LLM fill:")
        for idx, name in still_misses:
            print(f"  - [#{idx}] {name}")


if __name__ == "__main__":
    main()
