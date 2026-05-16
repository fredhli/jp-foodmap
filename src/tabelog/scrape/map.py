"""
Plot data/tabelog/tabelog_osaka.csv (299 restaurants — full 3.65+ range)
on an interactive Osaka map with a client-side filter panel: rating
threshold, dinner-price bucket, Tabelog bookable.

Geocoding via GSI AddressSearch; results cached to data/cache/geocode_cache.json.

CSV is utf-8-sig so Japanese addresses round-trip through Excel cleanly.

Output: data/output/tabelog_osaka_map.html  (single file, open in any browser).
"""

import base64
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import folium
import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from folium.plugins import MarkerCluster

# Build-time access password. Same value must be entered in the browser to
# decrypt the payload. Changing this requires rebuilding + re-pushing the HTML.
ACCESS_PASSWORD = "085258"
# PBKDF2 iteration count. Higher = slower brute force, slower legit unlock.
# 200k ≈ ~100ms on a laptop — imperceptible for one attempt but multiplies
# the 10^6 6-digit space to ~28 hours (CPU) of pure brute force.
PBKDF2_ITERS = 200_000


def encrypt_payload(payload_bytes: bytes, password: str) -> dict:
    """AES-256-GCM with a PBKDF2-SHA256 derived key. Returns the bundle the
    browser needs to attempt decrypt: random salt + iv + ciphertext (which
    AESGCM already appends the auth tag to). All as base64 for JSON embed."""
    salt = os.urandom(16)
    iv = os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=PBKDF2_ITERS)
    key = kdf.derive(password.encode("utf-8"))
    ciphertext = AESGCM(key).encrypt(iv, payload_bytes, None)
    return {
        "salt":       base64.b64encode(salt).decode("ascii"),
        "iv":         base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "iters":      PBKDF2_ITERS,
    }

# Force UTF-8 stdout — Windows console defaults to cp1252 and chokes on
# the Japanese restaurant names printed during the geocode pass.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from tabelog.paths import (
    TABELOG_OSAKA_CSV,
    GEOCODE_CACHE,
    OSAKA_MAP_HTML,
    FAVORITES_JSON,
    BLACKLIST_JSON,
    OUTPUT_DIR,
    CACHE_DIR,
)

GSI_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"

CSV_PATH = TABELOG_OSAKA_CSV
CACHE_PATH = GEOCODE_CACHE
OUT_HTML = OSAKA_MAP_HTML

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

OSAKA_CENTER = (34.6937, 135.5023)

# 20 hand-picked Osaka tourist anchors. Coords typed from memory — accurate to
# ~100-300m, fine for travel reference. Districts (e.g. 心斎橋) are pinned to
# their commonly-cited centroid.
ATTRACTIONS = [
    ("大阪城", "🏯", 34.687315, 135.526201),
    ("道顿堀", "🎭", 34.668731, 135.501291),
    ("心斋桥筋商店街", "🛍️", 34.671804, 135.501306),
    ("通天阁", "🗼", 34.652500, 135.506306),
    ("新世界", "🍢", 34.652194, 135.506167),
    ("日本环球影城", "🎢", 34.665442, 135.432338),
    ("海游馆", "🐋", 34.654528, 135.428944),
    ("梅田蓝天大厦 空中庭园", "🌃", 34.705278, 135.489722),
    ("阿倍野海阔天空大厦", "🏢", 34.645833, 135.514444),
    ("黑门市场", "🍣", 34.665278, 135.506389),
    ("大阪站·梅田", "🚉", 34.702485, 135.495951),
    ("难波", "🚆", 34.663333, 135.501944),
    ("法善寺横丁", "🍶", 34.668056, 135.502222),
    ("美国村", "🎵", 34.672222, 135.498333),
    ("中之岛公园", "🌳", 34.692222, 135.512222),
    ("国立国际美术馆", "🖼️", 34.691389, 135.491667),
    ("住吉大社", "⛩️", 34.612222, 135.492500),
    ("万博纪念公园 太阳塔", "🌞", 34.809444, 135.532500),
    ("天保山大摩天轮", "🎡", 34.656111, 135.431111),
]


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


