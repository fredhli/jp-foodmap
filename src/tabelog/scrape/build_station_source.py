"""Build src/tabelog/scrape/station_source_v2.json from the local OSM extract.

v1 (`station_source_v1.json`, written by transit_postprocess.py) carries six
fields per published station.  v2 keeps those six fields byte-for-byte, in
the same row order, and appends four service signals that the 4.3.6 station
layer needs:

  lines      distinct ordinary lines that stop here (route relations)
  operators  distinct operators among those relations
  ltd        distinct limited-express / shinkansen services that stop here
  modes      bitmask: 1 = tram/subway/non-JR rail, 2 = JR, 4 = shinkansen

Inputs, all local (data/osm/ is gitignored, see extract_japan_transit.py):

  data/osm/japan-routes.opl    route relation tags + member ids
  data/osm/japan-rail.geojson  every rail point node, keyed by OSM node id
  docs/transit/japan.geojson   the post-processed line features

The root cause this fixes: extract_japan_transit.parse_route_relations()
only ever kept way members, so the relations' stop *nodes* -- the one direct
statement of "which lines stop at this station" -- were discarded and 4.3.5a
had to guess from geometry and hard-coded overrides.

The output is deterministic: rows follow v1's order, every count is a set
length, and nothing reads the clock or a random source.  The script ends with
a self-check against the counts measured during the 4.3.6 research
(audit_output/4.3.6-research/) and exits non-zero on a drift above 3% or on
any failed spot check.

  uv run python src/tabelog/scrape/build_station_source.py
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_SRC_DIR = str(Path(__file__).resolve().parents[2])
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from tabelog.paths import DATA, DOCS_DIR, atomic_write_text  # noqa: E402
from tabelog.scrape.extract_japan_transit import (  # noqa: E402
    KEEP_ROUTE_TYPES,
    opl_decode,
)
from tabelog.scrape.transit_postprocess import (  # noqa: E402
    M_PER_DEG_LAT,
    M_PER_DEG_LON,
    _point_seg_dist_sq_m,
    _route_key,
)

OSM_DIR = DATA / "osm"
ROUTES_OPL = OSM_DIR / "japan-routes.opl"
RAIL_POINTS_GEOJSON = OSM_DIR / "japan-rail.geojson"
TRANSIT_GEOJSON = DOCS_DIR / "transit" / "japan.geojson"
STATION_SOURCE_V1 = Path(__file__).with_name("station_source_v1.json")
STATION_SOURCE_V2 = Path(__file__).with_name("station_source_v2.json")

V1_FIELDS = ["lon", "lat", "name", "name_en", "railway", "line_count"]
V2_FIELDS = V1_FIELDS + ["lines", "operators", "ltd", "modes"]

MODE_RAIL = 1
MODE_JR = 2
MODE_SHINKANSEN = 4

# Member roles that mean "the service calls here".  The empty role is kept
# because a sizeable share of older relations list stop nodes without one.
STOP_ROLES = frozenset({
    "stop", "stop_entry_only", "stop_exit_only", "station", "platform",
    "platform_entry_only", "platform_exit_only", "stop_position", "",
})

# A stop node snaps to the nearest published station within this radius.
STOP_SNAP_M = 300.0
# ...but a tram_stop candidate is pushed back by this much.  Without it the
# JR stop positions at 熊本 snap onto the 熊本駅前 tram stop, which sits
# closer to the JR platforms than the merged JR station point does.
TRAM_STOP_PENALTY_M = 200.0
SNAP_CELL_DEG = 0.004

# Geometric fallback radius (point to segment), same as line_count.
GEOMETRY_NEAR_M = 120.0
GEOMETRY_CELL_DEG = 0.0025
GEOMETRY_SAMPLE_M = 60.0

# ---- name / operator normalisation ----------------------------------------
# Copied from audit_output/4.3.6-research/scripts/score.py (norm_name,
# jr_of, JR_REF, SK, SK_TRAIN, LTD) and badge6.py (isjr_op, FREIGHT).

_PAREN_RE = re.compile(r"\s*[（(][^)）]*[)）]\s*$")
_DIRECTION_RE = re.compile(
    r"\s*[:：]?\s*[（(]?\s*\S+\s*(?:→|=>|⇒|->)\s*\S+\s*[)）]?\s*$"
)
# Service-class words: 各駅停車 / 快速 / 急行 of one line are one line.
_SERVICE_RE = re.compile(
    r"\s*(?:各駅停車|普通|快速急行|通勤快速|通勤急行|新快速|特別快速|区間快速|区快|快速|急行|準急|直通|--)\s*"
)
# `東海(?!道)`: keep 東海道本線 intact (see transit_postprocess._JR_PREFIX_RE).
_JR_NAME_PREFIX_RE = re.compile(r"^JR(?:北海道|東日本|東海(?!道)|西日本|四国|九州)?")
_REF_SPLIT_RE = re.compile(r"[;,/]")

JR_COMPANIES = (
    ("北海道旅客鉄道", "JR北海道"),
    ("東日本旅客鉄道", "JR東日本"),
    ("東海旅客鉄道", "JR東海"),
    ("西日本旅客鉄道", "JR西日本"),
    ("四国旅客鉄道", "JR四国"),
    ("九州旅客鉄道", "JR九州"),
)
_JR_ABBREV_RE = re.compile(r"JR\s*(北海道|東日本|東海|西日本|四国|九州)")
# `^JR\b` (a bare "JR" operator) vs `^JR` (any JR-prefixed operator, e.g.
# `JR東日本`, where \b does not fire because 東 is a word character).  The
# two are distinct in the research scripts and the counts depend on it.
_JR_BARE_RE = re.compile(r"^JR\b")
_JR_PREFIXED_RE = re.compile(r"^JR")
# JR station-numbering line codes, for relations that carry a ref but no
# operator tag.
JR_REF_RE = re.compile(r"^(J[A-Z]|CA|CB|CC|CD|CE|CF|CG|CI|CJ|CL|CM)\d*$")
FREIGHT_RE = re.compile(r"貨物")

SHINKANSEN_RE = re.compile(r"新幹線|Shinkansen", re.I)
SHINKANSEN_TRAIN_RE = re.compile(
    r"のぞみ|ひかり|こだま|みずほ|さくら|つばめ|はやぶさ|はやて|こまち|つばさ|とき|"
    r"たにがわ|かがやき|はくたか|あさま|つるぎ|かもめ"
)
LIMITED_EXPRESS_RE = re.compile(
    r"特急|Limited Express|ライナー|N'EX|はるか|サンダーバード|しなの|ひだ|あずさ|"
    r"かいじ|スーパー|ソニック|きらめき|にちりん|かもめ|みどり|ハウステンボス|"
    r"しおかぜ|南風|うずしお|やくも|こうのとり|きのさき|はしだて|まいづる|くろしお|"
    r"おおぞら|とかち|カムイ|ライラック|すずらん|北斗|宗谷|オホーツク|サロベツ|"
    r"いなほ|つがる|ひたち|ときわ|しおさい|わかしお|さざなみ|あやめ|"
    r"成田エクスプレス|ふじさん|あさぎり|ロマンスカー|しまかぜ|ひのとり|"
    r"アーバンライナー|あをによし|ビスタ|サザン|ラピート|こうや|りんかん|"
    r"泉北ライナー|スカイライナー|モーニング|レッドアロー|ラビュー|スペーシア|"
    r"リバティ|きぬ|けごん|はまかぜ|らくラクはりま|WEST EXPRESS"
)


def _norm_line_name(name: str | None) -> str:
    s = (name or "").strip()
    prev = None
    while s != prev:
        prev = s
        s = _PAREN_RE.sub("", s).strip()
        s = _DIRECTION_RE.sub("", s).strip()
    return s


def _first_ref(ref: str | None) -> str:
    return _REF_SPLIT_RE.split(ref or "")[0].strip()


def _first_operator(operator: str | None) -> str:
    return (operator or "").split(";")[0].strip()


def _jr_company(operator: str | None, name: str = "") -> str | None:
    """JR company short name from an operator (or line name), else None."""
    text = f"{operator or ''} {name or ''}"
    for full, short in JR_COMPANIES:
        if full in text:
            return short
    m = _JR_ABBREV_RE.search(text)
    if m:
        return "JR" + m.group(1)
    if _JR_BARE_RE.match((operator or "").strip()):
        return "JR"
    return None


def _is_jr_operator(operator: str | None) -> bool:
    op = (operator or "").strip()
    return any(full in op for full, _ in JR_COMPANIES) or bool(_JR_PREFIXED_RE.match(op))


# Spelling variants of one operator seen in the relations' operator tags.
# Keys are compared after _COMPANY_SUFFIX_RE has removed 株式会社 and friends.
OPERATOR_ALIASES = {
    "Tokyo Metro": "東京地下鉄",
    "東京メトロ": "東京地下鉄",
    "Meitetsu": "名古屋鉄道",
    "Kintetsu Corporation": "近畿日本鉄道",
    "南海電鉄": "南海電気鉄道",
    "京王": "京王電鉄",
    "相鉄": "相模鉄道",
    "京都市営地下鉄": "京都市交通局",
    "Seibu Railway Company, Ltd.": "西武鉄道",
    "阪堺電車": "阪堺電気軌道",
    "土佐電気鉄道": "とさでん交通",
    "土佐電氣鐵道": "とさでん交通",
    "Kōbe Rapid Transit Railway": "神戸高速鉄道",
}
_COMPANY_SUFFIX_RE = re.compile(r"^(?:株式会社|株式會社)|(?:株式会社|株式會社)$")
# JR spellings that name a JR line rather than a company (`JR東北線`) or no
# company at all (`JR`, `JR West` via _JR_BARE_RE) share one generic key;
# _station_line_count folds it into the station's single JR company.
_JR_LINE_OPERATOR_RE = re.compile(r"^JR")


def _canonical_operator(operator: str | None, name: str) -> str:
    """Operator half of the line identity key.

    The operator has to take part: `ref` collides between JR西日本 and
    大阪メトロ (both use S / O / T), so a bare ref would fold two different
    companies' lines into one.  JR spellings (東日本旅客鉄道 / JR東日本) fold
    to one short name, and the common spelling variants of other operators
    (Tokyo Metro / 東京地下鉄, 株式会社 suffixes) fold via OPERATOR_ALIASES."""
    jr = _jr_company(operator, name)
    if jr:
        return jr
    op = _PAREN_RE.sub("", _first_operator(operator)).strip()
    op = _COMPANY_SUFFIX_RE.sub("", op).strip()
    if _JR_LINE_OPERATOR_RE.match(op):
        return "JR"
    return OPERATOR_ALIASES.get(op, op)


def _line_identity(tags: dict[str, str], norm_name: str) -> tuple[str, str]:
    operator = _canonical_operator(tags.get("operator"), norm_name)
    ref = _first_ref(tags.get("ref"))
    if ref:
        return operator, "ref:" + ref
    name = norm_name
    prev = None
    while name != prev:
        prev = name
        name = _SERVICE_RE.sub("", name).strip()
        name = _PAREN_RE.sub("", name).strip()
    name = _JR_NAME_PREFIX_RE.sub("", name).strip()
    return operator, "name:" + (name or norm_name)


def _station_line_count(line_ids: set[tuple[str, str]]) -> int:
    """Distinct lines at one station, after filling in missing operators.

    416 of the 2,053 relations carry no operator tag, and some JR relations
    say only `JR`.  Counted as-is, `('', ref:NK)` next to
    `('南海電気鉄道', ref:NK)` would make one line two.  Within a single
    station, an operator-less (or generic-JR) key adopts the operator of the
    other keys with the same line half when exactly one such operator
    exists; otherwise it stays distinct.  This never merges two named
    operators, so the JR西日本 / 大阪メトロ ref collision stays split."""
    named: dict[str, set[str]] = defaultdict(set)
    for operator, line in line_ids:
        if operator and operator != "JR":
            named[line].add(operator)
    resolved: set[tuple[str, str]] = set()
    for operator, line in sorted(line_ids):
        candidates = named.get(line, set())
        if not operator:
            if len(candidates) == 1:
                operator = next(iter(candidates))
        elif operator == "JR":
            jr = sorted(c for c in candidates if c.startswith("JR"))
            if len(jr) == 1:
                operator = jr[0]
        resolved.add((operator, line))
    return len(resolved)


# ---- inputs ----------------------------------------------------------------

def _load_v1() -> tuple[bytes, list[list]]:
    raw = STATION_SOURCE_V1.read_bytes()
    payload = json.loads(raw)
    if payload.get("v") != 1 or payload.get("fields") != V1_FIELDS:
        raise SystemExit(f"{STATION_SOURCE_V1.name}: unexpected header")
    return raw, payload["stations"]


def _v1_key(row: list) -> tuple[str, float, float]:
    return (row[2], row[0], row[1])


def _load_transit(v1_rows: list[list]) -> list[dict]:
    """Line features of japan.geojson, after checking its stations are v1's."""
    with TRANSIT_GEOJSON.open(encoding="utf-8") as f:
        gj = json.load(f)
    lines = []
    by_key: dict[tuple[str, float, float], int] = {}
    for idx, row in enumerate(v1_rows):
        key = _v1_key(row)
        if key in by_key:
            raise SystemExit(f"duplicate v1 station key {key}")
        by_key[key] = idx
    matched: set[int] = set()
    for ftr in gj["features"]:
        geom = ftr.get("geometry") or {}
        if geom.get("type") == "LineString":
            lines.append(ftr)
        elif geom.get("type") == "Point":
            lon, lat = geom["coordinates"][:2]
            key = ((ftr.get("properties") or {}).get("name") or "", lon, lat)
            idx = by_key.get(key)
            if idx is None:
                raise SystemExit(f"japan.geojson station {key} has no v1 row")
            matched.add(idx)
    if len(matched) != len(v1_rows):
        missing = [v1_rows[i][2] for i in range(len(v1_rows)) if i not in matched]
        raise SystemExit(
            f"{len(missing)} v1 stations are not in japan.geojson, e.g. {missing[:5]}"
        )
    return lines


