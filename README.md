# jp-foodmap

A personal interactive map of restaurants and landmarks across Japan, live at
[jpfoodmap.com](https://jpfoodmap.com). A Python pipeline scrapes Tabelog,
geocodes the addresses, and bakes everything into one static page under
`docs/`, served by Cloudflare Pages from `main` / `/docs`.

Per-user Saved restaurants, the Hidden list, pins and lists sync through a
small Cloudflare Worker (`worker/`) behind Google Sign-In. Visitors who skip
sign-in keep their state purely in `localStorage`.

**Version: 4.1.3.** See [CHANGELOG.md](CHANGELOG.md) for what changed, and
[CLAUDE.md](CLAUDE.md) for the architecture notes, the storage-key contract
and the backwards-compatibility red lines.

The About sheet reports two different data dates: the historical corpus
baseline and the latest valid row-level timestamp from a recent partial
scrape. Rows without their own timestamp do not inherit that newer date.

## Install on a phone

The site is an installable Progressive Web App with a standalone window,
launcher icon, and an offline-cached application shell.

- Android Chrome: open `jpfoodmap.com` → menu → **Install app**.
- iPhone/iPad Safari: open `jpfoodmap.com` → Share → **Add to Home Screen**.

The restaurant database, default-language popup data, and transit renderer
are warmed by the service worker. Live map tiles, place search, and cloud
sync still require a network connection.

On a phone, the main destinations are always reachable from the text bottom
bar: **Map**, **Results**, **Saved**, and **Filters**. The area and nearby
controls stay separate so a nearby search can return to the planned area.

## Android APP

`android/` holds a Kotlin WebView shell around this site, shipped as a
sideloaded APK (`android/apk/jpfoodmap.apk`, carried to the phone by
Dropbox — the APK is gitignored, the `BUILD-INFO.txt` stamp beside it is
not). The shell is **3.2.3**, `versionCode 30203`, and loads the current site;
`minSdk 31`, `targetSdk 36`, built for one device (Galaxy Z Fold 8). The 4.1.3
website update requires no new APK because it changes no native setting.

What the shell adds over the PWA: the page survives a fold/unfold without
reloading, `jpfoodmap.com` links open in it (App Links), sign-in goes
through Android's Credential Manager because Google refuses web sign-in
inside a WebView, off-site links open in a Custom Tab that Back returns
from, sharing raises the system sheet, and JSON import/export uses Android's
system file picker. Everything else is the site itself: the shell transfers
the selected JSON bytes but does not interpret or mutate Saved restaurants,
Hidden entries, pins, lists, language, or other page-owned state.

```bash
cd android && ./build.sh          # → apk/jpfoodmap.apk + apk/BUILD-INFO.txt
```

Install steps, the one-time Google Cloud Console step that sign-in needs,
and the on-phone checklist are in [android/README.md](android/README.md);
the acceptance harness is `android/tools/VERIFY.md` and the per-task state
is `android/docs/STATUS.md`.

Two things outside `android/` belong to the app: `docs/.well-known/
assetlinks.json` (App Links verification, deployed with the site) and one
guarded `// ===== APP BRIDGE (Android shell) =====` block in
`src/tabelog/scrape/map.py`, which changes nothing in a browser.

## Install / run locally

Python 3.13, managed by [`uv`](https://docs.astral.sh/uv/). No JS build step
and no bundler — the front end is hand-written JS emitted by `map.py`.

```bash
uv sync                                   # create .venv from pyproject/uv.lock
cp .env.example .env                      # then fill in the keys below
uv run python scripts/fetch_vendor.py     # once: populate docs/vendor/
```

`.env` needs exactly one key, `CARTO_BASEMAP_API_KEY`: the basemap tile URL
is baked into the page at build time, and `map.py` raises rather than shipping
a page with no tiles. (The Google OAuth client id is a public constant in
`map.py`, not a secret — nothing else needs an environment variable.)

Then build:

```bash
flock /tmp/tabelog-build.lock uv run python src/tabelog/scrape/map.py
uv run python scripts/verify_build.py
```

The `flock` is not decoration: the build rewrites `docs/index.html`,
`docs/sw.js` and `docs/data/*.json` in place, and two builds at once
interleave their writes. It takes 1-2 minutes.

The build only geocodes rows whose `lat`/`lon` are blank (GSI AddressSearch,
cached in `data/cache/geocode_cache.json`). Pass `--fillall` only when you
really want to re-geocode everything — it takes about an hour and burns GSI
quota. Genre buckets, marker styling and every HTML/JS template are rebuilt
from scratch on every run, so editing a template and re-running is always
enough to see the change.

If you added a new emoji to `GENRE_EMOJI` or to any committed JSON, pre-cache
its Apple-style PNG first:

```bash
uv run python src/tabelog/scrape/build_emoji_cache.py
```

`data/` is mostly gitignored — only `data/favorites_builtin.json` and
`data/i18n/{en,ja}.json` are committed, because they are build-time inputs
and the page has to be reproducible from a fresh clone.

## Tests

No framework, no CI: every suite is a plain script run from the repo root.
Full details, including how to add a case, are in
[`tests/README.md`](tests/README.md).

```bash
# build first — compat, smoke and verify_build all read docs/
flock /tmp/tabelog-build.lock uv run python src/tabelog/scrape/map.py

uv run python scripts/verify_build.py   # 11 build-output contracts
uv run python tests/pipeline/run.py     # scrape → CSV → geocode → publish
uv run python tests/compat/run.py       # pre-2.0 localStorage still loads
uv run python tests/smoke_playwright.py # the built page on 5 viewports
node tests/worker/run.mjs               # the Worker against an in-memory KV
uv run python tests/sync/run_all.py     # sync state machine vs a fake Worker
```

- **`scripts/verify_build.py`** reads the build output and asserts what the
  front end silently assumes: the four `popups-*.json` variants agree on
  their URLs and slot count, `restaurants.json` has not silently shrunk and
  every coordinate is inside Japan, every load-bearing `localStorage` key
  name is still present in the page, the sub-collection invariants hold, the
  service worker's cache allowlist still names all four popup variants, the
  manifest's install identity is unchanged, the About sheet carries the real
  version, and the count of untranslated Chinese UI strings has not grown
  past the recorded baseline. It exits non-zero on the first violation.
- **`tests/pipeline/run.py`** works on copies of the corpus in a temp dir and
  never touches `data/tabelog/tabelog.csv`. Two checks skip on a fresh clone
  where `data/` is absent.
- **`tests/compat/run.py`** replays snapshots of pre-2.0 `localStorage` from
  `tests/compat/fixtures/*.json` against the current build. This is the
  executable form of the cardinal rule below.
- **`tests/smoke_playwright.py`** boots the built page on eleven viewports,
  including the measured Fold windows at 475×751, 932×704 and 591×689,
  iPhone widths 375/393/430, desktop widths 1000/1440 and the older Fold
  samples. It fails on console errors outside the offline allowlist.
- **`tests/feature_retention_playwright.py`** exercises the existing feature
  entries and collection operations. **`tests/ux/`** covers the planning
  flow, detail return and constrained viewports in Chromium or WebKit;
  **`tests/reliability/`** injects network and storage failures.
- **`tests/worker/run.mjs`** and **`tests/sync/run_all.py`** run against an
  in-memory KV mock and a fake Worker on localhost respectively. **Never
  point either at `api.jpfoodmap.com`** — that KV holds real user state.

## Backing up user data

The user-facing backup path is the avatar menu → **导出 favorites.json**
(M-005). It writes a single JSON file containing the Saved list, the Hidden
list, and every pin / list / list-membership entry. On Android 3.1.0 the
system document picker chooses the destination; import uses the system file
picker and is limited to a 2 MiB JSON file. The page validates the complete
known structure before committing an import and preserves compatible future
fields, but there is no import undo.

The Android WebView keeps local page data in the app's private storage.
Anything that has not reached sync or been exported to a chosen document is
lost when the app is uninstalled. Signing in again can retrieve the current
KV value; it cannot recreate a local-only edit or an older server snapshot.
There is no server-side snapshot, per-user KV history, or admin export, so
export before a big change.

On the build side, `src/tabelog/paths.py` routes every write through
`atomic_write_*` (write to `.tmp`, `os.replace` into place) and keeps one
generation of the corpus as `data/tabelog/tabelog.csv.prev` (M-020). An
exception mid-write therefore leaves the previous file byte-identical rather
than truncated, and `tests/pipeline/run.py` pins that behaviour down.

## Deploy

Pushing `main` publishes the site. Cloudflare Pages serves `/docs` and
rebuilds in about a minute. `docs/_headers` sets the cache rules (immutable
`/vendor/*` and emoji PNGs, no-cache `sw.js`) and is Cloudflare-Pages
specific — it silently no-ops on any other host.

```bash
flock /tmp/tabelog-build.lock uv run python src/tabelog/scrape/map.py
uv run python scripts/verify_build.py
git add docs src/tabelog/scrape/map.py
git commit -m "rebuild map"
git push
```

The sync Worker deploys separately, and is **not** part of the Pages deploy:

```bash
cd worker
npx wrangler deploy
```

### Rolling back

- **The site:** Cloudflare Pages → the project → *Deployments* → pick the
  last good deployment → **Rollback**. This is instant and does not need a
  git revert; do the revert afterwards so the next push does not re-ship the
  bad build.
- **The Worker:** Cloudflare Workers → the worker → *Deployments* → the
  previous version → **Rollback**. Worker and page roll back independently,
  so check the compatibility matrix in `CLAUDE.md` before rolling back only
  one of them: an old page must keep working against a new Worker and vice
  versa.
- A release tag marks the exact tree a deployment came from.

## Sync

Sign in with Google from the avatar menu. The Worker at `api.jpfoodmap.com`
verifies the ID token, sets an `HttpOnly` session cookie, and persists state
to Cloudflare KV keyed by the Google `sub`. `GET`/`PUT /api/state` carry a
`v` / `baseV` version: when the Worker observes a changed base it returns
409 and the client performs a three-way merge. An uncertain PUT is recorded
under `tabelog.pendingWrite` before it is sent, then reconciled by bounded
readback without inventing a new write identity.

Tabs share the pending write's ownership and retry budget. Where Web Locks
is available, a short browser-local lock coordinates claims and cleanup;
network requests run after that lock is released. Without Web Locks, readback
and merging continue, but the uncertain body is not automatically replayed.

Cloudflare KV is eventually consistent and does not provide compare-and-set.
Two devices can therefore read the same visible version and a later write can
still overwrite an earlier one without a 409. Sync reduces that risk and
keeps ambiguous changes local, but it is not a substitute for JSON backups.
There is no setup beyond clicking sign-in.
