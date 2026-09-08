"""In-process fake of worker/src/index.js (/api/state, /api/me, /api/session).

Faithful port of handleState's versioned-PUT logic:

    cur   = KV.get(key)
    curV  = cur.v or 0
    if typeof baseV === 'number' and baseV !== curV -> 409 + current blob
    parsed.v = curV + 1 ; KV.put(key, parsed) ; -> {v}

The only thing added is `kv_window_ms`: an artificial sleep inserted between
the KV read and the KV write, which is exactly the non-atomic window that
Cloudflare KV genuinely has (Workers KV has no CAS; a get followed by a put
is two independent round-trips). Setting it to 0 gives the tightest possible
window that a local threaded server can produce.

Auth is deliberately a no-op: every request is user `u1`. The audit forbids
real Google sign-in, and the client's own auth surface (localStorage
tabelog.auth + /api/me profile) is seeded directly. Nothing here touches the
production Worker or production KV.
"""
import json
import pathlib
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

DOCS = pathlib.Path(__file__).resolve().parents[2] / 'docs'

ORIGIN = None  # set by start()


class WorkerState:
    def __init__(self):
        self.kv = {}                 # 'state:u1' -> json string
        self.log = []                # request log
        self.lock = threading.Lock()
        self.kv_window_ms = 0        # sleep between KV get and KV put
        self.put_fail = False        # force 5xx on PUT
        self.put_status = None       # injected rejection for small-body limit tests
        self.session = True          # the seeded auth == a valid session cookie
        self.t0 = time.time()

    def rel(self):
        return round((time.time() - self.t0) * 1000, 1)

    def blob(self, key='state:u1'):
        raw = self.kv.get(key)
        return json.loads(raw) if raw else None

    def reset(self, initial=None):
        with self.lock:
            self.kv = {}
            self.log = []
            self.session = True
            self.put_status = None
            self.t0 = time.time()
            if initial is not None:
                self.kv['state:u1'] = json.dumps(initial)


STATE = WorkerState()