_MEMBER_NODE_RE = re.compile(r"^n(\d+)(?:@(.*))?$")


def _load_route_relations() -> list[tuple[dict[str, str], list[tuple[int, str]]]]:
    """(tags, [(node_id, role)]) per kept route relation, in OPL order.

    Unlike extract_japan_transit.parse_route_relations(), this keeps the
    node members -- the stops -- and ignores the ways."""
    rels = []
    with ROUTES_OPL.open(encoding="utf-8") as f:
        for line in f:
            if not line.startswith("r"):
                continue
            tags: dict[str, str] = {}
            nodes: list[tuple[int, str]] = []
            for tok in line.rstrip("\n").split(" "):
                if tok.startswith("T") and len(tok) > 1:
                    for kv in tok[1:].split(","):
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            tags[opl_decode(k)] = opl_decode(v)
                elif tok.startswith("M") and len(tok) > 1:
                    for member in tok[1:].split(","):
                        m = _MEMBER_NODE_RE.match(member)
                        if m:
                            nodes.append((int(m.group(1)), opl_decode(m.group(2) or "")))
            if tags.get("type") != "route" or tags.get("route") not in KEEP_ROUTE_TYPES:
                continue
            rels.append((tags, nodes))
    return rels


_FEATURE_ID_RE = re.compile(r'"id":\s*"n(\d+)"')


