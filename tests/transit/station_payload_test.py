#!/usr/bin/env python3
"""Contract checks for the generated station payload.

Run after the normal build::

    uv run python tests/transit/station_payload_test.py

4.3.6 replaced the v3 payload (18 reviewed per-station overrides on top of a
raw line-count tier) with v4: a continuous importance score, a size tier
derived from it, a modes bit mask, and a build-time Poisson-disk icon
selection.  Every assertion below that replaced a 4.3.5a one says which.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "data"
SCRAPE = ROOT / "src" / "tabelog" / "scrape"
sys.path.insert(0, str(ROOT / "src"))
from tabelog.scrape.station_payload import (  # noqa: E402
    STATION_SOURCE_JSON,
    build_station_payload,
    restaurant_coords_from_rows,
)

# 4.3.6: v3's seven fields plus modes and importance (spec §2.6).
EXPECTED_FIELDS = [
    "lon", "lat", "name", "name_en", "railway", "line_count", "display_tier",
    "modes", "importance",
]
EXPECTED_PROFILES = {"local-max130", "en-max130"}
# Spec §2.4 / §3.1, written out here rather than imported so a silent edit to
# station_payload.py's constants fails this test.
# 4.3.6 integration: z14+ is pure badge non-overlap (4.3.5a drew every
# station from z14), and two tier-3 hubs are exempt from the z12/z13 disc.
SPACING = {12: 140, 13: 110, 14: 0, 15: 0, 16: 0, 17: 0, 18: 0, 19: 0}
BADGES_BY_ZOOM = [1, 1, 2, 3, 3, 3, 3, 3]
SIZES = [11, 14, 18]
GAP = 2


def built_payload() -> tuple[Path, bytes, dict]:
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    names = sorted(set(re.findall(r"data/(stations\.[0-9a-f]{12}\.json)", html)))
    if len(names) != 1:
        raise AssertionError(f"expected one station payload reference, found {names}")
    path = DATA / names[0]
    if not path.exists():
        raise AssertionError(f"referenced station payload is missing: {path}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    match = re.fullmatch(r"stations\.([0-9a-f]{12})\.json", path.name)
    if not match or match.group(1) != digest[:12]:
        raise AssertionError(
            f"{path.name} is not named from its content SHA-256 {digest[:12]}"
        )
    return path, raw, json.loads(raw)


def world_point(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    size = 256.0 * (2**zoom)
    x = (lon + 180.0) / 360.0 * size
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * size
    return x, y


def badge_count(row: list, zoom: int) -> int:
    return min(BADGES_BY_ZOOM[zoom - 12], bin(row[7]).count("1"))


def strip_half(row: list, zoom: int) -> float:
    n = badge_count(row, zoom)
    size = SIZES[row[6] - 1]
    return (n * size + (n - 1) * GAP) / 2


class StationPayloadContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path, cls.raw, cls.payload = built_payload()
        cls.rows = cls.payload.get("stations")
        cls.icon_masks = cls.payload["icons"]["maskByItem"]
        cls.by_name: dict[str, list[list]] = {}
        for row in cls.rows:
            cls.by_name.setdefault(row[2], []).append(row)

    def one(self, name: str) -> list:
        rows = self.by_name.get(name, [])
        self.assertEqual(len(rows), 1, f"expected exactly one station named {name}")
        return rows[0]

    # Replaces test_v3_shape_and_real_corpus: v == 4, nine fields, and
    # display_tier is always 1-3 (4.3.5a allowed 0 = "no override").
    def test_v4_shape_and_real_corpus(self) -> None:
        self.assertEqual(self.payload.get("v"), 4)
        self.assertEqual(self.payload.get("fields"), EXPECTED_FIELDS)
        self.assertIsInstance(self.rows, list)
        self.assertEqual(len(self.rows), 8954)

        for i, row in enumerate(self.rows):
            with self.subTest(row=i):
                self.assertEqual(len(row), len(EXPECTED_FIELDS))
                lon, lat, name, name_en, railway, line_count, tier, modes, imp = row
                self.assertIsInstance(lon, (int, float))
                self.assertIsInstance(lat, (int, float))
                self.assertTrue(122 <= lon <= 154)
                self.assertTrue(20 <= lat <= 46)
                self.assertIsInstance(name, str)
                self.assertIsInstance(name_en, str)
                self.assertIsInstance(railway, str)
                self.assertGreaterEqual(line_count, 0)
                self.assertIn(tier, {1, 2, 3})
                self.assertIsInstance(modes, int)
                self.assertTrue(1 <= modes <= 7)
                self.assertIsInstance(imp, (int, float))
                self.assertGreaterEqual(imp, 0)

    # Unchanged intent (principle 4): coordinates, names, railway and the raw
    # line_count stay byte-identical to v1, in v1 order.  v2's first six
    # fields must equal v1 too.
    def test_rows_retain_source_geographic_order(self) -> None:
        v1 = json.loads((SCRAPE / "station_source_v1.json").read_text(encoding="utf-8"))
        v2 = json.loads(STATION_SOURCE_JSON.read_text(encoding="utf-8"))
        self.assertEqual([row[:6] for row in self.rows], v1["stations"])
        self.assertEqual([row[:6] for row in v2["stations"]], v1["stations"])

    # Replaces test_reviewed_importance_overrides_are_sparse_and_exact: there
    # are no overrides any more (spec §2.1).  Every tier is recomputed from
    # the v2 source by the §2.2 formula and §2.3 thresholds, and the big hubs
    # the 18 overrides used to force now reach tier 3 on their own.
    def test_importance_and_tier_follow_the_formula_without_overrides(self) -> None:
        self.assertFalse((SCRAPE / "station_importance_overrides.json").exists())
        importance = self.payload.get("importance")
        self.assertEqual(importance.get("algorithm"), "continuous-v1")
        self.assertNotIn("overrideCount", importance)
        v2 = json.loads(STATION_SOURCE_JSON.read_text(encoding="utf-8"))["stations"]
        for i, (src, row) in enumerate(zip(v2, self.rows)):
            lines, operators, ltd, modes = src[6:10]
            score = (3.0 * math.log2(1 + lines) + 2.0 * math.log2(1 + operators)
                     + 2.5 * math.log2(1 + ltd) + (4.0 if modes & 4 else 0.0))
            if src[4] == "tram_stop":
                score *= 0.55
            score = round(score, 3)
            tier = 3 if score >= 14.0 else 2 if score >= 9.0 else 1
            with self.subTest(row=i, station=row[2]):
                self.assertEqual(row[8], score)
                self.assertEqual(row[6], tier)
                self.assertEqual(row[7], modes)
        tiers = [sum(1 for row in self.rows if row[6] == t) for t in (1, 2, 3)]
        self.assertEqual(importance.get("tierCounts"), tiers)
        # Spec §2.3 estimates tier3 ~158 / tier2 ~480; the shipped source
        # gives 162 / 522.  Guard the order of magnitude, not the estimate.
        self.assertTrue(130 <= tiers[2] <= 200, tiers)
        self.assertTrue(400 <= tiers[1] <= 650, tiers)
        for name in ("東京", "品川", "新宿", "渋谷", "新大阪", "大阪", "博多", "札幌"):
            with self.subTest(hub=name):
                self.assertEqual(self.one(name)[6], 3)

    # New in 4.3.6 (spec §1.2 spot checks and principle 1): icon type is what
    # you can ride, orthogonal to size.
    def test_modes_spot_checks(self) -> None:
        self.assertEqual(self.one("梅田")[7], 1)
        self.assertEqual(self.one("大阪")[7], 3)
        self.assertEqual(self.one("原宿")[7], 2)
        self.assertEqual(self.one("JR難波")[7], 2)
        self.assertEqual(self.one("難波")[7], 1)
        self.assertEqual(self.one("東京")[7], 7)
        shinkansen = sum(1 for row in self.rows if row[7] & 4)
        jr = sum(1 for row in self.rows if row[7] & 2)
        self.assertLessEqual(abs(shinkansen - 118), 5)
        self.assertLessEqual(abs(jr - 4290), 4290 * 0.03)
        # Karuizawa carries three modes but is not a mega-hub by badges alone.
        self.assertGreater(self.one("渋谷")[8], self.one("軽井沢")[8])

    def test_precomputed_label_profiles_match_every_station(self) -> None:
        placement = self.payload.get("placement")
        self.assertIsInstance(placement, dict)
        self.assertEqual(placement.get("version"), 1)
        self.assertEqual(placement.get("zoomMin"), 12)
        self.assertEqual(placement.get("zoomMax"), 19)
        self.assertEqual(set(placement.get("profiles", {})), EXPECTED_PROFILES)
        self.assertIsInstance(placement.get("measurement"), dict)
        self.assertEqual(placement["measurement"].get("iconGuardCssPx"), 2)

        for name, profile in placement["profiles"].items():
            masks = profile.get("visibleMaskByItem")
            widths = profile.get("labelWidthByItem")
            with self.subTest(profile=name):
                self.assertEqual(len(masks), len(self.rows))
                self.assertEqual(len(widths), len(self.rows))
                self.assertTrue(all(isinstance(mask, int) and 0 <= mask <= 255 for mask in masks))
                self.assertTrue(all(
                    isinstance(width, (int, float)) and width >= 0
                    for width in widths
                ))

    # Replaces test_distant_label_candidates_follow_station_tiers: the tier is
    # now row[6] directly (4.3.5a derived an "effective tier" from line_count
    # when no override applied), and a label bit additionally requires the
    # icon bit (spec §2.5: a label never floats without its badge).
    def test_distant_label_candidates_follow_station_tiers(self) -> None:
        for name, profile in self.payload["placement"]["profiles"].items():
            for i, (row, mask) in enumerate(zip(self.rows, profile["visibleMaskByItem"])):
                if mask & 1:
                    self.fail(f"{name}: {row[2]} labelled at z12")
                if (mask & 2) and row[6] < 3:
                    self.fail(f"{name}: non-tier-3 {row[2]} labelled at z13")
                if mask & ~self.icon_masks[i]:
                    self.fail(f"{name}: {row[2]} has a label without an icon ({mask} vs "
                              f"{self.icon_masks[i]})")

    # New in 4.3.6 (spec §2.6): the icons block and its constants.
    def test_icons_block(self) -> None:
        icons = self.payload["icons"]
        self.assertEqual(icons["version"], 1)
        self.assertEqual((icons["zoomMin"], icons["zoomMax"]), (12, 19))
        self.assertEqual(icons["sizesPx"], SIZES)
        self.assertEqual(icons["gapPx"], GAP)
        self.assertEqual(icons["badgeCountByZoom"], BADGES_BY_ZOOM)
        self.assertEqual(icons["spacingPx"], {str(z): d for z, d in SPACING.items()})
        self.assertEqual(icons["densityFloor"],
                         {"grid": 0.01, "steps": [[120, 14.0], [40, 9.0]], "maxZoom": 13})
        self.assertEqual(len(self.icon_masks), len(self.rows))
        self.assertTrue(all(isinstance(m, int) and 0 <= m <= 255 for m in self.icon_masks))
        # Replaces 4.3.5a's "z12 = tier >= 2" visibility rule: the Poisson
        # pass decides.  Deeper zooms never show fewer stations, and from z16
        # every station is drawn.
        counts = [sum(1 for m in self.icon_masks if m & (1 << (z - 12))) for z in range(12, 20)]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[4], len(self.rows))
        self.assertTrue(1500 <= counts[0] <= 2800, counts)

    # New in 4.3.6 (spec §2.4): an independent re-check of the spacing rule
    # and the z12-z13 restaurant density floor (the spec said z12-z14; z14 was
    # dropped at integration so dense restaurant areas keep their stations).
    def test_icon_selection_respects_spacing_and_density_floor(self) -> None:
        restaurants = json.loads((DATA / "restaurants.json").read_bytes())
        grid: dict[tuple[int, int], int] = {}
        for lon, lat in restaurant_coords_from_rows(restaurants):
            key = (math.floor(lon / 0.01), math.floor(lat / 0.01))
            grid[key] = grid.get(key, 0) + 1
        for i, row in enumerate(self.rows):
            gx, gy = math.floor(row[0] / 0.01), math.floor(row[1] / 0.01)
            rd = sum(grid.get((gx + dx, gy + dy), 0) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
            floor = 14.0 if rd >= 120 else 9.0 if rd >= 40 else 0.0
            if row[8] < floor and self.icon_masks[i] & 0b11:
                self.fail(f"{row[2]} importance {row[8]} < floor {floor} drawn at z12-13")

        for zoom in range(12, 20):
            bit = 1 << (zoom - 12)
            cell = 256.0
            buckets: dict[tuple[int, int], list[tuple[float, float, float, str, bool]]] = {}
            for i, row in enumerate(self.rows):
                if not self.icon_masks[i] & bit:
                    continue
                x, y = world_point(row[0], row[1], zoom)
                half = strip_half(row, zoom)
                gx, gy = math.floor(x / cell), math.floor(y / cell)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for ox, oy, oh, oname, omajor in buckets.get((gx + dx, gy + dy), ()):
                            disc = 0 if (row[6] == 3 and omajor) else SPACING[zoom]
                            need = max(disc, half + oh + 4)
                            if math.hypot(x - ox, y - oy) < need - 1e-6:
                                self.fail(f"z{zoom}: {row[2]} and {oname} are "
                                          f"{math.hypot(x - ox, y - oy):.1f}px apart < {need}")
                buckets.setdefault((gx, gy), []).append((x, y, half, row[2], row[6] == 3))

    # New in 4.3.6 (spec §2.5): no placed label rectangle overlaps another
    # drawn station's icon strip, recomputed from the payload alone.
    def test_labels_clear_neighbouring_icon_strips(self) -> None:
        measurement = self.payload["placement"]["measurement"]
        em = measurement["emCssPx"]
        halo = measurement["haloCssPx"]
        gap = measurement["labelGapCssPx"]
        for name, profile in self.payload["placement"]["profiles"].items():
            masks = profile["visibleMaskByItem"]
            widths = profile["labelWidthByItem"]
            for zoom in range(13, 20):
                bit = 1 << (zoom - 12)
                cell = 128.0
                strips: dict[tuple[int, int], list[tuple[int, tuple]]] = {}
                for i, row in enumerate(self.rows):
                    if not self.icon_masks[i] & bit:
                        continue
                    x, y = world_point(row[0], row[1], zoom)
                    hw = strip_half(row, zoom)
                    hh = SIZES[row[6] - 1] / 2
                    rect = (x - hw, y - hh, x + hw, y + hh)
                    strips.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append(
                        (i, rect))
                for i, row in enumerate(self.rows):
                    if not masks[i] & bit:
                        continue
                    x, y = world_point(row[0], row[1], zoom)
                    base = y - SIZES[row[6] - 1] / 2 - gap
                    lab = (x - widths[i] / 2 - halo, base - em - halo,
                           x + widths[i] / 2 + halo, base + halo)
                    gx, gy = math.floor(x / cell), math.floor(y / cell)
                    for dx in (-2, -1, 0, 1, 2):
                        for dy in (-1, 0, 1):
                            for j, r in strips.get((gx + dx, gy + dy), ()):
                                if j == i:
                                    continue
                                if lab[0] < r[2] and lab[2] > r[0] and lab[1] < r[3] and lab[3] > r[1]:
                                    self.fail(f"{name} z{zoom}: label {row[2]} overlaps "
                                              f"{self.rows[j][2]}'s badges")

    # Replaces the 4.3.5a ImportanceOverrideGate (zero / multiple matches):
    # the build is now a pure function of the checked-in source and the
    # published restaurants, independent of restaurant order.
    def test_payload_reproduces_from_source_and_restaurants(self) -> None:
        restaurants = json.loads((DATA / "restaurants.json").read_bytes())
        coords = restaurant_coords_from_rows(restaurants)
        raw, meta = build_station_payload(STATION_SOURCE_JSON, coords)
        self.assertEqual(raw, self.raw)
        raw_reversed, _ = build_station_payload(STATION_SOURCE_JSON, list(reversed(coords)))
        self.assertEqual(raw_reversed, self.raw)
        self.assertEqual(meta["payloadSha256"][:12], self.path.name.split(".")[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
