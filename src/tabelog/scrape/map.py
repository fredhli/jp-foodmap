"""
Plot data/tabelog/tabelog.csv on an interactive Japan-wide map with a
client-side filter panel: rating threshold, dinner-price bucket, cuisine,
Tabelog bookable. Each row carries a `region` column so rows from
different regions (osaka, kobe, okayama, tottori, ...) all coexist in
one file.

Geocoding via GSI AddressSearch; results cached to data/cache/geocode_cache.json.

CSV is utf-8-sig so Japanese addresses round-trip through Excel cleanly.

Output: docs/index.html  (single file, open in any browser).
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import folium
import httpx
from folium.plugins import MarkerCluster

# Force UTF-8 stdout — Windows console defaults to cp1252 and chokes on
# the Japanese restaurant names printed during the geocode pass.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from tabelog.paths import (
    TABELOG_CSV,
    GEOCODE_CACHE,
    MAP_HTML,
    RESTAURANTS_JSON,
    POPUPS_JSON,
    SW_JS,
    DOCS_DATA_DIR,
    FAVORITES_JSON,
    BLACKLIST_JSON,
    BOOKMARKS_JSON,
    OUTPUT_DIR,
    CACHE_DIR,
)
from tabelog.scrape.map_data import (
    ATTRACTIONS,
    DEFAULT_OFF_GENRES,
    GENRE_CATEGORIES,
    GENRE_EMOJI,
    MEAL_GROUPS,
)

GSI_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"

CSV_PATH = TABELOG_CSV
CACHE_PATH = GEOCODE_CACHE
OUT_HTML = MAP_HTML

# Baseline lists — JSON arrays of detail_url strings. Rebuilding the map
# re-reads these files; in-browser ⭐ / 🚫 clicks live in localStorage as a
# diff against the baseline and can be exported back to overwrite the file.
FAVORITES_PATH = FAVORITES_JSON
BLACKLIST_PATH = BLACKLIST_JSON


def _load_url_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return set()


def load_favorites() -> set[str]:
    return _load_url_set(FAVORITES_PATH)


def load_blacklist() -> set[str]:
    return _load_url_set(BLACKLIST_PATH)


def load_bookmarks() -> list[dict]:
    """User-named map pins. Each entry: {id, name, emoji, lat, lon, category}.
    Category is 'bookmark' (under the ⭐收藏 FAB) or 'attraction' (under the
    🗾景点 FAB, alongside the curated data/attractions.csv entries). Missing
    or unknown category falls back to 'bookmark' for backwards compat. Rows
    missing required coords are dropped silently — the in-browser editor is
    the source of truth, the file is just the build-time seed."""
    if not BOOKMARKS_JSON.exists():
        return []
    try:
        raw = json.loads(BOOKMARKS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        cat = str(item.get("category") or "bookmark")
        if cat not in ("bookmark", "attraction"):
            cat = "bookmark"
        out.append({
            "id": str(item.get("id") or f"{lat:.6f},{lon:.6f}"),
            "name": str(item.get("name") or "").strip() or "未命名",
            "emoji": str(item.get("emoji") or "📍"),
            "lat": lat,
            "lon": lon,
            "category": cat,
        })
    return out

JAPAN_CENTER = (36.2048, 138.2529)


def load_cache() -> dict[str, dict | None]:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


_FLOOR_RE = re.compile(
    r"\s*[BbＢ]?[\d０-９]{1,2}\s*(?:[FfＦ]|階).*$"
)
_BUILDING_KW = ("ビル", "メゾン", "ハイツ", "マンション", "別邸", "アネックス")


def simplify_address(addr: str) -> str:
    """Strip floor / building suffix to give the geocoder a fighting chance.
    Handles ASCII and full-width floor markers (1F / ２Ｆ / 1 階) and trailing
    building names — the building strip is unconditional (Sapporo's grid
    addresses like 南5西3 don't carry hyphenated 番地 but still trip on a
    ビル suffix)."""
    if not addr:
        return ""
    s = addr.strip()
    s = _FLOOR_RE.sub("", s).rstrip()
    for kw in _BUILDING_KW:
        i = s.find(kw)
        if i > 5:
            space = s.rfind(" ", 0, i)
            if space > 0:
                s = s[:space]
                break
    return s.strip()


# Kyoto's old-town addresses encode the nearest street intersection rather
# than just block numbers — e.g. "松原通大和大路東入2丁目轆轤町101". The
# 町名+番地 at the tail (轆轤町101) is what GSI can actually resolve; the
# 通り prefix confuses the matcher. This helper carves out <区><町名><番地>
# by finding the last cross-street marker and keeping only what follows.
_KYOTO_KU_RE = re.compile(r"^(京都府京都市\S+?区)")
_KYOTO_CROSS_RE = re.compile(r"(?:西入ル|東入ル|西入|東入|上ル|下ル|上る|下る)")
_KYOTO_TAIL_PREFIX_RE = re.compile(r"^(?:\d+丁目|(?:南側|北側|東側|西側)(?!町))+")


def kyoto_simplify(addr: str) -> str | None:
    if not addr or "京都府京都市" not in addr:
        return None
    m = _KYOTO_KU_RE.match(addr.strip())
    if not m:
        return None
    ku = m.group(1)
    rest = addr[m.end():]
    last_end = -1
    for c in _KYOTO_CROSS_RE.finditer(rest):
        last_end = c.end()
    if last_end < 0:
        return None
    tail = _KYOTO_TAIL_PREFIX_RE.sub("", rest[last_end:].lstrip())
    tail = simplify_address(tail).strip()
    if not tail:
        return None
    return ku + tail


# Sapporo's grid is officially '南N条西M丁目' but Tabelog (and locals)
# write '南N西M'. GSI's index uses the long form, so splice 条 in to match.
_SAPPORO_GRID_RE = re.compile(r"([東西南北])(\d+)\s*([東西南北])(\d+)")


def sapporo_simplify(addr: str) -> str | None:
    if not addr or "札幌" not in addr:
        return None
    s = simplify_address(addr)
    new, n = _SAPPORO_GRID_RE.subn(r"\1\2条\3\4", s, count=1)
    return new if n else None


# Some Tabelog Kyoto addresses drop the trailing 町 ('樋之口467-2' for
# what GSI indexes as '樋之口町'). When the tail is a bare <name><番地>
# with no 通 / 町, splice 町 in front of the number.
_KYOTO_BARE_RE = re.compile(
    r"^(京都府京都市\S+?区)([^\d\s通町]+?)(\d+(?:[-－]\d+)*)\s*$"
)


def kyoto_append_chome(addr: str) -> str | None:
    if not addr or "京都府京都市" not in addr:
        return None
    m = _KYOTO_BARE_RE.match(simplify_address(addr).strip())
    if not m:
        return None
    ku, tail, num = m.groups()
    return f"{ku}{tail}町{num}"


# Final-fallback for Kyoto addresses that omit the 入ル/西入 anchor
# entirely (e.g. "高辻通高倉泉正寺町465-2", "東大路安井北門通月見町13").
# Scans for the rightmost <町名><番地> token after the 区 prefix —
# [^\s通]+? can't span 通 so the match auto-stops at the last 通り
# boundary, giving us just the residential tail.
_KYOTO_CHOME_RE = re.compile(r"[^\s通]+?町\d+(?:[-－]\d+)*")


def kyoto_extract_chome(addr: str) -> str | None:
    if not addr or "京都府京都市" not in addr:
        return None
    m = _KYOTO_KU_RE.match(addr.strip())
    if not m:
        return None
    matches = list(_KYOTO_CHOME_RE.finditer(addr[m.end():]))
    if not matches:
        return None
    return m.group(1) + matches[-1].group(0)


def gsi_geocode(query: str, client: httpx.Client) -> dict | None:
    """Hit GSI AddressSearch. Returns the top hit's coords + title, or None."""
    try:
        r = client.get(GSI_URL, params={"q": query}, timeout=30.0)
        r.raise_for_status()
        hits = r.json()
    except Exception as e:
        print(f"  GSI error on {query!r}: {e}")
        return None
    if not hits:
        return None
    top = hits[0]
    geom = (top.get("geometry") or {}).get("coordinates") or []
    if len(geom) != 2:
        return None
    lon, lat = geom  # GSI returns [lon, lat]
    return {
        "lat": float(lat),
        "lon": float(lon),
        "matched_query": query,
        "display": (top.get("properties") or {}).get("title", ""),
    }


def geocode(addr: str, client: httpx.Client, cache: dict) -> dict | None:
    if addr in cache:
        return cache[addr]
    candidates = [
        addr,
        simplify_address(addr),
        kyoto_simplify(addr),
        kyoto_extract_chome(addr),
        kyoto_append_chome(addr),
        sapporo_simplify(addr),
    ]
    candidates = [c for c in candidates if c]
    seen = set()
    for q in candidates:
        if q in seen:
            continue
        seen.add(q)
        loc = gsi_geocode(q, client)
        if loc is not None:
            cache[addr] = loc
            return loc
    cache[addr] = None
    return None


_GENRE_TO_CAT = {tok: cat for cat, toks in GENRE_CATEGORIES.items() for tok in toks}
_GENRE_SPLIT_RE = re.compile(r"[、,，]")


def categorize_genre(genre_str: str) -> list[str]:
    """Single-tag: scan tokens left-to-right, return the first that maps to a
    known bucket. Falls through to '其他' if no token matches. Returns a list
    (length 0 or 1) so downstream iteration keeps working."""
    if not genre_str:
        return []
    for tok in (t.strip() for t in _GENRE_SPLIT_RE.split(genre_str) if t.strip()):
        cat = _GENRE_TO_CAT.get(tok)
        if cat:
            return [cat]
    return ["其他"]


# Price buckets — keys must match the JS filter values below.
# (key, label, color, lower_inclusive, upper_exclusive)
PRICE_BUCKETS = [
    ("lt1k",   "< ¥1,000",         "#15803d", None,   1000),
    ("1to3k",  "¥1,000 – 3,000",   "#16a34a", 1000,   3000),
    ("3to5k",  "¥3,000 – 5,000",   "#84cc16", 3000,   5000),
    ("5to10k", "¥5,000 – 10,000",  "#eab308", 5000,  10000),
    ("10to20k","¥10,000 – 20,000", "#f97316", 10000, 20000),
    ("ge20k",  "¥20,000+",         "#dc2626", 20000, None),
    ("na",     "价格 NA",           "#9ca3af", None,   None),
]


def _as_price_int(v) -> int | None:
    try:
        return int(v) if v not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


def price_bucket(row: dict) -> tuple[str, str, str]:
    """Return (key, label, hex_color) for the marker.

    Bucket by dinner_upper; when dinner is NA, fall back to lunch_upper so
    lunch-only shops (ラーメン, うどん, 定食…) still get coloured."""
    n = _as_price_int(row.get("dinner_upper"))
    if n is None:
        n = _as_price_int(row.get("lunch_upper"))
    if n is None:
        return ("na", "价格 NA", "#9ca3af")
    for key, label, color, lo, hi in PRICE_BUCKETS:
        if key == "na":
            continue
        if (lo is None or n >= lo) and (hi is None or n < hi):
            return (key, label, color)
    return ("na", "价格 NA", "#9ca3af")


