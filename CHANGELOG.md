# Changelog

All notable changes to jpfoodmap.com. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the site is
versioned loosely — a release is a batch of milestones, not a semver API.

Entries reference the audit ids (`M-###`) from
`audit_outputs/integrated-2026-09-05/00-final/附录C-问题全文卡片.md`, which
carry the mechanism, the evidence and the red lines for each change.

> Milestone agents append to `[Unreleased]` — add to a section, don't rewrite
> someone else's line. The integrator cuts the version heading at the end.

## [Unreleased]

## [3.2.0] - 2026-09-09

The phone layout goes back to being map-first. 2.3.0 shipped an overlay
drawer over a full-bleed map; 3.1.x replaced it with a persistent text
bottom bar and a stack of separate controls, which cost map area and made
"where am I in this app" ambiguous. 3.2.0 restores 2.3.0's hierarchy model
and keeps every journey 3.1.x added on top of it (source-aware back,
scroll/focus restore, find-nearby's planning context, filter convergence,
toast serialization). The visuals move onto tokens and a single glass
control layer, and the basemap becomes CARTO Positron.

Plan and decisions: `audit_outputs/3.2.0-plan/00-总工程师计划.md` (§2 D1–D8),
spec `audit_outputs/3.2.0-plan/UIUX-SPEC-3.2.0.md`, evidence under
`audit_outputs/3.2.0-plan/{ux-phone,ux-desktop,groundwork-4.0,design-research}/REPORT.md`.
Per-milestone logs, measurements and screenshots:
`audit_outputs/3.2.0-impl/M-3.2-01.md` … `M-3.2-R1.md`; the side-by-side
design gallery is `audit_outputs/3.2.0-impl/review/index.html` and the
reviewer entry point is `audit_outputs/3.2.0-impl/index.html`.

### Changed

- **Phone hierarchy: one map, one overlay drawer (M-3.2-02).** The map is
  full-bleed again; Results / Saved / Filters open as an overlay drawer over
  it instead of a route change. `PHONE_DRAWER_HTML` was rewritten and the
  whole `--phone-nav-h` family of "lift everything above the bar" rules is
  gone with it.
- **A floating segmented pill replaces `#ff-fab` and the hamburger
  (M-3.2-03).** `#wb-seg` sits bottom-left with three segments (结果 /
  收藏 / 筛选), each carrying its own count, and it stays clear of the FAB
  column via the live `--fab-w` published by `syncSheetOffset()`.
- **Search capsule back to its 2.3.0 width, plus a chip row (M-3.2-04).**
  The 3.1.x full-width phone override is gone: 402 → 386px capsule with a
  312px input (it was ≈129px in 3.1.2). The floating `#ux-context` bar is
  replaced by `#ss-chips` — area / nearby / back-to-plan — reusing the
  existing `#ux-region` / `#ux-nearby` / `#ux-restore-plan` ids and their
  handlers.
- **Find-nearby is the locate FAB (M-3.2-05).** One control instead of two;
  the `window.confirm()` that used to gate it became an undoable toast, and
  a fix outside Japan now rolls the map back to the plan instead of flying
  off the corpus.
- **Detail card: a 44px head row and one glass footer (M-3.2-06).** The
  source-return link moved into `#bs-head`; `#ux-detail-actions` and the
  in-body `.rst-actions-bar` were replaced by a single sticky `#bs-foot`
  that the desktop detail column shares.
- **One CTA in the filter footer, one line in the Saved header
  (M-3.2-07).** `#ux-filter-done` lost its explanatory paragraph, and
  `#ux-saved-scope` is gone — `#fv-sum` says it once.
- **Desktop columns tidied and a glass top bar over a full-bleed map
  (M-3.2-09).** The map now runs under `#wb-top` on mid/wide; the header
  carries its glass on `#wb-top::before`. The left column head reads as two
  counts (符合筛选 / 屏幕内), the detail column gets the shared footer.
- **Shorter copy, whole-sentence i18n keys (M-3.2-08).** `localizeText()`
  normalizes punctuation per language, so no full-width `，` / `；` reaches
  the EN/JA build output. Long strings became whole-sentence keys instead
  of fragments concatenated around Chinese punctuation.
- **Visual tokens and one glass layer (M-3.2-01, R1).** CSS cascade layers,
  named z-index tokens, radius/transition/elevation tokens, and a single
  breakpoint set (560 / 750 / 1280, with 319 / 360 / 900 as exceptions)
  enforced by `verify_build.py`'s `breakpoints` check. Glass
  (`backdrop-filter`) is applied only to controls floating over the map,
  falls back to opaque white under `@supports not` and
  `prefers-reduced-transparency`, and is switched off during map movement
  via `body.map-moving`.
- **Basemap: CARTO Voyager → Positron, @2x on high-DPR screens
  (M-3.2-12).** `carto_tile_url` in `map.py` `main()` now points at
  `rastertiles/light_all` and carries `{r}` after `{y}`, so Leaflet requests
  `@2x` wherever `devicePixelRatio > 1`. Positron's quieter ground lifts the
  contrast of markers and of the glass control layer. The service-worker
  tile cache keeps its name (`tabelog-tiles-v2`) and its 400-entry LRU; the
  old Voyager tiles age out on their own. Budget for that cache is now
  ≈13 MB at 1x / ≈40 MB at @2x. Side-by-side:
  `audit_outputs/3.2.0-impl/review/basemap-compare.png`.
- **Android shell 3.2.0 / `versionCode 30200`.** No native change — the
  shell loads the live page; the bump only keeps the APK label in step
  (`android/CHANGELOG-ANDROID.md`).

### Fixed

- **Glass on a container broke a fixed descendant (M-3.2-09).**
  `backdrop-filter` makes an element the containing block for its
  `position: fixed` descendants, and `wbRelocate()` moves `#ss-box`
  (with `#ss-list`) into `#wb-top`: the search dropdown collapsed from
  320×656 to 320×1 on both engines. The filter moved to `#wb-top::before`.
- **A stale toast could sit on top of the FAB column for ~20 s
  (M-3.2-10).** Toasts are serialized through one queue; a plain
  informational toast never preempts one that carries an action, and the
  action toasts themselves are last-writer-wins so an "undo" always points
  at the step the user just took.
- **`window.confirm()` on find-nearby (M-3.2-05).** Replaced by an
  action toast with 撤销, which is also what the geolocation-failure and
  out-of-Japan paths now use.
- **The signed-out "this device only" hint is one-shot (M-3.2-10).** It
  reuses the existing `tabelog.syncHintDismissed` key; no new key.
- **Focus ring no longer lands on a `<select>` when a panel opens
  (M-3.2-10)**, and `focusFirstIn()` skips form fields.
- **Photo placeholder no longer leaves a blank band (M-3.2-06/R1)** — a
  failed `<img>` used to hide the whole button while keeping its box.
- **`#bs-foot`'s border and shadow were being eaten by `.glass-reg`
  (M-3.2-09)** — restored through the `overrides` layer.

### Removed

- The phone text bottom bar (`#phone-nav`) and every rule that offset the
  FAB stack, the attribution, `#sync-stack`, `#ss-list` and `#bs-sheet`
  above it.
- `#ff-fab` (and its `.needs-sync` breathing animation), the hamburger
  drawer button, the floating `#ux-context` bar and `#ux-context-note`,
  `#ux-detail-actions`, `.rst-actions-bar` (with `gmapsQ` / `gmapsUrl` /
  `gmapsBtn` / `shareBtn`), `#ux-saved-scope`, and the filter footer's
  explanatory paragraph.
- Six i18n keys that no longer appear anywhere on the page. The other
  unused keys were deliberately left in place so the untranslated-run
  baseline does not move.

### Tests

- `tests/ux/visibility.py` is new: the map-visibility, truncation and
  full-width-punctuation probes from the audit script
  `audit_outputs/3.2.0-plan/ux-phone/run_flows.py` became a standing gate.
  **Do not run `run_flows.py`** — its selectors still point at the deleted
  `#phone-nav`; `tests/ux/README.md` says so.
- `tests/smoke_playwright.py` runs the iPhone 402×874 rows on WebKit
  (`"browser": "webkit"` in `VIEWPORTS`); Fold and desktop rows stay on
  Chromium.
- `scripts/verify_build.py` keeps 11 checks / 17 assertions; the
  untranslated baseline is unchanged at EN 477 / JA 477.
- `tests/sync` `H1` / `I4` / `I5b` were traced to probes reading the
  deleted `#ff-fab .needs-sync`, not to a regression; `G3` was given an
  8 s deadline poll instead of a fixed sleep.

### Known gaps — carried to 4.0.0

Collected from the milestone logs' 遗留 sections; none of these blocks the
release, and each names where the decision lives.

- **Cascade layers are declared but mostly empty.** `reset` / `base` /
  `layout` / `surfaces` / `overrides` hold little or nothing, and the
  `vendor` layer is empty because putting the four Leaflet `<link>`s into it
  needs `@import url(...) layer(vendor)`, which changes load ordering and
  bypasses the preload scanner. Move one block at a time when CSS is
  externalized in 4.0.0.
- **`#sync-stack` was not lowered below the FAB layer** — it makes room
  horizontally (`padding-right: calc(12px + var(--fab-w))`) instead,
  because lowering it would also put toasts under the detail card. Lowering
  it properly means moving the toast queue out of `#sync-stack` first.
- **700–749px now gets the full-width sheet** rather than 3.1.x's centred
  card. That is a direct consequence of the unified breakpoint set; keeping
  the centred card would need a new `min-width: 700` breakpoint, which the
  `breakpoints` check would reject.
- **`prefers-reduced-transparency` and the look of the glass are unverified
  on hardware.** Playwright emulates neither, and WebKit's software
  renderer does not composite `backdrop-filter` at all. On the device
  checklist.
- **`html`/`body` still have `overflow-x: visible`**; the off-canvas
  `#wb-detail` is clipped by the inner shell.
- **402px detail state shows 19.9% of the map** vs 23.4% in 2.3.0 — inside
  the 18% floor, still 3.5pp short.
- **Two Chinese words for the same act on different screens**: the detail
  card's ⋯ menu says 隐藏这间店 while the Saved page's bulk action says
  弃用. Unifying the Chinese would drag JA to 非表示 and break the M-106
  four-word table, so it stays a known inconsistency.
- **Back does not consume a level for a toast, and a `?r=` deep link's
  first Back does not leave the site** — both are pre-existing behaviors
  that conflict with the wording in SPEC §E5; the spec text needs the edit,
  not the code.
- **Token coverage is partial**: roughly 30 filter-panel radii, ~12
  transitions, `#ff-region`'s 6px radius, `#wb-left-collapse`'s 8px and
  `#layers-pop`'s 14px font are still literals.
- **`l10nPunct`'s EN branch also converts `（）` to ASCII parentheses** for
  every string that passes through `localizeText`. No side effect was found
  across 200 sampled records, but it is a global behavior change.
- **The 628-entry `TEXT_TRAD_MAP` has not had a full s2twp over-conversion
  audit** — only the strings touched this release were checked.
- **EN/JA typography at 402px was not measured end to end** — only zh-CN
  runs the layout gate; the full-width-punctuation scan does cover all
  three languages.

## [3.1.2] - 2026-09-08

Website-only sync hotfix. The Android shell stays at `versionName 3.1.1` /
`versionCode 30101` — it loads the live page, so this ships to it without a
new APK. Findings and repro in
`audit_outputs/3.2.0-plan/backend/REPORT.md` (§3 P0-1, §4 P1-1/P1-2/P1-3,
§5 P2-1); the P0 repro is `audit_outputs/3.2.0-plan/backend/repro/wedge.mjs`.

### Fixed

- **Sync could stop for good on one device after two failed PUTs (P0-1).**
  `tabelog.pendingWrite` records a write whose outcome is unknown, and
  `push()` refuses to send anything while such a record exists. Clearing it
  required `retryReady`, which demanded `navigator.locks` **and**
  `attempts < 2`. So a browser with no Web Locks (Safari < 15.4, Firefox
  < 96, any non-secure context) wedged on its first uncertain write, and
  every other browser wedged on its second: the record persisted across
  reloads and sign-outs, and this device never sent another PUT. Local data
  was intact, but nothing reached the cloud — one uninstall or cleared
  profile later it was gone. The fix gives the record a **determination**:
  when the server returns exactly the base the write was built on (same `v`,
  same write id, same content) on three consecutive reads spanning the dwell
  window, the write never landed. The record is dropped and the *current*
  state goes up under a fresh write id — replaying an old body can duplicate
  a write that did land, discarding one proven not to have landed cannot
  lose anything. If the determination is wrong anyway (a stale KV read held
  the old version past the window), the replacement push 409s and the merge
  still receives the original body, so post-send edits are preserved exactly
  as the replay preserved them. `navigator.locks` is no longer required, and
  the hard cap of two attempts became a bounded 75 s / 150 s / 300 s dwell —
  slower under sustained failure, never stopped.
