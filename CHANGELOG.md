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

_Nothing yet._

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
