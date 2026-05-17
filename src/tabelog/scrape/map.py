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
    FAVORITES_JSON,
    BLACKLIST_JSON,
    OUTPUT_DIR,
    CACHE_DIR,
)
from tabelog.scrape.map_data import (
    ATTRACTIONS,
    DEFAULT_OFF_GENRES,
    GENRE_CATEGORIES,
    GENRE_EMOJI,
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


def simplify_address(addr: str) -> str:
    """Strip floor / building suffix to give Nominatim a fighting chance."""
    if not addr:
        return ""
    s = addr.strip()
    # cut at floor markers like "1F" "B1F" "7F"
    m = re.search(r"\s+[B]?\d{1,2}F\b", s)
    if m:
        s = s[: m.start()]
    # cut at common building keywords if a hyphen-numeric block is already present
    if re.search(r"\d+[-－]\d+", s):
        for kw in ("ビル", "メゾン", "ハイツ", "マンション"):
            i = s.find(kw)
            if i > 5:
                # walk back to last space before kw
                space = s.rfind(" ", 0, i)
                if space > 0:
                    s = s[:space]
                    break
    return s.strip()


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
    candidates = [addr, simplify_address(addr)]
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
    # DEFAULT_OFF_GENRES (中/韩/西/南亚) are not shown in the cuisine filter
    # at all — they're controlled by the standalone "隐藏外国料理" toggle below.
    genre_rows = "\n".join(
        f'        <label style="display:block;margin:1px 0;line-height:1.4;">'
        f'<input type="checkbox" name="ff-genre" value="{cat}" checked> '
        f'{cat} <span style="color:#9ca3af;">({cat_counts.get(cat, 0)})</span></label>'
        for cat in GENRE_CATEGORIES
        if cat not in DEFAULT_OFF_GENRES
    )
    foreign_count = sum(cat_counts.get(c, 0) for c in DEFAULT_OFF_GENRES)
    return f"""
<div id="ff-panel" style="
     position: fixed; top: 12px; right: 12px; z-index: 9999;
     background: rgba(255,255,255,0.97); padding: 10px 12px;
     border: 1px solid #d1d5db; border-radius: 8px;
     font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
     font-size: 12px; line-height: 1.45; color: #111827;
     box-shadow: 0 4px 12px rgba(0,0,0,0.12); width: 232px;">
  <div id="ff-header" style="display:flex;justify-content:space-between;align-items:center;
              border-bottom:1px solid #e5e7eb;padding-bottom:5px;margin-bottom:6px;">
    <span style="font-weight:700;font-size:13px;">筛选</span>
    <div style="display:flex;align-items:center;gap:8px;">
      <span style="font-size:11px;color:#6b7280;">显示 <b id="ff-count">–</b> / <span id="ff-total">–</span></span>
      <button id="ff-collapse" title="折叠"
              style="border:1px solid #d1d5db;background:#f9fafb;color:#374151;
                     width:22px;height:22px;border-radius:4px;cursor:pointer;
                     font-size:15px;line-height:1;padding:0;font-weight:700;
                     display:flex;align-items:center;justify-content:center;">−</button>
    </div>
  </div>

  <div id="ff-body">
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
    <input type="checkbox" id="ff-hide-foreign" checked> 隐藏外国料理 (🇨🇳🇰🇷🇫🇷🇮🇳)
  </label>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:4px;">
    <button id="ff-reset" style="padding:4px 0;border:1px solid #d1d5db;
            background:#f9fafb;border-radius:4px;cursor:pointer;font-size:11px;
            color:#374151;">重置筛选</button>
    <button id="ff-settings" style="padding:4px 0;border:1px solid #d1d5db;
            background:#eff6ff;border-radius:4px;cursor:pointer;font-size:11px;
            color:#1d4ed8;">⚙️ 同步设置</button>
  </div>
  <div id="ff-sync-status" style="font-size:10px;color:#6b7280;text-align:center;
       margin-top:2px;min-height:13px;">本地模式</div>
  </div>
</div>

<!-- Settings modal (Gist ID + PAT). Hidden by default. -->
<div id="ff-modal-bg" style="display:none;position:fixed;inset:0;z-index:10000;
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
<style>
  /* Suppress iOS long-press callout + text-selection on the map so the
     contextmenu handler fires cleanly on touch. */
  .leaflet-container {
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
  }
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
  /* Tighten on narrow screens — drop the label, keep just the icon. */
  @media (max-width: 480px) {
    .map-fab { padding: 9px 10px; }
    .map-fab-label { display: none; }
    .map-fab-ic { font-size: 17px; }
  }
</style>
<div class="map-fab-stack" role="group" aria-label="图层切换">
  <button id="fab-transit" class="map-fab" type="button"
          aria-pressed="false" title="叠加铁路 / 公交线路">
    <span class="map-fab-ic">🚇</span><span class="map-fab-label">公共交通</span>
  </button>
  <button id="fab-attractions" class="map-fab active" type="button"
          aria-pressed="true" title="景点锚点">
    <span class="map-fab-ic">🗾</span><span class="map-fab-label">景点</span>
  </button>
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
  /* Tablet: cap width and center; still bottom-anchored. */
  @media (min-width: 700px) {
    #bs-sheet { left: 50%; transform: translate(-50%, 100%);
                max-width: 680px; right: auto;
                max-height: 80vh; max-height: 80dvh;
                border-radius: 14px 14px 0 0; }
    #bs-sheet.bs-open { transform: translate(-50%, 0); }
  }
  /* Desktop: roomier sheet so the 2-column popup layout has space. */
  @media (min-width: 1100px) {
    #bs-sheet { max-width: 880px;
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
  #bs-close {
    position: absolute; top: 4px; right: 6px;
    background: none; border: none;
    font-size: 22px; line-height: 1; color: #9ca3af;
    cursor: pointer; padding: 4px 10px;
  }
  #bs-close:hover { color: #374151; }
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
  <button id="bs-close" aria-label="关闭">×</button>
  <div id="bs-content"></div>
</div>
"""


FILTER_JS_TEMPLATE = r"""
<script>
(function() {
  var EMBEDDED_DATA = __PAYLOAD__;

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
  function startEmojiObserver() {
    emojify(document.body);
    new MutationObserver(function(muts) {
      for (var i = 0; i < muts.length; i++) {
        var added = muts[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var nd = added[j];
          if (nd.nodeType === 1) emojify(nd);
          else if (nd.nodeType === 3 && nd.parentNode) emojify(nd.parentNode);
        }
      }
    }).observe(document.body, {childList: true, subtree: true});
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
    var files = json.files || {};
    return {
      fav: setFromFile(files['favorites.json']),
      black: setFromFile(files['blacklist.json']),
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

    // Live geolocation: click once to fly to current position, click again
    // for continuous follow; the plugin handles permission UI + accuracy ring.
    L.control.locate({
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

    // ===== FAB layer toggles: transit overlay + attractions =====
    // OpenRailwayMap rail/transit overlay (transparent tile layer on top of
    // the Voyager base). Off by default; user toggles via "🚇 公共交通" FAB.
    // Tiles include public-rail (JR + private + subway) plus station icons.
    var ormLayer = L.tileLayer(
      'https://{s}.tiles.openrailwaymap.org/standard/{z}/{x}/{y}.png',
      {
        attribution: 'Rail data &copy; ' +
          '<a href="https://www.openrailwaymap.org/">OpenRailwayMap</a> ' +
          '(CC-BY-SA)',
        subdomains: ['a', 'b', 'c'],
        maxZoom: 19, minZoom: 2,
        tileSize: 256, opacity: 0.85
      }
    );

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

    function applyToggle(btn, layer, on) {
      if (!btn || !layer) return;
      if (on) {
        if (!map.hasLayer(layer)) map.addLayer(layer);
        btn.classList.add('active');
        btn.setAttribute('aria-pressed', 'true');
      } else {
        if (map.hasLayer(layer)) map.removeLayer(layer);
        btn.classList.remove('active');
        btn.setAttribute('aria-pressed', 'false');
      }
    }

    function wireFab(btnId, layer, storageKey, defaultOn) {
      var btn = document.getElementById(btnId);
      if (!btn || !layer) return;
      var on = defaultOn;
      try {
        var v = localStorage.getItem(storageKey);
        if (v !== null) on = (v === '1');
      } catch (e) {}
      applyToggle(btn, layer, on);
      btn.addEventListener('click', function() {
        on = !on;
        applyToggle(btn, layer, on);
        try { localStorage.setItem(storageKey, on ? '1' : '0'); } catch (e) {}
      });
    }
    wireFab('fab-transit',     ormLayer,         'tabelog.showTransit',     false);
    wireFab('fab-attractions', attractionsLayer, 'tabelog.showAttractions', true);

    // Right-click (desktop) / long-press (mobile) on any blank spot of the
    // map -> popup with the coords + a copy button. Leaflet fires
    // 'contextmenu' for both gestures so we only need one handler.
    map.on('contextmenu', function(e) {
      var s = e.latlng.lat.toFixed(6) + ', ' + e.latlng.lng.toFixed(6);
      var html =
        '<div style="font:13px sans-serif;text-align:center;min-width:140px;">' +
          '<div style="font-family:monospace;margin-bottom:6px;">' + s + '</div>' +
          '<button id="ff-copy-coord" ' +
                  'style="padding:3px 10px;font-size:12px;cursor:pointer;' +
                  'border:1px solid #d1d5db;border-radius:4px;background:#f9fafb;">' +
                  '复制</button>' +
        '</div>';
      L.popup().setLatLng(e.latlng).setContent(html).openOn(map);
      setTimeout(function() {
        var btn = document.getElementById('ff-copy-coord');
        if (!btn) return;
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
      }, 0);
    });

    var cluster = L.markerClusterGroup({maxClusterRadius: 40, disableClusteringAtZoom: 17});
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
      markers.forEach(function(m){ m.setIcon(makeIcon(m._d)); });
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
      var color = d.color || '#9ca3af';
      var emoji = d.emoji || '🍽️';
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
    var bsClose    = document.getElementById('bs-close');
    var bsGrip     = document.getElementById('bs-grip');
    var bsActive   = null;

    function openSheet(d) {
      bsContent.innerHTML = d.popup;
      bsActive = d;
      // Mirror the old popupopen ⭐/🚫 sync.
      var favBtn   = bsContent.querySelector('.ff-fav-btn');
      var blackBtn = bsContent.querySelector('.ff-black-btn');
      if (favBtn)   syncFavButton(favBtn, d);
      if (blackBtn) syncBlackButton(blackBtn, d);
      bsContent.scrollTop = 0;
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
    }
    bsBackdrop.addEventListener('click', closeSheet);
    bsClose.addEventListener('click', closeSheet);
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

    var markers = data.map(function(d) {
      var m = L.marker([d.lat, d.lon], {icon: makeIcon(d)});
      m.on('click', function(){ openSheet(d); });
      m._d = d;
      return m;
    });
    // Tap on empty map area closes the sheet (mirrors Leaflet popup behavior).
    map.on('click', function(){ if (bsActive) closeSheet(); });

    var markerByUrl = {};
    markers.forEach(function(m){ markerByUrl[m._d.detail_url] = m; });
    document.getElementById('ff-total').textContent = markers.length;

    function updateFavCount() {
      var n = 0;
      markers.forEach(function(m){ if (isFav(m._d)) n++; });
      document.getElementById('ff-fav-count').textContent = n;
    }
    function updateBlackCount() {
      var n = 0;
      markers.forEach(function(m){ if (isBlack(m._d)) n++; });
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
      var m = url && markerByUrl[url];
      if (!m) return;
      if (favBtn)   syncFavButton(favBtn, m._d);
      if (blackBtn) syncBlackButton(blackBtn, m._d);
    });

    // One delegated handler for both ⭐ and 🚫 clicks anywhere in the DOM.
    document.addEventListener('click', function(e) {
      var favBtn   = e.target.closest && e.target.closest('.ff-fav-btn');
      var blackBtn = e.target.closest && e.target.closest('.ff-black-btn');
      var btn = favBtn || blackBtn;
      if (!btn) return;
      var m = markerByUrl[btn.getAttribute('data-url')];
      if (!m) return;
      if (favBtn) {
        toggleFav(m._d.detail_url);
        syncFavButton(favBtn, m._d);
        updateFavCount();
      } else {
        toggleBlack(m._d.detail_url);
        syncBlackButton(blackBtn, m._d);
        updateBlackCount();
      }
      m.setIcon(makeIcon(m._d));
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

    function apply() {
      var minRating = parseFloat(ratingSlider.value);
      ratingLabel.textContent = minRating.toFixed(2);
      var pSet = {};
      document.querySelectorAll('input[name=ff-price]:checked').forEach(function(c){ pSet[c.value]=1; });
      var gSet = {};
      var gAny = false;
      document.querySelectorAll('input[name=ff-genre]:checked').forEach(function(c){ gSet[c.value]=1; gAny=true; });
      var bEl = document.querySelector('input[name=ff-bookable]:checked');
      var book = bEl ? bEl.value : 'all';
      var onlyFav = onlyFavEl.checked;
      var hideBlack = hideBlackEl.checked;
      var hideForeign = hideForeignEl.checked;
      updateGenreSummary();

      cluster.clearLayers();
      var keep = [];
      markers.forEach(function(m) {
        var d = m._d;
        // Blacklist short-circuits when "隐藏" is on, regardless of other
        // filters. Hide-blacklist defeats only-fav so a starred-then-blacklisted
        // restaurant still hides — easier mental model.
        if (hideBlack && isBlack(d)) return;
        // Slider min 3.4 covers the whole dataset, so we only filter when
        // the user actually moves it above the minimum.
        if (minRating > 3.4) {
          if (d.rating == null || d.rating < minRating) return;
        }
        if (!pSet[d.bucket]) return;
        // Foreign cuisines (中/韩/西/南亚) bypass the regular genre filter —
        // they're gated entirely by hideForeignEl. When shown, they appear
        // regardless of which Japanese-cuisine boxes are checked.
        var cats = d.categories || [];
        var isForeign = cats.length > 0 && FOREIGN_GENRES.has(cats[0]);
        if (isForeign) {
          if (hideForeign) return;
        } else {
          // Genre filter: OR across selected categories. If none checked, hide all.
          if (!gAny) return;
          var catMatch = false;
          for (var i = 0; i < cats.length; i++) {
            if (gSet[cats[i]]) { catMatch = true; break; }
          }
          if (!catMatch) return;
        }
        if (book === 'yes' && !d.bookable) return;
        if (book === 'no' && d.bookable) return;
        if (onlyFav && !isFav(d)) return;
        keep.push(m);
      });
      cluster.addLayers(keep);
      document.getElementById('ff-count').textContent = keep.length;
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
    document.querySelectorAll('#ff-panel input[type=checkbox], #ff-panel input[type=radio]').forEach(function(el) {
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
    // Collapse / expand the panel — handy on phones where the filter sits
    // on top of the map. State is per-device, persisted in localStorage.
    var collapseBtn = document.getElementById('ff-collapse');
    var ffBody = document.getElementById('ff-body');
    var ffHeader = document.getElementById('ff-header');
    var COLLAPSE_KEY = 'tabelog.ffPanel.collapsed';
    function setCollapsed(c, persist) {
      ffBody.style.display = c ? 'none' : '';
      collapseBtn.textContent = c ? '+' : '−';
      collapseBtn.title = c ? '展开' : '折叠';
      ffHeader.style.borderBottom = c ? 'none' : '1px solid #e5e7eb';
      ffHeader.style.paddingBottom = c ? '0' : '5px';
      ffHeader.style.marginBottom = c ? '0' : '6px';
      if (persist !== false) {
        try { localStorage.setItem(COLLAPSE_KEY, c ? '1' : '0'); } catch (e) {}
      }
    }
    collapseBtn.addEventListener('click', function() {
      setCollapsed(ffBody.style.display !== 'none');
    });
    try {
      var saved = localStorage.getItem(COLLAPSE_KEY);
      if (saved === '1') {
        setCollapsed(true);
      } else if (saved === null && window.matchMedia
                 && window.matchMedia('(max-width: 768px)').matches) {
        // First-load on a phone: collapse the panel so it doesn't eat the
        // map. Don't persist — so opening on desktop later still defaults
        // to expanded (localStorage sync across viewports is rare but cheap
        // to avoid surprising).
        setCollapsed(true, false);
      }
    } catch (e) {}

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

    restoreFilterState();
    updateFavCount();
    updateBlackCount();
    apply();
    // Kick off the first pull (or stay in local mode if not configured).
    startSync();
  }
  function boot() { initMap(EMBEDDED_DATA); }
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


def fmt_popup(row: dict) -> str:
    def esc(s):
        if s is None:
            return ""
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    name = esc(row.get("name"))
    rating = esc(row.get("rating"))
    genre = esc(row.get("genre"))
    genre_zh = esc(row.get("genre_chinese"))
    dinner = row.get("dinner_upper")
    lunch = row.get("lunch_upper")
    seat = esc(row.get("seat_count"))
    station = esc(row.get("station"))
    addr = esc(row.get("address"))
    policy_zh = esc(row.get("reservation_policy_chinese"))
    bookable = parse_bool(row.get("tabelog_bookable"))
    url = esc(row.get("detail_url"))

    # Photo strip — thumbnails come from 150x150_square_ (~7KB each); click
    # opens the 640x640_rect_ version in a new tab.
    photos = [row.get(f"photo{i}_url") or "" for i in (1, 2, 3)]
    photos = [p for p in photos if p]
    if photos:
        thumbs = "".join(
            f'<a href="{esc(big)}" target="_blank" rel="noopener">'
            f'<img src="{esc(big.replace("640x640_rect_", "150x150_square_"))}" '
            f'loading="lazy" alt="" '
            f'onerror="this.parentElement.style.display=\'none\'"></a>'
            for big in photos
        )
        photo_html = f'<div class="rst-photos">{thumbs}</div>'
    else:
        photo_html = ""

    bookable_chip = (
        '<span class="rst-chip">可Tabelog预约</span>' if bookable
        else '<span class="rst-chip rst-chip-off">不可Tabelog预约</span>'
    )
    dinner_s = f"¥{dinner}" if dinner not in (None, "", "None") else "NA"
    lunch_s = f"¥{lunch}" if lunch not in (None, "", "None") else "NA"
    # Labels are filled in by JS at popupopen time based on current state;
    # the data-url binds each button to its restaurant.
    fav_btn = (
        f'<button class="ff-fav-btn rst-btn" data-url="{url}">'
        f'<span class="ff-fav-label">☆ 收藏</span></button>'
    )
    black_btn = (
        f'<button class="ff-black-btn rst-btn" data-url="{url}">'
        f'<span class="ff-black-label">🚫 弃用</span></button>'
    )
    return f"""
    <div class="rst-card">
      <div class="rst-header">
        <div class="rst-title">{name}<span class="rst-rating">★{rating}</span></div>
        <div class="rst-actions">{fav_btn}{black_btn}</div>
      </div>
      {photo_html}
      <div class="rst-genre">{genre} / {genre_zh}</div>
      <div class="rst-info">
        <div class="rst-info-row"><span class="rst-label">晚</span><span class="rst-value">{dinner_s}</span></div>
        <div class="rst-info-row"><span class="rst-label">车站</span><span class="rst-value">📍 {station}</span></div>
        <div class="rst-info-row"><span class="rst-label">午</span><span class="rst-value">{lunch_s}</span></div>
        <div class="rst-info-row"><span class="rst-label">座位</span><span class="rst-value">{seat or '—'}</span></div>
        <div class="rst-info-row"><span class="rst-label">地址</span><span class="rst-value">{addr}</span></div>
      </div>
      {f'<div class="rst-policy">{policy_zh}</div>' if policy_zh else ''}
      <div class="rst-footer">
        {bookable_chip}
        <a href="{url}" target="_blank" rel="noopener">Tabelog 详情 ↗</a>
      </div>
    </div>
    """


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fillempty", action=argparse.BooleanOptionalAction, default=False,
                    help="only geocode rows whose lat/lon are missing in CSV "
                         "(default: re-geocode all, cache makes repeats cheap)")
    return ap.parse_args()


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


def main() -> None:
    args = parse_args()

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
          f"(of {len(all_rows)} total){'  [fillempty mode]' if args.fillempty else ''}")

    cache = load_cache()
    geocoded: list[tuple[dict, dict]] = []
    failed: list[dict] = []
    n_skipped = 0
    client = httpx.Client(headers={"User-Agent": "omakase-tabelog-mapper/0.1"})
    for i, row in enumerate(addr_rows, 1):
        addr = row["address"]
        if args.fillempty:
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
    if args.fillempty:
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
    m = folium.Map(location=JAPAN_CENTER, zoom_start=6, tiles=None)
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

    # Build the per-restaurant JSON payload the filter JS consumes.
    fav_set = load_favorites()
    black_set = load_blacklist()
    payload = []
    cat_counts: dict[str, int] = {cat: 0 for cat in GENRE_CATEGORIES}
    unmapped_tokens: set[str] = set()
    for row, loc in geocoded:
        bkey, blabel, bcolor = price_bucket(row)
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
        emoji = GENRE_EMOJI.get(cats[0]) if cats else GENRE_EMOJI["其他"]
        payload.append({
            "lat": loc["lat"],
            "lon": loc["lon"],
            "name": row.get("name") or "",
            "rating": rating_num,
            "bucket": bkey,
            "color": bcolor,
            "categories": cats,
            "emoji": emoji,
            "bookable": parse_bool(row.get("tabelog_bookable")),
            "detail_url": url,
            "favorited": url in fav_set,
            "blacklisted": url in black_set,
            "popup": fmt_popup(row),
            "tooltip": f"{row.get('name')} · ★{row.get('rating')} · {blabel}",
        })
    if unmapped_tokens:
        print(f"  unmapped genre tokens (fell into 其他): {sorted(unmapped_tokens)}")
    n_fav = sum(1 for p in payload if p['favorited'])
    n_black = sum(1 for p in payload if p['blacklisted'])
    print(f"  favorites: {n_fav} from favorites.json, blacklist: {n_black} from blacklist.json")

    payload_json = json.dumps(payload, ensure_ascii=False)
    print(f"  payload: {len(payload_json.encode('utf-8')):,} bytes embedded")

    panel_html = build_filter_panel_html(cat_counts)
    default_off_json = json.dumps(sorted(DEFAULT_OFF_GENRES), ensure_ascii=False)
    filter_js = (
        FILTER_JS_TEMPLATE
        .replace("__PAYLOAD__", payload_json)
        .replace("__DEFAULT_OFF_GENRES__", default_off_json)
    )
    m.get_root().header.add_child(folium.Element(LOCATE_ASSETS))
    m.get_root().header.add_child(folium.Element(MOBILE_UX_ASSETS))
    m.get_root().html.add_child(folium.Element(BOTTOM_SHEET_HTML))
    m.get_root().html.add_child(folium.Element(MAP_FAB_HTML))
    m.get_root().html.add_child(folium.Element(panel_html))
    m.get_root().html.add_child(folium.Element(filter_js))

    m.save(str(OUT_HTML))
    print(f"\nMap written to {OUT_HTML}")
    print(f"  {len(payload)} restaurants embedded")


if __name__ == "__main__":
    main()
