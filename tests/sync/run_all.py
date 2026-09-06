"""Run every sync scenario against the built docs/index.html.

    uv run python tests/sync/run_all.py            # all scenarios
    uv run python tests/sync/run_all.py g h        # a subset (by letter)
    SYNC_TEST_PORT=8931 uv run python tests/sync/run_all.py

Exit code 1 if any case fails. Evidence JSON + screenshots go to
mc_lib.OUT (default audit_outputs/impl-2026-09-05/m1/sync-layer/, override
with SYNC_TEST_OUT). Also takes two reference screenshots of a signed-in,
synced page at 1440x900 (desktop) and 416x657 (Fold outer screen).
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mc_lib as L
import fake_worker as W
from playwright.sync_api import sync_playwright

SCENARIOS = {
    'g': ('scen_g_syncbase', 'M-001 P0: shared merge base'),
    'a': ('scen_a_anon', 'M-003 anonymous two-tab matrix'),
    'b': ('scen_b_signedin', 'M-004 / M-002 signed-in two-tab + two-device'),
    'h': ('scen_h_signout', 'M-041 / M-124 accounts + sign-out'),
    'i': ('scen_i_misc', 'M-045 / M-046 / M-047 / M-053 acceptance'),
}


def reference_shots(br):
    U = L.urls(3)
    W.STATE.reset({'favorites': [U[0], U[1]], 'blacklist': [], 'bookmarks': [], 'v': 1, 'w': 'ref'})
    seed = {'tabelog.auth': L.AUTH,
            'tabelog.lang': 'zh-CN',   # 2.1.0: keep the first-visit language chooser out of the reference shots
            'omakase_state_cache_v2': L.cache_blob([U[0], U[1]], [], False),
            'tabelog.syncBase': L.sync_base(1, [U[0], U[1]], [], []),
            'tabelog.bookmarks': []}
    out = {}
    for name, vp in (('desktop-1440x900', (1440, 900)), ('fold-outer-416x657', (416, 657))):
        ctx = L.make_context(br, seed=seed, viewport={'width': vp[0], 'height': vp[1]},
                             device_scale_factor=1)
        p = L.open_tab(ctx, 'R')
        p.wait_for_timeout(2500)
        ui = p.evaluate('window.__mcUI()')
        (L.OUT / 'shots').mkdir(parents=True, exist_ok=True)
        p.screenshot(path=str(L.OUT / f'shots/ref-{name}.jpg'), quality=70, type='jpeg')
        out[name] = {'ui': ui, 'page_errors': list(p.mc_errors)}
        ctx.close()
    return out


def main(argv):
    picked = [a.lower() for a in argv] or list(SCENARIOS)
    L.OUT.mkdir(parents=True, exist_ok=True)
    L.start_site()
    summary = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'scenarios': {}, 'reference': None}
    any_fail = False
    with sync_playwright() as p:
        br = p.chromium.launch()
        for key in picked:
            mod_name, title = SCENARIOS[key]
            mod = __import__(mod_name)
            print(f'\n===== {key}: {title}')
            t0 = time.time()
            cases = mod.run(br)
            fails = [c['id'] for c in cases if not c.get('pass')]
            any_fail = any_fail or bool(fails)
            summary['scenarios'][key] = {
                'title': title, 'n': len(cases), 'failed': fails,
                'seconds': round(time.time() - t0, 1)}
            print(f'----- {key}: {len(cases) - len(fails)}/{len(cases)} pass'
                  + (f'  FAILED: {fails}' if fails else ''))
        summary['reference'] = reference_shots(br)
        br.close()
    summary['all_pass'] = not any_fail
    (L.OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print('\n' + json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if not any_fail else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
