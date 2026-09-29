"""Offline fixtures for the bimonthly refresh. No browser or network."""

import asyncio
import csv
import contextlib
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import main as entry
from tabelog.scrape import bimonth_update as shim, region_selection as updater, region_update, scrape_all


def row(name, number, rating="3.60", *, region="tokyo", url=None):
    return {
        "region": region, "rank": "12", "name": name, "rating": rating,
        "review_count": "10", "save_count": "20", "awards": "old-award",
        "genre": "寿司", "station": "東京駅", "station_distance_m": "100",
        "dinner_upper": "9999", "lunch_upper": "4999", "holiday": "月曜日",
        "seat_count": "12", "address": "東京都千代田区1", "reservation_policy": "予約可",
        "reservation_policy_chinese": "可预约", "tabelog_bookable": "True",
        "detail_url": url or f"https://tabelog.com/tokyo/A1301/A130101/{number}/",
        "source_page": "1", "source_query": "rating", "lat": "35.1", "lon": "139.1",
        "photo1_url": "https://example.test/old.jpg", "photo2_url": "", "photo3_url": "",
        "scraped_at": "2026-01-01T00:00:00Z", "operating_status": "unknown",
    }


def list_row(base, **changes):
    data = dict(base)
    data.update({"rank": "2", "rating": "4.00", "review_count": "99",
                 "awards": "", "seat_count": "", "address": "",
                 "reservation_policy": "", "reservation_policy_chinese": "",
                 "lat": "", "lon": "", "photo1_url": "", "source_page": 5,
                 "source_query": "rating", "dinner_upper": 9999,
                 "lunch_upper": 4999})
    data.update(changes)
    return data


def detail(score=3.6, status="unknown", address="東京都港区2"):
    return {"rating": score, "operating_status": status, "address": address,
            "seat_count": "40", "reservation_policy": "電話予約可",
            "tabelog_bookable": True, "photos": ["https://example.test/new.jpg"]}


class BimonthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory(prefix="tabelog-bimonth-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.csv = self.base / "tabelog.csv"
        self.archive = self.base / "departures.jsonl"
        self.report = self.base / "run.json"

    def save(self, rows):
        fields = list(dict.fromkeys(scrape_all.FIELDS + ["rating_check", "custom_legacy"]))
        with self.csv.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows([{key: value.get(key, "") for key in fields} for value in rows])

    def load(self):
        with self.csv.open(encoding="utf-8-sig", newline="") as file:
            return list(csv.DictReader(file))

    async def test_patch_verify_depart_and_add(self):
        selected_old = row("selected", "13000001", url="http://tabelog.com/tokyo/A1301/A130101/13000001/?tracking=1")
        selected_old["custom_legacy"] = "keep me"
        low = row("low", "13000002")
        boundary = row("boundary", "13000003")
        closed = row("closed", "13000004")
        failed = row("failed", "13000005")
        other = row("other region", "27000001", region="osaka")
        self.save([selected_old, low, boundary, closed, failed, other])
        selected = [list_row(row("selected", "13000001")),
                    list_row(row("new", "13000006", rating="3.60"), rating="3.60")]
        selection = {"rows": selected, "total": 1000, "target": 2,
                     "pages": 60, "status": "truncated", "issue": "", "fine_dine_count": 0}
        answers = {
            low["detail_url"]: detail(3.39),
            boundary["detail_url"]: detail(3.40),
            closed["detail_url"]: detail(None, "temporarily_closed"),
            selected[1]["detail_url"]: detail(None),
        }

        async def fetch(_session, url):
            if url == failed["detail_url"]:
                raise RuntimeError("HTTP 404")
            return answers[url]

        with patch.object(scrape_all, "fetch_detail", new=fetch):
            report = await region_update.apply_region(object(), "tokyo", self.csv, self.archive,
                                                   self.report, "offline-run", selection=selection,
                                                   translate=False)
        rows = {r["name"]: r for r in self.load()}
        self.assertEqual(set(rows), {"selected", "boundary", "failed", "other region", "new"})
        self.assertEqual(rows["selected"]["detail_url"], selected_old["detail_url"])
        self.assertEqual(rows["selected"]["custom_legacy"], "keep me")
        self.assertEqual(rows["selected"]["address"], selected_old["address"])
        self.assertEqual(rows["selected"]["lat"], selected_old["lat"])
        self.assertEqual(rows["selected"]["photo1_url"], selected_old["photo1_url"])
        self.assertEqual(rows["selected"]["reservation_policy_chinese"], "可预约")
        self.assertEqual(rows["selected"]["rating"], "4.00")
        self.assertEqual(rows["selected"]["awards"], "")
        self.assertEqual(rows["boundary"]["rating"], "3.40")
        self.assertEqual(rows["failed"]["rating"], "3.60")
        self.assertEqual(rows["new"]["address"], "東京都港区2")
        self.assertEqual(rows["new"]["seat_count"], "40")
        self.assertEqual(rows["new"]["photo1_url"], "https://example.test/new.jpg")
        self.assertEqual(rows["new"]["rating_check"], "list")
        self.assertEqual(rows["selected"]["scraped_at"], selected_old["scraped_at"])
        self.assertTrue(rows["selected"]["score_checked_at"])
        self.assertEqual(rows["boundary"]["scraped_at"], boundary["scraped_at"])
        self.assertTrue(rows["boundary"]["details_checked_at"])
        archived = [json.loads(line) for line in self.archive.read_text().splitlines()]
        self.assertEqual({entry["row"]["name"] for entry in archived}, {"low", "closed"})
        self.assertEqual({entry["reason"] for entry in archived}, {"rating_below_3.40", "temporarily_closed"})
        self.assertEqual(report["selection"]["status"], "truncated")
        self.assertEqual(report["commit"], "complete")
        self.assertEqual(len(report["pending"]), 1)
        # Stopping at page 60 is reported through the status, not as a failure.
        self.assertFalse(report["incomplete"])

    async def test_partial_list_still_checks_every_missing_old_row(self):
        old = [row(f"old{i}", str(13000010 + i)) for i in range(3)]
        self.save(old)
        selected = [list_row(old[0])]
        selection = {"rows": selected, "total": 1000, "target": 10,
                     "pages": 1, "status": "partial", "issue": "page 2: verification",
                     "fine_dine_count": 0}
        fetch = AsyncMock(side_effect=[detail(3.4), RuntimeError("verification page")])
        with patch.object(scrape_all, "fetch_detail", new=fetch):
            report = await region_update.apply_region(object(), "tokyo", self.csv, self.archive,
                                                   self.report, "offline-run", selection=selection,
                                                   translate=False)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(len(self.load()), 3)
        self.assertFalse(self.archive.exists())
        self.assertEqual(report["selection"]["status"], "partial")

    async def test_dry_run_is_read_only_and_does_not_start_browser(self):
        self.save([row("one", "13000001"), row("two", "27000001", region="osaka")])
        project = shim.project_main()
        with patch.object(project, "async_playwright", side_effect=AssertionError("browser started")):
            result = await shim.run_bimonth_update(csv_path=self.csv, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["existing"], {"osaka": 1, "tokyo": 1})
        self.assertFalse(self.archive.exists())
        self.assertFalse((self.base / "tabelog.csv.bimonth.lock").exists())

    async def test_pagination_failure_and_sixty_page_limit(self):
        sample = list_row(row("one", "13000001"))

        async def page(_session, _region, number):
            if number == 2:
                raise RuntimeError("HTTP 503")
            return [sample], 1000

        with patch.object(scrape_all, "scrape_list_page", new=page):
            result = await entry.collect_region(object(), "tokyo", 1.0, 500, 0.1, special_lists=False)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["rows"]), 1)
        self.assertIn("HTTP 503", result["issue"])

        async def sixty(_session, _region, number):
            fresh = dict(sample, detail_url=f"https://tabelog.com/tokyo/A1301/A130101/{13000000 + number}/")
            return [fresh], 10000

        with patch.object(scrape_all, "scrape_list_page", new=sixty):
            result = await entry.collect_region(object(), "tokyo", 1.0, 500, 0.1, special_lists=False)
        self.assertEqual(result["status"], "truncated")
        self.assertEqual(result["pages"], 60)
        self.assertEqual(len(result["rows"]), 60)

    async def test_list_requires_total_unique_pages_order_and_valid_urls(self):
        first = list_row(row("first", "13000001"), rating="4.00")
        higher = list_row(row("higher", "13000002"), rating="4.10")

        async def no_total(_session, _region, _page):
            return [first], None
        with patch.object(scrape_all, "scrape_list_page", new=no_total):
            result = await entry.collect_region(object(), "tokyo", 1.0, 500, 0.1, special_lists=False)
        self.assertEqual(result["status"], "partial")
        self.assertIn("region total", result["issue"])
        self.assertEqual(result["pages"], 1)

        async def repeated(_session, _region, _page):
            return [first], 1000
        with patch.object(scrape_all, "scrape_list_page", new=repeated):
            result = await entry.collect_region(object(), "tokyo", 1.0, 500, 0.1, special_lists=False)
        self.assertEqual(result["status"], "partial")
        self.assertIn("repeated", result["issue"])

        async def unordered(_session, _region, page):
            return [first if page == 1 else higher], 1000
        with patch.object(scrape_all, "scrape_list_page", new=unordered):
            result = await entry.collect_region(object(), "tokyo", 1.0, 500, 0.1, special_lists=False)
        self.assertEqual(result["status"], "partial")
        self.assertIn("descending", result["issue"])

        async def external(_session, _region, _page):
            return [dict(first, detail_url="https://example.com/tokyo/A1301/A130101/13000001/")], 1000
        with patch.object(scrape_all, "scrape_list_page", new=external):
            result = await entry.collect_region(object(), "tokyo", 1.0, 500, 0.1, special_lists=False)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["rows"], [])

    async def test_selected_missing_score_is_verified_and_new_unscored_stays_pending(self):
        selected_old = row("selected", "13000001")
        other_region = row("other", "13000007", region="osaka",
                           url="https://tabelog.com/osaka/A2701/A270101/13000007/")
        self.save([selected_old, other_region])
        selected = [list_row(selected_old, rating=""),
                    list_row(row("duplicate", "13000007")),
                    list_row(row("unscored", "13000008"), rating="")]
        selection = {"rows": selected, "total": 300, "target": 3,
                     "pages": 1, "status": "quota_reached", "issue": "", "fine_dine_count": 0}
        urls = {selected_old["detail_url"]: detail(3.41),
                selected[2]["detail_url"]: detail(None)}
        fetch = AsyncMock(side_effect=lambda _session, url: urls[url])
        with patch.object(scrape_all, "fetch_detail", new=fetch):
            report = await region_update.apply_region(object(), "tokyo", self.csv, self.archive,
                                                   self.report, "offline-run", selection=selection,
                                                   translate=False)
        rows = {r["name"]: r for r in self.load()}
        self.assertEqual(set(rows), {"selected", "other"})
        self.assertEqual(rows["selected"]["rating"], "3.41")
        self.assertEqual(rows["selected"]["scraped_at"], selected_old["scraped_at"])
        self.assertTrue(rows["selected"]["score_checked_at"])
        self.assertTrue(rows["selected"]["details_checked_at"])
        self.assertEqual(report["counts"]["skipped_duplicate"], 1)
        self.assertEqual(report["counts"]["new_added"], 0)
        self.assertEqual(fetch.await_count, 2)
        self.assertIn("score_missing", {item["reason"] for item in report["pending"]})

    async def run_with_report(self, status):
        self.save([row("one", "13000001")])
        class Playwright:
            async def __aenter__(self):
                return object()
            async def __aexit__(self, *_):
                return False
        class Session:
            def __init__(self, _playwright):
                pass
            async def connect(self):
                pass
        report = {"region": "tokyo", "incomplete": status == "partial", "departures": [],
                  "selection": {"status": status}, "counts": {}, "pending": [],
                  "commit": "complete"}
        project = shim.project_main()
        with patch.object(project, "async_playwright", return_value=Playwright()), \
             patch.object(project, "collect_region", new=AsyncMock(return_value={"rows": [], "status": status})), \
             patch.object(scrape_all, "Session", Session), \
             patch.object(region_update, "apply_region", new=AsyncMock(return_value=report)):
            return await shim.run_bimonth_update(["tokyo"], csv_path=self.csv,
                                                 archive_path=self.archive,
                                                 run_dir=self.base / "runs",
                                                 translate=False)

    async def test_incomplete_region_propagates_to_run_result(self):
        result = await self.run_with_report("partial")
        self.assertFalse(result["ok"])
        self.assertEqual(result["incomplete_regions"], ["tokyo"])
        self.assertEqual(result["page_limited_regions"], [])

    async def test_page_limited_region_completes_the_run(self):
        result = await self.run_with_report("truncated")
        self.assertTrue(result["ok"])
        self.assertEqual(result["incomplete_regions"], [])
        self.assertEqual(result["page_limited_regions"], ["tokyo"])
        manifest = json.loads((Path(result["run_dir"]) / "manifest.json").read_text())
        self.assertTrue(manifest["finished"])
        self.assertEqual(manifest["completed"], ["tokyo"])

    async def test_full_and_bimonth_share_selection_but_skip_different_old_details(self):
        old = [row("old one", "13000001"), row("old two", "13000002")]
        self.save(old)
        full_csv = self.base / "full.csv"
        shutil.copyfile(self.csv, full_csv)
        fresh_old = [list_row(value, rating="4.00") for value in old]
        new = list_row(row("new", "13000003"), rating="3.80")
        selection = {"rows": fresh_old + [new], "observed_rows": fresh_old,
                     "selected_origins": {}, "status": "quota_reached", "issue": "",
                     "pages": 1, "main_meal_target": 1}
        fetched_urls = []
        async def fetch(_session, url):
            fetched_urls.append(url)
            return detail(3.80)
        with patch.object(scrape_all, "fetch_detail", new=fetch):
            await region_update.apply_region(None, "tokyo", self.csv,
                                             self.base / "bimonth-archive.jsonl",
                                             self.base / "bimonth-report.json", "bimonth-fixture",
                                             selection=selection, translate=False, full_details=False)
            self.assertEqual(fetched_urls, [new["detail_url"]])
            fetched_urls.clear()
            await region_update.apply_region(None, "tokyo", full_csv,
                                             self.base / "full-archive.jsonl",
                                             self.base / "full-report.json", "full-fixture",
                                             selection=selection, translate=False, full_details=True)
        self.assertEqual(fetched_urls, [old[0]["detail_url"], old[1]["detail_url"], new["detail_url"]])
        bimonth_rows = {value["name"]: value for value in region_update.read_corpus(self.csv)[1]}
        full_rows = {value["name"]: value for value in region_update.read_corpus(full_csv)[1]}
        self.assertEqual(bimonth_rows["old one"]["seat_count"], "12")
        self.assertEqual(full_rows["old one"]["seat_count"], "40")
        self.assertEqual(bimonth_rows["old one"]["rating"], "4.00")

    async def test_full_detail_failure_keeps_list_score_and_old_detail(self):
        old = row("old", "13000001")
        self.save([old])
        fresh = list_row(old, rating="4.00")
        selection = {"rows": [fresh], "observed_rows": [fresh],
                     "status": "quota_reached", "issue": "", "pages": 1,
                     "main_meal_target": 1}
        with patch.object(scrape_all, "fetch_detail", new=AsyncMock(side_effect=RuntimeError("HTTP 503"))) as fetch:
            report = await region_update.apply_region(None, "tokyo", self.csv, self.archive,
                                                      self.report, "full-fixture", selection=selection,
                                                      translate=False, full_details=True)
        fetch.assert_awaited_once_with(None, old["detail_url"])
        saved = self.load()[0]
        self.assertEqual(saved["rating"], "4.00")
        self.assertEqual(saved["rating_check"], "list")
        self.assertEqual(saved["address"], old["address"])
        self.assertEqual(saved["photo1_url"], old["photo1_url"])
        self.assertEqual(report["counts"]["pending"], 1)

    async def test_append_only_preserves_old_row_even_if_list_score_is_low(self):
        old = row("old", "13000001")
        self.save([old])
        new = list_row(row("new", "13000002"), rating="3.50")
        observed_old = list_row(old, rating="3.39")
        selection = {"rows": [new], "observed_rows": [observed_old],
                     "selected_origins": {updater.identity(new["detail_url"]): ["special"]},
                     "status": "score_floor", "issue": "", "pages": 1,
                     "main_meal_target": None}
        with patch.object(scrape_all, "fetch_detail", new=AsyncMock(return_value=detail(3.50))) as fetch:
            report = await region_update.apply_region(None, "tokyo", self.csv, self.archive,
                                                      self.report, "append-fixture", selection=selection,
                                                      translate=False, append_only=True)
        fetch.assert_awaited_once_with(None, new["detail_url"])
        saved = {value["name"]: value for value in self.load()}
        for key in ("rating", "address", "seat_count", "rank", "source_page", "scraped_at", "photo1_url"):
            self.assertEqual(saved["old"][key], old[key], key)
        self.assertEqual(saved["new"]["rating"], "3.50")
        self.assertEqual(report["counts"]["removed_low_rating"], 0)
        self.assertFalse(self.archive.exists())

    async def test_new_corpus_can_initialize_from_a_selected_restaurant(self):
        new = list_row(row("new", "13000001"), rating="3.80")
        selection = {"rows": [new], "observed_rows": [new],
                     "status": "quota_reached", "issue": "", "pages": 1,
                     "main_meal_target": 1}
        with patch.object(scrape_all, "fetch_detail", new=AsyncMock(return_value=detail(3.80))):
            report = await region_update.apply_region(None, "tokyo", self.csv, self.archive,
                                                      self.report, "new-fixture", selection=selection,
                                                      translate=False)
        self.assertEqual(report["counts"]["new_added"], 1)
        self.assertTrue(self.csv.exists())
        self.assertEqual(self.load()[0]["detail_url"], new["detail_url"])

    def test_windows_lock_branch(self):
        calls = []
        fake = types.ModuleType("msvcrt")
        fake.LK_NBLCK = 1
        fake.LK_UNLCK = 2
        fake.locking = lambda _fd, mode, length: calls.append((mode, length))
        with patch.object(region_update, "os", types.SimpleNamespace(name="nt")), \
             patch.dict(sys.modules, {"msvcrt": fake}):
            with region_update.corpus_lock(self.csv):
                pass
        self.assertEqual(calls, [(1, 1), (2, 1)])

    async def test_fetch_detail_404_and_missing_score_are_unknown(self):
        class Page:
            def __init__(self, status):
                self.status = status

            async def goto(self, _url, **_kwargs):
                return type("Response", (), {"status": self.status})()

            async def evaluate(self, _script):
                return {"address": "東京都中央区3", "rating": None,
                        "status_text": None, "seat_count": "8",
                        "reservation_policy": "予約可", "tabelog_bookable": False}

            async def content(self):
                return "<html></html>"

        with patch.object(scrape_all, "MAX_RETRIES", 1), \
             patch.object(scrape_all, "DETAIL_PAGE_DELAY_S", 0):
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                await scrape_all.fetch_detail(type("Session", (), {"page": Page(404)})(),
                                              "https://tabelog.com/tokyo/A1301/A130101/13000001/")
            redirected = Page(200)
            redirected.url = "https://tabelog.com/verify"
            with self.assertRaisesRegex(RuntimeError, "redirected away"):
                await scrape_all.fetch_detail(type("Session", (), {"page": redirected})(),
                                              "https://tabelog.com/tokyo/A1301/A130101/13000001/")
            got = await scrape_all.fetch_detail(type("Session", (), {"page": Page(200)})(),
                                                "https://tabelog.com/tokyo/A1301/A130101/13000001/")
            closed = Page(200)
            closed.evaluate = AsyncMock(return_value={
                "address": "大阪市北区1", "rating": None, "status_text": None,
                "name_status_text": "閉店 KOZONO このお店は現在閉店しております。店舗の掲載情報に関して",
            })
            closed_result = await scrape_all.fetch_detail(
                type("Session", (), {"page": closed})(),
                "https://tabelog.com/osaka/A2701/A270103/27136105/",
            )
            generic = Page(200)
            generic.evaluate = AsyncMock(return_value={
                "address": "大阪市北区1", "rating": None,
                "status_text": "閉店・休業・移転の報告",
                "name_status_text": "閉店 KOZONO このお店は現在閉店しております。店舗の掲載情報に関して",
            })
            generic_result = await scrape_all.fetch_detail(
                type("Session", (), {"page": generic})(),
                "https://tabelog.com/osaka/A2701/A270103/27136105/",
            )
            pending = Page(200)
            pending.evaluate = AsyncMock(return_value={
                "address": "名古屋市中区1", "rating": None, "status_text": "掲載保留",
                "name_status_text": "閉店 KOZONO このお店は現在閉店しております。店舗の掲載情報に関して",
            })
            pending_result = await scrape_all.fetch_detail(
                type("Session", (), {"page": pending})(),
                "https://tabelog.com/aichi/A2301/A230104/23052979/",
            )
        self.assertEqual(closed_result["operating_status"], "closed")
        self.assertEqual(generic_result["operating_status"], "closed")
        self.assertEqual(pending_result["operating_status"], "unknown")
        self.assertIn("label === '店名'", scrape_all.DETAIL_JS)
        self.assertIsNone(got["rating"])
        self.assertEqual(got["operating_status"], "unknown")
        self.assertEqual(got["address"], "東京都中央区3")

    def test_semantics_and_identity(self):
        self.assertEqual(updater.identity("https://tabelog.com/tokyo/A1301/A130101/13000001/"),
                         updater.identity("http://tabelog.com/tokyo/A1301/A130101/13000001/?x=1"))
        self.assertEqual(scrape_all.parse_detail_rating("3.40"), 3.4)
        self.assertIsNone(scrape_all.parse_detail_rating("-"))
        self.assertIsNone(scrape_all.parse_detail_rating("404"))
        self.assertIsNone(scrape_all.parse_detail_rating("0.00"))
        self.assertIsNone(scrape_all.parse_detail_rating("0.50"))
        self.assertEqual(scrape_all.parse_detail_rating("1.00"), 1.0)
        self.assertEqual(scrape_all.classify_operating_status("一時休業中"), "temporarily_closed")
        self.assertEqual(scrape_all.classify_operating_status("このお店は現在休業しております"), "temporarily_closed")
        self.assertEqual(scrape_all.classify_operating_status("閉店しました"), "closed")
        self.assertEqual(scrape_all.classify_operating_status("移転のため休業"), "unknown")
        self.assertEqual(scrape_all.classify_operating_status("閉店・休業の報告"), "unknown")
        self.assertEqual(scrape_all.classify_operating_status("閉店予定です"), "unknown")
        self.assertEqual(scrape_all.classify_operating_status("休業中でしたが営業再開"), "unknown")
        self.assertEqual(scrape_all.classify_operating_status("閉店していません"), "unknown")
        self.assertEqual(updater.identity("https://example.com/tokyo/A1301/A130101/13000001/"), "")
        self.assertEqual(updater.identity("https://tabelog.com/tokyo/A1301/A130101/13000001/", "osaka"), "")
        self.assertEqual(scrape_all.classify_operating_status(None), "unknown")


if __name__ == "__main__":
    unittest.main()
