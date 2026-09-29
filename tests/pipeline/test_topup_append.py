"""Offline checks for Tokyo/Osaka score-floor append pagination."""

import asyncio
import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tabelog.scrape import scrape_topup as topup


TOKYO_URL = topup.TOKYO_APPEND_LIST_URL
OSAKA_URL = topup.OSAKA_APPEND_LIST_URL


def card(number, rating="3.50", region="tokyo", **changes):
    row = {
        "detail_url": f"https://tabelog.com/{region}/A1301/A130101/{number}/",
        "rating": rating,
        "genre": "allowed",
        "dinner_upper": 5000,
        "lunch_upper": None,
    }
    row.update(changes)
    return row


class AppendUrlTests(unittest.TestCase):
    def test_page_path_changes_without_changing_query(self):
        url = "https://tabelog.com/osaka/rstLst/RC/8/?Srt=D&SrtT=rt&svd=20260924&x=a%2Bb"
        self.assertEqual(
            topup.append_list_page_url(url, "osaka", 9),
            "https://tabelog.com/osaka/rstLst/RC/9/?Srt=D&SrtT=rt&svd=20260924&x=a%2Bb",
        )
        self.assertEqual(topup.append_start_page(url, "osaka", None), 8)
        self.assertEqual(topup.append_start_page(url, "osaka", 12), 12)

    def test_defaults_and_placeholder_date(self):
        day = topup.datetime.date(2026, 9, 24)
        self.assertEqual(topup.append_start_page(TOKYO_URL, "tokyo", None), 10)
        self.assertEqual(topup.append_start_page(OSAKA_URL, "osaka", None), 8)
        self.assertIn("svd=20260924", topup.resolve_list_url_date(TOKYO_URL, day))
        actual = topup.resolve_list_url_date(OSAKA_URL, day)
        expected = OSAKA_URL.replace(
            "%7B%E8%BF%90%E8%A1%8C%E6%97%A5%E6%9C%9FYYYYMMDD%7D",
            "20260924",
        )
        self.assertEqual(actual, expected)
        fixed = OSAKA_URL.replace(
            "%7B%E8%BF%90%E8%A1%8C%E6%97%A5%E6%9C%9FYYYYMMDD%7D",
            "20260701",
        )
        self.assertEqual(topup.resolve_list_url_date(fixed, day), fixed)

    def test_rejects_untrusted_or_wrong_region_url(self):
        bad = (
            "http://tabelog.com/tokyo/rstLst/RC/10/",
            "https://tabelog.com.evil.example/tokyo/rstLst/RC/10/",
            "https://evil.example/tokyo/rstLst/RC/10/",
            "https://tabelog.com/osaka/rstLst/RC/8/",
            "https://tabelog.com/tokyo/other/RC/10/",
            "https://tabelog.com/tokyo/rstLst/RC/10/#frag",
            "https://tabelog.com/tokyo/rstLst/%2e%2e/RC/10/",
            "https://tabelog.com/tokyo/rstLst/RC%2Fother/10/",
            "https://tabelog.com/tokyo/rstLst/RC/10/?SrtT=pr",
            "https://tabelog.com/tokyo/rstLst/RC/10/?SrtT=rt&Srt=A",
            "https://tabelog.com/tokyo/rstLst/RC/10/?SrtT=rt&sort_mode=2",
        )
        for url in bad:
            with self.subTest(url=url), self.assertRaises(ValueError):
                topup.append_list_page_url(url, "tokyo", 11)
        with self.assertRaises(ValueError):
            topup.append_start_page(TOKYO_URL, "tokyo", 61)


class AppendIdentityTests(unittest.TestCase):
    def test_numeric_id_survives_area_query_and_slash_changes(self):
        original = "https://tabelog.com/tokyo/A1301/A130101/00123?source=old"
        moved = "https://tabelog.com/tokyo/A1303/A130301/123/"
        self.assertEqual(topup._restaurant_id_from_url(original), "123")
        self.assertEqual(topup._restaurant_id_from_url(moved), "123")
        self.assertIsNone(topup._restaurant_id_from_url(
            "https://tabelog.com/tokyo/rstLst/RC/123/"
        ))


