# `src/tabelog/ui/` — the 4.0.0 front end

This directory is the **source** of everything the browser runs except the
vendored libraries. `map.py` reads these files at build time, substitutes the
placeholders listed below, and **inlines** them into `docs/index.html` (one
`<style>` per CSS file, one `<script>` per JS file, the shell HTML as-is).
Nothing here is served as a separate URL — the single-file deployment and
the service worker's `APP_SHELL_URLS` atomic-install contract are unchanged.
`docs/index.html` is still a build output; edit here, rebuild, never there.

```
src/tabelog/ui/
  shell.html            the <body> skeleton: fixed DOM slots (CONTRACT §4)
  css/tokens.css        DESIGN-TOKENS.json → CSS variables (the only source of numbers)
  css/base.css          reset, type roles, shared components, #app / #map-root structure
  css/{map,containers,list,detail,filters,overlays}.css    one per module
  js/business.js        GENERATED — the 3.2.x business layer, verbatim (see below)
  js/core.js            App: state tree, events, rAF render, nav router, layout, i18n, emoji, motion
  js/adapter.js         the ONE seam: App.act / Data.* ⇄ Business.api; owns the per-device prefs
  js/{map,containers,list,detail,filters,overlays}.js      one per module (stubs until ported)
  i18n/ui-strings.json  every zh key the modules pass to t(), with en + ja; tw is generated
```

Load order in the built page (fixed by `map.py`, mirrors the demo's `app.html`):

```
<head>  tokens.css → base.css → {map,containers,list,detail,filters,overlays}.css
        (folium's own <head>: leaflet.css, MarkerCluster*.css, locate plugin, transit-layer.js)
<body>  TILE_DPR_SWITCH_JS → shell.html → business.js → UI_I18N_TABLES / MEAL_GROUPS
        → core.js → adapter.js → map.js → containers.js → list.js → detail.js
        → filters.js → overlays.js → TILE_CORS_FALLBACK_JS → folium's map script
        → <script>Adapter.start()</script>
```

`Adapter.start()` waits for folium's map (the same 200 × 50 ms poll the old
`initMap` used), calls `Business.boot(hooks)` (fetch `restaurants.json` →
`Business.init`), then `App.boot({leafletMap, mapEl, lang, seed})`, then
`Business.api.startSync()`.

## Who owns what

| file | owner | may be edited by |
|---|---|---|
| `shell.html`, `css/tokens.css`, `css/base.css`, `js/core.js`, `js/adapter.js`, `README.md`, `i18n/ui-strings.json` (structure) | architect | architect only — modules file a *coreRequest* |
| `js/business.js` | architect (generated) | nobody by hand — edit `map.py`'s `FILTER_JS_TEMPLATE` or the seam table in `audit_outputs/4.0.0-impl/tools/extract_business.py`, re-run it |
| `js/<m>.js` + `css/<m>.css` | the `<m>` module agent | that agent only |
| `i18n/ui-strings.json` (entries) | shared, append-only | any module agent adds its own keys; never edit another module's |

## business.js — the rules

`business.js` is produced by `audit_outputs/4.0.0-impl/tools/extract_business.py`
from explicit line ranges of `map.py`'s `FILTER_JS_TEMPLATE`, plus a table of
**seams** — literal edits, each of which must match exactly once. A seam only
ever (a) replaces a DOM access with `hooks.<name>(…)` or (b) turns a closure
variable into a parameter. It never changes what a function decides, stores
or sends. `PORT-PLAN.md §4` lists every seam with its reason. What it keeps
byte-for-byte: the OAuth/cookie auth, the sync engine (pull/push/409
merge/pendingWrite/reconcile), every `localStorage` key, the bookmarks store
and the `category:'meta'` sub-collections, import/export, share, the Android
bridge, `passesFilter`, the sort comparators, the saved grouping, the local
search matcher, the runtime CJK localizer, the emoji PNG swapper, the SW
update handshake and the install-hint state machine. All `window.__*` hooks
the old page exposed still come from here.

Modules **never** call `Business` directly. They call `App.act.*` and read
`Data.*` / `App.state`; `adapter.js` is the only file that knows `Business.api`.

## Placeholders `map.py` substitutes

Applied to the concatenated JS (same `.replace` chain the old template used)
and to `shell.html`:

