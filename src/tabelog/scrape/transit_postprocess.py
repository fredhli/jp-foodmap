"""Second-pass cleanup for docs/transit/japan.geojson.

Runs after extract_japan_transit.py (or against the committed final geojson
directly). Four things:

  - Drop amusement-park internals,砂防/工事 maintenance loops, and 廃線
    relics from the line set. These aren't useful to a tourist and clutter
    Tokyo / Osaka at low zoom.
  - Merge stations that sit within 200m of each other across name groups
    (the build's first pass only merges within a name group). Picks up
    cases like 渋谷 ↔ 渋谷駅東口 where the JR / 京王 / メトロ entrances
    share the same physical complex but were tagged differently.
  - Tag each station with `line_count`: distinct route_name values whose
    geometry passes within ~80m. Drives the larger circle for 3-line and
    6-line hubs in the renderer.
  - Tag each line with `is_longhaul`: True for shinkansen, JR limited
    express, and JR mainlines whose aggregate length is significant.
    Used by the renderer to split into a 长途 / 市内 layer pair.

Idempotent: re-running on the output of a prior run yields the same file
(blocklist patterns + longhaul rules are stable; the 200m station merge
won't find new pairs the second time).

  uv run python src/tabelog/scrape/transit_postprocess.py
"""

import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

_SRC_DIR = str(Path(__file__).resolve().parents[2])
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from tabelog.paths import DOCS_DIR

GEOJSON_PATH = DOCS_DIR / "transit" / "japan.geojson"


# ---- blocklist -----------------------------------------------------------

# Operators that only run internal / non-public / freight-only services.
# Disney's two rail rides, the Tateyama sand-control gauge line, the Iwate
# Kaihatsu freight line, etc. — all tagged railway= but nothing a visitor
# would ride as transit.
BAN_OPERATOR_RE = re.compile(
    "舞浜リゾートライン|"           # Disney Resort monorail
    "ディズニーシー|"                # DisneySea Electric Railway operator
    "オリエンタルランド|"            # Tokyo Disney parent
    "立山砂防|"                    # Tateyama sand-control railway (gov't)
    "国土交通省北陸地方整備局|"      # MLIT internal
    "羅須地人鉄道|"                 # preserved hobby line
    "岩手開発鉄道|"                 # cement freight only
    "新日鐵|新日本製鐵|"            # steel mill internals
    "電源開発|"                    # power-co internals
    "森林鉄道"                     # logging railways (all closed)
)
BAN_NAME_RE = re.compile(
    "ディズニー|Disney|"
    "廃線|abandoned|"
    "西武遊園地|"
    "遊園地内|"
    "砂防工事|"
    "Busy Buggies"
)


# ---- long-haul tagging ---------------------------------------------------

# Service-class regex: a route_name matching any of these is long-haul
# regardless of operator. Covers shinkansen, JR limited express (特急 / 寝台),
# named airport/inter-city services, and a few named shinkansen sub-services
# that don't carry "新幹線" in their route_name.
LONGHAUL_SERVICE_RE = re.compile(
    "新幹線|Shinkansen|"
    "特急|"
    "寝台|"
    "サンライズ|"
    "スカイライナー|"               # Keisei NRT airport express
    "ナリタエクスプレス|N'EX|"      # JR NRT airport express
    "はるか|"                       # JR KIX airport express
    "マリンライナー"                 # JR rapid Okayama-Takamatsu via Seto-Ohashi
)

# JR operators (the 6 passenger JR companies). Used as a gate before
# applying the JR-mainline allowlist so that "京浜急行電鉄本線" stays in
# the city bucket.
JR_OPERATOR_RE = re.compile(
    r"^(東日本旅客鉄道|西日本旅客鉄道|東海旅客鉄道|"
    "九州旅客鉄道|北海道旅客鉄道|四国旅客鉄道)"
)

# JR mainlines whose aggregate length warrants the long-haul tag even for
# the普通-service ways that don't sit under a 特急 relation. Strict allowlist
# so JR commuter loops (山手・京浜東北・中央線快速・etc.) stay city.
JR_MAINLINE_RE = re.compile(
    "東海道本線|山陽本線|山陰本線|東北本線|信越本線|北陸本線|"
    "中央本線|中央西線|関西本線|紀勢本線|高山本線|"
    "鹿児島本線|日豊本線|長崎本線|筑豊本線|久大本線|豊肥本線|唐津線|"
    "函館本線|室蘭本線|奥羽本線|羽越本線|宗谷本線|石北本線|"
    "根室本線|石勝線|"
    "土讃線|予讃線|高徳線|"
    "伯備線|瀬戸大橋線"
)


# ---- geometry helpers ----------------------------------------------------

# Equirectangular at Tokyo's ~35°N. Plenty precise for sub-km thresholds.
M_PER_DEG_LON = 91000.0
M_PER_DEG_LAT = 111000.0

