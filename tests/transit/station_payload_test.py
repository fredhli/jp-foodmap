import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from tabelog.scrape.transit_postprocess import _station_record, _write_stations


def station(lon, lat, name, line_count, **extra):
    props = {"kind": "station", "railway": "station", "name": name,
             "line_count": line_count, **extra}
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


class StationPayloadTest(unittest.TestCase):
    def test_v1_record_keeps_three_size_inputs(self):
        rows = [
            _station_record(station(139.1, 35.1, "小駅", 2)),
            _station_record(station(139.2, 35.2, "乗換", 3, name_en="Transfer")),
            _station_record(station(139.3, 35.3, "大駅", 6)),
        ]
        self.assertEqual([r[5] for r in rows], [2, 3, 6])
        self.assertEqual(rows[1][3], "Transfer")

    def test_writer_is_compact_deterministic_and_publishable(self):
        stations = [
            station(139.123456, 35.654321, "甲", 1),
            station(140.0, 36.0, "乙", 8, railway="tram_stop"),
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "japan-stations.json"
            first = _write_stations(stations, path)
            encoded = path.read_bytes()
            payload = json.loads(encoded)
            second = _write_stations(stations, path)

        self.assertEqual(payload["v"], 1)
        self.assertEqual(
            payload["fields"],
            ["lon", "lat", "name", "name_en", "railway", "line_count"],
        )
        self.assertEqual(payload["stations"][0][:2], [139.12346, 35.65432])
        self.assertEqual(first, second)
        self.assertEqual(first["bytes"], len(encoded))
        self.assertEqual(first["gzip_bytes"], len(gzip.compress(encoded, 9, mtime=0)))
        self.assertRegex(first["object_name"], r"^japan-stations\.[0-9a-f]{12}\.json$")


if __name__ == "__main__":
    unittest.main()
