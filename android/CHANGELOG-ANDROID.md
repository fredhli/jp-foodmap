# Changelog — Japan Foodmap for Android

The APK's own history. The website has its own `CHANGELOG.md` at the repo root; this file
only records what changed in the Android shell. Version numbers are kept in step with the
site: an APK labelled 2.0.0 wraps the 2.0.0 site.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
`versionCode` = major×10000 + minor×100 + patch, so it can be derived from the name and
always increases.

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
