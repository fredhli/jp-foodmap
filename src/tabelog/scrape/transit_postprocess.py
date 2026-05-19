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
GEOJSON_LOW_PATH = DOCS_DIR / "transit" / "japan-low.geojson"
GEOJSON_MID_PATH = DOCS_DIR / "transit" / "japan-mid.geojson"
SUSPICIOUS_REPORT_PATH = DOCS_DIR / "transit" / "suspicious_lines.md"

# LOD tolerances in degrees. ~111 km / degree latitude (less in longitude
# off the equator, but visual simplification doesn't care about that level
# of precision). Picked so the simplified line stays within ~1px of the
# original at the zoom band the LOD is intended for:
#   low  (z 0-8):  ~1.5 km tolerance — country-scale, only shinkansen +
#                 JR mainlines drawn anyway, point reduction ~85-90%.
#   mid  (z 9-13): ~200 m tolerance — regional/city; subway/tram start
#                 appearing at z=11 so the tolerance has to stay tight
#                 enough that 200m doesn't visibly displace lines on a
#                 dense Tokyo grid.
#   high (z 14+): no simplification — current japan.geojson is reused.
LOD_LOW_EPSILON = 0.015
LOD_MID_EPSILON = 0.002


# Manual overrides applied during track-uniformity enforcement. (name, op)
# pairs forced into one bucket regardless of how the per-route-key pass
# voted. These are derived from human review of suspicious_lines.md:
# either a real 特急 traversal that the propagation pass under-counts
# (< 5 features named like the physical track inside long-haul groups), or
# the inverse — a commuter through-line that majority-wins flipped to
# long-haul because of incidental shared-track features.
#
# Tip when adding: keep the operator string EXACT to the geojson value.
# OSM has multiple operator-string variants for the same operator
# (e.g. "西日本旅客鉄道" vs "JR西日本", "東武鉄道" vs "東武鉄道 (Tobu Railway)")
# and each maps to a separate physical-track row in the audit.
FORCE_LONGHAUL_TRACKS = {
    # JR West
    ("JR湖西線",   "西日本旅客鉄道"),   # サンダーバード 大阪 ↔ 敦賀 全程
    ("JR播但線",   "西日本旅客鉄道"),   # はまかぜ 姫路 ↔ 和田山 全程
    ("JR舞鶴線",   "西日本旅客鉄道"),   # まいづる / はしだて 全程
    ("JR阪和線",   "JR西日本"),         # くろしお / はるか — alt-op-string stub
    # JR East
    ("上越線",     "東日本旅客鉄道"),   # たにがわ + 湘南新宿ライン
    # JR Hokkaido
    ("JR根室本線", "北海道旅客鉄道"),   # おおぞら 札幌 ↔ 釧路
    # JR Shikoku
    ("内子線",     "四国旅客鉄道"),     # しおかぜ / 宇和海 — 予讃線 internal shortcut
    # Private (private 特急 carriers)
    ("近畿日本鉄道名古屋線", "近畿日本鉄道"),       # アーバンライナー / しまかぜ
    ("近畿日本鉄道志摩線",   "近畿日本鉄道"),       # しまかぜ / 伊勢志摩ライナー
    ("南海電気鉄道南海本線", "南海電気鉄道"),       # サザン / ラピート
    ("東武日光線",           "東武鉄道"),           # スペーシア / きぬ
    ("東武鬼怒川線",         "東武鉄道 (Tobu Railway)"),  # スペーシア / きぬ
}
FORCE_CITY_TRACKS = {
    # Tokyo-metro commuter through-running — physically a new underground
    # line, behaviorally a commuter limb of 相鉄・JR・東急 直通網. Same
    # category as 武蔵野線 / 京葉線 (which stayed city).
    ("相鉄新横浜線", "相模鉄道"),
    # One-station stub off 京成本線 to old NRT terminal; primarily exists
    # for 芝山鉄道 through-running. Bulk is local.
    ("京成東成田線", "京成電鉄"),
}


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

