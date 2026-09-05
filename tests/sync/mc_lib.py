"""Shared plumbing for the multi-context (two tabs / two windows) audit.

Everything runs against the committed build output docs/index.html, served
locally.  Nothing in the repo is modified: the one patched byte-range
(the API base URL) is applied to an in-memory copy and handed to the page
through page.route, so docs/index.html on disk is untouched.

Two pages created from the SAME BrowserContext share localStorage, cookies
and the origin's storage partition -- that is exactly what two browser tabs,
or two Chrome windows side by side on a Fold inner screen, are.
"""
import json
import os
import pathlib
import re
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCS = REPO / 'docs'
# Evidence / screenshots. Override with SYNC_TEST_OUT; default is the M1
# report folder (gitignored via audit_outputs/).
OUT = pathlib.Path(os.environ.get(
    'SYNC_TEST_OUT', REPO / 'audit_outputs/impl-2026-09-05/m1/sync-layer'))
# Scratch files (import fixtures) + the CDN cache. Leaflet & co. are
# fetched once from jsdelivr/cdnjs into the cache; every run after that is
# offline.
SCRATCH = pathlib.Path(os.environ.get(
    'SYNC_TEST_SCRATCH', pathlib.Path.home() / '.cache' / 'jpfoodmap-sync-tests'))
CDN = SCRATCH / 'cdn'

# One origin for both the page and the fake Worker (see fake_worker.Handler):
# Chrome blocks loopback->other-loopback-port fetches (Local Network Access).
SITE_PORT = int(os.environ.get('SYNC_TEST_PORT', '8921'))
SITE = f'http://127.0.0.1:{SITE_PORT}'
API = f'http://127.0.0.1:{SITE_PORT}/api'

BLOCK = ('basemaps.cartocdn.com', 'nominatim.openstreetmap.org',
         'accounts.google.com', 'tblg.k-img.com', 'emojicdn.elk.sh',
         'translate.googleapis.com', 'www.wikidata.org', 'wikipedia.org')

CDN_MAP = {
    'leaflet@1.9.3/dist/leaflet.js': 'leaflet.js',
    'leaflet@1.9.3/dist/leaflet.css': 'leaflet.css',
    'L.Control.Locate.min.js': 'L.Control.Locate.min.js',
    'L.Control.Locate.min.css': 'L.Control.Locate.min.css',
    'leaflet.markercluster.js': 'leaflet.markercluster.js',
    'MarkerCluster.Default.css': 'MarkerCluster.Default.css',
    'MarkerCluster.css': 'MarkerCluster.css',
}
CDN_SRC = {
    'leaflet.js': 'https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js',
    'leaflet.css': 'https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.css',
    'L.Control.Locate.min.js': 'https://cdn.jsdelivr.net/npm/leaflet.locatecontrol@0.79.0/dist/L.Control.Locate.min.js',
    'L.Control.Locate.min.css': 'https://cdn.jsdelivr.net/npm/leaflet.locatecontrol@0.79.0/dist/L.Control.Locate.min.css',
    'leaflet.markercluster.js': 'https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.4.1/leaflet.markercluster.js',
    'MarkerCluster.Default.css': 'https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.4.1/MarkerCluster.Default.css',
    'MarkerCluster.css': 'https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.4.1/MarkerCluster.css',
}


def ensure_cdn_cache():
    """Populate CDN once (network), then serve every run from disk."""
    CDN.mkdir(parents=True, exist_ok=True)
    for fn, url in CDN_SRC.items():
        p = CDN / fn
        if p.exists() and p.stat().st_size > 0:
            continue
        # The page's own <script src> tells us the exact URL it wants; fall
        # back to the pinned one above.
        m = re.search(r'https://[^"\']+/' + re.escape(fn.split('/')[-1]), patched_html())
        src = m.group(0) if m else url
        with urllib.request.urlopen(src, timeout=60) as r:
            p.write_bytes(r.read())