def build_filter_panel_html(cat_counts: dict[str, int]) -> str:
    price_rows = "\n".join(
        f'      <label style="display:block;margin:1px 0;">'
        f'<input type="checkbox" name="ff-price" value="{key}" checked> '
        f'<span style="display:inline-block;width:11px;height:11px;background:{color};'
        f'border-radius:50%;margin:0 4px;vertical-align:middle;"></span>{label}</label>'
        for key, label, color, _, _ in PRICE_BUCKETS
    )
    # DEFAULT_OFF_GENRES (中/韩/西/南亚/中东·非洲) are not shown in the
    # cuisine filter — they're controlled by the standalone "隐藏外国料理"
    # toggle below. Remaining buckets are grouped by MEAL_GROUPS with a
    # section header above each cluster.
    def _genre_section(group: str, buckets: list[str]) -> str:
        visible = [cat for cat in buckets if cat not in DEFAULT_OFF_GENRES]
        if not visible:
            return ""
        rows = "\n".join(
            f'        <label style="display:block;margin:1px 0;line-height:1.4;">'
            f'<input type="checkbox" name="ff-genre" value="{cat}" checked> '
            f'{cat} <span style="color:#9ca3af;">({cat_counts.get(cat, 0)})</span></label>'
            for cat in visible
        )
        # Per-group 全选/全清 chips: same wiring as the section-wide ones,
        # scoped to checkboxes inside this wrapper via the data attribute.
        header = (
            f'        <div style="display:flex;justify-content:space-between;'
            f'align-items:baseline;margin:6px 0 2px;">'
            f'<span style="font-weight:600;color:#374151;font-size:11px;'
            f'letter-spacing:0.5px;">{group}</span>'
            f'<span style="font-size:10px;">'
            f'<a href="#" class="ff-group-all" style="color:#2563eb;text-decoration:none;">全选</a>'
            f'<span style="color:#d1d5db;"> | </span>'
            f'<a href="#" class="ff-group-none" style="color:#2563eb;text-decoration:none;">全清</a>'
            f'</span>'
            f'</div>'
        )
        return f'      <div data-genre-group="{group}">\n{header}\n{rows}\n      </div>'
    genre_rows = "\n".join(
        _genre_section(group, buckets) for group, buckets in MEAL_GROUPS.items()
    )
    foreign_count = sum(cat_counts.get(c, 0) for c in DEFAULT_OFF_GENRES)
    return f"""
<style>
  /* Filter bottom-sheet — same visual treatment as the restaurant detail
     sheet (#bs-sheet), with parallel width breakpoints. The two sheets are
     mutually exclusive (opening one closes the other), so they share the
     same vertical slot at the bottom of the viewport. */
  #ff-backdrop {{
    position: fixed; inset: 0; z-index: 10001;
    background: rgba(0,0,0,0.35);
    opacity: 0; pointer-events: none;
    transition: opacity 0.22s ease-out;
  }}
  #ff-backdrop.ff-open {{ opacity: 1; pointer-events: auto; }}
  #ff-sheet {{
    position: fixed; left: 0; right: 0; bottom: 0;
    z-index: 10002;
    max-height: 75vh; max-height: 75dvh;
    background: #fff;
    border-radius: 14px 14px 0 0;
    box-shadow: 0 -8px 24px rgba(0,0,0,0.18);
    transform: translateY(100%);
    transition: transform 0.25s ease-out;
    display: flex; flex-direction: column;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    padding-bottom: env(safe-area-inset-bottom);
  }}
  #ff-sheet.ff-open {{ transform: translateY(0); }}
  @media (min-width: 700px) {{
    #ff-sheet {{ left: 50%; transform: translate(-50%, 100%);
                 width: min(560px, calc(100vw - 32px)); right: auto;
                 max-height: 80vh; max-height: 80dvh;
                 border-radius: 14px 14px 0 0; }}
    #ff-sheet.ff-open {{ transform: translate(-50%, 0); }}
  }}
  @media (min-width: 1100px) {{
    #ff-sheet {{ width: min(640px, calc(100vw - 32px));
                 max-height: 85vh; max-height: 85dvh; }}
  }}
  #ff-grip {{
    position: relative;
    padding: 9px 0 6px; flex-shrink: 0;
    cursor: grab; touch-action: none;
  }}
  #ff-grip::before {{
    content: ''; display: block;
    width: 38px; height: 4px; margin: 0 auto;
    background: #d1d5db; border-radius: 2px;
  }}
  #ff-sheet-content {{
    overflow-y: auto;
    padding: 0 16px 16px;
    flex: 1 1 auto;
    -webkit-overflow-scrolling: touch;
    font-size: 13px; line-height: 1.5; color: #111827;
  }}
  /* Bottom-left FAB that opens the sheet. Matches the right-side .map-fab
     style but stands alone — labelled with the live filter count so the
     "how many results match" feedback survives the collapse to a sheet. */
  #ff-fab {{
    position: fixed; bottom: 18px; left: 14px;
    z-index: 9995;
    background: #fff; color: #374151;
    border: 1px solid #d1d5db;
    border-radius: 999px;
    padding: 9px 14px;
    font-size: 13px; font-weight: 600;
    cursor: pointer;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    display: inline-flex; align-items: center; gap: 8px;
    user-select: none;
    transition: background 0.15s ease-out, box-shadow 0.15s ease-out;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    line-height: 1;
  }}
  #ff-fab:hover {{ background: #f9fafb;
                   box-shadow: 0 4px 10px rgba(0,0,0,0.18); }}
  #ff-fab[hidden] {{ display: none; }}
  #ff-fab .ff-fab-ic {{ font-size: 15px; }}
  #ff-fab .ff-fab-count {{ font-variant-numeric: tabular-nums; }}
  #ff-fab .ff-fab-count b {{ color: #2563eb; }}
</style>
<button id="ff-fab" type="button" aria-label="打开筛选" title="筛选">
  <span class="ff-fab-ic">🎛</span>
  <span class="ff-fab-count"><b class="ff-count">–</b> / <span class="ff-total">–</span></span>
</button>
<div id="ff-backdrop"></div>
<div id="ff-sheet" role="dialog" aria-modal="true" aria-hidden="true"
     aria-labelledby="ff-sheet-title">
  <div id="ff-grip"></div>
  <div id="ff-sheet-content">
  <div style="display:flex;justify-content:space-between;align-items:center;
              border-bottom:1px solid #e5e7eb;padding:2px 0 8px;margin-bottom:10px;">
    <span id="ff-sheet-title" style="font-weight:700;font-size:15px;">筛选</span>
    <span style="font-size:12px;color:#6b7280;">
      显示 <b class="ff-count">–</b> / <span class="ff-total">–</span>
    </span>
  </div>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">
    <span style="font-weight:600;">评分</span>
    <span style="font-size:11px;color:#374151;">≥ <b id="ff-rating-val">3.4</b></span>
  </div>
  <input type="range" id="ff-rating" min="3.4" max="4.5" step="0.05" value="3.4"
         style="width:100%;margin-bottom:6px;">

  <div style="display:flex;justify-content:space-between;align-items:baseline;">
    <span style="font-weight:600;">晚餐价格</span>
    <span style="font-size:11px;">
      <a href="#" id="ff-price-all" style="color:#2563eb;text-decoration:none;">全选</a>
      <span style="color:#d1d5db;">|</span>
      <a href="#" id="ff-price-none" style="color:#2563eb;text-decoration:none;">全清</a>
    </span>
  </div>
{price_rows}

  <details id="ff-genre-box" style="margin-top:6px;margin-bottom:6px;">
    <summary style="cursor:pointer;list-style:none;display:flex;
                    justify-content:space-between;align-items:center;
                    padding:4px 6px;border:1px solid #d1d5db;border-radius:4px;
                    background:#f9fafb;font-size:12px;">
      <span style="font-weight:600;">菜系
        <span id="ff-genre-summary" style="font-weight:400;color:#6b7280;
              font-size:11px;margin-left:4px;">全部</span>
      </span>
      <span style="color:#6b7280;font-size:10px;">▾</span>
    </summary>
    <div style="margin-top:4px;font-size:11px;">
      <div style="display:flex;justify-content:flex-end;gap:6px;margin-bottom:3px;">
        <a href="#" id="ff-genre-all" style="color:#2563eb;text-decoration:none;">全选</a>
        <span style="color:#d1d5db;">|</span>
        <a href="#" id="ff-genre-none" style="color:#2563eb;text-decoration:none;">全清</a>
      </div>
      <div style="max-height:180px;overflow-y:auto;border:1px solid #e5e7eb;
                  border-radius:4px;padding:4px 6px;background:#fff;">
{genre_rows}
      </div>
    </div>
  </details>

  <div style="font-weight:600;margin-top:6px;margin-bottom:2px;">Tabelog 预约</div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:0 4px;margin-bottom:6px;">
    <label><input type="radio" name="ff-bookable" value="all" checked> 全部</label>
    <label><input type="radio" name="ff-bookable" value="yes"> 可</label>
    <label><input type="radio" name="ff-bookable" value="no"> 不可</label>
  </div>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px;margin-bottom:2px;">
    <span style="font-weight:600;">收藏</span>
    <span style="font-size:11px;color:#6b7280;">⭐ <b id="ff-fav-count">0</b></span>
  </div>
  <label style="display:block;margin-bottom:4px;">
    <input type="checkbox" id="ff-only-fav"> 只显示已收藏
  </label>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px;margin-bottom:2px;">
    <span style="font-weight:600;">弃用名单</span>
    <span style="font-size:11px;color:#6b7280;">🚫 <b id="ff-black-count">0</b></span>
  </div>
  <label style="display:block;margin-bottom:6px;">
    <input type="checkbox" id="ff-hide-black" checked> 隐藏弃用名单
  </label>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px;margin-bottom:2px;">
    <span style="font-weight:600;">外国料理</span>
    <span style="font-size:11px;color:#6b7280;">🌏 <b>{foreign_count}</b></span>
  </div>
  <label style="display:block;margin-bottom:6px;">
    <input type="checkbox" id="ff-hide-foreign" checked> 隐藏外国料理 (🇨🇳🇹🇼🇰🇷🇫🇷🇮🇹🇺🇸🇮🇳🇹🇭🇱🇧)
  </label>

  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin-bottom:4px;">
    <button id="ff-reset" style="padding:4px 0;border:1px solid #d1d5db;
            background:#f9fafb;border-radius:4px;cursor:pointer;font-size:11px;
            color:#374151;">重置筛选</button>
    <button id="ff-settings" style="padding:4px 0;border:1px solid #d1d5db;
            background:#eff6ff;border-radius:4px;cursor:pointer;font-size:11px;
            color:#1d4ed8;">⚙️ 同步设置</button>
    <!-- Language placeholder: persists the user's choice to localStorage
         under `tabelog.lang`; the actual translation pass will read it later. -->
    <select id="ff-lang" aria-label="语言"
            style="padding:4px 4px;border:1px solid #d1d5db;
                   background:#f9fafb;border-radius:4px;cursor:pointer;font-size:11px;
                   color:#374151;font-family:inherit;line-height:1.3;
                   text-align:center;text-align-last:center;
                   appearance:none;-webkit-appearance:none;-moz-appearance:none;">
      <option value="zh-CN">🌐 简体</option>
      <option value="zh-TW">🌐 繁體</option>
      <option value="en">🌐 EN</option>
    </select>
  </div>
  <div id="ff-sync-status" style="font-size:10px;color:#6b7280;text-align:center;
       margin-top:2px;min-height:13px;">本地模式</div>
  </div>
</div>

<!-- Settings modal (Gist ID + PAT). Hidden by default. -->
<!-- Lives outside #ff-sheet so opening it doesn't have to fight the sheet's
     own transform / overflow. Still owned by the filter UI for wiring. -->
<!-- z-index must beat ff-sheet (10002) and bm-modal (10011) — this modal is
     spawned from inside the filter sheet, so it has to render on top of it.
     Otherwise opening 同步设置 looks like nothing happened. -->
<div id="ff-modal-bg" style="display:none;position:fixed;inset:0;z-index:10020;
     background:rgba(0,0,0,0.4);align-items:center;justify-content:center;">
  <div style="background:#fff;border-radius:10px;padding:18px 20px;width:340px;
       font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       font-size:13px;color:#111827;box-shadow:0 10px 30px rgba(0,0,0,0.25);">
    <div style="font-weight:700;font-size:15px;margin-bottom:4px;">同步设置</div>
    <div style="color:#6b7280;font-size:11px;margin-bottom:12px;line-height:1.5;">
      把收藏/弃用名单实时同步到 GitHub Gist。<br>
      仅保存在你这台设备的浏览器里，不会上传到代码仓库。
    </div>

    <label style="display:block;font-weight:600;margin-bottom:3px;">Gist ID</label>
    <input id="ff-cfg-gist" type="text" placeholder="例如 a1b2c3d4e5f6..."
           style="width:100%;padding:6px 8px;border:1px solid #d1d5db;
                  border-radius:4px;font-family:monospace;font-size:12px;
                  box-sizing:border-box;margin-bottom:10px;">

    <label style="display:block;font-weight:600;margin-bottom:3px;">
      Personal Access Token
      <span style="font-weight:400;color:#6b7280;font-size:11px;">（只看不改可留空）</span>
    </label>
    <input id="ff-cfg-pat" type="password" placeholder="ghp_..."
           style="width:100%;padding:6px 8px;border:1px solid #d1d5db;
                  border-radius:4px;font-family:monospace;font-size:12px;
                  box-sizing:border-box;margin-bottom:6px;">
    <div style="font-size:10px;color:#6b7280;margin-bottom:12px;line-height:1.5;">
      创建：<a href="https://github.com/settings/tokens?type=beta" target="_blank"
      style="color:#2563eb;">github.com/settings/tokens</a>
      → Fine-grained → 只勾 <code>Gists</code> 权限。
    </div>

    <div id="ff-cfg-msg" style="font-size:11px;min-height:14px;margin-bottom:8px;"></div>

    <div style="display:flex;gap:6px;">
      <button id="ff-cfg-save" style="flex:1;padding:6px;border:1px solid #2563eb;
              background:#2563eb;color:#fff;border-radius:4px;cursor:pointer;
              font-size:12px;font-weight:600;">保存并测试</button>
      <button id="ff-cfg-clear" style="padding:6px 10px;border:1px solid #d1d5db;
              background:#f9fafb;color:#374151;border-radius:4px;cursor:pointer;
              font-size:12px;">清除</button>
      <button id="ff-cfg-cancel" style="padding:6px 10px;border:1px solid #d1d5db;
              background:#f9fafb;color:#374151;border-radius:4px;cursor:pointer;
              font-size:12px;">取消</button>
    </div>
  </div>
</div>
"""


LOCATE_ASSETS = """
<meta name="robots" content="noindex,nofollow,noarchive,nosnippet">
<meta name="googlebot" content="noindex,nofollow,noarchive,nosnippet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet.locatecontrol@0.79.0/dist/L.Control.Locate.min.css"/>
<script defer src="https://cdn.jsdelivr.net/npm/leaflet.locatecontrol@0.79.0/dist/L.Control.Locate.min.js"></script>
<script defer src="transit-layer.js"></script>
<style>
  /* Suppress iOS long-press callout + text-selection on the map so the
     contextmenu handler fires cleanly on touch. */
  .leaflet-container {
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
  }
  /* The locate plugin renders its own top-left button; we drive it from
     the bottom-right FAB stack instead, so suppress the default UI. The
     control instance stays alive for its .start() / .stop() methods. */
  .leaflet-control-locate { display: none !important; }
</style>
"""


# Page-level zoom lock + iOS Safari bounce kill. The viewport meta folium
# emits already has user-scalable=no, but iOS Safari has ignored that
# since iOS 10 for accessibility, so we need event listeners too.
# Leaflet uses raw touch events for its own map gestures, not iOS gesture*
# events, so blocking gesture* on the document does NOT break map pinch.
MAP_FAB_HTML = """
<style>
  /* Floating layer-control replacement (Google-Maps-style pills, bottom-right).
     Container is pointer-events:none so the gaps don't block map drags;
     each button re-enables pointer events. */
  .map-fab-stack {
    position: fixed; bottom: 18px; right: 14px;
    z-index: 9995;
    display: flex; flex-direction: column; gap: 8px;
    pointer-events: none;
  }
  .map-fab {
    pointer-events: auto;
    background: #fff; color: #374151;
    border: 1px solid #d1d5db;
    border-radius: 999px;
    padding: 8px 14px;
    font-size: 13px; font-weight: 600;
    cursor: pointer;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    display: inline-flex; align-items: center; gap: 6px;
    user-select: none;
    transition: background 0.15s ease-out, box-shadow 0.15s ease-out,
                color 0.15s ease-out, border-color 0.15s ease-out;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    line-height: 1;
  }
  .map-fab:hover { background: #f9fafb;
                   box-shadow: 0 4px 10px rgba(0,0,0,0.18); }
  .map-fab.active { background: #2563eb; color: #fff;
                    border-color: #2563eb; }
  .map-fab.active:hover { background: #1d4ed8; }
  .map-fab-ic { font-size: 15px; line-height: 1; }
  /* Locate button is icon-only on every viewport — round, no label. The
     SVG keeps it visually distinct from the pill-shaped layer toggles. */
  .map-fab.map-fab-circle {
    width: 40px; height: 40px; padding: 0;
    border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
    align-self: flex-end;       /* line up flush with the pill stack edge */
  }
  .map-fab .map-fab-svg {
    width: 20px; height: 20px; display: block;
    stroke: currentColor;
  }
  /* While the plugin is following the user, paint the FAB blue. The class
     is added/removed by locateactivate/locatedeactivate map events. */
  .map-fab.map-fab-circle.locating { background: #2563eb; color: #fff;
                                     border-color: #2563eb; }
  /* Tighten on narrow screens — drop the label, keep just the icon. */
  @media (max-width: 480px) {
    .map-fab { padding: 9px 10px; }
    .map-fab-label { display: none; }
    .map-fab-ic { font-size: 17px; }
  }
</style>
<div class="map-fab-stack" role="group" aria-label="图层切换">
  <button id="fab-locate" class="map-fab map-fab-circle" type="button"
          title="定位到我的位置" aria-label="定位到我的位置">
    <svg class="map-fab-svg" viewBox="0 0 24 24" fill="none"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
         aria-hidden="true">
      <circle cx="12" cy="12" r="2.5" fill="currentColor" stroke="none"></circle>
      <circle cx="12" cy="12" r="7.5"></circle>
      <line x1="12" y1="1.5" x2="12" y2="4"></line>
      <line x1="12" y1="20" x2="12" y2="22.5"></line>
      <line x1="1.5" y1="12" x2="4" y2="12"></line>
      <line x1="20" y1="12" x2="22.5" y2="12"></line>
    </svg>
  </button>
  <button id="fab-transit-long" class="map-fab" type="button"
          aria-pressed="false" title="新干线 / JR 长途线路">
    <span class="map-fab-ic">🚄</span><span class="map-fab-label">长途</span>
  </button>
  <button id="fab-transit-city" class="map-fab" type="button"
          aria-pressed="false" title="地铁 / 私铁 / 城市轨道">
    <span class="map-fab-ic">🚇</span><span class="map-fab-label">市内</span>
  </button>
  <button id="fab-attractions" class="map-fab active" type="button"
          aria-pressed="true" title="景点锚点">
    <span class="map-fab-ic">🗾</span><span class="map-fab-label">景点</span>
  </button>
  <button id="fab-bookmarks" class="map-fab active" type="button"
          aria-pressed="true" title="我的收藏">
    <span class="map-fab-ic">⭐</span><span class="map-fab-label">收藏</span>
  </button>
</div>
"""


# Top-center floating search box. Hits Nominatim (OSM) and drops the
# results into a clickable dropdown. CORS-safe from the browser; rate
# limited by the JS-side debounce (~300ms per keystroke).
SEARCH_BOX_HTML = """
<style>
  #ss-box {
    position: fixed; top: 12px; left: 50%;
    transform: translateX(-50%);
    z-index: 9996;
    width: min(calc(100vw - 32px), 380px);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }
  #ss-input-wrap {
    position: relative; display: flex; align-items: center;
    background: #fff; border: 1px solid #d1d5db; border-radius: 22px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    transition: box-shadow 0.15s ease-out;
  }
  #ss-input-wrap:focus-within {
    box-shadow: 0 4px 14px rgba(0,0,0,0.22);
    border-color: #93c5fd;
  }
  #ss-icon {
    padding-left: 14px; color: #6b7280; font-size: 14px;
    line-height: 1; user-select: none;
  }
  #ss-input {
    flex: 1; min-width: 0;
    padding: 9px 6px 9px 8px;
    border: none; outline: none; background: transparent;
    font-size: 14px; color: #1f2937;
    font-family: inherit;
  }
  #ss-input::placeholder { color: #9ca3af; }
  #ss-clear {
    border: none; background: none; cursor: pointer;
    color: #6b7280; font-size: 20px; line-height: 1;
    padding: 8px 14px 8px 8px;
    display: none;
    -webkit-tap-highlight-color: transparent;
  }
  #ss-clear:hover, #ss-clear:active { color: #1f2937; }
  #ss-input-wrap.has-text #ss-clear,
  #ss-input-wrap.searching #ss-clear { display: block; }
  #ss-spinner {
    display: none;
    width: 14px; height: 14px;
    border: 2px solid #e5e7eb; border-top-color: #2563eb;
    border-radius: 50%;
    margin-right: 12px;
    animation: ss-spin 0.8s linear infinite;
  }
  #ss-input-wrap.busy #ss-spinner { display: block; }
  #ss-input-wrap.busy #ss-clear   { display: none; }
  @keyframes ss-spin { to { transform: rotate(360deg); } }
  #ss-list {
    margin-top: 6px;
    background: #fff;
    border: 1px solid #d1d5db; border-radius: 10px;
    box-shadow: 0 6px 16px rgba(0,0,0,0.16);
    overflow: hidden;
    display: none;
    max-height: 65vh; max-height: 65dvh;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }
  #ss-list.open { display: block; }
  #ss-list .ss-row {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 10px;
    border-bottom: 1px solid #f3f4f6;
    cursor: pointer;
    transition: background 0.1s ease-out;
  }
  #ss-list .ss-row:last-child { border-bottom: none; }
  #ss-list .ss-row:hover { background: #f9fafb; }
  #ss-list .ss-row.ss-empty {
    cursor: default; color: #6b7280; font-size: 12px;
    justify-content: center; padding: 14px 10px;
  }
  #ss-list .ss-row.ss-empty:hover { background: transparent; }
  #ss-list .ss-row.ss-error { color: #b91c1c; }
  #ss-list .ss-text { flex: 1; min-width: 0; }
  #ss-list .ss-name {
    font-size: 13px; font-weight: 600; color: #1f2937;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  #ss-list .ss-addr {
    font-size: 11px; color: #6b7280;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    margin-top: 1px;
  }
  #ss-list .ss-fav {
    flex-shrink: 0;
    background: #f9fafb; border: 1px solid #d1d5db;
    border-radius: 6px; cursor: pointer;
    font-size: 14px; line-height: 1;
    padding: 5px 8px; color: #374151;
  }
  #ss-list .ss-fav:hover { background: #fef3c7; border-color: #facc15; }
  #ss-list .ss-icon {
    flex-shrink: 0; font-size: 16px; line-height: 1; width: 20px;
    text-align: center; color: #6b7280;
  }
  @media (max-width: 480px) {
    #ss-input { font-size: 16px; }       /* iOS no-zoom */
    #ss-box { top: 8px; width: calc(100vw - 16px); }
  }
</style>
<div id="ss-box">
  <div id="ss-input-wrap">
    <span id="ss-icon">🔍</span>
    <input id="ss-input" type="text" autocomplete="off"
           placeholder="搜索景点 / 地址 ...">
    <div id="ss-spinner"></div>
    <button id="ss-clear" type="button" aria-label="清空">×</button>
  </div>
  <div id="ss-list" role="listbox"></div>
</div>
"""