def _load_stop_nodes(wanted: set[int]) -> dict[int, tuple[float, float]]:
    """node id -> (lon, lat) for the wanted point nodes of japan-rail.geojson.

    The file is one feature per line; the id is read with a regex first so
    the ~140k irrelevant features never go through json.loads."""
    out: dict[int, tuple[float, float]] = {}
    with RAIL_POINTS_GEOJSON.open(encoding="utf-8") as f:
        for line in f:
            m = _FEATURE_ID_RE.search(line)
            if not m or int(m.group(1)) not in wanted:
                continue
            ftr = json.loads(line.strip().rstrip(","))
            geom = ftr.get("geometry") or {}
            if geom.get("type") != "Point":
                continue
            lon, lat = geom["coordinates"][:2]
            out[int(m.group(1))] = (lon, lat)
    return out


# ---- relation membership ---------------------------------------------------

def _served_relations(v1_rows, rels, nodes) -> list[list[dict[str, str]]]:
    """Per station, the tags of every relation with a stop that snaps to it."""
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, row in enumerate(v1_rows):
        grid[(int(row[0] / SNAP_CELL_DEG), int(row[1] / SNAP_CELL_DEG))].append(idx)

    def nearest(lon: float, lat: float) -> int | None:
        gx, gy = int(lon / SNAP_CELL_DEG), int(lat / SNAP_CELL_DEG)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for idx in grid.get((gx + dx, gy + dy), ()):
                    row = v1_rows[idx]
                    d = math.hypot((row[0] - lon) * M_PER_DEG_LON, (row[1] - lat) * M_PER_DEG_LAT)
                    if d > STOP_SNAP_M:
                        continue
                    if row[4] == "tram_stop":
                        d += TRAM_STOP_PENALTY_M
                    if best is None or (d, idx) < best:
                        best = (d, idx)
        return None if best is None else best[1]

    served: list[list[dict[str, str]]] = [[] for _ in v1_rows]
    for tags, members in rels:
        seen: set[int] = set()
        for node_id, role in members:
            if role not in STOP_ROLES:
                continue
            coord = nodes.get(node_id)
            if coord is None:
                continue
            idx = nearest(*coord)
            if idx is None or idx in seen:
                continue
            seen.add(idx)
            served[idx].append(tags)
    return served