- **Manual escape valve.** The settings sync row gained a 立即重试 /
  Retry now / 今すぐ再試行 button, shown only while the engine is waiting on
  the cloud or holding an uncertain write. It clears the backoff and
  resolves the pending record on the next readback instead of waiting out
  the dwell window.
- **A rolled-back server left the device silent forever (P1-2).** Refusing
  to rebase onto a lower version was correct, but there was no way out: one
  restored KV backup (or a long run of stale cross-PoP reads) and the device
  stayed on "等待云端状态更新" and never pushed again. The same older blob
  seen three times across two minutes is now accepted as a real rollback and
  merged as a union — it can resurrect a deletion, it can never drop
  anything this device holds.
- **Import rejected the whole backup over one bad entry (P1-1).** A file was
  refused outright if any favorite URL failed to parse or any bookmark
  failed validation — including id-less pins, which pre-2.0 exports really
  contain. Only structural problems (not an object, over 2 MiB, none of the
  five known keys, a known key that is not an array) refuse a file now; a
  single unreadable entry is skipped and counted, the count is shown before
  the user confirms and again in the result, and an id-less entry that
  carries valid coordinates is given a generated `bm-` id instead of being
  discarded.

### Changed

- `scripts/verify_build.py`'s localStorage key list gained
  `tabelog.pendingWrite`, replaced the long-dead `tabelog.showTransit` with
  the two keys that actually exist (`tabelog.showTransitLong` /
  `tabelog.showTransitCity`), and now matches the quoted literal — the
  substring match made the dead key a permanently-green assertion (P2-1).
- `tests/reliability/pending-tabs.mjs` R3 no longer recovers by calling
  `__reliability.resetWait()`, a probe the page never invokes on that path;
  it asserts the page recovers on its own, and the probe is gone from
  `browser.mjs`. New R5 covers the one combination the suite never had —
  frozen server plus PUTs that keep failing — and asserts a new edit still
  uploads 20 minutes later (P1-3). New cases cover the rollback exit and the
  manual retry button; new compat fixture `09_export_idless_pin.json` covers
  an id-less legacy pin plus an unreadable favorite URL.

## [3.1.1] - 2026-09-08

### Changed

- On phone layouts in the website and Android app, area selection and nearby
  search are separate rounded buttons between the search field and avatar.
  Their previous row is hidden when no nearby context or planning-return
  message is needed. Desktop layout and existing actions are retained.
- Phone buttons show the selected region name without an action prefix;
  English uses the compact labels Region and Nearby before selection.

## [3.1.0] - 2026-09-08

Website and Android release. Existing features and data are retained; no
Widget is included.

### Added

- Phone layouts have a persistent text bottom bar for Map, Results, Saved,
  and Filters. Area selection, nearby search, and return to the previous
  planned area are explicit, while the existing filters and data remain
  available.
- `tabelog.pendingWrite` records the account, write id, exact body, merge
  base, and bounded recovery counters before a PUT. An unknown result is
  reconciled by readback across refreshes without generating a new write id;
  ambiguous changes remain local and dirty.
- Android JSON backup and restore use the system Storage Access Framework
  through an origin- and main-frame-checked bridge. The page still owns the
  schema and validates files; native transfer is capped at 2 MiB.

### Changed

- The phone detail flow now keeps its source (Map, Results, or Saved), gives
  it an explicit return action, and keeps Save and map navigation available
  as primary actions. Reservation copy describes whether a source link was
  detected and tells users to confirm date, party size, and availability on
  that source.
- System Back returns from a restaurant to its original list, then to the
  map. Closing or reopening details does not leave extra history entries.
- The About sheet separates the historical corpus baseline from the newest
  valid row-level timestamp produced by recent partial scraping. Restaurants
  without row timestamps are not presented as freshly updated.
- Sync measures JSON as UTF-8 before the Worker's 200,000-byte limit, blocks
  an unchanged body after 413, respects bounded `Retry-After`, and retains a
  cached signed-in identity through temporary network failure. Lower-version
  GET/409 responses no longer lower the local merge base; versionless and
  same-version/different-write-id compatibility remain supported.
- Edits made after an uncertain upload survive recovery, including cancelling
  a newly saved restaurant after another device has received it.
- Android notification readiness includes the state of the actual channel
  and links to its channel settings. The notification model remains one
  user-enabled local channel; this release adds no server push or background
  polling.
- The Android power audit now compares numeric per-UID activity counters and
  preserves their units. Missing or empty samples report SKIP instead of a
  false PASS; the result is an activity check, not a physical energy or heat
  measurement.

### Fixed

- Import validates the full candidate and all known field types before it
  mutates memory or storage, preserves compatible unknown fields, and rolls
  back a failed commit. There is no import undo claim.
- Popup data, transit data, sign-in/session calls, and response body reads
  now have bounded deadlines and release failed request state so a later
  attempt can retry.
- Phone and folding-width layouts account for the text bottom bar, safe-area
  insets, visible keyboard height, scrollable dialogs, large text, and
  landscape. Detail, filter, and import actions remain reachable in narrow
  viewports.
- Android export reports success only after the chosen JSON document is
  written; cancellation and failure do not produce a false saved state.
  Import authorization is single-use and accepts only the selected content
  URI without enabling general file access.

## [2.3.0] - 2026-09-07

UI release answering the 2.2.0 bug report
(`audit_outputs/2.2.0_BUG_REPORT/`). Filtering moves into the left column,
the restaurant card loses its half-open step, and the narrow layouts stop
overflowing.

### Changed

- **W-1** — Cluster bubbles go back to blue (soft, high-transparency
  `rgba(59,130,246,…)` with a `#1e3a8a` count). The neutral blue-grey read as
  "unavailable / no price". Map and the 图例 swatch move together.
- **W-2 / W-3 / W-7** — 筛选 is now the third tab in the left column, styled
  exactly like 结果 and 收藏, on phone, mid and wide alike. The bottom sheet
  and the top-bar popover no longer host the filter panel; the 筛选 FAB and
  the top-bar 筛选 button both just switch to that tab.
- **W-4** — The collapsed rail shows both numbers: 符合筛选 and 屏幕内.
- **W-5** — Above 500 matches the card's ↑↓ stepper greys out
  (`aria-disabled`, still clickable) and a tap explains that the filters need
  narrowing first.
- **W-6** — The left column can be collapsed in mid as well as wide.
- **W-8** — The card's half-open "上滑查看详情" state is gone: it opens full
  height, and a swipe / Escape / map tap closes it in one step. On a phone,
  closing the card pans the map back to the restaurant that was open.
- **W-9** — iOS width overflow in the phone drawer: the count and summary
  rows follow the container width instead of a baked-in 319px.
- **A-1** — Mid layout: the search pill flexes and the buttons beside it no
  longer get pushed off; at ≥900px the brand name and full-size logo return.
- **A-2** — `WB_BP_SPLIT` and `WB_BP_MID` are both 750px, so the split layout
  is unreachable and a Fold inner screen at 60% width gets the phone design.
- **Wording** — 命中 / 视野内 → 符合筛选 / 在屏幕范围内; the 聚合圆圈 legend
  now says the cluster breaks apart as you zoom in; the 景点 layer row counts
  the user's own landmarks beside the built-in ones.

## [2.2.0] - 2026-09-07

Performance release. Nothing visible changed; the map should pan the way it
did in 1.x again, and a phone should stop warming up while you drag it.
Diagnosis and numbers: `audit_outputs/2.2.0-perf/DIAGNOSIS.md`; the plan the
fixes follow: `audit_outputs/2.2.0-perf/PLAN.md`.

### Performance

The idle page was already free (0.0 ms of script/layout/style over 15 s on
both 1.x and 2.1.0), so the heat was not a stray timer. It was per-frame work
while panning, plus Service Worker tile churn.

- **Scale control (P1).** `L.control.scale` without `updateWhenIdle` subscribes
  to `move`, so every drag frame rewrote the control's DOM and turned a
  compositor-only pan into Layout + Paint on the main thread. It now refreshes
  on `moveend`. Desktop, Tokyo z12, 10 drags: LayoutCount 410 → 32 (1.x: 25).
  This one line recovers most of the regression on its own.
- **Results list (P2).** A pure pan no longer rebuilds the 30-row window (568
  nodes + 30 `<img>` per moveend). `wbListRender` computes a window signature
  from row identity and the ★ / ✕ / checked / cursor bits and returns early
  when it matches; a star toggled from the card still repaints the row.
  Review caught that hoisting the `scrollTop` read above the spacer write let
  a shrinking filter leave the list on a stale page with a RangeError; the
  read stays after the write, with `first`/`count` clamped.
- **Full-corpus scan (P3).** `recompute()` on moveend recounts only the
  viewport; the 9.8k-row pass that produces the "N 家符合标准" total runs when
  filters, Saved, Hidden or a sub-collection focus change, behind a
  `fullPassStale()` fuse keyed on the set sizes so a write that bypasses
  `apply()` still triggers it.
- **Counts (P4) and i18n observer (P6).** Count sentences are written only
  when a number changes (textContent, not innerHTML); the `#wb-left`
  MutationObserver that re-localised every rebuilt row is narrowed to
  `#wb-detail`.
- **Result** (desktop 1440×900, medians of 4 alternating 1.x/2.2.0 pairs):
  TaskDuration 1.07× 1.x, ScriptDuration 1.13× (2.1.0 sat at ~1.5×);
  per-moveend main thread 6.75 → 1.42 ms (1.x 1.08); Fold cover at 4× CPU
  9.59 → 5.48 ms (1.x 4.75); idle 15 s stays 0.0 ms.
- **Service Worker tiles (P5).** Tiles were stale-while-revalidate, so every
  tile was fetched twice, and they were stored as opaque responses, which
  Cache Storage bills at 4–7 MB each: 70 tiles ≈ 300–520 MB, a 200 MB phone
  quota was full on first paint and the data cache (`tabelog-data-v1`) got
  evicted and re-downloaded. Now the tile layer requests with
  `crossOrigin: "anonymous"`, the SW serves tiles cache-first, stores only
  non-opaque responses (review: storing opaque ones under the new 400-entry
  cap would have made the quota bomb 5.7× bigger for a tab still on the old
  page during a deploy), caps the bucket at 400 entries ≈ 13 MB and moves it
  to `tabelog-tiles-v2` so `activate` reclaims the old one. A page-side guard
  drops crossOrigin and redraws once if 6 consecutive tiles fail while
  online, for a CDN edge that omits ACAO. Desktop, 20 drags: Cache Storage
  growth +186 MB → +1 MB; 200 MB quota 100.7% → 8.9%; tile requests with the
  SW on/off 104/72 → 112/112. Offline panning over seen tiles still works.
  The three SW rules (atomic install, no `skipWaiting` in install, activate
  only after the new shell holds `./`) are untouched.

### Fixed

- **Android:** the system Back key closes the top layer (card, lightbox,
  drawer, filter sheet) before leaving the app; a cold `?r=` deep link still
  exits on the first Back, matching Chrome. `verify-flows.sh`'s back segment
  now carries a probe-free hard assertion (`versionCode 20200`).

### Data

- **Tokyo top-up.** `scrape_topup --tokyo` walks Tabelog's filtered list (RC
  category, dinner ¥3,000–¥20,000) from page 10 because the plain list's
  60-page cap was exhausted. 443 main-meal restaurants added (Tokyo 936 →
  1,379; published corpus 9,807 → 10,250), geocoded, 442 of 443
  Google-verified. `google_enrich --tokyo` added, and the script now refuses
  to spend API calls on rows that have no GSI point yet. `tests/compat` reads
  the corpus size from the build instead of pinning 9,807.

### Deferred to 2.3.0 (found in review, not shipped)

- `verify-flows.sh` detects its diagnostics source before the app has ever
  been READY, so a default run still SKIPs the back segment
  (`JPFM_DIAG_SOURCE=native` works).
- A `tests/perf` regression gate; `p2p4check.py` / `shrink_rows_ok.py` under
  `audit_outputs/2.2.0-fix` are worth promoting into `tests/`.
- `wbSetLeft()` is dead; `#lang-gate` should be excluded from `localizeTree`;
  a superseded `gotoRestaurant()` flight leaves its 3 s timer idling.

## [2.1.0] - 2026-09-07

The first release driven by the owner's own bug report on 2.0.0
(`audit_outputs/2.0.0_BUG_REPORT/`), verified line-by-line in
`audit_outputs/2.1.0-verify/` and specified in
`audit_outputs/2.1.0-fix/PLAN.md`. Twelve web issues (W-1 … W-12) and three
app issues (A-1 … A-3): the wide layout stops spending three quarters of a
1440px window on two columns, the detail card's photos stop being cropped to
a sliver, picking a restaurant actually flies the map to it, and the Android
shell ships with a status bar the top bar no longer hides under. It also
carries the Android app, which was written after 2.0.0 was cut and had been
sitting in `[Unreleased]`.

