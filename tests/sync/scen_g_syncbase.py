"""Scenario G — M-001 (the P0): the shared merge base (tabelog.syncBase)
getting out of step with the shared state cache (omakase_state_cache_v2).

Before 2.0 each tab read both keys once at boot and then only wrote them,
so the pair on disk could describe a state no tab ever had:

    omakase_state_cache_v2 = tab A's memory       (missing tab B's edit)
    tabelog.syncBase       = tab B's push result  (v == the server's v)

The next boot pushed A's list with baseV == the server's v: no 409, no
merge, B's already-synced favourite deleted from the cloud.

G1  A's PUT fails (offline / 5xx) after B pushed; fresh boot re-pushes
G2  A's tab is closed inside the push debounce after B pushed
G3  control: same thing with only ONE tab
G4  pre-2.0 leftovers: the disk already holds {cache: stale + dirty,
    base: newer, no w} when the page boots — no tab ever saw a storage
    event for it, so only the boot-time rule can save the entry

Pass = every favourite either tab ever made is on the server after the
fresh boot, the status bar never shows green "已同步" while something is
missing, and no uncaught page errors.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mc_lib as L
import fake_worker as W
from playwright.sync_api import sync_playwright

U = L.urls(14)
S = L.short


def seed(fav, v=1, cache=None, base=None):
    W.STATE.reset({'favorites': list(fav), 'blacklist': [], 'bookmarks': [], 'v': v})
    return {'tabelog.auth': L.AUTH,
            'omakase_state_cache_v2': cache or L.cache_blob(fav, [], False),
            'tabelog.syncBase': base or L.sync_base(v, fav, [], []),
            'tabelog.bookmarks': []}


def disk(page):
    return page.evaluate("""() => {
      const g = k => { try { return JSON.parse(localStorage.getItem(k)||'null'); }
                       catch(e){ return null; } };
      const sb = g('tabelog.syncBase');
      return {cache: g('omakase_state_cache_v2'),
              syncBase: sb && {v: sb.v, w: sb.w, favorites: sb.favorites}};
    }""")


def case(br, mode):
    tl, errors = [], []
    if mode == 'pre20-disk':
        # What a pre-2.0 pair of tabs could leave behind: A's stale list
        # marked dirty, B's newer base, server == B's base, nobody has w.
        seeded = seed([U[0], U[7]], v=2,
                      cache=L.cache_blob([U[0], U[6]], [], True),
                      base=L.sync_base(2, [U[0], U[7]], [], []))
        ctx = L.make_context(br, seed=seeded)
        expected = {U[0], U[6], U[7]}
    else:
        ctx = L.make_context(br, seed=seed([U[0]], v=1))
        a = L.open_tab(ctx, 'A')
        b = L.open_tab(ctx, 'B') if mode != 'one-tab' else None
        tl.append({'step': 'start', 'kv': W.STATE.blob(), 'disk': disk(a)})
        expected = {U[0], U[6]}
        if b is not None:
            b.evaluate('u => window.__mcTapFav(u)', U[7])
            b.wait_for_timeout(L.PUSH_WAIT)
            expected.add(U[7])
            tl.append({'step': 'B favourites #7, push accepted',
                       'kv': W.STATE.blob(), 'disk': disk(b),
                       'ui_B': b.evaluate('window.__mcUI()')})
        if mode == 'offline':
            W.STATE.put_fail = True
            a.evaluate('u => window.__mcTapFav(u)', U[6])
            a.wait_for_timeout(L.PUSH_WAIT)
            d = disk(a)
            tl.append({'step': 'A favourites #6 while offline (PUT 503)',
                       'kv': W.STATE.blob(), 'disk': d,
                       'ui_A': a.evaluate('window.__mcUI()')})
            W.STATE.put_fail = False
        else:
            a.evaluate('u => window.__mcTapFav(u)', U[6])
            a.wait_for_timeout(200)          # inside the 2.5 s debounce
            d = disk(a)
            tl.append({'step': 'A favourites #6, tab closed 200ms later',
                       'kv': W.STATE.blob(), 'disk': d})
        errors += a.mc_errors
        a.close()
        if b is not None:
            errors += b.mc_errors
            b.close()

    # Everything closed (browser quit / Android killed Chrome), reopened.
    c = L.open_tab(ctx, 'C')
    c.wait_for_timeout(L.PUSH_WAIT + 1500)
    # M-3.2-11: G3 was intermittently red on a warm machine. The recovery it
    # measures is a chain — A's keepalive PUT (or, if that was lost with the
    # tab, C's boot-time merge) then C's own debounced push — and a single
    # fixed sleep scores a slow-but-correct chain as a lost favourite. Give
    # it a deadline instead of a stopwatch: still red if the entry never
    # arrives, no longer red because the entry arrived 300 ms late. Nothing
    # about what is asserted changes, and no probe pushes the page along.
    for _ in range(16):
        if expected <= set((W.STATE.blob() or {}).get('favorites') or []):
            break
        c.wait_for_timeout(500)
    kv = W.STATE.blob()
    ui_c = c.evaluate('window.__mcUI()')
    errors += c.mc_errors
    tl.append({'step': 'fresh boot in the same browser', 'kv': kv,
               'disk': disk(c), 'ui_C': ui_c})
    server_fav = set((kv or {}).get('favorites') or [])
    missing = sorted(S(x) for x in expected - server_fav)
    all_there = not missing
    # A green "已同步" while an entry is missing is the M-047 half of the P0.
    status_honest = all_there or ('已同步' not in ui_c['status'])
    try:
        (L.OUT / 'shots').mkdir(parents=True, exist_ok=True)
        c.screenshot(path=str(L.OUT / f'shots/G-{mode}-fresh-boot.jpg'),
                     quality=70, type='jpeg')
    except Exception:
        pass
    ctx.close()
    ident = {'offline': 'G1', 'closed': 'G2', 'one-tab': 'G3', 'pre20-disk': 'G4'}[mode]
    return {
        'id': ident, 'mode': mode,
        'pass': all_there and status_honest and not errors,
        'expected': sorted(S(x) for x in expected),
        'server_favorites': sorted(S(x) for x in server_fav),
        'missing_from_cloud': missing,
        'final_status_text': ui_c['status'],
        'page_errors': errors,
        'puts': [{'tab': x.get('tab'), 'baseV': x.get('baseV'),
                  'curV': x.get('curV'), 'status': x.get('status'),
                  'newV': x.get('newV'), 'keepalive': x.get('keepalive')}
                 for x in W.STATE.log if x['m'] == 'PUT'],
        'timeline': tl,
    }


def run(br=None):
    L.start_site()
    out = []

    def go(br):
        for mode in ('offline', 'closed', 'one-tab', 'pre20-disk'):
            r = case(br, mode)
            out.append(r)
            print(f"{r['id']} {mode:10s} pass={r['pass']} server={r['server_favorites']} "
                  f"missing={r['missing_from_cloud']} status={r['final_status_text']!r}")

    if br is None:
        with sync_playwright() as p:
            b = p.chromium.launch()
            go(b)
            b.close()
    else:
        go(br)
    L.write_json('evidence/scenario-g-syncbase.json', {'cases': out})
    return out


if __name__ == '__main__':
    sys.exit(0 if all(r['pass'] for r in run()) else 1)