# ---- geometric neighbours --------------------------------------------------

def _near_line_signals(v1_rows, lines) -> list[tuple[bool, bool]]:
    """Per station: (JR line within 120 m, other operator's line within 120 m).

    Freight (貨物) and shinkansen line features are ignored: a freight line is
    not a passenger service, and shinkansen stops are decided from relations
    only (see _modes)."""
    grid_deg = GEOMETRY_CELL_DEG
    cells: dict[tuple[int, int], set[int]] = defaultdict(set)
    kinds: list[str] = []
    for li, ftr in enumerate(lines):
        props = ftr["properties"]
        operator = props.get("operator") or ""
        label = " ".join(
            filter(None, [props.get("name"), props.get("route_name"),
                          props.get("route_name_en"), props.get("operator")])
        )
        if FREIGHT_RE.search(operator) or FREIGHT_RE.search(_route_key(props)):
            kind = ""
        elif SHINKANSEN_RE.search(label):
            kind = ""
        elif _is_jr_operator(operator):
            kind = "jr"
        elif operator:
            kind = "other"
        else:
            kind = ""
        kinds.append(kind)
        if not kind:
            continue
        coords = ftr["geometry"]["coordinates"]
        for i, (x, y) in enumerate(coords):
            cells[(int(x / grid_deg), int(y / grid_deg))].add(li)
            if i + 1 < len(coords):
                nx, ny = coords[i + 1]
                seg = math.hypot((nx - x) * M_PER_DEG_LON, (ny - y) * M_PER_DEG_LAT)
                if seg > GEOMETRY_SAMPLE_M:
                    steps = int(seg / GEOMETRY_SAMPLE_M) + 1
                    for k in range(1, steps):
                        t = k / steps
                        cells[(int((x + t * (nx - x)) / grid_deg),
                               int((y + t * (ny - y)) / grid_deg))].add(li)

    near_sq = GEOMETRY_NEAR_M * GEOMETRY_NEAR_M
    out = []
    for row in v1_rows:
        p = (row[0], row[1])
        gx, gy = int(p[0] / grid_deg), int(p[1] / grid_deg)
        candidates: set[int] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                candidates |= cells.get((gx + dx, gy + dy), set())
        jr = other = False
        for li in sorted(candidates):
            kind = kinds[li]
            if (kind == "jr" and jr) or (kind == "other" and other):
                continue
            coords = lines[li]["geometry"]["coordinates"]
            if len(coords) == 1:
                hit = (
                    (coords[0][0] - p[0]) * M_PER_DEG_LON) ** 2 + (
                    (coords[0][1] - p[1]) * M_PER_DEG_LAT) ** 2 <= near_sq
            else:
                hit = any(
                    _point_seg_dist_sq_m(p, coords[i], coords[i + 1]) <= near_sq
                    for i in range(len(coords) - 1)
                )
            if hit:
                if kind == "jr":
                    jr = True
                else:
                    other = True
            if jr and other:
                break
        out.append((jr, other))
    return out


