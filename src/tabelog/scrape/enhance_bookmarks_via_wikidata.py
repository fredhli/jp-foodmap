"""One-shot quality pass: for every category=attraction entry in
data/user/bookmarks.json, look the place up on Wikidata via Wikipedia
and overwrite name_sc / name_tc / name_jp / name_en with the canonical
multilingual labels (community-curated, far better than MT).

Aggressive mode: clear all four name_* fields up-front, then refill
only what Wikidata returns. Misses leave the four fields empty — that's
intentional; the user is going to ask an LLM to hand-fill those next.
name_src is preserved (it's the original input / migration source).

Pipeline per entry:
  1. Resolve name_src to a Wikidata Q-ID via Wikipedia pageprops
     (try ja.wikipedia first, then en + zh as fallbacks). Disambig pages
     are rejected.
  2. Optional sanity check: if the Wikidata entity has a coordinate
     statement (P625), reject the hit if it's > MAX_DRIFT_M from the
     pin. Guards against e.g. "新世界" hitting the wrong article.
  3. Pull labels from Wikidata. For Chinese, prefer zh-hans / zh-hant
     directly; fall back to OpenCC s2t / t2s if only one variant is
     given.
  4. Write back. Empty fields are honest — display falls back to
     name_src via bmDisplayName.

Run:  uv run python src/tabelog/scrape/enhance_bookmarks_via_wikidata.py
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


USER_AGENT = "tabelog-personal-map/0.1 (Wikidata bookmark enhancement)"
PER_CALL_DELAY_S = 0.3   # well under Wikimedia's 200/min unauth limit
TIMEOUT_S = 15.0
MAX_DRIFT_M = 5_000      # 5km: Wikidata coords are sometimes city-centroid

WIKI_LANGS = ("ja", "en", "zh")   # try in this order

_s2t = OpenCC("s2t")
_t2s = OpenCC("t2s")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def wikipedia_to_qid(client: httpx.Client, title: str, lang: str
                    ) -> tuple[str, float | None, float | None] | None:
    """Look up `title` on <lang>.wikipedia.org. Returns (qid, lat, lon)
    where lat/lon are the page's coords if tagged. None on miss /
    disambiguation / not linked to Wikidata."""
    url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "titles": title,
        "prop": "pageprops|coordinates",
        "redirects": "1",
        "formatversion": "2",
        "format": "json",
    }
    r = client.get(url, params=params, timeout=TIMEOUT_S)
    if r.status_code != 200:
        return None
    j = r.json()
    pages = j.get("query", {}).get("pages") or []
    if not pages:
        return None
    page = pages[0]
    if page.get("missing"):
        return None
    pp = page.get("pageprops") or {}
    if "disambiguation" in pp:
        return None
    qid = pp.get("wikibase_item")
    if not qid:
        return None
    coords = page.get("coordinates") or []
    if coords:
        c = coords[0]
        return qid, c.get("lat"), c.get("lon")
    return qid, None, None


def wikidata_labels(client: httpx.Client, qid: str) -> dict[str, str]:
    """Fetch all language labels for a Wikidata entity."""
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    r = client.get(url, timeout=TIMEOUT_S)
    if r.status_code != 200:
        return {}
    j = r.json()
    entity = (j.get("entities") or {}).get(qid) or {}
    raw = entity.get("labels") or {}
    return {lang: (info.get("value") or "") for lang, info in raw.items()}


def pick_chinese(labels: dict[str, str]) -> tuple[str, str]:
    """Return (sc, tc). Prefer explicit hans/hant; otherwise derive the
    missing variant from the present one via OpenCC. If both missing,
    fall back through generic 'zh' as a last guess."""
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


def main() -> None:
    if not BOOKMARKS_JSON.exists():
        raise SystemExit(f"missing {BOOKMARKS_JSON}")
    entries: list[dict] = json.loads(BOOKMARKS_JSON.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise SystemExit("bookmarks.json is not a JSON array")

    targets = [(i, e) for i, e in enumerate(entries)
               if isinstance(e, dict) and e.get("category") == "attraction"
               and "name_src" in e]
    print(f"[wikidata] {len(targets)} attraction entries to enhance "
          f"(of {len(entries)} total)")
    print(f"[wikidata] expect ~{len(targets) * 0.6:.0f}s "
          f"with {PER_CALL_DELAY_S}s gap")
    print()

    stats = {"hit": 0, "miss_no_article": 0, "miss_drift": 0, "err": 0}
    misses: list[tuple[int, str]] = []

    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for n, (idx, e) in enumerate(targets, start=1):
            name = (e.get("name_src") or "").strip()
            prefix = f"  [{n:3d}/{len(targets)}] {name}"
            if not name:
                print(f"{prefix}  SKIP (empty name_src)")
                stats["err"] += 1
                continue

            # Wipe the four translation fields up-front. Wikidata refills
            # what it can; misses keep them blank for LLM follow-up.
            for f in ("name_sc", "name_tc", "name_jp", "name_en"):
                e[f] = ""

            # Step 1: find the Wikidata Q-ID via a Wikipedia title lookup.
            hit = None
            for lang in WIKI_LANGS:
                try:
                    hit = wikipedia_to_qid(client, name, lang)
                except Exception as exc:   # noqa: BLE001
                    print(f"{prefix}  warn ({lang} wiki): {exc}")
                    time.sleep(PER_CALL_DELAY_S)
                    continue
                if hit:
                    break
                time.sleep(PER_CALL_DELAY_S)

            if not hit:
                print(f"{prefix}  MISS")
                stats["miss_no_article"] += 1
                misses.append((idx, name))
                continue

            qid, hit_lat, hit_lon = hit
            if hit_lat is not None and hit_lon is not None:
                drift = haversine_m(float(e["lat"]), float(e["lon"]),
                                    hit_lat, hit_lon)
                if drift > MAX_DRIFT_M:
                    print(f"{prefix}  REJECT {qid} (drift {drift:.0f}m)")
                    stats["miss_drift"] += 1
                    misses.append((idx, name))
                    continue

            # Step 2: pull labels.
            try:
                labels = wikidata_labels(client, qid)
            except Exception as exc:   # noqa: BLE001
                print(f"{prefix}  ERR fetching {qid}: {exc}")
                stats["err"] += 1
                misses.append((idx, name))
                continue
            time.sleep(PER_CALL_DELAY_S)

            sc, tc = pick_chinese(labels)
            jp = labels.get("ja") or ""
            en = labels.get("en") or ""

            e["name_sc"] = sc
            e["name_tc"] = tc
            e["name_jp"] = jp
            e["name_en"] = en

            print(f"{prefix}  HIT {qid}")
            print(f"      sc={sc!r}  tc={tc!r}  jp={jp!r}  en={en!r}")
            stats["hit"] += 1

    BOOKMARKS_JSON.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print()
    print(f"[wikidata] stats: {stats}")
    print(f"[wikidata] hit rate: "
          f"{stats['hit'] / len(targets) * 100:.0f}%")
    print(f"[wikidata] wrote {len(entries)} entries back to {BOOKMARKS_JSON}")
    if misses:
        print()
        print(f"[wikidata] {len(misses)} entries with all-empty translation "
              f"fields (need LLM fill):")
        for idx, name in misses:
            print(f"  - [#{idx}] {name}")


if __name__ == "__main__":
    main()
