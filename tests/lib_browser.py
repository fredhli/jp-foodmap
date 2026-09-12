"""Shared Playwright plumbing for tests/compat and tests/smoke_playwright.py.

Serves docs/ over a local http.server (localStorage needs a real origin) and
blocks third-party hosts so a run is deterministic and costs nobody's quota.
Nothing here talks to api.jpfoodmap.com — the sync layer's own harness in
tests/sync covers that with a fake Worker.

    uv run python tests/compat/run.py
    uv run python tests/smoke_playwright.py
"""

from __future__ import annotations

import http.server
import socket
import socketserver
import threading
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"

# Requests that must never leave the machine during a test run. Tiles are
# allowed (the map needs something to draw) but everything third-party that
# costs quota, money, or wall-clock time is stubbed out.
BLOCKED_HOST_FRAGMENTS = (
    "tblg.k-img.com",          # Tabelog photos
    "emojicdn.elk.sh",         # emoji fallback CDN
    "translate.googleapis.com",
    "nominatim.openstreetmap.org",
    "api.jpfoodmap.com",       # never write to production sync
    "www.google-analytics.com",
)

# Console / network noise that is expected offline and must NOT fail a run.
CONSOLE_ALLOWLIST = (
    "accounts.google.com",     # GIS 403 when the page is not on the real origin
    "gsi/client",
    "ERR_BLOCKED_BY_CLIENT",
    "net::ERR_FAILED",
    "Failed to load resource",
    "The FetchEvent for",      # SW passthrough for a blocked request
    "service worker",
    "Content Security Policy",
    "GSI_LOGGER",              # "origin is not allowed for this client ID"
    "origin is not allowed",
)


def _free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Handler(http.server.SimpleHTTPRequestHandler):
    # Keep-alive: the page pulls restaurants.json (3.3 MB) plus a popups
    # variant (6.4 MB) plus tiles, and a reload does it again. HTTP/1.0 with
    # connection-close per request made those large reads fail intermittently
    # ("TypeError: Failed to fetch") once a browser-side cancel had killed a
    # handler thread.
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DOCS), **kw)

    def log_message(self, *a):  # keep the test output readable
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # The browser cancelled (navigation, reload, SW takeover). Normal.
            self.close_connection = True


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # connection resets are expected; don't spam the test output


@contextmanager
def serve_docs(port: int = 8926):
    """Serve docs/ on 127.0.0.1:<port> for the duration of the block."""
    port = _free_port(port)
    httpd = _Server(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def install_guards(page, errors: list[str]) -> None:
    """Block the third-party hosts and collect console errors / page errors
    that are not on the allowlist."""

    # Route ONLY the blocked hosts. A catch-all "**/*" handler makes every
    # local response round-trip through the driver, which turns the 3.3 MB
    # restaurants.json fetch into a flaky one (broken pipes, "Failed to
    # fetch"). Pattern-scoped aborts leave same-origin traffic untouched.
    for frag in BLOCKED_HOST_FRAGMENTS:
        page.route(f"**{frag}**", lambda r: r.abort())

    def on_console(msg):
        if msg.type != "error":
            return
        text = msg.text
        if any(a in text for a in CONSOLE_ALLOWLIST):
            return
        errors.append(f"console: {text}")

    page.on("console", on_console)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))


def seed_local_storage(page, entries: dict[str, str]) -> None:
    """Write localStorage before any page script runs. add_init_script fires
    on every navigation in this page, which is what makes a reload-based
    assertion (state survived a refresh) work."""
    js = "(() => { const e = %s; try { for (const k in e) localStorage.setItem(k, e[k]); } catch (_) {} })()" % (
        __import__("json").dumps(entries)
    )
    page.add_init_script(js)


