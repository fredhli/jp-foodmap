#!/usr/bin/env python3
"""Cross-viewport smoke test for the built page (M-056).

    uv run python tests/smoke_playwright.py
    uv run python tests/smoke_playwright.py --viewport fold-outer
    uv run python tests/smoke_playwright.py --screenshots <dir>

Twelve things, on each of the viewports the site is actually used at:

  boot      the payload lands and the counter shows the corpus size
  search    typing a restaurant name hits the local index
  card      tapping a marker opens the detail sheet
  save      the star toggles, the counter moves, and it lands in localStorage
  filter    changing one filter changes the visible count
  account   the avatar menu opens and scrolls
  lang      (W-9) first visit below 700px asks which language, once
  phone-nav (3.1) below 750px the visible Map / Results / Saved / Filters
              navigation reaches each real panel, restores focus after a
              detail, and keeps the filter content in one host
  chrome    (W-10 / 3.1) the map controls clear the phone navigation and the
              intro bar lines up with the search input
  workbench (W-1/2/3) mid + wide: the rail with both counts, the
            auto-collapsing detail column, and the left-column collapse
            (mid AND wide as of 2.3.0)

A console error that is not on the offline allowlist (Google Identity's 403,
the third-party hosts this run blocks) fails the viewport. Serves docs/ over
a local http.server; no network, no sign-in, no writes to api.jpfoodmap.com.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

from lib_browser import (  # noqa: E402
    boot,
    install_guards,
    serve_docs,
    shown_count,
    total_count,
    wait_ready,
)

# Historical Fold samples, actual 3.1 Fold CSS geometries, iPhone 15 and
# desktop. Keep the historical samples because they protect the older shell
# sizes while 475x751 and 932x704 pin the current Fold measurements.
#
# A-2 (2.3.0): WB_BP_SPLIT and WB_BP_MID are both 750 now, so the old
# 520-699 "split" mode (a permanent bottom panel over the map) is
# unreachable. Everything below 750px — the Fold's inner screen in portrait
# (616), the same screen at 60% width (591), the cover screen (416) and every
# phone — is the phone layout with the four-destination bottom navigation.
VIEWPORTS = {
    "fold-outer": {"width": 416, "height": 657, "mobile": True},
    "fold-inner": {"width": 616, "height": 816, "mobile": True},
    # A-2: the Fold's inner screen in a 60%-width split window. Was 'split';
    # is phone from 2.3.0 on. Kept as its own row because it is the narrowest
    # window the multi-window shell can hand the page and still be usable.
    "fold-inner-60": {"width": 591, "height": 689, "mobile": True},
    "fold-actual-outer": {"width": 475, "height": 751, "mobile": True},
    # M-027: the mid layout (top bar + left column + icon rail) exists between
    # 750 and 1279px, and the Fold's inner screen in landscape lives there.
    "fold-inner-landscape": {"width": 816, "height": 616, "mobile": True},
    "fold-actual-inner": {"width": 932, "height": 704, "mobile": True},
    "iphone-small": {"width": 375, "height": 667, "mobile": True},
    "iphone": {"width": 393, "height": 852, "mobile": True},
    "iphone-large": {"width": 430, "height": 932, "mobile": True},
    # W-2: the wide threshold moved 1100 -> 1280, so a 1000px window that used
    # to be one resize away from the old boundary is now solidly mid. A
    # non-touch mid viewport is its own layout (the rail, the collapsed detail
    # column) and nothing else in this table covers it.
    "desktop-mid": {"width": 1000, "height": 800, "mobile": False},
    "desktop": {"width": 1440, "height": 900, "mobile": False},
}

# A restaurant that is in every build of the corpus; the local search index
# should match it without touching Nominatim.
SEARCH_TERM = "寿司"


def eq(actual, expected, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: expected {expected!r}, got {actual!r}")


def open_filter_panel(page) -> None:
    """Open the single filter panel through the visible user entry point."""
    if page.eval_on_selector(
            "#ff-sheet-content", "el => el.offsetParent !== null"):
        return
    phone = page.evaluate("() => !/wb-(mid|wide)/.test(document.body.className)")
    if phone:
        entry = page.locator('#phone-nav [data-ux-tab="filter"]')
    else:
        entry = page.locator('.wb-filter-btn:visible').first
    if entry.count() != 1:
        raise AssertionError("no unique visible way to open the filter panel")
    entry.click(trial=True)
    entry.click()
    page.wait_for_function(
        "() => { const c = document.getElementById('ff-sheet-content');"
        "  return c && c.offsetParent !== null; }",
        timeout=15000,
    )
    page.wait_for_timeout(350)
    host = page.eval_on_selector(
        "#ff-sheet-content", "el => el.parentElement && el.parentElement.id")
    if host != "wb-filter-host":
        raise AssertionError(f"filter content moved to a second host: {host!r}")


# --- the six checks ---------------------------------------------------------

def check_boot(page, name):
    total = total_count(page)
    if total < 9000:
        raise AssertionError(f"corpus counter shows {total}, expected the full corpus")
    if shown_count(page) <= 0:
        raise AssertionError("no restaurants visible on first paint")
    return f"{shown_count(page):,} / {total:,} restaurants"


def check_search(page, name):
    # Only #ss-local matters here — that is the in-page restaurant index. The
    # #ss-api section is the Nominatim place lookup, whose host this run
    # blocks on purpose, so it legitimately renders "搜索失败".
    page.eval_on_selector("#ss-input", "el => el.focus()")
    page.fill("#ss-input", SEARCH_TERM)
    page.wait_for_function(
        "() => document.querySelectorAll('#ss-local .ss-row:not(.ss-empty)')"
        "  .length > 0",
        timeout=20000,
    )
    rows = page.eval_on_selector_all(
        "#ss-local .ss-row:not(.ss-empty)", "els => els.length")
    if rows < 5:
        raise AssertionError(
            f"local restaurant search for {SEARCH_TERM!r} returned only {rows} rows"
        )
    # (.ss-empty inside #ss-local is the "+N more matches, type more to
    # narrow" footer, not a no-results state — it is expected on a broad term.)
    # Close the dropdown again so it doesn't sit over the map for the next check.
    page.keyboard.press("Escape")
    page.fill("#ss-input", "")
    page.locator("#ss-input").blur()
    page.wait_for_function(
        "() => !document.getElementById('ss-list').classList.contains('open')",
        timeout=5000,
    )
    return f"{rows} local result rows"


def check_card(page, name):
    page.wait_for_function(
        "() => document.querySelectorAll('.leaflet-marker-icon .rst-mk,"
        " .leaflet-marker-icon').length > 0",
        timeout=30000,
    )
    # Click a restaurant marker (not a bookmark/attraction one).
    clicked = page.evaluate(
        "() => { const els = Array.from(document.querySelectorAll("
        "  '.leaflet-marker-icon')).filter(e =>"
        "    !e.classList.contains('bm-mk') &&"
        "    !e.classList.contains('marker-cluster'));"
        "  if (!els.length) return false;"
        "  els[0].dispatchEvent(new MouseEvent('click', {bubbles: true}));"
        "  return true; }"
    )
    if not clicked:
        raise AssertionError("no restaurant marker found to click")
    # W-5 (2.1.0): a marker tap now flies the map first and opens the card on
    # arrival (~1s), and the old third clause here — "the computed transform
    # is a matrix" — is true of #bs-sheet even while it is closed, so the wait
    # returned instantly and the next line read an empty sheet. Wait for the
    # state the check is actually about: open AND filled.
    page.wait_for_function(
        "() => { const s = document.getElementById('bs-sheet');"
        "  const c = document.getElementById('bs-content');"
        "  return s && c && s.classList.contains('bs-open')"
        "    && c.textContent.trim().length > 0; }",
        timeout=20000,
    )
    txt = page.eval_on_selector("#bs-content", "el => el.textContent.trim().slice(0, 40)")
    if not txt:
        raise AssertionError("detail sheet opened empty")
    photos = _photo_geometry(page)
    # .bs-open starts the 250ms entrance transition. Wait for the action dock
    # to finish entering before measuring actual user reachability.
    page.wait_for_function("""() => [...document.querySelectorAll(
      '#ux-detail-actions .ff-fav-btn,#ux-detail-actions .rst-gmaps')]
      .length===2 && [...document.querySelectorAll(
      '#ux-detail-actions .ff-fav-btn,#ux-detail-actions .rst-gmaps')]
      .every(e=>{const r=e.getBoundingClientRect(),t=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
        return r.top>=0&&r.bottom<=innerHeight&&(t===e||e.contains(t))})""",
      timeout=3000)
    first_actions = _detail_action_geometry(page)
    page.evaluate("a => { window.__smokeFirstMarkerActions = a; }", first_actions)
    page.evaluate(
        "() => { const b = document.querySelector('#bs-content .rst-close');"
        "  if (b) b.click(); }"
    )
    page.wait_for_timeout(300)
    return f"card shows {txt[:20]!r}, {photos}"


def _detail_action_geometry(page):
    return page.evaluate("""() => { const sheet=document.getElementById('bs-sheet').getBoundingClientRect();
      const content=document.getElementById('bs-content').getBoundingClientRect();
      return {sheet:{top:sheet.top,bottom:sheet.bottom,height:sheet.height},
      content:{top:content.top,bottom:content.bottom,height:content.height,scrollHeight:document.getElementById('bs-content').scrollHeight},
      viewport:{width:innerWidth,height:innerHeight},items:['#ux-detail-actions .ff-fav-btn',
      '#ux-detail-actions .rst-gmaps'].map(s=>{const e=document.querySelector(s);
      if(!e)return null;const r=e.getBoundingClientRect(),t=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
      return {top:r.top,bottom:r.bottom,h:r.height,inView:r.top>=0&&r.bottom<=innerHeight,hit:t===e||e.contains(t)};})};}""")


def check_marker_actions(page, name):
    """A marker-origin detail keeps Save and Maps in the visible action dock."""
    page.evaluate("""() => { const el=document.getElementById('ff-rating');
      el.value=el.min;el.dispatchEvent(new Event('input',{bubbles:true}));
      el.dispatchEvent(new Event('change',{bubbles:true}));
      const close=document.querySelector('#bs-content .rst-close');if(close)close.click();
      if(window.__wbFavDrawer&&window.__wbFavDrawer.isOpen())window.__wbFavDrawer.close(); }""")
    page.wait_for_timeout(400)
    page.wait_for_function("""() => [...document.querySelectorAll('.leaflet-marker-icon')]
      .some(e=>!e.classList.contains('bm-mk')&&!e.classList.contains('marker-cluster'))""", timeout=20000)
    page.evaluate("""() => [...document.querySelectorAll('.leaflet-marker-icon')]
      .find(e=>!e.classList.contains('bm-mk')&&!e.classList.contains('marker-cluster'))
      .dispatchEvent(new MouseEvent('click',{bubbles:true}))""")
    page.wait_for_function("() => document.getElementById('bs-sheet').classList.contains('bs-open')", timeout=20000)
    page.wait_for_timeout(350)
    actions = _detail_action_geometry(page)
    first_actions = page.evaluate("() => window.__smokeFirstMarkerActions")
    bad = lambda data: any(
        a is None or a["h"] < 44 or not a["inView"] or not a["hit"]
        for a in data["items"]
    )
    if bad(first_actions) or bad(actions):
        raise AssertionError(
            "marker-origin detail primary actions are not consistently reachable: "
            f"first={first_actions}, later={actions}")
    page.locator("#ux-detail-actions .ff-fav-btn").click(trial=True)
    page.locator("#ux-detail-actions .rst-gmaps").click(trial=True)
    return f"Save and Maps are visible 44px actions in {actions['viewport']}"


def _photo_geometry(page):
    """W-4: the three thumbnails are 4:3 boxes, not 320px-tall columns.

    The <img> carried width/height attributes as an intrinsic-size
    placeholder; `width:100%` beat the width hint, nothing beat
    `height:320px`, and CSS aspect-ratio only fills in a side when the other
    is auto — so every photo was a 107x320 slice showing ~11% of itself.
    The photo host is blocked on this run, so this asserts the *box*, which
    is exactly what regressed; a broken image keeps its aspect-ratio box.
    """
    st = page.evaluate("""() => {
      const box = document.querySelector('#bs-content .rst-photos .rst-ph');
      const img = document.querySelector('#bs-content .rst-photos img');
      if (!box || !img) return null;
      const r = img.getBoundingClientRect(), b = box.getBoundingClientRect();
      return {w: r.width, h: r.height, bw: b.width, bh: b.height,
              tag: box.tagName, href: box.hasAttribute('href'),
              src: img.getAttribute('src') || ''};
    }""")
    if st is None:
        # 9,803 of 9,807 rows carry photos, so this is a real signal, not a
        # tolerable skip — but say which card so it is debuggable.
        raise AssertionError("the open card has no .rst-photos thumbnails")
    if st["tag"] != "BUTTON" or st["href"]:
        raise AssertionError(
            f"a thumbnail is still a link (it opened a Custom Tab in the app): {st}")
    if "_square_" in st["src"]:
        raise AssertionError(
            f"thumbnail still asks for a server-side square crop: {st['src']}")
    if st["w"] < 40:
        raise AssertionError(f"photo box collapsed: {st}")
    want = st["w"] * 3 / 4
    if abs(st["h"] - want) > 2:
        raise AssertionError(
            f"photo img is {st['w']:.1f}x{st['h']:.1f}; expected height "
            f"~{want:.1f} (4:3). W-4 regression: {st}")
    if abs(st["bh"] - st["h"]) > 2:
        raise AssertionError(f"the img overflows its box (it gets cropped): {st}")
    return f"photos {st['w']:.0f}x{st['h']:.0f} (4:3)"


def check_save(page, name):
    before = page.evaluate(
        "() => { try { const c = JSON.parse("
        "  localStorage.getItem('omakase_state_cache_v2') || '{}');"
        "  return (c.fav || []).length; } catch (e) { return -1; } }"
    )
    n_before = int(page.eval_on_selector("#ff-fav-count", "el => el.textContent") or 0)
    # Open a card and press its star.
    page.evaluate(
        "() => { const els = Array.from(document.querySelectorAll("
        "  '.leaflet-marker-icon')).filter(e =>"
        "    !e.classList.contains('bm-mk') &&"
        "    !e.classList.contains('marker-cluster'));"
        "  els[0].dispatchEvent(new MouseEvent('click', {bubbles: true})); }"
    )
    page.wait_for_selector("#ux-detail-actions .ff-fav-btn", timeout=20000)
    page.eval_on_selector("#ux-detail-actions .ff-fav-btn", "el => el.click()")
    page.wait_for_function(
        "(n) => Number(document.getElementById('ff-fav-count').textContent) !== n",
        arg=n_before,
        timeout=15000,
    )
    n_after = int(page.eval_on_selector("#ff-fav-count", "el => el.textContent") or 0)
    eq(n_after, n_before + 1, "favorites counter after starring")
    after = page.evaluate(
        "() => { try { const c = JSON.parse("
        "  localStorage.getItem('omakase_state_cache_v2') || '{}');"
        "  return (c.fav || []).length; } catch (e) { return -1; } }"
    )
    eq(after, before + 1, "favorites persisted to localStorage")
    # Undo so the run leaves no state behind for the next check.
    page.eval_on_selector("#ux-detail-actions .ff-fav-btn", "el => el.click()")
    page.wait_for_timeout(300)
    page.evaluate(
        "() => { const b = document.querySelector('#bs-content .rst-close');"
        "  if (b) b.click(); }"
    )
    return f"{n_before} -> {n_after} favorites, persisted"


def check_filter(page, name):
    before = shown_count(page)
    open_filter_panel(page)
    # Raise the rating threshold to its maximum — always a strict subset.
    page.eval_on_selector(
        "#ff-rating",
        "el => { el.value = el.max; el.dispatchEvent(new Event('input', {bubbles: true}));"
        "        el.dispatchEvent(new Event('change', {bubbles: true})); }",
    )
    page.wait_for_function(
        "(n) => { const el = document.querySelector('.ff-count');"
        "  return el && Number(el.textContent.replace(/[^0-9]/g, '')) !== n; }",
        arg=before,
        timeout=15000,
    )
    after = shown_count(page)
    if after >= before:
        raise AssertionError(
            f"raising the rating filter did not shrink the result set "
            f"({before} -> {after})"
        )
    saved = json.loads(
        page.evaluate("() => localStorage.getItem('tabelog.filterState') || '{}'")
    )
    if "rating" not in saved:
        raise AssertionError("filter state was not persisted")
    return f"{before:,} -> {after:,} restaurants"


def check_account(page, name):
    page.eval_on_selector("#ss-avatar", "el => el.click()")
    page.wait_for_function(
        "() => { const m = document.getElementById('ss-menu');"
        "  return m && !m.hidden && m.classList.contains('open'); }",
        timeout=15000,
    )
    box = page.eval_on_selector(
        "#ss-menu",
        "el => ({h: el.getBoundingClientRect().height,"
        "        sh: el.scrollHeight, ov: getComputedStyle(el).overflowY})",
    )
    if box["h"] <= 0:
        raise AssertionError("account menu opened with zero height")
    vh = page.evaluate("() => window.innerHeight")
    if box["h"] > vh:
        raise AssertionError(
            f"account menu is {box['h']:.0f}px tall on a {vh}px viewport and "
            f"cannot be scrolled to its end"
        )
    if box["sh"] > box["h"] + 1 and box["ov"] not in ("auto", "scroll"):
        raise AssertionError(
            f"account menu content ({box['sh']:.0f}px) overflows its box "
            f"({box['h']:.0f}px) with overflow-y: {box['ov']}"
        )
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    return f"menu {box['h']:.0f}px tall on a {vh}px viewport"


def check_lang(page, name):
    """W-9: a first visit below 700px asks for a language before anything
    else. Choosing one writes tabelog.lang and reloads, so it can only ever
    be asked once; the workbench modes never ask (they have the resident
    language button in the top bar). Runs FIRST — the modal owns the screen
    until it is answered."""
    st = page.evaluate("""() => {
      const g = document.getElementById('lang-gate');
      return {gate: !!g && !g.hidden,
              opts: document.querySelectorAll('#lang-gate [data-lg]').length,
              phone: !/wb-(mid|wide)/.test(document.body.className),
              lang: (() => { try { return localStorage.getItem('tabelog.lang'); }
                             catch (_) { return 'ERR'; } })()};
    }""")
    if not st["phone"]:
        if st["gate"]:
            raise AssertionError("the language chooser must not open on a workbench mode")
        return "no chooser on a workbench mode (top bar owns it)"
    if not st["gate"]:
        raise AssertionError(f"first visit did not ask for a language: {st}")
    if st["opts"] != 4:
        raise AssertionError(f"language chooser offers {st['opts']} options, expected 4")
    if st["lang"] is not None:
        raise AssertionError(f"tabelog.lang was already set on a first visit: {st}")
    # Let the background popups prefetch (6.4 MB) finish first: setLanguage()
    # navigates, and navigating on top of that fetch aborts it, which the page
    # correctly logs as an error — the same test artefact reload_and_wait()
    # avoids in lib_browser.
    try:
        page.wait_for_load_state("networkidle", timeout=90000)
    except Exception:
        pass
    # Pick 简体中文 — setLanguage() drops ?lang for the default and reloads.
    page.evaluate("() => document.querySelector('#lang-gate [data-lg=\"zh-CN\"]').click()")
    page.wait_for_load_state("domcontentloaded")
    wait_ready(page)
    # The reload restarts the popups prefetch (6.4 MB). Let it land before
    # handing the page to the checks that follow — the same courtesy
    # reload_and_wait() pays, and without it the first card opens against a
    # payload that is still in flight.
    try:
        page.wait_for_load_state("networkidle", timeout=45000)
    except Exception:
        pass
    page.wait_for_timeout(400)
    after = page.evaluate("""() => ({gate: !document.getElementById('lang-gate').hidden,
        lang: localStorage.getItem('tabelog.lang')})""")
    if after["gate"] or after["lang"] != "zh-CN":
        raise AssertionError(f"the chooser came back after a choice: {after}")
    return "asked once, answered, gone"


def check_phone_nav(page, name):
    """3.1: exercise the visible four-destination phone navigation.

    The old hamburger and filter FAB remain as compatibility hooks, but they
    are deliberately hidden. This check clicks the controls a user sees and
    verifies their destination, the one filter host, detail return and focus.
    """
    phone = page.evaluate("() => !/wb-(mid|wide)/.test(document.body.className)")
    if not phone:
        shown = page.evaluate("""() => ['#phone-nav','#ss-drawer-btn','#ff-fab']
          .filter(s => { const e=document.querySelector(s); return e && getComputedStyle(e).display!=='none'; })""")
        if shown:
            raise AssertionError(f"phone-only navigation leaked into workbench: {shown}")
        return "phone navigation and legacy entries hidden in workbench"

    state = page.evaluate("""() => {
      const nav=document.getElementById('phone-nav'), nr=nav.getBoundingClientRect();
      const buttons=[...nav.querySelectorAll('[data-ux-tab]')];
      return {count:buttons.length, bottom:Math.round(nr.bottom), vh:innerHeight,
        old:[getComputedStyle(document.getElementById('ss-drawer-btn')).display,
             getComputedStyle(document.getElementById('ff-fab')).display],
        buttons:buttons.map(b=>{const r=b.getBoundingClientRect(),t=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
          return {tab:b.dataset.uxTab,w:r.width,h:r.height,inView:r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight,
                  hit:t===b||b.contains(t)};})};
    }""")
    if state["count"] != 4 or [b["tab"] for b in state["buttons"]] != [
            "map", "results", "fav", "filter"]:
        raise AssertionError(f"phone navigation destinations changed: {state}")
    if state["old"] != ["none", "none"]:
        raise AssertionError(f"legacy phone entries compete with 3.1 navigation: {state}")
    if abs(state["bottom"] - state["vh"]) > 1:
        raise AssertionError(f"phone navigation is not docked to the viewport: {state}")
    if any(b["h"] < 44 or not b["inView"] or not b["hit"] for b in state["buttons"]):
        raise AssertionError(f"a phone navigation destination is unreachable: {state}")

    def go(tab):
        button = page.locator(f'#phone-nav [data-ux-tab="{tab}"]')
        button.click(trial=True)
        button.click()
        page.wait_for_timeout(250)
        current = page.get_attribute(f'#phone-nav [data-ux-tab="{tab}"]', 'aria-current')
        if current != "page":
            raise AssertionError(f"{tab} did not become the current destination")

    go("results")
    if not page.locator("#wb-list").is_visible():
        raise AssertionError("Results did not expose the restaurant list")
    go("filter")
    filtered = page.evaluate("""() => ({panel:document.getElementById('ff-sheet-content').offsetParent!==null,
      host:document.getElementById('ff-sheet-content').parentElement.id,
      done:document.getElementById('ux-filter-done').offsetParent!==null,
      sheet:document.getElementById('ff-sheet').classList.contains('ff-open'),
      backdropDisplay:getComputedStyle(document.getElementById('wb-fav-backdrop')).display,
      backdropPointer:getComputedStyle(document.getElementById('wb-fav-backdrop')).pointerEvents})""")
    if not filtered["panel"] or filtered["host"] != "wb-filter-host" or not filtered["done"]:
        raise AssertionError(f"Filters did not expose the single complete panel: {filtered}")
    if filtered["sheet"] or filtered["backdropDisplay"] != "none":
        raise AssertionError(f"Filters opened a competing modal layer: {filtered}")
    go("fav")
    if not page.locator("#wb-fav").is_visible() or not page.locator("#ux-saved-scope").is_visible():
        raise AssertionError("Saved did not expose its list and scope")

    # A real result click must provide a return action, and that action must
    # restore the source row as the keyboard focus target.
    go("results")
    row = page.locator("#wb-list .wb-row").first
    row.wait_for(state="visible")
    ref = row.get_attribute("id")
    row.click()
    page.wait_for_selector("#bs-sheet.bs-open")
    page.locator("#ux-detail-back").click(trial=True)
    page.locator("#ux-detail-back").click()
    page.wait_for_timeout(350)
    returned = page.evaluate("""() => ({tab:document.querySelector('#phone-nav [aria-current="page"]')?.dataset.uxTab,
      focus:document.activeElement?.id,list:document.getElementById('wb-list').offsetParent!==null})""")
    if returned != {"tab": "results", "focus": ref, "list": True}:
        raise AssertionError(f"detail return lost its source or keyboard focus: {returned}")
    go("map")
    focused = page.evaluate("() => document.activeElement?.dataset.uxTab")
    if focused != "map" or page.evaluate("() => document.body.classList.contains('wb-fav-open')"):
        raise AssertionError(f"Map did not close the page panel or retain focus: {focused!r}")
    return "4 reachable destinations, one filter host, detail source/focus restored"


def check_chrome(page, name):
    """The map controls clear the phone nav; intro aligns to the header actions."""
    g = page.evaluate("""() => {
      const R = s => { const e = document.querySelector(s);
        if (!e) return null;
        if (getComputedStyle(e).display === 'none') return null;
        const r = e.getBoundingClientRect();
        return {r: r.right, b: r.bottom, w: r.width}; };
      const N=s=>{const e=document.querySelector(s);if(!e||getComputedStyle(e).display==='none')return null;
        const r=e.getBoundingClientRect();return {t:r.top,b:r.bottom};};
      return {nav:N('#phone-nav'), stack: N('.map-fab-stack'),
              intro: R('#intro-bar'), input: R('#ss-input-wrap'), nearby: R('#ux-nearby'),
              phone: !/wb-(split|mid|wide)/.test(document.body.className)};
    }""")
    out = []
    if g["phone"] and g["nav"] and g["stack"]:
        clearance = g["nav"]["t"] - g["stack"]["b"]
        if clearance < -1:
            raise AssertionError(
                f"map controls overlap the phone navigation by {-clearance:.0f}px: {g}")
        out.append(f"map controls clear phone navigation by {clearance:.0f}px")
    if g["phone"] and g["intro"] and g["input"]:
        if not g["nearby"]:
            raise AssertionError("phone header is missing its nearby control")
        right_delta = abs(g["intro"]["r"] - g["nearby"]["r"])
        left_delta = abs((g["intro"]["r"] - g["intro"]["w"])
                         - (g["input"]["r"] - g["input"]["w"]))
        if max(left_delta, right_delta) > 4:
            raise AssertionError(
                f"intro bar is misaligned with search and nearby controls "
                f"(left {left_delta:.0f}px, right {right_delta:.0f}px): {g}")
        out.append(f"intro aligns with header actions (left {left_delta:.0f}px, right {right_delta:.0f}px)")
    return "; ".join(out) or "workbench mode — phone chrome is hidden"


def check_workbench(page, name):
    """W-1 / W-2 / W-3: the mid + wide column layout.

    W-2  the detail column costs the map nothing until a restaurant is picked
    W-3  the rail is the hamburger plus BOTH counts (W-4: 符合筛选 N and
         屏幕内 M), nothing else; mid and wide can both collapse the left
         column onto that rail and get it back (W-6)
    W-1  the card inside the detail column has real top padding (there is no
         #bs-grip up there to stand in for it)
    """
    mode = page.evaluate("() => (window.__wbMode ? window.__wbMode() : 'phone')")
    if mode not in ("mid", "wide"):
        return f"n/a in {mode} mode"

    # The rail's search / filter buttons were copies of top-bar controls that
    # are on screen at the same time (W-3b).
    extra = page.evaluate(
        "() => ({search: !!document.getElementById('wb-rail-search'),"
        "        filter: document.querySelectorAll("
        "                  '#wb-rail .wb-filter-btn').length})")
    if extra["search"] or extra["filter"]:
        raise AssertionError(f"#wb-rail still carries duplicate controls: {extra}")
    # W-4: two numbers on the rail, not one.
    nums = page.evaluate(
        "() => ({n: document.querySelectorAll('#wb-rail .ff-count').length,"
        "        m: document.querySelectorAll('#wb-rail .ff-inview').length})")
    if nums["n"] != 1 or nums["m"] != 1:
        raise AssertionError(f"#wb-rail does not carry both counts: {nums}")

    # W-2: nothing selected -> the detail column is off-canvas and --wb-right
    # is 0, so the map owns that space.
    page.evaluate(
        "() => { const b = document.querySelector('#bs-content .rst-close');"
        "  if (b) b.click(); }")
    page.wait_for_timeout(400)
    closed = page.evaluate("""() => {
      const d = document.getElementById('wb-detail');
      const r = d.getBoundingClientRect();
      return {right: getComputedStyle(document.documentElement)
                       .getPropertyValue('--wb-right').trim(),
              offscreen: r.left >= innerWidth - 1,
              open: document.body.classList.contains('wb-detail-open')};
    }""")
    if closed["open"] or closed["right"] != "0px" or not closed["offscreen"]:
        raise AssertionError(f"detail column still resident with no selection: {closed}")

    # ... and selecting a restaurant slides it in and pays for it. check_filter
    # ran before this one and left the rating slider at its maximum, so put it
    # back first — 31 survivors nationwide can leave this viewport with no
    # restaurant marker at all to click.
    page.evaluate(
        "() => { const el = document.getElementById('ff-rating');"
        "  if (!el) return;"
        "  el.value = el.min;"
        "  el.dispatchEvent(new Event('input', {bubbles: true}));"
        "  el.dispatchEvent(new Event('change', {bubbles: true})); }")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.leaflet-marker-icon'))"
        "  .filter(e => !e.classList.contains('bm-mk')"
        "            && !e.classList.contains('marker-cluster')).length > 0",
        timeout=20000)
    page.evaluate(
        "() => { const els = Array.from(document.querySelectorAll("
        "  '.leaflet-marker-icon')).filter(e =>"
        "    !e.classList.contains('bm-mk') &&"
        "    !e.classList.contains('marker-cluster'));"
        "  els[0].dispatchEvent(new MouseEvent('click', {bubbles: true})); }")
    page.wait_for_function(
        "() => document.body.classList.contains('wb-detail-open')", timeout=20000)
    page.wait_for_selector("#bs-content .rst-card", timeout=20000)
    page.wait_for_timeout(400)
    opened = page.evaluate("""() => {
      const d = document.getElementById('wb-detail');
      const c = document.getElementById('bs-content');
      const card = c.querySelector('.rst-card');
      return {right: getComputedStyle(document.documentElement)
                       .getPropertyValue('--wb-right').trim(),
              onscreen: d.getBoundingClientRect().left < innerWidth - 100,
              gap: card ? Math.round(card.getBoundingClientRect().top
                                     - d.getBoundingClientRect().top) : -1};
    }""")
    if opened["right"] == "0px" or not opened["onscreen"]:
        raise AssertionError(f"detail column did not slide in: {opened}")
    # W-1: the card used to start at exactly 0px from the column's top edge.
    if opened["gap"] < 10:
        raise AssertionError(
            f"card is flush against the top of the detail column: {opened}")
    page.evaluate(
        "() => { const b = document.querySelector('#bs-content .rst-close');"
        "  if (b) b.click(); }")
    page.wait_for_timeout(400)

    # W-6 (2.3.0): mid AND wide -- collapse the left column onto the rail
    # and back. The expanded width is the mode's own column width.
    full = "344px" if mode == "wide" else "320px"
    page.evaluate("() => document.getElementById('wb-left-collapse').click()")
    page.wait_for_timeout(400)
    coll = page.evaluate("""() => {
      const rail = document.getElementById('wb-rail');
      return {left: getComputedStyle(document.documentElement)
                      .getPropertyValue('--wb-left').trim(),
              rail: getComputedStyle(rail).display,
              count: !!rail.querySelector('.ff-count'),
              saved: JSON.parse(localStorage.getItem('tabelog.listView') || '{}')
                       .leftCollapsed};
    }""")
    if coll["left"] != "58px" or coll["rail"] == "none" or not coll["count"]:
        raise AssertionError(f"collapsing the left column did not land: {coll}")
    if coll["saved"] is not True:
        raise AssertionError(
            f"leftCollapsed was not persisted in tabelog.listView: {coll}")
    page.evaluate("() => document.getElementById('wb-rail-back').click()")
    page.wait_for_timeout(400)
    back = page.evaluate(
        "() => ({left: getComputedStyle(document.documentElement)"
        "          .getPropertyValue('--wb-left').trim(),"
        "        rail: getComputedStyle(document.getElementById('wb-rail')).display,"
        "        saved: JSON.parse(localStorage.getItem('tabelog.listView') || '{}')"
        "                 .leftCollapsed})")
    if back["left"] != full or back["rail"] != "none" or back["saved"] is not False:
        raise AssertionError(f"the rail's hamburger did not restore the column: {back}")
    return f"{mode}: detail auto-collapses, left column collapses to the rail"


MAP_HANDLE_JS = (
    "const k = Object.keys(window).find(x => /^map_[0-9a-f]{8}/.test(x)"
    " && window[x] && window[x].getZoom); const m = k ? window[k] : null;")


def check_goto_zoom(page, name):
    """W-5: picking a restaurant from the result list flies the map to it.

    Until 2.1.0 only the search box did. The list, the Saved tab, the card's
    up/down stepper and marker taps all called openSheet(), which only ever
    pans — so choosing a Kanazawa restaurant from a zoom-10 view of Japan
    opened a card for a pin that stayed invisible. Everything routes through
    gotoRestaurant() now and lands at GOTO_ZOOM (17).
    """
    page.evaluate(
        "() => { const b = document.querySelector('#bs-content .rst-close');"
        "  if (b) b.click(); }")
    page.wait_for_timeout(300)
    # Phone-likes keep the list behind the Results destination.
    if not page.evaluate("() => !!document.querySelector('.wb-row')"):
        page.locator('#phone-nav [data-ux-tab="results"]').click()
        page.wait_for_timeout(500)
    if not page.evaluate("() => !!document.querySelector('.wb-row')"):
        raise AssertionError("no result row to click")
    # A country-wide view, i.e. the state the bug was reported from.
    page.evaluate("() => { " + MAP_HANDLE_JS
                  + " if (m) m.setView([35.68, 139.76], 10, {animate: false}); }")
    page.wait_for_timeout(400)
    before = page.evaluate("() => { " + MAP_HANDLE_JS + " return m ? m.getZoom() : null; }")
    if before != 10:
        raise AssertionError(f"could not set the starting view (zoom={before})")
    page.evaluate("() => document.querySelector('.wb-row')"
                  "  .dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    # The card opens on arrival, so waiting for it also waits out the flight.
    page.wait_for_function(
        "() => { const c = document.getElementById('bs-content');"
        "  return c && c.textContent.trim().length > 0; }", timeout=20000)
    page.wait_for_timeout(400)
    after = page.evaluate("() => { " + MAP_HANDLE_JS + " return m.getZoom(); }")
    if not after or after < 16:
        raise AssertionError(
            f"picking a result row left the map at zoom {after} (want >= 16): "
            f"W-5 regression")
    page.evaluate(
        "() => { const b = document.querySelector('#bs-content .rst-close');"
        "  if (b) b.click(); }")
    page.wait_for_timeout(300)
    return f"list row: zoom {before} -> {after}, card opened"


def check_filter_copy(page, name):
    """W-8 / W-11 / W-12: the filter panel collapsed onto two row-height
    variables and one 其它 section (the five toggle ids are load-bearing and
    must survive), the two counters read a whole sentence instead of two
    labels glued to two numbers, and the "all matches are off-screen" hint
    appears at most once per page load."""
    m = page.evaluate(
        "() => { const sc = document.getElementById('ff-sheet-content');"
        "  const cs = getComputedStyle(sc);"
        "  const cnt = document.querySelector('.ff-count');"
        "  return {"
        "    row: cs.getPropertyValue('--ff-row').trim(),"
        "    head: cs.getPropertyValue('--ff-head').trim(),"
        "    missing: ['ff-bookable-only', 'ff-only-fav', 'ff-hide-black',"
        "              'ff-hide-foreign', 'ff-gcal-only']"
        "             .filter(i => !document.getElementById(i)),"
        "    others: document.querySelectorAll('.ff-other-row').length,"
        "    helpInLabel: !!document.querySelector("
        "      '.ff-other-row label .ff-help-trigger'),"
        "    n: cnt ? cnt.textContent.replace(/[^0-9]/g, '') : '',"
        "    head_txt: (document.getElementById('ff-head-counts') || {}).textContent || '',"
        "    counts_txt: (document.querySelector('.wb-counts') || {}).textContent || ''"
        "  }; }"
    )
    if m["missing"]:
        raise AssertionError(f"filter toggle id(s) gone: {m['missing']}")
    if m["others"] != 5:
        raise AssertionError(f"expected 5 .ff-other-row, got {m['others']}")
    if m["helpInLabel"]:
        raise AssertionError("a .ff-help-trigger is inside a <label> — "
                             "clicking help would toggle the checkbox")
    if not m["row"] or not m["head"]:
        raise AssertionError("--ff-row / --ff-head not set on #ff-sheet-content")
    # The sentence carries the number; the number alone is not the sentence.
    for key in ("head_txt", "counts_txt"):
        txt = " ".join(m[key].split())
        if m["n"] and m["n"] not in txt.replace(",", ""):
            raise AssertionError(f"{key} lost the match count: {txt!r}")
        if len(txt) <= len(m["n"]) + 6:
            raise AssertionError(f"{key} is not a sentence: {txt!r}")
    # W-12a: the hint carries its own × and "don't show again", and the card
    # is pointer-events:none, so both have to switch them back on.
    btns = page.evaluate(
        "() => ['ffe-off-close', 'ffe-off-never'].map(function(id) {"
        "  const b = document.getElementById(id);"
        "  return b ? getComputedStyle(b).pointerEvents : 'MISSING'; })"
    )
    if btns != ["auto", "auto"]:
        raise AssertionError(f"off-screen hint buttons unclickable: {btns}")
    # And it is once per page load: park the map on open water twice. The
    # first pan may find the latch already spent (an earlier check can have
    # filtered every match off screen), so only the second pan is asserted.
    mapjs = "window[Object.keys(window).find(k => k.startsWith('map_'))]"
    home = page.evaluate(
        "() => { const m = %s; const c = m.getCenter();"
        "  return [c.lat, c.lng, m.getZoom()]; }" % mapjs
    )
    seen = False
    for lat, lon in ((30.0, 140.0), (30.5, 140.5)):
        page.evaluate("() => { %s.setView([%f, %f], 11); }" % (mapjs, lat, lon))
        page.wait_for_timeout(2500)
        vis = page.evaluate(
            "() => { const o = document.getElementById('ffe-offscreen');"
            "  return !!(o && !o.hidden); }"
        )
        if seen and vis:
            raise AssertionError("off-screen hint came back on the second pan")
        seen = seen or vis
    page.evaluate(
        "() => { %s.setView([%f, %f], %d); }" % (mapjs, home[0], home[1], home[2])
    )
    page.wait_for_timeout(800)
    return (f"rows {m['row']}/{m['head']}, 5 toggles kept, "
            f"hint at most once (seen={seen})")


CHECKS = [
    ("lang", check_lang),      # W-9 — first, it is modal until answered
    ("boot", check_boot),
    ("search", check_search),
    ("card", check_card),
    ("save", check_save),
    ("filter", check_filter),
    ("account", check_account),
    ("phone-nav", check_phone_nav),     # 3.1 four primary destinations
    ("chrome", check_chrome),           # W-10 / 3.1 nav clearance
    ("workbench", check_workbench),     # W-1 / W-2 / W-3
    ("filter-copy", check_filter_copy), # W-8 / W-11 / W-12
    ("goto-zoom", check_goto_zoom),     # W-5
    ("marker-actions", check_marker_actions),  # 3.1 primary action dock
]


def main(argv: list[str] | None = None) -> int:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--viewport", action="append", choices=sorted(VIEWPORTS),
                    help="run only this viewport (repeatable)")
    ap.add_argument("--screenshots", type=Path, default=None,
                    help="write one PNG per viewport into this directory")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)

    names = args.viewport or list(VIEWPORTS)
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)

    n_fail = 0
    with serve_docs(8926) as base, sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        for name in names:
            vp = VIEWPORTS[name]
            print(f"\n[{name}] {vp['width']}x{vp['height']}")
            errors: list[str] = []
            ctx = browser.new_context(
                viewport={"width": vp["width"], "height": vp["height"]},
                is_mobile=vp["mobile"],
                has_touch=vp["mobile"],
                device_scale_factor=2 if vp["mobile"] else 1,
                # docs/sw.js caches the multi-MB payloads; a half-warm SW cache
                # makes local fetches flaky. The SW is not what this test is for.
                service_workers="block",
            )
            page = ctx.new_page()
            install_guards(page, errors)
            try:
                boot(page, base)
                for check_name, fn in CHECKS:
                    detail = fn(page, name)
                    print(f"  ok    {check_name}: {detail}")
                if args.screenshots:
                    shot = args.screenshots / f"smoke-{name}.png"
                    page.screenshot(path=str(shot))
                    print(f"  shot  {shot}")
                if errors:
                    raise AssertionError(
                        f"{len(errors)} console/page error(s): {errors[:5]}"
                    )
                print(f"  PASS  {name} (console clean)")
            except Exception:
                n_fail += 1
                print(f"  FAIL  {name}")
                traceback.print_exc()
                if args.screenshots:
                    try:
                        page.screenshot(
                            path=str(args.screenshots / f"smoke-{name}-FAIL.png"))
                    except Exception:
                        pass
            finally:
                ctx.close()
        browser.close()

    print(f"\nsmoke: {len(names) - n_fail}/{len(names)} viewports passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