# Two-sided thresholds for the long-haul propagation pass (see _tag_longhaul).
#
# SOURCE side — LONGHAUL_PROPAGATE_MIN_FEATURES: a `name` (e.g. "JR福知山線")
# is treated as a long-haul-carrier only if it shows up on at least this
# many features that already sit in a long-haul group. Filters out the
# 2-3-way junction outliers a 特急 relation sweeps up at station throats
# (e.g., こうのとり's two 'JR東西線' ways near 尼崎). Real cases — こうのとり
# carries 8 ways named 'JR福知山線', しなの ~20 named 'JR篠ノ井線' — clear
# this easily.
#
# TARGET side — LONGHAUL_PROPAGATE_MIN_TARGET_FRAC: a candidate route-name
# group only inherits the flag if at least this share of its own features
# sits on the trusted physical track. Stops a single mis-tagged junction
# way from flipping a whole private/local line: e.g., the しなの鉄道線
# relation has 1 way out of 217 tagged 'name=篠ノ井線' (a junction at
# 篠ノ井 station), which is plenty to overlap the trusted-name set but
# clearly doesn't make the third-sector しなの鉄道線 a long-haul carrier.
# Bulk-sharing relations (the 福知山線 普通 and 丹波路快速 groups, ~99%
# of features on 'JR福知山線') sail through.
LONGHAUL_PROPAGATE_MIN_FEATURES = 5
LONGHAUL_PROPAGATE_MIN_TARGET_FRAC = 0.5


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
# Strip JR-company prefixes so "予讃線" / "JR予讃線" / "JR四国予讃線" /
# "JR西日本山陽本線" all collapse to one key. Without this OSM's mixed
# naming splits a single physical line across multiple aggregation keys
# and the long-haul flag ends up inconsistent within one line — exactly
# the bug the user hit on 予讃線 around Matsuyama.
_JR_PREFIX_RE = re.compile(r"^JR(?:北海道|東日本|東海|西日本|四国|九州|貨物)?")

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
    s = _JR_PREFIX_RE.sub("", s).strip()
    return s

def _tag_longhaul(lines):
    """Mutates each line's properties to add is_longhaul: bool.

    Two-pass decision:

      Pass 1 — per logical line. Group ways by normalized route_name and
      tag the group long-haul if its aggregated names/operators match the
      service-class regex (新幹線 / 特急 / 寝台 / named airport express) or
      the strict JR-mainline allowlist. This catches the 特急 relations and
      mainline 普通 service directly, and indirectly catches things like the
      `JR中央線快速` relation (which doesn't itself match anything, but its
      member ways have name='中央本線' so the group's name concat matches).

      Pass 2 — physical-track propagation. The blind spot the first pass
      misses is the inverse case: a 特急 service whose route_name relation
      got tagged onto a few ways, while most ways on the same physical
      track ended up in a `普通` / `快速` group whose own name doesn't
      match the regex. OSM models the same track as a member of every
      service relation that runs on it; `build_way_route_map` keeps just
      one route_name per way via setdefault, so a single physical line
      can end up split across multiple route_name groups with only some
      flagged. The 福知山線 (こうのとり stranded as fragments) and the
      篠ノ井線 (しなの stranded) bugs are both this shape. We propagate by
      reading the `name` field — the physical track identifier — out of
      already-flagged groups: if 'JR福知山線' shows up on ≥N (see
      LONGHAUL_PROPAGATE_MIN_FEATURES) features inside long-haul groups,
      we treat that physical line as long-haul-carrying, and any other
      group with features named 'JR福知山線' inherits the flag.
    """
    groups = defaultdict(lambda: {"names": [], "ops": set()})
    for f in lines:
        p = f["properties"]
        key = _route_key(p)
        if not key:
            continue
        g = groups[key]
        if p.get("name"):       g["names"].append(p["name"])
        if p.get("route_name"): g["names"].append(p["route_name"])
        if p.get("operator"):   g["ops"].add(p["operator"])

    long_keys = set()
    for key, info in groups.items():
        nm_concat = " ".join(info["names"])
        op_concat = " ".join(info["ops"])
        if LONGHAUL_SERVICE_RE.search(nm_concat):
            long_keys.add(key)
            continue
        if JR_OPERATOR_RE.search(op_concat) and JR_MAINLINE_RE.search(nm_concat):
            long_keys.add(key)

    # Pass 2: physical-track propagation. Walk every feature once to
    # collect (a) name -> count-in-already-longhaul-groups (source side)
    # and (b) per-group total feature count + per-group count of features
    # whose name is in the trusted set (target side, computed after we
    # know the trusted set).
    name_long_count = defaultdict(int)
    feature_keys = [None] * len(lines)
    feature_names = [None] * len(lines)
    group_total = defaultdict(int)
    for i, f in enumerate(lines):
        p = f["properties"]
        key = _route_key(p)
        if not key:
            continue
        feature_keys[i] = key
        group_total[key] += 1
        nm = p.get("name")
        if nm:
            feature_names[i] = nm
            if key in long_keys:
                name_long_count[nm] += 1
    trusted_names = {nm for nm, c in name_long_count.items()
                     if c >= LONGHAUL_PROPAGATE_MIN_FEATURES}
    n_propagated_keys = 0
    if trusted_names:
        group_trusted = defaultdict(int)
        for i in range(len(lines)):
            key = feature_keys[i]
            nm = feature_names[i]
            if key and nm and nm in trusted_names:
                group_trusted[key] += 1
        for key, n_trusted in group_trusted.items():
            if key in long_keys:
                continue
            if n_trusted / group_total[key] >= LONGHAUL_PROPAGATE_MIN_TARGET_FRAC:
                long_keys.add(key)
                n_propagated_keys += 1

    n_long = 0
    for f in lines:
        key = _route_key(f["properties"])
        p = f["properties"]
        if key in long_keys:
            p["is_longhaul"] = True
            n_long += 1
        elif "is_longhaul" in p:
            del p["is_longhaul"]
    if n_propagated_keys:
        print(f"  longhaul propagation: {n_propagated_keys} extra route keys "
              f"promoted via {len(trusted_names)} trusted physical-track names")
    return n_long


