#!/usr/bin/env python3
"""Cross-viewport smoke test for the built page (M-056).

    uv run python tests/smoke_playwright.py                    # the Chromium rows
    uv run python tests/smoke_playwright.py --browser webkit   # the iPhone rows
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
  seg-pill  (3.2 / M-3.2-02+03) below 750px the floating segmented pill is
            the drawer's only entry: three reachable segments that never
            meet the FAB column, each opening the drawer on its own tab,
            the current one closing it again, one filter host, detail
            return with focus, and the map still painted beside the drawer
  chrome    (W-10) the intro bar lines up with the search input
  workbench (W-1/2/3) mid + wide: the rail with both counts, the
            auto-collapsing detail column, and the left-column collapse
            (mid AND wide as of 2.3.0)

Every viewport declares the engine it is measured on and only runs under
that one (M-3.2-11): an iPhone row is a WebKit row, and Chromium is never
allowed to stand in for it. `--browser` picks the engine, and the default
run is the Chromium half; the gate is both halves.

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
# phone — is the phone layout, which as of 3.2.0 means the overlay drawer
# under the floating segmented pill.
#
# M-3.2-11: every row also names the engine it is measured on, and the runner
# refuses to run it under any other. The iPhone rows are WebKit rows because
# that is the engine those devices actually ship; a Chromium pass on 402x874
# proves nothing about iOS Safari. The Fold and desktop rows are Chromium.
VIEWPORTS = {
    "fold-outer": {"width": 416, "height": 657, "mobile": True, "browser": "chromium"},
    "fold-inner": {"width": 616, "height": 816, "mobile": True, "browser": "chromium"},
    # A-2: the Fold's inner screen in a 60%-width split window. Was 'split';
    # is phone from 2.3.0 on. Kept as its own row because it is the narrowest
    # window the multi-window shell can hand the page and still be usable.
    "fold-inner-60": {"width": 591, "height": 689, "mobile": True, "browser": "chromium"},
    "fold-actual-outer": {"width": 475, "height": 751, "mobile": True, "browser": "chromium"},
    # M-027: the mid layout (top bar + left column + icon rail) exists between
    # 750 and 1279px, and the Fold's inner screen in landscape lives there.
    "fold-inner-landscape": {"width": 816, "height": 616, "mobile": True, "browser": "chromium"},
    "fold-actual-inner": {"width": 932, "height": 704, "mobile": True, "browser": "chromium"},
    "iphone-small": {"width": 375, "height": 667, "mobile": True, "browser": "webkit"},
    "iphone": {"width": 393, "height": 852, "mobile": True, "browser": "webkit"},
    "iphone-large": {"width": 430, "height": 932, "mobile": True, "browser": "webkit"},
    # M-3.2-11: the iPhone 17 Pro CSS viewport, which is what the 3.2.0 phone
    # work was designed and measured against (ux-phone's iphone402 row).
    "iphone-17pro": {"width": 402, "height": 874, "mobile": True, "browser": "webkit"},
    # W-2: the wide threshold moved 1100 -> 1280, so a 1000px window that used
    # to be one resize away from the old boundary is now solidly mid. A
    # non-touch mid viewport is its own layout (the rail, the collapsed detail
    # column) and nothing else in this table covers it.
    "desktop-mid": {"width": 1000, "height": 800, "mobile": False, "browser": "chromium"},
    "desktop": {"width": 1440, "height": 900, "mobile": False, "browser": "chromium"},
}

# A restaurant that is in every build of the corpus; the local search index
# should match it without touching Nominatim.
SEARCH_TERM = "寿司"

def eq(actual, expected, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: expected {expected!r}, got {actual!r}")


# --- the six checks ---------------------------------------------------------

# ---------------------------------------------------------------- 4.0 helpers
#
# 3.2.x drove the page by id (#ss-input, #bs-sheet, #ff-rating, #wb-seg, …).
# 4.0 replaced the whole presentation layer, so each helper below names the old
# hook it stands in for and what the check is really protecting. Nothing here
# weakens an assertion — where a 3.2.x element simply has no 4.0 counterpart,
# the check asserts the same user-visible guarantee through the element that
# now provides it.

MAP_HANDLE_JS = (
    "const k = Object.keys(window).find(x => /^map_[0-9a-f]{8}/.test(x)"
    " && window[x] && window[x].getZoom); const m = k ? window[k] : null;")

DETAIL_OPEN_JS = (
    "() => !!(window.App && App.state.selected.id"
    "         && document.getElementById('detail-root')"
    "         && document.getElementById('detail-root').textContent.trim())"
)


def open_first_marker(page, timeout=30000):
    """Tap a restaurant marker (never a pin / landmark / cluster) and wait for
    the detail card. 3.2.x: `.leaflet-marker-icon` minus `.bm-mk` /
    `.marker-cluster`, then #bs-sheet.bs-open with #bs-content filled."""
    # 3.2.x took the first `.leaflet-marker-icon` that was neither `.bm-mk`
    # nor `.marker-cluster`. In 4.0 every divIcon wrapper is `.mp-mk-wrap`
    # and the kind is on the inner node — a restaurant is `.mp-mk`, a landmark
    # or pin `.mp-lm` / `.mp-pin`, a cluster `.mp-cluster-wrap`. Select on the
    # inner node so a landmark can never stand in for a restaurant.
    # On a short phone the map band is small enough that every restaurant in
    # it is still inside a cluster at the default zoom. A user zooms in; do the
    # same, bounded, rather than failing on a legitimate clustered view.
    for _ in range(6):
        if page.evaluate("() => !!document.querySelector('.leaflet-marker-icon .mp-mk')"):
            break
        page.evaluate("() => { " + MAP_HANDLE_JS + " if (m) m.setZoom(m.getZoom() + 2); }")
        page.wait_for_timeout(900)
    page.wait_for_function(
        "() => !!document.querySelector('.leaflet-marker-icon .mp-mk')",
        timeout=timeout)
    page.evaluate(
        "() => document.querySelector('.leaflet-marker-icon .mp-mk')"
        "  .dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    page.wait_for_function(DETAIL_OPEN_JS, timeout=timeout)
    # "open" is not "loaded": the card paints identity first and fills the rest
    # when the multi-MB popups payload lands (3.2.x waited out 加载中… here).
    page.wait_for_function(
        "() => { const r = document.getElementById('detail-root');"
        "  return r && !/加载中/.test(r.textContent); }", timeout=90000)


def save_restaurant(page, on=True):
    """Press the detail card's Save and follow it through.

    3.2.x's `#bs-foot .ff-fav-btn` toggled Saved directly. 4.0's
    `[data-act="fav"]` opens the collection picker instead (DATA-03), and the
    save happens when a collection is ticked — 默认收藏夹 is the "just save
    it" row, and 取消收藏 removes it from Saved and every collection at once.
    Same user intent, one more deliberate step."""
    page.wait_for_selector('.dt-actions [data-act="fav"]', timeout=20000)
    page.eval_on_selector('.dt-actions [data-act="fav"]', "el => el.click()")
    page.wait_for_function(
        "() => App.state.overlay.kind === 'memberPicker'"
        "      || App.state.overlay.kind === null", timeout=10000)
    if page.evaluate("() => App.state.overlay.kind !== 'memberPicker'"):
        return                       # the module's own fallback toggled directly
    sel = ('[data-ov="member-unsave"]' if not on else '[data-ov="member-default"]')
    page.wait_for_selector(sel, timeout=10000)
    before = page.evaluate("() => App.state.user.fav.size")
    page.eval_on_selector(sel, "el => el.click()")
    # Ticking a collection does not close the picker — you may want several.
    page.wait_for_function("(n) => App.state.user.fav.size !== n",
                           arg=before, timeout=10000)
    if page.evaluate("() => App.state.overlay.kind === 'memberPicker'"):
        page.evaluate("() => App.act.closeOverlay('done')")
        page.wait_for_timeout(200)


def close_detail_card(page):
    page.evaluate("() => App.act.closeDetail && App.act.closeDetail()")
    page.wait_for_timeout(250)


def fav_count(page) -> int:
    """3.2.x read the #ff-fav-count badge; 4.0 renders the Saved count from
    App.state.user.fav, which is what that badge was painted from."""
    return int(page.evaluate("() => App.state.user.fav.size"))


def open_filter_panel(page):
    """3.2.x clicked .wb-filter-btn / #wb-seg's filter segment and waited for
    #ff-sheet-content to be laid out. 4.0: act.setTab('filters') puts the
    filters module's root into whichever host the current mode uses."""
    page.evaluate("() => App.act.setTab('filters')")
    page.wait_for_function(
        "() => { const r = document.getElementById('filters-root');"
        "  return !!(r && r.offsetParent !== null && r.querySelector('#ft-rating')); }",
        timeout=20000)
    page.wait_for_timeout(300)


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
    # 3.2.x: #ss-input, then #ss-local .ss-row (the in-page restaurant index)
    # as distinct from #ss-api (Nominatim, whose host this run blocks). 4.0 has
    # one suggestion list, #ov-sugs, whose restaurant rows carry a rating; the
    # place rows this run cannot fetch simply never appear.
    page.evaluate("() => Overlays.focusSearch()")
    page.wait_for_selector("#ov-sinput", timeout=15000)
    page.fill("#ov-sinput", SEARCH_TERM)
    page.wait_for_function(
        "() => document.querySelectorAll('#ov-sugs .ov-sug').length > 0",
        timeout=20000,
    )
    rows = page.evaluate(
        "() => (Data.search(App.state.search.query, {}) .restaurants || []).length")
    if rows < 5:
        raise AssertionError(
            f"local restaurant search for {SEARCH_TERM!r} returned only {rows} rows"
        )
    # (.ss-empty inside #ss-local is the "+N more matches, type more to
    # narrow" footer, not a no-results state — it is expected on a broad term.)
    # Close the dropdown again so it doesn't sit over the map for the next check.
    # Close the dropdown again so it doesn't sit over the map for the next
    # check. SEARCH-03: 清空 (×) empties the query and stays active; 取消
    # leaves search altogether — press both, in that order, like a user would.
    page.eval_on_selector('[data-ov="search-clear"]', "el => el.click()")
    page.wait_for_timeout(120)
    page.eval_on_selector('[data-ov="search-cancel"]', "el => el.click()")
    page.wait_for_function("() => !App.state.search.active", timeout=5000)
    return f"{rows} local result rows"


def check_card(page, name):
    """Tapping a marker opens a filled detail card with reachable actions.

    3.2.x drove #bs-sheet/.bs-open + #bs-content and measured
    `#bs-foot .ff-fav-btn` / `.rst-gmaps`. 4.0's card is the detail module's
    #detail-root / #detail-foot, whose fixed action row is `.dt-actions` with
    `[data-act="fav"]` (Save) and `.dt-gmaps`'s button (在Google Map 打开).
    The guarantee is unchanged: the card fills, and its two primary actions
    are on screen, at least 44px tall, and actually hit-testable."""
    open_first_marker(page)
    txt = page.eval_on_selector(
        "#detail-root", "el => el.textContent.trim().slice(0, 40)")
    if not txt:
        raise AssertionError("detail card opened empty")
    if page.evaluate("() => /加载失败/.test("
                     "document.getElementById('detail-root').textContent)"):
        raise AssertionError("the detail payload failed to load for this card")
    photos = _photo_geometry(page)
    page.wait_for_function(
        "() => { const n = [...document.querySelectorAll("
        "  '.dt-actions [data-act=\"fav\"], .dt-actions .dt-gmaps')];"
        "  return n.length === 2 && n.every(e => {"
        "    const t = e.closest('a,button') || e;"
        "    const r = t.getBoundingClientRect();"
        "    const h = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
        "    return r.top >= 0 && r.bottom <= innerHeight && (h === t || t.contains(h)); }); }",
        timeout=8000)
    first_actions = _detail_action_geometry(page)
    page.evaluate("a => { window.__smokeFirstMarkerActions = a; }", first_actions)
    close_detail_card(page)
    return f"card shows {txt[:20]!r}, {photos}"


def _detail_action_geometry(page):
    """3.2.x measured #bs-sheet / #bs-content / the two #bs-foot buttons.
    4.0's equivalents are the detail column or sheet body (#detail-root) and
    the fixed action row in #detail-foot."""
    return page.evaluate("""() => {
      const root = document.getElementById('detail-root');
      const foot = document.getElementById('detail-foot');
      const rr = root.getBoundingClientRect();
      const fr = foot ? foot.getBoundingClientRect() : rr;
      const pick = sel => {
        const raw = document.querySelector(sel);
        if (!raw) return null;
        const e = raw.closest('a,button') || raw;
        const r = e.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return {top: r.top, bottom: r.bottom, h: r.height,
                inView: r.top >= 0 && r.bottom <= innerHeight,
                hit: hit === e || e.contains(hit)};
      };
      return {sheet: {top: fr.top, bottom: fr.bottom, height: fr.height},
              content: {top: rr.top, bottom: rr.bottom, height: rr.height,
                        scrollHeight: root.scrollHeight},
              viewport: {width: innerWidth, height: innerHeight},
              items: ['.dt-actions [data-act="fav"]', '.dt-actions .dt-gmaps'].map(pick)};
    }""")


def check_marker_actions(page, name):
    """A marker-origin detail keeps Save and Maps in the visible action dock.

    3.2.x reset the rating slider (#ff-rating), closed the card (.rst-close)
    and the fav drawer (__wbFavDrawer) before re-opening a marker. 4.0 does the
    same setup through the state the modules render from."""
    page.evaluate("""() => {
      App.act.applyFilters({ratingMin: Data.config.RATING_MIN});
      App.act.closeDetail && App.act.closeDetail();
      App.act.setTab('results');
    }""")
    page.wait_for_timeout(400)
    open_first_marker(page, timeout=20000)
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
    page.locator('.dt-actions [data-act="fav"]').first.click(trial=True)
    page.locator('.dt-actions').locator("xpath=.//*[contains(@class,'dt-gmaps')]/..").first.click(trial=True)
    close_detail_card(page)
    return f"Save and Maps are visible 44px actions in {actions['viewport']}"


def _photo_geometry(page):
    """W-4: a thumbnail is a bounded box the image is cropped into — never a
    tall slice showing 11% of itself.

    The 2.x regression: the <img> carried width/height attributes as an
    intrinsic-size placeholder; `width:100%` beat the width hint, nothing beat
    `height:320px`, and CSS aspect-ratio only fills in a side when the other is
    auto — so every photo was a 107x320 slice. 3.2.x asserted the box was 4:3
    and that the <img> did not overflow it.

    4.0's gallery is a bounded strip/grid (`.dt-gallery` has a fixed height,
    `.dt-photo { height: 100% }`) and the image is `object-fit: cover`, so the
    same failure is prevented by a different mechanism. This asserts that
    mechanism plus the two facts that never depended on the geometry:

      * the thumbnail is a BUTTON, not a link (a link opened an Android
        Custom Tab instead of the in-page lightbox);
      * it does not ask the server for a square crop.

    The photo host is blocked on this run, so the <img> may already have been
    replaced by the 照片暂不可用 fallback — that is a legitimate 4.0 state and
    the box, which is what regressed, is asserted either way.
    """
    st = page.evaluate("""() => {
      const g = document.querySelector('#detail-root .dt-gallery');
      if (!g) return null;
      const box = g.querySelector('.dt-photo');
      if (!box) return {empty: g.classList.contains('is-empty')};
      const img = box.querySelector('.dt-photo-img');
      const b = box.getBoundingClientRect();
      const gr = g.getBoundingClientRect();
      return {empty: false, tag: box.tagName, href: box.hasAttribute('href'),
              bw: b.width, bh: b.height, gh: gr.height,
              overflow: getComputedStyle(box).overflow,
              failed: box.classList.contains('is-failed'),
              img: img ? {w: img.getBoundingClientRect().width,
                          h: img.getBoundingClientRect().height,
                          fit: getComputedStyle(img).objectFit,
                          src: img.getAttribute('src') || ''} : null};
    }""")
    if st is None:
        raise AssertionError("the open card has no gallery at all")
    if st.get("empty"):
        # 9,803 of 9,807 rows carry photos; an empty gallery on the row the
        # smoke run happened to pick is legitimate, but say so.
        return "gallery empty for this row"
    if st["tag"] != "BUTTON" or st["href"]:
        raise AssertionError(
            f"a thumbnail is still a link (it opened a Custom Tab in the app): {st}")
    if st["img"] and "_square_" in st["img"]["src"]:
        raise AssertionError(
            f"thumbnail still asks for a server-side square crop: {st['img']['src']}")
    if st["bw"] < 40 or st["bh"] < 40:
        raise AssertionError(f"photo box collapsed: {st}")
    # The box must stay inside the gallery's own height — that is what stops a
    # 320px-tall column from existing at all.
    if st["bh"] > st["gh"] + 2:
        raise AssertionError(f"a thumbnail is taller than its gallery: {st}")
    if st["overflow"] not in ("hidden", "clip"):
        raise AssertionError(
            f"the thumbnail does not clip its image ({st['overflow']}): {st}")
    if st["img"]:
        if st["img"]["fit"] != "cover":
            raise AssertionError(
                f"the image is not object-fit: cover, so it can be squeezed: {st}")
        if st["img"]["h"] > st["bh"] + 2 or st["img"]["w"] > st["bw"] + 2:
            raise AssertionError(f"the img overflows its box (it gets cropped): {st}")
        return f"photo box {st['bw']:.0f}x{st['bh']:.0f}, image cover-fitted"
    if not st["failed"]:
        raise AssertionError(f"the thumbnail lost its image without failing: {st}")
    return f"photo box {st['bw']:.0f}x{st['bh']:.0f} (image unavailable on this run)"


def check_save(page, name):
    """The star toggles, the count moves, and it lands in localStorage.

    3.2.x pressed `#bs-foot .ff-fav-btn` and read `#ff-fav-count`. 4.0's
    button is the detail action row's `[data-act="fav"]`, and the Saved count
    is rendered from App.state.user.fav. localStorage is asserted unchanged —
    it is the half that matters (CLAUDE.md's cardinal rule)."""
    before = page.evaluate(
        "() => { try { const c = JSON.parse("
        "  localStorage.getItem('omakase_state_cache_v2') || '{}');"
        "  return (c.fav || []).length; } catch (e) { return -1; } }"
    )
    n_before = fav_count(page)
    open_first_marker(page, timeout=20000)
    save_restaurant(page)
    page.wait_for_function("(n) => App.state.user.fav.size !== n",
                           arg=n_before, timeout=15000)
    n_after = fav_count(page)
    eq(n_after, n_before + 1, "favorites counter after starring")
    after = page.evaluate(
        "() => { try { const c = JSON.parse("
        "  localStorage.getItem('omakase_state_cache_v2') || '{}');"
        "  return (c.fav || []).length; } catch (e) { return -1; } }"
    )
    eq(after, before + 1, "favorites persisted to localStorage")
    # Undo so the run leaves no state behind for the next check.
    save_restaurant(page, on=False)
    page.wait_for_function("(n) => App.state.user.fav.size === n",
                           arg=n_before, timeout=15000)
    close_detail_card(page)
    return f"{n_before} -> {n_after} favorites, persisted"


def check_filter(page, name):
    """Changing one filter changes the visible count and is persisted.

    3.2.x drove #ff-rating and watched `.ff-count`. 4.0's slider is #ft-rating
    and the match count is Data.M(App.state) — the set the map and the result
    list are both drawn from. The persisted key and its `rating` field are
    unchanged, and that is deliberately still asserted verbatim."""
    before = shown_count(page)
    open_filter_panel(page)
    # Raise the rating threshold to its maximum — always a strict subset.
    page.eval_on_selector(
        "#ft-rating",
        "el => { el.value = el.max; el.dispatchEvent(new Event('input', {bubbles: true}));"
        "        el.dispatchEvent(new Event('change', {bubbles: true})); }",
    )
    page.wait_for_function("(n) => Data.M(App.state).length !== n",
                           arg=before, timeout=15000)
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
    # leave the corpus as we found it for the checks that follow
    page.evaluate("() => App.act.applyFilters({ratingMin: Data.config.RATING_MIN})")
    page.wait_for_timeout(300)
    return f"{before:,} -> {after:,} restaurants"


def check_account(page, name):
    """The account surface opens and can be scrolled to its end.

    3.2.x: #ss-avatar opened #ss-menu and the check asserted the popup was
    non-zero, never taller than the viewport, and scrollable when its content
    overflowed. 4.0 has no avatar dropdown — the same surface is the account
    overlay (LAYER-01: a bottom sheet at narrow, an anchored panel above).
    The three geometry assertions are unchanged, measured on that panel."""
    page.evaluate("() => App.act.openOverlay('account')")
    page.wait_for_function(
        "() => { const n = document.querySelector('#modal-root .ov-modal,"
        " #overlay-root .ov-pop');"
        "  return !!(n && n.getBoundingClientRect().height > 0); }",
        timeout=15000,
    )
    box = page.evaluate(
        "() => { const n = document.querySelector('#modal-root .ov-modal,"
        " #overlay-root .ov-pop');"
        "  const b = n.querySelector('.ov-modal-body, .ov-pop-body') || n;"
        "  return {h: n.getBoundingClientRect().height,"
        "          sh: b.scrollHeight, bh: b.getBoundingClientRect().height,"
        "          ov: getComputedStyle(b).overflowY}; }"
    )
    if box["h"] <= 0:
        raise AssertionError("account panel opened with zero height")
    vh = page.evaluate("() => window.innerHeight")
    if box["h"] > vh + 1:
        raise AssertionError(
            f"account panel is {box['h']:.0f}px tall on a {vh}px viewport and "
            f"cannot be scrolled to its end"
        )
    if box["sh"] > box["bh"] + 1 and box["ov"] not in ("auto", "scroll"):
        raise AssertionError(
            f"account panel content ({box['sh']:.0f}px) overflows its box "
            f"({box['bh']:.0f}px) with overflow-y: {box['ov']}"
        )
    page.keyboard.press("Escape")
    page.wait_for_function("() => !App.state.overlay.kind", timeout=5000)
    return f"panel {box['h']:.0f}px tall on a {vh}px viewport"


def check_lang(page, name):
    """W-9: a first visit below 700px asks for a language before anything
    else. Choosing one writes tabelog.lang and reloads, so it can only ever
    be asked once; the workbench modes never ask (they have the resident
    language button in the top bar). Runs FIRST — the gate owns the screen
    until it is answered.

    3.2.x: #lang-gate with four [data-lg] buttons. 4.0: the overlays module
    renders the same gate into #modal-root as .ov-langgate with four
    [data-ov="gate-lang"] buttons. Every assertion is unchanged."""
    st = page.evaluate("""() => {
      const g = document.querySelector('#modal-root .ov-langgate');
      return {gate: !!g,
              opts: document.querySelectorAll(
                '#modal-root .ov-langgate [data-ov="gate-lang"]').length,
              phone: App.state.layout.mode === 'narrow',
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
    page.evaluate("() => document.querySelector("
                  "'#modal-root .ov-langgate [data-ov=\"gate-lang\"][data-lang=\"zh\"]').click()")
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
    after = page.evaluate("""() => ({gate: !!document.querySelector('#modal-root .ov-langgate'),
        lang: localStorage.getItem('tabelog.lang')})""")
    if after["gate"] or after["lang"] != "zh-CN":
        raise AssertionError(f"the chooser came back after a choice: {after}")
    return "asked once, answered, gone"


def check_segmented_pill(page, name):
    """4.0 (LAY-03): the bottom sheet's header is the narrow entry.

    History: 3.1.x docked a four-button `#phone-nav` bar; 3.2 replaced it with
    a floating `#wb-seg` pill that opened an overlay drawer. 4.0 replaces both
    with the bottom sheet itself — its collapsed header IS the entry bar, with
    three segments (结果 / 收藏 / 筛选), and opening one raises the sheet over
    a map that keeps painting behind it.

    The list of things this check protects is carried over unchanged, each
    re-pointed at the element that now provides it:

      * none of the retired phone chrome (`#phone-nav`, `#ss-drawer-btn`,
        `#wb-seg`) is in the DOM, and nothing phone-only leaks into the
        column layouts;
      * all three segments are reachable touch targets that hit-test to
        themselves, and the entry bar never overlaps the right-hand FAB stack;
      * each segment opens the panel on its own tab, and the panel can be
        collapsed back to the map;
      * the filter panel has exactly one host and no competing modal layer;
      * a detail opened from a result row returns to that row with focus;
      * the map stays painted beside/behind the open panel.
    """
    phone = page.evaluate("() => App.state.layout.mode === 'narrow'")
    retired = ['#phone-nav', '#ss-drawer-btn', '#wb-seg']
    present = page.evaluate(
        "(sels) => sels.filter(s => !!document.querySelector(s))", retired)
    if present:
        raise AssertionError(f"retired phone chrome is still in the DOM: {present}")
    if not phone:
        return "phone entries hidden in workbench"

    # A previous check may have left the panel open on the filters tab.
    page.evaluate("() => { App.act.closeDetail && App.act.closeDetail();"
                  "        App.act.setSheet('collapsed'); }")
    page.wait_for_timeout(400)
    state = page.evaluate("""() => {
      const R = el => { const r = el.getBoundingClientRect();
        return {l:r.left, t:r.top, r:r.right, b:r.bottom, w:r.width, h:r.height}; };
      const head = document.getElementById('sheet-head');
      const fab  = document.getElementById('fab-root');
      const segs = [...head.querySelectorAll('[data-ct="tab"]')].map(e => {
        const r = e.getBoundingClientRect();
        const t = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
        return {tab: e.dataset.tab, w: r.width, h: r.height,
                inView: r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight,
                hit: t === e || e.contains(t)};
      });
      const fr = fab && fab.getBoundingClientRect();
      const fabBox = (fr && fr.width) ? R(fab) : null;
      return {segments: segs, sheet: R(document.getElementById('sheet')), stack: fabBox};
    }""")
    if len(state["segments"]) != 3:
        raise AssertionError(
            f"the entry bar carries {len(state['segments'])} segments, expected 3")
    if {e["tab"] for e in state["segments"]} != {"results", "saved", "filters"}:
        raise AssertionError(f"unexpected segments: {state['segments']}")
    # 44px is the touch minimum; the segments sit in a row, so each one only
    # has to be 44 tall.
    if any(e["h"] < 44 or e["w"] < 28 or not e["inView"] or not e["hit"]
           for e in state["segments"]):
        raise AssertionError(f"an entry segment is unreachable: {state['segments']}")
    # The entry bar and the FAB column share the bottom of the screen and must
    # not intersect (3.1.x's #ff-fab did — the audit's fold416 shots).
    if state["stack"]:
        p, st = state["sheet"], state["stack"]
        if p["r"] > st["l"] and p["l"] < st["r"] and p["b"] > st["t"] and p["t"] < st["b"]:
            raise AssertionError(
                f"the collapsed sheet overlaps the FAB stack: sheet={p}, stack={st}")

    def go(tab):
        page.evaluate("(t) => App.act.setTab(t)", tab)
        page.wait_for_timeout(500)
        st = page.evaluate("""() => { const m = document.querySelector('.folium-map');
          const cs = getComputedStyle(m); const sh = document.getElementById('sheet');
          return {tab: App.state.sheet.tab, state: App.state.sheet.state,
                  h: sh.getBoundingClientRect().height, vh: innerHeight,
                  mapPainted: cs.visibility !== 'hidden' && cs.display !== 'none'}; }""")
        if st["tab"] != tab or st["state"] == "collapsed":
            raise AssertionError(f"panel state wrong for {tab}: {st}")
        if not st["mapPainted"]:
            raise AssertionError(f"the map stopped painting behind the panel: {st}")
        # LAY-03: the panel never takes the whole screen from a tab switch —
        # the map band above it is what makes this map-first.
        if st["h"] > st["vh"] - 80:
            raise AssertionError(f"the panel swallowed the map: {st}")

    go("results")
    page.wait_for_selector("#list-root .ls-row[data-id]", timeout=20000)
    go("filters")
    filtered = page.evaluate("""() => {
      const r = document.getElementById('filters-root');
      return {panel: r.offsetParent !== null, host: r.parentElement.id,
              overlay: App.state.overlay.kind,
              see: !!document.querySelector('#filters-foot [data-see-results]')};
    }""")
    if not filtered["panel"] or filtered["host"] != "sheet-body" or not filtered["see"]:
        raise AssertionError(
            f"Filters did not expose the single complete panel: {filtered}")
    if filtered["overlay"]:
        raise AssertionError(f"Filters opened a competing modal layer: {filtered}")
    go("saved")
    go("results")

    # A real result click must provide a return action, and that action must
    # restore the source row as the keyboard focus target (NAV-02).
    page.evaluate("() => App.act.setSheet('expanded')")
    page.wait_for_timeout(400)
    row = page.locator("#list-root .ls-row[data-id]").first
    row.wait_for(state="visible")
    ref = row.get_attribute("data-id")
    row.locator(".ls-open").click()
    page.wait_for_function(DETAIL_OPEN_JS, timeout=30000)
    page.locator('#sheet-head [data-ct="back"]').click(trial=True)
    page.locator('#sheet-head [data-ct="back"]').click()
    # The restore runs behind a rAF and repeats once with measured row heights,
    # so wait for the result rather than for a fixed number of milliseconds.
    try:
        page.wait_for_function(
            "(ref) => { const a = document.activeElement;"
            "  const row = a && a.closest && a.closest('.ls-row');"
            "  return !App.state.selected.id && row"
            "         && row.getAttribute('data-id') === ref; }",
            arg=ref, timeout=8000)
    except Exception:
        pass                      # let the assertion below report what happened
    returned = page.evaluate("""(ref) => ({tab: App.state.sheet.tab,
      selected: App.state.selected.id,
      focusRow: document.activeElement && document.activeElement.closest
                && document.activeElement.closest('.ls-row')
                && document.activeElement.closest('.ls-row').getAttribute('data-id'),
      active: document.activeElement && (document.activeElement.id ||
              document.activeElement.className || document.activeElement.tagName),
      rowsRendered: document.querySelectorAll('#list-root .ls-row[data-id]').length,
      rowPresent: !!document.querySelector('#list-root .ls-row[data-id="' +
                  (window.CSS && CSS.escape ? CSS.escape(ref) : ref) + '"]'),
      sheet: App.state.sheet.state,
      list: document.getElementById('list-root').offsetParent !== null})""", ref)
    if (returned["tab"] != "results" or returned["selected"] is not None
            or not returned["list"] or returned["focusRow"] != ref):
        raise AssertionError(
            f"detail return lost its source or keyboard focus: {returned} (row {ref})")
    page.evaluate("() => App.act.setSheet('collapsed')")
    page.wait_for_timeout(300)
    if page.evaluate("() => App.state.sheet.state") != "collapsed":
        raise AssertionError("collapsing the panel left it open")
    return ("3 entry segments reachable and clear of the FAB stack, 3 tabs, "
            "one filter host, detail source/focus restored, map painted")


def check_chrome(page, name):
    """The chip row lines up with the search capsule and the intro row clears it.

    3.2.x measured #ss-chips / #ux-region / #intro-bar / #ss-input-wrap. In 4.0
    the chip row is `.ov-chips` under the search capsule `.ov-capsule`, and the
    first-run intro (M-109) is a notice row in #notice-root. Same two
    guarantees: the chips share the capsule's left edge, and the intro row does
    not sit on top of them."""
    g = page.evaluate("""() => {
      const R = s => { const e = document.querySelector(s);
        if (!e || !e.getClientRects().length) return null;
        const r = e.getBoundingClientRect();
        return {l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height}; };
      return {cover: !!App.state.layout.foldCover, caprow: R('.ov-caprow'),
              nearby: R('#search-root [data-ov="nearby"]'), avatar: R('#search-root .ov-avatar'),
              stack: R('#fab-root'), chips: R('.ov-chips'),
              region: R(App.state.layout.foldCover ? '#search-root [data-kind="regionPicker"]' : '.ov-chips [data-kind="regionPicker"]'),
              intro: R('#notice-root .ov-notice'),
              input: R('.ov-capsule') || R('#ov-sinput') || R('.ov-topfield'),
              phone: App.state.layout.mode === 'narrow'};
    }""")
    out = []
    if g["phone"] and g["cover"]:
        controls = [g[k] for k in ["input", "region", "nearby", "avatar"]]
        if not all(c and c["w"] >= 44 and c["h"] >= 44 for c in controls):
            raise AssertionError(f"Fold header controls need 44px touch bounds: {g}")
        centres = [c["t"] + c["h"] / 2 for c in controls]
        if max(centres) - min(centres) > 1 or any(a["r"] > b["l"] for a,b in zip(controls,controls[1:])):
            raise AssertionError(f"Fold header must order search, region, nearby, account on one row: {g}")
        out.append("Fold header controls share one row and retain 44px targets")
    elif g["phone"]:
        if not g["chips"] or not g["region"]:
            raise AssertionError(f"phone header is missing its chip row / region chip: {g}")
        if abs(g["chips"]["l"] - g["input"]["l"]) > 4:
            raise AssertionError(f"chip row is not aligned with the search capsule: {g}")
        out.append("chip row aligned with the capsule")
    if g["phone"] and g["intro"] and g["input"]:
        # 3.2.x asserted the intro bar shared the search input's LEFT EDGE,
        # because it was a header-width bar. 4.0's approved design (the demo's
        # own #notice-root: `display:grid; justify-items:center` with the row
        # capped at min(560px,100%)) makes every notice a centred pill, so an
        # exact left match is asserting the old layout, not the guarantee. What
        # W-10 is about is that the intro row belongs to the header band and
        # does not sit on top of the chip row, so that is what is asserted:
        # inside the page gutter, no wider than the header, below the chips.
        band = (g["caprow"] if g["cover"] else g["chips"]) or g["input"]
        if g["intro"]["l"] < band["l"] - 1 or g["intro"]["r"] > band["r"] + 1:
            raise AssertionError(
                f"intro row escapes the header band (the chip row's span): {g}")
        if g["chips"] and g["intro"]["t"] < g["chips"]["b"] - 1:
            raise AssertionError(f"intro row overlaps the chip row: {g}")
        out.append("intro row inside the header band, below the chip row")
    if not g["phone"] and g["intro"] and g["chips"] and g["intro"]["t"] < g["chips"]["b"] - 1:
        raise AssertionError(f"intro row overlaps the chip row on the workbench: {g}")
    return "; ".join(out) or "no intro row / phone chrome on this viewport"


def check_workbench(page, name):
    """W-1 / W-2 / W-3: the mid + wide column layout.

    W-2  the detail column costs the map nothing until a restaurant is picked
    W-3  the 56px rail carries the tab buttons with BOTH counts (符合筛选 N and
         屏幕内 M) and no duplicate of a control the top bar already shows;
         mid and wide can both collapse the left column onto that rail and get
         it back (W-6), and the choice is persisted
    W-1  the card inside the detail column has real top padding

    3.2.x drove #wb-rail / #wb-detail / #wb-left-collapse / #wb-rail-back and
    the --wb-left / --wb-right custom properties. 4.0's equivalents are
    #col-left-rail / #col-detail / [data-ct="collapse-left"] /
    [data-ct="expand-left"] and --col-left-w / --col-detail-w. Every assertion
    is the same fact about the same layout."""
    mode = page.evaluate("() => App.state.layout.mode")
    if mode not in ("mid", "wide"):
        return f"n/a in {mode} mode"

    # The rail's search / filter buttons were copies of top-bar controls that
    # are on screen at the same time (W-3b).
    extra = page.evaluate("""() => {
      const rail = document.getElementById('col-left-rail');
      return {search: rail.querySelectorAll('[data-ov="search-activate"], .ov-capsule').length,
              dup: rail.querySelectorAll('#ov-sinput').length};
    }""")
    if extra["search"] or extra["dup"]:
        raise AssertionError(f"the rail still carries duplicate controls: {extra}")
    # W-4: two numbers on the rail, not one — the match count and the in-view
    # count. 3.2.x read .ff-count / .ff-inview; 4.0 renders them into the
    # rail's own tab buttons and the left column head.
    nums = page.evaluate("""() => {
      const txt = (document.getElementById('col-left-rail').textContent || '') +
                  (document.getElementById('col-left-head').textContent || '');
      return {digits: (txt.match(/[0-9][0-9,]*/g) || []).length,
              M: Data.M(App.state).length,
              mv: window.MapMod && MapMod.MV ? MapMod.MV().length : -1};
    }""")
    if nums["digits"] < 2:
        raise AssertionError(f"the rail/head does not carry both counts: {nums}")
    if nums["mv"] < 0:
        raise AssertionError(f"no in-view count is published: {nums}")

    # W-2: nothing selected -> the detail column is off-canvas and
    # --col-detail-w is 0, so the map owns that space.
    page.evaluate("() => App.act.closeDetail && App.act.closeDetail()")
    page.wait_for_timeout(500)
    closed = page.evaluate("""() => {
      const r = document.getElementById('col-detail').getBoundingClientRect();
      return {right: getComputedStyle(document.documentElement)
                       .getPropertyValue('--col-detail-w').trim(),
              offscreen: r.left >= innerWidth - 1 || r.width === 0,
              open: App.state.columns.detailOpen};
    }""")
    if closed["open"] or closed["right"] != "0px" or not closed["offscreen"]:
        raise AssertionError(f"detail column still resident with no selection: {closed}")

    # ... and selecting a restaurant slides it in and pays for it. check_filter
    # ran before this one; it restores the rating slider itself, but be explicit
    # — 31 survivors nationwide can leave this viewport with no marker to click.
    page.evaluate("() => App.act.applyFilters({ratingMin: Data.config.RATING_MIN})")
    page.wait_for_timeout(400)
    open_first_marker(page, timeout=20000)
    page.wait_for_timeout(500)
    opened = page.evaluate("""() => {
      const d = document.getElementById('col-detail');
      const card = document.querySelector('#col-detail-body #detail-root');
      const title = d.querySelector('.dt-title-row');
      const close = title && title.querySelector('[data-ct="close-detail"]');
      const tr = title && title.getBoundingClientRect(), cr = close && close.getBoundingClientRect();
      return {mode: App.state.layout.mode,
              titlePad: tr ? tr.left - d.getBoundingClientRect().left : -1,
              titleTop: tr ? tr.top - d.getBoundingClientRect().top : -1,
              closeHit: cr ? [cr.width, cr.height] : null,
              closeInTitle: !!(cr && tr && cr.top < tr.bottom && cr.bottom > tr.top),
              right: getComputedStyle(document.documentElement)
                       .getPropertyValue('--col-detail-w').trim(),
              onscreen: d.getBoundingClientRect().left < innerWidth - 100,
              gap: card ? Math.round(card.getBoundingClientRect().top
                                     - d.getBoundingClientRect().top) : -1};
    }""")
    if opened["right"] == "0px" or not opened["onscreen"]:
        raise AssertionError(f"detail column did not slide in: {opened}")
    # 4.1.0 merges mid's close into the title row. Protect its actual padding
    # and 44px control instead of requiring the removed, empty header row.
    if opened["mode"] == "mid":
        if (opened["titlePad"] < 16 or opened["titleTop"] < 10
                or not opened["closeInTitle"] or not opened["closeHit"]
                or min(opened["closeHit"]) < 44):
            raise AssertionError(f"compact title lost spacing or close target: {opened}")
    elif opened["gap"] < 10:
        raise AssertionError(
            f"card is flush against the top of the detail column: {opened}")
    page.evaluate("() => App.act.closeDetail && App.act.closeDetail()")
    page.wait_for_timeout(500)

    # W-6 (2.3.0): mid AND wide -- collapse the left column onto the rail and
    # back. The expanded width is the mode's own column width, which 4.0
    # derives from the viewport (LAY-02) rather than pinning to 320/344.
    full = page.evaluate("() => App.layout.columns(App.state).fullLeft + 'px'")
    page.eval_on_selector('[data-ct="collapse-left"]', "el => el.click()")
    page.wait_for_timeout(500)
    coll = page.evaluate("""() => {
      const rail = document.getElementById('col-left-rail');
      return {left: getComputedStyle(document.documentElement)
                      .getPropertyValue('--col-left-w').trim(),
              rail: getComputedStyle(rail).display,
              railVisible: rail.getBoundingClientRect().width > 0,
              saved: JSON.parse(localStorage.getItem('tabelog.listView') || '{}')
                       .leftCollapsed};
    }""")
    if coll["left"] != "56px" or coll["rail"] == "none" or not coll["railVisible"]:
        raise AssertionError(f"collapsing the left column did not land: {coll}")
    if coll["saved"] is not True:
        raise AssertionError(
            f"leftCollapsed was not persisted in tabelog.listView: {coll}")
    page.eval_on_selector('[data-ct="expand-left"]', "el => el.click()")
    page.wait_for_timeout(500)
    back = page.evaluate(
        "() => ({left: getComputedStyle(document.documentElement)"
        "          .getPropertyValue('--col-left-w').trim(),"
        "        saved: JSON.parse(localStorage.getItem('tabelog.listView') || '{}')"
        "                 .leftCollapsed})")
    if back["left"] != full or back["saved"] is not False:
        raise AssertionError(
            f"the rail's hamburger did not restore the column: {back} (expected {full})")
    return f"{mode}: detail auto-collapses, left column collapses to the rail"


def check_goto_zoom(page, name):
    """W-5: picking a restaurant from the result list flies the map to it.

    Until 2.1.0 only the search box did. The list, the Saved tab, the card's
    up/down stepper and marker taps all called openSheet(), which only ever
    pans — so choosing a Kanazawa restaurant from a zoom-10 view of Japan
    opened a card for a pin that stayed invisible.

    3.2.x clicked `.wb-row` and asserted the map ended at >= 16 (GOTO_ZOOM).
    4.0's result rows are `#list-root .ls-row[data-id]`; opening one goes
    through act.openDetail -> MapMod.reveal, which is the same promise: the
    chosen restaurant ends up inside the visible map rect at a zoom you can
    see it at."""
    # closeDetail collapses the phone sheet, but virtual rows can remain in
    # its hidden DOM until the next map render. Open the task explicitly;
    # DOM presence alone does not mean a result is available to click.
    page.evaluate("""() => {
      App.act.closeDetail();
      App.act.setTab('results');
      if (App.state.layout.mode === 'narrow') App.act.setSheet('expanded');
    }""")
    row = page.locator('#list-root .ls-row[data-id]').first
    row.wait_for(state='visible', timeout=20000)
    # A country-wide view, i.e. the state the bug was reported from.
    page.evaluate("() => { " + MAP_HANDLE_JS
                  + " if (m) m.setView([35.68, 139.76], 10, {animate: false}); }")
    page.wait_for_function("() => App.state.mapView.zoom === 10", timeout=20000)
    # Moving the map can rebuild the virtual window. Resolve the real row
    # again after the move; an empty visible Results list must still fail.
    row.wait_for(state='visible', timeout=20000)
    before = page.evaluate("() => { " + MAP_HANDLE_JS + " return m ? m.getZoom() : null; }")
    if before != 10:
        raise AssertionError(f"could not set the starting view (zoom={before})")
    rid = row.get_attribute('data-id')
    row.locator('.ls-open').click()
    page.wait_for_function(DETAIL_OPEN_JS, timeout=20000)
    page.wait_for_timeout(1200)      # let the move finish
    after = page.evaluate("() => { " + MAP_HANDLE_JS + " return m.getZoom(); }")
    # 2.1.0 fixed this by flying to GOTO_ZOOM (17) and the check asserted
    # zoom >= 16. 4.0 keeps the promise by a different mechanism (LAY-05):
    # reveal() makes ONE minimal move — deliberately preserving the user's
    # spatial context instead of throwing it away — and the picked restaurant
    # is pulled out of its cluster into its own pane with a selection ring, a
    # price tag and a name bubble, so it is identifiable at whatever zoom the
    # user was at. Asserting the zoom number would now be asserting the old
    # implementation; what W-5 is about is that the restaurant you picked is
    # SOMETHING YOU CAN SEE, so that is what is asserted.
    seen = page.evaluate("""(id) => {
      const r = Data.byId(id); if (!r) return {err: 'unknown id'};
      const b = MapMod.bounds();
      const inBounds = r.lat >= b.south && r.lat <= b.north
                    && r.lon >= b.west && r.lon <= b.east;
      const sel = document.querySelector('.mp-mk.is-selected');
      let onScreen = false, box = null;
      if (sel) {
        const q = sel.getBoundingClientRect();
        box = {w: q.width, h: q.height};
        const mr = App.state.layout.mapRect;
        onScreen = q.width > 0 && q.height > 0
                && q.left >= mr.x - 1 && q.top >= mr.y - 1
                && q.right <= mr.x + mr.w + 1 && q.bottom <= mr.y + mr.h + 1;
      }
      return {inBounds, marker: !!sel, onScreen, box,
              selected: App.state.selected.id === id};
    }""", rid)
    if not seen.get("selected"):
        raise AssertionError(f"the row did not select its own restaurant: {seen}")
    if not seen["inBounds"]:
        raise AssertionError(
            f"picking a result row left the restaurant outside the map view: "
            f"{seen} (W-5 regression)")
    if not seen["marker"]:
        raise AssertionError(
            f"the picked restaurant has no marker of its own — it is still "
            f"inside a cluster and therefore invisible: {seen} (W-5 regression)")
    if not seen["onScreen"]:
        raise AssertionError(
            f"the picked restaurant's marker is outside the visible map rect "
            f"(behind a column or the panel): {seen} (W-5 regression)")
    page.evaluate("() => App.act.closeDetail && App.act.closeDetail()")
    page.wait_for_timeout(300)
    return (f"list row: zoom {before} -> {after}, card opened, the picked "
            f"restaurant has its own visible marker inside the map rect")


def check_filter_copy(page, name):
    """W-8 / W-11 / W-12: one 其它 section whose five toggles are load-bearing,
    counters that read as a sentence rather than a number glued to a label,
    and the "all matches are off-screen" hint appearing at most once per load.

    3.2.x drove #ff-sheet-content's --ff-row/--ff-head, the five #ff-*-only
    checkbox ids, .ff-count / #ff-head-counts / .wb-counts and the
    #ffe-offscreen card with #ffe-off-close / #ffe-off-never. 4.0's filter
    form is the filters module (`[data-sw="<field>"]` switches in the
    `[data-section="other"]` section), the match sentence is the footer's
    查看 N 家结果, and the off-screen card is the list module's `.ls-oov` with
    [data-act="oov-close"] / [data-act="oov-never"]. The three things asserted
    are unchanged: the five filter FIELDS survive, the counters are sentences,
    and the hint is once-per-load."""
    open_filter_panel(page)
    m = page.evaluate("""() => {
      const root = document.getElementById('filters-root');
      const want = ['bookableOnly','favOnly','hideBlack','hideForeign','gcalOnly'];
      const other = root.querySelector('[data-section="other"]');
      return {
        missing: want.filter(k => !root.querySelector('[data-sw="' + k + '"]')),
        others: other ? other.querySelectorAll('[data-sw]').length : 0,
        helpInLabel: !!root.querySelector('.ft-sw-row .ft-help-btn'),
        n: String(Data.M(App.state).length),
        foot_txt: (document.getElementById('filters-foot') || {}).textContent || '',
        head_txt: ((document.getElementById('sheet-head') || {}).textContent || '') +
                  ((document.getElementById('col-left-head') || {}).textContent || '')
      };
    }""")
    if m["missing"]:
        raise AssertionError(f"filter toggle field(s) gone: {m['missing']}")
    if m["others"] != 5:
        raise AssertionError(f"expected 5 switches in 其它, got {m['others']}")
    if m["helpInLabel"]:
        raise AssertionError("a help button is inside a switch <label> — "
                             "clicking help would toggle the checkbox")
    # The sentence carries the number; the number alone is not the sentence.
    txt = " ".join(m["foot_txt"].split())
    if m["n"] not in txt.replace(",", ""):
        raise AssertionError(f"the footer lost the match count: {txt!r}")
    if len(txt) <= len(m["n"]) + 4:
        raise AssertionError(f"the footer is not a sentence: {txt!r}")

    # W-12: the off-screen hint is once per page load, and its × / 不再提示
    # are really clickable (the card used to be pointer-events:none). Pan onto
    # open water, dismiss the card, then pan again: it must not come back.
    page.evaluate("() => { App.act.setTab('results'); App.act.setSheet('browse'); }")
    page.wait_for_timeout(500)
    mapjs = "window[Object.keys(window).find(k => k.startsWith('map_'))]"
    home = page.evaluate(
        "() => { const m = %s; const c = m.getCenter();"
        "  return [c.lat, c.lng, m.getZoom()]; }" % mapjs
    )
    oov_visible = ("() => { const o = document.querySelector('#list-root .ls-oov');"
                   "  return !!(o && o.getClientRects().length); }")
    seen = False
    for i, (lat, lon) in enumerate(((30.0, 140.0), (30.5, 140.5))):
        page.evaluate("() => { %s.setView([%f, %f], 11); }" % (mapjs, lat, lon))
        page.wait_for_timeout(2500)
        vis = page.evaluate(oov_visible)
        if i == 1 and vis:
            raise AssertionError("off-screen hint came back after being dismissed")
        if vis:
            seen = True
            btns = page.evaluate(
                "() => ['oov-close','oov-never'].map(k => { const b ="
                "  document.querySelector('#list-root [data-act=\"' + k + '\"]');"
                "  return b ? getComputedStyle(b).pointerEvents : 'MISSING'; })")
            if btns != ["auto", "auto"]:
                raise AssertionError(f"off-screen hint buttons unclickable: {btns}")
            page.eval_on_selector('#list-root [data-act="oov-close"]', "el => el.click()")
            page.wait_for_timeout(400)
            if page.evaluate(oov_visible):
                raise AssertionError("the hint's x did not dismiss it")
    page.evaluate(
        "() => { %s.setView([%f, %f], %d); }" % (mapjs, home[0], home[1], home[2])
    )
    page.wait_for_timeout(800)
    return (f"5 其它 toggles kept, footer is a sentence, "
            f"hint at most once (seen={seen})")


CHECKS = [
    ("lang", check_lang),      # W-9 — first, it is modal until answered
    ("boot", check_boot),
    ("search", check_search),
    ("card", check_card),
    ("save", check_save),
    ("filter", check_filter),
    ("account", check_account),
    ("seg-pill", check_segmented_pill), # M-3.2-02/03 the drawer's one entry
    ("chrome", check_chrome),           # W-10 / 3.2 chip-row clearance
    ("workbench", check_workbench),     # W-1 / W-2 / W-3
    ("filter-copy", check_filter_copy), # W-8 / W-11 / W-12
    ("goto-zoom", check_goto_zoom),     # W-5
    ("marker-actions", check_marker_actions),  # M-3.2-06 #bs-foot action dock
]


def main(argv: list[str] | None = None) -> int:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--viewport", action="append", choices=sorted(VIEWPORTS),
                    help="run only this viewport (repeatable)")
    ap.add_argument("--browser", choices=("chromium", "webkit"), default="chromium",
                    help="engine to run. Each viewport declares the one it is "
                         "measured on; the iPhone rows are WebKit-only and are "
                         "skipped under Chromium rather than faked by it.")
    ap.add_argument("--screenshots", type=Path, default=None,
                    help="write one PNG per viewport into this directory")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)

    asked = args.viewport or list(VIEWPORTS)
    # M-3.2-11: never let one engine stand in for another. An explicit
    # --viewport for the wrong engine is an error (the caller meant something
    # this run cannot honour); the default sweep just skips the other half.
    wrong = [n for n in asked if VIEWPORTS[n]["browser"] != args.browser]
    if args.viewport and wrong:
        print("these viewports are measured on a different engine: "
              + ", ".join(f"{n} ({VIEWPORTS[n]['browser']})" for n in wrong))
        return 2
    names = [n for n in asked if VIEWPORTS[n]["browser"] == args.browser]
    if not names:
        print(f"smoke: no {args.browser} viewports selected")
        return 1
    for n in wrong:
        print(f"[{n}] skipped here — it is a {VIEWPORTS[n]['browser']} row")
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)

    n_fail = 0
    with serve_docs(8926) as base, sync_playwright() as p:
        browser = getattr(p, args.browser).launch(headless=not args.headed)
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

    print(f"\nsmoke[{args.browser}]: {len(names) - n_fail}/{len(names)} viewports passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