| placeholder | value | used by |
|---|---|---|
| `__DEFAULT_OFF_GENRES__` | JSON list of foreign buckets (`map_data.DEFAULT_OFF_GENRES`) | business.js `FOREIGN_GENRES` |
| `__BOOKMARKS__` | `load_bookmarks()` — the repo-shipped seed | business.js `EMBEDDED_BOOKMARKS` |
| `__FAVORITES_BUILTIN__` | `load_favorites_builtin()` | business.js `EMBEDDED_FAVORITES_BUILTIN` → `Data.landmarks` |
| `__BUCKET_COLORS__` | `{key: hex}` from `PRICE_BUCKETS` | business.js `BUCKET_COLOR` |
| `__GENRE_EMOJI__` | `map_data.GENRE_EMOJI` | business.js `GENRE_EMOJI` |
| `__PREFS__` | 47 × `{ja, sc, tc, en, n}` (one line — stripped from the CJK scan) | business.js `PREFS` → `Data.config.REGIONS` |
| `__PRICE_BUCKETS__` | `[[key, label, lo, hi], …]` | business.js `PRICE_BUCKETS` |
| `__EMOJI_MANIFEST__` | `docs/emoji/_manifest.json` | business.js `EMOJI_MAP` |
| `__HAN_VARIANTS__` | variant → canonical char table (stripped from the scan) | business.js `HAN_VARIANTS` |
| `__KNOWN_LOCS__` | location stems (stripped from the scan) | business.js `KNOWN_LOCS` |
| `__GOOGLE_CLIENT_ID__` | `GOOGLE_CLIENT_ID` | business.js |
| `__HELP_COPY__` | `build_help_copy_json()` | reserved (filters help popovers) |
| *(no token)* | **new (4.0.0)** `map_data.MEAL_GROUPS` as JSON, one line, same direct assignment | `window.MEAL_GROUPS` → `Data.config.MEAL_GROUPS` |
| *(no token)* | **new (4.0.0)** `{en:{…}, ja:{…}, tw:{…}}` from `i18n/ui-strings.json`, tw via `to_trad()` — emitted directly as a one-line assignment, not substituted into a token | `window.UI_I18N_TABLES` → `I18N.register` |
| `__APP_VERSION__` / `__DATA_SCRAPED_AT__` / `__LATEST_SCRAPE__` | release, corpus baseline, latest row-level scrape date | `shell.html` `#build-meta` |
| `__TEXT_TRAD_MAP__` / `__TEXT_EN_MAP__` / `__TEXT_JA_MAP__` | **second pass** over the saved HTML: CJK runs actually on the page × `data/i18n/*.json` | business.js `TEXT_*_MAP` (the runtime localizer) |

## What the build refuses to ship

`map.py`'s emission is not a copy — it is the last place anything can be
checked before ten hand-edited files become one page that either runs or
doesn't. These are hard failures (the build stops, `docs/index.html` is not
rewritten):

- **A literal `</script>` in a `.js` file, or `</style>` in a `.css` file.**
  Inlined, that closes the tag in the HTML parser and the rest of the bundle
  becomes visible text. Write `'<\/script>'`.
- **A surviving `__PLACEHOLDER__`.** `var PREFS = __PREFS__;` is a syntax
  error that takes the whole bundle down with it. Tokens inside `//` or `/* */`
  comments are exempt, and so are the two sentinels that end the one-line
  `window.MEAL_GROUPS` / `window.UI_I18N_TABLES` literals.
- **A missing source file.** `read_ui()` names the path.

And these are warnings printed in the build log — visible, not fatal, because
six modules land in parallel and a half-written file should not block a build:

- `node --check` over every filled script (`ui js does not parse: …`).
- A key in `data/i18n/en.json` with no counterpart in `ja.json`, or the reverse.
- A key in `ui-strings.json` with an `en` but no `ja`, or the reverse. Such a
  key is **not** granted the punch-list exemption below — its runs fall back
  to the ordinary `data/i18n` check, so a half-translated key fails the gate
  rather than hiding behind it.
- Per-file byte counts for every CSS and JS file, so a module that doubles in
  size shows up in the log rather than only in the transfer size.

`scripts/verify_build.py`'s `ui-bundle` check re-asserts the structural half
of this against the built page: every `shell.html` slot is present, the load
order is business → core → adapter → the six modules → `Adapter.start()`,
all six modules register between the adapter and the boot call, no stranded
placeholder, and the UI i18n tables arrived with matching en/ja key sets.

## New emoji

`emojiImg()` falls back to `emojicdn.elk.sh` for any character the manifest
does not carry, so a new glyph costs every visitor a round-trip on first
render. `build_emoji_cache.py` scans this whole tree (`shell.html`, `css/*`,
`js/*`, `i18n/*.json`) along with `map.py`, the popups payloads and
`data/i18n`, so the fix is just to run it before the build:

```
uv run python src/tabelog/scrape/build_emoji_cache.py
flock /tmp/tabelog-build.lock uv run python src/tabelog/scrape/map.py
```

It never deletes a PNG (M-060) and, since 4.0.0, never drops an entry from
the manifest either: entries whose file is still on disk are carried forward
even when nothing on the page mentions that character any more. Note that the
collector's regex is deliberately loose and picks up typographic characters
(★ ✓ ✕ ❶) that have no Apple emoji and 404 at the CDN — those are reported as
failures and simply stay out of the manifest, which is correct: they render
in the system font.

## i18n — two mechanisms, one rule

