# Changelog — Japan Foodmap for Android

The APK's own history. The website has its own `CHANGELOG.md` at the repo root; this file
only records what changed in the Android shell. Version numbers are kept in step with the
site: an APK labelled 2.0.0 wraps the 2.0.0 site.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
`versionCode` = major×10000 + minor×100 + patch, so it can be derived from the name and
always increases.

## [2.2.0] - 2026-09-07 · `versionCode 20200`

Ships with the 2.2.0 site. One entry, and it is about a bug report that turned out to be a
gate hole rather than a shell defect.

### Fixed

- **The back acceptance can finally go red on the APK that ships.** INTEGRATE-1 §6 reported
  that the system back key leaves the app instead of closing the open card, and the reason
  the gate had never caught it was that `tools/verify-flows.sh`'s back segment hung entirely
  off `open_card`, whose only rung a release build can reach is the cold `?r=` deep link —
  and every line that looked at the result read the DOM over the DevTools endpoint, which a
  release build does not publish. So the flow measured one path and then reported it as
  SKIP. The segment is now three, and every assertion in all three reads only
  `dumpsys window` and the shell's own diagnostics line.

  What the measurements say (`audit_outputs/2.2.0-fix/impl/android-back.md`): an overlay the
  user opened **with a finger** already pops one layer per press, on the unmodified 2.1.0
  shell — card, photo lightbox, results drawer, and the filter sheet stacked on top of the
  drawer, in that order, and only then does the app close. What does not pop is an overlay
  the **page** opened with no user activation, which is what a `?r=` deep link does: Chromium
  marks the entry underneath such a `pushState` `skip_on_back_forward_ui`, `canGoBack()`
  answers false with an entry right there, and the press falls through to the system. That is
  the same thing Chrome does with the same URL in a fresh tab. No WebView API walks past such
  an entry — `canGoBackOrForward(-1)` is false as well, and a forced `goBackOrForward(-1)`
  moves nothing and silently eats the press — so the shell's `canGoBack()` test is kept
  exactly as it was, now with the measurement written beside it.

- **…and it goes red in the DEFAULT run, not just when told which diagnostics source to
  use.** The first cut of the segment above still reported `SKIP back stack readings` on a
  plain `./verify-flows.sh back` against the release APK, in wording that claimed the build
  was pre-2.2.0. Both verify scripts choose native-vs-probe once, seconds after the install
  and before the app has ever reached READY, with an 8s budget that a cold start on this
  emulator image regularly overruns — so a 2.2.0 APK gets labelled "probe", and the probe
  half is the page's own view, which has no `back` block whether or not a DevTools endpoint
  exists. `back_state()` now retries once with `JPFM_DIAG_SOURCE=native` forced before it
  gives up, latches the answer for the rest of the run, and only calls a build pre-2.2.0
  when `native` itself answered without a `back` block. Measured on the release APK from a
  cold-booted emulator: 5 PASS / 0 FAIL / 0 SKIP where the same run used to report
  1 PASS / 2 SKIP, and a shell patched to leave `backCallback` disabled still goes
  2 PASS / 2 FAIL / 0 SKIP on that same default path.

### Added

- **`back` in the diagnostics JSON** — `enabled`, `canGoBack`, `index`, `size`. The
  acceptance needs a witness for "an overlay is open" that survives a release build, and
  everything else that could see one goes through the DevTools probe. Diagnostics only: no
  behaviour changed, and the block is read by `tools/verify-flows.sh` through
  `diag.sh get … back.index`.

## [2.1.0] - 2026-09-07 · `versionCode 20100`

Ships with the 2.1.0 site. Three shell-visible fixes out of Fred's 2.0.0 bug report, plus a
tooling fix that had been quietly turning a release-APK acceptance run into a partial one.

### Fixed

- **The site's top bar no longer sits under the status bar on the inner screen** (bug A-1).
  The fix is in the page — its fixed top bar now pads itself by
  `max(env(safe-area-inset-top), var(--app-inset-top))` — and the shell supplies the second
  half of that `max()`: every time the window insets change (a fold, a rotation, a
  multi-window resize) it writes the status-bar inset, converted to CSS px, into the page as
  `--app-inset-top`, and rewrites it at first paint of every document. The insets are still
  passed through and never consumed, so `env()` remains the primary source; the variable only
  matters on a build that reports 0 for `safe-area-inset-top` under a status bar that is
  really there. Where both are right they are equal and `max()` pads once. Measured on the
  emulator: 24 px from both, and 48 px from both with a display cutout switched on.
- **The launcher icon is no longer blown up** (bug A-2). `tools/gen-launcher-icon.py` insets
  the artwork to 47.5 % of the 108dp adaptive canvas (`FG_SCALE = 0.76`) instead of drawing
  the site's icon at full canvas size, which had put it at 62.5 % — filling ~82 % of the
  visible circle and letting a round mask slice the map glyph's white border off. It now
  measures the same as the PWA icon of the same site on the same launcher, which is the
  comparison Fred asked for. The splash screen keeps the full-size artwork: it has its own
  bitmap (`ic_splash_foreground`) out of the same script and the same source.
- **Blue boxes when tapping a button** (bug A-3) are gone, fixed on the page with one global
  `-webkit-tap-highlight-color: transparent`. Android WebView's default for that property is
  Holo blue and rectangular; Chrome's is a faint grey, which is why it had never been visible
  outside the app. No shell change.
- **`emu.sh diag` addressed the wrong package on a release install**, and the activity
  manager answers a launch at a package that is not installed with a quiet `result code=-92`.
  Both `tools/emu.sh` and `tools/diag.sh` defaulted `JPFM_PKG` to the `.debug` id, so a
  hand-installed release APK produced no diagnostics line at all — which was read during the
  2.0.0 audit as "R8 strips the `JpfmDiag` log from release builds". It does not: there is no
  `-assumenosideeffects` for `android.util.Log` anywhere in the merged R8 configuration, and
  the tag survives in the release dex. The two scripts now ask the device which of the two
  ids is installed (`JPFM_PKG` still overrides, which is how `lib-verify.sh` passes down what
  `aapt2` read off the APK under test). `verify-geometry.sh` / `verify-flows.sh` were never
  affected — they detect the package from the APK — so their `imeMode` / `pageLoads` /
  `activityCreates` assertions were real, not silent SKIPs.

### Changed

- The diagnostics field `safeVar` used to be hard-coded `false` "for shape"; it now reports
  whether the shell has pushed `--app-inset-top` into the current document, and a new
  `appInsetTop` field carries the value in CSS px (`-1` before the first push). Compare it
  with the page half's `env.t`.

## [2.0.0] - 2026-09-06 · `versionCode 20000`

First release. A Kotlin WebView shell around `https://jpfoodmap.com/`, built for one phone
(Galaxy Z Fold 8) and sideloaded from Dropbox. `minSdk 31`, `compileSdk` = `targetSdk 36`,
release build R8-minified and signed with the same debug keystore as the dashboard app, so
it installs over itself for as long as that key lives.

### Added

- **The app itself.** One activity, one WebView, the site inside it. `singleTask`, a page
  state machine (NONE / LOADING / READY / ERROR) with cold / warm / hot routing for an
  incoming intent, a 10 s watchdog for a navigation Chromium never reports, a renderer-crash
  rebuild, and an error panel with Retry when the page cannot be reached.
- **The fold stays put.** Everything a fold produces is in the activity's `configChanges`,
  so closing or opening the phone reaches `onConfigurationChanged` instead of recreating the
  activity: the document is never reloaded, the open restaurant card stays open, the map
  keeps its position. Verified on the emulator's fold proxy — `activityCreates` 1, `pageLoads`
  unchanged, `bootId` unchanged across cover ↔ inner.
- **Three Fold 8 geometries.** The page is handed a true CSS width in each: 475 dp on the
  cover screen, 932 / 704 dp on the inner screen, and 591 / 688 CSS px in a 60 % split
  window — that last one is the only window where dp and CSS px differ, because 1808 px /
  2.625 = 688.76 rounds the window up to 689 dp and floors the layout viewport to 688. All
  at `devicePixelRatio` 2.625. Edge-to-edge with the insets passed through to the page's own
  `env(safe-area-inset-*)` padding rather than consumed by the shell.
- **App Links** for `jpfoodmap.com` (`autoVerify`, and `docs/.well-known/assetlinks.json`
  ships with the site). A `?r=<id>` share link opens that restaurant's card: cold start loads
  it, and a link arriving while the app is running switches the card through the page's
  `window.__jpfmOpenShare` without a navigation.