Nothing here changes a localStorage key name, the KV blob shape, the Worker
API, a built-in landmark id, or the 12-slot popup array. Exactly one new
localStorage key exists in the whole release (`tabelog.oovHintDismissed`,
W-12a); every other new piece of remembered state rides in the existing
`tabelog.listView` object as an additive field.

### Added

- **An Android app.** `android/` is a Kotlin WebView shell around
  jpfoodmap.com, sideloaded as `android/apk/jpfoodmap.apk` and version-locked
  to the site (2.1.0 / `versionCode 20100`, `minSdk 31`, `targetSdk 36`,
  built for the Galaxy Z Fold 8). It exists for the five things a browser
  tab cannot do on a folding phone: survive a fold/unfold without reloading
  the document, take `jpfoodmap.com` links through App Links, sign in with
  Google (which refuses web sign-in inside a WebView), open an off-site link
  in a Custom Tab that Back returns from, and raise the system share sheet.
  The shell never reads or writes anything the page owns — no localStorage,
  no favourites, no language, no map position — so a phone and a desktop
  hold exactly the same state through the same Worker. Its own history is
  `android/CHANGELOG-ANDROID.md`; install and setup are `android/README.md`.
- `docs/.well-known/assetlinks.json` — App Links verification for
  `com.fredhli.jpfoodmap` (and the `.debug` variant), both under the
  sideloading certificate's fingerprint. Deployed with the site; it makes a
  tapped jpfoodmap link open in the app instead of a browser tab.
- One guarded `// ===== APP BRIDGE (Android shell) =====` block in
  `src/tabelog/scrape/map.py`. Inside the app it hides the PWA install
  prompts (the shell *is* the install), swaps the GIS sign-in button for a
  native one, routes sharing through the system sheet, adds an "App
  settings" row to the avatar menu, and clears the native credential on
  sign-out. In a browser it defines two side-effect-free globals
  (`__jpfmBootId`, `__jpfmOpenShare`) and returns on its third line. No new
  localStorage key, no change to the KV blob shape, the Worker API or the
  built-in landmark ids.
- **A photo lightbox inside the detail card** (W-4). A thumbnail is now a
  `<button>`, not an `<a target=_blank>`: tapping it opens the 640px image
  full-screen over everything (`#ph-lb`, `object-fit: contain`), and the
  overlay registers with the same `uiRegister`/`uiPush` stack every other
  layer uses, so the system Back button closes the photo and leaves the card
  open. Inside the Android shell this replaces a jump out to a Custom Tab.
  Pinch-zoom works because the map's `gesturestart` interception is bound to
  `.leaflet-container`, which the overlay covers.
- **A language chooser on first visit below 700px** (W-9). When
  `tabelog.lang` has never been written and the layout is phone or split,
  `#lang-gate` asks once, in four languages at once, with the four buttons
  labelled in their own language; picking one calls the existing
  `setLanguage()` and nothing else. `navigator.language` only decides which
  button gets a hint border — it never reaches `activeLang` and is never
  stored. The intro bar waits for that answer instead of competing with it.
- **A language button in the wide/mid top bar** (W-9). `#wb-lang` sits left
  of `#wb-sync` and opens a four-row popover; in mid mode only the 🌐
  remains. The four labels are written by JS in each language's own name, so
  the runtime localizer cannot translate "简体" into "Simplified". The
  account menu's language row moved up from below "delete my cloud data" to
  just above "reset filters" — it used to sit 657px down a menu.
- **A left drawer on phones** (W-6). The 🔍 at the head of the search capsule
  becomes a 44px ≡ below 520px and opens `#wb-left` as a left-hand drawer
  (`min(86vw, 380px)`, full height) with three tabs: results / Saved /
  filters. "Filters" opens the existing `#ff-sheet` *on top of* the drawer
  rather than instead of it, so Back closes the filters first and the drawer
  second. The drawer, its backdrop, its focus trap and its Back registration
  are the 2.0.0 ones, unchanged.
- **A collapsible left column in wide mode** (W-3). A "收起列表" button at the
  right end of `.wb-tabs` slides the result column out and brings up the icon
  rail (☰ + the hit count) that mid mode already had; ☰ brings it back. The
  state persists as an additive `leftCollapsed` boolean inside
  `tabelog.listView` — no new key, and anything that is not exactly `true`
  means "expanded".
- **An "其它" section in the filter panel** (W-11) collecting the five
  standalone switches (Tabelog booking / Saved / Hidden / non-Japanese
  cuisine / Google-verified coordinates) that used to be five separate
  one-row sections. All five `input` ids are unchanged —
  `#ff-bookable-only`, `#ff-only-fav`, `#ff-hide-black`, `#ff-hide-foreign`,
  `#ff-gcal-only` — because `tabelog.filterState` and every deployed page
  address them by id.
- **A dismissable "not in the current view" hint** (W-12a). The card now
  carries a × (this page load only) and a "不再提示" that writes
  `tabelog.oovHintDismissed = '1'`; either way it appears at most once per
  page load. The read is `try/catch` and anything but `'1'` counts as unset.
  The `matched === 0` branch is deliberately not latched — that one is the
  answer to "why is the map empty", and it has to keep answering.
- `desktop-mid` (1000×800) in `tests/smoke_playwright.py`, plus four new
  checks — `lang`, `chrome`, `workbench`, `filter-copy`, `goto-zoom` — and a
  rewritten `fav-drawer`. Six viewports × twelve checks.

### Changed

- **The wide layout stops paying for a column nobody is looking at** (W-2).
  `#wb-detail` in wide mode now behaves the way mid mode already did: it sits
  outside the viewport until a restaurant is selected and slides back out
  when the card is closed, so an unselected 1440×900 window gives the map
  1096px instead of 712px. The card's × is the collapse control; every inset
  change still goes through `wbSyncVars()` → `wbScheduleInvalidate()` and
  nothing writes `--wb-right` directly.
- **`WB_BP_WIDE` 1100 → 1280.** At 1100px the old wide mode left the map
  372px — narrower than the mid layout it replaced, so widening the window by
  one pixel shrank the map by 407px. 1100–1279 is now mid.
- The detail column gets its top margin back (W-1): `body.wb-mid #bs-content`
  gets `padding-top: 14px`, `body.wb-wide` 18px. The `#bs-content` padding
  shorthand is untouched — on phones the sheet's grip provides that space and
  a top padding there would be wrong.
- The mid rail is down to ☰ and the hit count (W-3): the rail's search and
  filter buttons are gone, both of which duplicated a control the top bar
  already has. Their `data/i18n` entries stay as orphans.
- **Detail-card photos are 4:3 and no longer cropped** (W-4). The root cause
  was H11's `width="320" height="320"` presentational hints: `width: 100%`
  overrode one of them and nothing overrode the other, so `aspect-ratio`
  never applied and each photo was a 320px-tall column clipped to about 11%
  of itself. `height: auto` restores it. Tabelog's `320x320_square_` token
  became `320x320_rect_` (`640x640_rect_` on high-dpr or wide screens) so the
  server stops square-cropping too, and a failed image now goes
  `visibility: hidden` instead of `display: none`, which used to collapse the
  three-column grid.
- **Picking a restaurant flies the map to it** (W-5). `gotoRestaurant(d,
  {zoom, peek, animate})`, factored out of `ssGotoRestaurant()`, is now the
  one path used by the result rows, Enter on a result row, the Saved tab, the
  card's ↑↓, the search box and `?r=` (cold start and the app's hot deep
  link). Target zoom is `Math.max(getZoom(), 17)` — `GOTO_ZOOM`, the same
  number as MarkerCluster's `disableClusteringAtZoom`, so the marker is
  always uncluttered on arrival. Clicking a marker flies only when the map is
  below zoom 15 (`MARKER_MIN_ZOOM`), which keeps the surrounding area
  visible. The order is fly, then open the card — never the reverse. The
  `?r=` path also picked up the `ensureMarker` / `pinnedRow` handling it had
  been missing.
- **Cluster bubbles are misty blue** (W-8): outer
  `rgba(91,119,153,0.11)`, inner `rgba(91,119,153,0.28)`, `#172033` at weight
  700 — legible against both the light basemap and the marker colours,
  where the old translucent `#2563eb` read as a second kind of marker. The
  legend's sample circle changed with it, in the same values.
- **The filter panel's row height is one variable** (W-11).
  `#ff-sheet-content` carries `--ff-row: 34px; --ff-head: 30px`, and
  `@media (pointer: coarse)` raises them to 44/40 — so a mouse gets a compact
  panel and a finger gets the 44px target it needs, from one declaration
  instead of four scattered overrides. Option text is 13px everywhere; the
  11px inline size on the cuisine grid and the 12px on award rows are gone.
- **The counts read as sentences** (W-12b). "命中 7626 视野内 631" in the
  left column header, the filter panel header and the FAB's aria-label is now
  "筛选后 7626 家餐厅符合标准 · 其中屏幕内 631 家"; the Saved tab says "你的
  收藏夹共 N 家餐厅 · 不受筛选影响"; the workbench footer says "其中 N 家能在
  Tabelog 上订座" over a "只看这 N 家" button. Every one of those is a
  per-language whole-sentence template (`COUNT_TPL` / `FAV_TPL` / `FOOT_TPL`
  / `FOOT_BTN_TPL`, the `LOCATE_STRINGS` pattern) — a sentence with a number
  in it can never go through the CJK-run translation table, which is what
  M-103 was. The phone's `#ff-fab` pill and the rail keep the short labels;
  they have 44px to work with, not a line.
- The legend explains the cluster number ("圆里的数字是这一片符合当前筛选条件
  的餐厅数量") and the marker colour ("标记颜色取决于这家店申报的晚餐价格上限",
  plus the lunch fallback that `price_bucket()` really does apply), and the
  sign-in panel says "不登录也完全可用" (W-12b ④⑤⑥).
- The empty-map card names the number it is talking about — "当前筛选的 N 家
  餐厅都不在地图范围内" over "缩放回全部结果" — and dropped the subtitle that
  restated the title (W-12b ⑦⑧).
- **Uncalibrated coordinates say what to do about it** (W-12b ③). A row whose
  address only geocoded to block level now shows, in red and bold, "餐厅的
  地址无法被 Google 地图校准，请确认好餐厅的具体位置再前往！"; a row that is
  merely not Google-verified gets a grey line. Both are visible in the
  half-open `bs-peek` state, which is exactly the screen someone is looking
  at when they are about to walk somewhere. The four languages are written
  out literally per `_lang` (a sentence containing "Google" cannot go through
  the run table) and stripped from the build-time CJK scan the way
  `PREFS` already is.
- **The bottom-right FABs line up with the filter pill on every phone**
  (W-7). The `(orientation: portrait) and (max-width: 699px) and
  (max-height: 795px)` rule with its `max(56px, var(--sheet-h))` floor is
  deleted: it existed because five stacked buttons needed 252px, and A11 moved
  four of those into the layers popover. The landscape `max-height: 560px`
  rule is untouched.
- **The top overlays share one inset** (W-10). `--chrome-inset` is 12px, 8px
  below 480px, and both `#ss-box` and `#intro-bar` read it; below 480px the
  intro bar's right edge clears the avatar and lands on the search input's
  edge. `#ff-fab` hides its labels below 360px instead of 420px, so 393 /
  430 / 475px phones all look the same. The intro bar's × is a 44px target.
  The scale bar is clear of the pills again, because the ⭐ pill that used to
  sit on it is gone.
- **The top bar respects the status bar** (A-1). `--wb-top` is now
  `calc(var(--wb-top-h) + max(env(safe-area-inset-top, 0px),
  var(--app-inset-top, 0px)))`, `#wb-top` pads by the same `max()`, and the
  phone search capsule's `--chrome-top` follows suit. On a desktop browser
  both terms are 0 and every measured offset is byte-for-byte what 2.0.0
  produced. The Android shell writes the second term from its own inset
  listener (see below), so the bar is correct even if `env()` reports 0 —
  and because it is a `max()`, a device where both are right does not get
  double padding.
- **The launcher icon is inset** (A-2). `gen-launcher-icon.py` grew
  `FG_SCALE = 0.76` and renders the foreground centred on a plate of the
  source image's own corner colour, so the mask no longer eats the artwork;
  the splash screen keeps a full-size image of its own
  (`ic_splash_foreground.png`, scale 1.0) because a splash icon is specified
  inside a 192dp circle. Same script, same source, one run — the two cannot
  drift apart. `docs/icons/` and `docs/manifest.webmanifest` are untouched.