class Handler(SimpleHTTPRequestHandler):
    """One server for both the static site and the fake Worker.

    Same-origin on purpose: Chrome's Local Network Access rules block a
    loopback page from calling a different loopback port, and same-origin
    also removes CORS preflights from the timing picture.
    """
    protocol_version = 'HTTP/1.1'

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DOCS), **kw)

    def log_message(self, *a):
        pass

    @property
    def apipath(self):
        return urllib.parse.urlparse(self.path).path

    @property
    def qtab(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (q.get('tab') or ['?'])[0]

    @property
    def qkeepalive(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (q.get('keepalive') or ['0'])[0] == '1'

    def _cors(self):
        origin = self.headers.get('Origin') or '*'
        self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Credentials', 'true')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Allow-Methods', 'GET, PUT, POST, DELETE, OPTIONS')
        self.send_header('Cache-Control', 'no-store')

    def _send(self, code, body=b'', ctype='application/json'):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        if self.apipath == '/api/session':
            self._read_body()
            STATE.session = True
            self._send(200, json.dumps(PROFILE))
            return
        self._send(404, 'nope', 'text/plain')

    def do_DELETE(self):
        if self.apipath == '/api/session':
            # Like the real Worker: the cookie is gone for every tab from here.
            STATE.session = False
            with STATE.lock:
                STATE.log.append({'t': STATE.rel(), 'm': 'DELETE', 'p': '/api/session',
                                  'tab': self.qtab})
            self._send(200, json.dumps({'ok': True}))
            return
        self._send(404, 'nope', 'text/plain')

    def _read_body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n).decode() if n else ''

    def do_GET(self):
        if not self.apipath.startswith('/api/'):
            return super().do_GET()
        if self.apipath == '/api/me':
            with STATE.lock:
                STATE.log.append({'t': STATE.rel(), 'm': 'GET', 'p': '/api/me',
                                  'tab': self.qtab, 'status': 200 if STATE.session else 401})
            if not STATE.session:
                self._send(401, 'no auth', 'text/plain')
                return
            self._send(200, json.dumps(PROFILE))
            return
        if self.apipath == '/api/state':
            if not STATE.session:
                self._send(401, 'no auth', 'text/plain')
                return
            with STATE.lock:
                data = STATE.kv.get('state:u1')
                STATE.log.append({'t': STATE.rel(), 'm': 'GET', 'p': '/api/state',
                                  'tab': self.qtab,
                                  'resp': json.loads(data) if data else {}})
            self._send(200, data if data else '{}')
            return
        self._send(404, 'nope', 'text/plain')

    def do_HEAD(self):
        if not self.apipath.startswith('/api/'):
            return super().do_HEAD()
        self._send(404, 'nope', 'text/plain')

    def do_PUT(self):
        if self.apipath != '/api/state':
            self._send(404, 'nope', 'text/plain')
            return
        body = self._read_body()
        tab = self.qtab
        t_in = STATE.rel()
        if not STATE.session:
            self._send(401, 'no auth', 'text/plain')
            return
        body_bytes = len(body.encode('utf-8'))
        if body_bytes > 200_000:
            with STATE.lock:
                STATE.log.append({'t': t_in, 'm': 'PUT', 'tab': tab, 'status': 413,
                                  'chars': len(body), 'bytes': body_bytes})
            self._send(413, 'payload too large', 'text/plain')
            return
        try:
            parsed = json.loads(body)
        except Exception:
            self._send(400, 'invalid json', 'text/plain')
            return
        if STATE.put_status:
            with STATE.lock:
                STATE.log.append({'t': t_in, 'm': 'PUT', 'tab': tab, 'status': STATE.put_status,
                                  'bytes': body_bytes, 'w': parsed.get('w'), 'sent': _summ(parsed)})
            self._send(STATE.put_status, 'injected rejection', 'text/plain')
            return
        if STATE.put_fail:
            with STATE.lock:
                STATE.log.append({'t': t_in, 'm': 'PUT', 'tab': tab,
                                  'baseV': parsed.get('baseV'), 'status': 503,
                                  'sent': _summ(parsed)})
            self._send(503, 'injected failure', 'text/plain')
            return

        # --- faithful handleState PUT, with the KV get->put window exposed ---
        cur = STATE.kv.get('state:u1')
        curV = 0
        if cur:
            try:
                o = json.loads(cur)
                if isinstance(o.get('v'), int):
                    curV = o['v']
            except Exception:
                curV = 0
        baseV = parsed.get('baseV')
        if isinstance(baseV, int) and not isinstance(baseV, bool) and baseV != curV:
            with STATE.lock:
                STATE.log.append({'t': t_in, 'm': 'PUT', 'tab': tab, 'baseV': baseV,
                                  'curV': curV, 'status': 409, 'sent': _summ(parsed),
                                  'returned': _summ(json.loads(cur)) if cur else {}})
            self._send(409, cur if cur else '{}')
            return
        versioned = isinstance(baseV, int) and not isinstance(baseV, bool)
        if STATE.kv_window_ms:
            time.sleep(STATE.kv_window_ms / 1000.0)
        parsed.pop('baseV', None)
        parsed['v'] = curV + 1
        with STATE.lock:
            STATE.kv['state:u1'] = json.dumps(parsed)
            STATE.log.append({'t': t_in, 't_done': STATE.rel(), 'm': 'PUT', 'tab': tab,
                              'baseV': baseV, 'curV': curV, 'status': 200,
                              'newV': parsed['v'], 'w': parsed.get('w'),
                              'keepalive': self.qkeepalive, 'sent': _summ(parsed)})
        if versioned:
            self._send(200, json.dumps({'v': parsed['v']}))
        else:
            self._send(200, 'ok', 'text/plain')


def _summ(o):
    """Compact summary of a blob so timelines stay readable."""
    if not isinstance(o, dict):
        return o
    return {
        'favorites': o.get('favorites'),
        'blacklist': o.get('blacklist'),
        'bookmarks': [b.get('id') if isinstance(b, dict) else b
                      for b in (o.get('bookmarks') or [])],
        'nbm': len(o.get('bookmarks') or []),
        'v': o.get('v'),
        'w': o.get('w'),
    }


PROFILE = {
    'sub': 'u1',
    'email': 'owner@example.com',
    'name': 'Owner',
    'picture': '',
    'exp': int((time.time() + 90 * 86400) * 1000),
}


def start(port):
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv
