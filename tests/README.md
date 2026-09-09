# tests/

These are plain-script suites with no shared framework or CI wrapper. Run
them from the repo root with the project Python (or `node` where shown).

Before the 2.0.0 work there were no tests at all (audit M-056): the sync
state machine, the Worker's KV read-modify-write, and a 9,000-line generator
were all defended by nothing but a careful reader.

| directory | what it covers | needs |
|---|---|---|
| `tests/pipeline/` | scrape → CSV → geocode → publish, in-process | nothing (uses fixtures; skips the two checks that want the gitignored corpus) |
| `tests/compat/` | pre-2.0 `localStorage` still loads correctly | a build in `docs/`, Playwright Chromium |
| `tests/worker/` | the Cloudflare Worker: routing, auth, KV, CORS, versioning | `node` |
| `tests/sync/` | the browser-side sync state machine against a fake Worker | Playwright Chromium |
| `tests/smoke_playwright.py` | the built page on phone, Fold and desktop viewports, console clean | a build in `docs/`, Playwright Chromium |
| `tests/feature_retention_playwright.py` | functional access paths for features outside the 3.1 UX demo | a build in `docs/`, Playwright Chromium or WebKit |
| `tests/ux/` | planning, navigation, detail return, short-viewport regressions, and the 3.2.0 map-visibility thresholds | a build in `docs/`, Playwright WebKit **and** Chromium |
| `tests/reliability/` | browser fault injection, resource deadlines, and the documented KV stale-read limit | a build in `docs/`; Node, with Playwright found from the project venv when needed |

And one checker that is not a test suite but belongs to the same gate:

| script | what it asserts |
|---|---|
| `scripts/verify_build.py` | build-output contracts: popup variant parity, row count / uniqueness / bbox, localStorage key names, service-worker cache allowlist, untranslated-string count |

## Running everything

```bash
# 1. build (needed by compat + smoke + verify_build)
flock /tmp/tabelog-build.lock uv run python src/tabelog/scrape/map.py

# 2. build contracts
uv run python scripts/verify_build.py

# 3. suites
uv run python tests/pipeline/run.py
uv run python tests/compat/run.py
uv run python tests/smoke_playwright.py                    # the Chromium viewports
uv run python tests/smoke_playwright.py --browser webkit   # the iPhone viewports
uv run python tests/feature_retention_playwright.py
.venv-wsl/bin/python tests/ux/run.py --docs docs --browser webkit --output /tmp/jpfoodmap-ux
.venv-wsl/bin/python tests/ux/run.py --docs docs --browser chromium --output /tmp/jpfoodmap-ux-cr
.venv-wsl/bin/python tests/ux/supplement.py --docs docs --browser webkit --output /tmp/jpfoodmap-ux-supplement
.venv-wsl/bin/python tests/ux/visibility.py --output /tmp/jpfoodmap-visibility
node tests/reliability/browser.mjs --built
node tests/reliability/resource-deadlines.mjs
node tests/reliability/kv-eventual.mjs
node tests/worker/run.mjs
uv run python tests/sync/run_all.py
```

Nothing here touches production. The Worker suite runs against an in-memory
KV mock, the sync suite against a fake Worker on localhost, and the browser
suites block `api.jpfoodmap.com` (plus Tabelog's photo CDN, emojicdn,
Google Translate and Nominatim) at the network layer. **Never point any of
these at `api.jpfoodmap.com`** — the real KV holds real user state.

## `tests/pipeline/` — data pipeline (M-019 / M-020 / M-094 / M-096 / M-172)

```bash
uv run python tests/pipeline/run.py           # all
uv run python tests/pipeline/run.py t_merge   # one, by function name
```

Imports the real `tabelog.scrape.scrape_all` and `tabelog.scrape.map`, works
on copies of the master CSV in a temp dir, and never writes to
`data/tabelog/tabelog.csv` or `data/cache/geocode_cache.json`. What it pins
down:

- a re-scraped row whose `address` came back empty is discarded, the old row
  survives, and the rejection lands in the failed ledger;
- an empty `lat`/`lon`/`photo*`/`reservation_policy_chinese` keeps the old
  value, while `awards` can still be cleared (no blanket merge);
- a batch with >5% empty addresses is refused outright — the master CSV is
  left byte-identical and the process exits non-zero;
- an exception mid-write leaves the original file intact, no stray `.tmp`,
  and a successful write leaves a `.prev`;
