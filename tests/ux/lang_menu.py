"""Top-bar language menu (4.2.3 fix). The chip next to 筛选 opened a menu that
the render it had just queued wiped about 10 ms later, so nothing ever showed.

    uv run python tests/ux/lang_menu.py
    uv run python tests/ux/lang_menu.py --browser webkit
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import lib_browser  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--browser", choices=["chromium", "webkit"], default="chromium")
args = parser.parse_args()

# The chip only exists where #topbar-right does: mid and wide layouts
# (desktop, tablets, the Fold's inner screen).
VIEWPORTS = [("wide", 1440, 900), ("mid", 900, 700), ("fold-inner", 816, 616)]

MENU_JS = """() => { const m = document.getElementById('ov-lang-menu'), c = document.querySelector('[data-ov="lang-menu"]');
  if (!m) return { open: false, expanded: c && c.getAttribute('aria-expanded') };
  const r = m.getBoundingClientRect(), cr = c.getBoundingClientRect();
  return { open: m.getClientRects().length > 0, items: m.querySelectorAll('[role="menuitemradio"]').length,
           checked: [...m.querySelectorAll('[aria-checked="true"]')].map(e => e.dataset.lang),
           below: r.top >= cr.bottom - 1, inside: r.left >= 0 && r.right <= innerWidth + 1,
           expanded: c.getAttribute('aria-expanded') }; }"""


def check(cond, what):
    if not cond:
        raise AssertionError(what)


def main():
    with lib_browser.serve_docs(8996) as base, sync_playwright() as p:
        browser = getattr(p, args.browser).launch()
        for name, w, h in VIEWPORTS:
            errors = []
            ctx = browser.new_context(viewport={"width": w, "height": h}, service_workers="block")
            page = ctx.new_page()
            lib_browser.install_guards(page, errors)
            lib_browser.seed_local_storage(page, {"tabelog.seenIntro": "1"})
            page.add_init_script("if (!localStorage.getItem('tabelog.lang')) localStorage.setItem('tabelog.lang', 'zh-CN')")
            lib_browser.boot(page, base)
            chip = '[data-ov="lang-menu"]'

            page.click(chip)
            page.wait_for_timeout(700)          # well past the render that used to wipe it
            m = page.evaluate(MENU_JS)
            check(m["open"] and m["items"] == 4 and m["checked"] == ["zh"], f"{name}: menu open with 4 languages: {m}")
            check(m["below"] and m["inside"] and m["expanded"] == "true", f"{name}: anchored under the chip: {m}")

            page.click(chip)
            page.wait_for_timeout(250)
            check(not page.evaluate(MENU_JS)["open"], f"{name}: the chip closes it again")

            page.click(chip)
            page.wait_for_timeout(250)
            page.mouse.click(w // 2, h // 2)
            page.wait_for_timeout(250)
            check(not page.evaluate(MENU_JS)["open"], f"{name}: an outside press closes it")

            page.click(chip)
            page.wait_for_timeout(250)
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            check(not page.evaluate(MENU_JS)["open"], f"{name}: Escape closes it")

            page.click(chip)
            page.wait_for_timeout(250)
            with page.expect_navigation(timeout=30000):
                page.click('#ov-lang-menu [data-lang="en"]')
            lib_browser.wait_ready(page)
            check(page.evaluate("() => App.state.lang") == "en", f"{name}: picking English switches the page")
            check(not errors, f"{name}: console/page errors: {errors[:5]}")
            print(f"  ok    {name} ({args.browser})")
            ctx.close()
        browser.close()
    print(f"lang menu: {len(VIEWPORTS)} viewports passed on {args.browser}")


if __name__ == "__main__":
    main()