def _dist_sq_m(a, b):
    dx = (a[0] - b[0]) * M_PER_DEG_LON
    dy = (a[1] - b[1]) * M_PER_DEG_LAT
    return dx * dx + dy * dy

def _seg_len_m(coords):
    tot = 0.0
    for i in range(1, len(coords)):
        a, b = coords[i - 1], coords[i]
        dx = (b[0] - a[0]) * M_PER_DEG_LON
        dy = (b[1] - a[1]) * M_PER_DEG_LAT
        tot += math.hypot(dx, dy)
    return tot


# ---- pass 1: blocklist ---------------------------------------------------

def _banned(props):
    op = props.get("operator") or ""
    nm = (props.get("name") or "") + " " + (props.get("route_name") or "")
    if op and BAN_OPERATOR_RE.search(op):
        return True
    if nm.strip() and BAN_NAME_RE.search(nm):
        return True
    return False


# ---- pass 2: long-haul tagging -------------------------------------------

_PAREN_SUFFIX_RE = re.compile(r"\s*[（(][^)）]*[)）]\s*$")

def _route_key(props):
    """The label we group ways under for length aggregation and line_count.

    OSM tags both directions of the same physical line as separate route
    relations (`JR内房線 (千葉 → 安房鴨川)` and `(安房鴨川 → 千葉)`), and
    sometimes attaches a parenthetical service class or English alias. We
    repeatedly strip trailing parens so reciprocal pairs collapse to the
    same key. Without this, 金山 / 子安 / etc. end up counted as 10+ line
    transfer hubs purely from directional tagging."""
    s = (props.get("route_name") or props.get("name") or "").strip()
    prev = None
    while s != prev:
        prev = s
        s = _PAREN_SUFFIX_RE.sub("", s).strip()
    return s

def _tag_longhaul(lines):
    """Mutates each line's properties to add is_longhaul: bool."""
    agg_len = defaultdict(float)
    for f in lines:
        key = _route_key(f["properties"])
        if not key:
            continue
        agg_len[key] += _seg_len_m(f["geometry"]["coordinates"])

    n_long = 0
    for f in lines:
        p = f["properties"]
        nm = (p.get("name") or "") + " " + (p.get("route_name") or "")
        op = p.get("operator") or ""
        long = False
        if LONGHAUL_SERVICE_RE.search(nm):
            long = True
        elif JR_OPERATOR_RE.search(op) and JR_MAINLINE_RE.search(nm):
            # JR mainline allowlist hit — only count if the aggregate run is
            # actually substantial (>80km), so a stray track fragment named
            # "山陽本線 (連絡)" doesn't flip into long-haul on its own.
            if agg_len.get(_route_key(p), 0.0) > 80_000:
                long = True
        if long:
            p["is_longhaul"] = True
            n_long += 1
    return n_long


# ---- pass 3: cross-name station merge (200m, any name) -------------------

def _cross_merge_stations(stations, threshold_m):
    """Single-linkage cluster stations within threshold_m of each other,
    regardless of name. Within a cluster, pick the most-common name (or the
    longest if no majority) and combine operators."""
    if not stations:
        return stations

    thresh_sq = threshold_m * threshold_m

    # Spatial grid for fast neighbor lookup. Cell size ~ threshold so each
    # station's neighborhood is the 3x3 around its cell.
    grid_deg = threshold_m / M_PER_DEG_LAT * 1.1   # slight margin
    cell_index = defaultdict(list)
    for i, s in enumerate(stations):
        c = s["geometry"]["coordinates"]
        gx = int(c[0] / grid_deg)
        gy = int(c[1] / grid_deg)
        cell_index[(gx, gy)].append(i)

    # Union-find
    parent = list(range(len(stations)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i, s in enumerate(stations):
        c = s["geometry"]["coordinates"]
        gx0 = int(c[0] / grid_deg)
        gy0 = int(c[1] / grid_deg)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in cell_index.get((gx0 + dx, gy0 + dy), ()):
                    if j <= i:
                        continue
                    if _dist_sq_m(c, stations[j]["geometry"]["coordinates"]) < thresh_sq:
                        union(i, j)

    clusters = defaultdict(list)
    for i in range(len(stations)):
        clusters[find(i)].append(i)

    out = []
    n_collapsed = 0
    for members in clusters.values():
        if len(members) == 1:
            out.append(stations[members[0]])
            continue
        n_collapsed += len(members) - 1
        # Centroid
        xs = [stations[i]["geometry"]["coordinates"][0] for i in members]
        ys = [stations[i]["geometry"]["coordinates"][1] for i in members]
        cx = round(sum(xs) / len(xs), 5)
        cy = round(sum(ys) / len(ys), 5)
        # Pick name: most common, fall back to longest. Names like
        # "渋谷" + "渋谷駅東口" should keep the simpler one.
        name_counts = defaultdict(int)
        for i in members:
            nm = (stations[i]["properties"].get("name") or "").strip()
            if nm:
                name_counts[nm] += 1
        if name_counts:
            best_count = max(name_counts.values())
            top = [n for n, c in name_counts.items() if c == best_count]
            chosen_name = sorted(top, key=lambda n: (-len(n), n))[-1]
        else:
            chosen_name = None
        operators = set()
        railways = set()
        all_names = set()
        for i in members:
            mp = stations[i]["properties"]
            if mp.get("operator"):
                operators.add(mp["operator"])
            railways.add(mp.get("railway") or "station")
            if mp.get("name"):
                all_names.add(mp["name"])
        railway = "station" if "station" in railways else next(iter(railways))
        merged_props = {"kind": "station", "railway": railway}
        if chosen_name:
            merged_props["name"] = chosen_name
        if len(all_names) > 1:
            # Stash the merged-away names so the hover tooltip can show the
            # alternates if the chosen one is unfamiliar to the user.
            merged_props["alt_names"] = sorted(all_names - {chosen_name})
        if operators:
            merged_props["operator"] = "; ".join(sorted(operators))
        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [cx, cy]},
            "properties": merged_props,
        })

    print(f"  cross-name merge: {len(stations)} -> {len(out)} ({n_collapsed} collapsed)")
    return out