- **No blue tap boxes** (A-3). `html { -webkit-tap-highlight-color:
  transparent }`, with `:active` backgrounds on `.map-fab`, `#ff-fab` and
  `.wb-row` so the feedback the highlight was providing is still there, and
  `user-select: none` on rows and card titles. No Kotlin change.
- `scripts/verify_build.py` reads `APP_VERSION` and `DATA_SCRAPED_AT` out of
  `map.py` instead of hard-coding "v2.0.0", and fails when `APP_VERSION` is
  not the release this tree claims to be (`EXPECTED_APP_VERSION`) — so
  forgetting the version bump now fails the gate instead of shipping the
  previous number in the About sheet.

### Fixed

- `android/tools/emu.sh` and `android/tools/diag.sh` defaulted `JPFM_PKG` to
  `com.fredhli.jpfoodmap.debug`, so reading the diagnostics line out of a
  hand-installed **release** build sent the intent to a package that was not
  there; `am` answered `result code=-92` on a stream nobody reads and the
  script timed out, which looked exactly like "this build has no diagnostics
  logging". They now ask the device (`pm path`) which of the two is
  installed. R8 was never stripping the log line —
  `proguard-android-optimize.txt` carries no `-assumenosideeffects` for
  `android.util.Log`, and `JpfmDiag` is in the release `classes.dex`.
  `verify-geometry.sh` / `verify-flows.sh` were always right: they read the
  package name out of the APK under test.
- `en.json`'s legend line for the Hidden list read "A place you hid / hidden
  from the map by default"; the second half is now "Not shown on the map by
  default", which reads as a sentence next to the first.
- `_TRAD_FIXUPS` gains 谷歌地圖 → Google 地圖: Taiwan writes the product name,
  not a transliteration. A 訂座 → 訂位 rule was tried and reverted before
  release — `to_trad()` replacements are literal and unbounded, and every one
  of the 198 訂座 in the Tabelog policy corpus is followed by 位 (預訂座位 /
  只訂座位), so the rule only ever produced 預訂位位 in `popups-tw.json`.

### Removed

- The ⭐ Saved pill at the bottom-left of phone screens (`#wb-fav-fab`) and
  its `wbFavFabSync()` observer — the ≡ in the search capsule replaces it,
  and the pill was what covered the scale bar. The "≡ 命中 N · 视野内 M" pill
  next to it stays; it is the filter panel's entry point.
- The `.wb-empty` placeholder text in the detail column, which no longer has
  a moment in which it can be read (the column is off-screen until something
  is selected). The node stays so its translation entries stay valid.
- The rail's search and filter buttons (see Changed).

## [2.0.0] - 2026-09-06

The 2026-09-05 audit release: the whole 198-item punch list from
`audit_outputs/integrated-2026-09-05/`, worked through in four milestones.
Highlights — a wide-screen workbench layout, sub-collections inside 收藏,
self-hosted front-end dependencies, a versioned sync protocol with a real
three-way merge, and the project's first tests.

### Added

- A region filter. `#ff-region` at the top of the filter panel lists all 47
  prefectures grouped the way a Japanese map legend groups them, each with
  this build's row count, in the reader's own language; picking one filters
  and then flies the map to that prefecture's bounding box (derived at
  runtime from the rows themselves, so it can never disagree with what the
  filter is about to show). The comparison is an **integer index**, never a
  substring test on the address — 東京都 and 京都府 share two of three
  characters, and 936 Tokyo rows would have leaked into a Kyoto search.
  `tabelog.filterState` gains one additive `region` field; a state written
  before this build, or one carrying a string / out-of-range value, silently
  means "every region" and never throws. (M-023)
- Rating shortcuts: the 3.4-4.5 slider gets tick marks, both end labels, a
  24px thumb and a row of one-tap thresholds (全部 / ≥3.6 / ≥3.8 / ≥4.0 /
  ≥4.2). A `?` next to it explains that 3.4 is the corpus floor, not a UI
  limit — so the left end really does mean "no rating filter". (M-114)
- Every condition that is currently narrowing the map gets a chip with its
  own × — region, rating, price tiers, cuisines, awards, 只看网订, 只看收藏,
  plus the pre-existing 隐藏非日本料理 one. The same count is written into
  the top bar's 筛选 badge, and each section header carries a live summary
  (`≥3.80`, `5 / 7 档`, `Silver · Bronze`) that survives the section being
  scrolled past. (C5, on top of M-022)
- An amber line under 隐藏非日本料理 saying how many restaurants *this*
  filter combination is hiding because of it and how many of those can be
  booked online, with a 一起显示 button that just unticks the box. The
  toggle is on by default and is the single largest silent subtraction on
  the page; the numbers are derived by re-running `passesFilter` with the
  flag flipped, never hard-coded. (B8)
- Keyboard shortcuts, dispatched from one capture-phase handler so the five
  existing per-overlay Escape listeners cannot double-fire: `/` focuses
  search, `J` / `K` step through the results, `F` saves the open card, `X`
  discards it, `Esc` closes exactly one layer (respecting the card's
  full → peek → closed ladder), and `?` opens a shortcut sheet. Typing in a
  field, any modifier, and an open modal all suppress the single-key
  bindings. (M-017)
- 用地点名填入 in the pin dialog, plus ⛩️ and ♨️ as the 7th and 8th emoji
  presets. A Nominatim result with no `name` now prefills from the first
  segment of its address instead of opening a blank field. (G5)
- A copy button next to the coordinates in the pin dialog
  (`navigator.clipboard` with an `execCommand` fallback). (G6)

- The workbench layout shell — the page grows three wide-screen modes on top
  of the phone layout, driven by one `ResizeObserver` on
  `<html>` and a single `wbApplyMode()`. **≥1100px**: 56px top bar (brand,
  search, region, 筛选, sync chip, avatar) + 344px result column + map +
  384px resident detail column. **700-1099px**: 48px top bar + 320px column
  + map; opening a card collapses the column to a 58px icon rail (☰ back /
  live 命中 count / 🔍 / 筛选) and slides a 340px detail column in from the
  right, leaving the map ≥410px on an 816px Fold. **520-699px**: the phone
  chrome (floating search, bottom-right FABs, bottom sheets) plus a draggable
  bottom panel at 45% of the viewport (`#wb-split-handle`, ↑↓ moves it 5% at
  a time). **<520px is untouched** — no body class, no DOM move, and every
  `#wb-*` node stays `display:none`; a masked pixel diff of the 416x657 outer
  screen against the previous build is 0 differing pixels outside the F2/
  M-159 regions below. Filtering, the detail card and the search dropdown are
  the *same DOM nodes* re-parented by `appendChild` (`#ff-sheet-content` ↔
  `#wb-filter-pop`, `#bs-content` ↔ `#wb-detail-body`, `#ss-box` ↔ the top
  bar, the avatar trio ↔ `#wb-top-acct`), so every listener, every id lookup
  and every MutationObserver survives, and `readFilterInputs` /
  `saveFilterState` / `restoreFilterState` are byte-for-byte unchanged.
  Crossing 700px with a card open keeps the selection, the card's content and
  the list's scroll position. On ≥1100/700 the filter panel is a non-modal
  popover anchored under its button (no backdrop, click-outside to close), so
  the result list stays readable while filters change. (M-027)
- Explicit map zoom: a `+ / −` pair at the top of the FAB stack (≥700px, where
  there is no pinch) and a metric scale bar bottom-left. folium builds the map
  with Leaflet's own zoom control off, so before this a trackpad-only laptop
  had no zoom affordance at all and nothing on the page said what a screen
  distance meant. (M-159)

- Build-time structure behind the reservation policy, so the card can stop
  printing one undifferentiated blob. `docs/data/popups*.json` grows three
  append-only slots — no existing slot changes, and a service-worker cache
  still holding 9-slot arrays keeps working because every reader
  length-checks. Slot 9 is `{b, lead, cancel, note}` or `null`: `b`
  (0 = 予約不可 / 1 = 予約可 / 2 = 完全予約制, 99.6% coverage) is read off the
  **Japanese** head word so all four language variants agree on the verdict,
  while `lead` / `cancel` / `note` are extracted from each variant's own text
  with that language's keywords. Every emitted string is a verbatim sentence
  from the text the card would otherwise have shown — nothing is inferred,
  and a field that cannot be found is simply absent. Slot 10 is the closing
  days (5,346 rows; the 2,578 rows whose `holiday` is the literal `-` stay
  `null`) and slot 11 the metres to the nearest station (5,047 rows).
  (M-029, M-030)
- `pref` on every `restaurants.json` row: the prefecture as an integer index
  into map.py's `_PREFECTURES` (0-46, all 9,807 rows, all 47 prefectures
  represented), plus the matching `PREFS` table inlined in the page
  (`{ja, sc, tc, en, n}` per entry). The region filter compares integers —
  substring-matching an address would make 京都 match 東京都. (M-023)
- `st` on every `restaurants.json` row: the nearest station's short name, so
  the result list and the search rows can show it without waiting for the
  6 MB popups payload to arrive. (B1/G2)
- `PRICE_BUCKETS` inlined into the page as `[key, label, lo, hi]`, letting the
  detail card turn a raw ¥ upper bound back into the bucket label the filter
  panel uses. (D2)
- `scripts/verify_build.py` gains two checks: `popup-slots` (slot 9 is
  `null`/dict with keys ⊆ {b,lead,cancel,note}, `b` ∈ {0,1,2} and identical
  across the four variants at ≥99% coverage; slot 10 never the literal `-`
  and present on 5,000-5,700 rows; slot 11 a non-negative int) and
  `row-fields` (`pref` an int 0-46 on ≥99% of rows with all 47 present, `st`
  a non-empty string, exactly one 47-entry `PREFS` table in the HTML). The
  slot **count** is still only asserted as "the four variants agree", never
  against a hard-coded number. (M-029, M-030, M-023, M-056)
- `tests/pipeline/run.py` gains `t_policy_struct`, `t_holiday_slot` and
  `t_prefecture_index`.
- The Android / PWA back button now closes the topmost floating layer
  instead of leaving the app. Detail card, filter sheet, add-bookmark modal,
  import modal, account menu, help popover and the Leaflet coordinate /
  search-result bubbles each push one state-only history entry (`pushState`
  is called with **no** url argument, so `?lang=` parsing and the language
  switch's full navigation are untouched, and the map position never reaches
  the address bar). Two open layers take two back presses, in order;
  re-opening the same layer never stacks twice; closing from the UI hands the
  entry back so the next back press really leaves. Nothing is ever *opened*
  from history state, so a bfcache restore that comes back with a stale
  `history.state.tabelogUi` is ignored. The search dropdown is deliberately
  excluded — typing is not navigation. (M-015)

- Storage durability, which had no call sites at all before this release.
  The first favorite / bookmark asks `navigator.storage.persist()` once per
  browser (latched on the new `tabelog.persistAsked` key; a refusal is
  ignored silently). A signed-out browser holding 20+ favorites gets the
  count in the existing 收藏 hint instead of the generic wording, and when
  the browser refused persistence the hint adds
  浏览器清理时可能删除本地收藏 · 无痕窗口关闭即失 — the honest fallback for
  private windows, which cannot be detected. The account menu gains a
  本地存储 row with `storage.estimate()` usage / quota and the persistence
  state. (M-013, GAP-2-13)
- `DELETE /api/state`: deletes every key the Worker holds for the signed-in
  user (`state:`, `profile:`, `sv:`) and signs the browser out. `GET`/`PUT`
  are untouched; the route's `Allow` header advertises the new method.
  (M-008)
- `docs/privacy.html` — one page in Chinese, English and Japanese saying what
  is collected, where it lives, and how to delete it. Self-contained: no
  external CSS, JS or fonts, readable in light and dark. (M-008)
- `worker/scripts/backup-kv.sh` — timestamped local backup of the sync KV
  namespace (`wrangler kv key list` + one `get` per key, a manifest with the
  restore command). Refuses to run and exits non-zero when wrangler is not
  authenticated, so it can never write an empty backup that looks fine.
  (M-005)
- `tests/worker/` — offline test bench for the Worker: the real
  `worker/src/index.js` driven by `Request` objects against a Map-backed KV
  mock and a stubbed tokeninfo. 93 assertions, each tagged with the audit id
  it guards, including the new-Worker × old-client and old-Worker ×
  new-client compatibility matrix. `node tests/worker/run.mjs`.
- `SESSION_HMAC_PREV`: the session secret can be rotated without signing
  everyone out — verification tries the current secret then the previous one,
  signing always uses the current. (M-126)
- Non-HttpOnly hint cookie `tabelog_has_session=1` (domain-wide, no secret in
  it) so the page can tell whether probing `/api/me` is worth a round-trip
  even after a `localStorage` wipe. (M-013)
