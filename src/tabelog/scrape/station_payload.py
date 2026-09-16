"""Build the content-addressed station payload used by the map.

The source is the compact six-field payload shipped with 4.3.0.  This module
applies reviewed display-tier overrides and adds deterministic, whole-zoom
label placement.  Placement uses
a conservative text-width envelope so the browser only has to select a
precomputed label set; it never lays out all stations at runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Iterable
from pathlib import Path


SOURCE_FIELDS = ["lon", "lat", "name", "name_en", "railway", "line_count"]
OUTPUT_FIELDS = SOURCE_FIELDS + ["display_tier"]
PAYLOAD_VERSION = 3
PLACEMENT_VERSION = 1
ZOOM_MIN = 12
ZOOM_MAX = 19
HIDDEN_MIN_ZOOM = ZOOM_MAX + 1
MIN_STATION_COUNT = 8_000
MAX_STATION_COUNT = 10_000
STATION_SOURCE_JSON = Path(__file__).with_name("station_source_v1.json")
STATION_IMPORTANCE_OVERRIDES_JSON = Path(__file__).with_name(
    "station_importance_overrides.json"
)
M_PER_DEG_LON = 91_000.0
M_PER_DEG_LAT = 111_000.0

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


def _effective_tier(row: list[object]) -> int:
    display_tier = int(row[6]) if len(row) > 6 else 0
    if 1 <= display_tier <= 3:
        return display_tier
    line_count = int(row[5])
    return 3 if line_count >= 6 else 2 if line_count >= 3 else 1


def _icon_size(row: list[object], zoom: int) -> float:
    if zoom <= 14:
        return 11.0
    return (11.0, 14.0, 18.0)[_effective_tier(row) - 1]


def _shown(row: list[object], zoom: int) -> bool:
    tier = _effective_tier(row)
    if zoom == 12:
        return tier >= 3
    if zoom == 13:
        return tier >= 2
    return zoom >= 14


def _label_shown(row: list[object], zoom: int) -> bool:
    if zoom == 12:
        return False
    if zoom == 13:
        return _effective_tier(row) >= 3
    return zoom >= 14


def _distance_m(row: list[object], near: list[object]) -> float:
    return math.hypot(
        (float(row[0]) - float(near[0])) * M_PER_DEG_LON,
        (float(row[1]) - float(near[1])) * M_PER_DEG_LAT,
    )


def apply_importance_overrides(
    rows: list[list[object]], override_path: Path = STATION_IMPORTANCE_OVERRIDES_JSON
) -> tuple[list[list[object]], dict[str, object]]:
    """Append display_tier and apply every reviewed rule exactly once."""
    raw = override_path.read_bytes()
    config = json.loads(raw)
    rules = config.get("rules") if isinstance(config, dict) else None
    if (
        not isinstance(config, dict)
        or config.get("version") != 1
        or not isinstance(config.get("reviewed"), str)
        or not isinstance(rules, list)
    ):
        raise ValueError(f"{override_path}: expected importance override version 1")

    output = [row.copy() + [0] for row in rows]
    matched_indices: set[int] = set()
    matches: list[dict[str, object]] = []
    for rule_index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"{override_path}: malformed rule {rule_index}")
        name = rule.get("name")
        near = rule.get("near")
        radius_m = rule.get("radius_m")
        display_tier = rule.get("display_tier")
        source = rule.get("source")
        if (
            not isinstance(name, str)
            or not isinstance(near, list)
            or len(near) != 2
            or not all(isinstance(value, (int, float)) for value in near)
            or not isinstance(radius_m, (int, float))
            or not 0 < radius_m <= 1_000
            or display_tier not in {1, 2, 3}
            or not isinstance(rule.get("reason"), str)
            or not rule["reason"].strip()
            or not isinstance(source, list)
            or not source
            or not all(isinstance(url, str) and url.startswith("https://") for url in source)
        ):
            raise ValueError(f"{override_path}: malformed rule {rule_index}")
        found = [
            i for i, row in enumerate(output)
            if row[2] == name and _distance_m(row, near) <= float(radius_m)
        ]
        if len(found) != 1:
            raise ValueError(
                f"{override_path}: rule {rule_index} {name!r} matched "
                f"{len(found)} stations, expected exactly 1"
            )
        row_index = found[0]
        if row_index in matched_indices:
            raise ValueError(
                f"{override_path}: rule {rule_index} duplicates station row {row_index}"
            )
        matched_indices.add(row_index)
        output[row_index][6] = display_tier
        matches.append({
            "row": row_index,
            "name": name,
            "displayTier": display_tier,
        })
    return output, {
        "version": config["version"],
        "reviewed": config["reviewed"],
        "overrideCount": len(matches),
        "configSha256": hashlib.sha256(raw).hexdigest(),
        "matches": matches,
    }


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
    lon, lat, _name, _name_en, _railway, _line_count, _display_tier = row
    x, y = _world_point(float(lon), float(lat), zoom)
    radius = _icon_size(row, zoom) / 2
    baseline = y - radius - LABEL_GAP_PX
    left = x - width / 2 - HALO_PX
    top = baseline - MEASURE_EM_PX - HALO_PX
    right = x + width / 2 + HALO_PX
    bottom = baseline + HALO_PX
    return left, top, right, bottom


def _placement_for(rows: list[list[object]], texts: list[str]) -> dict[str, list[float | int]]:
    widths = [conservative_text_width(text) for text in texts]
    visible_masks = [0] * len(rows)
    priority = sorted(
        range(len(rows)),
        key=lambda i: (-_effective_tier(rows[i]), -int(rows[i][5]), i),
    )
    for zoom in range(ZOOM_MIN, ZOOM_MAX + 1):
        index = _RectIndex()
        # Each integer zoom has one nationwide, deterministic placement pass.
        # Every tile at that zoom reads the same bit, so panning and tile load
        # order cannot change which labels are present.
        for row_index, row in enumerate(rows):
            if not _shown(row, zoom):
                continue
            x, y = _world_point(float(row[0]), float(row[1]), zoom)
            half = _icon_size(row, zoom) / 2 + ICON_GUARD_PX
            index.add((x - half, y - half, x + half, y + half), owner=row_index)

        for i in priority:
            if not texts[i] or not _label_shown(rows[i], zoom):
                continue
            rect = _label_rect(rows[i], widths[i], zoom)
            # The label is anchored to its own badge and may touch its halo;
            # the 2px guard applies to every *other* station badge.
            if index.intersects(rect, ignore_owner=i):
                continue
            index.add(rect)
            visible_masks[i] |= 1 << (zoom - ZOOM_MIN)

    return {
        "visibleMaskByItem": visible_masks,
        "labelWidthByItem": widths,
    }


def build_station_payload(source_path: Path) -> tuple[bytes, dict[str, object]]:
    """Validate *source_path* and return deterministic v3 payload bytes."""
    source_raw = source_path.read_bytes()
    source_sha = hashlib.sha256(source_raw).hexdigest()
    source = json.loads(source_raw)
    rows = source.get("stations") if isinstance(source, dict) else None
    if (
        not isinstance(source, dict)
        or source.get("v") != 1
        or source.get("fields") != SOURCE_FIELDS
        or not isinstance(rows, list)
    ):
        raise ValueError(f"{source_path}: expected the compact v1 six-field station source")
    if not MIN_STATION_COUNT <= len(rows) <= MAX_STATION_COUNT:
        raise ValueError(
            f"{source_path}: expected {MIN_STATION_COUNT:,}–{MAX_STATION_COUNT:,} stations, "
            f"got {len(rows):,}"
        )

    source_rows: list[list[object]] = []
    for i, row in enumerate(rows):
        if (
            not isinstance(row, list)
            or len(row) != 6
            or not isinstance(row[0], (int, float))
            or not isinstance(row[1], (int, float))
            or not isinstance(row[2], str)
            or not isinstance(row[3], str)
            or not isinstance(row[4], str)
            or not isinstance(row[5], int)
        ):
            raise ValueError(f"{source_path}: malformed row {i}")
        source_rows.append(row.copy())

    output_rows, importance = apply_importance_overrides(source_rows)

    local_texts = [str(row[2]) for row in output_rows]
    en_texts = [str(row[3]).strip() or str(row[2]) for row in output_rows]
    profiles = {
        "local-max130": _placement_for(output_rows, local_texts),
        "en-max130": _placement_for(output_rows, en_texts),
    }
    payload = {
        "v": PAYLOAD_VERSION,
        "fields": OUTPUT_FIELDS,
        "stations": output_rows,
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
        },
        "importance": {
            key: value for key, value in importance.items() if key != "matches"
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    counts = {
        key: {
            str(zoom): sum(
                1
                for mask in profile["visibleMaskByItem"]
                if int(mask) & (1 << (zoom - ZOOM_MIN))
            )
            for zoom in range(ZOOM_MIN, ZOOM_MAX + 1)
        }
        for key, profile in profiles.items()
    }
    return raw, {
        "sourceSha256": source_sha,
        "payloadSha256": hashlib.sha256(raw).hexdigest(),
        "stationCount": len(output_rows),
        "profileCounts": counts,
        "importance": importance,
    }