# Price buckets — keys must match the JS filter values below.
# (key, label, color, lower_inclusive, upper_exclusive)
PRICE_BUCKETS = [
    ("lt3k",   "< ¥3,000",         "#16a34a", None,   3000),
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


def build_filter_panel_html() -> str:
    price_rows = "\n".join(
        f'      <label style="display:block;margin:1px 0;">'
        f'<input type="checkbox" name="ff-price" value="{key}" checked> '
        f'<span style="display:inline-block;width:11px;height:11px;background:{color};'
        f'border-radius:50%;margin:0 4px;vertical-align:middle;"></span>{label}</label>'
        for key, label, color, _, _ in PRICE_BUCKETS
    )
    return f"""
<div id="ff-panel" style="
     position: fixed; top: 12px; right: 12px; z-index: 9999;
     background: rgba(255,255,255,0.97); padding: 10px 12px;
     border: 1px solid #d1d5db; border-radius: 8px;
     font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
     font-size: 12px; line-height: 1.45; color: #111827;
     box-shadow: 0 4px 12px rgba(0,0,0,0.12); width: 232px;">
  <div style="display:flex;justify-content:space-between;align-items:center;
              border-bottom:1px solid #e5e7eb;padding-bottom:5px;margin-bottom:6px;">
    <span style="font-weight:700;font-size:13px;">筛选</span>
    <span style="font-size:11px;color:#6b7280;">显示 <b id="ff-count">–</b> / <span id="ff-total">–</span></span>
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
  // ===== Encrypted payload + access gate =====
  //
  // The 299-restaurant payload is AES-256-GCM encrypted with a key derived
  // via PBKDF2-SHA256 from the user's 6-digit password. The HTML source
  // contains only the ciphertext + salt + iv. On first unlock we cache the
  // *derived key* (not the password) in localStorage for 24h, so reloads
  // skip the ~100ms PBKDF2 step. Wrong password = trial decrypt fails
  // (AES-GCM auth tag mismatch) → "密码错误" with no information leak.
  var ENCRYPTED = __ENCRYPTED__;
  var GATE_CACHE_KEY = 'omakase_gate_key_v1';
  var GATE_TTL_MS = 24 * 60 * 60 * 1000;

  function b64ToBytes(s) {
    return Uint8Array.from(atob(s), function(c) { return c.charCodeAt(0); });
  }
  function bytesToB64(b) {
    var s = '', arr = new Uint8Array(b);
    for (var i = 0; i < arr.length; i++) s += String.fromCharCode(arr[i]);
    return btoa(s);
  }
  async function deriveKey(password, saltBytes, iters) {
    var enc = new TextEncoder();
    var baseKey = await crypto.subtle.importKey(
      'raw', enc.encode(password), {name: 'PBKDF2'}, false, ['deriveKey']);
    return crypto.subtle.deriveKey(
      {name: 'PBKDF2', salt: saltBytes, iterations: iters, hash: 'SHA-256'},
      baseKey, {name: 'AES-GCM', length: 256}, true, ['decrypt']);
  }
  async function tryDecrypt(key) {
    try {
      var plain = await crypto.subtle.decrypt(
        {name: 'AES-GCM', iv: b64ToBytes(ENCRYPTED.iv)},
        key, b64ToBytes(ENCRYPTED.ciphertext));
      return JSON.parse(new TextDecoder().decode(plain));
    } catch (_) { return null; }
  }
  async function importCachedKey() {
    try {
      var c = JSON.parse(localStorage.getItem(GATE_CACHE_KEY) || '{}');
      if (!c.key || !c.exp || c.exp < Date.now()) return null;
      return await crypto.subtle.importKey(
        'raw', b64ToBytes(c.key), {name: 'AES-GCM'}, true, ['decrypt']);
    } catch (_) { return null; }
  }
  async function persistKey(key) {
    var raw = await crypto.subtle.exportKey('raw', key);
    localStorage.setItem(GATE_CACHE_KEY, JSON.stringify({
      key: bytesToB64(raw), exp: Date.now() + GATE_TTL_MS,
    }));
  }
  function showGateError(msg) {
    var el = document.getElementById('gate-msg');
    var btn = document.getElementById('gate-submit');
    var input = document.getElementById('gate-input');
    if (el) { el.style.color = '#dc2626'; el.textContent = msg; }
    if (btn) btn.disabled = false;
    if (input) { input.value = ''; input.focus(); }
  }
  function hideGate() {
    var bg = document.getElementById('gate-bg');
    if (bg) bg.remove();
  }
  async function bootGate() {
    // Fast path: cached key still valid → decrypt and start map.
    var cached = await importCachedKey();
    if (cached) {
      var data = await tryDecrypt(cached);
      if (data) { hideGate(); initMap(data); return; }
      // Key didn't fit (payload was re-encrypted with a new salt). Fall through.
      localStorage.removeItem(GATE_CACHE_KEY);
    }
    var form = document.getElementById('gate-form');
    var input = document.getElementById('gate-input');
    var msg = document.getElementById('gate-msg');
    var btn = document.getElementById('gate-submit');
    if (!form) return;
    if (input) input.focus();
    form.addEventListener('submit', async function(e) {
      e.preventDefault();
      var pw = (input.value || '').trim();
      if (!pw) return;
      btn.disabled = true;
      msg.style.color = '#6b7280';
      msg.textContent = '解锁中…';
      try {
        var key = await deriveKey(pw, b64ToBytes(ENCRYPTED.salt), ENCRYPTED.iters);
        var data = await tryDecrypt(key);
        if (data) {
          await persistKey(key);
          hideGate();
          initMap(data);
        } else {
          showGateError('密码错误');
        }
      } catch (err) {
        showGateError('解锁失败: ' + err.message);
      }
    });
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
      return {fav: d.fav || null, black: d.black || null};
    } catch (_) { return {fav: null, black: null}; }
  }
  function saveCache(state) {
    localStorage.setItem(CACHE_KEY, JSON.stringify({
      fav: Array.from(state.fav),
      black: Array.from(state.black),
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
    var etag = null, dirty = false, pushTimer = null, pollTimer = null;

    function configured() {
      var c = loadConfig();
      return c.gistId ? c : null;
    }
    function refreshAllMarkers() {
      markers.forEach(function(m){ m.setStyle(markerStyle(m._d)); });
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
            saveCache(state);
            refreshAllMarkers();
            setStatus('已同步 ' + new Date().toLocaleTimeString(), 'ok');
          });
        })
        .catch(function(e) { setStatus('同步失败: ' + e.message, 'err'); });
    }
    function push() {
      var c = configured();
      saveCache(state);            // always cache locally
      if (!c) { dirty = false; return; }
      if (!c.pat) {                // read-only mode
        dirty = false;
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
          setStatus('已同步 ' + new Date().toLocaleTimeString(), 'ok');
        })
        .catch(function(e) {
          dirty = false;
          setStatus('保存失败: ' + e.message + '（已存本地）', 'err');
        });
    }
    function schedulePush() {
      dirty = true;
      saveCache(state);
      clearTimeout(pushTimer);
      pushTimer = setTimeout(push, 500);
    }
    function startSync() {
      pull();
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

    // Blacklist wins visually because it implies "don't go here", which
    // matters more than "I starred this" when both are set.
    function markerStyle(d) {
      if (isBlack(d)) return {radius: 8, weight: 3, color: '#dc2626',
                              fillColor: d.color, fillOpacity: 0.35};
      if (isFav(d))   return {radius: 8, weight: 3, color: '#fbbf24',
                              fillColor: d.color, fillOpacity: 1.0};
      return {radius: 8, weight: 2, color: '#ffffff',
              fillColor: d.color, fillOpacity: 1.0};
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
      var m = L.circleMarker([d.lat, d.lon], markerStyle(d));
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
      m.setStyle(markerStyle(m._d));
      apply();
    });

    var ratingSlider = document.getElementById('ff-rating');
    var ratingLabel = document.getElementById('ff-rating-val');

    var onlyFavEl = document.getElementById('ff-only-fav');
    var hideBlackEl = document.getElementById('ff-hide-black');

    function apply() {
      var minRating = parseFloat(ratingSlider.value);
      ratingLabel.textContent = minRating.toFixed(2);
      var pSet = {};
      document.querySelectorAll('input[name=ff-price]:checked').forEach(function(c){ pSet[c.value]=1; });
      var bEl = document.querySelector('input[name=ff-bookable]:checked');
      var book = bEl ? bEl.value : 'all';
      var onlyFav = onlyFavEl.checked;
      var hideBlack = hideBlackEl.checked;

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
    document.getElementById('ff-reset').addEventListener('click', function() {
      ratingSlider.value = '3.4';
      document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = true; });
      document.querySelector('input[name=ff-bookable][value="all"]').checked = true;
      onlyFavEl.checked = false;
      hideBlackEl.checked = true;  // default: hide blacklist
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
          cfgMsg.textContent = hasFiles ? '✓ 配置成功，正在拉取' : '✓ 已连接，Gist 是空的，下次操作会写入';
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
  // Entry point: gate first; gate calls initMap(data) on successful decrypt.
  if (document.readyState !== 'loading') bootGate();
  else document.addEventListener('DOMContentLoaded', bootGate);
})();
</script>
"""


GATE_HTML = """
<div id="gate-bg" style="
     position:fixed;inset:0;z-index:100000;background:#0f172a;
     display:flex;align-items:center;justify-content:center;
     font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     color:#e2e8f0;">
  <form id="gate-form" autocomplete="off" style="text-align:center;
        background:rgba(255,255,255,0.04);padding:32px 36px;border-radius:14px;
        border:1px solid rgba(255,255,255,0.08);
        box-shadow:0 10px 40px rgba(0,0,0,0.4);">
    <div style="font-size:46px;line-height:1;margin-bottom:10px;">🔒</div>
    <div style="font-size:13px;color:#94a3b8;margin-bottom:20px;">
      请输入访问密码
    </div>
    <input id="gate-input" type="password" inputmode="numeric"
           pattern="[0-9]*" maxlength="6" autocomplete="off" autofocus
           style="font-size:30px;letter-spacing:10px;text-align:center;
                  padding:10px 14px;width:220px;background:#1e293b;
                  border:1px solid #334155;color:#f1f5f9;border-radius:8px;
                  font-family:'SF Mono',Menlo,monospace;outline:none;">
    <div id="gate-msg" style="height:18px;font-size:12px;margin-top:10px;
         color:#94a3b8;"></div>
    <button id="gate-submit" type="submit" style="
            margin-top:10px;padding:9px 28px;font-size:14px;font-weight:600;
            background:#2563eb;color:#fff;border:none;border-radius:7px;
            cursor:pointer;letter-spacing:1px;">
      解锁
    </button>
  </form>
</div>
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


def main() -> None:
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("address")]
    print(f"{len(rows)} rows with non-empty address")

    cache = load_cache()
    geocoded = []
    failed = []
    client = httpx.Client(headers={"User-Agent": "omakase-tabelog-mapper/0.1"})
    for i, row in enumerate(rows, 1):
        addr = row["address"]
        loc = geocode(addr, client, cache)
        if loc:
            geocoded.append((row, loc))
            print(
                f"  [{i}/{len(rows)}] {row.get('name')!r} -> "
                f"({loc['lat']:.4f}, {loc['lon']:.4f})"
            )
        else:
            failed.append(row)
            print(f"  [{i}/{len(rows)}] {row.get('name')!r}: NO MATCH ({addr!r})")
        if i % 5 == 0:
            save_cache(cache)
    save_cache(cache)

    print(f"\nGeocoded {len(geocoded)} / {len(rows)}; failed {len(failed)}")
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
    m = folium.Map(location=OSAKA_CENTER, zoom_start=12, tiles=None)
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
    for row, loc in geocoded:
        bkey, blabel, bcolor = price_bucket(row)
        try:
            rating_num = float(row.get("rating")) if row.get("rating") not in (None, "") else None
        except (TypeError, ValueError):
            rating_num = None
        url = row.get("detail_url") or ""
        payload.append({
            "lat": loc["lat"],
            "lon": loc["lon"],
            "name": row.get("name") or "",
            "rating": rating_num,
            "bucket": bkey,
            "color": bcolor,
            "bookable": parse_bool(row.get("tabelog_bookable")),
            "detail_url": url,
            "favorited": url in fav_set,
            "blacklisted": url in black_set,
            "popup": fmt_popup(row),
            "tooltip": f"{row.get('name')} · ★{row.get('rating')} · {blabel}",
        })
    n_fav = sum(1 for p in payload if p['favorited'])
    n_black = sum(1 for p in payload if p['blacklisted'])
    print(f"  favorites: {n_fav} from favorites.json, blacklist: {n_black} from blacklist.json")

    # Encrypt the whole payload — names, addresses, popups, favorites/blacklist
    # baseline — so the deployed HTML reveals nothing about Tabelog content
    # without the access password.
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    enc = encrypt_payload(payload_bytes, ACCESS_PASSWORD)
    print(f"  payload: {len(payload_bytes):,} bytes → encrypted "
          f"({len(enc['ciphertext']):,} b64), PBKDF2 iters={enc['iters']:,}")

    panel_html = build_filter_panel_html()
    filter_js = FILTER_JS_TEMPLATE.replace(
        "__ENCRYPTED__", json.dumps(enc)
    )
    m.get_root().header.add_child(folium.Element(LOCATE_ASSETS))
    m.get_root().html.add_child(folium.Element(GATE_HTML))
    m.get_root().html.add_child(folium.Element(panel_html))
    m.get_root().html.add_child(folium.Element(filter_js))

    m.save(str(OUT_HTML))
    print(f"\nMap written to {OUT_HTML}")
    print(f"  {len(payload)} restaurants embedded (encrypted); "
          f"gate password = {ACCESS_PASSWORD!r}")


if __name__ == "__main__":
    main()