- The two transit FABs show a spinner while their 1–4 MB LOD file is
  downloading, and a failed load bounces the toggle back off with a readable
  toast ("交通图层加载失败，请稍后再试") instead of leaving a lit button over an
  empty map. A failure on top of an already-drawn layer only toasts.
  (M-193, BUG-16)
- Search: a "菜系" section at the top of the dropdown when the query matches a
  cuisine bucket in the active UI language — "sushi" now offers the whole
  1,224-restaurant 寿司·海鲜 bucket instead of only the 8 shops with "sushi"
  in their name. One tap narrows the cuisine filter to that bucket (a foreign
  bucket, which has no checkbox of its own, turns off 隐藏非日本料理 instead).
  (M-105, half)
- Search: the "name location" constraint is a visible, removable chip
  ("東京 23 ✕") carrying how many rows it keeps, with a "<place>以外的区域"
  sub-header over the rest and an explicit note when the place matches
  nothing. (M-025, BUG-07)
- The filter panel shows an "已启用：隐藏非日本料理 (N)" chip whenever that
  default-on toggle is hiding rows. N is derived by running the real filter
  with the toggle flipped, so it can never drift from the rule. (M-022)
- Empty state: when the filter matches restaurants but none are on screen, the
  map card offers "缩放到全部结果" instead of telling the user to loosen a
  filter that is not the problem. (M-022)
- Coordinate honesty: the 727 rows whose GSI match never reached a house
  number (the geocoder answered with a 町 / 丁目 centroid — median error
  724 m, a third of them over 1 km) now say 坐标为街区级近似 on the card and
  wear a dashed ring on the marker. The verdict comes from the `display`
  string GSI already returned and the build has been caching all along; it
  is skipped for Google-calibrated rows, and the build refuses to publish if
  the flag lands on more than 1,500 rows. New optional payload field
  `approx`. (M-021)
- Closed businesses are labelled instead of silently misleading: 6
  permanently closed and 9 temporarily closed restaurants get a gray
  已永久歇业 / 暂停营业 badge on the card and a desaturated marker, from
  Google's `businessStatus`. They deliberately keep their marker and their
  place in the payload — a favourited restaurant that vanishes from the map
  reads as lost data. New optional payload field `closed` (1 / 2). (M-093)
- `docs/vendor/` — Leaflet 1.9.3, MarkerCluster 1.1.0, leaflet.locatecontrol
  0.79.0 and emoji-picker-element 1.27.0 (plus its 430 KB emojibase data
  file) now ship from this origin instead of cdnjs and jsDelivr. Same builds,
  byte for byte: `scripts/fetch_vendor.py` downloads them and verifies a
  pinned sha256 for each, and every hash was cross-checked against a second
  and third CDN before being written down. The package version is part of the
  directory name, so `/vendor/*` can be cached `immutable` honestly.
  (M-006, M-011)
- `docs/404.html` — a real not-found page. Unknown paths used to answer 200
  with the full app shell; `/emoji/does-not-exist.png` came back as 200
  `text/html` marked `immutable`, which is a cache entry that never heals.
  Self-contained, no `/vendor/`, readable offline, all three languages at
  once. (M-049)

- The restaurant card leads with a three-tile decision strip — 人均·晚 /
  人均·午 / 预订方式 — directly under the name, so the two numbers and the
  one fact a plan actually turns on are readable without scrolling, in the
  bottom sheet and in the workbench's detail column alike. (§5.3, M-111)
- A five-row reservation-policy table (能否预订 / 网上订位 / 提前多久 /
  取消规则 / 需要注意) built from the structured policy the build step now
  extracts. **Only rows that were actually extracted are drawn** — no
  placeholder dashes, no inferred "shops like this usually…" line — and the
  machine-translated original is always one click away behind 看原文, now
  12.5px `#374151` instead of decorative 12px grey. Most restaurants show
  two rows; that is the true shape of the data, not a gap. (M-029, §5.4)
- A 定休 line, tokenised out of the Japanese closing-days string: the seven
  weekdays plus 祝日 / 不定休 / 無休 are re-emitted as translatable runs
  (zh-CN 周一 · zh-TW 週一 · EN Mon · JA 月曜日); anything unrecognised is
  passed through verbatim under `lang="ja"` rather than guessed at. (M-030)
- 步行 N 分 next to the nearest station, derived from the station distance
  at the 80 m/min rate Tabelog's own walk time uses. (D1)
- 第 n / N 家 with ↑ / ↓ in the card header, stepping through the result
  list without going back to it (and reachable as `window.__bsNav`, which is
  what the J / K keys call). Hidden on the phone layout, where there is no
  list on screen and the header has no width to spare. (D4)
- Saving or discarding a restaurant now says so: a toast with a 5-second
  撤销 that runs the same toggle back through the same sync bookkeeping, and
  is announced on the existing `aria-live` region. (M-031)

- The result list. On every screen 520px and wider the left column now holds
  the matching restaurants as a scrollable list instead of leaving them as
  dots on a map: cuisine emoji with a price-band dot, name, ★rating, price
  band + 上限 + nearest station, award badges (金奖 / 银奖 / 铜奖 / 百名店 /
  热门 — kanji, so colour is never the only signal), 可网订 or 仅电话 / 到店,
  and a ⭐ / 🚫 pair at the row end (34px visual, 44px hit area). Rows are
  56px on a mouse, 60px under a finger, and a discarded restaurant stays in
  the list at 40% opacity with a red ✕ rather than vanishing. The whole
  corpus goes through one code path: `#wb-list-spacer` carries the full
  scroll height and only the visible slice ±8 rows exists in the DOM, so an
  unfiltered "all of Japan" list is ~30 nodes and one window rewrite measures
  1.6 ms (gate: 8 ms). (M-027 / B1 / B6)
- Five sort orders for that list — 评分 / 价位 / 奖项 / 距地图中心 / 名称 —
  in a `<select>` above it. Distance re-sorts on `moveend`, and only while
  that order is the active one. The choice is remembered per browser in a new
  `tabelog.listView` key (`{sort, select}`); an unreadable or unknown value
  falls back to 评分 and never throws. Sorting is deliberately **not** part of
  `tabelog.filterState`, which stays a description of what counts as a match.
  (M-027 / B2)
- Hovering or keyboard-cursoring a row lights that restaurant's marker in the
  same blue the search highlight uses, and clears it on the way out. It is a
  separate, narrower state than `setHighlight()`: no ghost marker, no banner,
  and only markers that already exist get repainted — one hover costs at most
  two icon rebuilds, rAF-coalesced. The list is a single tab stop driven by
  `aria-activedescendant` (↑↓ / PgUp / PgDn / Home / End / Enter / Space), so
  7,626 results never become 7,626 tab stops. (M-027 / B3)
- Batch save / batch discard. 选择 turns the rows into checkboxes with
  全选可见 / 批量收藏 / 批量弃用 / 取消 above them. `toggleFav()` and
  `toggleBlack()` take an optional `{defer: true}` that skips their
  `schedulePush()` tail, so starring twelve places writes
  `omakase_state_cache_v2` **once**, repaints the markers once and schedules
  one debounced PUT instead of twelve of each. Called with no options — every
  pre-existing call site — the behaviour is unchanged. (M-027 / B4, H2)
- A footer under the list: 其中 N 家能在网上订 · 只看这 N 家可网订的店, where
  the button just ticks 只看网订. Hidden when the number is zero or the box is
  already ticked. (M-027 / B7)
- The search dropdown's restaurant rows read the same way as the list rows:
  price-band dot on the icon, nearest station (`lang="ja"`) and price band in
  the sub-line, ✓ for online booking. Same `.ss-row` structure, so the
  dropdown's observer and keyboard navigation are untouched. (M-115 / G2)
- Restaurant markers carry an accessible name. Leaflet makes every marker a
  focusable `role="button"` but only copies `alt` onto `<img>` icons, and
  these are `divIcon`s — so every marker was an unnamed button in the tab
  order. Each one now announces "name ★rating". Cluster bubbles are
  unchanged. (M-164)
- Shareable restaurant links. `?r=<id>` opens the map straight onto one
  restaurant's card, where `<id>` is the trailing numeric segment of its
  Tabelog URL in base36 (all 9,807 rows have one, all distinct, ≤5
  characters — nothing extra is baked into `restaurants.json`). The
  parameter is consumed once and removed with `replaceState` before the
  card opens, so the address bar stays clean, the card is the only new
  history entry, and one back press closes it without leaving the site. An
  unknown or malformed id is ignored in silence rather than shown as an
  error. A language-switch reload takes priority, so switching language
  with a card open cannot open it twice. `window.__shareUrlFor(row)` builds
  the link from origin + pathname only — never from `location.href`, which
  would carry `?lang` and permanently overwrite the recipient's UI
  language — and `window.__shareRestaurant(row, ev)` offers it through
  `navigator.share` (synchronously, inside the click, or the browser
  rejects it), falling back to the clipboard with a 已复制链接 toast and
  then to `execCommand`. Deliberately NOT shareable: user pins, the map
  view, the filter state, the user's location. (M-032)
- Exported `favorites.json` entries carry context. `favorites` and
  `blacklist` are now `[{url, name, rating, city}]` instead of bare
  tabelog.com URLs, so the file can be read by a person or opened in a
  spreadsheet. `schema` deliberately stays `1` (nothing gates on it, and
  bumping it could only make an older build refuse a file it can read),
  and files exported before this — plain string arrays — still import
  unchanged; `tests/compat/fixtures/08_export_legacy.json` locks that
  down. The KV sync body is untouched: `buildBody()` still sends plain
  URL-string arrays. (M-032)

- Sub-collections — one flat level of named lists over the places already
  saved, with the data layer behind `window.__flLists / __flListsOf /
  __flMembers / __flCreate / __flRename / __flDelete / __flAdd / __flRemove`
  and a shared name+icon dialog behind `__flEditModal`, plus an `fl:change`
  event on `document` after every mutation. **No new storage anywhere**: a
  list is `{id:'list:<k>', category:'meta', kind:'list', name, emoji,
  created}` and each membership is its own `{id:'lm:<k>:<ref>',
  category:'meta', kind:'member', list, ref}` row, both appended to the
  existing `bookmarks` array — so they ride the existing `bookmarks` field
  of the KV blob and need no new localStorage key and no new top-level
  field (which the Worker's whole-blob replace would have required M-044
  for). One row per member, never a `members[]` array, because
  `mergeBookmarks()` merges by id: two devices adding different restaurants
  to the same list both survive. `category` is `'meta'`, never `'hidden'` —
  `rebuildHiddenIds()` keeps meaning exactly "built-in landmark tombstone".
  A restaurant added to a list is also starred (through the existing
  `toggleFav`), so a client that knows nothing about lists still shows it
  as saved; removing it from a list, or deleting the list, never un-stars
  it. Rows whose target is no longer saved are hidden on read and swept on
  the next write only — never on a read path, and the on-disk favorites
  cache counts as "still saved" so a cross-tab race cannot delete a live
  membership. `renderBookmark()`'s existing numeric-coordinate guard is
  what keeps all of this off the map. `tests/compat/fixtures/
  07_bookmarks_with_meta.json` locks the round trip down, and
  `scripts/verify_build.py` gained a `subcollections` check asserting the
  four invariants the scheme rests on. (M-031, design §4.1 / E2)
- Sub-collections can be reached from the two places a restaurant is
  actually decided on. The detail card gains a 加入子收藏夹 row just above
  its action bar — one 32px/44px-hit-area chip per list plus a ＋ that opens
  the shared new-list dialog and files the restaurant into whatever it
  creates. Tapping a chip stars the restaurant as a side effect (the ⭐
  button, the favourites counter, the marker icon and the filter all update
  in the same tick); un-ticking it only leaves the list — it never un-stars.
  The row repaints itself on `fl:change` instead of repainting the card, so
  the card keeps its scroll position, and it is hidden in the outer-screen
  peek state where only the decision tiles belong. The pin dialog gets the
  same chip row under the emoji presets; picks are held in memory and only
  written once 保存 succeeds, after the `bm-*` pin exists, so a cancelled
  dialog cannot leave a membership row pointing at nothing. (E11 / M-031)
- The search dropdown puts restaurants you have already saved in their own
  已收藏 section above 餐厅库, without repeating them below it, and tags
  every restaurant row with the sub-collections it is filed under (two
  names, then `+N`). With no saved hits the dropdown renders exactly as
  before — same single section, same 屏幕内 / 其他区域 split, same
  `.ss-loc-first` promotion of 地图搜索. (G3 / M-031)
- A 分享 button on the detail card, beside the Google Maps square: 44×44,
  `navigator.share` where the browser has it and a clipboard copy with a
  「已复制链接」 toast where it does not. It hands out the `?r=<id>` deep
  link with no `lang` attached, so a shared link never rewrites the
  recipient's UI language. User pins have no share entry point at all.
  (M-032)
