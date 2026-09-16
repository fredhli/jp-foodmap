"""Build the content-addressed station payload used by the map.

The source is the checked-in ten-field v2 station table
(``build_station_source.py``).  This module derives a continuous importance
score and a size tier from that table alone -- there are no per-station
overrides -- then selects which station icons each whole zoom draws with a
deterministic Poisson-disk pass, and finally adds whole-zoom label placement.
Placement uses a conservative text-width envelope so the browser only has to
select precomputed icon and label sets; it never lays out all stations at
runtime.

Restaurant coordinates feed a density floor at z12-z14.  They are passed in
by the caller (``map.py`` hands over the rows it is about to publish as
``restaurants.json``) and never read from disk here, so ``verify_build.py``
can rebuild the exact payload from the published files.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path


SOURCE_VERSION = 2
SOURCE_FIELDS = [
    "lon", "lat", "name", "name_en", "railway", "line_count",
    "lines", "operators", "ltd", "modes",
]
# The first six source fields are copied byte-for-byte; lines / operators /
# ltd only feed importance and are not shipped.  display_tier keeps its v3
# position so an older cached page still finds it at index 6.
OUTPUT_FIELDS = SOURCE_FIELDS[:6] + ["display_tier", "modes", "importance"]
PAYLOAD_VERSION = 4
PLACEMENT_VERSION = 1
ICONS_VERSION = 1
ZOOM_MIN = 12
ZOOM_MAX = 19
HIDDEN_MIN_ZOOM = ZOOM_MAX + 1
MIN_STATION_COUNT = 8_000
MAX_STATION_COUNT = 10_000
STATION_SOURCE_JSON = Path(__file__).with_name("station_source_v2.json")

# modes bit mask, written by build_station_source.py.
MODE_RAIL = 1
MODE_JR = 2
MODE_SHINKANSEN = 4
MODE_ALL = MODE_RAIL | MODE_JR | MODE_SHINKANSEN

# 4.3.6 spec §2.2: continuous importance.
IMPORTANCE_ALGORITHM = "continuous-v1"
W_LINES = 3.0
W_OPERATORS = 2.0
W_LTD = 2.5
SHINKANSEN_BONUS = 4.0
TRAM_STOP_FACTOR = 0.55
IMPORTANCE_DIGITS = 3

# §2.3: size tier.  display_tier = size_index + 1.
TIER3_MIN_IMPORTANCE = 14.0
TIER2_MIN_IMPORTANCE = 9.0
STATION_SIZES_PX = [11, 14, 18]
BADGE_GAP_PX = 2

# §2.4: Poisson-disk icon selection, one pass per whole zoom.
# z12/z13 thin the map; from z14 the spacing is pure badge non-overlap, which
# keeps 4.3.5a's accepted "every station from z14" density.
SPACING_PX = {12: 140, 13: 110, 14: 0, 15: 0, 16: 0, 17: 0, 18: 0, 19: 0}
SPACING_EXTRA_PX = 4
# §3.1: badges drawn per zoom, index = zoom - ZOOM_MIN.
BADGE_COUNT_BY_ZOOM = [1, 1, 2, 3, 3, 3, 3, 3]
# Restaurant density floor, z12-z14 only.  Steps are (min count, min
# importance), highest first; a station below its floor skips the pass.
DENSITY_GRID_DEG = 0.01
DENSITY_FLOOR_STEPS = [(120, 14.0), (40, 9.0)]
# z14 is where a restaurant search needs stations to orient by, so the floor
# stops at z13.
DENSITY_FLOOR_MAX_ZOOM = 13

# The renderer draws labels at 11 CSS px before the user's text scale.  One
# 130% envelope covers every supported fontScale (100/115/130) and UI density
# (.95/1).  Widths exclude halo and icon clearance; those are recorded here
# and applied to collision rectangles below and by the renderer at runtime.
BASE_FONT_PX = 11.0
MAX_FONT_SCALE = 1.30
MEASURE_EM_PX = BASE_FONT_PX * MAX_FONT_SCALE
HALO_PX = 3.0
LABEL_GAP_PX = 3.0
ICON_GUARD_PX = 2.0
GRID_CELL_PX = 64.0
FONT_STACK = 'system-ui,-apple-system,"Hiragino Sans","Noto Sans CJK JP",sans-serif'
MEASUREMENT_CANVAS_FONT = f"600 {MEASURE_EM_PX:g}px {FONT_STACK}"


def _char_advance_em(char: str) -> float:
    """Conservative advance bound for the declared sans-serif font stacks.

    This is deliberately an upper-bound table rather than a host-font
    measurement: builds stay byte-identical on Linux, macOS and Windows.
    Full-width/CJK glyphs receive 1.15 em, Latin letters and digits 1.10 em,
    and all remaining printable glyphs 1.15 em.  These exceed typical advances
    in the declared sans-serif stack and absorb fallback-font variation.
    """
    if not char or unicodedata.combining(char):
        return 0.0
    if char.isspace():
        return 0.50
    eaw = unicodedata.east_asian_width(char)
    if eaw in {"W", "F", "A"}:
        return 1.15
    category = unicodedata.category(char)
    if category.startswith(("L", "N")):
        return 1.10
    return 1.15


def conservative_text_width(text: str) -> float:
    """Return the max-130 label width in CSS px, rounded upward to 0.1px."""
    return math.ceil(sum(_char_advance_em(c) for c in text) * MEASURE_EM_PX * 10) / 10


def _world_point(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    size = 256.0 * (2**zoom)
    x = (lon + 180.0) / 360.0 * size
    sin_lat = math.sin(math.radians(max(-85.05112878, min(85.05112878, lat))))
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * size
    return x, y


def _popcount(value: int) -> int:
    return value.bit_count()


def station_importance(row: list[object]) -> float:
    """§2.2 continuous importance of one v2 source row, rounded to 3 places.

    The rounded value is the one written to the payload, and it is also the
    value every threshold and ordering below compares, so a reader holding only
    the payload derives the same tier as the build did.
    """
    lines, operators, ltd, modes = (int(row[6]), int(row[7]), int(row[8]), int(row[9]))
    score = (
        W_LINES * math.log2(1 + lines)
        + W_OPERATORS * math.log2(1 + operators)
        + W_LTD * math.log2(1 + ltd)
        + (SHINKANSEN_BONUS if modes & MODE_SHINKANSEN else 0.0)
    )
    if row[4] == "tram_stop":
        score *= TRAM_STOP_FACTOR
    return round(score, IMPORTANCE_DIGITS)


def _size_index(importance: float) -> int:
    if importance >= TIER3_MIN_IMPORTANCE:
        return 2
    if importance >= TIER2_MIN_IMPORTANCE:
        return 1
    return 0


def _row_importance(row: list[object]) -> float:
    return float(row[8])


def _effective_tier(row: list[object]) -> int:
    return int(row[6])


def _icon_size(row: list[object], zoom: int) -> float:
    # 4.3.6: the size tier applies from z12 up; 4.3.5a drew 11px at z<=14.
    return float(STATION_SIZES_PX[_effective_tier(row) - 1])


def _badge_count(row: list[object], zoom: int) -> int:
    """How many badges the renderer draws for *row* at *zoom* (§3.1)."""
    return min(BADGE_COUNT_BY_ZOOM[zoom - ZOOM_MIN], _popcount(int(row[7])))


def _icon_strip_width(row: list[object], zoom: int) -> float:
    n = _badge_count(row, zoom)
    return n * _icon_size(row, zoom) + (n - 1) * BADGE_GAP_PX


def _label_shown(row: list[object], zoom: int) -> bool:
    if zoom == 12:
        return False
    if zoom == 13:
        return _effective_tier(row) >= 3
    return zoom >= 14


def _cell(value: float) -> int:
    return math.floor(value / DENSITY_GRID_DEG)


def restaurant_coords_from_rows(rows: Iterable[Mapping[str, object]]) -> list[tuple[float, float]]:
    """(lon, lat) of every published restaurant row with numeric coordinates.

    map.py passes its ``core_rows`` through this; verify_build passes the
    parsed ``restaurants.json``.  Both therefore feed the same list in.
    """
    coords: list[tuple[float, float]] = []
    for row in rows:
        lon, lat = row.get("lon"), row.get("lat")
        if (
            isinstance(lon, (int, float)) and not isinstance(lon, bool)
            and isinstance(lat, (int, float)) and not isinstance(lat, bool)
            and math.isfinite(lon) and math.isfinite(lat)
        ):
            coords.append((float(lon), float(lat)))
    return coords


def _restaurant_density(
    rows: list[list[object]], restaurant_coords: Iterable[tuple[float, float]]
) -> list[int]:
    """Restaurants in the 3x3 block of 0.01-degree cells around each station."""
    grid: dict[tuple[int, int], int] = {}
    for lon, lat in restaurant_coords:
        key = (_cell(lon), _cell(lat))
        grid[key] = grid.get(key, 0) + 1
    density: list[int] = []
    for row in rows:
        gx, gy = _cell(float(row[0])), _cell(float(row[1]))
        density.append(sum(
            grid.get((gx + dx, gy + dy), 0) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
        ))
    return density


def _density_floor(count: int) -> float:
    for min_count, floor in DENSITY_FLOOR_STEPS:
        if count >= min_count:
            return floor
    return 0.0


def _icon_masks(rows: list[list[object]], density: list[int]) -> list[int]:
    """§2.4 Poisson-disk selection: bit (zoom - ZOOM_MIN) = icon drawn."""
    masks = [0] * len(rows)
    order = sorted(
        range(len(rows)),
        key=lambda i: (-_row_importance(rows[i]), str(rows[i][2]), i),
    )
    floors = [_density_floor(count) for count in density]
    for zoom in range(ZOOM_MIN, ZOOM_MAX + 1):
        base = float(SPACING_PX[zoom])
        max_half = (
            BADGE_COUNT_BY_ZOOM[zoom - ZOOM_MIN] * max(STATION_SIZES_PX)
            + (BADGE_COUNT_BY_ZOOM[zoom - ZOOM_MIN] - 1) * BADGE_GAP_PX
        ) / 2
        # No accepted pair can conflict beyond this distance, so a candidate
        # only needs to look at its own and the eight neighbouring cells.
        cell = max(base, 2 * max_half + SPACING_EXTRA_PX)
        accepted: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
        for i in order:
            row = rows[i]
            if zoom <= DENSITY_FLOOR_MAX_ZOOM and _row_importance(row) < floors[i]:
                continue
            x, y = _world_point(float(row[0]), float(row[1]), zoom)
            half = _icon_strip_width(row, zoom) / 2
            major = _effective_tier(row) == 3
            gx, gy = math.floor(x / cell), math.floor(y / cell)
            blocked = False
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for ox, oy, other_half, other_major in accepted.get((gx + dx, gy + dy), ()):
                        # Two top-tier hubs only need their badges apart: a
                        # 140px disc around 新宿 would otherwise hide 渋谷 at z12.
                        floor_px = 0.0 if (major and other_major) else base
                        spacing = max(floor_px, half + other_half + SPACING_EXTRA_PX)
                        if math.hypot(x - ox, y - oy) < spacing:
                            blocked = True
                            break
                    if blocked:
                        break
                if blocked:
                    break
            if blocked:
                continue
            accepted.setdefault((gx, gy), []).append((x, y, half, major))
            masks[i] |= 1 << (zoom - ZOOM_MIN)
    return masks


class _RectIndex:
    """Small deterministic uniform-grid index for collision rectangles."""

    def __init__(self) -> None:
        self._cells: dict[
            tuple[int, int],
            list[tuple[tuple[float, float, float, float], int | None]],
        ] = {}

    @staticmethod
    def _keys(rect: tuple[float, float, float, float]) -> Iterable[tuple[int, int]]:
        left, top, right, bottom = rect
        x0, x1 = math.floor(left / GRID_CELL_PX), math.floor(right / GRID_CELL_PX)
        y0, y1 = math.floor(top / GRID_CELL_PX), math.floor(bottom / GRID_CELL_PX)
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                yield gx, gy

    def intersects(
        self,
        rect: tuple[float, float, float, float],
        *,
        ignore_owner: int | None = None,
    ) -> bool:
        left, top, right, bottom = rect
        seen: set[int] = set()
        for key in self._keys(rect):
            for entry in self._cells.get(key, ()):
                marker = id(entry)
                if marker in seen:
                    continue
                seen.add(marker)
                other, owner = entry
                if ignore_owner is not None and owner == ignore_owner:
                    continue
                ol, ot, oright, ob = other
                if left < oright and right > ol and top < ob and bottom > ot:
                    return True
        return False

    def add(
        self, rect: tuple[float, float, float, float], owner: int | None = None
    ) -> None:
        entry = (rect, owner)
        for key in self._keys(rect):
            self._cells.setdefault(key, []).append(entry)


def _label_rect(
    row: list[object], width: float, zoom: int
) -> tuple[float, float, float, float]:
    # The badge strip is centred on the station, so the label's horizontal
    # centre stays the station coordinate.
    x, y = _world_point(float(row[0]), float(row[1]), zoom)
    radius = _icon_size(row, zoom) / 2
    baseline = y - radius - LABEL_GAP_PX
    left = x - width / 2 - HALO_PX
    top = baseline - MEASURE_EM_PX - HALO_PX
    right = x + width / 2 + HALO_PX
    bottom = baseline + HALO_PX
    return left, top, right, bottom


def _placement_for(
    rows: list[list[object]], texts: list[str], icon_masks: list[int]
) -> dict[str, list[float | int]]:
    widths = [conservative_text_width(text) for text in texts]
    visible_masks = [0] * len(rows)
    priority = sorted(
        range(len(rows)),
        key=lambda i: (-_row_importance(rows[i]), str(rows[i][2]), i),
    )
    for zoom in range(ZOOM_MIN, ZOOM_MAX + 1):
        bit = 1 << (zoom - ZOOM_MIN)
        index = _RectIndex()
        # Each integer zoom has one nationwide, deterministic placement pass.
        # Every tile at that zoom reads the same bit, so panning and tile load
        # order cannot change which labels are present.
        for row_index, row in enumerate(rows):
            if not icon_masks[row_index] & bit:
                continue
            x, y = _world_point(float(row[0]), float(row[1]), zoom)
            half_w = _icon_strip_width(row, zoom) / 2 + ICON_GUARD_PX
            half_h = _icon_size(row, zoom) / 2 + ICON_GUARD_PX
            index.add((x - half_w, y - half_h, x + half_w, y + half_h), owner=row_index)

        for i in priority:
            # A label never floats over a station whose badges are not drawn.
            if not icon_masks[i] & bit:
                continue
            if not texts[i] or not _label_shown(rows[i], zoom):
                continue
            rect = _label_rect(rows[i], widths[i], zoom)
            # The label is anchored to its own badge strip and may touch its
            # halo; the 2px guard applies to every *other* station's strip.
            if index.intersects(rect, ignore_owner=i):
                continue
            index.add(rect)
            visible_masks[i] |= bit

    return {
        "visibleMaskByItem": visible_masks,
        "labelWidthByItem": widths,
    }


def _mask_counts(masks: list[int]) -> dict[str, int]:
    return {
        str(zoom): sum(1 for mask in masks if int(mask) & (1 << (zoom - ZOOM_MIN)))
        for zoom in range(ZOOM_MIN, ZOOM_MAX + 1)
    }


def build_station_payload(
    source_path: Path, restaurant_coords: Iterable[tuple[float, float]]
) -> tuple[bytes, dict[str, object]]:
    """Validate *source_path* and return deterministic v4 payload bytes.

    *restaurant_coords* is the ``(lon, lat)`` of every published restaurant
    (see ``restaurant_coords_from_rows``); it drives the z12-z14 density floor.
    """
    coords = list(restaurant_coords)
    for i, pair in enumerate(coords):
        if (
            not isinstance(pair, (tuple, list))
            or len(pair) != 2
            or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in pair
            )
        ):
            raise ValueError(f"restaurant coordinate {i} is not a finite (lon, lat) pair")

    source_raw = source_path.read_bytes()
    source_sha = hashlib.sha256(source_raw).hexdigest()
    source = json.loads(source_raw)
    rows = source.get("stations") if isinstance(source, dict) else None
    if (
        not isinstance(source, dict)
        or source.get("v") != SOURCE_VERSION
        or source.get("fields") != SOURCE_FIELDS
        or not isinstance(rows, list)
    ):
        raise ValueError(f"{source_path}: expected the v2 ten-field station source")
    if not MIN_STATION_COUNT <= len(rows) <= MAX_STATION_COUNT:
        raise ValueError(
            f"{source_path}: expected {MIN_STATION_COUNT:,}–{MAX_STATION_COUNT:,} stations, "
            f"got {len(rows):,}"
        )

    output_rows: list[list[object]] = []
    for i, row in enumerate(rows):
        if (
            not isinstance(row, list)
            or len(row) != len(SOURCE_FIELDS)
            or not isinstance(row[0], (int, float))
            or not isinstance(row[1], (int, float))
            or not isinstance(row[2], str)
            or not isinstance(row[3], str)
            or not isinstance(row[4], str)
            or not all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0
                for v in row[5:10]
            )
            or not 1 <= row[9] <= MODE_ALL
        ):
            raise ValueError(f"{source_path}: malformed row {i}")
        importance = station_importance(row)
        output_rows.append(
            row[:6] + [_size_index(importance) + 1, row[9], importance]
        )

    density = _restaurant_density(output_rows, coords)
    icon_masks = _icon_masks(output_rows, density)

    local_texts = [str(row[2]) for row in output_rows]
    en_texts = [str(row[3]).strip() or str(row[2]) for row in output_rows]
    profiles = {
        "local-max130": _placement_for(output_rows, local_texts, icon_masks),
        "en-max130": _placement_for(output_rows, en_texts, icon_masks),
    }
    tier_counts = [
        sum(1 for row in output_rows if row[6] == tier) for tier in (1, 2, 3)
    ]
    payload = {
        "v": PAYLOAD_VERSION,
        "fields": OUTPUT_FIELDS,
        "stations": output_rows,
        "icons": {
            "version": ICONS_VERSION,
            "zoomMin": ZOOM_MIN,
            "zoomMax": ZOOM_MAX,
            "maskByItem": icon_masks,
            "sizesPx": STATION_SIZES_PX,
            "gapPx": BADGE_GAP_PX,
            "badgeCountByZoom": BADGE_COUNT_BY_ZOOM,
            "spacingPx": {str(zoom): SPACING_PX[zoom] for zoom in range(ZOOM_MIN, ZOOM_MAX + 1)},
            "densityFloor": {
                "grid": DENSITY_GRID_DEG,
                "steps": [[count, floor] for count, floor in DENSITY_FLOOR_STEPS],
                "maxZoom": DENSITY_FLOOR_MAX_ZOOM,
            },
        },
        "placement": {
            "version": PLACEMENT_VERSION,
            "zoomMin": ZOOM_MIN,
            "zoomMax": ZOOM_MAX,
            "profiles": profiles,
            "measurement": {
                "algorithm": "unicode-eaw-upper-bound-v1",
                "fontStacks": {
                    "local-max130": FONT_STACK,
                    "en-max130": FONT_STACK,
                },
                "measurementCanvasFont": MEASUREMENT_CANVAS_FONT,
                "baseFontCssPx": BASE_FONT_PX,
                "fontWeight": 600,
                "fontScaleEnvelope": [100, 115, 130],
                "uiDensityEnvelope": [0.95, 1.0],
                "measuredAtFontScale": 130,
                "measuredAtUiDensity": 1.0,
                "emCssPx": MEASURE_EM_PX,
                "advanceEm": {
                    "eastAsianWideFullAmbiguous": 1.15,
                    "latinLetterDigit": 1.10,
                    "space": 0.50,
                    "otherPrintable": 1.15,
                },
                "haloCssPx": HALO_PX,
                "labelGapCssPx": LABEL_GAP_PX,
                "iconGuardCssPx": ICON_GUARD_PX,
                "widthIncludesHalo": False,
            },
        },
        "source": {
            "sha256": source_sha,
            "fields": SOURCE_FIELDS,
            "stationCount": len(output_rows),
            "restaurantCount": len(coords),
        },
        "importance": {
            "algorithm": IMPORTANCE_ALGORITHM,
            "weights": {
                "lines": W_LINES,
                "operators": W_OPERATORS,
                "ltd": W_LTD,
                "shinkansen": SHINKANSEN_BONUS,
            },
            "tramStopFactor": TRAM_STOP_FACTOR,
            "roundDigits": IMPORTANCE_DIGITS,
            "tierMinImportance": [0.0, TIER2_MIN_IMPORTANCE, TIER3_MIN_IMPORTANCE],
            "tierCounts": tier_counts,
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    counts = {
        key: _mask_counts(profile["visibleMaskByItem"])
        for key, profile in profiles.items()
    }
    return raw, {
        "sourceSha256": source_sha,
        "payloadSha256": hashlib.sha256(raw).hexdigest(),
        "stationCount": len(output_rows),
        "restaurantCount": len(coords),
        "tierCounts": tier_counts,
        "iconCounts": _mask_counts(icon_masks),
        "profileCounts": counts,
    }
