# UX regressions

Run against the normal generated `docs/` after the root build:

```sh
.venv-wsl/bin/python tests/ux/run.py --output /tmp/jpfoodmap-ux-webkit
.venv-wsl/bin/python tests/ux/run.py --browser chromium --widths 393,475,591,932,1440 --output /tmp/jpfoodmap-ux-chromium
.venv-wsl/bin/python tests/ux/supplement.py --output /tmp/jpfoodmap-ux-supplement
.venv-wsl/bin/python tests/ux/visibility.py --output /tmp/jpfoodmap-ux-visibility
.venv-wsl/bin/python tests/ux/tiles.py
.venv-wsl/bin/python tests/ux/tiles.py --browser webkit
.venv-wsl/bin/python tests/ux/pinch.py
.venv-wsl/bin/python tests/ux/pinch.py --browser webkit
```

The Fold regression sizes are 475×751 and 932×704 CSS pixels, with 591×689 for the partial-width layout. Other WebKit cases cover 320×568, 393×852, 667×375, 852×393 and 1440×900.

Each run uses a fresh browser context, blocks external HTTPS requests, and serves local files only on 127.0.0.1. No real sign-in, sync API, external navigation, GPS or user profile is used. Location success and denial are injected into the browser API. Keyboard tests synthesize visualViewport changes and an occluding layer; import text tests double computed font sizes. These are not native iOS keyboard or Page Zoom tests.

`visibility.py` (M-3.2-11) is the release's acceptance measurement: the map's share of the screen on 402x874 (WebKit), 475x751 and 591x689 in the home, drawer, detail and layers states, against the floors the engineering plan set (home >= 78%, drawer >= 30% at 591 and >= 10% at 402, detail >= 18%), plus no page-level horizontal overflow, none of the `ux-phone/table_E.md` strings clipped, and no full-width comma or semicolon in the shipped EN/JA tables. Since M-3.2-R1 every viewport is measured in all three UI languages (zh-CN / en / ja; `--langs` narrows it) — the intro bar's height is language-dependent and the English run was the one that broke the 78% floor. It replaces `audit_outputs/3.2.0-plan/ux-phone/run_flows.py`, which still drives the deleted `#phone-nav` and needs two versions of the site served side by side -- do not run that script.

`tiles.py` (3.2.2) is the tile-resolution switch, on a DPR 2 context: `@2x` in the tile URLs with no rail layer on, none once 长途 is switched on, `@2x` back after both go off, and — the one that needs the script to run before folium's map script — no `@2x` at all on a cold start whose localStorage already has 市内 on. It stubs the tile CDN with a 1×1 PNG and the R2 overlay with an empty FeatureCollection, so it asserts URLs, not pictures, and a toggle-on cannot roll itself back through `lodloaderror`. It writes no screenshots and takes no `--output`.

`pinch.py` (3.2.3) is the page-zoom gate: can a two-finger spread scale the document instead of the map, on 932×704 and 475×751 at DPR 2.625 with the main thread throttled 4×/6× around each gesture. It is explicit about its own limits — headless Chromium delivers a synthesized pinch (Leaflet zooms from it, which is how the map cases are asserted) but never applies *browser* pinch zoom, so `visualViewport.scale` reads 1 even on a build that allows zoom and is only a tripwire. The proof is what Chrome consults first: the `<meta viewport>` string and the effective `touch-action` walked from `elementFromPoint` up to `<html>` (which is also why the map still pinches — `.leaflet-container` is `touch-action: none`), plus, for iOS, a cancelable `gesturestart` dispatched at each surface. Eight moments: the first painted frame (an rAF probe installed before any page script), the window where the UI is up and the map is not (`L.map` poisoned so the container is never created), the language gate, the home chrome, the map at `minZoom` and Tokyo z13, the result list, the card, and desktop ctrl+wheel at 1440×900. `--label before` records without asserting; `--docs` points it at another generated page, which is how the 3.2.2 column of `audit_outputs/3.2.3/REPORT.md` was measured.

`run.py` checks region/filter completion, result scroll and Saved source restoration, the segmented pill (three segments, live count, clear of the FAB column) and the `#ss-chips` region chip, Save/Maps reachability in `#bs-foot`, nearby scope, persistent return after reload, permission denial, collection keyboard access, import containment and page errors. `supplement.py` checks keyboard shortcuts, Escape, retained detail nodes through Fold-width changes, visualViewport offset, and first-visit context menu clicks.

`--docs <directory>` selects a separate generated page. `--baseline` is only for an old 2.3.0 build: it records the original defects without asserting repaired behavior. `build_preview.py` is a disposable implementation preview helper; it redirects all generator writes to the audit output directory, and must run under the normal build lock. It does not replace the production build gate.