- A **收藏 tab** beside 结果 in the left column, with the saved count on it.
  It lists every favourite grouped **按城市** (Japanese city name, rating
  descending inside each group) or **按子收藏夹**, and it is the one view on
  the page that **ignores the filter panel entirely** — tightening the rating
  to ≥4.3 and ticking 隐藏非日本料理 removes nothing from it, because a
  favourite that silently disappears reads as lost data. Favourites whose URL
  is no longer in the corpus get their own 不在当前数据里 group and can still
  be un-starred. Which tab you were on rides in the existing
  `tabelog.listView` as one additive `tab` field — no new localStorage key,
  and a state written before this build simply opens on 结果. (E1 / B9 /
  M-031)
- Sub-collection management in that tab: a collapsible group per collection
  (icon, name, count) with 重命名 / 复制清单文本 / 只看这个 / 删除 behind its
  ⋯, a ＋新建子收藏夹 button, and a per-row ⋯ that ticks the collections a
  place belongs to — one place can sit in several at once. Everything reads
  and writes through the `window.__fl*` data layer; the `bookmarks` array is
  never touched directly. Deleting a collection is undoable for 9 seconds and
  never changes any member's ⭐. Built-in landmarks (`fb-*`) and personal pins
  (`bm-*`) show up as members too and fly the map to themselves when clicked.
  Selecting rows enables 移到… / 从子收藏夹移除 / 弃用, and a batch 弃用 of N
  rows costs exactly one `omakase_state_cache_v2` write and one debounced
  PUT. (E2 / M-031)
- **复制清单文本** on any collection and on the whole favourites list:
  plain text, one line per place as `店名 · ★评分 · <Tabelog URL>` under a
  `清单名 · N 家` header, via `navigator.clipboard` with an `execCommand`
  fallback. No CSV. (E8 / M-120)
- **只看这个** crops the map to one collection's restaurants: 命中, 视野内,
  the markers and the result list all agree, and a 正在看 … 显示全部 bar
  floats over the map (tracking the workbench insets) so the state can never
  be mistaken for a broken filter. It is one line at the top of
  `passesFilter`, so ordinary filters still apply on top of it. (E9 / M-031)
- A first-visit value bar: one dismissible row under the search box saying
  what the site is, with **选一个地区开始** (opens the filter panel with the
  focus already on the region select) and **地图怎么看**. Not a tour, not a
  scrim, and deliberately no automatic geolocation — most sessions are trip
  planning from outside Japan, and a `locate()` on boot would also throw away
  the restored `tabelog.mapView`. Dismissing it writes the new local-only key
  `tabelog.seenIntro` and it never returns. (M-109)
- An on-demand map legend (`#legend-pop`), reachable from the value bar and
  from a new 地图图例 row in the account menu. Four sections: all **seven**
  price tiers with their real marker colours (generated from `PRICE_BUCKETS`
  at runtime, so the legend cannot drift), what the emoji in a marker means,
  what the number in a cluster bubble counts, and ⭐ / ✕. The price wording
  says "价格区间上限" because that is what the buckets actually are, and the
  cluster wording never claims the bubble's *size* encodes anything — it
  does not. (M-109 / M-110)
- 关于本站 (`#about-modal`), also from the account menu: data source
  (Tabelog, scraped 2026-05-19), coordinates (GSI + Google Maps), base map
  (OpenStreetMap / CARTO), the curation rule (reusing the one authored
  wording from the price-curation help popover), the "check the original
  listing" disclaimer, the version, a link to the privacy policy, and an
  entry point for deleting cloud data that hands off to the existing
  `#ssm-delete-cloud` rather than reimplementing deletion. New `APP_VERSION`
  / `DATA_SCRAPED_AT` constants in `map.py` feed it. (M-119)
- Install-as-an-app, three ways. `beforeinstallprompt` is captured at module
  scope (it fires long before the payload lands) and spent by a 安装为应用
  row in the account menu; browsers that never fire it get a text card
  instead — iOS share-sheet steps (all iOS browsers are WebKit, so no
  "switch to Safari" advice), the Chromium desktop menu path, or a neutral
  fallback that claims neither support nor its absence. After a visitor's
  **first** favourite, a bottom snackbar offers the same thing once, with
  安装 / 稍后 / 不再提示. Frequency lives in the new local-only key
  `tabelog.installHint` (once per session, 30 days after 稍后 or a dismissed
  system prompt, at most 3 offers per 90 days, permanent after 不再提示 or
  `appinstalled`). Everything disappears in standalone display mode. Neither
  new key is in the sync blob, `buildBody()`, or KV. (M-066 / M-145 / M-197)
- `manifest.webmanifest` gains two `shortcuts`. Both point at `/` — the site
  has no routes other than `start_url`, so a shortcut can only pre-name the
  intent, not deep-link to it. `id` / `start_url` / `scope` are untouched
  (changing `id` orphans every installed icon) and no `theme_color` was
  re-added. `verify_build.py` now asserts all three, plus that the About
  sheet really carries `v2.0.0` and `2026-05-19`. (M-145 / M-119)
- A results / collections drawer on the phone. Below 520px the workbench
  left column never existed, so the Fold's outer screen — the 30% of use
  that happens in Japan, on the move — had no list at all. A ⭐ pill now
  sits one step above the 筛选 pill and slides `#wb-left` up from the bottom
  as a 66dvh drawer: the *same* element, the same 结果 / 收藏 tabs, the same
  rows, the same handlers. It is an overlay, not a layout change (the map
  keeps its box); the FAB stack and both pills step aside while it is up,
  exactly as they already do for the filter sheet. The drawer and the
  filter panel are mutually exclusive, it joins the overlay stack so
  Android back closes it instead of leaving the site, Tab is trapped inside
  it with the background `inert`, and Esc / the scrim / the × all hand
  focus back to the pill. With favourites it opens on 收藏, without them on
  结果 — and once the reader has picked a tab themselves, that choice wins
  (it rides in the existing `tabelog.listView`; no new storage key). No
  automatic geolocation, ever. (F1 / M-031)
- Sorting by distance is offered only after the reader has actually tapped
  ⊙ and a fix came back — and it then measures from *their* position, not
  from the map centre, relabelling itself 距我的位置. Until then the option
  is not in the menu at all, because "距地图中心" answered a question nobody
  asked. Nothing on the page ever calls `locate()` on its own. (F1)

### Changed

- One word per concept, in all four languages (M-106). 「收藏」 used to mean
  both a starred restaurant and a user-made map pin, and English had split
  the reject list across *Hide*, *Hidden*, *Discard*, *Discarded* and
  *excluded*. The vocabulary is now: starred restaurant = 收藏 / 收藏 /
  **Saved** / お気に入り; user pin = 书签 / 書籤 / **Pins** / ピン; reject
  list = 弃用 / 棄用 / **Hidden** / 除外; built-in tourist anchor = 景点 /
  景點 / **Landmarks** / 名所; the Google-corrected-coordinate filter =
  校准 / **Google-verified** / Google 補正. In Japanese `非表示` stays the
  generic "hide" so it no longer collides with the reject list, and in
  English `Hide` survives only as the verb on the button. The pin dialog,
  its delete action, the 收藏 workbench chips, the layer popover subtitle and
  the three help popovers say 书签 where they used to say 收藏. **Only
  display strings changed** — `omakase_state_cache_v2`, `tabelog.bookmarks`,
  the `category` values and every id are byte-identical, so nothing about
  stored or synced state moved.
- `data/i18n/en.json` and `data/i18n/ja.json` now hold **identical key sets**
  (1,033 each; `日本語` was previously English-only), and the untranslated-run
  count fell to EN 478 / JA 478 against the 488 / 489 baseline.
- `README.md` rewritten for 2.0.0: what `.env` needs, the `flock`ed build
  command, every test suite and what it covers, the fact that the avatar
  menu's 导出 favorites.json is the **only** user-data backup path (M-005),
  the pipeline's atomic writes and `.prev` generation (M-020), and the
  Cloudflare Pages / Workers rollback procedure. (M-051)
- `CLAUDE.md` brought back in line with the code (M-051): the data-flow
  section now lists every build output and the 12 positional `popups*.json`
  slots, `docs/vendor/` and the R2 transit overlay; the Worker is described
  as the 652-line cookie-session + `baseV`/409 + read-modify-write service it
  actually is; the localStorage section is an exhaustive, grouped table of
  all 17 keys plus the one `sessionStorage` key, calling out `tabelog.syncBase`
  as unclearable and `tabelog.showTransitLong` / `…City` as the real names;
  a new section documents how sub-collections live inside the `bookmarks`
  array and why nothing goes to the KV blob's top level; and a new "Build
  gate" section explains `verify_build.py` and the i18n baseline.
- MarkerCluster's default bubbles were green / yellow / orange by size — a
  third colour scale competing with the price halos (green→red) and the
  filter chips, saying "many restaurants" in the hue that everywhere else
  means "expensive". All three sizes are now one neutral blue-grey; the
  number inside already says how many. Overridden with a two-class selector
  so it outranks the vendor stylesheet without `!important`. (M-110)
- The signed-out account pane answered none of the three questions people
  actually have about signing in. It is now three ≥12px lines — what syncs
  (收藏 / 弃用 / 书签 / 子收藏夹), that we read only the email and avatar,
  and that the data is hosted on Cloudflare and can be exported or deleted —
  plus "不想登录也完全可用". Each line is a single CJK run with its own
  `data/i18n/{en,ja}.json` entry, so the per-language hand-override that used
  to patch the old one-liner is gone. No sign-in logic changed. (M-118)
- The bottom-right FAB column is three controls instead of seven. The four
  layer toggles (长途 / 市内 / 景点 / 收藏) moved into a 图层 popover with a
  count badge; the zoom pair (≥700px) and the round locate button stay where
  they were. Each row is 44px with a one-line explanation and a switch, and
  the 景点 third state ("also show sights I hid") gets its own checkbox
  instead of being reachable only by clicking twice more. The panel is
  non-modal — it closes on Escape, on a click anywhere outside it, and on
  Android's back gesture — and hands focus back to the 图层 pill. Nothing
  about the toggles themselves changed: same four button ids, same click
  handlers, same `aria-pressed`, and the same four localStorage keys with
  the same `'0'` / `'1'` / `'2'` values, so a browser that had transit on
  and 收藏 off yesterday comes back exactly that way. Measured across eight
  viewports × four states (no card / detail card / filter open / split panel
  at 75%), every FAB that paints is hit-testable — the two reachability
  regressions the M3 gate found are fixed by construction, since a 96-200px
  stack can no longer collide with a card the way a 252-356px one did.
  (A11, on top of M-071)
- The filter panel's option rows are real touch targets: 40px on a mouse,
  44px under a coarse pointer, 18px checkboxes. The inline `display:block`
  that made this impossible to fix from a stylesheet is gone from the
  generated markup. The `?` badges are 18px with a 44px hit halo that stays
  inside their (≥40px) header row, so they can no longer steal a tap from
  the checkbox on the line below. The cuisine list lost its 180px inner
  scroller — a second scrollbar inside a scrolling sheet, on the one list
  people scrub through — and lays out in two columns above 360px; the
  `<details>` still collapses the whole box. The pin dialog's inputs, save /
  cancel and close button are 44, its emoji chips 34 with a 44px halo.
  (M-078)
- The 7 `<a href="#">` 全选 / 全清 controls in the filter panel are
  `<button type="button">`. They were links that went nowhere, announced as
  links, draggable, and one missed `preventDefault` away from writing `#`
  into the URL. The rating slider gained a real `<label for>` and an
  `aria-valuetext` that says `≥ 3.80` rather than a bare number. (M-088)
- Overlays that declare `aria-modal="true"` now behave like it. The filter
  sheet (phone / split), the pin dialog and the import dialog trap Tab,
  mark the map / search / FAB stack / workbench columns `inert`, and hand
  focus back to whatever opened them. The mid/wide filter popover stays
  deliberately non-modal — it only lands focus inside and returns it on
  close — and the panel header gained an explicit × (44px) so closing it no
  longer requires finding the backdrop or the FAB it hid.
  (M-074, M-113)
- The pin dialog's keyboard avoidance applies to every touch device, not
  only viewports ≤480px — a Fold inner screen is 616 CSS px wide and very
  much has a soft keyboard. When `visualViewport` reports a real shrink the
  dialog is also clamped to the visible height and pinned to its top.
  (M-080)
- Import reports outcomes in the page instead of `window.alert()` — into
  `#imp-error` when the dialog is open, otherwise a toast. Four call sites;
  a blocking browser dialog is suppressed outright in some in-app webviews
  and, inside the installed PWA, looks like it came from somewhere else.
  The three "已同步 hh:mm:ss" timestamps are formatted with the page's
  language, not the browser's. (H11)