# ---- per-station signals ---------------------------------------------------

def _relation_signals(served: list[dict[str, str]]) -> dict:
    line_ids: set[tuple[str, str]] = set()
    operators: set[str] = set()
    limited: set[str] = set()
    jr_companies: set[str] = set()
    shinkansen = False
    for tags in served:
        name = _norm_line_name(tags.get("name"))
        ref = tags.get("ref") or ""
        blob = f"{name} {ref} {tags.get('network', '')} {tags.get('operator', '')}"
        # A relation named only after a shinkansen train (はやぶさ, かがやき)
        # with no line ref is a shinkansen service; with a ref the same word
        # is usually an in-conventional-line limited express (かもめ, つばめ).
        is_shinkansen = bool(SHINKANSEN_RE.search(blob)) or (
            bool(SHINKANSEN_TRAIN_RE.search(name))
            and tags.get("route") == "train" and not ref
        )
        shinkansen = shinkansen or is_shinkansen
        # Limited-express and shinkansen services count toward `ltd` only:
        # they ride existing track, so counting them as lines too would
        # double-count every mainline hub.
        if is_shinkansen or LIMITED_EXPRESS_RE.search(blob) or SHINKANSEN_TRAIN_RE.search(name):
            limited.add(name or ref)
        else:
            line_ids.add(_line_identity(tags, name))
        operator = _first_operator(tags.get("operator"))
        if operator:
            operators.add(operator)
        jr = _jr_company(tags.get("operator"), name)
        if not jr and JR_REF_RE.match(_first_ref(ref) or "x"):
            jr = "JR"
        if jr:
            jr_companies.add(jr)
    return {
        "lines": _station_line_count(line_ids),
        "operators": operators,
        "ltd": len(limited),
        "jr": bool(jr_companies),
        "shinkansen": shinkansen,
    }


