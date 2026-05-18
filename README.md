# jp-foodmap

A personal interactive map of restaurants in the Kansai region. The static
page lives in `docs/` and is served via GitHub Pages.

Per-user favorites and dismissals sync through a GitHub Gist (configured
per-device, no shared secrets in the repo).

## Local build

```bash
uv sync
uv run python src/tabelog/scrape/map.py
open docs/index.html
```

The build step pulls geocodes via GSI AddressSearch (cached locally in
`data/cache/`) and embeds the payload directly into `docs/index.html`.

`data/` is gitignored — only `docs/index.html` is committed.

## Deploy

The repo is already wired up to GitHub Pages serving from `main` / `/docs`.

```bash
uv run python src/tabelog/scrape/map.py  # regenerate after data changes
git add docs/index.html
git commit -m "rebuild map"
git push
```

Pages rebuilds in ~1 minute.

## Sync (favorites + dismissals + bookmarks)

Each browser keeps a local copy of these lists and pushes diffs to a shared
secret GitHub Gist.

1. Create a **secret** gist on gist.github.com containing these files:
   - `favorites.json` — JSON array of detail URLs
   - `blacklist.json` — JSON array of detail URLs
   - `bookmarks.json` — JSON array of map pins (`{id, name, emoji, lat, lon}`)
     — optional; the page auto-creates it on first push if absent.
2. Create a fine-grained PAT at github.com/settings/tokens with **Gists →
   Read and write** only.
3. On the deployed page → ⚙️ 同步设置 → paste Gist ID + PAT → 保存并测试.

Read-only viewers can fill in just the Gist ID and leave the PAT blank.