1. **Module copy** goes through `t('简体 key', params)`. The table is
   `i18n/ui-strings.json` (`{key: {en, ja}}`); `map.py` generates `tw` with
   `to_trad()` and injects all three. Whole-sentence keys with `{n}`
   placeholders — never concatenate fragments. `map.py` excludes the CJK
   runs of these keys from the *missing EN/JA* punch list because this table
   translates them.
2. **Business strings** (sync status, toasts, error texts) are Simplified
   Chinese runs that the second-pass `TEXT_*_MAP` translates at runtime
   (`Business.localizeText`). `t()` falls back to it for any key the UI
   table lacks, so a module may pass a business string straight through.

**Every new Simplified string needs an `en` and a `ja`** — in
`ui-strings.json` if it is module copy, in `data/i18n/{en,ja}.json` if it is a
business string. `scripts/verify_build.py`'s `i18n` check fails the build on
a run that is untranslated today and was not in the 2026-09-05 baseline.
Japanese source text (restaurant names, addresses, genre tokens, list names
the user typed) must be rendered inside an element with `lang="ja"`.

Two things that surprise people:

- **The scan splits on punctuation.** `_CJK_RUN_RE` matches runs of Han
  characters only, so the key `导入按并集合并，同 ID 的条目保留当前版本；不提供导入撤销。`
  is four runs, not one. That is fine — registering the whole key in
  `ui-strings.json` with both `en` and `ja` exempts all four. Registering it
  with only one of the two exempts none of them.
- **A Chinese comment in a module is a run like any other.** The scan reads
  the rendered page, and an inlined `// 编辑 / 删除 / 隐藏景点` is on it. Either
  write the comment in English or expect the string on the punch list.

## State the adapter adds to `App.state` (read-only for modules)

| key | shape | source |
|---|---|---|
| `account` | `{signedIn, email, name, picture, message, messageColor}` | `Business.configured()`, `hooks.onAuthMessage` |
| `sync` | `{text, kind:''|'ok'|'busy'|'err', dirty, retryVisible, storageInfo, hintDismissed}` | `Business.api.syncStatus` |
| `install` | `{standalone, installed, canPrompt, ios, snackEligible}` | `Business.api.installState()` |
| `nearby` | `{active, planning:{region, sort, center, zoom}|null, pending, fix}` | `tabelog.listView` + `tabelog.lastLocation` |
| `notices.introSeen`, `notices.langChosen` | booleans | `tabelog.seenIntro`, `tabelog.lang` |
| `buildMeta` | `{appVersion, scrapedAt, latestScrape}` | `#build-meta` |
| `toast.action` | `{label, run}` (additive) | business toasts that carry a callback |
| `nativeSettings` | `{label}` when the Android shell offers a settings screen | `Business.nativeSettings` |

## Local preview

```
cd /mnt/d/Dropbox/proj_2026/tabelog
flock /tmp/tabelog-build.lock uv run python src/tabelog/scrape/map.py
uv run python scripts/verify_build.py
.venv-wsl/bin/python -m http.server 8901 --bind 127.0.0.1   # then /docs/index.html
```

## Integration-pass additions (2026-09-12)

New `Data` / `act` surface, all reached the way everything else is — a module
calls the adapter, never `Business`:

| added | why |
|---|---|
| `Data.detail(id, lang)` | the 原文 switch needs the Japanese slot 6; `detail.js` no longer fetches `popups-ja.json` itself |
| `Data.inJapan({lat,lng})` | the coverage test is Business's; `map.js` kept a duplicate bbox + west-edge table |
| `Adapter.liveFix` | the freshest `locationfound`; `Data.sort('distance')` prefers it, so the list and the map cannot disagree |
| `act.editPin(bm, {name, emoji, category})` | a pin could be created but never edited. **The id is never rewritten** — every `category:'meta'` member row references it |
| `validRef()` on `toggleFav` / `setBlack` / `batch` | a junk id used to go straight into `omakase_state_cache_v2` and from there into the KV blob |

`core.js`: `defaultState()` now declares `nearby`, `install`, `buildMeta`,
`nativeSettings` and `search.dropLoc` (the adapter seeds the first four, but a
module reading them before boot used to get `undefined`), and `act.showToast`
carries `action` and an explicit `remainingMs` through.

Three things every module author should know, because they were each a bug:

1. **`innerHTML` and `appendChild` drop the keyboard caret to `<body>`.**
   `containers.place()` and `containers.html()` now carry focus across a root
   move and a header rewrite, and `list.paintWindow()` does the same for a
   window repaint. If you add another host that rewrites markup under a
   focusable element, do the same.
2. **`nav.sync()` counts the history entries it has pushed**, not the change in
   stack length — a single change that closes one layer and opens another must
   still leave one entry per open layer, or the Back button stops working.
3. **Chinese in a `.js` comment is scanned as an untranslated UI string**,
   because the scan reads the rendered page. Write comments in English, or add
   the run to `data/i18n/{en,ja}.json`.
