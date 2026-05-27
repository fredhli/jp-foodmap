"""
Human-review surface for google_enrich.py 'review' matches.

google_enrich classifies uncertain matches as 'review' rather than auto-
accepting them. This turns those rows into a self-contained HTML sheet you
click through — each card shows the Tabelog photo + name and links out to both
the Tabelog page and the exact Google place, so you can eyeball "same shop?"
in a second. Decisions are kept in the browser (localStorage), so you can stop
and resume. Export them, then apply back into google_places.csv.

Workflow:
  # 1. Build the review sheet (open the file in any browser — no server needed):
  uv run python src/tabelog/scrape/review_google.py
  #    -> data/tabelog/google_review.html

  # 2. Click ✓/✗ (or use j/k to move, a/r to decide). Hit "下载 decisions.csv".

  # 3. Apply: review -> accepted / rejected in google_places.csv:
  uv run python src/tabelog/scrape/review_google.py --apply ~/Downloads/decisions.csv

  # 4. Re-run map.py so the newly-accepted rows get calibrated into the page.

map.py picks up 'accepted' rows; 'rejected' rows are left out, and google_enrich
treats 'rejected' as terminal so a later re-run won't re-fetch and undo you.
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tabelog.paths import GOOGLE_PLACES_CSV, GOOGLE_REVIEW_HTML, TABELOG_CSV


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_review_rows() -> list[dict]:
    with GOOGLE_PLACES_CSV.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if (r.get("status") or "") == "review"]


def load_tabelog_extras() -> dict[str, dict]:
    """detail_url -> photo / genre / station, for richer review cards."""
    extras: dict[str, dict] = {}
    if not TABELOG_CSV.exists():
        return extras
    with TABELOG_CSV.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            url = r.get("detail_url") or ""
            if url:
                extras[url] = {
                    "photo": r.get("photo1_url") or "",
                    "genre": r.get("genre") or "",
                    "station": r.get("station") or "",
                }
    return extras


def gmaps_link(name: str, addr: str, place_id: str) -> str:
    q = quote(f"{name} {addr}".strip())
    url = f"https://www.google.com/maps/search/?api=1&query={q}"
    if place_id:
        url += "&query_place_id=" + quote(place_id)
    return url


def build_html(rows: list[dict], extras: dict[str, dict]) -> str:
    data = []
    for r in rows:
        url = r.get("detail_url") or ""
        ex = extras.get(url, {})
        photo = ex.get("photo", "")
        thumb = photo.replace("640x640_rect_", "150x150_square_") if photo else ""
        data.append({
            "url": url,
            "region": r.get("region", ""),
            "tname": r.get("tabelog_name", ""),
            "gname": r.get("g_name", ""),
            "dist": r.get("dist_m", ""),
            "sim": r.get("name_sim", ""),
            "taddr": r.get("tabelog_address", ""),
            "gaddr": r.get("g_address_ja", "") or r.get("g_address_en", ""),
            "genre": ex.get("genre", ""),
            "station": ex.get("station", ""),
            "thumb": thumb,
            "gmaps": gmaps_link(
                r.get("tabelog_name", ""), r.get("tabelog_address", ""),
                r.get("place_id", ""),
            ),
            "notes": r.get("notes", ""),
        })
    # Nearest-first: the dist<=50m rows are near-certain accepts, so the easy
    # ones cluster at the top and you build momentum before the murky middle.
    data.sort(key=lambda d: (_f(d["dist"]) if _f(d["dist"]) is not None else 9e9))
    return _HTML_TEMPLATE.replace("__ROWS__", json.dumps(data, ensure_ascii=False))


def apply_decisions(path: Path) -> None:
    if not path.exists():
        print(f"ERROR: decisions file not found: {path}")
        sys.exit(1)
    dec: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            u = (r.get("detail_url") or "").strip()
            d = (r.get("decision") or "").strip().lower()
            if not u:
                continue
            if d in ("accept", "accepted", "y", "yes"):
                dec[u] = "accepted"
            elif d in ("reject", "rejected", "n", "no"):
                dec[u] = "rejected"

    with GOOGLE_PLACES_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        allrows = list(reader)

    n_acc = n_rej = 0
    for r in allrows:
        if (r.get("status") or "") != "review":
            continue  # only review rows are eligible; keeps re-apply idempotent
        u = r.get("detail_url") or ""
        verdict = dec.get(u)
        if verdict == "accepted":
            r["status"] = "accepted"
            n_acc += 1
        elif verdict == "rejected":
            r["status"] = "rejected"
            n_rej += 1

    with GOOGLE_PLACES_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(allrows)

    remaining = sum(1 for r in allrows if (r.get("status") or "") == "review")
    print(f"applied {len(dec)} decisions: {n_acc} -> accepted, {n_rej} -> rejected")
    print(f"remaining 'review' rows: {remaining}")
    print("re-run map.py to calibrate the newly-accepted rows.")


def accept_all_review() -> None:
    """Bulk-promote every current 'review' row to 'accepted'. Use only when
    you've spot-checked that the whole review batch is correct (it was: a
    manual pass of the first ~224 came back 224/224)."""
    with GOOGLE_PLACES_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        allrows = list(reader)
    n = 0
    for r in allrows:
        if (r.get("status") or "") == "review":
            r["status"] = "accepted"
            n += 1
    with GOOGLE_PLACES_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(allrows)
    print(f"accepted all {n} 'review' rows. re-run map.py to calibrate them.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Review google_enrich.py 'review' matches.")
    ap.add_argument("--apply", metavar="DECISIONS_CSV", type=Path,
                    help="apply a downloaded decisions.csv back into google_places.csv")
    ap.add_argument("--accept-all-review", action="store_true",
                    help="promote ALL current 'review' rows to 'accepted' "
                         "(only after spot-checking the batch is clean)")
    args = ap.parse_args()

    if args.apply:
        apply_decisions(args.apply)
        return
    if args.accept_all_review:
        accept_all_review()
        return

    extras = load_tabelog_extras()
    rows = load_review_rows()
    if not rows:
        print("no 'review' rows in google_places.csv — nothing to review.")
        return
    GOOGLE_REVIEW_HTML.write_text(build_html(rows, extras), encoding="utf-8")
    print(f"wrote {len(rows)} review rows -> {GOOGLE_REVIEW_HTML}")
    print("open it in a browser, decide, download decisions.csv, then:")
    print("  uv run python src/tabelog/scrape/review_google.py --apply <decisions.csv>")


# Self-contained review page. No server, no external assets (Tabelog photos
# load from their CDN). Decisions live in localStorage so the sheet is
# resumable; the download button serializes them to decisions.csv.
_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Google 校准复核</title>
<style>
  :root { --ok:#16a34a; --no:#dc2626; --mut:#6b7280; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f3f4f6; color:#111827; font-size:14px; }
  header { position:sticky; top:0; z-index:10; background:#fff; border-bottom:1px solid #e5e7eb;
           padding:10px 16px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  header h1 { font-size:15px; margin:0 8px 0 0; }
  #prog { color:var(--mut); }
  #prog b { color:#111827; }
  button.act { border:1px solid #d1d5db; background:#f9fafb; border-radius:6px;
               padding:6px 12px; cursor:pointer; font-size:13px; }
  button.act:hover { background:#eef2ff; }
  .wrap { max-width:920px; margin:0 auto; padding:16px; }
  .card { background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:12px;
          margin-bottom:10px; display:grid; grid-template-columns:96px 1fr auto; gap:12px;
          align-items:start; }
  .card.cur { outline:2px solid #2563eb; }
  .card.accept { border-color:var(--ok); background:#f0fdf4; }
  .card.reject { border-color:var(--no); background:#fef2f2; opacity:0.7; }
  .thumb { width:96px; height:96px; border-radius:8px; object-fit:cover; background:#e5e7eb; }
  .mid { min-width:0; }
  .nm { font-weight:600; }
  .nm .g { color:var(--mut); font-weight:400; }
  .row { margin:3px 0; line-height:1.4; }
  .lab { color:var(--mut); font-size:12px; margin-right:4px; }
  .addr { font-size:12px; color:#374151; word-break:break-all; }
  .badge { display:inline-block; padding:1px 7px; border-radius:999px; font-size:12px;
           margin-right:6px; }
  .b-ok { background:#dcfce7; color:#166534; }
  .b-warn { background:#fef9c3; color:#854d0e; }
  .b-far { background:#fee2e2; color:#991b1b; }
  .links a { display:inline-block; margin-right:8px; color:#2563eb; text-decoration:none;
             font-size:13px; }
  .links a:hover { text-decoration:underline; }
  .dec { display:flex; flex-direction:column; gap:6px; }
  .dec button { border:1px solid #d1d5db; border-radius:6px; padding:8px 10px; cursor:pointer;
                font-size:13px; white-space:nowrap; background:#fff; }
  .dec .y.on { background:var(--ok); color:#fff; border-color:var(--ok); }
  .dec .n.on { background:var(--no); color:#fff; border-color:var(--no); }
  .hint { color:var(--mut); font-size:12px; padding:0 16px 16px; max-width:920px;
          margin:0 auto; }
  .region { font-size:11px; color:var(--mut); }
</style></head>
<body>
<header>
  <h1>Google 校准复核</h1>
  <span id="prog"></span>
  <button class="act" id="dl">下载 decisions.csv</button>
  <button class="act" id="next">跳到下一个未决定</button>
  <button class="act" id="clear">清空本地决定</button>
</header>
<div class="hint">快捷键:<b>j / k</b> 上下移动 · <b>a</b> 接受 · <b>r</b> 拒绝(决定后自动跳下一个未决定)。
  绿底「likely」= 距离 ≤50m 或名字几乎一致,基本可直接接受;黄/红需要点开两边对比。
  决定存在本浏览器,可随时关掉再回来。</div>
<div class="wrap" id="wrap"></div>
<script>
const ROWS = __ROWS__;
const LS = 'tabelog.greview';
let dec = {};
try { dec = JSON.parse(localStorage.getItem(LS) || '{}') || {}; } catch(e) { dec = {}; }
let cur = 0;
const wrap = document.getElementById('wrap');
const cards = [];

function distBadge(d) {
  const v = parseFloat(d.dist);
  if (isNaN(v)) return '<span class="badge b-warn">距离 NA</span>';
  const cls = v <= 50 ? 'b-ok' : (v <= 400 ? 'b-warn' : 'b-far');
  return '<span class="badge ' + cls + '">' + Math.round(v) + ' m</span>';
}
function simBadge(d) {
  const v = parseFloat(d.sim);
  if (isNaN(v)) return '';
  const cls = v >= 0.9 ? 'b-ok' : (v >= 0.6 ? 'b-warn' : 'b-far');
  return '<span class="badge ' + cls + '">名 ' + v.toFixed(2) + '</span>';
}
function likely(d) {
  const dv = parseFloat(d.dist), sv = parseFloat(d.sim);
  return (!isNaN(dv) && dv <= 50) || (!isNaN(sv) && sv >= 0.95);
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

ROWS.forEach(function(d, i) {
  const c = document.createElement('div');
  c.className = 'card';
  const img = d.thumb
    ? '<img class="thumb" src="' + esc(d.thumb) + '" loading="lazy" '
      + 'onerror="this.style.visibility=\\'hidden\\'">'
    : '<div class="thumb"></div>';
  c.innerHTML = img
    + '<div class="mid">'
      + '<div class="row nm">' + esc(d.tname)
        + ' <span class="region">[' + esc(d.region) + ']</span>'
        + (likely(d) ? ' <span class="badge b-ok">likely</span>' : '') + '</div>'
      + '<div class="row nm g">Google: ' + esc(d.gname) + '</div>'
      + '<div class="row">' + distBadge(d) + simBadge(d) + '</div>'
      + '<div class="row addr"><span class="lab">Tabelog</span>' + esc(d.taddr)
        + (d.genre ? ' · ' + esc(d.genre) : '') + '</div>'
      + '<div class="row addr"><span class="lab">Google</span>' + esc(d.gaddr) + '</div>'
      + '<div class="row links">'
        + '<a href="' + esc(d.url) + '" target="_blank" rel="noopener">Tabelog ↗</a>'
        + '<a href="' + esc(d.gmaps) + '" target="_blank" rel="noopener">Google 地图 ↗</a>'
      + '</div>'
    + '</div>'
    + '<div class="dec">'
      + '<button class="y" data-i="' + i + '" data-v="accept">✓ 接受</button>'
      + '<button class="n" data-i="' + i + '" data-v="reject">✗ 拒绝</button>'
    + '</div>';
  wrap.appendChild(c);
  cards.push(c);
});

function paint(i) {
  const d = ROWS[i], c = cards[i], v = dec[d.url];
  c.classList.toggle('accept', v === 'accept');
  c.classList.toggle('reject', v === 'reject');
  c.querySelector('.y').classList.toggle('on', v === 'accept');
  c.querySelector('.n').classList.toggle('on', v === 'reject');
}
function progress() {
  let a = 0, r = 0;
  ROWS.forEach(function(d){ if (dec[d.url] === 'accept') a++; else if (dec[d.url] === 'reject') r++; });
  document.getElementById('prog').innerHTML =
    '已决定 <b>' + (a + r) + '</b> / ' + ROWS.length + ' (接受 ' + a + ' · 拒绝 ' + r + ')';
}
function save() { try { localStorage.setItem(LS, JSON.stringify(dec)); } catch(e){} }
function decide(i, v) {
  const url = ROWS[i].url;
  dec[url] = (dec[url] === v) ? undefined : v;
  if (dec[url] === undefined) delete dec[url];
  save(); paint(i); progress();
  if (dec[url]) gotoNextUndecided(i + 1);
}
function setCur(i) {
  if (i < 0 || i >= cards.length) return;
  cards[cur] && cards[cur].classList.remove('cur');
  cur = i;
  cards[cur].classList.add('cur');
  cards[cur].scrollIntoView({block:'center', behavior:'smooth'});
}
function gotoNextUndecided(from) {
  for (let k = from; k < ROWS.length; k++) {
    if (!dec[ROWS[k].url]) { setCur(k); return; }
  }
  for (let k = 0; k < ROWS.length; k++) {
    if (!dec[ROWS[k].url]) { setCur(k); return; }
  }
}

wrap.addEventListener('click', function(e) {
  const b = e.target.closest('button[data-v]');
  if (!b) return;
  decide(parseInt(b.dataset.i, 10), b.dataset.v);
});
document.addEventListener('keydown', function(e) {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === 'j' || e.key === 'ArrowDown') { e.preventDefault(); setCur(Math.min(cur + 1, cards.length - 1)); }
  else if (k === 'k' || e.key === 'ArrowUp') { e.preventDefault(); setCur(Math.max(cur - 1, 0)); }
  else if (k === 'a') { e.preventDefault(); decide(cur, 'accept'); }
  else if (k === 'r' || k === 'd') { e.preventDefault(); decide(cur, 'reject'); }
});
document.getElementById('dl').addEventListener('click', function() {
  let csv = 'detail_url,decision\\n';
  Object.keys(dec).forEach(function(u) {
    if (dec[u]) csv += '"' + u.replace(/"/g,'""') + '",' + dec[u] + '\\n';
  });
  const blob = new Blob([csv], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'decisions.csv';
  a.click();
});
document.getElementById('next').addEventListener('click', function(){ gotoNextUndecided(cur + 1); });
document.getElementById('clear').addEventListener('click', function() {
  if (!confirm('清空本浏览器里的所有复核决定?')) return;
  dec = {}; save();
  ROWS.forEach(function(_, i){ paint(i); });
  progress();
});

ROWS.forEach(function(_, i){ paint(i); });
progress();
setCur(0);
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