# ---- pass 4: line_count per station --------------------------------------

def _tag_line_count(stations, lines, proximity_m):
    """Compute distinct route_name count within proximity_m of each station.
    Stores as `line_count` on station properties.

    Uses a spatial grid built by sampling each line segment at ~step_m and
    inserting (route_key, line_idx) into every cell touched. This way long
    straight segments don't slip through the cracks between widely-spaced
    coords."""
    if not stations:
        return
    grid_deg = proximity_m / M_PER_DEG_LAT * 1.5
    step_m = proximity_m * 0.6
    cell_index = defaultdict(set)

    for li, f in enumerate(lines):
        key = _route_key(f["properties"])
        if not key:
            continue
        coords = f["geometry"]["coordinates"]
        for i in range(len(coords)):
            x, y = coords[i]
            gx = int(x / grid_deg); gy = int(y / grid_deg)
            cell_index[(gx, gy)].add((key, li))
            if i + 1 < len(coords):
                nx, ny = coords[i + 1]
                seg_len = math.hypot(
                    (nx - x) * M_PER_DEG_LON,
                    (ny - y) * M_PER_DEG_LAT
                )
                if seg_len > step_m:
                    steps = int(seg_len / step_m) + 1
                    for k in range(1, steps):
                        t = k / steps
                        px = x + t * (nx - x)
                        py = y + t * (ny - y)
                        cell_index[(int(px / grid_deg), int(py / grid_deg))].add((key, li))

    prox_sq = proximity_m * proximity_m
    hub3 = hub6 = 0
    for s in stations:
        sx, sy = s["geometry"]["coordinates"]
        gx0 = int(sx / grid_deg); gy0 = int(sy / grid_deg)
        candidates = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                candidates |= cell_index.get((gx0 + dx, gy0 + dy), set())
        distinct = set()
        for key, li in candidates:
            if key in distinct:
                continue
            for c in lines[li]["geometry"]["coordinates"]:
                if _dist_sq_m((sx, sy), c) < prox_sq:
                    distinct.add(key)
                    break
        s["properties"]["line_count"] = len(distinct)
        if len(distinct) >= 6: hub6 += 1
        elif len(distinct) >= 3: hub3 += 1
    print(f"  line_count tagged: {hub3} hubs (3-5 lines), {hub6} mega-hubs (6+ lines)")


# ---- driver --------------------------------------------------------------

def postprocess(in_path: Path, out_path: Path | None = None) -> None:
    out_path = out_path or in_path
    print(f"  reading {in_path}")
    with in_path.open(encoding="utf-8") as f:
        gj = json.load(f)

    lines, stations = [], []
    for ftr in gj["features"]:
        geom = ftr.get("geometry") or {}
        if geom.get("type") == "LineString":
            lines.append(ftr)
        elif geom.get("type") == "Point":
            stations.append(ftr)

    n_lines_in, n_stations_in = len(lines), len(stations)
    print(f"  in: {n_lines_in} lines, {n_stations_in} stations")

    lines = [f for f in lines if not _banned(f["properties"])]
    print(f"  blocklist: {n_lines_in} -> {len(lines)} lines ({n_lines_in - len(lines)} dropped)")

    n_long = _tag_longhaul(lines)
    print(f"  long-haul tagged: {n_long}/{len(lines)} lines")

    stations = _cross_merge_stations(stations, threshold_m=200.0)
    # 120m proximity for line counting: a typical big-station platform/concourse
    # spans 200-400m end-to-end, but the station point sits roughly at the
    # platform centroid, so 120m catches all the tracks belonging to that
    # complex without bleeding into a parallel-but-separate station next door.
    _tag_line_count(stations, lines, proximity_m=120.0)

    out = {"type": "FeatureCollection", "features": lines + stations}
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"  wrote {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    target = GEOJSON_PATH
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    if not target.exists():
        sys.exit(f"missing {target}")
    postprocess(target)