- Touch targets on the always-on chrome now clear 44px: the search pill is
  44 high (12px input padding), the avatar keeps its 36px face but grows a
  44px hit area (the round crop moved from the button to its `<img>` so the
  button is no longer `overflow:hidden`), the locate FAB is 44×44, every
  layer FAB is at least 44 tall, and below 480px they are square 44×44 icon
  buttons instead of a 37×35 pill. The account-menu rows are 40 high.
  (M-078)
- Search result rows give the name two lines instead of one ellipsized line —
  Japanese restaurant names routinely run past 20 characters, so two
  different branches of the same shop used to render identically. The address
  keeps one line, and the box itself widens from a flat 380px to
  `clamp(380px, 64vw, 560px)` (it is `flex:1; max-width:560px` inside the top
  bar). (M-086)
- The search field's focus ring moved from the `<input>` to
  `#ss-input-wrap:focus-within`, so the whole pill lights up and the ring no
  longer depends on an `outline:none` with nothing replacing it. The field
  also gained an `aria-label` (a placeholder is not an accessible name — it
  disappears the moment you type) and `aria-haspopup="listbox"`, and each
  render of the dropdown announces its row count through the existing
  `#sync-sr` live region. (M-084)
- The account dropdown is a disclosure, not a menu: `role="menu"` is gone
  (its contents are a settings pane, not menu items), `#ss-avatar` carries
  `aria-expanded` / `aria-controls`, closing it hands focus back to the
  avatar when focus was inside, and the four language buttons carry
  `aria-pressed`. (M-088)
- Safe-area insets reach the last few fixed elements: the FAB column's right
  edge, the filter FAB's left edge, the search box's top offset below 480px
  (it was a hard-coded 8px, i.e. under the status bar on a notched phone) and
  the basemap credit's bottom padding. `env()` is 0 on every device without
  a cutout, so nothing moves there. (M-090)
- Corner radii and small type on the map chrome and the search box converge
  on the four-step scale (control 6 / card 10 / popover 12 / pill 999) and
  the 11px floor; the strings that carry real information — the result
  address, the rating, the sync status — go to 12px. (G8, M-079)
- A viewport under 420px tall caps both bottom sheets at 60dvh, so a
  half-folded or vertically split window keeps a usable strip of map.
- The bottom sheets and the add-bookmark modal no longer fly across the
  screen when the viewport changes. `#bs-sheet` / `#ff-sheet` are centred with
  auto margins at every width, so the ≥700px breakpoint changes only their
  width — a 616→816 Fold rotation with a card open used to animate the sheet
  ~340px sideways over 200ms (measured left edge 408→303→214→141→89→68). The
  grip drag no longer branches on `innerWidth` either. A short `.no-anim`
  window around every resize covers the modal's 480px breakpoint the same
  way. (M-152, M-153)
- `#bs-sheet` / `#ff-sheet` height caps gained an absolute floor:
  `min(75dvh, calc(100dvh - 132px))` (80/85dvh at the wider breakpoints
  against the same floor). On a 416px-tall landscape Fold an expanded card
  used to leave a 116px sliver of map. (M-091)
- The bottom-sheet grip is a real `<button>` (disclosure, `aria-expanded`
  tracks peek/full, 44px tall in peek, unchanged 19px in full so the card
  layout does not shift). Its "swipe up for details" hint moved out of a CSS
  `content:` string into DOM text — the i18n TreeWalker can only see text
  nodes, so EN / JA / TW visitors were all reading the Simplified Chinese
  line. Both wordings ship and a `(hover: hover) and (pointer: fine)` query
  picks: desktop reads "click", touch reads "swipe up". (M-157, M-188)
- The help popover re-anchors on resize instead of dismissing itself —
  unfolding the phone used to kill the explanation the user had just opened —
  and clamps vertically, flipping above its trigger (arrow included) when
  there is no room below. It used to run ~58px past the bottom edge on a
  short viewport. (M-154, M-156)
- The expanded emoji picker scrolls itself into view and is capped at
  `min(280px, 45dvh)`; with the keyboard up it used to open entirely below the
  fold. (M-155)
- The search dropdown's mouse hover is `#f3f4f6` instead of `#f9fafb`, which
  was under 2% off white and invisible next to the keyboard highlight's
  `#eff6ff`. (M-158)
- All 40 decorative `:hover` rules are inside
  `@media (hover: hover) and (pointer: fine)`. A touch tap used to leave the
  hover tint stuck on the button — on the layer FABs indistinguishable from
  the blue "layer is on" state. The rules are gated, not deleted, so a Fold in
  DeX or any tablet with a mouse keeps the affordance; `:active` and
  `:focus-visible` stay ungated. (M-160)
- EN / JA detail cards give the label gutter 66px (`html[lang=…]`, Chinese
  keeps 38px) so Dinner / Station / Address and 夕食 / 最寄駅 / 住所 stop
  pushing the value column out of alignment. (M-161)

- `PUT /api/state` preserves unknown top-level fields. The three known arrays
  are still replaced wholesale so deletions propagate, but anything else the
  stored blob carries is copied forward instead of being erased by a client
  that never heard of it; an explicit `null` deletes a field on purpose.
  This unblocks the write-id and preferences fields the sync work depends on.
  (M-044)
- A `PUT` without `baseV` (the pre-2026-07-28 page shell) is now merged into
  the stored blob — favorites/blacklist unioned, bookmarks unioned by id —
  instead of replacing it wholesale. It still answers the literal `ok`.
  Worst case it revives something that client had deleted; it can no longer
  wipe another device's data. (M-043)
- The 200 KB request cap is measured in bytes, not UTF-16 code units (450 KB
  of CJK used to slip through), and the 413 body now carries
  `{limit, used}`. (M-053)
- Session cookie is host-only (`api.jpfoodmap.com`) instead of domain-wide,
  and the JWT carries only `{sub, iat, exp, sv}` — the profile moved to the
  `profile:<sub>` KV key. `/api/me` returns the same shape as before and
  falls back to the old cookie's inline claims. Sign-in also clears the old
  domain-wide cookie. Everyone signs in once more after the deploy; no data
  is affected (KV is keyed by Google sub). (M-037)
- Every error response is a JSON envelope (`{error, message}`); every 405
  carries `Allow`; `/api/me` and `/api/state` carry
  `Cache-Control: private, no-store`. Success bodies are unchanged. (M-132)
- Documentation: `CLAUDE.md` gains `tabelog.syncBase` (flagged as
  un-deletable — clearing it makes a merge read the remote as deleted) and
  `tabelog.lang` in the localStorage list, the real Worker size, the new KV
  keys, and the read-modify-write semantics of `PUT`; `README.md` no longer
  claims the payload is embedded in `index.html`. (M-051)
- Cached emoji PNGs are resampled to 64×64 at build time (`pillow` is now a
  dependency) and ship under a new `<hex>-64.png` name; the manifest still
  carries all 125 entries, so nothing falls back to emojicdn. 2,726 KB → 753 KB
  (−72%) for the set the page references. The 160 px originals stay on disk —
  an `index.html` sitting in someone's service-worker cache still points at
  them. Pillow missing degrades to the original bytes with a warning.
  (M-060)
- Transit LOD choice accounts for what is visible and what the link can
  afford, not zoom alone: with only 长途 on it stops at the 2.55 MB `mid`
  file instead of pulling the 4.2 MB `high` one, and `saveData` /
  2g / 3g connections cap there too. Zooming out (or turning 市内 off) no
  longer *downloads* a coarser file — it swaps only if that LOD is already
  parsed. (M-009)

- The service worker installs all-or-nothing. `APP_SHELL_URLS` plus a new
  build-time `CRITICAL_URLS` list are fetched with no per-URL `catch`; a
  non-`ok` response rejects `install`, so the running worker and its caches
  survive a deploy fetched through a captive portal or a 5xx window. The
  big data payloads (`PRECACHE_URLS`) stay fail-soft. (M-012)
- The new worker no longer calls `skipWaiting()` on its own. It waits, the
  page offers 有新版本 · 重新载入 / 稍后 in `#sync-stack`, and only an
  accepted prompt posts `{type:'SKIP_WAITING'}`. `controllerchange` reloads
  exactly once, and nothing in that path pushes, schedules a push, or clears
  `tabelog.syncBase` — pending edits are already on disk. (M-012)
- `activate` confirms the new shell cache actually holds `'./'` before
  deleting any other shell cache; if it doesn't, older shells are kept and a
  warning is logged. The `?v=` garbage collection runs either way. (M-012)
- Basemap tiles moved to their own cache (`tabelog-tiles-v1`, LRU-capped at
  70). They used to share the CDN cache with Leaflet / MarkerCluster, and
  once the tile pattern started matching (M-058), a minute of panning
  evicted the very bundles M-011 cached for an offline boot — measured 72
  tile entries and 0 script entries before the split. CDN code keeps its
  own cache, capped at 120. The trim trigger is a deterministic every-10th
  put instead of `Math.random() < 0.02`. (M-144, M-011)
- Navigation cache entries are keyed on `'./'` instead of the full URL, so
  `/?lang=en`, `/?lang=ja` and (soon) `/?r=<slug>` share one copy of the
  document rather than one 760 KB copy each. Reads still try the exact
  request first. (M-149)
- `.json` / `.png` / `.js` / `.css` responses that come back as
  `text/html` are refused by the cache. A static host's 200-status error
  page cached under `data/foo.json?v=<hash>` is permanent poison — the hash
  means it is never refetched. (M-049)
- Traditional Chinese is now generated with OpenCC `s2twp` plus a small fixup
  table (`to_trad()` in `map.py`) instead of `s2t`. `s2t` only swapped
  glyphs, so the 繁體 UI read as mainland copy in Traditional clothes:
  登錄 → 登入, 導出 / 導入 → 匯出 / 匯入, 設置 → 設定, 屏幕 → 螢幕,
  搜索 → 搜尋, 數據 → 資料, 賬戶 → 帳戶. The fixup table then undoes what
  s2twp over-converts: Japanese proper nouns (栗林公園, 夫婦岩, 御台場,
  時計台) and Taiwanese software jargon that doesn't fit this page
  (型別 → 類型). (M-102)
- Cuisine tagging reads the whole genre string. `categorize_genre` scans
  twice — the first pass skips container tokens (レストラン, ビュッフェ,
  ファミレス, ホテル, 売店, その他), so 24 rows listed as
  "レストラン、フレンチ" moved out of 其他 and into their real bucket. And
  "is this non-Japanese cuisine?" is now decided over every token rather
  than whichever one Tabelog happened to list first, which used to make the
  同一家店 visible or hidden depending on listing order. Consequence: the
  default-on 隐藏非日本料理 toggle hides 2,181 rows instead of 1,638, and the
  panel's 非日本料理 counter reports that same number. New optional payload
  field `foreign`; a stale cached payload without it falls back to the old
  first-token rule instead of breaking. (M-095)
- Seven built-in sights had no Chinese name and two had no English one, so
  zh-CN / zh-TW / EN visitors saw raw Japanese: 小町通, 尾道, 近江町市場,
  奥入濑溪流, 五色沼, 直岛, 草千里. Six more had Traditional names with the
  wrong character (知牀國立公園, 首裏城, 吉野裏遺址, 慄林公園, 夫婦巖,
  御臺場). Names only — every `fb-*` id is unchanged, as they are permanent
  by contract. (M-180, M-102)

- The detail card is ordered by decision, not by Tabelog's field order:
  awards and the closed badge, then the name, then cuisine · station · walk,
  then the decision tiles, the policy table and the closing days, then
  seats / address, then the photos, then a 44px action row. The photo grid
  used to sit between the name and everything worth reading. (M-111, D1)
- Prices are always a bucket plus the word 上限 (`¥20,000+ 上限`), matching
  the labels the filter panel and the marker colours already use. The raw
  Tabelog ceiling survives in `title=`; printing `¥39999` as the headline
  read as a price the restaurant charges. (M-029, D2)
- Card actions moved out of the header into one bottom row —
  [⭐ 收藏][🚫 弃用][🗺 Google Maps] and Tabelog on the right — every control
  44px tall, and × keeps its 28px look with a 44px hit box. The header now
  carries only the result stepper and ×. (D5, H3)
- The card's two-column info grid switches on the **card's** width, not the
  viewport's: at 1440px with the workbench open the card is 384px wide, and
  a viewport media query was giving it two columns it had no room for.
  (M-111)
- The outer-screen peek state is a decision surface: ribbons, name, rating,
  cuisine · station, the three tiles and ⭐ / 🚫, inside 40% of the viewport.
  The "已被 Google 地图校准" note in particular is reassurance about the pin,
  not a decision input, and no longer competes for that space. (D6, M-112)
- ⭐ / 🚫 state is a class rather than four inline style writes, so the
  action row can style the buttons; `aria-pressed` still carries the state.
  Card text below 11px is gone, the booking chip is 12px, and the card's
  radii collapse onto the four-step scale. (M-078, M-079, G8)