- the legacy `geocode_cache.json` shapes (`null` and a bare `{lat, lon}`)
  still resolve from cache — all 9,888 real entries, zero re-queries;
- a corrupt cache refuses to run instead of silently re-geocoding;
- `scraped_at` is additive: rows written before the column keep an empty
  value and the age printer tolerates both.

Two checks (`t_legacy_geocode_cache_compat`'s real-cache half and
`master_head`) SKIP on a fresh clone where `data/` is absent. Everything
else runs anywhere.

## `tests/compat/` — pre-2.0 localStorage

```bash
uv run python tests/compat/run.py        # all fixtures
uv run python tests/compat/run.py 03     # one, by filename fragment
```

Each JSON in `tests/compat/fixtures/` is a snapshot of what a real browser
held before this release, injected with `add_init_script` and asserted
against the current `docs/index.html`. This is the executable form of the
cardinal rule in `CLAUDE.md`: *a user who refreshes after a deploy sees
exactly the state they saw before it.* The fixtures cover the old
`filterState` shape (legacy `bookable` string, pre-M-016 checked lists), an
`omakase_state_cache_v2` with `dirty:true` that must not be cleared, a
`{id:'fb-…', category:'hidden'}` built-in tombstone, and a `tabelog.syncBase`
that must survive boot and a refresh.

Add a fixture whenever you change how a stored shape is read. Keep the
`__note__` line saying which release wrote it.

## `tests/smoke_playwright.py` — the built page

```bash
uv run python tests/smoke_playwright.py                    # the 8 Chromium rows
uv run python tests/smoke_playwright.py --browser webkit   # the 4 iPhone rows
uv run python tests/smoke_playwright.py --viewport fold-outer
uv run python tests/smoke_playwright.py --screenshots /tmp/shots
```

Twelve viewports retain the older Fold samples (416×657, 616×816, 591×689,
816×616), add the measured 3.1 Fold geometries (475×751, 932×704), cover
iPhone widths 375, 393, 402 and 430, and keep desktop 1000 and 1440.

**Every viewport declares the engine it is measured on and only runs under
that one** (M-3.2-11). The four iPhone rows — 402×874 included, the width
the 3.2.0 phone layout was designed against — are WebKit rows, because a
Chromium pass on those dimensions says nothing about iOS Safari; everything
else is Chromium. So the gate is two runs, and `--viewport` naming a row
from the other engine exits 2 rather than quietly running it on the wrong
one.

The phone checks (`seg-pill`) click the floating segmented pill that
replaced 3.1.x's `#phone-nav` bar: three reachable 44px segments that never
intersect the FAB column, each opening the overlay drawer on its own tab,
the pill standing down under the open drawer, one filter host, detail
return with focus, and the map still painted beside the drawer. The card
checks require Save / Maps to be reachable in `#bs-foot` from both the
first and a later marker detail. A console error that is not on the offline
allowlist fails that viewport.

## `tests/feature_retention_playwright.py` — features outside the UX demo

```bash
.venv-wsl/bin/python tests/feature_retention_playwright.py
.venv-wsl/bin/python tests/feature_retention_playwright.py --browser webkit --output /tmp/feature-retention
```

This reads `tests/fixtures/feature-dom-2.3.0.json` as the published 2.3
inventory, but static IDs are only prerequisites. It checks the complete region
and advanced-filter choices, persists a filter change and a two-restaurant
batch save, creates and renames collections, adds the same restaurants to two
collections, batch-removes them from one collection while preserving the
other membership and favourites, adds them back, copies list text through an
observed clipboard API, and deletes one collection without removing its
restaurants or the other membership.
It also checks access to all four map layers, custom bookmarks and attractions,
sharing, Tabelog, and account backup/import/privacy controls. Those latter
checks cover entry points, not full external-service operations. Interactions
use isolated browser state and every external HTTPS request is blocked.

## `tests/ux/` — flows, history and the 3.2.0 visibility thresholds

```bash
.venv-wsl/bin/python tests/ux/run.py --docs docs --browser webkit --output /tmp/ux-wk
.venv-wsl/bin/python tests/ux/run.py --docs docs --browser chromium --output /tmp/ux-cr
.venv-wsl/bin/python tests/ux/supplement.py --docs docs --browser webkit --output /tmp/ux-sup
.venv-wsl/bin/python tests/ux/review_regressions.py --docs docs --browser webkit --output /tmp/ux-rr
.venv-wsl/bin/python tests/ux/visibility.py --output /tmp/ux-vis
```

`run.py` walks the planning route on eight widths: pick a region, sort,
open a result, come back to the same scroll position and focused row, open
a Saved row, switch to nearby through the locate FAB and back through
`#ux-restore-plan`, and it now also pins the phone chrome that replaced
3.1.x's header buttons — the three-segment pill clear of the FAB column and
carrying the live count, and the `#ss-chips` row naming the region.
`review_regressions.py` covers place navigation, closed-detail focus and
the nearby-sort copy. `supplement.py` covers keyboard, resize and the
first-visit context menu.

`visibility.py` (M-3.2-11) is the executable form of the release's
acceptance numbers. It is the audit script
`audit_outputs/3.2.0-plan/ux-phone/run_flows.py` reduced to its pixel
sampler and pointed at 3.2.0's selectors — **use this, not that**: the
audit script still drives the deleted `#phone-nav` and needs two versions
of the site running on two ports. What it gates, per state and viewport:

| state | 402×874 (WebKit) | 475×751 | 591×689 |
|---|---|---|---|
| home (intro bar up) | map ≥ 78% | ≥ 78% | recorded |
| drawer open | ≥ 10% | recorded | ≥ 30% |
| detail open | ≥ 18% | ≥ 18% | ≥ 18% |

plus: no page-level horizontal overflow in any state, none of the strings
on the audit's truncation list (`table_E.md` — price ceilings, the booking
verdict, distances, the landmark count) clipped, and no full-width comma or
semicolon anywhere in the shipped EN / JA translation tables. A drawer
state counts the scrim as visible: the overlay drawer leaves the map
painted and legible behind `.18` of black, which is the whole point of
going back to it, while an opaque panel scores zero either way.

The history regression suite checks two-step source returns, close actions,
rapid reopen, and layout changes against the built page:

```bash
.venv-wsl/bin/python tests/ux/back_history.py --built --browser chromium --output /tmp/back-chromium
.venv-wsl/bin/python tests/ux/back_history.py --built --browser webkit --output /tmp/back-webkit
```

It observes navigation through a console probe installed before page load.
For Android system-Back checks, use real input and CDP observations with
`userGesture: false`: Playwright `page.evaluate` can change Chromium's
history-skipping behavior by injecting user activation.

## `tests/reliability/` — bounded failures and storage limits

```bash
node tests/reliability/browser.mjs --built
node tests/reliability/resource-deadlines.mjs
node tests/reliability/kv-eventual.mjs
```

`browser.mjs --built` injects hangs, partial bodies, status failures and
lost responses into the generated page while blocking real services. It
locates Playwright through the project Python environment when there is no
standalone Node package. It also runs the multi-tab `pending-tabs.mjs` cases
(R1-R5) at the end. Two rules those cases exist to keep honest, both from
the 3.1.2 hotfix: recovery must never be produced by a probe the page itself
would not call (R3 used to end with `__reliability.resetWait()`, so it proved
the harness could unstick the engine, not that the engine unsticks itself),
and R5 pins the one combination that wedged 3.1.1 — a server that never moves
plus PUTs that keep failing — by asserting a new edit still uploads twenty
minutes of virtual time later. `resource-deadlines.mjs` checks that popup and
transit downloads time out, release their in-flight state and can retry.
`kv-eventual.mjs` preserves the executable demonstration that Cloudflare KV
can serve a stale cross-region read and therefore cannot provide CAS.

## `tests/worker/` and `tests/sync/`

Owned by the Worker and sync-layer work; see their own READMEs
(`tests/worker/README.md`, `tests/sync/README.md`) for what each scenario
covers and how to add one. Short version: `node tests/worker/run.mjs` runs
the Worker against an in-memory KV mock plus a legacy-Worker fixture (so the
old-client × new-Worker matrix stays covered), and
`uv run python tests/sync/run_all.py` drives the real page against a fake
Worker to exercise anonymous mode, sign-in, sign-out, 409 merges and the
`tabelog.syncBase` contract.

## Conventions

- No pytest, no package.json, no new dependencies. Each suite is a script
  with a `main()` returning an exit code.
- Tests assert what the shipped code does and say so in a comment when the
  behaviour is a deliberate trade-off (e.g. the pre-M-016 checked-list path
  in compat fixture 01).
- Anything that reads `data/` must skip, not fail, when the file is absent —
  `data/` is gitignored and a fresh clone has none of it.
