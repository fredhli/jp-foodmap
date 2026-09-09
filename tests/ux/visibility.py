"""The 3.2.0 acceptance numbers, as an executable gate (M-3.2-11).

    .venv-wsl/bin/python tests/ux/visibility.py --output /tmp/jpfoodmap-vis
    .venv-wsl/bin/python tests/ux/visibility.py --output DIR --only iphone402

This is the audit's `audit_outputs/3.2.0-plan/ux-phone/run_flows.py`, kept
only for the parts the plan turned into thresholds and rewritten against
3.2.0's selectors. The audit script drove two *running servers* (2.3.0 on
8231, 3.1.1 on 8311) through 23 narrative steps and wrote a comparison
table; it also still clicks `#phone-nav`, which 3.2.0 deleted. What is worth
keeping is its pixel sampler, so that is what came across verbatim.

The thresholds, from the engineering plan's acceptance section:

| state         | 402x874 | 475x751 | 591x689 |
|---------------|---------|---------|---------|
| home          | >= 78%  | >= 78%  | (recorded) |
| drawer open   | >= 10%  | (recorded) | >= 30% |
| detail open   | >= 18%  | >= 18%  | >= 18%  |

plus, on every viewport and every state: no page-level horizontal overflow,
and none of the strings on the audit's truncation list clipped. And, once
per run and independent of the browser: no full-width comma or semicolon
anywhere in the shipped EN / JA translation tables.

Two measurement notes, both inherited from the audit so the numbers stay
comparable with its table:

  * "visible" for a drawer state counts the scrim. The 2.3.0 overlay drawer
    leaves the map painted behind a `.18` scrim — dimmed, still legible,
    still the thing that tells you where you are — and that is exactly the
    36% the plan quotes for 591. An opaque panel scores zero either way.
  * the home state is measured with the intro bar up, which is what the
    plan's 78% baseline was measured with; hiding it would inflate the
    number by four points and stop protecting the first screen a new
    visitor sees.

402x874 is measured on WebKit and nothing else: it is an iPhone, and a
Chromium pass on those dimensions is not evidence about iOS Safari.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
import lib_browser  # noqa: E402


# The Fold rows are Chromium, the iPhone row is WebKit. `home`/`drawer`/
# `detail` are the floors from the plan; None means "record it, don't gate
# it" (the plan only names two devices for home and one for the drawer).
VIEWPORTS = {
    'iphone402': dict(w=402, h=874, browser='webkit', dpr=3,
                      home=0.78, drawer=0.10, detail=0.18),
    'fold475':   dict(w=475, h=751, browser='chromium', dpr=2,
                      home=0.78, drawer=None, detail=0.18),
    'fold591':   dict(w=591, h=689, browser='chromium', dpr=2,
                      home=None, drawer=0.30, detail=0.18),
}

# Strings the audit caught clipped at a phone width (ux-phone/table_E.md).
# Every one of them is a whole fact the user needs — a price ceiling, a
# booking verdict, a distance, how many landmarks a layer holds — so a
# clip is a lost fact, not a cosmetic nit. The station-name rows in that
# table are all fold816 / fold932, which are column layouts and out of
# scope here.
TRUNCATION_WATCH = (
    '#lp-n-builtin',            # "219 built-in landmarks + 0 of your own"
    '.lp-sub',                  # the layer sub-title that ran off the popover
    '.wb-row-pr',               # "¥10,000 - 20,000 max"
    '.wb-row-net',              # "No online booking link detected"
    '.ux-distance',             # "152 m"
    '.rst-list-new',            # the "+" chip on the detail card
)

# D6 / SPEC F: an EN or JA string must never carry a full-width comma or
# semicolon. Those appear when a long Chinese sentence is split on its own
# punctuation, each fragment translated, and the fragments joined back with
# the original separators — which is why 3.2.0 moved that copy to
# whole-sentence keys.
FULLWIDTH = '，；'

METRICS_JS = r"""
() => {
  const W = innerWidth, H = innerHeight;
  const mapEl = document.querySelector('.leaflet-container');
  const step = 10; let total = 0, vis = 0;
  const alphaOf = (bg) => { const m = bg && bg.match(/rgba?\(([^)]+)\)/); if (!m) return 0;
    const p = m[1].split(',').map(s => parseFloat(s)); return p.length < 4 ? 1 : p[3]; };
  // 0 = see-through, 1 = scrim (dims but the map is still readable), 2 = opaque
  function opaque(el) {
    if (el === document.documentElement || el === document.body) return 0;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return 0;
    const a = alphaOf(cs.backgroundColor) * parseFloat(cs.opacity || 1);
    if (a >= 0.5) return 2;
    if (a > 0.05) return 1;
    if (cs.backgroundImage !== 'none') return 2;
    if (/^(IMG|svg|SVG|CANVAS|BUTTON|INPUT|SELECT|TEXTAREA|VIDEO)$/.test(el.tagName)) return 2;
    for (const n of el.childNodes) { if (n.nodeType === 3 && n.textContent.trim()) return 2; }
    return 0;
  }
  let dim = 0;
  for (let y = step / 2; y < H; y += step) for (let x = step / 2; x < W; x += step) {
    total++;
    const els = document.elementsFromPoint(x, y);
    let occ = false, hit = false, scrim = false;
    for (const el of els) {
      if (mapEl && (el === mapEl || mapEl.contains(el))) { hit = true; break; }
      const o = opaque(el);
      if (o === 2) { occ = true; break; }
      if (o === 1) scrim = true;
    }
    // An inert map (setBackgroundInert while a drawer is open) is skipped by
    // hit-testing even though it is painted: fall back to geometry when
    // nothing opaque was found.
    if (!hit && !occ && mapEl) {
      const mr = mapEl.getBoundingClientRect(); const mcs = getComputedStyle(mapEl);
      if (mcs.visibility !== 'hidden' && mcs.display !== 'none' && x >= mr.left && x <= mr.right && y >= mr.top && y <= mr.bottom) hit = true;
    }
    if (hit && !occ) { if (scrim) dim++; else vis++; }
  }
  const descr = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    else if (el.className && typeof el.className === 'string') s += '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.');
    const t = (el.textContent || '').trim().replace(/\s+/g, ' ');
    return s + (t ? ' "' + t.slice(0, 40) + '"' : '');
  };
  const visibleRect = (el) => { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < H && r.right > 0 && r.left < W; };
  const shown = (el) => { for (let e = el; e && e !== document.body; e = e.parentElement) {
      const cs = getComputedStyle(e); if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false; }
    return true; };
  const overflowing = [];
  for (const el of document.querySelectorAll('body *')) {
    if (mapEl && mapEl.contains(el) && el !== mapEl) continue;
    if (!shown(el)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || r.bottom < 0 || r.top > H) continue;
    if (r.right > W + 1 || r.left < -1) {
      let clipped = false;
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const o = getComputedStyle(p).overflowX; if (o === 'hidden' || o === 'clip' || o === 'auto' || o === 'scroll') { clipped = true; break; } }
      overflowing.push({el: descr(el), left: Math.round(r.left), right: Math.round(r.right), clippedByAncestor: clipped});
    }
    if (overflowing.length >= 12) break;
  }
  const truncated = [];
  for (const el of document.querySelectorAll('body *')) {
    if (mapEl && mapEl.contains(el)) continue;
    if (!shown(el) || !visibleRect(el)) continue;
    const cs = getComputedStyle(el);
    const clipsX = cs.overflowX === 'hidden' || cs.overflowX === 'clip';
    if (!clipsX) continue;
    if (el.scrollWidth > el.clientWidth + 1 && el.textContent.trim()) {
      truncated.push({el: descr(el), sel: (el.id ? '#' + el.id : '') ,
                      cls: (typeof el.className === 'string' ? el.className : ''),
                      ellipsis: cs.textOverflow === 'ellipsis',
                      scrollW: el.scrollWidth, clientW: el.clientWidth});
    }
    if (truncated.length >= 25) break;
  }
  return {
    innerWidth: W, innerHeight: H, scrollWidth: document.documentElement.scrollWidth,
    pageOverflow: document.documentElement.scrollWidth > W,
    mapVisible: total ? +(vis / total).toFixed(3) : null,
    mapDimmed: total ? +(dim / total).toFixed(3) : null,
    overflowing, truncated,
    body: document.body.className,
    sheetOpen: !!document.querySelector('#bs-sheet.bs-open'),
    drawerOpen: document.body.classList.contains('wb-fav-open'),
  };
}
"""


def watched(entry: dict) -> list[str]:
    """Which TRUNCATION_WATCH selectors this clipped element answers to."""
    classes = set((entry.get('cls') or '').split())
    ident = entry.get('sel') or ''
    return [s for s in TRUNCATION_WATCH
            if (s.startswith('#') and s == ident)
            or (s.startswith('.') and s[1:] in classes)]


def i18n_tables(docs: Path) -> dict[str, dict]:
    """The EN and JA maps as they are actually shipped, read back out of the
    built page rather than out of data/i18n/ — the build is what a visitor
    gets, and `to_trad` / the CJK-run scan sit between the two."""
    html = (docs / 'index.html').read_text(encoding='utf-8')
    out = {}
    for lang, var in (('en', 'TEXT_EN_MAP'), ('ja', 'TEXT_JA_MAP')):
        i = html.index('var ' + var)
        i = html.index('{', i)
        depth, j, instr, esc = 0, i, False, False
        while j < len(html):
            c = html[j]
            if instr:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    instr = False
            elif c == '"':
                instr = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out[lang] = json.loads(html[i:j + 1])
    return out


def check_punctuation(docs: Path) -> list[str]:
    bad = []
    for lang, table in i18n_tables(docs).items():
        for src, dst in table.items():
            if isinstance(dst, str) and any(ch in dst for ch in FULLWIDTH):
                bad.append(f'{lang}: {src!r} -> {dst!r}')
    return bad


def measure(page, out_dir: Path, vp: str, state: str, records: list) -> dict:
    page.wait_for_timeout(450)
    m = page.evaluate(METRICS_JS)
    m.update(vp=vp, state=state)
    m['mapReachable'] = round((m['mapVisible'] or 0) + (m['mapDimmed'] or 0), 3)
    m['watched'] = [e for e in m['truncated'] if watched(e)]
    page.screenshot(path=str(out_dir / f'{vp}-{state}.png'))
    records.append(m)
    print(f"  {vp:9s} {state:8s} map={m['mapReachable']:.0%} "
          f"(solid {m['mapVisible']:.0%}) overflow={m['pageOverflow']} "
          f"clipped={len(m['watched'])}", flush=True)
    return m


def run_viewport(browser, base, vp: str, cfg: dict, out_dir: Path,
                 records: list, failures: list) -> None:
    ctx = browser.new_context(
        viewport={'width': cfg['w'], 'height': cfg['h']},
        device_scale_factor=cfg['dpr'], is_mobile=True, has_touch=True,
        service_workers='block', locale='zh-CN')
    ctx.set_default_timeout(15000)
    # A language, so the first-visit chooser does not own the screen — but
    # deliberately NOT tabelog.seenIntro: the plan's 78% home baseline was
    # measured with the intro bar up.
    ctx.add_init_script("localStorage.setItem('tabelog.lang','zh-CN')")
    page = ctx.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.route('https://**/*', lambda r: r.abort())
    lib_browser.boot(page, base)
    page.wait_for_timeout(700)

    def gate(state: str, floor):
        m = measure(page, out_dir, vp, state, records)
        if floor is not None and m['mapReachable'] < floor:
            failures.append(
                f'{vp} {state}: map is {m["mapReachable"]:.1%} of the screen, '
                f'floor is {floor:.0%}')
        if m['pageOverflow']:
            failures.append(
                f'{vp} {state}: the page scrolls sideways '
                f'({m["scrollWidth"]}px in a {m["innerWidth"]}px window); '
                f'{m["overflowing"][:3]}')
        for e in m['watched']:
            failures.append(f'{vp} {state}: {e["el"]} is clipped '
                            f'({e["scrollW"]} into {e["clientW"]}px)')
        return m

    gate('home', cfg['home'])

    # The drawer, opened the way a user opens it: the results segment of the
    # floating pill (M-3.2-03).
    page.locator('#wb-seg [data-ux-tab="results"]').click()
    page.wait_for_selector('#wb-list .wb-row')
    gate('drawer', cfg['drawer'])

    # A detail card, opened from a result row so it carries the source-return
    # link and the full #bs-foot dock.
    page.locator('#wb-list .wb-row').first.click()
    page.wait_for_selector('#bs-sheet.bs-open')
    page.wait_for_timeout(700)
    gate('detail', cfg['detail'])

    # Back to the map, then the layers popover — the only place
    # #lp-n-builtin and .lp-sub are on screen, and both were on the audit's
    # clipped list at every phone width.
    lib_browser.phone_tab(page, 'map')
    page.wait_for_timeout(300)
    page.locator('#fab-layers').click()
    page.wait_for_timeout(400)
    gate('layers', None)

    if errors:
        failures.append(f'{vp}: page errors {errors[:3]}')
    ctx.close()


def main(argv=None) -> int:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--docs', type=Path, default=ROOT / 'docs')
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--only', help='comma-separated viewport names')
    args = ap.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    lib_browser.DOCS = args.docs.resolve()

    picked = {k: v for k, v in VIEWPORTS.items()
              if not args.only or k in args.only.split(',')}
    records: list[dict] = []
    failures: list[str] = []

    bad = check_punctuation(args.docs)
    if bad:
        failures.append(f'{len(bad)} EN/JA string(s) carry a full-width '
                        f'comma or semicolon: {bad[:5]}')
    print(f'i18n punctuation: {len(bad)} offending string(s)')

    with lib_browser.serve_docs(8992) as base, sync_playwright() as p:
        for engine in ('chromium', 'webkit'):
            rows = {k: v for k, v in picked.items() if v['browser'] == engine}
            if not rows:
                continue
            browser = getattr(p, engine).launch()
            for vp, cfg in rows.items():
                print(f'[{vp}] {cfg["w"]}x{cfg["h"]} on {engine}')
                try:
                    run_viewport(browser, base, vp, cfg, args.output,
                                 records, failures)
                except Exception as exc:  # keep measuring the other viewports
                    failures.append(f'{vp}: {exc!r}')
                    print(f'  !! {vp} failed: {exc!r}'[:400], flush=True)
            browser.close()

    (args.output / 'results.json').write_text(
        json.dumps({'records': records, 'failures': failures},
                   ensure_ascii=False, indent=2))
    if failures:
        print('\nvisibility: FAIL')
        for f in failures:
            print('  -', f)
        return 1
    print(f'\nvisibility: {len(records)} measurements, all thresholds met')
    return 0


if __name__ == '__main__':
    sys.exit(main())
