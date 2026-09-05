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

### Added

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

### Changed

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

### Fixed

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
  to overprint it. (M-071, AUTO-05, AUTO-12)

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
