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

    var markers = data.map(function(d) {
      var m = L.marker([d.lat, d.lon], {icon: makeIcon(d)});
      m.bindPopup(d.popup, {maxWidth: 360});
      m.bindTooltip(d.tooltip);
      m._d = d;
      return m;
    });
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

    // Popups are re-instantiated each open, so sync button state then.
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
    function setCollapsed(c) {
      ffBody.style.display = c ? 'none' : '';
      collapseBtn.textContent = c ? '+' : '−';
      collapseBtn.title = c ? '展开' : '折叠';
      ffHeader.style.borderBottom = c ? 'none' : '1px solid #e5e7eb';
      ffHeader.style.paddingBottom = c ? '0' : '5px';
      ffHeader.style.marginBottom = c ? '0' : '6px';
      try { localStorage.setItem(COLLAPSE_KEY, c ? '1' : '0'); } catch (e) {}
    }
    collapseBtn.addEventListener('click', function() {
      setCollapsed(ffBody.style.display !== 'none');
    });
    try {
      if (localStorage.getItem(COLLAPSE_KEY) === '1') setCollapsed(true);
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

    bookable_chip = (
        '<span style="background:#3b9c4f;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;">可Tabelog预约</span>'
        if bookable
        else '<span style="background:#888;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;">不可Tabelog预约</span>'
    )
    dinner_s = f"晚 ¥{dinner}" if dinner not in (None, "", "None") else "晚 NA"
    lunch_s = f"午 ¥{lunch}" if lunch not in (None, "", "None") else "午 NA"
    # Labels are filled in by JS at popupopen time based on current state;
    # the data-url binds each button to its restaurant.
    fav_btn = (
        f'<button class="ff-fav-btn" data-url="{url}" '
        f'style="padding:3px 8px;font-size:12px;cursor:pointer;'
        f'border:1px solid #d1d5db;border-radius:4px;background:#f9fafb;">'
        f'<span class="ff-fav-label">☆ 收藏</span></button>'
    )
    black_btn = (
        f'<button class="ff-black-btn" data-url="{url}" '
        f'style="padding:3px 8px;font-size:12px;cursor:pointer;'
        f'border:1px solid #d1d5db;border-radius:4px;background:#f9fafb;">'
        f'<span class="ff-black-label">🚫 弃用</span></button>'
    )
    return f"""
    <div style="font-family:sans-serif;font-size:13px;max-width:340px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px;margin-bottom:4px;">
        <div style="font-weight:bold;font-size:15px;flex:1;min-width:0;">
          {name} <span style="color:#c33;">★{rating}</span>
        </div>
        <div style="display:flex;gap:4px;flex-shrink:0;">
          {fav_btn}
          {black_btn}
        </div>
      </div>
      <div style="color:#555;margin-bottom:4px;">{genre} / {genre_zh}</div>
      <div style="margin-bottom:4px;">{dinner_s} &nbsp; {lunch_s} &nbsp; · &nbsp; {seat}</div>
      <div style="margin-bottom:4px;">📍 {station}</div>
      <div style="font-size:12px;color:#666;margin-bottom:6px;">{addr}</div>
      <div style="margin-bottom:6px;">{bookable_chip}</div>
      <div style="font-size:11px;color:#666;margin-bottom:6px;">{policy_zh}</div>
      <div><a href="{url}" target="_blank">Tabelog 详情 ↗</a></div>
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

    # Two switchable base maps (radio in the layer control):
    #   - 公路: CartoDB Voyager — modern Google-Maps-like look, our default
    #   - 公共交通: GSI 標準地図 — Japan's official survey map; renders JR /
    #     private rail / subway lines and full station names. Picked over
    #     OpenRailwayMap because that one 403s requests with empty Referer,
    #     which is what browsers send when the HTML is opened via file://.
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
    folium.TileLayer(
        tiles="https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png",
        attr=('<a href="https://maps.gsi.go.jp/development/ichiran.html">'
              '地理院タイル</a>'),
        name="公共交通 (GSI 淡色)",
        max_zoom=18,
        show=False,
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

    folium.LayerControl(collapsed=True, position="bottomleft").add_to(m)

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
    m.get_root().html.add_child(folium.Element(panel_html))
    m.get_root().html.add_child(folium.Element(filter_js))

    m.save(str(OUT_HTML))
    print(f"\nMap written to {OUT_HTML}")
    print(f"  {len(payload)} restaurants embedded")


if __name__ == "__main__":
    main()