# Right-click → 加入收藏 modal. Two inputs (name + emoji) and a row of
# preset emoji chips, plus a full emoji picker (Web Component from
# CDN). The picker uses Shadow DOM, so the page-wide MutationObserver
# that swaps emoji glyphs for Apple-CDN images can't reach inside it —
# the picker renders system glyphs natively, which is exactly what its
# search index expects.
BOOKMARKS_MODAL_HTML = """
<script type="module"
        src="https://cdn.jsdelivr.net/npm/emoji-picker-element@^1/index.js"></script>
<style>
  #bm-backdrop {
    position: fixed; inset: 0; z-index: 10010;
    background: rgba(0,0,0,0.35);
    opacity: 0; pointer-events: none;
    transition: opacity 0.2s ease-out;
  }
  #bm-backdrop.bm-open { opacity: 1; pointer-events: auto; }
  #bm-modal {
    position: fixed; left: 50%; top: 50%;
    transform: translate(-50%, -50%) scale(0.96);
    z-index: 10011;
    width: min(92vw, 340px);
    max-height: 90vh; max-height: 90dvh;
    background: #fff;
    border-radius: 10px;
    box-shadow: 0 12px 32px rgba(0,0,0,0.25);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: #1f2937;
    opacity: 0; pointer-events: none;
    transition: opacity 0.18s ease-out, transform 0.18s ease-out;
    display: flex; flex-direction: column;
  }
  #bm-modal.bm-open {
    opacity: 1; pointer-events: auto;
    transform: translate(-50%, -50%) scale(1);
  }
  #bm-modal .bm-head {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid #e5e7eb;
    flex-shrink: 0;
  }
  #bm-modal .bm-title { font-weight: 700; font-size: 14px; }
  #bm-modal .bm-close {
    background: none; border: none; cursor: pointer;
    font-size: 20px; line-height: 1; color: #9ca3af;
    padding: 2px 6px;
  }
  #bm-modal .bm-close:hover { color: #374151; }
  #bm-modal .bm-body {
    padding: 12px 14px; font-size: 13px;
    overflow-y: auto; flex: 1 1 auto; min-height: 0;
    -webkit-overflow-scrolling: touch;
  }
  #bm-modal .bm-coord {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color: #6b7280; font-size: 12px;
    margin-bottom: 10px; text-align: center;
  }
  #bm-modal .bm-row {
    display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
  }
  #bm-modal .bm-row > label {
    width: 42px; flex-shrink: 0; color: #6b7280; font-size: 12px;
  }
  #bm-modal .bm-row > input {
    flex: 1; min-width: 0;
    padding: 6px 8px;
    border: 1px solid #d1d5db; border-radius: 5px;
    font-size: 13px; font-family: inherit;
    box-sizing: border-box;
  }
  #bm-modal #bm-emoji {
    flex: 1; min-width: 0;
    text-align: left; font-size: 18px;
  }
  /* "常用" quick-pick row — small label + chip buttons, on a tinted
     panel so the section is visually separate from the typed input. */
  #bm-modal .bm-quick {
    display: flex; align-items: center; gap: 10px;
    padding: 7px 10px; margin-bottom: 8px;
    background: #f9fafb; border: 1px solid #f3f4f6; border-radius: 6px;
  }
  #bm-modal .bm-quick-label {
    font-size: 11px; color: #6b7280; font-weight: 600;
    flex-shrink: 0;
  }
  #bm-modal .bm-quick-list {
    display: flex; gap: 6px; flex-wrap: wrap;
  }
  #bm-modal .bm-quick-list button {
    background: #fff; border: 1px solid #d1d5db; border-radius: 5px;
    cursor: pointer; font-size: 16px; line-height: 1;
    padding: 4px 7px;
    font-family: inherit;
  }
  #bm-modal .bm-quick-list button:hover {
    background: #eff6ff; border-color: #93c5fd;
  }
  /* Full-picker toggle sits inside .bm-quick-list as a 7th chip, but
     tinted blue so it reads as "open a different surface" rather than
     "another preset". */
  #bm-modal #bm-emoji-more {
    background: #eff6ff; border-color: #bfdbfe;
  }
  #bm-modal #bm-emoji-more:hover {
    background: #dbeafe; border-color: #93c5fd;
  }
  #bm-modal #bm-emoji-picker {
    display: none;
    width: 100%;
    height: 280px;
    margin-top: 8px;
    /* Tokens consumed by emoji-picker-element's shadow DOM. */
    --background: #fff;
    --border-color: #e5e7eb;
    --border-radius: 8px;
    --emoji-size: 1.15rem;
    --num-columns: 8;
  }
  #bm-modal #bm-emoji-picker.bm-show { display: block; }
  #bm-modal .bm-error {
    color: #b91c1c; font-size: 12px;
    min-height: 16px; margin-top: 8px;
  }
  #bm-modal .bm-foot {
    display: flex; justify-content: flex-end; gap: 8px;
    padding: 8px 14px 12px;
    flex-shrink: 0;
    border-top: 1px solid #f3f4f6;
  }
  #bm-modal .bm-foot button {
    padding: 6px 14px; border-radius: 5px; cursor: pointer;
    font-size: 13px; font-weight: 600; border: 1px solid #d1d5db;
    background: #f9fafb; color: #1f2937;
  }
  #bm-modal .bm-foot button.bm-save {
    background: #2563eb; border-color: #2563eb; color: #fff;
  }
  #bm-modal .bm-foot button.bm-save:hover { background: #1d4ed8; }
  #bm-modal .bm-foot button.bm-cancel:hover { background: #f3f4f6; }
  /* 类型 segmented control: two buttons sharing one rounded shell, the
     active one paints blue. Same shell width as a single text input so
     it lines up with the rest of the form. */
  #bm-modal .bm-kind-seg {
    flex: 1; min-width: 0;
    display: flex;
    border: 1px solid #d1d5db; border-radius: 6px;
    overflow: hidden;
    background: #fff;
  }
  #bm-modal .bm-kind-seg button {
    flex: 1; min-width: 0;
    background: transparent; border: none; cursor: pointer;
    font-family: inherit; font-size: 13px; font-weight: 600;
    color: #6b7280;
    padding: 7px 8px;
    transition: background 0.12s ease-out, color 0.12s ease-out;
  }
  #bm-modal .bm-kind-seg button + button {
    border-left: 1px solid #d1d5db;
  }
  #bm-modal .bm-kind-seg button:hover:not(.active) {
    background: #f9fafb; color: #374151;
  }
  #bm-modal .bm-kind-seg button.active {
    background: #2563eb; color: #fff;
  }
</style>
<div id="bm-backdrop"></div>
<div id="bm-modal" role="dialog" aria-modal="true" aria-hidden="true"
     aria-labelledby="bm-modal-title">
  <div class="bm-head">
    <span class="bm-title" id="bm-modal-title">加入收藏</span>
    <button class="bm-close" aria-label="关闭">×</button>
  </div>
  <div class="bm-body">
    <div class="bm-coord" id="bm-coord"></div>
    <div class="bm-row">
      <label>类型</label>
      <div class="bm-kind-seg" role="radiogroup" aria-label="类型">
        <button type="button" data-kind="bookmark" class="active"
                role="radio" aria-checked="true">⭐ 收藏</button>
        <button type="button" data-kind="attraction"
                role="radio" aria-checked="false">🗾 景点</button>
      </div>
    </div>
    <div class="bm-row">
      <label for="bm-name">名称</label>
      <input type="text" id="bm-name" maxlength="40"
             placeholder="例如：东京塔" autocomplete="off">
    </div>
    <div class="bm-row">
      <label for="bm-emoji">Emoji</label>
      <input type="text" id="bm-emoji" maxlength="8" value="📍"
             placeholder="可粘贴任意 emoji" autocomplete="off">
    </div>

    <div class="bm-quick">
      <span class="bm-quick-label">常用</span>
      <div class="bm-quick-list">
        <button type="button" data-emoji="🏠">🏠</button>
        <button type="button" data-emoji="🏨">🏨</button>
        <button type="button" data-emoji="🍽️">🍽️</button>
        <button type="button" data-emoji="⭐">⭐</button>
        <button type="button" data-emoji="❤️">❤️</button>
        <button type="button" data-emoji="🛍️">🛍️</button>
        <button type="button" id="bm-emoji-more"
                aria-expanded="false" aria-controls="bm-emoji-picker"
                title="打开完整 emoji 选择器">🔽</button>
      </div>
    </div>

    <emoji-picker id="bm-emoji-picker"></emoji-picker>

    <div class="bm-error" id="bm-error" aria-live="polite"></div>
  </div>
  <div class="bm-foot">
    <button type="button" class="bm-cancel">取消</button>
    <button type="button" class="bm-save">保存</button>
  </div>
</div>
"""


MOBILE_UX_ASSETS = """
<style>
  html, body { overscroll-behavior: none; }
</style>
<script>
(function() {
  // iOS Safari page-level pinch.
  document.addEventListener('gesturestart',  function(e){ e.preventDefault(); });
  document.addEventListener('gesturechange', function(e){ e.preventDefault(); });
  document.addEventListener('gestureend',    function(e){ e.preventDefault(); });

  // iOS Safari double-tap zoom. Scope to outside the map so Leaflet's
  // own double-tap-to-zoom-in keeps working.
  var lastTouchEnd = 0;
  document.addEventListener('touchend', function(e){
    if (e.target && e.target.closest && e.target.closest('.leaflet-container')) return;
    var now = Date.now();
    if (now - lastTouchEnd <= 350) e.preventDefault();
    lastTouchEnd = now;
  }, { passive: false });

  // Desktop ctrl/cmd + wheel. Leaflet's wheel zoom doesn't use ctrlKey.
  document.addEventListener('wheel', function(e){
    if (e.ctrlKey || e.metaKey) e.preventDefault();
  }, { passive: false });

  // Desktop cmd/ctrl + 0/-/=/+ keyboard zoom.
  document.addEventListener('keydown', function(e){
    if (!(e.ctrlKey || e.metaKey)) return;
    if (e.key === '=' || e.key === '-' || e.key === '+' || e.key === '0') {
      e.preventDefault();
    }
  });
})();
</script>
<style>
  /* Bottom-sheet popup replacement. The markup lives near </body>; the
     filter JS controls open/close. Default Leaflet popups got cut off at
     mobile viewport edges; this sheet always docks to the bottom and
     scrolls internally. */
  #bs-backdrop {
    position: fixed; inset: 0; z-index: 10001;
    background: rgba(0,0,0,0.35);
    opacity: 0; pointer-events: none;
    transition: opacity 0.22s ease-out;
  }
  #bs-backdrop.bs-open { opacity: 1; pointer-events: auto; }
  #bs-sheet {
    position: fixed; left: 0; right: 0; bottom: 0;
    z-index: 10002;
    max-height: 75vh; max-height: 75dvh;
    background: #fff;
    border-radius: 14px 14px 0 0;
    box-shadow: 0 -8px 24px rgba(0,0,0,0.18);
    transform: translateY(100%);
    transition: transform 0.25s ease-out;
    display: flex; flex-direction: column;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    padding-bottom: env(safe-area-inset-bottom);
  }
  #bs-sheet.bs-open { transform: translateY(0); }
  /* Tablet: cap width and center; still bottom-anchored. The explicit
     width keeps the sheet at a stable size regardless of content — without
     it, `left: 50%; right: auto` makes the sheet shrink-to-fit, so swapping
     the bottom-sheet content (e.g., loading placeholder → full card) would
     make it jump wider. */
  @media (min-width: 700px) {
    #bs-sheet { left: 50%; transform: translate(-50%, 100%);
                width: min(680px, calc(100vw - 32px)); right: auto;
                max-height: 80vh; max-height: 80dvh;
                border-radius: 14px 14px 0 0; }
    #bs-sheet.bs-open { transform: translate(-50%, 0); }
  }
  /* Desktop: roomier sheet so the 2-column popup layout has space. */
  @media (min-width: 1100px) {
    #bs-sheet { width: min(880px, calc(100vw - 32px));
                max-height: 85vh; max-height: 85dvh; }
  }
  #bs-grip {
    position: relative;
    padding: 9px 0 6px; flex-shrink: 0;
    cursor: grab; touch-action: none;
  }
  #bs-grip::before {
    content: ''; display: block;
    width: 38px; height: 4px; margin: 0 auto;
    background: #d1d5db; border-radius: 2px;
  }
  #bs-content {
    overflow-y: auto;
    padding: 0 14px 14px;
    flex: 1 1 auto;
    -webkit-overflow-scrolling: touch;
  }
  /* ===== Restaurant detail card (lives inside #bs-content) ===== */
  .rst-card { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
              font-size: 13px; color: #1f2937; }
  .rst-header { display: flex; justify-content: space-between;
                align-items: flex-start; gap: 8px; margin-bottom: 8px; }
  .rst-title { font-weight: 700; font-size: 16px; flex: 1; min-width: 0;
               line-height: 1.3; }
  .rst-title .rst-rating { color: #c33; margin-left: 4px; font-weight: 700; }
  .rst-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .rst-photos { display: grid; grid-template-columns: repeat(3, 1fr);
                gap: 6px; margin-bottom: 10px; }
  .rst-photos a { display: block; min-width: 0; }
  .rst-photos img { width: 100%; aspect-ratio: 1 / 1; object-fit: cover;
                    border-radius: 6px; display: block; background: #f3f4f6; }
  .rst-genre { color: #4b5563; margin-bottom: 8px; }
  .rst-info { display: grid; grid-template-columns: 1fr;
              gap: 4px 16px; margin-bottom: 8px; }
  .rst-info-row { display: flex; gap: 6px; align-items: baseline;
                  line-height: 1.45; }
  .rst-info-row .rst-label { color: #9ca3af; flex-shrink: 0;
                             font-size: 12px; min-width: 38px; }
  .rst-info-row .rst-value { color: #1f2937; min-width: 0;
                             overflow-wrap: anywhere; }
  .rst-policy { font-size: 12px; color: #6b7280; line-height: 1.5;
                margin-bottom: 8px; }
  .rst-footer { display: flex; justify-content: space-between;
                align-items: center; gap: 8px; flex-wrap: wrap; }
  .rst-footer a { color: #2563eb; text-decoration: none; font-size: 13px; }
  .rst-footer a:hover { text-decoration: underline; }
  .rst-chip { background: #3b9c4f; color: #fff; padding: 2px 8px;
              border-radius: 4px; font-size: 11px; font-weight: 500; }
  .rst-chip.rst-chip-off { background: #9ca3af; }
  .rst-btn { padding: 4px 10px; font-size: 12px; cursor: pointer;
             border: 1px solid #d1d5db; border-radius: 5px;
             background: #f9fafb; color: #1f2937; }
  .rst-btn:hover { background: #f3f4f6; }
  /* Tablet+: tighter title, larger photos */
  @media (min-width: 700px) {
    .rst-card { font-size: 14px; }
    .rst-title { font-size: 18px; }
    .rst-photos { gap: 8px; }
  }
  /* Desktop: 2-column info grid; photos still 3-up but bigger */
  @media (min-width: 1100px) {
    #bs-content { padding: 0 22px 22px; }
    .rst-card { font-size: 14px; }
    .rst-title { font-size: 20px; }
    .rst-info { grid-template-columns: 1fr 1fr; column-gap: 24px; }
  }
</style>"""


# Bottom-sheet DOM. Injected into <body>; populated by openSheet() in the
# filter JS. Backdrop is a sibling so taps fall through to it.
BOTTOM_SHEET_HTML = """
<div id="bs-backdrop"></div>
<div id="bs-sheet" role="dialog" aria-modal="true" aria-hidden="true">
  <div id="bs-grip"></div>
  <div id="bs-content"></div>
</div>
"""


# Service worker source. Written verbatim to docs/sw.js at build time after
# __BUILD_VERSION__ is substituted. Strategy by request type:
#   same-origin HTML / nav    → network-first   (so redeploys land quickly)
#   same-origin JSON / GeoJSON / JS → cache-first   (busted by version bump)
#   third-party tiles / CDN   → stale-while-revalidate
#   anything else (Nominatim, Gist API) → not intercepted, browser default
# Each build bumps the cache name via VERSION, so activate() purges the old
# cache and the next fetch repopulates with fresh data.
SW_JS_TEMPLATE = r"""// Auto-generated by src/tabelog/scrape/map.py — do not edit by hand.
// Build version: __BUILD_VERSION__
const VERSION = '__BUILD_VERSION__';
const CACHE = 'tabelog-' + VERSION;

self.addEventListener('install', () => { self.skipWaiting(); });

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    // sw.js itself must always come from the network so updates land.
    if (url.pathname.endsWith('/sw.js')) return;
    if (req.mode === 'navigate' || url.pathname.endsWith('.html')) {
      event.respondWith(networkFirst(req));
    } else {
      event.respondWith(cacheFirst(req));
    }
    return;
  }

  // Tiles + CDN assets we serve from cache on revisit but refresh in the
  // background. Anything outside this list (Nominatim search, GitHub Gist
  // sync) passes straight through to the browser default.
  if (/(\.tile\.openstreetmap\.org$)|(^[a-c]\.tile\.)/i.test(url.hostname) ||
      url.hostname === 'emojicdn.elk.sh' ||
      url.hostname === 'cdn.jsdelivr.net' ||
      url.hostname === 'unpkg.com') {
    event.respondWith(staleWhileRevalidate(req));
  }
});

// Opaque (cross-origin no-cors) responses report ok=false / status=0 but
// we still want them cached for revisits.
function cacheable(resp) {
  return !!resp && (resp.ok || resp.type === 'opaque');
}

async function cacheFirst(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req);
  if (cached) return cached;
  const fresh = await fetch(req);
  if (cacheable(fresh)) cache.put(req, fresh.clone());
  return fresh;
}

async function networkFirst(req) {
  const cache = await caches.open(CACHE);
  try {
    const fresh = await fetch(req);
    if (cacheable(fresh)) cache.put(req, fresh.clone());
    return fresh;
  } catch (e) {
    const cached = await cache.match(req);
    if (cached) return cached;
    throw e;
  }
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req);
  const fresh = fetch(req).then(resp => {
    if (cacheable(resp)) cache.put(req, resp.clone());
    return resp;
  }).catch(() => cached);
  return cached || fresh;
}
"""