# ---- pass 2b: per-track uniformity enforcement ---------------------------
#
# Hard invariant (per user request): one physical line — identified by
# (name, operator) — must be entirely long-haul or entirely city. Never
# split. The per-route-key decision in _tag_longhaul can leave splits
# because OSM models a single physical track as a member of multiple
# route relations (普通 / 快速 / 特急), and our extract step retains only
# one route relation per way. So a 福知山線 way that's a member of both
# 普通 (city) and こうのとり (long-haul) gets classified as one or the
# other depending on which relation won the route_name race.
#
# This pass resolves every split (name, op) by majority-wins (more
# features wins; on ties, default to city — the conservative choice).
# Every track that needed resolution is recorded so the user can review
# whether the auto-decision was right (see suspicious_lines.md).


def _enforce_track_uniformity(lines):
    """Force every (name, operator) physical track to a single is_longhaul
    classification. Returns a list of suspicious-track records for review."""
    tracks = defaultdict(lambda: {
        "T": 0, "F": 0,
        "T_feats": [], "F_feats": [],
        "T_keys": set(), "F_keys": set(),
    })
    for f in lines:
        p = f["properties"]
        nm = p.get("name")
        if not nm:
            continue
        op = p.get("operator") or ""
        rk = _route_key(p)
        d = tracks[(nm, op)]
        if p.get("is_longhaul"):
            d["T"] += 1
            d["T_feats"].append(f)
            d["T_keys"].add(rk)
        else:
            d["F"] += 1
            d["F_feats"].append(f)
            d["F_keys"].add(rk)

    suspicious = []
    forced_applied = []
    for (nm, op), d in tracks.items():
        t, f = d["T"], d["F"]
        key = (nm, op)
        is_forced_long = key in FORCE_LONGHAUL_TRACKS
        is_forced_city = key in FORCE_CITY_TRACKS
        is_forced = is_forced_long or is_forced_city
        is_mixed = t > 0 and f > 0
        if not is_forced and not is_mixed:
            continue

        if is_forced_long:
            decision_long = True
        elif is_forced_city:
            decision_long = False
        else:
            decision_long = t > f  # majority-wins; ties fall through to city

        if decision_long:
            for feat in d["F_feats"]:
                feat["properties"]["is_longhaul"] = True
        else:
            for feat in d["T_feats"]:
                feat["properties"].pop("is_longhaul", None)

        record = {
            "name": nm, "operator": op,
            "n_long": t, "n_city": f,
            "resolved_as": "longhaul" if decision_long else "city",
            "long_keys": sorted(d["T_keys"]),
            "city_keys": sorted(d["F_keys"]),
        }
        if is_forced:
            forced_applied.append(record)
        elif is_mixed:
            suspicious.append(record)
    return suspicious, forced_applied