# ------------------------------------------------- static site + fake Worker
_SRV = None


def start_site():
    """One server per process; every scenario's run() may call this."""
    global _SRV
    if _SRV is not None:
        return _SRV
    import fake_worker
    ensure_cdn_cache()
    fake_worker.DOCS = DOCS
    srv = ThreadingHTTPServer(('127.0.0.1', SITE_PORT), fake_worker.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _SRV = srv
    return srv


# ---------------------------------------------------------------- page HTML
_HTML = None


def patched_html():
    """docs/index.html with the API base pointed at the local fake Worker."""
    global _HTML
    if _HTML is None:
        t = (DOCS / 'index.html').read_text(encoding='utf-8')
        assert "https://api.jpfoodmap.com/api" in t
        t = t.replace("https://api.jpfoodmap.com/api", API)
        _HTML = t
    return _HTML


DRIVER = r"""
(() => {
  // Tag every request to the fake Worker with the tab name so the request
  // timeline can attribute PUTs.  Query-string only: does not change the
  // CORS preflight class of any request.
  const of = window.fetch.bind(window);
  window.__mcKeepalive = [];   // M-045: PUTs sent with {keepalive:true}
  window.fetch = function(input, init) {
    try {
      if (typeof input === 'string' && input.indexOf('/api/') >= 0) {
        input += (input.indexOf('?') < 0 ? '?' : '&') + 'tab=' + (window.__MC_TAB || '?');
        if (init && init.keepalive) {
          window.__mcKeepalive.push({t: Date.now(), method: init.method,
                                     bytes: (init.body || '').length});
          input += '&keepalive=1';
        }
      }
    } catch (e) {}
    return of(input, init);
  };

  const $ = (s) => document.querySelector(s);

  // Favourite / blacklist go through the page's own delegated click handler
  // (map.py:5938 `document.addEventListener('click', ...)`), i.e. the real
  // toggleFav()/toggleBlack() -> schedulePush() -> push() path.
  function tap(cls, url) {
    const b = document.createElement('button');
    b.className = cls;
    b.setAttribute('data-url', url);
    b.innerHTML = '<span class="ff-fav-label">x</span><span class="ff-black-label">y</span>';
    b.style.cssText = 'position:fixed;left:-9999px';
    document.body.appendChild(b);
    b.click();
    b.remove();
  }
  window.__mcTapFav = (u) => tap('ff-fav-btn', u);
  window.__mcTapBlack = (u) => tap('ff-black-btn', u);

  window.__mcMap = () => {
    const el = document.querySelector('.folium-map');
    return el ? window[el.id] : null;
  };

  // Right-click -> "⭐ 加入收藏" -> name -> save.  Same code path a user takes.
  window.__mcAddBookmark = (name, lat, lon) => new Promise((res) => {
    const m = window.__mcMap();
    if (!m) return res('no-map');
    m.fire('contextmenu', {latlng: L.latLng(lat, lon)});
    setTimeout(() => {
      const add = document.getElementById('ff-add-bm');
      if (!add) return res('no-add-btn');
      add.click();
      setTimeout(() => {
        const n = document.getElementById('bm-name');
        const e = document.getElementById('bm-emoji');
        if (!n) return res('no-modal');
        n.value = name;
        if (e) e.value = '';
        const save = document.querySelector('#bm-modal .bm-save');
        if (!save) return res('no-save');
        save.click();
        setTimeout(() => res('ok'), 30);
      }, 80);
    }, 80);
  });

  // Hide a built-in landmark: click its marker, then the 隐藏 button in the popup.
  function findMarkerAt(lat, lon) {
    const m = window.__mcMap();
    let found = null;
    function walk(layer) {
      if (found || !layer) return;
      if (layer.getLatLng && layer.options && typeof layer.options.icon === 'object') {
        const ll = layer.getLatLng();
        if (Math.abs(ll.lat - lat) < 1e-6 && Math.abs(ll.lng - lon) < 1e-6) {
          found = layer; return;
        }
      }
      if (layer.eachLayer) layer.eachLayer(walk);
    }
    if (m) m.eachLayer(walk);
    return found;
  }
  window.__mcHideBuiltin = (lat, lon) => new Promise((res) => {
    const target = findMarkerAt(lat, lon);
    if (!target) return res('marker-not-found');
    target.fire('click');
    setTimeout(() => {
      const b = document.getElementById('bm-hide');
      if (!b) return res('no-hide-btn');
      b.click();
      setTimeout(() => res('ok'), 30);
    }, 120);
  });

  window.__mcLS = () => {
    const g = (k) => { try { return JSON.parse(localStorage.getItem(k) || 'null'); }
                       catch (e) { return '<<unparseable>>'; } };
    return {
      cache: g('omakase_state_cache_v2'),
      bookmarks: g('tabelog.bookmarks'),
      syncBase: g('tabelog.syncBase'),
      auth: g('tabelog.auth'),
    };
  };

  // What THIS tab currently believes, read off the live UI counters that
  // updateFavCount()/updateBlackCount() drive from the in-memory sets.
  window.__mcUI = () => ({
    fav: +(document.getElementById('ff-fav-count') || {}).textContent || 0,
    black: +(document.getElementById('ff-black-count') || {}).textContent || 0,
    status: (document.getElementById('ff-sync-status') || {}).textContent || '',
    fabNeedsSync: !!(document.getElementById('ff-fab') || {classList: {contains: () => false}})
                    .classList.contains('needs-sync'),
    fabPending: !!(document.getElementById('ff-fab') || {classList: {contains: () => false}})
                    .classList.contains('needs-sync-pending'),
  });

  window.__mcVisChange = () => document.dispatchEvent(new Event('visibilitychange'));
  // Pretend the tab went to the background: visibilityState reads 'hidden'
  // for the duration of the event, then goes back to normal.
  window.__mcGoHidden = () => {
    const d = Object.getOwnPropertyDescriptor(Document.prototype, 'visibilityState');
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
    document.dispatchEvent(new Event('visibilitychange'));
    delete document.visibilityState;
    if (d) Object.defineProperty(Document.prototype, 'visibilityState', d);
  };
  // Make every localStorage write throw (private mode / quota) — M-046.
  window.__mcBlockStorage = () => {
    Object.defineProperty(Storage.prototype, 'setItem', {configurable: true,
      value: function() { throw new DOMException('audit: storage blocked', 'QuotaExceededError'); }});
  };
})();
"""


def make_context(browser, seed=None, extra_init=None, **ctx_kw):
    ctx = browser.new_context(**ctx_kw)
    # Seed localStorage before any page script runs (shared by every page
    # in this context, because they share the origin's storage).
    boot = ''
    if seed:
        # Guarded: add_init_script runs on EVERY navigation in the context,
        # so without the sentinel a second tab (or a go_back) would silently
        # restore the seed and hide the very state we are testing.
        sets = ''.join(
            f"localStorage.setItem({json.dumps(k)},{json.dumps(json.dumps(v) if not isinstance(v, str) else v)});"
            for k, v in seed.items())
        boot = ("try{if(!localStorage.getItem('__mc_seeded')){" + sets +
                "localStorage.setItem('__mc_seeded','1');}}catch(e){}")
    if extra_init:
        boot = extra_init + '\n' + boot
    if boot:
        ctx.add_init_script(boot)
    ctx.add_init_script(DRIVER)

    def route_doc(route):
        route.fulfill(status=200, content_type='text/html; charset=utf-8',
                      body=patched_html())

    def route_cdn(route):
        u = route.request.url
        for frag, fn in CDN_MAP.items():
            if frag in u:
                p = CDN / fn
                ct = 'text/css' if fn.endswith('.css') else 'application/javascript'
                route.fulfill(status=200, content_type=ct, body=p.read_bytes())
                return
        route.abort()

    ctx.route(f'{SITE}/', route_doc)
    ctx.route(f'{SITE}/index.html', route_doc)
    ctx.route('**/sw.js', lambda r: r.abort())          # keep SW out of the way
    ctx.route('https://cdn.jsdelivr.net/**', route_cdn)
    ctx.route('https://cdnjs.cloudflare.com/**', route_cdn)
    for host in BLOCK:
        ctx.route(f'**{host}**', lambda r: r.abort())
    return ctx


IGNORED_CONSOLE = ('net::ERR_', 'Failed to load resource', 'accounts.google.com',
                   'ERR_BLOCKED', 'the server responded with a status of 403',
                   # SW registration: the harness aborts **/sw.js on purpose
                   'An unknown error occurred when fetching the script',
                   # idle popups.json prefetch aborted by a reload (sign-out)
                   'popups.json load failed')


def open_tab(ctx, name, timeout=60000):
    page = ctx.new_page()
    # Uncaught exceptions + console.error lines (resource-load noise from
    # the blocked hosts filtered out). Scenarios assert these stay empty.
    page.mc_errors = []
    page.on('pageerror', lambda e: page.mc_errors.append('pageerror: ' + str(e)))

    def _console(msg):
        if msg.type != 'error':
            return
        text = msg.text
        if any(x in text for x in IGNORED_CONSOLE):
            return
        page.mc_errors.append('console: ' + text)
    page.on('console', _console)
    page.add_init_script(f'window.__MC_TAB = {json.dumps(name)};')
    page.goto(SITE + '/', wait_until='domcontentloaded', timeout=timeout)
    page.wait_for_function(
        "() => window.__mcMap && window.__mcMap() && "
        "document.getElementById('ff-fav-count') !== null", timeout=timeout)
    page.wait_for_timeout(400)
    return page


# push debounce (map.py schedulePush) is 2.5 s; wait this long for a PUT to land
PUSH_WAIT = 3600

AUTH = {'sub': 'u1', 'email': 'owner@example.com', 'name': 'Owner',
        'picture': '', 'exp': int((time.time() + 90 * 86400) * 1000)}


def cache_blob(fav, black, dirty=False):
    return {'fav': list(fav), 'black': list(black), 'dirty': dirty}


def sync_base(v, fav, black, bookmarks=None, sub='u1'):
    return {'v': v, 'sub': sub, 'favorites': list(fav), 'blacklist': list(black),
            'bookmarks': bookmarks or []}


def urls(n=12):
    rows = json.loads((DOCS / 'data' / 'restaurants.json').read_text())
    return [r['detail_url'] for r in rows[:n]]


def snap(page, label=''):
    ls = page.evaluate('window.__mcLS()')
    ui = page.evaluate('window.__mcUI()')
    return {'label': label, 'ls': _compact(ls), 'ui': ui}


def _compact(ls):
    """Keep timelines readable: bookmarks -> id list + count."""
    out = dict(ls)
    bm = out.get('bookmarks')
    if isinstance(bm, list):
        out['bookmarks'] = {'n': len(bm),
                            'user': [b.get('id') for b in bm
                                     if isinstance(b, dict)
                                     and str(b.get('id', '')).startswith('bm-')],
                            'hidden': [b.get('id') for b in bm
                                       if isinstance(b, dict)
                                       and b.get('category') == 'hidden']}
    sb = out.get('syncBase')
    if isinstance(sb, dict):
        out['syncBase'] = {'v': sb.get('v'), 'sub': sb.get('sub'),
                           'favorites': sb.get('favorites'),
                           'blacklist': sb.get('blacklist'),
                           'nbm': len(sb.get('bookmarks') or [])}
    out.pop('auth', None)
    return out


def short(u):
    """Short label for a tabelog detail url."""
    return u.rstrip('/').split('/')[-2] + '/' + u.rstrip('/').split('/')[-1]


def write_json(path, obj):
    p = OUT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    return str(p)