# 4.0.0 barrier. 3.2.x waited on two filter-FAB counters: `.ff-total` (set as
# soon as restaurants.json parsed) and `.ff-count` (set only after the first
# apply(), which was also when the favorites counter and the markers reached
# their final state). Both elements are gone with the old presentation layer.
#
# `window.Adapter.ready === true` is the same moment expressed against the new
# architecture: Adapter.start() sets it only after Business.boot() has fetched
# and parsed restaurants.json, Business.init() has built the state, App.boot()
# has run every module's init and the first render has flushed — i.e. the
# payload has landed AND the first filter pass has been applied. The extra
# Data.restaurants check keeps the "the payload actually parsed" half explicit
# rather than implied.
READY_JS = (
    "() => !!(window.Adapter && window.Adapter.ready === true"
    "         && window.Data && Array.isArray(window.Data.restaurants)"
    "         && window.Data.restaurants.length > 0"
    "         && window.App && window.App.state && window.App.state.user)"
)


def wait_ready(page, timeout_ms: int = 60000) -> None:
    page.wait_for_function(READY_JS, timeout=timeout_ms)


def boot(page, base_url: str, timeout_ms: int = 60000) -> None:
    """Load the map and wait until the payload has been applied
    (Adapter.ready — see READY_JS)."""
    page.goto(base_url + "/index.html", wait_until="domcontentloaded",
              timeout=timeout_ms)
    wait_ready(page, timeout_ms)


def reload_and_wait(page, timeout_ms: int = 60000) -> None:
    """Refresh in place. Deliberately NOT reload()+boot(): boot() navigates,
    and a goto() on top of an in-flight reload aborts the page's own
    restaurants.json fetch, which the page then logs as a console error."""
    # Let the background popups prefetch (6.4 MB) finish first. Reloading on
    # top of it aborts the fetch, and the page correctly logs that abort as an
    # error — a test artefact that would otherwise look like a page bug.
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    wait_ready(page, timeout_ms)


def total_count(page) -> int:
    """Rows in the corpus. 3.2.x read the `.ff-total` counter in the filter FAB;
    4.0 has no such element, and the number that counter displayed is
    Data.restaurants.length — the same quantity, read at its source."""
    return int(page.evaluate("() => Data.restaurants.length"))


def shown_count(page) -> int:
    """Rows passing the current filters. 3.2.x read `.ff-count`, which the page
    wrote from the same apply() pass that decided which markers to draw. In 4.0
    that quantity is Data.M(App.state) — the module-facing match set the map and
    the result list both render from."""
    return int(page.evaluate("() => Data.M(App.state).length"))


# M-3.2-02: the phone (<750px) navigation entry, in one place so the suites
# follow the page. 3.1.x had a fixed bottom bar with four [data-ux-tab]
# buttons; 3.2.0 is back to the 2.3.0 overlay drawer, opened from the
# bottom-left segmented pill (#wb-seg, M-3.2-03), whose segments carry the
# same data-ux-tab attribute.
# M-3.2-06: a card has two close buttons and only one of them is on screen
# at a time — `#bs-close` in the phone card's head row, `.rst-close` inside
# the content for the desktop detail column. Clicking the hidden one times
# out, so pick by what is actually visible rather than by viewport width.
def close_detail(page) -> None:
    head = page.locator("#bs-close")
    if head.count() and head.is_visible():
        head.click()
    else:
        page.locator("#bs-content .rst-close").first.click()
    page.wait_for_timeout(200)


# 'map' means "close whatever is open and show the map".
def phone_tab(page, name: str) -> None:
    if name == "map":
        if page.locator("#bs-sheet.bs-open").count():
            close_detail(page)
        if page.locator("body.wb-fav-open").count():
            page.locator("#wb-fav-close").click()
        return
    # M-3.2-03: one tap on the pill segment opens the drawer on that tab.
    # With the drawer already up the pill is under it, and the tab row in
    # the drawer head is what a user reaches; a card hides the pill too.
    if page.locator("body.wb-fav-open").count():
        page.locator("#wb-tab-" + name).click()
        return
    if page.locator("#bs-sheet.bs-open").count():
        close_detail(page)
    page.locator('#wb-seg [data-ux-tab="' + name + '"]').click()