class AppendRatingTests(unittest.TestCase):
    def test_rating_range_precision_and_type(self):
        for value in ("1", "1.00", "3.5", "3.50", "4.99", "5", "5.00", 3.5):
            with self.subTest(valid=value):
                self.assertIsNotNone(topup._append_rating(value))
        for value in (None, True, False, "-1", "0", "0.99", "5.01",
                      "6", "100", "3.500", "NaN", "Infinity", ""):
            with self.subTest(invalid=value):
                self.assertIsNone(topup._append_rating(value))


class AppendCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_scans_entire_boundary_page_and_keeps_equal_scores(self):
        pages = {10: [
            card(3, "3.51"),
            card(2, "3.50"),
            card(2, "3.50"),
            card(4, "3.49", detail_url="https://evil.example/4/"),
            card(5, "3.49", detail_url=""),
            card(1, "3.49"),
        ]}
        calls = []

        async def fake_list(_session, region, page, url):
            calls.append((region, page, url))
            return [row.copy() for row in pages[page]], 1000

        with patch.object(topup, "scrape_list_page", fake_list):
            result = await topup.collect_append(None, "tokyo", 10, TOKYO_URL, set(),
                                                apply_legacy_gates=False)
        self.assertEqual(result.stop_reason, "score_floor")
        self.assertEqual(result.last_page, 10)
        self.assertEqual([r["rating"] for r in result.rows], ["3.51", "3.50"])
        self.assertEqual(len(calls), 1)
        self.assertIn("/RC/10/", calls[0][2])

    async def test_dedupes_existing_and_new_area_aliases_by_numeric_id(self):
        old_url = "https://tabelog.com/tokyo/A1301/A130101/100?source=old"
        seen = {old_url}
        moved = "https://tabelog.com/tokyo/A1303/A130301/100/"
        first = "https://tabelog.com/tokyo/A1301/A130101/101?source=list"
        alias = "https://tabelog.com/tokyo/A1303/A130301/101/"
        rows = [
            card(100, "3.51", detail_url=moved),
            card(101, "3.50", detail_url=first),
            card(101, "3.50", detail_url=alias),
            card(102, "3.49"),
        ]

        async def fake_list(_session, _region, _page, _url):
            return [row.copy() for row in rows], None

        with patch.object(topup, "scrape_list_page", fake_list):
            result = await topup.collect_append(None, "tokyo", 10, TOKYO_URL, seen,
                                                apply_legacy_gates=False)
        self.assertEqual(result.stop_reason, "score_floor")
        self.assertEqual([r["detail_url"] for r in result.rows],
                         ["https://tabelog.com/tokyo/A1301/A130101/101/"])
        self.assertIn(old_url, seen)
        self.assertIn(result.rows[0]["detail_url"], seen)

    async def test_invalid_url_below_floor_does_not_stop(self):
        pages = {
            10: [card(1, "3.50"),
                 card(2, "3.49", detail_url="https://evil.example/2/")],
            11: [card(3, "3.49")],
        }

        async def fake_list(_session, _region, page, _url):
            return [row.copy() for row in pages[page]], None

        with patch.object(topup, "scrape_list_page", fake_list):
            result = await topup.collect_append(None, "tokyo", 10, TOKYO_URL, set(),
                                                apply_legacy_gates=False)
        self.assertEqual(result.stop_reason, "score_floor")
        self.assertEqual(result.last_page, 11)
        self.assertEqual(len(result.rows), 1)

    async def test_out_of_order_page_is_incomplete(self):
        pages = {10: [card(1, "3.49"), card(2, "3.70")]}

        async def fake_list(_session, _region, page, _url):
            return [row.copy() for row in pages[page]], None

        with patch.object(topup, "scrape_list_page", fake_list):
            with self.assertRaisesRegex(topup.AppendCollectionError,
                                        "out of descending order") as caught:
                await topup.collect_append(None, "tokyo", 10, TOKYO_URL, set(),
                                           apply_legacy_gates=False)
        self.assertEqual(caught.exception.rows, [])

    async def test_out_of_order_across_pages_is_incomplete(self):
        pages = {
            10: [card(1, "3.50")],
            11: [card(2, "3.51")],
        }

        async def fake_list(_session, _region, page, _url):
            return [row.copy() for row in pages[page]], None

        with patch.object(topup, "scrape_list_page", fake_list):
            with self.assertRaisesRegex(topup.AppendCollectionError,
                                        "out of descending order") as caught:
                await topup.collect_append(None, "tokyo", 10, TOKYO_URL, set(),
                                           apply_legacy_gates=False)
        self.assertEqual(caught.exception.page, 11)
        self.assertEqual(len(caught.exception.rows), 1)

    async def test_page_without_valid_score_and_url_is_incomplete(self):
        async def fake_list(_session, _region, _page, _url):
            return [card(1, None), card(2, "-1"),
                    card(3, "3.49", detail_url="https://evil.example/3/")], None

        with patch.object(topup, "scrape_list_page", fake_list):
            with self.assertRaisesRegex(topup.AppendCollectionError,
                                        "no cards with valid"):
                await topup.collect_append(None, "tokyo", 10, TOKYO_URL, set(),
                                           apply_legacy_gates=False)

    async def test_unknown_rating_is_not_a_floor(self):
        pages = {
            10: [card(1, None), card(2, "3.50")],
            11: [card(3, "3.49")],
        }

        async def fake_list(_session, _region, page, _url):
            return [row.copy() for row in pages[page]], None

        with patch.object(topup, "scrape_list_page", fake_list):
            result = await topup.collect_append(None, "tokyo", 10, TOKYO_URL, set(),
                                                apply_legacy_gates=False)
        self.assertEqual(result.stop_reason, "score_floor")
        self.assertEqual(result.last_page, 11)
        self.assertEqual(result.unknown_ratings, 1)
        self.assertEqual([r["detail_url"] for r in result.rows],
                         [card(2)["detail_url"]])

    async def test_page_60_is_a_distinct_stop_reason(self):
        async def fake_list(_session, _region, page, _url):
            self.assertEqual(page, 60)
            return [card(1, "3.50")], None

        with patch.object(topup, "scrape_list_page", fake_list):
            result = await topup.collect_append(None, "tokyo", 60, TOKYO_URL, set(),
                                                apply_legacy_gates=False)
        self.assertEqual(result.stop_reason, "page_limit")
        self.assertEqual(result.last_page, 60)
        self.assertEqual(len(result.rows), 1)

    async def test_fetch_failure_keeps_partial_rows_and_raises(self):
        async def fake_list(_session, _region, page, _url):
            if page == 10:
                return [card(1, "3.50")], None
            raise RuntimeError("offline simulated failure")

        with patch.object(topup, "scrape_list_page", fake_list):
            with self.assertRaises(topup.AppendCollectionError) as caught:
                await topup.collect_append(None, "tokyo", 10, TOKYO_URL, set(),
                                           apply_legacy_gates=False)
        self.assertEqual(caught.exception.page, 11)
        self.assertEqual(len(caught.exception.rows), 1)
        self.assertIn("list request failed", str(caught.exception))

    async def test_repeated_or_empty_page_raises(self):
        async def repeated(_session, _region, _page, _url):
            return [card(1, "3.50")], None

        with patch.object(topup, "scrape_list_page", repeated):
            with self.assertRaisesRegex(topup.AppendCollectionError, "repeated"):
                await topup.collect_append(None, "tokyo", 10, TOKYO_URL, set(),
                                           apply_legacy_gates=False)

        async def empty(_session, _region, _page, _url):
            return [], None

        with patch.object(topup, "scrape_list_page", empty):
            with self.assertRaisesRegex(topup.AppendCollectionError, "no cards"):
                await topup.collect_append(None, "tokyo", 10, TOKYO_URL, set(),
                                           apply_legacy_gates=False)

    async def test_existing_price_and_genre_gates_remain(self):
        rows = [
            card(1, "3.50", dinner_upper=20000),
            card(2, "3.50", dinner_upper=3000),
            card(3, "3.50", genre="other"),
            card(4, "3.50"),
            card(5, "3.49"),
        ]

        async def fake_list(_session, _region, _page, _url):
            return [row.copy() for row in rows], None

        with patch.object(topup, "scrape_list_page", fake_list), \
             patch.object(topup, "is_main_meal", lambda genre: genre == "allowed"):
            result = await topup.collect_append(None, "tokyo", 10, TOKYO_URL, set())
        self.assertEqual([r["detail_url"] for r in result.rows],
                         [card(4)["detail_url"]])

    async def test_append_detail_exclusions_before_master_write(self):
        class OfflineBrowser:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        class OfflineSession:
            def __init__(self, _playwright):
                pass

            async def connect(self):
                pass

        rows = [card(number, "3.50", name=f"restaurant {number}")
                for number in range(1, 6)]
        details = {
            1: {"operating_status": "closed", "rating": 4.0},
            2: {"operating_status": "temporarily_closed", "rating": 4.0},
            3: {"operating_status": "unknown", "rating": 3.49},
            4: {"operating_status": "unknown", "rating": None},
            5: {"operating_status": "unknown", "rating": 3.5},
        }
        captured = []

        other_region_url = "https://tabelog.com/nagano/A2001/A200101/999/"
        async def fake_collect(_session, region, _start_page, _list_url, seen_urls, **_kwargs):
            self.assertIn(other_region_url, seen_urls)
            return topup.AppendResult([row.copy() for row in rows] if region == "tokyo" else [],
                                      "score_floor", 10)

        async def fake_detail(_session, url):
            number = int(url.rstrip("/").split("/")[-1])
            return {"address": "offline address", "photos": [], **details[number]}

        def fake_append(new_rows, _path):
            captured.extend(row.copy() for row in new_rows)

        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / "tabelog.csv"
            with master.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["region", "detail_url", "source_page"])
                writer.writeheader()
                writer.writerow({"region": "nagano", "detail_url": other_region_url,
                                 "source_page": 1})
            with patch.object(topup, "TABELOG_CSV", master), \
                 patch.object(topup, "INTERMEDIATE_DIR", Path(directory)), \
                 patch.object(topup, "async_playwright", OfflineBrowser), \
                 patch.object(topup, "Session", OfflineSession), \
                 patch.object(topup, "collect_append", fake_collect), \
                 patch.object(topup, "fetch_detail", fake_detail), \
                 patch.object(topup, "write_intermediate"), \
                 patch.object(topup, "append_and_dedupe", fake_append):
                await topup.amain(["--tokyo-osaka-append", "--no-translate"])
        self.assertEqual([r["detail_url"] for r in captured],
                         [card(4)["detail_url"], card(5)["detail_url"]])

    async def test_dry_run_is_local_and_uses_both_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / "tabelog.csv"
            with master.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["region", "detail_url", "source_page"])
                writer.writeheader()
                writer.writerow({"region": "tokyo", "detail_url": card(1)["detail_url"],
                                 "source_page": 60})
            with patch.object(topup, "TABELOG_CSV", master), \
                 patch.object(topup, "load_totals_cache",
                              side_effect=AssertionError("totals must not be read")), \
                 patch.object(topup, "async_playwright",
                              side_effect=AssertionError("browser must not start")):
                await topup.amain(["--tokyo-osaka-append", "--dry-run"])


if __name__ == "__main__":
    unittest.main()