FILTER_JS_TEMPLATE = r"""
<script>
(function() {
  var EMBEDDED_BOOKMARKS = __BOOKMARKS__;
  // bucket → marker halo color, and bucket name → emoji glyph. Inlined from
  // PRICE_BUCKETS / GENRE_EMOJI in map_data.py so each restaurants.json row
  // only needs to carry the small keys (bucket / categories[0]), not the
  // resolved color / emoji. ~30 bytes saved per row × 9800 rows ≈ 300 KB
  // off restaurants.json on every cold load.
  var BUCKET_COLOR = __BUCKET_COLORS__;
  var GENRE_EMOJI  = __GENRE_EMOJI__;
  // Service worker registration. Caches restaurants.json, popups.json,
  // transit GeoJSON, map tiles + emoji CDN on first fetch so repeat visits
  // (and second-tab loads) skip the network for the heavy bits. Failures
  // are non-fatal — the page works without it (e.g. file:// preview).
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function() {
      navigator.serviceWorker.register('./sw.js').catch(function(err) {
        console.warn('[tabelog] SW registration failed:', err);
      });
    });
  }

  // ===== Lazy popup loader =====
  // The rendered popup HTML for all restaurants lives in docs/data/popups.json
  // (one entry per Tabelog detail_url). It's ~4 MB gzipped, so we don't pull
  // it on boot — only when the user taps the first marker. After that the
  // map serves popups instantly from memory. The single shared promise means
  // a second tap during the first fetch reuses it rather than racing.
  var popupsMap = null;
  var popupsPromise = null;
  function loadPopups() {
    if (popupsMap) return Promise.resolve(popupsMap);
    if (popupsPromise) return popupsPromise;
    popupsPromise = fetch('data/popups.json', {cache: 'force-cache'})
      .then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function(j) { popupsMap = j; return j; })
      .catch(function(e) {
        console.error('[tabelog] popups.json load failed:', e);
        popupsPromise = null;
        throw e;
      });
    return popupsPromise;
  }

  // ===== Apple-style emoji rendering via emojicdn =====
  // Windows ships no flag glyphs in its system font, so we swap every emoji
  // on the page for an <img> from emojicdn.elk.sh (?style=apple). The genre
  // marker emoji is baked in directly via emojiImg(); everything else
  // (popups, button labels, attraction divIcons) is caught by a
  // MutationObserver that scans subtrees as they're inserted.
  var EMOJI_RE = /[\u{1F1E6}-\u{1F1FF}][\u{1F1E6}-\u{1F1FF}]|\p{Emoji_Presentation}|\p{Emoji}\uFE0F/gu;
  function emojiImg(m, extraStyle) {
    return '<img src="https://emojicdn.elk.sh/' + encodeURIComponent(m) +
           '?style=apple" alt="' + m + '" draggable="false" ' +
           'style="height:1em;width:1em;vertical-align:-0.15em;' +
           'display:inline-block;' + (extraStyle || '') + '">';
  }
  function emojiHtml(s) {
    if (!s) return '';
    return String(s).replace(EMOJI_RE, function(m) { return emojiImg(m); });
  }
  function setEmojiHtml(el, text) {
    if (el) el.innerHTML = emojiHtml(text);
  }
  function emojify(root) {
    if (!root || root.nodeType !== 1) return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function(n) {
        var p = n.parentNode;
        if (!p) return NodeFilter.FILTER_REJECT;
        var t = p.tagName;
        if (t === 'SCRIPT' || t === 'STYLE' || t === 'TEXTAREA' || t === 'INPUT') {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var nodes = [], n;
    while ((n = walker.nextNode())) nodes.push(n);
    nodes.forEach(function(tn) {
      var v = tn.nodeValue;
      if (!v) return;
      var html = v.replace(EMOJI_RE, function(m) { return emojiImg(m); });
      if (html === v) return;
      var span = document.createElement('span');
      span.innerHTML = html;
      tn.replaceWith(span);
    });
  }
  // Attach a MutationObserver to one container so any future emoji-bearing
  // content under it gets swapped to Apple PNGs. Exposed so initMap() can
  // hook the Leaflet popup pane once Leaflet has created it.
  function observeForEmoji(root) {
    if (!root) return;
    new MutationObserver(function(muts) {
      for (var i = 0; i < muts.length; i++) {
        var added = muts[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var nd = added[j];
          if (nd.nodeType === 1) emojify(nd);
          else if (nd.nodeType === 3 && nd.parentNode) emojify(nd.parentNode);
        }
      }
    }).observe(root, {childList: true, subtree: true});
  }
  function startEmojiObserver() {
    // One-shot pass over the static page (filter panel, FAB labels, modal
    // titles…) — these never change after load.
    emojify(document.body);
    // Narrow ongoing observers only on the containers that mutate with
    // emoji-bearing HTML at runtime. The previous wider `document.body`
    // observer caught every marker insertion too, wasting one TreeWalker
    // pass per marker on each pan — marker divIcons already pre-swap their
    // emoji at construction (see makeIcon → emojiImg), so they don't need
    // the observer. Leaflet popups (search result, right-click "加入收藏")
    // live under .leaflet-popup-pane, which initMap() hooks once Leaflet
    // has built its panes.
    observeForEmoji(document.getElementById('bs-content'));
    observeForEmoji(document.getElementById('bm-modal'));
    observeForEmoji(document.getElementById('ss-list'));
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startEmojiObserver);
  } else {
    startEmojiObserver();
  }

  // ===== GitHub Gist sync layer =====
  //
  // Config (Gist ID + PAT) is per-device, stored in localStorage. The state
  // (favorites/blacklist sets) is also cached locally so the page works
  // offline / before the first sync. With Gist configured, the page pulls
  // on load + tab-focus + every 60s, and pushes (debounced 500ms) on toggle.
  // Last-writer-wins: we don't merge concurrent edits, but for a small
  // group this is fine — the next pull catches it.
  var CFG_KEY = 'omakase_gist_config';
  var CACHE_KEY = 'omakase_state_cache_v2';
  var GIST_API = 'https://api.github.com/gists/';

  function loadConfig() {
    try { return JSON.parse(localStorage.getItem(CFG_KEY) || '{}'); }
    catch (_) { return {}; }
  }
  function saveConfig(c) { localStorage.setItem(CFG_KEY, JSON.stringify(c)); }
  function clearConfig() { localStorage.removeItem(CFG_KEY); }

  function loadCache() {
    try {
      var d = JSON.parse(localStorage.getItem(CACHE_KEY) || '{}');
      return {fav: d.fav || null, black: d.black || null, dirty: !!d.dirty};
    } catch (_) { return {fav: null, black: null, dirty: false}; }
  }
  // Persists state + the dirty flag. Storing dirty across reloads is the
  // whole point — without it, an unpushed change would be silently
  // overwritten by the next pull after a refresh.
  function saveCache(state, dirtyFlag) {
    localStorage.setItem(CACHE_KEY, JSON.stringify({
      fav: Array.from(state.fav),
      black: Array.from(state.black),
      dirty: !!dirtyFlag,
    }));
  }

  function gistHeaders(pat) {
    var h = {'Accept': 'application/vnd.github+json'};
    if (pat) h['Authorization'] = 'Bearer ' + pat;
    return h;
  }
  function parseGistFiles(json) {
    function setFromFile(f) {
      if (!f || typeof f.content !== 'string') return null;
      try {
        var arr = JSON.parse(f.content);
        return Array.isArray(arr) ? new Set(arr) : null;
      } catch (_) { return null; }
    }
    function arrayFromFile(f) {
      if (!f || typeof f.content !== 'string') return null;
      try {
        var arr = JSON.parse(f.content);
        return Array.isArray(arr) ? arr : null;
      } catch (_) { return null; }
    }
    var files = json.files || {};
    return {
      fav: setFromFile(files['favorites.json']),
      black: setFromFile(files['blacklist.json']),
      // null when the gist doesn't have this file yet — preserves local
      // edits the first time a device starts syncing.
      bookmarks: arrayFromFile(files['bookmarks.json']),
    };
  }

  function initMap(data) {
    var mapEl = document.querySelector('.folium-map');
    if (!mapEl) { setTimeout(function(){ initMap(data); }, 50); return; }
    // Find the leaflet map object that folium attached to this element.
    var mapId = mapEl.id;
    var map = window[mapId];
    if (!map) { setTimeout(function(){ initMap(data); }, 50); return; }
    if (typeof L === 'undefined' || !L.markerClusterGroup) { setTimeout(function(){ initMap(data); }, 50); return; }
    if (!L.control.locate) { setTimeout(function(){ initMap(data); }, 50); return; }

    // Late-bound emoji observer for Leaflet popups: search-result temp
    // marker and the right-click "加入收藏" popup both inject HTML into
    // .leaflet-popup-pane, which only exists after the map initializes.
    var popupPane = map.getPane && map.getPane('popupPane');
    if (popupPane) observeForEmoji(popupPane);

    // ===== Persisted map view =====
    // Restore last center+zoom before any tiles render, then track every
    // moveend (fires once per pan/zoom gesture, not per frame).
    var STATE_KEY_VIEW = 'tabelog.mapView';
    try {
      var savedView = JSON.parse(localStorage.getItem(STATE_KEY_VIEW) || 'null');
      if (savedView && typeof savedView.lat === 'number'
                    && typeof savedView.lon === 'number'
                    && typeof savedView.zoom === 'number') {
        map.setView([savedView.lat, savedView.lon], savedView.zoom, {animate: false});
      }
    } catch (e) {}
    map.on('moveend', function() {
      try {
        var c = map.getCenter();
        localStorage.setItem(STATE_KEY_VIEW, JSON.stringify({
          lat: c.lat, lon: c.lng, zoom: map.getZoom()
        }));
      } catch (e) {}
    });
    // iOS Safari bfcache restore: the page comes back with stale container
    // dimensions, so tiles render at the wrong size (often a gray band on
    // the right edge or below the address bar). invalidateSize() forces
    // Leaflet to re-measure; if bounds shift, moveend fires naturally and
    // the marker recompute follows.
    window.addEventListener('pageshow', function(e) {
      if (e.persisted) map.invalidateSize();
    });

    // Live geolocation: click once to fly to current position, click again
    // to stop. The plugin's own top-left button is hidden via CSS in
    // LOCATE_ASSETS; we drive it from the bottom-right locate FAB so the
    // control sits alongside the layer toggles instead of being a stray
    // Leaflet UI in the corner.
    var locateCtl = L.control.locate({
      position: 'topleft',
      flyTo: true,
      setView: 'untilPan',
      initialZoomLevel: 16,
      keepCurrentZoomLevel: false,
      cacheLocation: true,
      showCompass: true,
      drawCircle: true,
      drawMarker: true,
      locateOptions: {enableHighAccuracy: true, maximumAge: 5000, watch: false},
      strings: {
        title: '显示我的位置',
        popup: '你在约 {distance} {unit} 范围内',
        outsideMapBoundsMsg: '当前位置在地图范围之外'
      }
    }).addTo(map);
    var locateFab = document.getElementById('fab-locate');
    if (locateFab) {
      locateFab.addEventListener('click', function() {
        // _active is the plugin's "currently tracking" flag. Toggle so a
        // second tap turns it off, matching Google Maps' behavior.
        if (locateCtl._active) locateCtl.stop(); else locateCtl.start();
      });
      // Paint the FAB blue while the plugin is tracking. The plugin emits
      // these events on the map; locatedeactivate fires on .stop() and on
      // permission denial.
      map.on('locateactivate',   function() { locateFab.classList.add('locating'); });
      map.on('locatedeactivate', function() { locateFab.classList.remove('locating'); });
    }

    // ===== FAB layer toggles: transit overlay + attractions =====
    // Vector transit layer rendered from precomputed docs/transit/japan.geojson
    // (extract_japan_transit.py + transit_postprocess.py from a Geofabrik
    // OSM extract). One layer instance, two FABs: 长途 (新干线 + JR 长途)
    // and 市内 (subway + 私铁 + tram + ...) — each toggles a bucket on the
    // same layer via setVisibleBuckets. Loaded lazily on first toggle-on.
    // LOD-aware loading: 'low' (long-haul only, ~1 MB gzipped) for the
    // country-scale view, 'mid' (~2 MB) at regional zoom, 'high' (~4 MB,
    // full detail) once the user is at street-level. Breaks line up with
    // the minZ thresholds in transit-layer.js — subway / tram / monorail
    // only appear at z>=11-12, so the 'mid' file kicks in just before
    // they become visible.
    var transitLayer = (typeof L.transitLayer === 'function')
      ? L.transitLayer({
          lodUrls: {
            low:  'transit/japan-low.geojson',
            mid:  'transit/japan-mid.geojson',
            high: 'transit/japan.geojson'
          },
          lodBreaks: { mid: 9, high: 14 },
          opacity: 0.4,
          casingOpacity: 0.2
        })
      : null;
    if (!transitLayer) {
      console.warn('[tabelog] L.transitLayer unavailable — transit-layer.js failed to load');
    }
    // The bucket-toggle FABs share the layer's add/remove lifecycle. The
    // layer must be on the map when EITHER bucket is on (so we don't pay
    // re-load cost flipping them); the layer is removed only when both are
    // off. setVisibleBuckets handles intra-layer culling.
    var transitBuckets = { long: false, city: false };
    function applyTransitBucket(btn, key, on) {
      if (!btn) return;
      transitBuckets[key] = on;
      if (btn) {
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
      if (!transitLayer) return;
      var anyOn = transitBuckets.long || transitBuckets.city;
      if (anyOn) {
        transitLayer.setVisibleBuckets({ long: transitBuckets.long, city: transitBuckets.city });
        if (!map.hasLayer(transitLayer)) map.addLayer(transitLayer);
      } else {
        if (map.hasLayer(transitLayer)) map.removeLayer(transitLayer);
      }
    }
    function wireTransitFab(btnId, key, storageKey, defaultOn) {
      var btn = document.getElementById(btnId);
      if (!btn) return;
      var on = defaultOn;
      try {
        var v = localStorage.getItem(storageKey);
        if (v !== null) on = (v === '1');
      } catch (e) {}
      applyTransitBucket(btn, key, on);
      btn.addEventListener('click', function() {
        on = !on;
        applyTransitBucket(btn, key, on);
        try { localStorage.setItem(storageKey, on ? '1' : '0'); } catch (e) {}
      });
    }
    wireTransitFab('fab-transit-long', 'long', 'tabelog.showTransitLong', false);
    wireTransitFab('fab-transit-city', 'city', 'tabelog.showTransitCity', false);

    // Find the attractions FeatureGroup among map._layers. Duck-type rather
    // than `instanceof L.MarkerClusterGroup` because that class symbol can
    // throw if the plugin hasn't fully exposed it yet — and any throw here
    // would skip the FAB wiring below. A MarkerClusterGroup has the
    // `refreshClusters` method; a plain FeatureGroup doesn't.
    var attractionsLayer = null;
    for (var lid in map._layers) {
      var lyr = map._layers[lid];
      if (typeof lyr.eachLayer !== 'function') continue;          // not a layer group
      if (typeof lyr.refreshClusters === 'function') continue;    // is a cluster
      if (!lyr._layers || Object.keys(lyr._layers).length === 0) continue;
      attractionsLayer = lyr;
      break;
    }

    function applyToggle(btn, layers, on) {
      if (!btn) return;
      var arr = Array.isArray(layers) ? layers : [layers];
      arr.forEach(function(layer) {
        if (!layer) return;
        if (on) {
          if (!map.hasLayer(layer)) map.addLayer(layer);
        } else {
          if (map.hasLayer(layer)) map.removeLayer(layer);
        }
      });
      if (on) {
        btn.classList.add('active');
        btn.setAttribute('aria-pressed', 'true');
      } else {
        btn.classList.remove('active');
        btn.setAttribute('aria-pressed', 'false');
      }
    }

    // `layers` can be a single layer or an array — used by fab-attractions to
    // co-toggle the folium-curated layer + the user-added attractions layer.
    function wireFab(btnId, layers, storageKey, defaultOn) {
      var btn = document.getElementById(btnId);
      var arr = Array.isArray(layers) ? layers : [layers];
      if (!btn || arr.every(function(l){ return !l; })) return;
      var on = defaultOn;
      try {
        var v = localStorage.getItem(storageKey);
        if (v !== null) on = (v === '1');
      } catch (e) {}
      applyToggle(btn, arr, on);
      btn.addEventListener('click', function() {
        on = !on;
        applyToggle(btn, arr, on);
        try { localStorage.setItem(storageKey, on ? '1' : '0'); } catch (e) {}
      });
    }
    // ===== 我的收藏 + 用户自添景点 (user-pinned places) =====
    // One JSON store (tabelog.bookmarks / bookmarks.json on the Gist) for
    // both kinds; each entry carries a `category` field — 'bookmark' (under
    // the ⭐收藏 FAB) or 'attraction' (under the 🗾景点 FAB, alongside the
    // curated data/attractions.csv set). The 景点 FAB toggles both layers
    // together; the 收藏 FAB toggles only the bookmarks layer.
    var BM_KEY = 'tabelog.bookmarks';
    var bookmarks = (function() {
      try {
        var raw = localStorage.getItem(BM_KEY);
        if (raw === null) {
          // First visit on this device — seed from the embedded baseline
          // and persist it so subsequent edits anchor against that copy.
          var seed = EMBEDDED_BOOKMARKS.slice();
          localStorage.setItem(BM_KEY, JSON.stringify(seed));
          return seed;
        }
        var arr = JSON.parse(raw);
        return Array.isArray(arr) ? arr : [];
      } catch (_) { return EMBEDDED_BOOKMARKS.slice(); }
    })();

    var bookmarksLayer = L.featureGroup();        // category === 'bookmark'
    var userAttractionsLayer = L.featureGroup();  // category === 'attraction'
    // Stores both the marker and its parent layer so removal works without
    // re-checking the entry's category (which the user could have changed
    // by deleting + re-adding, etc.).
    var bmMarkerById = {};

    function saveBookmarks() {
      try { localStorage.setItem(BM_KEY, JSON.stringify(bookmarks)); } catch (_) {}
    }
    function bookmarkIconHtml(emoji, name, emojiSize, labelSize) {
      // 22px / 10px for 收藏, 30px / 11px for user-added 景点 — the latter
      // matches the build-time curated attractions so user additions blend
      // visually with the existing tourist anchors.
      var es = emojiSize || 22;
      var ls = labelSize || 10;
      return '<div style="position:relative;transform:translate(-50%,-100%);' +
                       'text-align:center;width:max-content;">' +
               '<div style="font-size:' + es + 'px;line-height:1;' +
                          'filter:drop-shadow(0 1px 2px rgba(0,0,0,0.45));">' +
                 (emoji || '📍') +
               '</div>' +
               '<div style="font-size:' + ls + 'px;font-weight:700;color:#1f2937;' +
                          'background:rgba(255,255,255,0.92);' +
                          'padding:1px 5px;border-radius:4px;margin-top:1px;' +
                          'white-space:nowrap;' +
                          'box-shadow:0 1px 2px rgba(0,0,0,0.2);">' +
                 escapeHtml(name || '') +
               '</div>' +
             '</div>';
    }
    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, function(c) {
        return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
      });
    }
    // Render the restaurant card from structured data. Mirrors the old
    // Python fmt_popup() layout. `d` is the restaurants.json entry (name,
    // rating, categories, bookable, detail_url already there); `p` is the
    // popups.json positional array:
    //   [genre, dinner, lunch, seat, station, address, policy, photos]
    function renderPopup(d, p) {
      if (!d || !p) return '';
      var genre   = p[0], dinner  = p[1], lunch   = p[2], seat    = p[3],
          station = p[4], addr    = p[5], policy  = p[6], photos  = p[7] || [];
      var name    = escapeHtml(d.name || '');
      var rating  = d.rating == null ? '' : escapeHtml(d.rating);
      var bucket  = (d.categories && d.categories[0]) || '';
      var url     = escapeHtml(d.detail_url || '');
      var dinnerS = (dinner != null) ? '¥' + dinner : 'NA';
      var lunchS  = (lunch  != null) ? '¥' + lunch  : 'NA';
      var photoHtml = '';
      if (photos.length) {
        photoHtml = '<div class="rst-photos">' + photos.map(function(big) {
          var thumb = String(big).replace('640x640_rect_', '150x150_square_');
          return '<a href="' + escapeHtml(big) + '" target="_blank" rel="noopener">'
               + '<img src="' + escapeHtml(thumb) + '" loading="lazy" alt="" '
               + 'onerror="this.parentElement.style.display=\'none\'"></a>';
        }).join('') + '</div>';
      }
      var chip = d.bookable
        ? '<span class="rst-chip">可Tabelog预约</span>'
        : '<span class="rst-chip rst-chip-off">不可Tabelog预约</span>';
      return '<div class="rst-card">'
        + '<div class="rst-header">'
          + '<div class="rst-title">' + name
            + '<span class="rst-rating">★' + rating + '</span></div>'
          + '<div class="rst-actions">'
            + '<button class="ff-fav-btn rst-btn" data-url="' + url + '">'
              + '<span class="ff-fav-label">☆ 收藏</span></button>'
            + '<button class="ff-black-btn rst-btn" data-url="' + url + '">'
              + '<span class="ff-black-label">🚫 弃用</span></button>'
          + '</div>'
        + '</div>'
        + photoHtml
        + '<div class="rst-genre">' + escapeHtml(genre) + ' / ' + escapeHtml(bucket) + '</div>'
        + '<div class="rst-info">'
          + '<div class="rst-info-row"><span class="rst-label">晚</span><span class="rst-value">' + escapeHtml(dinnerS) + '</span></div>'
          + '<div class="rst-info-row"><span class="rst-label">车站</span><span class="rst-value">📍 ' + escapeHtml(station) + '</span></div>'
          + '<div class="rst-info-row"><span class="rst-label">午</span><span class="rst-value">' + escapeHtml(lunchS) + '</span></div>'
          + '<div class="rst-info-row"><span class="rst-label">座位</span><span class="rst-value">' + (seat ? escapeHtml(seat) : '—') + '</span></div>'
          + '<div class="rst-info-row"><span class="rst-label">地址</span><span class="rst-value">' + escapeHtml(addr) + '</span></div>'
        + '</div>'
        + (policy ? '<div class="rst-policy">' + escapeHtml(policy) + '</div>' : '')
        + '<div class="rst-footer">'
          + chip
          + '<a href="' + url + '" target="_blank" rel="noopener">Tabelog 详情 ↗</a>'
        + '</div>'
      + '</div>';
    }
    function renderBookmark(bm) {
      var isAttraction = bm.category === 'attraction';
      var targetLayer = isAttraction ? userAttractionsLayer : bookmarksLayer;
      var icon = L.divIcon({
        className: 'empty',
        iconSize: [0, 0],
        iconAnchor: [0, 0],
        html: bookmarkIconHtml(bm.emoji, bm.name,
                               isAttraction ? 30 : 22,
                               isAttraction ? 11 : 10)
      });
      var m = L.marker([bm.lat, bm.lon], {icon: icon});
      m.bindTooltip(bm.name || '', {sticky: true});
      m.on('click', function() { openBookmarkPopup(bm, m); });
      m.addTo(targetLayer);
      bmMarkerById[bm.id] = {marker: m, layer: targetLayer};
    }
    function removeBookmarkMarker(bm) {
      var entry = bmMarkerById[bm.id];
      if (entry) {
        entry.layer.removeLayer(entry.marker);
        delete bmMarkerById[bm.id];
      }
    }
    bookmarks.forEach(renderBookmark);

    function openBookmarkPopup(bm, marker) {
      var coord = bm.lat.toFixed(6) + ', ' + bm.lon.toFixed(6);
      var delLabel = (bm.category === 'attraction') ? '删除景点' : '删除收藏';
      // Larger popup offset for attractions because they render at 30px
      // instead of 22px — keeps the speech bubble tip from overlapping
      // the emoji.
      var offY = (bm.category === 'attraction') ? -30 : -22;
      var html =
        '<div style="font:13px sans-serif;text-align:center;min-width:160px;">' +
          '<div style="font-weight:700;margin-bottom:4px;">' +
            (bm.emoji || '📍') + ' ' + escapeHtml(bm.name || '') +
          '</div>' +
          '<div style="font-family:monospace;font-size:11px;color:#6b7280;' +
                      'margin-bottom:8px;">' + coord + '</div>' +
          '<button id="bm-del" ' +
                  'style="padding:4px 12px;font-size:12px;cursor:pointer;' +
                         'border:1px solid #fecaca;border-radius:4px;' +
                         'background:#fef2f2;color:#b91c1c;font-weight:600;">' +
                  delLabel + '</button>' +
        '</div>';
      L.popup({offset: [0, offY]})
        .setLatLng([bm.lat, bm.lon])
        .setContent(html)
        .openOn(map);
      setTimeout(function() {
        var del = document.getElementById('bm-del');
        if (!del) return;
        del.addEventListener('click', function() {
          var i = bookmarks.findIndex(function(x){ return x.id === bm.id; });
          if (i >= 0) bookmarks.splice(i, 1);
          removeBookmarkMarker(bm);
          saveBookmarks();
          schedulePush();
          map.closePopup();
        });
      }, 0);
    }

    wireFab('fab-attractions',
            [attractionsLayer, userAttractionsLayer],
            'tabelog.showAttractions', true);
    wireFab('fab-bookmarks', bookmarksLayer, 'tabelog.showBookmarks', true);

    // ----- 加入收藏 modal -----
    var bmModal      = document.getElementById('bm-modal');
    var bmBackdrop   = document.getElementById('bm-backdrop');
    var bmCoordEl    = document.getElementById('bm-coord');
    var bmTitleEl    = document.getElementById('bm-modal-title');
    var bmNameInput  = document.getElementById('bm-name');
    var bmEmojiInput = document.getElementById('bm-emoji');
    var bmEmojiMore  = document.getElementById('bm-emoji-more');
    var bmPicker     = document.getElementById('bm-emoji-picker');
    var bmError      = document.getElementById('bm-error');
    var bmKindBtns   = bmModal.querySelectorAll('.bm-kind-seg button');
    var bmPending    = null;          // {lat, lng}
    var bmKind       = 'bookmark';    // 'bookmark' | 'attraction'

    function bmSetKind(kind) {
      bmKind = (kind === 'attraction') ? 'attraction' : 'bookmark';
      bmKindBtns.forEach(function(b) {
        var on = b.getAttribute('data-kind') === bmKind;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
      });
      bmTitleEl.textContent = (bmKind === 'attraction') ? '加入景点' : '加入收藏';
    }
    bmKindBtns.forEach(function(btn) {
      btn.addEventListener('click', function() {
        bmSetKind(btn.getAttribute('data-kind'));
      });
    });

    // EMOJI_RE matches single emoji codepoints; ZWJ / VS / skin-tone
    // modifiers stitch sequences together (family, profession, tone). To
    // judge "is the whole string emoji-only", strip both and check nothing
    // remains. Pure-ASCII / Han / random punctuation will leave residue.
    function isPureEmoji(s) {
      if (!s) return false;
      // ZWJ U+200D, variation selectors U+FE0E/FE0F, Fitzpatrick skin tones
      // U+1F3FB–U+1F3FF. EMOJI_RE catches the base glyphs; this regex
      // catches the glue and modifiers that sit between/after them.
      var stripped = s.replace(EMOJI_RE, '')
                      .replace(/[‍︎️\u{1F3FB}-\u{1F3FF}]/gu, '');
      return stripped.trim() === '';
    }
    function bmShowError(msg) { bmError.textContent = msg || ''; }
    function bmFlagInput(input) {
      input.focus();
      input.style.borderColor = '#dc2626';
      setTimeout(function(){ input.style.borderColor = ''; }, 1200);
    }

    function bmCollapsePicker() {
      bmPicker.classList.remove('bm-show');
      bmEmojiMore.setAttribute('aria-expanded', 'false');
      bmEmojiMore.textContent = '🔽';
    }

    function openBookmarkModal(latlng, prefillName) {
      bmPending = {lat: latlng.lat, lng: latlng.lng};
      bmCoordEl.textContent = latlng.lat.toFixed(6) + ', ' + latlng.lng.toFixed(6);
      bmNameInput.value = prefillName || '';
      bmEmojiInput.value = '📍';
      bmSetKind('bookmark');           // reset default each open
      bmShowError('');
      bmCollapsePicker();
      bmBackdrop.classList.add('bm-open');
      bmModal.classList.add('bm-open');
      bmModal.setAttribute('aria-hidden', 'false');
      // Pull focus into the name field after the open transition starts so
      // mobile keyboards pop up immediately.
      setTimeout(function(){ bmNameInput.focus(); }, 50);
    }
    function closeBookmarkModal() {
      bmBackdrop.classList.remove('bm-open');
      bmModal.classList.remove('bm-open');
      bmModal.setAttribute('aria-hidden', 'true');
      bmCollapsePicker();
      bmShowError('');
      bmPending = null;
    }
    function commitBookmark() {
      if (!bmPending) return;
      var name = (bmNameInput.value || '').trim();
      var emojiRaw = (bmEmojiInput.value || '').trim();
      if (!name) {
        bmShowError('名称不能为空');
        bmFlagInput(bmNameInput);
        return;
      }
      if (emojiRaw && !isPureEmoji(emojiRaw)) {
        bmShowError('图标必须是 emoji（试试 😀 按钮里的选择器）');
        bmFlagInput(bmEmojiInput);
        return;
      }
      var emoji = emojiRaw || '📍';
      bmShowError('');
      var bm = {
        id: 'bm-' + Date.now().toString(36) + '-' +
            Math.random().toString(36).slice(2, 7),
        name: name,
        emoji: emoji,
        lat: bmPending.lat,
        lon: bmPending.lng,
        category: bmKind
      };
      bookmarks.push(bm);
      renderBookmark(bm);
      saveBookmarks();
      schedulePush();
      // If a search-temp 📍 sits at this exact spot it's the one being
      // bookmarked — drop it so the bookmark emoji doesn't stack on top.
      // Coord match instead of a "source" flag keeps the right-click path
      // from accidentally clearing an unrelated search pin elsewhere.
      if (ssTempMarker) {
        var t = ssTempMarker.getLatLng();
        if (Math.abs(t.lat - bm.lat) < 1e-7 && Math.abs(t.lng - bm.lon) < 1e-7) {
          ssRemoveTempMarker();
        }
      }
      // Make sure the right layer is visible after adding — if the user
      // has the corresponding FAB toggled off, surface the pin by
      // re-enabling it.
      var fabId = (bm.category === 'attraction') ? 'fab-attractions' : 'fab-bookmarks';
      var fab = document.getElementById(fabId);
      if (fab && fab.getAttribute('aria-pressed') !== 'true') fab.click();
      closeBookmarkModal();
    }
    bmBackdrop.addEventListener('click', closeBookmarkModal);
    bmModal.querySelector('.bm-close').addEventListener('click', closeBookmarkModal);
    bmModal.querySelector('.bm-cancel').addEventListener('click', closeBookmarkModal);
    bmModal.querySelector('.bm-save').addEventListener('click', commitBookmark);
    // Quick-pick chips live in their own block; the 😀 picker button is
    // a sibling of that block and is wired separately to toggle bmPicker.
    bmModal.querySelectorAll('.bm-quick-list button[data-emoji]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        bmEmojiInput.value = btn.getAttribute('data-emoji') || '📍';
        bmShowError('');
      });
    });
    bmEmojiMore.addEventListener('click', function() {
      var nowOpen = !bmPicker.classList.contains('bm-show');
      bmPicker.classList.toggle('bm-show', nowOpen);
      bmEmojiMore.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
      bmEmojiMore.textContent = nowOpen ? '🔼' : '🔽';
    });
    // emoji-picker-element fires 'emoji-click' with detail.unicode as the
    // rendered glyph. Setting the input + closing the picker mirrors what
    // most users expect after a pick.
    bmPicker.addEventListener('emoji-click', function(ev) {
      var u = ev && ev.detail && ev.detail.unicode;
      if (!u) return;
      bmEmojiInput.value = u;
      bmShowError('');
      bmEmojiInput.style.borderColor = '';
      bmCollapsePicker();
    });
    bmNameInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); commitBookmark(); }
      else if (e.key === 'Escape') { e.preventDefault(); closeBookmarkModal(); }
    });
    bmEmojiInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); commitBookmark(); }
      else if (e.key === 'Escape') { e.preventDefault(); closeBookmarkModal(); }
    });

    // Right-click (desktop) / long-press (mobile) on any blank spot of the
    // map -> popup with the coords + a copy button + a 加入收藏 button.
    // Leaflet fires 'contextmenu' for both gestures so we only need one handler.
    map.on('contextmenu', function(e) {
      var s = e.latlng.lat.toFixed(6) + ', ' + e.latlng.lng.toFixed(6);
      var html =
        '<div style="font:13px sans-serif;text-align:center;min-width:160px;">' +
          '<div style="font-family:monospace;margin-bottom:8px;">' + s + '</div>' +
          '<div style="display:flex;gap:6px;justify-content:center;">' +
            '<button id="ff-copy-coord" ' +
                    'style="padding:4px 10px;font-size:12px;cursor:pointer;' +
                    'border:1px solid #d1d5db;border-radius:4px;background:#f9fafb;">' +
                    '复制</button>' +
            '<button id="ff-add-bm" ' +
                    'style="padding:4px 10px;font-size:12px;cursor:pointer;' +
                    'border:1px solid #2563eb;border-radius:4px;' +
                    'background:#2563eb;color:#fff;font-weight:600;">' +
                    '⭐ 加入收藏</button>' +
          '</div>' +
        '</div>';
      L.popup().setLatLng(e.latlng).setContent(html).openOn(map);
      setTimeout(function() {
        var btn = document.getElementById('ff-copy-coord');
        if (btn) {
          btn.addEventListener('click', function() {
            var done = function() { btn.textContent = '已复制 ✓'; };
            var fallback = function() {
              var t = document.createElement('textarea');
              t.value = s; t.style.position = 'fixed'; t.style.opacity = '0';
              document.body.appendChild(t); t.select();
              try { document.execCommand('copy'); } catch (_) {}
              document.body.removeChild(t); done();
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(s).then(done).catch(fallback);
            } else {
              fallback();
            }
          });
        }
        var add = document.getElementById('ff-add-bm');
        if (add) {
          add.addEventListener('click', function() {
            map.closePopup();
            openBookmarkModal(e.latlng);
          });
        }
      }, 0);
    });

    // ===== Nominatim search =====
    // Browser → https://nominatim.openstreetmap.org/search?...&format=jsonv2
    // CORS-open. Policy is ≤1 req/s — JS-side 300ms debounce keeps us well
    // under that for normal typing. viewbox bounds Japan so "Tokyo" doesn't
    // match the US street. zh/ja in accept-language for nicer labels.
    var ssBox     = document.getElementById('ss-box');
    var ssWrap    = document.getElementById('ss-input-wrap');
    var ssInput   = document.getElementById('ss-input');
    var ssClear   = document.getElementById('ss-clear');
    var ssList    = document.getElementById('ss-list');
    var ssDebounce = null;
    var ssReqSeq  = 0;
    var ssTempMarker = null;

    function ssRemoveTempMarker() {
      if (ssTempMarker) { map.removeLayer(ssTempMarker); ssTempMarker = null; }
    }

    function ssRender(items) {
      ssList.innerHTML = '';
      if (!items) { ssList.classList.remove('open'); return; }
      if (items.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'ss-row ss-empty';
        empty.textContent = '没有匹配的结果';
        ssList.appendChild(empty);
        ssList.classList.add('open');
        return;
      }
      items.forEach(function(it) {
        var row = document.createElement('div');
        row.className = 'ss-row';
        row.setAttribute('role', 'option');

        var icon = document.createElement('span');
        icon.className = 'ss-icon';
        icon.textContent = '📍';

        var text = document.createElement('div');
        text.className = 'ss-text';
        var n = document.createElement('div');
        n.className = 'ss-name';
        n.textContent = it.name;
        var a = document.createElement('div');
        a.className = 'ss-addr';
        a.textContent = it.address;
        text.appendChild(n); text.appendChild(a);

        var favBtn = document.createElement('button');
        favBtn.className = 'ss-fav';
        favBtn.type = 'button';
        favBtn.title = '加入收藏';
        favBtn.textContent = '⭐';

        row.appendChild(icon);
        row.appendChild(text);
        row.appendChild(favBtn);

        row.addEventListener('click', function(ev) {
          if (ev.target === favBtn) return;
          ssGoto(it);
        });
        favBtn.addEventListener('click', function(ev) {
          ev.stopPropagation();
          ssCloseDropdown();
          openBookmarkModal({lat: it.lat, lng: it.lon}, it.name);
        });

        ssList.appendChild(row);
      });
      ssList.classList.add('open');
    }
    function ssShowError(msg) {
      ssList.innerHTML = '';
      var r = document.createElement('div');
      r.className = 'ss-row ss-empty ss-error';
      r.textContent = msg;
      ssList.appendChild(r);
      ssList.classList.add('open');
    }
    function ssCloseDropdown() {
      ssList.classList.remove('open');
    }
    function ssGoto(it) {
      ssCloseDropdown();
      ssInput.value = it.name;
      ssWrap.classList.add('has-text');
      ssInput.blur();        // dismiss the on-screen keyboard on mobile
      var latlng = L.latLng(it.lat, it.lon);
      // 16 is tight enough to read shop signs without losing context. flyTo
      // animates; Leaflet caps the duration so it's never jarring.
      map.flyTo(latlng, Math.max(map.getZoom(), 16), {duration: 0.8});
      ssRemoveTempMarker();
      var iconHtml =
        '<div style="position:relative;transform:translate(-50%,-100%);' +
                    'text-align:center;width:max-content;">' +
          '<div style="font-size:26px;line-height:1;' +
                      'filter:drop-shadow(0 1px 3px rgba(0,0,0,0.5));">📍</div>' +
          '<div style="font-size:11px;font-weight:700;color:#1f2937;' +
                      'background:rgba(255,255,255,0.95);' +
                      'padding:1px 6px;border-radius:4px;margin-top:1px;' +
                      'white-space:nowrap;max-width:220px;overflow:hidden;' +
                      'text-overflow:ellipsis;' +
                      'box-shadow:0 1px 2px rgba(0,0,0,0.2);">' +
            escapeHtml(it.name) + '</div>' +
        '</div>';
      ssTempMarker = L.marker(latlng, {
        icon: L.divIcon({className: 'empty', iconSize: [0,0], iconAnchor: [0,0], html: iconHtml})
      }).addTo(map);
      // Small popup attached so the user can immediately bookmark the
      // searched place without scrolling back to the dropdown.
      var popHtml =
        '<div style="font:13px sans-serif;text-align:center;min-width:170px;">' +
          '<div style="font-weight:700;margin-bottom:4px;">' +
            escapeHtml(it.name) + '</div>' +
          '<div style="font-family:monospace;font-size:11px;color:#6b7280;' +
                      'margin-bottom:8px;">' +
            it.lat.toFixed(6) + ', ' + it.lon.toFixed(6) + '</div>' +
          '<button id="ss-add-bm" ' +
                  'style="padding:4px 12px;font-size:12px;cursor:pointer;' +
                  'border:1px solid #2563eb;border-radius:4px;' +
                  'background:#2563eb;color:#fff;font-weight:600;">' +
                  '⭐ 加入收藏</button>' +
        '</div>';
      L.popup({offset: [0, -26]}).setLatLng(latlng).setContent(popHtml).openOn(map);
      setTimeout(function() {
        var btn = document.getElementById('ss-add-bm');
        if (!btn) return;
        btn.addEventListener('click', function() {
          map.closePopup();
          openBookmarkModal({lat: it.lat, lng: it.lon}, it.name);
        });
      }, 0);
    }
    function ssParseResult(r) {
      // Nominatim returns name/display_name as strings, lat/lon as
      // stringified floats. namedetails.name:zh / :ja if present is a
      // nicer label than the addressy display_name.
      var nd = r.namedetails || {};
      var name = nd['name:zh'] || nd['name:zh-Hans'] || nd['name:zh-Hant']
              || nd['name:ja'] || r.name || nd.name
              || (r.display_name || '').split(',')[0].trim();
      return {
        lat: parseFloat(r.lat),
        lon: parseFloat(r.lon),
        name: name || '未命名',
        address: r.display_name || ''
      };
    }
    function ssSearch(q) {
      var seq = ++ssReqSeq;
      ssWrap.classList.add('busy');
      // Japan bbox: lon 122-154, lat 24-46. viewbox order:
      //   x1 (left lon), y1 (top lat), x2 (right lon), y2 (bottom lat)
      // bounded=1 forbids matches outside the box; otherwise OSM happily
      // returns "Osaka, Texas" before the city in Japan.
      var url = 'https://nominatim.openstreetmap.org/search?'
              + 'format=jsonv2&limit=6&addressdetails=0&namedetails=1'
              + '&viewbox=122,46,154,24&bounded=1'
              + '&accept-language=zh-CN,zh,ja,en'
              + '&q=' + encodeURIComponent(q);
      fetch(url, {headers: {'Accept': 'application/json'}})
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(arr) {
          if (seq !== ssReqSeq) return;       // stale response
          ssWrap.classList.remove('busy');
          var items = (Array.isArray(arr) ? arr : [])
            .map(ssParseResult)
            .filter(function(x){ return !isNaN(x.lat) && !isNaN(x.lon); });
          ssRender(items);
        })
        .catch(function(err) {
          if (seq !== ssReqSeq) return;
          ssWrap.classList.remove('busy');
          ssShowError('搜索失败: ' + err.message);
        });
    }
    function ssOnInput() {
      var v = ssInput.value.trim();
      if (v) ssWrap.classList.add('has-text');
      else   ssWrap.classList.remove('has-text');
      clearTimeout(ssDebounce);
      if (!v) {
        ssReqSeq++;
        ssWrap.classList.remove('busy');
        ssCloseDropdown();
        ssRemoveTempMarker();
        return;
      }
      ssDebounce = setTimeout(function() { ssSearch(v); }, 300);
    }
    // exitSearch: full bail-out. Used by the × button and Escape — clears
    // text, drops the temp marker + popup, closes the dropdown, and blurs
    // the input so the mobile keyboard goes away.
    function ssExitSearch() {
      ssReqSeq++;
      ssInput.value = '';
      ssWrap.classList.remove('has-text');
      ssWrap.classList.remove('busy');
      ssWrap.classList.remove('searching');
      ssCloseDropdown();
      if (ssTempMarker) map.closePopup();
      ssRemoveTempMarker();
      ssInput.blur();
    }
    ssInput.addEventListener('input', ssOnInput);
    ssInput.addEventListener('focus', function() {
      ssWrap.classList.add('searching');
      if (ssList.children.length > 0) ssList.classList.add('open');
    });
    ssInput.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') {
        if (ssList.classList.contains('open') && ssInput.value) {
          ssCloseDropdown();
        } else {
          ssExitSearch();
        }
      } else if (e.key === 'Enter') {
        // Enter on a non-empty dropdown -> pick the first result.
        var first = ssList.querySelector('.ss-row:not(.ss-empty)');
        if (first) first.click();
      }
    });
    // Mousedown preventDefault keeps focus on the input until our click
    // handler runs — otherwise on desktop the blur fires first, strips the
    // 'searching' class, and the × button vanishes mid-tap.
    ssClear.addEventListener('mousedown', function(e) { e.preventDefault(); });
    ssClear.addEventListener('click', ssExitSearch);
    // Click outside the search box closes the dropdown AND drops the
    // 'searching' state so the × button hides once the user is back on the
    // map. (Text, if any, is kept so they can refine on re-focus.)
    document.addEventListener('click', function(e) {
      if (ssBox.contains(e.target)) return;
      ssCloseDropdown();
      ssWrap.classList.remove('searching');
    });

    // chunkedLoading: true splits the initial addLayers() of ~9800 markers
    // into async batches (defaults: 200ms work / 50ms pause) instead of one
    // synchronous pass that would jank the main thread for 1-2s on mobile.
    var cluster = L.markerClusterGroup({
      maxClusterRadius: 40,
      disableClusteringAtZoom: 17,
      chunkedLoading: true
    });
    map.addLayer(cluster);

    // ----- Build initial state. Baked-in baseline (from the JSON files at
    // build time) is the floor; localStorage cache overrides it (last known
    // state on this device); the Gist pull (if configured) overrides both.
    var state = {fav: new Set(), black: new Set()};
    data.forEach(function(d) {
      if (d.favorited) state.fav.add(d.detail_url);
      if (d.blacklisted) state.black.add(d.detail_url);
    });
    var cache = loadCache();
    if (cache.fav) state.fav = new Set(cache.fav);
    if (cache.black) state.black = new Set(cache.black);

    function isFav(d)   { return state.fav.has(d.detail_url); }
    function isBlack(d) { return state.black.has(d.detail_url); }

    // ----- Sync engine: pull on load/visible/poll, debounced push on toggle.
    var statusEl = document.getElementById('ff-sync-status');
    function setStatus(text, kind) {
      if (!statusEl) return;
      statusEl.textContent = text;
      statusEl.style.color = kind === 'err' ? '#dc2626'
                           : kind === 'ok'  ? '#16a34a'
                           : kind === 'busy'? '#2563eb' : '#6b7280';
    }
    // dirty is restored from cache so an unpushed change survives a refresh.
    var etag = null, dirty = cache.dirty || false;
    var pushTimer = null, pollTimer = null;

    function configured() {
      var c = loadConfig();
      return c.gistId ? c : null;
    }
    function refreshAllMarkers() {
      // Only the markers that have actually been materialized can have their
      // icon updated. Rows outside the viewport will pick up the new state
      // the next time they're added to the cluster (makeIcon reads fav/black
      // state at construction time).
      for (var i = 0; i < data.length; i++) {
        var d = data[i];
        if (d._m) d._m.setIcon(makeIcon(d));
      }
      updateFavCount();
      updateBlackCount();
      apply();
    }
    function pull() {
      var c = configured();
      if (!c) { setStatus('本地模式', ''); return; }
      // Don't clobber unsent local edits with a stale remote.
      if (dirty) return;
      setStatus('同步中…', 'busy');
      var headers = gistHeaders(c.pat);
      if (etag) headers['If-None-Match'] = etag;
      fetch(GIST_API + c.gistId, {headers: headers})
        .then(function(r) {
          if (r.status === 304) { setStatus('已同步', 'ok'); return; }
          if (!r.ok) throw new Error('HTTP ' + r.status);
          etag = r.headers.get('ETag');
          return r.json().then(function(j) {
            var remote = parseGistFiles(j);
            if (remote.fav)   state.fav   = remote.fav;
            if (remote.black) state.black = remote.black;
            if (remote.bookmarks) {
              // Wipe + re-render in place — closures hold the same array
              // reference, so we mutate rather than reassign. Both layers
              // (bookmarks + user-added attractions) get cleared so the
              // pull rebuild starts from a clean slate.
              bookmarks.length = 0;
              bookmarksLayer.clearLayers();
              userAttractionsLayer.clearLayers();
              bmMarkerById = {};
              remote.bookmarks.forEach(function(bm) {
                bookmarks.push(bm);
                renderBookmark(bm);
              });
              saveBookmarks();
            }
            saveCache(state, false);
            refreshAllMarkers();
            setStatus('已同步 ' + new Date().toLocaleTimeString(), 'ok');
          });
        })
        .catch(function(e) { setStatus('同步失败: ' + e.message, 'err'); });
    }
    function push() {
      var c = configured();
      if (!c) { dirty = false; saveCache(state, false); return; }
      if (!c.pat) {                // read-only mode
        dirty = false;
        saveCache(state, false);
        setStatus('只读模式（无 PAT，无法保存）', '');
        return;
      }
      setStatus('保存中…', 'busy');
      var body = JSON.stringify({
        files: {
          'favorites.json': {content: JSON.stringify(Array.from(state.fav), null, 2)},
          'blacklist.json': {content: JSON.stringify(Array.from(state.black), null, 2)},
          // Gist auto-creates this file on the first push from a device
          // that previously only had favorites + blacklist set up.
          'bookmarks.json': {content: JSON.stringify(bookmarks, null, 2)},
        },
      });
      var headers = gistHeaders(c.pat);
      headers['Content-Type'] = 'application/json';
      fetch(GIST_API + c.gistId, {method: 'PATCH', headers: headers, body: body})
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          etag = r.headers.get('ETag');
          dirty = false;
          saveCache(state, false);
          setStatus('已同步 ' + new Date().toLocaleTimeString(), 'ok');
        })
        .catch(function(e) {
          // Keep dirty=true (in memory AND in localStorage) on failure so the
          // next pull — or a page refresh — doesn't silently clobber our
          // unsaved change with the stale remote state. Change recovers when
          // a future push succeeds.
          saveCache(state, true);
          var hint = e.message.indexOf('403') >= 0
            ? '（PAT 没有写权限，去 settings → Gists 改 Read and write）'
            : '（已存本地，将在下次成功推送时同步）';
          setStatus('保存失败: ' + e.message + ' ' + hint, 'err');
        });
    }
    function schedulePush() {
      dirty = true;
      saveCache(state, true);
      clearTimeout(pushTimer);
      pushTimer = setTimeout(push, 500);
    }
    function startSync() {
      // If a previous session left an unpushed change, retry pushing it
      // *before* pulling; otherwise pull would just confirm the remote
      // state (which still lacks the change) and the user would see "已同步"
      // while their edit is actually still local-only.
      if (dirty) push(); else pull();
      clearInterval(pollTimer);
      pollTimer = setInterval(function() {
        if (document.visibilityState === 'visible') pull();
      }, 60000);
    }
    document.addEventListener('visibilitychange', function() {
      if (document.visibilityState === 'visible') pull();
    });

    function toggleFav(url) {
      if (state.fav.has(url)) state.fav.delete(url); else state.fav.add(url);
      schedulePush();
    }
    function toggleBlack(url) {
      if (state.black.has(url)) state.black.delete(url); else state.black.add(url);
      schedulePush();
    }

    // No hard border — price color is a radial-gradient halo behind the
    // emoji. Fav/black state shown via a small corner badge instead.
    function makeIcon(d) {
      var color = BUCKET_COLOR[d.bucket] || '#9ca3af';
      var cat   = d.categories && d.categories[0];
      var emoji = (cat && GENRE_EMOJI[cat]) || '🍽️';
      var size = 36;
      var opacity = 1.0;
      var badge = '';
      if (isBlack(d)) {
        opacity = 0.4;
        badge = '<span style="position:absolute;top:0;right:2px;font-size:11px;' +
                'line-height:1;color:#dc2626;text-shadow:0 0 2px #fff;">✕</span>';
      } else if (isFav(d)) {
        badge = '<span style="position:absolute;top:0;right:2px;font-size:11px;' +
                'line-height:1;text-shadow:0 0 2px #fff;">⭐</span>';
      }
      var html = '<div style="position:relative;width:' + size + 'px;height:' + size + 'px;' +
                 'display:flex;align-items:center;justify-content:center;' +
                 'opacity:' + opacity + ';">' +
                 '<div style="position:absolute;inset:0;border-radius:50%;' +
                 'background:radial-gradient(circle closest-side, ' +
                 color + 'EE 0%, ' + color + 'AA 50%, ' + color + '00 100%);"></div>' +
                 emojiImg(emoji, 'position:relative;width:16px;height:16px;' +
                                 'filter:drop-shadow(0 1px 2px rgba(0,0,0,0.35));') +
                 badge +
                 '</div>';
      return L.divIcon({className: '', html: html,
                        iconSize: [size, size],
                        iconAnchor: [size / 2, size / 2]});
    }
    function syncFavButton(btn, d) {
      var label = btn.querySelector('.ff-fav-label');
      if (!label) return;
      if (isFav(d)) {
        label.textContent = '⭐ 已收藏';
        btn.style.background = '#fef3c7';
        btn.style.borderColor = '#fbbf24';
      } else {
        label.textContent = '☆ 收藏';
        btn.style.background = '#f9fafb';
        btn.style.borderColor = '#d1d5db';
      }
    }
    function syncBlackButton(btn, d) {
      var label = btn.querySelector('.ff-black-label');
      if (!label) return;
      if (isBlack(d)) {
        label.textContent = '✕ 已弃用';
        btn.style.background = '#fee2e2';
        btn.style.borderColor = '#dc2626';
      } else {
        label.textContent = '🚫 弃用';
        btn.style.background = '#f9fafb';
        btn.style.borderColor = '#d1d5db';
      }
    }

    // ===== Bottom-sheet popup =====
    // Replaces Leaflet's bindPopup. The sheet docks to the bottom of the
    // viewport, never spills off-screen, and scrolls internally. On wide
    // screens it caps at 440px width and stays bottom-centered.
    var bsBackdrop = document.getElementById('bs-backdrop');
    var bsSheet    = document.getElementById('bs-sheet');
    var bsContent  = document.getElementById('bs-content');
    var bsGrip     = document.getElementById('bs-grip');
    var bsActive   = null;

    function openSheet(d) {
      // Mutual exclusion with the filter sheet — both dock to the bottom.
      // ffSheet exists once the filter UI has booted; marker clicks can't
      // fire earlier, so the guard is for paranoia, not for races.
      var ffs = document.getElementById('ff-sheet');
      if (ffs && ffs.classList.contains('ff-open')) {
        ffs.classList.remove('ff-open');
        document.getElementById('ff-backdrop').classList.remove('ff-open');
        ffs.setAttribute('aria-hidden', 'true');
        var ffb = document.getElementById('ff-fab');
        if (ffb) ffb.hidden = false;
      }
      bsActive = d;
      // Hide the filter FAB so the bottom-left corner stays clean while
      // the restaurant card occupies the bottom slot.
      var ffbtn = document.getElementById('ff-fab');
      if (ffbtn) ffbtn.hidden = true;
      function paint(html) {
        // Guard: user may have closed the sheet or opened another one while
        // the popups.json fetch was in flight.
        if (bsActive !== d) return;
        bsContent.innerHTML = html;
        var favBtn   = bsContent.querySelector('.ff-fav-btn');
        var blackBtn = bsContent.querySelector('.ff-black-btn');
        if (favBtn)   syncFavButton(favBtn, d);
        if (blackBtn) syncBlackButton(blackBtn, d);
        bsContent.scrollTop = 0;
      }
      if (popupsMap) {
        paint(renderPopup(d, popupsMap[d.detail_url]));
      } else {
        // First marker tap on this page load — show a minimal placeholder
        // (name + rating) while popups.json is on the wire. Usually <500ms.
        var name = (d.name || '').replace(/[&<>]/g, function(c) {
          return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
        });
        paint('<div class="rst-card"><div class="rst-header">' +
              '<div class="rst-title">' + name +
              '<span class="rst-rating">★' + (d.rating == null ? '–' : d.rating) +
              '</span></div></div>' +
              '<div style="margin-top:10px;color:#6b7280;font-size:13px;">加载中…</div>' +
              '</div>');
        loadPopups().then(function(map) { paint(renderPopup(d, map[d.detail_url])); })
                    .catch(function() {
                      paint('<div class="rst-card"><div class="rst-title">' + name +
                            '</div><div style="margin-top:10px;color:#dc2626;">加载失败,请检查网络</div></div>');
                    });
      }
      // Force reflow so the transition runs even on rapid reopen.
      void bsSheet.getBoundingClientRect();
      bsSheet.classList.add('bs-open');
      bsBackdrop.classList.add('bs-open');
      bsSheet.setAttribute('aria-hidden', 'false');
    }
    function closeSheet() {
      bsSheet.classList.remove('bs-open');
      bsBackdrop.classList.remove('bs-open');
      bsSheet.setAttribute('aria-hidden', 'true');
      bsActive = null;
      var ffbtn = document.getElementById('ff-fab');
      if (ffbtn) ffbtn.hidden = false;
    }
    bsBackdrop.addEventListener('click', closeSheet);
    document.addEventListener('keydown', function(e){
      if (e.key === 'Escape' && bsActive) closeSheet();
    });

    // Swipe-down on the grip to dismiss. Threshold 80px or fast flick.
    var bsDrag = null;
    function bsDragStart(e) {
      var p = e.touches ? e.touches[0] : e;
      bsDrag = { y0: p.clientY, t0: Date.now() };
      bsSheet.style.transition = 'none';
    }
    function bsDragMove(e) {
      if (!bsDrag) return;
      var p = e.touches ? e.touches[0] : e;
      var dy = Math.max(0, p.clientY - bsDrag.y0);
      // Match the desktop centered transform so the sheet doesn't snap left.
      var prefix = window.innerWidth >= 700 ? 'translate(-50%, ' + dy + 'px)'
                                            : 'translateY(' + dy + 'px)';
      bsSheet.style.transform = prefix;
    }
    function bsDragEnd(e) {
      if (!bsDrag) return;
      var p = (e.changedTouches && e.changedTouches[0]) || e;
      var dy = p.clientY - bsDrag.y0;
      var dt = Date.now() - bsDrag.t0;
      bsSheet.style.transition = '';
      bsSheet.style.transform = '';
      if (dy > 80 || (dy > 30 && dt < 200)) closeSheet();
      bsDrag = null;
    }
    bsGrip.addEventListener('touchstart', bsDragStart, { passive: true });
    bsGrip.addEventListener('touchmove',  bsDragMove,  { passive: true });
    bsGrip.addEventListener('touchend',   bsDragEnd);
    bsGrip.addEventListener('mousedown',  bsDragStart);
    document.addEventListener('mousemove', bsDragMove);
    document.addEventListener('mouseup',   bsDragEnd);

    // Tap on empty map area closes the sheet (mirrors Leaflet popup behavior).
    map.on('click', function(){ if (bsActive) closeSheet(); });

    // ===== Viewport-driven marker construction =====
    // We no longer build all 8000+ L.marker objects up front (that allocated
    // ~30-50MB of heap on boot and pinned an inline divIcon DOM string per
    // row). Instead, restaurants live as plain JS row objects; markers are
    // materialized on first appearance and cached on the row as `d._m`.
    // Pan/zoom triggers a viewport-clipped recompute; the filter UI calls
    // the same recompute path. The cluster only ever sees the subset that
    // (a) intersects the visible bbox and (b) passes the current filter.
    //
    // The 0.1° grid (~11 km cells) is fine enough that even a citywide view
    // walks a handful of cells, not the full row list.
    var GRID = 0.1;
    var gridIndex = new Map();
    var rowByUrl = {};
    for (var di = 0; di < data.length; di++) {
      var d = data[di];
      if (typeof d.lat !== 'number' || typeof d.lon !== 'number') continue;
      if (d.detail_url) rowByUrl[d.detail_url] = d;
      var gx = Math.floor(d.lon / GRID), gy = Math.floor(d.lat / GRID);
      var k = gx + ',' + gy;
      var cell = gridIndex.get(k);
      if (!cell) { cell = []; gridIndex.set(k, cell); }
      cell.push(d);
    }
    var onMap = new Set();  // rows currently added to the cluster

    function visibleRows() {
      var b = map.getBounds().pad(0.25);
      var W = b.getWest(), E = b.getEast(), S = b.getSouth(), N = b.getNorth();
      var gx0 = Math.floor(W / GRID), gx1 = Math.floor(E / GRID);
      var gy0 = Math.floor(S / GRID), gy1 = Math.floor(N / GRID);
      var out = [];
      for (var gx = gx0; gx <= gx1; gx++) {
        for (var gy = gy0; gy <= gy1; gy++) {
          var cell = gridIndex.get(gx + ',' + gy);
          if (!cell) continue;
          for (var i = 0; i < cell.length; i++) {
            var d = cell[i];
            if (d.lon < W || d.lon > E || d.lat < S || d.lat > N) continue;
            out.push(d);
          }
        }
      }
      return out;
    }

    function ensureMarker(d) {
      if (d._m) return d._m;
      var m = L.marker([d.lat, d.lon], {icon: makeIcon(d)});
      m._d = d;
      m.on('click', function() { openSheet(d); });
      d._m = m;
      return m;
    }

    function setCountText(cls, n) {
      var nodes = document.querySelectorAll('.' + cls);
      for (var i = 0; i < nodes.length; i++) nodes[i].textContent = n;
    }
    setCountText('ff-total', data.length);

    function updateFavCount() {
      var n = 0;
      for (var i = 0; i < data.length; i++) if (isFav(data[i])) n++;
      document.getElementById('ff-fav-count').textContent = n;
    }
    function updateBlackCount() {
      var n = 0;
      for (var i = 0; i < data.length; i++) if (isBlack(data[i])) n++;
      document.getElementById('ff-black-count').textContent = n;
    }

    // Keep ⭐/🚫 buttons in sync if the sheet is open while the user toggles
    // them from elsewhere (rare; kept for parity with the old popupopen path).
    map.on('popupopen', function(e) {
      var node = e.popup.getElement && e.popup.getElement();
      if (!node) return;
      var favBtn = node.querySelector('.ff-fav-btn');
      var blackBtn = node.querySelector('.ff-black-btn');
      var url = (favBtn || blackBtn) && (favBtn || blackBtn).getAttribute('data-url');
      var d = url && rowByUrl[url];
      if (!d) return;
      if (favBtn)   syncFavButton(favBtn, d);
      if (blackBtn) syncBlackButton(blackBtn, d);
    });

    // One delegated handler for both ⭐ and 🚫 clicks anywhere in the DOM.
    document.addEventListener('click', function(e) {
      var favBtn   = e.target.closest && e.target.closest('.ff-fav-btn');
      var blackBtn = e.target.closest && e.target.closest('.ff-black-btn');
      var btn = favBtn || blackBtn;
      if (!btn) return;
      var d = rowByUrl[btn.getAttribute('data-url')];
      if (!d) return;
      if (favBtn) {
        toggleFav(d.detail_url);
        syncFavButton(favBtn, d);
        updateFavCount();
      } else {
        toggleBlack(d.detail_url);
        syncBlackButton(blackBtn, d);
        updateBlackCount();
      }
      if (d._m) d._m.setIcon(makeIcon(d));
      apply();
    });

    var ratingSlider = document.getElementById('ff-rating');
    var ratingLabel = document.getElementById('ff-rating-val');

    var onlyFavEl = document.getElementById('ff-only-fav');
    var hideBlackEl = document.getElementById('ff-hide-black');
    var hideForeignEl = document.getElementById('ff-hide-foreign');
    var FOREIGN_GENRES = new Set(__DEFAULT_OFF_GENRES__);

    var genreSummaryEl = document.getElementById('ff-genre-summary');
    var genreBoxes = document.querySelectorAll('input[name=ff-genre]');
    function updateGenreSummary() {
      var total = genreBoxes.length;
      var n = 0;
      genreBoxes.forEach(function(c){ if (c.checked) n++; });
      if (!genreSummaryEl) return;
      if (n === total) genreSummaryEl.textContent = '全部';
      else if (n === 0) genreSummaryEl.textContent = '无';
      else genreSummaryEl.textContent = '已选 ' + n + ' / ' + total;
    }

    // Filter inputs are read once into this struct so `recompute()` (called
    // on every pan/zoom moveend) doesn't have to re-touch the DOM. `apply()`
    // is the user-callable side: it refreshes the cache, then recomputes.
    var filterState = {
      minRating: 3.4, pSet: {}, gSet: {}, gAny: false,
      book: 'all', onlyFav: false, hideBlack: true, hideForeign: true
    };
    function readFilterInputs() {
      filterState.minRating = parseFloat(ratingSlider.value);
      ratingLabel.textContent = filterState.minRating.toFixed(2);
      filterState.pSet = {};
      document.querySelectorAll('input[name=ff-price]:checked').forEach(function(c){ filterState.pSet[c.value]=1; });
      filterState.gSet = {};
      filterState.gAny = false;
      document.querySelectorAll('input[name=ff-genre]:checked').forEach(function(c){ filterState.gSet[c.value]=1; filterState.gAny=true; });
      var bEl = document.querySelector('input[name=ff-bookable]:checked');
      filterState.book = bEl ? bEl.value : 'all';
      filterState.onlyFav = onlyFavEl.checked;
      filterState.hideBlack = hideBlackEl.checked;
      filterState.hideForeign = hideForeignEl.checked;
    }
    function passesFilter(d) {
      var fs = filterState;
      // Blacklist short-circuits when "隐藏" is on, regardless of other
      // filters. Hide-blacklist defeats only-fav so a starred-then-blacklisted
      // restaurant still hides — easier mental model.
      if (fs.hideBlack && isBlack(d)) return false;
      // Slider min 3.4 covers the whole dataset, so we only filter when
      // the user actually moves it above the minimum.
      if (fs.minRating > 3.4) {
        if (d.rating == null || d.rating < fs.minRating) return false;
      }
      if (!fs.pSet[d.bucket]) return false;
      // Foreign cuisines (中/韩/西/南亚) bypass the regular genre filter —
      // they're gated entirely by hideForeignEl. When shown, they appear
      // regardless of which Japanese-cuisine boxes are checked.
      var cats = d.categories || [];
      var isForeign = cats.length > 0 && FOREIGN_GENRES.has(cats[0]);
      if (isForeign) {
        if (fs.hideForeign) return false;
      } else {
        // Genre filter: OR across selected categories. If none checked, hide all.
        if (!fs.gAny) return false;
        var ok = false;
        for (var i = 0; i < cats.length; i++) {
          if (fs.gSet[cats[i]]) { ok = true; break; }
        }
        if (!ok) return false;
      }
      if (fs.book === 'yes' && !d.bookable) return false;
      if (fs.book === 'no' && d.bookable) return false;
      if (fs.onlyFav && !isFav(d)) return false;
      return true;
    }

    function recompute() {
      var candidates = visibleRows();
      var desired = new Set();
      for (var i = 0; i < candidates.length; i++) {
        var d = candidates[i];
        if (passesFilter(d)) desired.add(d);
      }
      // Diff against the current cluster contents — only add/remove the
      // delta. MarkerCluster's bulk addLayers / removeLayers are much
      // cheaper than clearLayers + rebuild on every pan.
      var removeLayers = [];
      onMap.forEach(function(d) {
        if (!desired.has(d) && d._m) removeLayers.push(d._m);
      });
      if (removeLayers.length) {
        cluster.removeLayers(removeLayers);
        // Drop the cached L.marker references so GC can reclaim the DOM
        // + icon + click-closure for each one (~10KB apiece). Without this
        // step, panning across all of Japan accumulates ~100 MB of ghost
        // markers that aren't on the map anymore. ensureMarker() recreates
        // on re-entry; the cluster rebuilds on add either way.
        for (var ri = 0; ri < removeLayers.length; ri++) {
          if (removeLayers[ri]._d) removeLayers[ri]._d._m = null;
        }
      }
      onMap.forEach(function(d) { if (!desired.has(d)) onMap.delete(d); });

      var addLayers = [];
      desired.forEach(function(d) {
        if (!onMap.has(d)) {
          addLayers.push(ensureMarker(d));
          onMap.add(d);
        }
      });
      if (addLayers.length) cluster.addLayers(addLayers);

      setCountText('ff-count', desired.size);
    }

    // rAF-coalesce moveend so a long pan with many fired events still maps
    // to one recompute per frame at most. Same pattern as the transit layer.
    var recomputeRaf = 0;
    function scheduleRecompute() {
      if (recomputeRaf) return;
      recomputeRaf = requestAnimationFrame(function() {
        recomputeRaf = 0;
        recompute();
      });
    }
    map.on('moveend', scheduleRecompute);

    function apply() {
      readFilterInputs();
      updateGenreSummary();
      recompute();
      saveFilterState();
    }

    // ===== Persisted filter state =====
    // saveFilterState runs at the end of every apply(); restoreFilterState
    // runs once before the first apply() to repopulate inputs from the last
    // session. Reset button clears the inputs then calls apply(), which
    // overwrites stored state with defaults — a true reset.
    var STATE_KEY_FILTER = 'tabelog.filterState';
    function saveFilterState() {
      try {
        var prices = [];
        document.querySelectorAll('input[name=ff-price]:checked').forEach(function(c){ prices.push(c.value); });
        var genres = [];
        document.querySelectorAll('input[name=ff-genre]:checked').forEach(function(c){ genres.push(c.value); });
        var bEl = document.querySelector('input[name=ff-bookable]:checked');
        localStorage.setItem(STATE_KEY_FILTER, JSON.stringify({
          rating: parseFloat(ratingSlider.value),
          prices: prices,
          genres: genres,
          bookable: bEl ? bEl.value : 'all',
          onlyFav: onlyFavEl.checked,
          hideBlack: hideBlackEl.checked,
          hideForeign: hideForeignEl.checked
        }));
      } catch (e) {}
    }
    function restoreFilterState() {
      var s;
      try { s = JSON.parse(localStorage.getItem(STATE_KEY_FILTER) || 'null'); }
      catch (e) { return; }
      if (!s) return;
      if (typeof s.rating === 'number') ratingSlider.value = String(s.rating);
      if (Array.isArray(s.prices)) {
        var pSet = {};
        s.prices.forEach(function(v){ pSet[v] = 1; });
        document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = !!pSet[c.value]; });
      }
      if (Array.isArray(s.genres)) {
        var gSet = {};
        s.genres.forEach(function(v){ gSet[v] = 1; });
        document.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = !!gSet[c.value]; });
      }
      if (typeof s.bookable === 'string') {
        var bEl = document.querySelector('input[name=ff-bookable][value="' + s.bookable + '"]');
        if (bEl) bEl.checked = true;
      }
      if (typeof s.onlyFav === 'boolean') onlyFavEl.checked = s.onlyFav;
      if (typeof s.hideBlack === 'boolean') hideBlackEl.checked = s.hideBlack;
      if (typeof s.hideForeign === 'boolean') hideForeignEl.checked = s.hideForeign;
    }

    // Live update on drag, not just on release.
    ratingSlider.addEventListener('input', apply);
    document.querySelectorAll('#ff-sheet input[type=checkbox], #ff-sheet input[type=radio]').forEach(function(el) {
      el.addEventListener('change', apply);
    });
    document.getElementById('ff-price-all').addEventListener('click', function(e) {
      e.preventDefault();
      document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = true; });
      apply();
    });
    document.getElementById('ff-price-none').addEventListener('click', function(e) {
      e.preventDefault();
      document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = false; });
      apply();
    });
    document.getElementById('ff-genre-all').addEventListener('click', function(e) {
      e.preventDefault();
      document.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = true; });
      apply();
    });
    document.getElementById('ff-genre-none').addEventListener('click', function(e) {
      e.preventDefault();
      document.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = false; });
      apply();
    });
    document.querySelectorAll('[data-genre-group]').forEach(function(box) {
      box.querySelector('.ff-group-all').addEventListener('click', function(e) {
        e.preventDefault();
        box.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = true; });
        apply();
      });
      box.querySelector('.ff-group-none').addEventListener('click', function(e) {
        e.preventDefault();
        box.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = false; });
        apply();
      });
    });
    // ===== Filter bottom sheet =====
    // Same visual treatment as the restaurant detail sheet (#bs-sheet);
    // mutual exclusion ensures both never occupy the bottom slot at once.
    // The bottom-left #ff-fab is the entry point; the restaurant detail
    // sheet auto-closes the filter sheet when a marker is tapped.
    var ffSheet    = document.getElementById('ff-sheet');
    var ffBackdrop = document.getElementById('ff-backdrop');
    var ffGrip     = document.getElementById('ff-grip');
    var ffFab      = document.getElementById('ff-fab');

    function openFilterSheet() {
      if (bsActive) closeSheet();        // restaurant detail yields to filter
      ffSheet.classList.add('ff-open');
      ffBackdrop.classList.add('ff-open');
      ffSheet.setAttribute('aria-hidden', 'false');
      ffFab.hidden = true;
    }
    function closeFilterSheet() {
      ffSheet.classList.remove('ff-open');
      ffBackdrop.classList.remove('ff-open');
      ffSheet.setAttribute('aria-hidden', 'true');
      ffFab.hidden = false;
    }
    function ffIsOpen() { return ffSheet.classList.contains('ff-open'); }

    ffFab.addEventListener('click', openFilterSheet);
    ffBackdrop.addEventListener('click', closeFilterSheet);
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape' && ffIsOpen()) closeFilterSheet();
    });

    // Reuse the bottom-sheet swipe-down dismiss from the restaurant card.
    function makeSheetDrag(sheet, onClose) {
      var drag = null;
      function start(e) {
        var p = e.touches ? e.touches[0] : e;
        drag = { y0: p.clientY, t0: Date.now() };
        sheet.style.transition = 'none';
      }
      function move(e) {
        if (!drag) return;
        var p = e.touches ? e.touches[0] : e;
        var dy = Math.max(0, p.clientY - drag.y0);
        var prefix = window.innerWidth >= 700 ? 'translate(-50%, ' + dy + 'px)'
                                              : 'translateY(' + dy + 'px)';
        sheet.style.transform = prefix;
      }
      function end(e) {
        if (!drag) return;
        var p = (e.changedTouches && e.changedTouches[0]) || e;
        var dy = p.clientY - drag.y0;
        var dt = Date.now() - drag.t0;
        sheet.style.transition = '';
        sheet.style.transform = '';
        if (dy > 80 || (dy > 30 && dt < 200)) onClose();
        drag = null;
      }
      return { start: start, move: move, end: end };
    }
    var ffDrag = makeSheetDrag(ffSheet, closeFilterSheet);
    ffGrip.addEventListener('touchstart', ffDrag.start, { passive: true });
    ffGrip.addEventListener('touchmove',  ffDrag.move,  { passive: true });
    ffGrip.addEventListener('touchend',   ffDrag.end);
    ffGrip.addEventListener('mousedown',  ffDrag.start);
    document.addEventListener('mousemove', ffDrag.move);
    document.addEventListener('mouseup',   ffDrag.end);

    document.getElementById('ff-reset').addEventListener('click', function() {
      ratingSlider.value = '3.4';
      document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = true; });
      document.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = true; });
      document.querySelector('input[name=ff-bookable][value="all"]').checked = true;
      onlyFavEl.checked = false;
      hideBlackEl.checked = true;
      hideForeignEl.checked = true;
      apply();
    });

    // ----- Settings modal: edit Gist ID + PAT (stored per-device).
    var modalBg = document.getElementById('ff-modal-bg');
    var cfgGist = document.getElementById('ff-cfg-gist');
    var cfgPat  = document.getElementById('ff-cfg-pat');
    var cfgMsg  = document.getElementById('ff-cfg-msg');
    function openModal() {
      var c = loadConfig();
      cfgGist.value = c.gistId || '';
      cfgPat.value  = c.pat || '';
      cfgMsg.textContent = '';
      modalBg.style.display = 'flex';
    }
    function closeModal() { modalBg.style.display = 'none'; }

    document.getElementById('ff-settings').addEventListener('click', openModal);
    document.getElementById('ff-cfg-cancel').addEventListener('click', closeModal);
    modalBg.addEventListener('click', function(e) {
      if (e.target === modalBg) closeModal();
    });

    document.getElementById('ff-cfg-save').addEventListener('click', function() {
      var gistId = cfgGist.value.trim();
      var pat = cfgPat.value.trim();
      if (!gistId) {
        cfgMsg.style.color = '#dc2626';
        cfgMsg.textContent = '请填 Gist ID';
        return;
      }
      cfgMsg.style.color = '#2563eb';
      cfgMsg.textContent = '测试中…';
      // Verify by fetching the gist (and the PAT if given).
      fetch(GIST_API + gistId, {headers: gistHeaders(pat)})
        .then(function(r) {
          if (r.status === 404) throw new Error('Gist 找不到');
          if (r.status === 401) throw new Error('PAT 无效');
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(j) {
          saveConfig({gistId: gistId, pat: pat});
          cfgMsg.style.color = '#16a34a';
          var hasFiles = (j.files && (j.files['favorites.json'] || j.files['blacklist.json']));
          cfgMsg.textContent = dirty
            ? '✓ 配置成功，正在重试上次失败的同步'
            : (hasFiles ? '✓ 配置成功，正在拉取' : '✓ 已连接，Gist 是空的，下次操作会写入');
          etag = null;
          setTimeout(function() { closeModal(); startSync(); }, 700);
        })
        .catch(function(e) {
          cfgMsg.style.color = '#dc2626';
          cfgMsg.textContent = '✗ ' + e.message;
        });
    });

    document.getElementById('ff-cfg-clear').addEventListener('click', function() {
      if (!confirm('清除本地 Gist 配置?后续修改不会同步到云端。')) return;
      clearConfig();
      etag = null;
      clearInterval(pollTimer);
      cfgGist.value = '';
      cfgPat.value = '';
      cfgMsg.style.color = '#6b7280';
      cfgMsg.textContent = '已清除';
      setStatus('本地模式', '');
    });

    // Language picker — placeholder. Records the user's choice; the actual
    // translation pass is not implemented yet so changing this only updates
    // localStorage. Key follows the `tabelog.*` namespace.
    var langEl = document.getElementById('ff-lang');
    if (langEl) {
      try {
        var savedLang = localStorage.getItem('tabelog.lang');
        if (savedLang) langEl.value = savedLang;
      } catch (e) {}
      langEl.addEventListener('change', function() {
        try { localStorage.setItem('tabelog.lang', langEl.value); } catch (e) {}
      });
    }

    restoreFilterState();
    updateFavCount();
    updateBlackCount();
    apply();
    // Kick off the first pull (or stay in local mode if not configured).
    startSync();
  }
  function boot() {
    function setTotals(text) {
      var nodes = document.querySelectorAll('.ff-total');
      for (var i = 0; i < nodes.length; i++) nodes[i].textContent = text;
    }
    setTotals('加载中…');
    fetch('data/restaurants.json', {cache: 'force-cache'})
      .then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function(data) { initMap(data); })
      .catch(function(e) {
        console.error('[tabelog] restaurants.json load failed:', e);
        setTotals('加载失败');
      });
  }
  if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
</script>
"""