def _assert_no_mixed_tracks(lines) -> None:
    """After uniformity enforcement, no (name, op) should be mixed.

    The assertion guards against future regressions: if a new pass or rule
    ever introduces a split, the build fails loudly instead of silently
    shipping a fragmented overlay."""
    tracks = defaultdict(lambda: [0, 0])
    for f in lines:
        p = f["properties"]
        nm = p.get("name")
        if not nm:
            continue
        op = p.get("operator") or ""
        tracks[(nm, op)][0 if p.get("is_longhaul") else 1] += 1
    mixed = [(k, v[0], v[1]) for k, v in tracks.items() if v[0] > 0 and v[1] > 0]
    if mixed:
        lines_out = [f"  {nm} [{op}]: T={t} F={f}"
                     for (nm, op), t, f in mixed[:10]]
        raise AssertionError(
            f"_enforce_track_uniformity failed — {len(mixed)} tracks still "
            f"mixed after pass:\n" + "\n".join(lines_out)
        )


def _write_suspicious_report(suspicious, forced, path) -> None:
    """Markdown table of every track that needed disambiguation.

    Sorted by how suspicious the decision looks: Type C (10–90% mixed)
    first because those are the genuine coin-flips that most need human
    review; then Type A (resolved → city, but might be real 特急
    traversals worth flipping); then Type B (resolved → long-haul, the
    safe auto-resolutions). Manual overrides are listed separately at
    the top so the active force-list is visible at a glance."""
    path.parent.mkdir(parents=True, exist_ok=True)
    type_a, type_b, type_c = [], [], []
    for s in suspicious:
        pct = s["n_long"] / (s["n_long"] + s["n_city"])
        if pct >= 0.9:
            type_b.append(s)
        elif pct <= 0.1:
            type_a.append(s)
        else:
            type_c.append(s)

    out = ["# Auto-resolved mixed transit lines",
           "",
           "Generated by `src/tabelog/scrape/transit_postprocess.py`. "
           "Do not hand-edit — re-run postprocess to regenerate.",
           "",
           "Each row is a `(name, operator)` physical track whose features "
           "split between long-haul (`T`) and city (`F`) classifications. "
           "Either the manual override list in `transit_postprocess.py` "
           "decided the class, or majority-wins picked the side with more "
           "features.",
           "",
           f"**Manually overridden: {len(forced)} · Auto-resolved by majority-wins: {len(suspicious)}**",
           ""]

    def section(label, rows, hint):
        if not rows:
            return
        rows.sort(key=lambda s: -(s["n_long"] + s["n_city"]))
        out.append(f"## {label}  ({len(rows)})")
        if hint:
            out.append("")
            out.append(hint)
        out.append("")
        out.append("| T | F | T% | resolved | name | operator | long-haul keys | city keys |")
        out.append("|--:|--:|---:|----------|------|----------|----------------|-----------|")
        for s in rows:
            t, f = s["n_long"], s["n_city"]
            total = t + f
            pct = (t / total * 100) if total else 0.0
            tk = "; ".join(s["long_keys"][:4])
            fk = "; ".join(s["city_keys"][:4])
            out.append(
                f"| {t} | {f} | {pct:.1f}% | **{s['resolved_as']}** | "
                f"{s['name']} | {s['operator']} | {tk} | {fk} |"
            )
        out.append("")

    section(
        "Manual overrides (FORCE_LONGHAUL_TRACKS / FORCE_CITY_TRACKS)",
        forced,
        "These tracks bypass majority-wins because human review found the "
        "automatic decision wrong. Edit the FORCE_* sets in "
        "`transit_postprocess.py` to add/remove entries."
    )
    section(
        "Type C — closer mix (10% ≤ T% ≤ 90%)",
        type_c,
        "Real coin-flips. Read each row and decide if majority-wins picked right."
    )
    section(
        "Type A — resolved → city, may have been real 特急 traversal",
        type_a,
        "Bulk-city tracks with a handful of long-haul leaks. If the leak keys "
        "below name a real 特急 service that runs end-to-end on this line "
        "(e.g. サンダーバード / たにがわ / しまかぜ / はまかぜ / きのさき), "
        "this should probably be flipped to long-haul."
    )
    section(
        "Type B — resolved → long-haul (low-risk auto-resolutions)",
        type_b,
        "Bulk long-haul tracks with a handful of city outliers — usually OSM "
        "tagging noise."
    )

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


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
    long_only = city_only = 0
    for s in stations:
        sx, sy = s["geometry"]["coordinates"]
        gx0 = int(sx / grid_deg); gy0 = int(sy / grid_deg)
        candidates = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                candidates |= cell_index.get((gx0 + dx, gy0 + dy), set())
        # key -> is_longhaul of that key's lines. Same key can't be both
        # buckets after _tag_longhaul (it decides per-key), so reading the
        # flag off the first matching line is safe.
        distinct = {}
        for key, li in candidates:
            if key in distinct:
                continue
            for c in lines[li]["geometry"]["coordinates"]:
                if _dist_sq_m((sx, sy), c) < prox_sq:
                    distinct[key] = bool(lines[li]["properties"].get("is_longhaul"))
                    break
        s["properties"]["line_count"] = len(distinct)
        has_long = any(distinct.values())
        has_city = any(not v for v in distinct.values())
        # Default-true for stations with no nearby line at all (rare, can
        # happen with isolated halts or geometry quirks) so the dot doesn't
        # disappear just because we couldn't classify it. The renderer's
        # zoom gate (z>=12) and bucket-toggle check still apply.
        s["properties"]["has_long_line"] = has_long or not distinct
        s["properties"]["has_city_line"] = has_city or not distinct
        if has_long and not has_city: long_only += 1
        elif has_city and not has_long: city_only += 1
        if len(distinct) >= 6: hub6 += 1
        elif len(distinct) >= 3: hub3 += 1
    print(f"  line_count tagged: {hub3} hubs (3-5 lines), {hub6} mega-hubs (6+ lines)")
    print(f"  bucket split: {long_only} long-only, {city_only} city-only, "
          f"{len(stations)-long_only-city_only} either-or-mixed")


