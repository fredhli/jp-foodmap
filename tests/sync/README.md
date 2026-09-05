# tests/sync — multi-context sync tests

End-to-end tests for the browser-side sync engine in `src/tabelog/scrape/map.py`
(the `pull` / `push` / `saveCache` / `saveBookmarks` / `reconcile` block).
They run the **built** page (`docs/index.html`) in headless Chromium against
an in-process fake of `worker/src/index.js`, with two pages sharing one
`BrowserContext` (= two tabs of one browser) or two contexts (= two devices).

Nothing here touches production: the page's API base is rewritten to the
local fake on the fly, `accounts.google.com` and every third-party host are
blocked, and auth is seeded straight into `localStorage`.

## Run

```
uv run python src/tabelog/scrape/map.py      # build first (tests use docs/index.html)
uv run python tests/sync/run_all.py          # all scenarios (~8 min)
uv run python tests/sync/run_all.py g h      # a subset, by letter
```

Env: `SYNC_TEST_PORT` (default 8921), `SYNC_TEST_OUT` (evidence + screenshots,
default `audit_outputs/impl-2026-09-05/m1/sync-layer/`), `SYNC_TEST_SCRATCH`
(CDN cache + fixtures, default `~/.cache/jpfoodmap-sync-tests/`). Leaflet &
co. are downloaded once into the cache; after that the tests are offline.

Exit code 1 on any failing case; per-case evidence JSON lands in
`<OUT>/evidence/`, screenshots in `<OUT>/shots/`, and `<OUT>/summary.json`
lists failures per scenario.

## Scenarios

| file | what it proves |
|---|---|
| `scen_g_syncbase.py` | **M-001 (P0)**: a stale tab / a fresh boot can no longer push a list that deletes another tab's synced entry (G1 offline, G2 closed inside the debounce, G3 single-tab control, G4 a pre-2.0 inconsistent disk pair) |
| `scen_a_anon.py` | **M-003**: anonymous 4×6 action matrix across two tabs — nothing lost, and an un-favourite is **not** resurrected by the other tab (three-way merge, not union) |
| `scen_b_signedin.py` | **M-004**: signed-in two-tab cases (failed push next to a successful one, dirty vs. pull, import, pins, hide-builtin); **M-002 phase 0**: two devices racing inside the Worker's KV window — the loser detects its version with a foreign write id `w` and re-pushes |
| `scen_h_signout.py` | **M-041 / M-124**: sign-out in one tab is announced in the other and stops uploads; sign-in in one tab gives the other a proper base; account switch on a shared browser takes the cloud copy and never uploads the previous account's data; "also clear this device" on sign-out |
| `scen_i_misc.py` | **M-045** keepalive flush on hide, **M-047/M-138** storage-event propagation with zero PUTs from the receiver, **M-046** blocked localStorage, **M-053** dirty device keeps pulling + 413 message |

`mc_lib.py` is the shared plumbing (site + fake Worker on one origin, page
driver helpers `__mcTapFav` / `__mcAddBookmark` / `__mcGoHidden` / …,
console-error capture per tab); `fake_worker.py` is a faithful port of the
Worker's versioned PUT with an adjustable KV get→put window.
