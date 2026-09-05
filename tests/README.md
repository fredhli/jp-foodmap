# tests/

Four suites, no framework, no CI yet — every one is a plain script you run
with `uv run python` (or `node` for the Worker). Run them from the repo root.

Before the 2.0.0 work there were no tests at all (audit M-056): the sync
state machine, the Worker's KV read-modify-write, and a 9,000-line generator
were all defended by nothing but a careful reader.

| directory | what it covers | needs |
|---|---|---|
| `tests/pipeline/` | scrape → CSV → geocode → publish, in-process | nothing (uses fixtures; skips the two checks that want the gitignored corpus) |
| `tests/compat/` | pre-2.0 `localStorage` still loads correctly | a build in `docs/`, Playwright Chromium |
| `tests/worker/` | the Cloudflare Worker: routing, auth, KV, CORS, versioning | `node` |
| `tests/sync/` | the browser-side sync state machine against a fake Worker | Playwright Chromium |
| `tests/smoke_playwright.py` | the built page on 4 viewports, console clean | a build in `docs/`, Playwright Chromium |

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
uv run python tests/smoke_playwright.py
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
uv run python tests/smoke_playwright.py
uv run python tests/smoke_playwright.py --viewport fold-outer
uv run python tests/smoke_playwright.py --screenshots /tmp/shots
```

Four viewports (Fold 8 outer 416×657, Fold 8 inner portrait 616×816,
iPhone 393×852, desktop 1440×900) × six things: boot, local search, detail
card, save/star, filter, account menu. A console error that is not on the
offline allowlist fails that viewport.

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