# ---- LOD generation ------------------------------------------------------
# Pure-Python iterative Douglas-Peucker. Avoids adding shapely (~12 MB
# C-extension wheel) just for one geometry op; perf is fine at build time
# even with ~700k points across ~10k lines (~10-20s on a midrange laptop).
# Uses perpendicular distance to the segment endpoints' infinite line —
# the classical DP formulation. For nearly-straight rail geometry this is
# indistinguishable from point-to-segment distance in practice.

def _simplify_dp(coords: list, epsilon: float) -> list:
    n = len(coords)
    if n <= 2:
        return list(coords)
    keep = [False] * n
    keep[0] = keep[-1] = True
    eps_sq = epsilon * epsilon
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        ax, ay = coords[lo]
        bx, by = coords[hi]
        dx, dy = bx - ax, by - ay
        denom_sq = dx * dx + dy * dy
        max_d_sq = 0.0
        max_i = lo + 1
        if denom_sq == 0.0:
            # Endpoints coincide — measure plain distance to the common point.
            for i in range(lo + 1, hi):
                px, py = coords[i]
                d_sq = (px - ax) * (px - ax) + (py - ay) * (py - ay)
                if d_sq > max_d_sq:
                    max_d_sq = d_sq
                    max_i = i
        else:
            for i in range(lo + 1, hi):
                px, py = coords[i]
                num = dy * px - dx * py + bx * ay - by * ax
                d_sq = num * num / denom_sq
                if d_sq > max_d_sq:
                    max_d_sq = d_sq
                    max_i = i
        if max_d_sq > eps_sq:
            keep[max_i] = True
            stack.append((lo, max_i))
            stack.append((max_i, hi))
    return [coords[i] for i in range(n) if keep[i]]