def _modes(row: list, rel: dict, near_jr: bool, near_other: bool) -> int:
    """The three bits use three different strategies on purpose.

    4 shinkansen -- relation membership only.  By geometry 342 stations sit
    within 120 m of a shinkansen line against ~120 real stops: the elevated
    track passes straight over many small conventional-line stations.
    Relation stops give 118.

    2 JR -- relations OR geometry (a JR line within 120 m), with a veto.
    Relations alone give 3,089 stations against ~4,300 real JR stations;
    rural relations are incomplete (高山, 由布院, 長門峡, 熊野市, 幕別 have
    none).  Pure geometry gives 4,387 but flags private-railway stations
    that sit beside JR track (烏森/近鉄, 滝の茶屋/山陽, 上星川/相鉄,
    新浜松/遠州, 鳥羽街道/京阪).  So a geometric JR hit is vetoed when the
    station's relations name operators and none of them is JR: 4,290.

    1 other rail -- a non-JR operator in the relations, or the station is a
    tram_stop.  Geometry is consulted only when the relations name no
    operator at all: a subway tunnel passing under a station is always
    "near", which would give the JR-only 原宿 (副都心線 60 m below), JR難波
    and 城崎温泉 a subway badge.

    A station with no bit set falls back to 1 so it still draws something.
    """
    rel_ops = rel["operators"]
    rel_other = any(not _is_jr_operator(op) for op in rel_ops)
    modes = 0
    if rel["shinkansen"]:
        modes |= MODE_SHINKANSEN
    if rel["jr"] or (near_jr and not (rel_ops and not rel["jr"])):
        modes |= MODE_JR
    if rel_other or row[4] == "tram_stop" or (not rel_ops and near_other):
        modes |= MODE_RAIL
    return modes or MODE_RAIL


# ---- self-check ------------------------------------------------------------

EXPECTED_COUNTS = {
    "stations": 8954,
    "shinkansen": 118,
    "jr": 4290,
    "rail": 5004,
    "one_badge": 8547,
    "two_badges": 356,
    "three_badges": 51,
}
TOLERANCE = 0.03

# (name, lon, lat, expected modes).  Coordinates only disambiguate the name.
SPOT_CHECKS = (
    ("原宿", 139.7024, 35.6702, MODE_JR),
    ("JR難波", 135.4953, 34.6665, MODE_JR),
    ("梅田", 135.4977, 34.7034, MODE_RAIL),
    ("難波", 135.5018, 34.6637, MODE_RAIL),
    ("烏森", 136.8639, 35.1530, MODE_RAIL),
    ("高山", 137.2513, 36.1411, MODE_JR),
    ("東京", 139.7664, 35.6806, MODE_RAIL | MODE_JR | MODE_SHINKANSEN),
    ("新高岡", 137.0113, 36.7269, MODE_JR | MODE_SHINKANSEN),
)
SPOT_CHECK_RADIUS_M = 2000.0