- Card photos declare `width` / `height` / `decoding="async"` alongside
  `loading="lazy"`. (H11)
- The open card names itself for assistive tech (`aria-label` on the sheet),
  and closing it hands focus back to whatever opened it instead of dropping
  the caret on `<body>` — only when focus was inside the card, so dismissing
  it by panning the map does not yank focus. (M-074)

### Fixed

- The sync size warning reaches the person who needs it. Above 180,000
  characters of PUT body (the Worker rejects 200,000 with a 413) the page
  only wrote "同步数据接近上限" into `#sync-status`, which exists solely
  inside the settings modal — so in practice the first visible symptom was
  "同步失败: HTTP 413". The same message now also goes out as a toast, at
  most once an hour, and the PUT is still sent: nothing about what the
  Worker sees has changed. (M-053)

- The import dialog's "N 个书签" count no longer includes metadata-only
  entries. It already excluded built-in hide-tombstones; it now excludes
  the sub-collection rows too, so the number matches the pins the user is
  about to get. The import itself still carries every entry across —
  including the metadata, which `doImport`'s M-133 coordinate check was
  otherwise rejecting as junk, so exported lists survive the round trip.
  (M-031, on top of M-133)

- The layer FABs and the basemap attribution ride above the restaurant
  detail card. Measured reachability of the five buttons with a card open was
  0/5 on 416x657, 616x816, 657x416 and 393x852, and the OSM / CARTO credit —
  which the tile terms require to stay visible — was completely covered on 28
  tested viewports. `openSheet` publishes the card's live height (a
  ResizeObserver catches peek↔full and the placeholder growing into the real
  card) and both stacks sit on it. Now 5/5 on every viewport where the stack
  physically fits and 3/5 where it cannot (657x416, 246px split-view), with
  the credit fully visible on all seven. The lift backs off entirely when it
  would leave under 60px of map, and is capped so no button is pushed off the
  top edge. The credit also stops short of the FAB column, which at 246px used
  to overprint it. The two viewports left at 3/5 here are finished off by the
  short-viewport row below. (M-071, AUTO-05, AUTO-12)

- On a short viewport the FAB stack lies down. Growing every button to 44px
  (F2/M-078) made the column 252px tall, and this release's fuller detail
  card opens 469px tall — 252 + 469 + 56 (the search capsule) is 777px, which
  does not fit on a 657px-tall Fold cover screen in portrait or a 416px one
  in landscape. No clamp wins on a single axis, so below 561px tall, and in
  portrait below 796px tall and 700px wide, the stack becomes a 252x44 row
  above the card instead of a column beside it (`wrap-reverse`, so a second
  row grows upward and never falls off the bottom). Buttons stay 44x44 and
  the order is unchanged; taller phones (393x852, 616x816, 370x800) keep the
  familiar right-hand column. Split mode also gains a hard ceiling on the
  stack's offset — the results panel's height fed the same `bottom` calc with
  no upper bound and pushed `#fab-locate` to y = -41 at 657x416 — and the
  basemap credit stops subtracting a panel height it never had, which used to
  throw it off the top of the screen. Measured 5/5 reachable on 416x657,
  393x852, 657x416, 616x816, 246x816 and 370x800 with the card open, the
  panel at its 75% ceiling, or both. (M-071 follow-up)

- The service worker's tile pattern (`^[a-c]\.tile\.` /
  `.tile.openstreetmap.org`) never matched a single request: the basemap is
  `{s}.basemaps.cartocdn.com`. Tiles were therefore never cached and an
  offline pan showed grey squares. (M-058)
- `networkFirst` handed a 5xx straight to the navigation while a good cached
  document sat one line away — the origin's error page replaced the site
  during any server hiccup. Only an `ok` response now wins the race; the
  same rule applies in `staleWhileRevalidate`. (M-151)
- Connectivity is finally visible: `navigator.onLine` had zero call sites, so
  a traveller on a dead SIM saw photos, search and popups stop with no
  explanation. `online` / `offline` toggle a neutral grey bar (当前离线 ·
  显示的是本机缓存的数据) in `#sync-stack`, announced through the existing
  `aria-live` region. Deliberately not the red failure styling — the cached
  map still works. (M-070)
- A restaurant missing from `popups.json` opened a 33 px white strip with a
  grip and nothing else: no name, no rating, no way to close it. It now
  renders a minimum card — name, rating, working ×. (BUG-10)
- The emojicdn fallback `<img>` carries `crossorigin="anonymous"`, so those
  responses are CORS rather than opaque and Chrome bills them their real
  ~4 KB instead of a ~7 MB padded estimate. Local `emoji/*.png` are
  same-origin and unchanged. (M-144)
- `unpkg.com` dropped from the worker's cross-origin allow-list; nothing in
  the build requests it. `cdn.jsdelivr.net` / `cdnjs.cloudflare.com` stay
  for now. (M-135)
- Dead `/transit/` branch removed from the fetch handler — that data moved to
  R2 (`assets.jpfoodmap.com`) and never reaches the same-origin path.
  `CLAUDE.md` corrected to match. (M-058)

- The filter pill was `<viewport-cropped matches> / <corpus size>` — two
  different questions joined by a slash, so the same filter read
  46 / 46 / 14 / 13 / 0 depending only on where the map sat. It now reads
  命中 N (matches across the whole corpus) · 视野内 M (what is on screen), and
  the FAB's `aria-label` says the same. `.ff-total` keeps carrying the corpus
  size inside the filter sheet. (M-022, M-087; M-107 was not reproducible —
  the "27% hidden by default" figure was the viewport crop, the real number
  is 1,638 = 16.7%)
- Search: a query that is entirely a place name ("新宿", "京都") puts the
  地图搜索 section above 餐厅库 and ranks restaurants that are *in* that place
  above ones merely named after it. The ward used to be the 12th clickable
  row, behind 11 restaurants — the first of them in Osaka. (M-024)
- Search: famous neighbourhood names (池袋 / 六本木 / 秋葉原 / 難波 / 銀座 …,
  ~60 of them) are recognised as locations. `KNOWN_LOCS` was built only from
  the 都道府県/市/区/町/村 prefix of an address, so "拉面 池袋" was glued into
  one restaurant-name query and returned nothing. (BUG-07)
- Search: clicking a map result no longer always lands at
  `max(currentZoom, 16)`. The result's bounding box and address type decide —
  a city fits its bounds capped at z12, a ward z14, a POI still z16 — so
  searching for something bigger can finally zoom the map out. City-level
  results no longer pop a latitude/longitude bubble. (M-026)
- Search: the keyboard highlight and the row Enter actually opens can no
  longer disagree. `ssResetActive` clears the `.ss-active` class it used to
  leave painted, and an API-only re-render puts the highlight back on the same
  row instead of silently moving it. Row order for the arrow keys follows what
  is on screen, not DOM order. (BUG-04)
- A cookie whose signature is not valid base64url returned 500 (`atob` threw
  outside the try block) instead of 401. `verifyJWT` is fully guarded, the
  router has an outer catch, and error responses now carry CORS headers — a
  500 used to reach the browser as an unreadable network error. (M-127)
- A JSON primitive or array request body returned 500 and, for arrays, was
  stored verbatim (losing the version counter). Now 400 with nothing
  written. (M-127)
- One bookmark with a non-string `emoji` no longer travels to every device:
  the entry is dropped server-side, the rest of the array survives. The
  deployed client throws on such an entry and wipes the whole bookmarks
  layer, built-in pins included. (M-042)
- An unparseable stored blob is returned as `{}` on `GET` and in the 409
  body, so the client voids its base, unions and re-uploads instead of
  getting stuck permanently dirty. (M-129)
- A failed `KV.put` (1 write/s/key, or the daily quota) answers 429/503 with
  `Retry-After` and CORS headers instead of an uncaught 500. (M-040)
- Turning the transit layer off gives the memory back. The parsed LOD data,
  the spatial index and the feature arrays are dropped on `onRemove`, and the
  parser no longer retains the source GeoJSON features (lines keep only their
  derived geometry/style, stations a flat six-field record). Measured on the
  fold inner screen: 17.1 MB baseline → 31.1 MB with the layer → 17.2 MB after
  closing it, where it used to stay at 43.1 MB forever. In-flight LOD requests
  are aborted on close and when a zoom change moves the target elsewhere.
  (M-010)
- The locate plugin is no longer a boot dependency. `initMap` used to refuse
  to start until `L.control.locate` existed, so one optional file — one FAB —
  could hold the entire page: ~10 s of retries, then the red 地图组件加载失败
  banner and a permanent stop, with no markers, no filters and no search. The
  control is now built behind a capability check; if the file never arrives
  the locate FAB is hidden (rather than left dead) and the plugin is picked up
  later from the script's own load event or a bounded 10 s poll. (BUG-01)
- `<link rel="preload" href="data/restaurants.json" as="fetch">` gained
  `crossorigin`. Without it the preload is a no-cors/include request while
  `boot()`'s `fetch()` is cors/same-origin — different keys, so the preloaded
  response sat unclaimed. (M-139)

- Release QA (2.0.0 gate): on the phone layouts the first-visit value bar
  sat at the same z-index as the FAB stack, and an open bottom sheet lifts
  that stack (M-071) straight into the bar's band — on a 416x657 outer
  screen every tap on 定位 / 图层 landed on the bar instead. The bar now
  yields while a bottom sheet is open and returns when it closes; it is
  only marked "seen" by the user's own × or CTA. (M-109 × M-071)
- Release QA: a sub-collection or pin named by the user was run through
  the translation table in the 收藏 tab — a list called 京都美食 showed as
  "Kyoto eats" on the EN page while the card chip kept the real name. Both
  the group header and the pin row now carry the `lang="ja"` "user text"
  sentinel like every other user-named node. (M-098)
- Release QA: the un-saved card button read "☆ Saved" in English (and
  "☆ お気に入り" in Japanese) because it reused the noun run 收藏. It now
  uses the verb entry 加入收藏 → "☆ Save" / "☆ お気に入りに追加" /
  "☆ 加入收藏"; the saved state is unchanged. (M-106)

### Security

- Signing out is now a real revocation: `DELETE /api/session` bumps
  `sv:<sub>`, which invalidates cookies minted before it. Cookies issued
  before this deploy carry no `sv` and stay valid, as intended. (M-037)
- The session JWT no longer carries email, display name or avatar URL, and is
  no longer sent to every `*.jpfoodmap.com` host. (M-037)
- A malformed `id_token` is rejected before the outbound call to Google, and
  that call has a 5 s timeout. (M-040)
- No third-party script executes on this origin any more except Google
  Sign-In. Four unpinned, SRI-less CDN dependencies could each read
  `localStorage['tabelog.auth']` and talk to `/api/state` with the session
  cookie; they now come from `/vendor/`. cdnjs going dark used to be a white
  screen (`L.markerClusterGroup is not a function`, zero markers, the counter
  stuck on 加载中…) — reproduced with a DNS black hole, and the same test now
  renders the map normally. Two CDNs also stopped seeing every visitor's IP.
  (M-006, M-011)
- Security response headers on every response: `Content-Security-Policy-
  Report-Only` (an explicit allowlist plus `base-uri 'none'`,
  `object-src 'none'`, `frame-ancestors 'self'`), `X-Frame-Options`,
  `Permissions-Policy` (geolocation only for us, camera and microphone off)
  and HSTS. HSTS is deliberately `max-age=86400` with **no** `preload` — a
  short, reversible first step. The CSP is Report-Only for two weeks before
  it is enforced; a full pass over the UI (search, cards, the emoji picker,
  the sign-in button, offline boot) reports zero violations. (M-131)
- `/data/*` answers with `Access-Control-Allow-Origin: https://jpfoodmap.com`
  rather than leaving the corpus readable by any origin. The page fetches it
  same-origin, which skips the CORS check entirely. (M-131)
- `docs/transit-test.html` deleted — a dev preview page that loaded Leaflet
  from unpkg (a fourth supply-chain entry point) and pointed at a GeoJSON
  file that moved to R2 long ago. (M-135)

### Deferred

Not in this release, deliberately — each needs a network call this milestone
did not make.

- Re-checking `businessStatus` against Google (`--refresh-status` plus a TTL
  on `data/tabelog/google_places.csv`). The 6 + 9 closures shipped here come
  from the enrichment CSV as it already stands, so the labels are only as
  fresh as that file. (M-093)
- 23 rows have a Japanese reservation policy but an empty Chinese
  translation; filling them needs `translate_policies.py --target zh-CN`.
  (M-181)
- Re-scraping the corpus (M-096) and the Google enrichment back-fill for the
  ~5,300 rows still on GSI coordinates (M-092) — which is also what would
  shrink the 727 `approx` rows.
