# UX regressions

Run against the normal generated `docs/` after the root build:

```sh
.venv-wsl/bin/python tests/ux/run.py --output /tmp/jpfoodmap-ux-webkit
.venv-wsl/bin/python tests/ux/run.py --browser chromium --widths 393,475,591,932,1440 --output /tmp/jpfoodmap-ux-chromium
.venv-wsl/bin/python tests/ux/supplement.py --output /tmp/jpfoodmap-ux-supplement
```

The Fold regression sizes are 475×751 and 932×704 CSS pixels, with 591×689 for the partial-width layout. Other WebKit cases cover 320×568, 393×852, 667×375, 852×393 and 1440×900.

Each run uses a fresh browser context, blocks external HTTPS requests, and serves local files only on 127.0.0.1. No real sign-in, sync API, external navigation, GPS or user profile is used. Location success and denial are injected into the browser API. Keyboard tests synthesize visualViewport changes and an occluding layer; import text tests double computed font sizes. These are not native iOS keyboard or Page Zoom tests.

`run.py` checks region/filter completion, result scroll and Saved source restoration, Save/Maps reachability, nearby scope, persistent return after reload, permission denial, collection keyboard access, import containment and page errors. `supplement.py` checks keyboard shortcuts, Escape, retained detail nodes through Fold-width changes, visualViewport offset, and first-visit context menu clicks.

`--docs <directory>` selects a separate generated page. `--baseline` is only for an old 2.3.0 build: it records the original defects without asserting repaired behavior. `build_preview.py` is a disposable implementation preview helper; it redirects all generator writes to the audit output directory, and must run under the normal build lock. It does not replace the production build gate.