def _self_check(rows: list[list]) -> None:
    modes = [r[9] for r in rows]
    counts = {
        "stations": len(rows),
        "shinkansen": sum(1 for m in modes if m & MODE_SHINKANSEN),
        "jr": sum(1 for m in modes if m & MODE_JR),
        "rail": sum(1 for m in modes if m & MODE_RAIL),
        "one_badge": sum(1 for m in modes if bin(m).count("1") == 1),
        "two_badges": sum(1 for m in modes if bin(m).count("1") == 2),
        "three_badges": sum(1 for m in modes if bin(m).count("1") == 3),
    }
    failures = []
    print("  self-check (actual / expected):")
    for key, expected in EXPECTED_COUNTS.items():
        actual = counts[key]
        ok = abs(actual - expected) <= expected * TOLERANCE
        print(f"    {key:13} {actual:5} / {expected:5}  {'ok' if ok else 'OUT OF RANGE'}")
        if not ok:
            failures.append(f"{key}={actual} (expected {expected} ±3%)")

    print("  spot checks:")
    for name, lon, lat, expected in SPOT_CHECKS:
        hits = [
            r for r in rows
            if r[2] == name and math.hypot((r[0] - lon) * M_PER_DEG_LON,
                                           (r[1] - lat) * M_PER_DEG_LAT) <= SPOT_CHECK_RADIUS_M
        ]
        if len(hits) != 1:
            failures.append(f"{name}: {len(hits)} candidate rows")
            print(f"    {name:6} {len(hits)} candidate rows  FAIL")
            continue
        actual = hits[0][9]
        ok = actual == expected
        print(f"    {name:6} modes={actual} expected={expected}  {'ok' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"{name}: modes={actual}, expected {expected}")
    if failures:
        raise SystemExit("station_source_v2 self-check failed: " + "; ".join(failures))


# ---- driver ----------------------------------------------------------------

def _dumps_row(values: list) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def build() -> list[list]:
    v1_raw, v1_rows = _load_v1()
    print(f"  v1: {len(v1_rows)} stations")
    lines = _load_transit(v1_rows)
    print(f"  japan.geojson: {len(lines)} lines, stations match v1")
    rels = _load_route_relations()
    wanted = {node_id for _, members in rels for node_id, _ in members}
    nodes = _load_stop_nodes(wanted)
    n_members = sum(len(m) for _, m in rels)
    n_resolved = sum(1 for _, m in rels for node_id, _ in m if node_id in nodes)
    print(f"  route relations: {len(rels)}, node members resolved {n_resolved}/{n_members}")
    served = _served_relations(v1_rows, rels, nodes)
    print(f"  stations served by >=1 relation: {sum(1 for s in served if s)}")
    near = _near_line_signals(v1_rows, lines)

    rows = []
    for row, rel_tags, (near_jr, near_other) in zip(v1_rows, served, near):
        rel = _relation_signals(rel_tags)
        rows.append(list(row) + [
            rel["lines"], len(rel["operators"]), rel["ltd"],
            _modes(row, rel, near_jr, near_other),
        ])

    # The first six fields must be v1's bytes, not merely equal values.
    for v1_row, row in zip(v1_rows, rows):
        if _dumps_row(row[:6]) != _dumps_row(v1_row):
            raise SystemExit(f"row drifted from v1: {v1_row}")
    if _dumps_row(v1_rows).encode("utf-8") not in v1_raw:
        raise SystemExit("v1 rows do not re-serialise to the v1 file's bytes")
    return rows


def main() -> None:
    rows = build()
    _self_check(rows)
    payload = {"v": 2, "fields": V2_FIELDS, "stations": rows}
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    atomic_write_text(STATION_SOURCE_V2, text)
    print(f"  wrote {STATION_SOURCE_V2} ({len(rows)} stations)")


if __name__ == "__main__":
    main()
