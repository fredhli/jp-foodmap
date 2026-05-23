"""Fill name_sc / name_tc / name_jp / name_en + lat / lon for entries in
data/favorites_builtin.json that the curator only gave name_src + emoji.

Per entry:
  - name_jp ← name_src                  (curator only ships Japanese names)
  - name_sc / name_en ← Wikidata labels via the ja-wiki → Q-ID lookup
                        (same pattern as enhance_bookmarks_via_wikidata.py)
  - name_tc ← OpenCC s2t over name_sc
  - lat / lon ← Nominatim search, Japan-bounded, mirroring the URL the
                in-page search box uses

Idempotent: an entry with every field populated is skipped. Translation
and geocoding are independent — a Wikidata miss still attempts geocoding,
and vice versa. The file is rewritten after every entry so a mid-run
crash doesn't lose progress.

Run:  uv run python src/tabelog/scrape/enrich_favorites_builtin.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
from opencc import OpenCC

from tabelog.paths import FAVORITES_BUILTIN_JSON


USER_AGENT = "tabelog-personal-map/0.1 (favorites_builtin enrichment)"
WIKI_DELAY_S = 0.5        # well under Wikimedia's 200/min unauth limit
NOMINATIM_DELAY_S = 1.1   # OSM Nominatim asks for <=1 req/s
TIMEOUT_S = 20.0
WIKI_RETRIES = 3          # for 429 / 5xx / transient network failures
WIKI_LANGS = ("ja", "en", "zh")
NOMINATIM = "https://nominatim.openstreetmap.org/search"

KEY_ORDER = ["id", "name_src", "name_sc", "name_tc", "name_jp", "name_en",
             "emoji", "lat", "lon", "category"]

_s2t = OpenCC("s2t")
_t2s = OpenCC("t2s")


def _get_with_retry(client: httpx.Client, url: str,
                    params: dict | None = None,
                    tag: str = "") -> httpx.Response | None:
    """GET with retry on 429 / 5xx / network errors. Exponential backoff,
    honors Retry-After header when present. Returns the final response on
    success, or None after all retries exhausted (and prints why)."""
    backoff = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, WIKI_RETRIES + 1):
        try:
            r = client.get(url, params=params, timeout=TIMEOUT_S)
        except Exception as exc:   # noqa: BLE001
            last_exc = exc
            print(f"      {tag} attempt {attempt} network err: "
                  f"{type(exc).__name__}: {exc}")
            time.sleep(backoff)
            backoff *= 2
            continue
        if r.status_code == 200:
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            ra = r.headers.get("retry-after")
            wait = float(ra) if (ra and ra.replace(".", "", 1).isdigit()) \
                else backoff
            print(f"      {tag} attempt {attempt} HTTP {r.status_code}, "
                  f"sleeping {wait:.1f}s")
            time.sleep(wait)
            backoff *= 2
            continue
        # 4xx other than 429: not retryable
        print(f"      {tag} HTTP {r.status_code} (not retried)")
        return None
    if last_exc:
        print(f"      {tag} gave up after {WIKI_RETRIES} attempts: {last_exc}")
    else:
        print(f"      {tag} gave up after {WIKI_RETRIES} attempts (5xx/429)")
    return None


def wiki_to_qid(client: httpx.Client, title: str, lang: str) -> str | None:
    url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "titles": title,
        "prop": "pageprops",
        "redirects": "1",
        "formatversion": "2",
        "format": "json",
    }
    r = _get_with_retry(client, url, params, tag=f"wiki[{lang}]")
    if r is None:
        return None
    try:
        j = r.json()
    except Exception as exc:   # noqa: BLE001
        print(f"      wiki[{lang}] non-JSON body: {exc}")
        return None
    pages = (j.get("query") or {}).get("pages") or []
    if not pages:
        return None
    page = pages[0]
    if page.get("missing"):
        return None
    pp = page.get("pageprops") or {}
    if "disambiguation" in pp:
        return None
    return pp.get("wikibase_item")


def wikidata_labels(client: httpx.Client, qid: str) -> dict[str, str]:
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    r = _get_with_retry(client, url, tag=f"wd[{qid}]")
    if r is None:
        return {}
    try:
        j = r.json()
    except Exception as exc:   # noqa: BLE001
        print(f"      wd[{qid}] non-JSON body: {exc}")
        return {}
    entity = ((j.get("entities") or {}).get(qid) or {})
    raw = entity.get("labels") or {}
    return {lang: (info.get("value") or "") for lang, info in raw.items()}


def pick_sc(labels: dict[str, str]) -> str:
    for k in ("zh-hans", "zh-cn", "zh-sg", "zh-my"):
        v = labels.get(k)
        if v:
            return v
    # Bare 'zh' is often Traditional in Japan-tagged articles -> convert.
    zh = labels.get("zh")
    if zh:
        try:
            return _t2s.convert(zh)
        except Exception:   # noqa: BLE001
            return zh
    return ""


def nominatim_search(client: httpx.Client, q: str
                    ) -> tuple[float, float] | None:
    """Mirror the URL the in-page search box builds: jsonv2, Japan bbox
    (left,top,right,bottom = 122,46,154,24), bounded=1, JP-first
    accept-language. Take the top hit."""
    params = {
        "format": "jsonv2",
        "limit": "1",
        "addressdetails": "0",
        "namedetails": "1",
        "viewbox": "122,46,154,24",
        "bounded": "1",
        "accept-language": "ja,zh-CN,zh,en",
        "q": q,
    }
    r = client.get(NOMINATIM, params=params, timeout=TIMEOUT_S)
    if r.status_code != 200:
        return None
    arr = r.json()
    if not isinstance(arr, list) or not arr:
        return None
    hit = arr[0]
    try:
        return float(hit["lat"]), float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def _has(e: dict, k: str) -> bool:
    v = e.get(k)
    return isinstance(v, str) and v.strip() != ""


def needs_translation(e: dict) -> bool:
    return not all(_has(e, k)
                   for k in ("name_sc", "name_tc", "name_jp", "name_en"))


def needs_coords(e: dict) -> bool:
    lat, lon = e.get("lat"), e.get("lon")
    return lat in (None, "") or lon in (None, "")


def reorder(e: dict) -> dict:
    out = {k: e[k] for k in KEY_ORDER if k in e}
    for k, v in e.items():
        if k not in out:
            out[k] = v
    return out


def save(entries: list[dict]) -> None:
    FAVORITES_BUILTIN_JSON.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    if not FAVORITES_BUILTIN_JSON.exists():
        raise SystemExit(f"missing {FAVORITES_BUILTIN_JSON}")
    entries: list[dict] = json.loads(
        FAVORITES_BUILTIN_JSON.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise SystemExit("favorites_builtin.json is not a JSON array")

    targets = [(i, e) for i, e in enumerate(entries)
               if isinstance(e, dict)
               and e.get("category") == "attraction"
               and (e.get("name_src") or "").strip()
               and (needs_translation(e) or needs_coords(e))]

    print(f"[enrich] {len(targets)} entries to process "
          f"(of {len(entries)} total)")
    eta_s = len(targets) * (2 * WIKI_DELAY_S + WIKI_DELAY_S + NOMINATIM_DELAY_S)
    print(f"[enrich] expect roughly {eta_s / 60:.1f} min (rate-limited)")
    print()

    stats = {"wiki_hit": 0, "wiki_miss": 0,
             "nom_hit": 0, "nom_miss": 0,
             "translated_any": 0, "geocoded": 0}
    misses_wiki: list[tuple[int, str]] = []
    misses_nom: list[tuple[int, str]] = []

    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for n, (idx, e) in enumerate(targets, start=1):
            name = (e.get("name_src") or "").strip()
            prefix = f"  [{n:3d}/{len(targets)}] {name}"

            # name_jp is always name_src for this dataset (curator ships JP).
            if not _has(e, "name_jp"):
                e["name_jp"] = name

            if needs_translation(e):
                qid = None
                for lang in WIKI_LANGS:
                    try:
                        qid = wiki_to_qid(client, name, lang)
                    except Exception as exc:   # noqa: BLE001
                        print(f"{prefix}  wiki warn ({lang}): {exc}")
                    time.sleep(WIKI_DELAY_S)
                    if qid:
                        break

                if qid:
                    try:
                        labels = wikidata_labels(client, qid)
                    except Exception as exc:   # noqa: BLE001
                        print(f"{prefix}  wikidata err: {exc}")
                        labels = {}
                    time.sleep(WIKI_DELAY_S)

                    sc = pick_sc(labels)
                    en = labels.get("en") or ""

                    wrote_any = False
                    if sc and not _has(e, "name_sc"):
                        e["name_sc"] = sc
                        wrote_any = True
                    if en and not _has(e, "name_en"):
                        e["name_en"] = en
                        wrote_any = True

                    print(f"{prefix}  wiki HIT {qid}  "
                          f"sc={e.get('name_sc') or ''!r}  "
                          f"en={e.get('name_en') or ''!r}")
                    stats["wiki_hit"] += 1
                    if wrote_any:
                        stats["translated_any"] += 1
                else:
                    print(f"{prefix}  wiki MISS")
                    stats["wiki_miss"] += 1
                    misses_wiki.append((idx, name))

            # Derive TC from SC whenever we have an SC and no TC yet.
            if _has(e, "name_sc") and not _has(e, "name_tc"):
                try:
                    e["name_tc"] = _s2t.convert(e["name_sc"])
                except Exception:   # noqa: BLE001
                    pass

            if needs_coords(e):
                try:
                    coords = nominatim_search(client, name)
                except Exception as exc:   # noqa: BLE001
                    print(f"{prefix}  nominatim err: {exc}")
                    coords = None
                if coords:
                    e["lat"], e["lon"] = coords
                    print(f"{prefix}  nom  HIT  "
                          f"({coords[0]:.5f}, {coords[1]:.5f})")
                    stats["nom_hit"] += 1
                    stats["geocoded"] += 1
                else:
                    print(f"{prefix}  nom  MISS")
                    stats["nom_miss"] += 1
                    misses_nom.append((idx, name))
                time.sleep(NOMINATIM_DELAY_S)

            entries[idx] = reorder(e)
            save(entries)

    print()
    print(f"[enrich] done. stats: {stats}")
    if misses_wiki:
        print(f"[enrich] {len(misses_wiki)} wiki misses "
              f"(SC/EN left blank — fill by hand):")
        for idx, name in misses_wiki:
            print(f"  - [#{idx}] {name}")
    if misses_nom:
        print(f"[enrich] {len(misses_nom)} nominatim misses "
              f"(lat/lon left blank — fill by hand):")
        for idx, name in misses_nom:
            print(f"  - [#{idx}] {name}")


if __name__ == "__main__":
    main()
