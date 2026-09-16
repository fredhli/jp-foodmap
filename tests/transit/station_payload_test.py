#!/usr/bin/env python3
"""Contract checks for the generated station payload.

Run after the normal build::

    .venv-wsl/bin/python tests/transit/station_payload_test.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "data"
sys.path.insert(0, str(ROOT / "src"))
from tabelog.scrape.station_payload import apply_importance_overrides  # noqa: E402
EXPECTED_FIELDS = [
    "lon", "lat", "name", "name_en", "railway", "line_count", "display_tier"
]
EXPECTED_PROFILES = {"local-max130", "en-max130"}


def built_payload() -> tuple[Path, dict]:
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
    return path, json.loads(raw)


class StationPayloadContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path, cls.payload = built_payload()
        cls.rows = cls.payload.get("stations")

    def test_v3_shape_and_real_corpus(self) -> None:
        self.assertEqual(self.payload.get("v"), 3)
        self.assertEqual(self.payload.get("fields"), EXPECTED_FIELDS)
        self.assertIsInstance(self.rows, list)
        self.assertEqual(len(self.rows), 8954)

        for i, row in enumerate(self.rows):
            with self.subTest(row=i):
                self.assertEqual(len(row), len(EXPECTED_FIELDS))
                lon, lat, name, name_en, railway, line_count, display_tier = row
                self.assertIsInstance(lon, (int, float))
                self.assertIsInstance(lat, (int, float))
                self.assertTrue(122 <= lon <= 154)
                self.assertTrue(20 <= lat <= 46)
                self.assertIsInstance(name, str)
                self.assertIsInstance(name_en, str)
                self.assertIsInstance(railway, str)
                self.assertGreaterEqual(line_count, 0)
                self.assertIsInstance(display_tier, int)
                self.assertIn(display_tier, {0, 1, 2, 3})
    def test_rows_retain_source_geographic_order(self) -> None:
        source = json.loads(
            (ROOT / "src/tabelog/scrape/station_source_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual([row[:6] for row in self.rows], source["stations"])

    def test_reviewed_importance_overrides_are_sparse_and_exact(self) -> None:
        importance = self.payload.get("importance")
        self.assertEqual(importance.get("version"), 1)
        self.assertEqual(importance.get("reviewed"), "2026-09-16")
        self.assertEqual(importance.get("overrideCount"), 18)
        overridden = [row for row in self.rows if row[6]]
        self.assertEqual(len(overridden), 18)
        self.assertTrue(all(row[6] == 3 for row in overridden))
        names = {row[2] for row in overridden}
        self.assertTrue({"大阪", "梅田", "難波", "なんば", "札幌", "博多"} <= names)
        self.assertFalse(any(row[2] in {"JR難波", "東梅田", "西梅田", "さっぽろ"} for row in overridden))

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

    def test_distant_label_candidates_follow_station_tiers(self) -> None:
        for name, profile in self.payload["placement"]["profiles"].items():
            for i, (row, mask) in enumerate(zip(self.rows, profile["visibleMaskByItem"])):
                with self.subTest(profile=name, row=i, station=row[2], zoom=12):
                    self.assertEqual(mask & 1, 0)
                effective_tier = row[6] or (3 if row[5] >= 6 else 2 if row[5] >= 3 else 1)
                if effective_tier < 3:
                    with self.subTest(profile=name, row=i, station=row[2]):
                        self.assertEqual(mask & 2, 0)


class ImportanceOverrideGate(unittest.TestCase):
    def _config_path(self, rule: dict) -> tuple[tempfile.TemporaryDirectory, Path]:
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "overrides.json"
        path.write_text(json.dumps({
            "version": 1,
            "reviewed": "2026-09-16",
            "rules": [rule],
        }), encoding="utf-8")
        return temp, path

    def test_zero_match_fails(self) -> None:
        rule = {
            "name": "missing", "near": [135.0, 35.0], "radius_m": 100,
            "display_tier": 3, "reason": "test", "source": ["https://example.com"],
        }
        temp, path = self._config_path(rule)
        self.addCleanup(temp.cleanup)
        with self.assertRaisesRegex(ValueError, "matched 0 stations"):
            apply_importance_overrides([[135.0, 35.0, "present", "", "station", 1]], path)

    def test_multiple_matches_fail(self) -> None:
        rule = {
            "name": "same", "near": [135.0, 35.0], "radius_m": 100,
            "display_tier": 3, "reason": "test", "source": ["https://example.com"],
        }
        temp, path = self._config_path(rule)
        self.addCleanup(temp.cleanup)
        rows = [
            [135.0, 35.0, "same", "", "station", 1],
            [135.0001, 35.0001, "same", "", "station", 1],
        ]
        with self.assertRaisesRegex(ValueError, "matched 2 stations"):
            apply_importance_overrides(rows, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