def _simplify_lines(lines: list, epsilon: float) -> list:
    # Returns fresh feature dicts so the caller's `lines` list (which gets
    # written to high LOD) isn't mutated. Lines that collapse to <2 points
    # after simplification are dropped — they wouldn't render anyway.
    out = []
    total_before = total_after = 0
    for f in lines:
        coords = f["geometry"]["coordinates"]
        simplified = _simplify_dp(coords, epsilon)
        total_before += len(coords)
        total_after += len(simplified)
        if len(simplified) < 2:
            continue
        out.append({
            "type": f.get("type", "Feature"),
            "properties": f["properties"],
            "geometry": {"type": "LineString", "coordinates": simplified},
        })
    return out, total_before, total_after


def _write_lods(lines: list, stations: list) -> None:
    # Low LOD: only long-haul lines (shinkansen + JR mainlines). Nothing
    # else is drawn at z<9 anyway (see CLASSES.minZ in transit-layer.js),
    # so we ship a much smaller file. Stations dropped — at country zoom
    # they'd just be a screen-filling cloud of dots.
    long_lines = [f for f in lines if f["properties"].get("is_longhaul")]
    low_lines, lp_before, lp_after = _simplify_lines(long_lines, LOD_LOW_EPSILON)
    low = {"type": "FeatureCollection", "features": low_lines}
    GEOJSON_LOW_PATH.write_text(
        json.dumps(low, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"  wrote {GEOJSON_LOW_PATH.name} "
          f"({GEOJSON_LOW_PATH.stat().st_size / 1024 / 1024:.1f} MB, "
          f"{len(low_lines)}/{len(long_lines)} long-haul lines, "
          f"points {lp_before} -> {lp_after})")

    # Mid LOD: everything (both buckets) but moderately simplified.
    # Stations carry over verbatim — they're points, no geometry to
    # simplify, and at z 9-13 they're already the primary thing.
    mid_lines, mp_before, mp_after = _simplify_lines(lines, LOD_MID_EPSILON)
    mid = {"type": "FeatureCollection", "features": mid_lines + stations}
    GEOJSON_MID_PATH.write_text(
        json.dumps(mid, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"  wrote {GEOJSON_MID_PATH.name} "
          f"({GEOJSON_MID_PATH.stat().st_size / 1024 / 1024:.1f} MB, "
          f"{len(mid_lines)} lines + {len(stations)} stations, "
          f"points {mp_before} -> {mp_after})")


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

    suspicious, forced = _enforce_track_uniformity(lines)
    _assert_no_mixed_tracks(lines)
    if suspicious or forced:
        _write_suspicious_report(suspicious, forced, SUSPICIOUS_REPORT_PATH)
        print(f"  uniformity: {len(forced)} manual overrides + "
              f"{len(suspicious)} majority-wins → {SUSPICIOUS_REPORT_PATH.name}")
    else:
        if SUSPICIOUS_REPORT_PATH.exists():
            SUSPICIOUS_REPORT_PATH.unlink()
        print("  uniformity: 0 mixed tracks (nothing to resolve)")
    # Recount after uniformity enforcement.
    n_long = sum(1 for f in lines if f["properties"].get("is_longhaul"))
    print(f"  long-haul after uniformity: {n_long}/{len(lines)} lines")

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

    # Pre-compute low/mid LODs so transit-layer.js can pick the right one
    # for the current zoom. The full file above stays the high LOD.
    _write_lods(lines, stations)


if __name__ == "__main__":
    target = GEOJSON_PATH
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    if not target.exists():
        sys.exit(f"missing {target}")
    postprocess(target)
