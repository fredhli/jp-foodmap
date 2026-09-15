#!/usr/bin/env python3
"""Contract checks for the generated 4.3.1a station payload.

Run after the normal build::

    .venv-wsl/bin/python tests/transit/station_payload_test.py
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "data"
EXPECTED_FIELDS = ["lon", "lat", "name", "name_en", "railway", "line_count"]
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

    def test_v2_shape_and_real_corpus(self) -> None:
        self.assertEqual(self.payload.get("v"), 2)
        self.assertEqual(self.payload.get("fields"), EXPECTED_FIELDS)
        self.assertIsInstance(self.rows, list)
        self.assertEqual(len(self.rows), 8954)

        for i, row in enumerate(self.rows):
            with self.subTest(row=i):
                self.assertEqual(len(row), len(EXPECTED_FIELDS))
                lon, lat, name, name_en, railway, line_count = row
                self.assertIsInstance(lon, (int, float))
                self.assertIsInstance(lat, (int, float))
                self.assertTrue(122 <= lon <= 154)
                self.assertTrue(20 <= lat <= 46)
                self.assertIsInstance(name, str)
                self.assertIsInstance(name_en, str)
                self.assertIsInstance(railway, str)
                self.assertGreaterEqual(line_count, 0)
    def test_rows_retain_source_geographic_order(self) -> None:
        source = json.loads(
            (ROOT / "src/tabelog/scrape/station_source_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(self.rows, source["stations"])

    def test_precomputed_label_profiles_match_every_station(self) -> None:
        placement = self.payload.get("placement")
        self.assertIsInstance(placement, dict)
        self.assertEqual(placement.get("version"), 1)
        self.assertEqual(placement.get("zoomMin"), 14)
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
                self.assertTrue(all(isinstance(mask, int) and 0 <= mask <= 63 for mask in masks))
                self.assertTrue(all(
                    isinstance(width, (int, float)) and width >= 0
                    for width in widths
                ))

    def test_z14_label_candidates_are_hubs_only(self) -> None:
        for name, profile in self.payload["placement"]["profiles"].items():
            for i, (row, mask) in enumerate(zip(self.rows, profile["visibleMaskByItem"])):
                if row[5] < 6:
                    with self.subTest(profile=name, row=i, station=row[2]):
                        self.assertEqual(mask & 1, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