- **Native Google sign-in.** Google refuses web sign-in inside a WebView
  (`disallowed_useragent`), so the app asks Credential Manager for an ID token and hands it
  to the page, which exchanges it for its own 90-day session cookie exactly as the browser
  does. The Worker and the KV schema are untouched. The GIS script tag is intercepted and
  never fetched inside the app. **Needs a one-time Android OAuth client in Google Cloud
  Console (README → 真机清单 #4); until it exists the sign-in button reports a failure and
  offers "open in browser", and everything else works.**
- **The external-link ladder.** Links that leave the site (Tabelog, Google Maps) open in a
  Chrome Custom Tab by default — in this app's own task, so Back returns to the map and
  Recents never shows a second card. Settable to Chrome or the system default browser. A
  Google Maps URL is offered to the Maps app first.
- **System share.** The card's share button raises the system chooser with the `?r=` link,
  instead of the page's copy-to-clipboard fallback. Only the site's own URLs can be shared.
- **App settings** (launcher long-press → 设置, or the page's avatar menu → App settings):
  link policy, text size (follow system / 90 / 95 / 100 / 115 / 130 %), a notification switch
  with a test notification, "open the site in a browser", diagnostics, and the version row.
- **Diagnostics.** One screen and one logcat line (`JpfmDiag`) carrying the window geometry,
  `env()` insets, font scale and text zoom, WebView version, page-load and activity-create
  counters, and the last exit reasons. No cookie, no token, and the URL has its query
  stripped.
- **Notifications**: one channel (`jpfoodmap_general`), silent, one fixed-id test entry,
  the switch off by default. `POST_NOTIFICATIONS` is requested only when the switch is turned
  on, never at launch, and the switch springs back if the grant is refused. There is no push
  server in this version and no FCM in the APK.
- **Location** is requested only when the page's locate FAB asks for it, scoped to the site's
  own origin, and never at startup.
- **Offline**: the site's own service worker is what serves a cold start without a network;
  the shell adds nothing to it and caches nothing itself.
- **Build pipeline**: `build.sh` rsyncs the sources to an ext4 scratch, builds there, and
  copies back `apk/jpfoodmap.apk` plus a committed `apk/BUILD-INFO.txt` stamp
  (version, size, sha256) so "did the new build reach the phone?" is answerable from Dropbox.
- **Acceptance harness**: `tools/verify-geometry.sh` (three windows, both orientations, the
  fold proxy) and `tools/verify-flows.sh` (back key, deep links, external links, share,
  offline, location, night mode, font scale, sign-in degradation), plus
  `tools/static-audit.sh` (36 hardening assertions) and `tools/power-audit.sh` (13 battery
  assertions including a 5-minute background soak). 106 JVM unit tests.

### Fixed before release (2026-09-06, after the acceptance run)

- **The site's own offline banner works inside the app.** The shell now declares
  `ACCESS_NETWORK_STATE`, the permission Chromium's `NetworkChangeNotifierAutoDetect` needs
  before it registers a connectivity callback; without it `navigator.onLine` inside a WebView
  is stuck at `true`, so the page never learns it is offline and its "当前离线 · 显示的是本机
  缓存的数据" bar never appears. No code of the app's own uses the permission, it is
  `normal`-level (install-time grant, no prompt, absent from the system permission page), and
  it reveals only whether a network exists and of what kind. The declared set is five, not
  four; `docs/STANDARDS.md` §10.3a records the reasoning and the exact way to back it out.
- **The error panel no longer covers a page the service worker rescued.** Offline, Chromium
  reports `ERR_INTERNET_DISCONNECTED` for the main frame and only *then* does the site's
  service worker answer the same navigation out of its cache — and both finish under the same
  URL, so the shell used to leave its "page cannot be opened" panel over a perfectly good map.
  `onPageFinished` now asks the document itself whether it committed and whether it is under
  the service worker's control (`resolveRescuedPage`); Chromium's own error page is an
  internal document with no controller, so the no-cache path still gets the panel. The panel
  is also armed rather than shown, and revealed 1.2 s later, so an offline launch does not
  flash an error before the map arrives.

### Website changes that ride with it

- `src/tabelog/scrape/map.py` gained one guarded `// ===== APP BRIDGE (Android shell) =====`
  block: inside the app it hides the PWA install prompts (the shell *is* the install),
  replaces the Google sign-in button with the native one, routes sharing through the system
  sheet, adds an "App settings" row to the avatar menu, and clears the native credential on
  sign-out. In a browser the block adds two side-effect-free globals and returns on its third
  line. No new localStorage key, no change to the KV blob, the Worker API or the built-in
  landmark ids.
- `docs/.well-known/assetlinks.json` — the App Links verification file, naming
  `com.fredhli.jpfoodmap` and `com.fredhli.jpfoodmap.debug` under the same certificate
  fingerprint.

### Known limits in 2.0.0

- Sign-in cannot complete until the Android OAuth client exists in Google Cloud Console.
- ~~The page's own "offline" banner never appears inside the app.~~ Fixed on 2026-09-06 by
  declaring `ACCESS_NETWORK_STATE`, the permission Chromium's `NetworkChangeNotifierAutoDetect`
  needs before it will register a connectivity callback; without it `navigator.onLine` inside
  a WebView stays `true` forever. It is a `normal`-level permission — granted at install,
  never prompted, absent from the system permission page — and no line of the app's own code
  uses it. The declared set is now five, not four; `STANDARDS.md` §10.3/§10.3a,
  `PLAN.md` gate_static #1, `tools/static-audit.sh` and `tools/power-audit.sh` say so.
- No push notifications (no server to send them).
- Dark mode is not implemented: the site is `color-scheme: only light` and the shell forces
  a light window to match.
- Only the Fold 8 is targeted. Other phones will run it, but nothing about their geometry has
  been checked.
- The emulator cannot answer four things; they are listed as 真机待验 in `docs/STATUS.md` —
  a real fold (rather than a `wm size` proxy), real inset values, a real Google account, and
  App Links auto-verification against the live site.