def parse_bool(v) -> bool:
    """CSV stores tabelog_bookable as the string 'True'/'False'/''.
    bool('False') would be True, so handle string forms explicitly."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y")


def popup_data(row: dict) -> list:
    # Per-restaurant fields needed to render the popup card client-side.
    # Positional layout, matched in JS renderPopup():
    #   [genre, dinner_upper, lunch_upper, seat, station, address, policy, photos]
    # name / rating / bucket / bookable / detail_url are NOT included — they
    # already live in restaurants.json, so the JS reader pulls them from there.
    # Dropping the duplicates + the static HTML scaffold takes popups.json
    # from ~28 MB to ~5 MB.
    def _empty_to_none(v):
        return v if v not in (None, "", "None") else None
    photos = [row.get(f"photo{i}_url") for i in (1, 2, 3)]
    photos = [p for p in photos if p]
    return [
        row.get("genre") or "",
        _empty_to_none(row.get("dinner_upper")),
        _empty_to_none(row.get("lunch_upper")),
        row.get("seat_count") or "",
        row.get("station") or "",
        row.get("address") or "",
        row.get("reservation_policy_chinese") or "",
        photos,
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fillall", action=argparse.BooleanOptionalAction, default=False,
                    help="re-geocode every row (default: only geocode rows "
                         "whose lat/lon are missing in CSV)")
    return ap.parse_args(argv)


def _parse_latlon(row: dict) -> tuple[float, float] | None:
    try:
        lat, lon = float(row.get("lat") or ""), float(row.get("lon") or "")
    except (TypeError, ValueError):
        return None
    return lat, lon


def write_csv_with_coords(rows: list[dict], fieldnames: list[str]) -> None:
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        all_rows = list(reader)
    for k in ("lat", "lon"):
        if k not in fieldnames:
            fieldnames.append(k)
        for r in all_rows:
            r.setdefault(k, "")

    addr_rows = [r for r in all_rows if r.get("address")]
    print(f"{len(addr_rows)} rows with non-empty address "
          f"(of {len(all_rows)} total){'  [fillall mode]' if args.fillall else ''}")

    cache = load_cache()
    geocoded: list[tuple[dict, dict]] = []
    failed: list[dict] = []
    n_skipped = 0
    client = httpx.Client(headers={"User-Agent": "omakase-tabelog-mapper/0.1"})
    for i, row in enumerate(addr_rows, 1):
        addr = row["address"]
        if not args.fillall:
            existing = _parse_latlon(row)
            if existing is not None:
                lat, lon = existing
                geocoded.append((row, {"lat": lat, "lon": lon,
                                       "matched_query": addr, "display": ""}))
                n_skipped += 1
                continue
        loc = geocode(addr, client, cache)
        if loc:
            row["lat"] = loc["lat"]
            row["lon"] = loc["lon"]
            geocoded.append((row, loc))
            print(
                f"  [{i}/{len(addr_rows)}] {row.get('name')!r} -> "
                f"({loc['lat']:.4f}, {loc['lon']:.4f})"
            )
        else:
            failed.append(row)
            print(f"  [{i}/{len(addr_rows)}] {row.get('name')!r}: NO MATCH ({addr!r})")
        if i % 5 == 0:
            save_cache(cache)
    save_cache(cache)

    write_csv_with_coords(all_rows, fieldnames)
    print(f"\nWrote lat/lon back to {CSV_PATH.name}")
    if not args.fillall:
        print(f"Skipped {n_skipped} rows that already had coords")
    print(f"Geocoded {len(geocoded)} / {len(addr_rows)}; failed {len(failed)}")
    if failed:
        print("Failed:")
        for f in failed:
            print(f"  - {f.get('name')!r}: {f.get('address')!r}")

    # CartoDB Voyager as the single base — clean Google-Maps-style. The
    # transit option lives in custom JS as a togglable OpenRailwayMap
    # overlay (rail lines + station markers drawn on top), wired to the
    # floating "🚇 公共交通" pill button (see FAB_HTML / initMap).
    m = folium.Map(location=JAPAN_CENTER, zoom_start=6, tiles=None,
                   zoom_control=False)
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
        attr=('&copy; <a href="https://www.openstreetmap.org/copyright">'
              'OpenStreetMap</a> contributors &copy; '
              '<a href="https://carto.com/attributions">CARTO</a>'),
        name="公路 (CartoDB Voyager)",
        max_zoom=19,
        subdomains="abcd",
    ).add_to(m)

    # Empty MarkerCluster only to pull in plugin JS/CSS; markers are built
    # client-side in JS so the filter panel can re-cluster on the fly.
    MarkerCluster(name="_assets", control=False).add_to(m)

    # Tourist attraction anchors — separate togglable layer, large emoji+label.
    attr_layer = folium.FeatureGroup(name="景点锚点", show=True).add_to(m)
    for name, emoji, lat, lon in ATTRACTIONS:
        icon_html = f"""
        <div style="position: relative; transform: translate(-50%, -100%);
                    text-align: center; width: max-content;">
          <div style="font-size: 30px; line-height: 1;
                      filter: drop-shadow(0 1px 2px rgba(0,0,0,0.45));">{emoji}</div>
          <div style="font-size: 11px; font-weight: 700; color: #1f2937;
                      background: rgba(255,255,255,0.92);
                      padding: 1px 6px; border-radius: 4px;
                      margin-top: 1px; white-space: nowrap;
                      box-shadow: 0 1px 2px rgba(0,0,0,0.2);">{name}</div>
        </div>
        """
        folium.Marker(
            location=[lat, lon],
            tooltip=name,
            icon=folium.features.DivIcon(
                icon_size=(0, 0), icon_anchor=(0, 0), html=icon_html
            ),
        ).add_to(attr_layer)

    # No LayerControl — replaced by the floating FAB stack (FAB_HTML).

    # Build the per-restaurant JSON payload the filter JS consumes. We split
    # into two files: a small "core" payload (everything the marker and the
    # filter UI need, fetched on boot) and a fat popups map (rendered HTML for
    # the bottom sheet, fetched lazily on first marker click). The core
    # payload used to be inlined into index.html — splitting it out drops the
    # HTML from ~25 MB to ~50 KB and lets the browser parse / paint before
    # the popups have downloaded.
    fav_set = load_favorites()
    black_set = load_blacklist()
    core_rows: list[dict] = []
    popups_map: dict[str, str] = {}
    cat_counts: dict[str, int] = {cat: 0 for cat in GENRE_CATEGORIES}
    unmapped_tokens: set[str] = set()
    for row, loc in geocoded:
        bkey, _blabel, _bcolor = price_bucket(row)
        try:
            rating_num = float(row.get("rating")) if row.get("rating") not in (None, "") else None
        except (TypeError, ValueError):
            rating_num = None
        url = row.get("detail_url") or ""
        cats = categorize_genre(row.get("genre") or "")
        for cat in cats:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        # Track tokens that fell through to "其他" so we notice when Tabelog
        # adds a new genre label that deserves its own bucket.
        for tok in (t.strip() for t in _GENRE_SPLIT_RE.split(row.get("genre") or "") if t.strip()):
            if tok not in _GENRE_TO_CAT:
                unmapped_tokens.add(tok)
        # color, emoji and tooltip are derived in JS now: color from bucket
        # via BUCKET_COLOR lookup, emoji from categories[0] via GENRE_EMOJI,
        # and the tooltip was actually dead weight (never read by the JS).
        core_rows.append({
            "lat": loc["lat"],
            "lon": loc["lon"],
            "name": row.get("name") or "",
            "rating": rating_num,
            "bucket": bkey,
            "categories": cats,
            "bookable": parse_bool(row.get("tabelog_bookable")),
            "detail_url": url,
            "favorited": url in fav_set,
            "blacklisted": url in black_set,
        })
        if url:
            popups_map[url] = popup_data(row)
    if unmapped_tokens:
        print(f"  unmapped genre tokens (fell into 其他): {sorted(unmapped_tokens)}")
    n_fav = sum(1 for p in core_rows if p['favorited'])
    n_black = sum(1 for p in core_rows if p['blacklisted'])
    print(f"  favorites: {n_fav} from favorites.json, blacklist: {n_black} from blacklist.json")

    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    restaurants_bytes = json.dumps(
        core_rows, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    popups_bytes = json.dumps(
        popups_map, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    RESTAURANTS_JSON.write_bytes(restaurants_bytes)
    POPUPS_JSON.write_bytes(popups_bytes)
    print(f"  restaurants.json: {len(restaurants_bytes):,} bytes "
          f"({len(core_rows)} rows)")
    print(f"  popups.json:      {len(popups_bytes):,} bytes "
          f"({len(popups_map)} entries)")

    panel_html = build_filter_panel_html(cat_counts)
    default_off_json = json.dumps(sorted(DEFAULT_OFF_GENRES), ensure_ascii=False)
    bookmarks_json = json.dumps(load_bookmarks(), ensure_ascii=False)
    bucket_colors_json = json.dumps(
        {key: color for key, _label, color, _lo, _hi in PRICE_BUCKETS}
    )
    genre_emoji_json = json.dumps(GENRE_EMOJI, ensure_ascii=False)
    filter_js = (
        FILTER_JS_TEMPLATE
        .replace("__DEFAULT_OFF_GENRES__", default_off_json)
        .replace("__BOOKMARKS__", bookmarks_json)
        .replace("__BUCKET_COLORS__", bucket_colors_json)
        .replace("__GENRE_EMOJI__", genre_emoji_json)
    )
    # Service worker — version-stamp the cache name so each redeploy
    # invalidates the previous one. Unix seconds is plenty granular for a
    # personal site rebuilt by hand.
    build_version = str(int(time.time()))
    SW_JS.write_text(
        SW_JS_TEMPLATE.replace("__BUILD_VERSION__", build_version),
        encoding="utf-8",
    )
    print(f"  sw.js:            build {build_version}")
    m.get_root().header.add_child(folium.Element(LOCATE_ASSETS))
    m.get_root().header.add_child(folium.Element(MOBILE_UX_ASSETS))
    m.get_root().html.add_child(folium.Element(BOTTOM_SHEET_HTML))
    m.get_root().html.add_child(folium.Element(MAP_FAB_HTML))
    m.get_root().html.add_child(folium.Element(SEARCH_BOX_HTML))
    m.get_root().html.add_child(folium.Element(BOOKMARKS_MODAL_HTML))
    m.get_root().html.add_child(folium.Element(panel_html))
    m.get_root().html.add_child(folium.Element(filter_js))

    m.save(str(OUT_HTML))
    print(f"\nMap written to {OUT_HTML}")
    print(f"  {len(core_rows)} restaurants in payload (fetched at runtime)")


if __name__ == "__main__":
    main()
