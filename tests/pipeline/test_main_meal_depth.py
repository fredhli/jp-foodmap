"""Offline boundaries for the extra main-meal depth through the 3.50 tie band.

All list pages are in-memory fixtures. No browser, network, master CSV or build.
"""
from __future__ import annotations

import contextlib
import csv
import tempfile
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

import main as entry  # noqa: E402
from tabelog.scrape import scrape_all, region_update  # noqa: E402
from tabelog.scrape.region_selection import identity  # noqa: E402


def card(number: int, rating: str, *, region: str = 'aichi', genre: str = '寿司',
         dinner: int | None = 9999, lunch: int | None = 4999) -> dict:
    code = {'aichi': '23', 'tokyo': '13', 'osaka': '27'}[region]
    return {
        'region': region,
        'detail_url': f'https://tabelog.com/{region}/A{code}01/A{code}0101/{int(code) * 1000000 + number}/',
        'name': f'fixture {number}', 'rating': rating, 'genre': genre,
        'dinner_upper': dinner, 'lunch_upper': lunch,
        'source_page': 1, 'source_query': 'rating',
    }


class MainMealDepthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    async def collect_plain(self, pages: dict[int, list[dict]], *, total: int = 1000,
                            existing=(), ordinary_cap: int = 1,
                            main_ratio: float = 0.008) -> tuple[dict, list[int]]:
        calls: list[int] = []
        async def fetch(_session, region, page, template=None):
            self.assertEqual(region, 'aichi')
            self.assertIsNone(template)
            calls.append(page)
            if page not in pages:
                raise AssertionError(f'unexpected network page {page}')
            return [dict(row, source_page=page) for row in pages[page]], total
        with patch.object(scrape_all, 'scrape_list_page', side_effect=fetch):
            result = await entry.collect_region(None, 'aichi', top_pct=1,
                                                hard_cap=1, fine_dine_pct=.1,
                                                existing_rows=existing,
                                                main_meal_ratio=main_ratio,
                                                main_meal_cap=ordinary_cap,
                                                special_lists=False)
        return result, calls

    async def test_depth_can_extend_beyond_300_through_the_350_boundary(self):
        rows = [card(n, '3.60') for n in range(1, 306)]
        rows += [card(n, '3.50') for n in range(306, 316)]
        rows += [card(316, '3.49')]
        pages = {page: rows[(page - 1) * 20:page * 20]
                 for page in range(1, 17)}
        result, calls = await self.collect_plain(pages, total=100000,
                                                 ordinary_cap=300)
        self.assertEqual(calls, list(range(1, 17)))
        self.assertTrue(result['main_meal_depth_started'])
        self.assertTrue(result['main_meal_depth_complete'])
        self.assertEqual(result['main_meal_new'], 300)
        self.assertEqual(result['main_meal_depth_new'], 14)
        self.assertEqual(len(result['rows']), 315)  # base 1 + ordinary 300 + depth 14
        selected = {identity(row['detail_url']) for row in result['rows']}
        self.assertNotIn(identity(rows[-1]['detail_url']), selected)

    async def test_all_350_ties_across_pages_are_collected_before_349(self):
        pages = {
            1: [card(1, '4.00'), card(2, '3.60'), card(3, '3.50')],
            2: [card(4, '3.50'), card(5, '3.50')],
            3: [card(6, '3.49')],
        }
        result, calls = await self.collect_plain(pages)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(result['main_meal_new'], 1)
        self.assertEqual(result['main_meal_depth_new'], 3)
        self.assertTrue(result['main_meal_depth_complete'])
        selected = {identity(row['detail_url']) for row in result['rows']}
        self.assertEqual(selected, {identity(row['detail_url']) for row in pages[1][:3] + pages[2]})

    async def test_existing_whole_region_minimum_at_350_disables_extra_depth(self):
        old = card(9, '3.50', genre='喫茶店')
        pages = {1: [card(1, '4.00'), card(2, '3.60'), card(3, '3.50'), old]}
        result, calls = await self.collect_plain(pages, existing=[old])
        self.assertEqual(calls, [1])
        self.assertFalse(result['main_meal_depth_started'])
        self.assertEqual(result['main_meal_depth_new'], 0)
        self.assertNotIn(identity(pages[1][2]['detail_url']),
                         {identity(row['detail_url']) for row in result['rows']})

    async def test_ineligible_350_does_not_stop_before_later_eligible_ties(self):
        pages = {
            1: [card(1, '4.00'), card(2, '3.60'),
                card(3, '3.50', genre='喫茶店'), card(4, '3.50')],
            2: [card(5, '3.50', dinner=29999), card(6, '3.50'), card(7, '3.49')],
        }
        result, calls = await self.collect_plain(pages)
        self.assertEqual(calls, [1, 2])
        self.assertTrue(result['main_meal_depth_complete'])
        self.assertEqual(result['main_meal_depth_new'], 2)
        selected = {identity(row['detail_url']) for row in result['rows']}
        self.assertIn(identity(pages[1][3]['detail_url']), selected)
        self.assertIn(identity(pages[2][1]['detail_url']), selected)
        self.assertNotIn(identity(pages[1][2]['detail_url']), selected)
        self.assertNotIn(identity(pages[2][0]['detail_url']), selected)

    async def test_all_350_cards_filtered_are_observed_but_not_forced_in(self):
        pages = {
            1: [card(1, '4.00'), card(2, '3.60'),
                card(3, '3.50', genre='喫茶店')],
            2: [card(4, '3.50', dinner=20000), card(5, '3.49')],
        }
        result, calls = await self.collect_plain(pages)
        self.assertEqual(calls, [1, 2])
        self.assertTrue(result['main_meal_depth_started'])
        self.assertTrue(result['main_meal_depth_complete'])
        self.assertEqual(result['main_meal_depth_new'], 0)
        self.assertEqual(len(result['rows']), 2)
        self.assertGreater(result['observed_count'], len(result['rows']))

    async def test_old_349_can_be_observed_but_new_349_never_enters_depth(self):
        old = card(9, '4.00')
        pages = {
            1: [card(1, '4.00'), card(2, '3.60')],
            2: [card(3, '3.50'), card(9, '3.49'), card(4, '3.49')],
        }
        result, calls = await self.collect_plain(pages, existing=[old])
        self.assertEqual(calls, [1, 2])
        self.assertTrue(result['main_meal_depth_started'])
        self.assertEqual(result['main_meal_depth_new'], 1)
        observed = {identity(row['detail_url']): row for row in result['observed_rows']}
        self.assertEqual(observed[identity(old['detail_url'])]['rating'], '3.49')
        selected = {identity(row['detail_url']) for row in result['rows']}
        self.assertIn(identity(pages[2][0]['detail_url']), selected)
        self.assertNotIn(identity(pages[2][2]['detail_url']), selected)

    async def test_plain_page_60_ending_at_350_warns_without_page_61(self):
        pages = {1: [card(1, '4.00'), card(2, '3.60')]}
        pages.update({page: [card(page + 10, '3.50')] for page in range(2, 61)})
        result, calls = await self.collect_plain(pages)
        self.assertEqual(calls, list(range(1, 61)))
        self.assertTrue(result['main_meal_depth_started'])
        self.assertFalse(result['main_meal_depth_complete'])
        self.assertTrue(result['warnings'])
        self.assertIn('[WARNING]', self.output.getvalue())

    async def test_special_page_60_ending_at_350_or_351_warns_without_page_61(self):
        for score in ('3.50', '3.51'):
            with self.subTest(score=score):
                calls = []
                async def fetch(_session, region, page, template=None):
                    self.assertEqual(region, 'tokyo')
                    self.assertIsNotNone(template)
                    calls.append(page)
                    self.assertEqual(page, 60)
                    return [card(100, score, region='tokyo')], 100000
                with patch.object(scrape_all, 'scrape_list_page', side_effect=fetch):
                    result = await entry.collect_region(None, 'tokyo',
                        existing_rows=(), append_only=True,
                        special_start_pages={'tokyo': 60})
                self.assertEqual(calls, [60])
                self.assertEqual(result['special']['status'], 'page_limit')
                self.assertEqual(result['special']['last_score'], float(score))
                self.assertTrue(result['warnings'])
        self.assertIn('[WARNING]', self.output.getvalue())

    async def test_explicit_zero_ratio_or_cap_disables_depth(self):
        pages = {1: [card(1, '4.00'), card(2, '3.60')]}
        for ratio, cap in ((0, 1), (.008, 0)):
            result, calls = await self.collect_plain(pages, main_ratio=ratio,
                                                     ordinary_cap=cap)
            self.assertEqual(calls, [1])
            self.assertFalse(result['main_meal_depth_started'])
            self.assertEqual(result['main_meal_depth_new'], 0)

    async def test_late_trigger_revisits_cached_unselected_main_meals(self):
        old = card(9, '3.49', genre='喫茶店')
        pages = {
            1: [card(1, '4.00'), card(2, '3.90')],
            2: [card(9, '3.80', genre='喫茶店'), card(3, '3.70')],
            3: [card(4, '3.50'), card(5, '3.49')],
        }
        result, calls = await self.collect_plain(pages, total=100, existing=[old])
        self.assertEqual(calls, [1, 2, 3])
        self.assertTrue(result['main_meal_depth_started'])
        self.assertEqual(result['main_meal_depth_new'], 3)
        chosen = {identity(row['detail_url']) for row in result['rows']}
        self.assertIn(identity(pages[1][1]['detail_url']), chosen)
        self.assertNotIn(identity(pages[3][1]['detail_url']), chosen)

    async def test_depth_only_detail_349_is_rejected_but_old_349_remains(self):
        old = dict(card(1, '4.00'), address='old address')
        other_old = dict(card(9, '4.00'), address='another old address')
        pages = {1: [card(1, '4.00'), card(2, '3.60'), card(3, '3.50')],
                 2: [card(9, '3.49'), card(4, '3.49')]}
        result, _ = await self.collect_plain(pages, total=100, existing=[old, other_old])
        self.assertEqual(result['selected_origins'][identity(card(2, '3.60')['detail_url'])], ['main_meal_depth'])
        async def detail(_session, url):
            score = 3.49 if url == card(2, '3.60')['detail_url'] else 3.50
            return {'rating': score, 'address': 'new address'}
        with tempfile.TemporaryDirectory(prefix='meal-depth-commit-') as directory:
            root = Path(directory)
            csv_path = root / 'restaurants.csv'
            with csv_path.open('w', encoding='utf-8-sig', newline='') as file:
                writer = csv.DictWriter(file, fieldnames=list(old))
                writer.writeheader(); writer.writerows([old, other_old])
            with patch.object(scrape_all, 'fetch_detail', side_effect=detail) as fetch:
                report = await region_update.apply_region(None, 'aichi', csv_path,
                    root/'archive.jsonl', root/'report.json', 'fixture',
                    selection=result, translate=False)
            self.assertEqual(fetch.await_count, 2)
            with csv_path.open(encoding='utf-8-sig', newline='') as file:
                saved = {identity(row['detail_url']): row for row in csv.DictReader(file)}
            self.assertEqual(len(saved), 3)
            self.assertEqual(saved[identity(other_old['detail_url'])]['rating'], '3.49')
            self.assertNotIn(identity(card(2, '3.60')['detail_url']), saved)
            self.assertEqual(report['counts']['new_added'], 1)

    async def test_page_limit_message_distinguishes_already_crossed_score_band(self):
        old = card(999, '4.00')
        pages = {1: [card(1, '4.00'), card(2, '3.60')],
                 2: [card(3, '3.50')], 3: [card(4, '3.49')]}
        pages.update({page: [card(10+page, '3.45')] for page in range(4, 61)})
        result, calls = await self.collect_plain(pages, existing=[old])
        warning = next(w for w in result['warnings'] if w['code'] == 'page_limit')
        self.assertEqual(calls[-1], 60)
        self.assertTrue(warning['target_band_complete'])
        self.assertIn('已读过3.50', warning['message'])

    async def test_tokyo_osaka_custom_urls_share_one_collector_path(self):
        special_urls = {
            'tokyo': 'https://tabelog.com/tokyo/rstLst/RC/11/?SrtT=rt&probe=depth-test',
            'osaka': 'https://tabelog.com/osaka/rstLst/RC/9/?SrtT=rt&probe=depth-test',
        }
        for region, start in (('tokyo', 11), ('osaka', 9)):
            with self.subTest(region=region):
                calls = []
                async def fetch(_session, seen_region, page, template=None):
                    self.assertEqual(seen_region, region)
                    calls.append((page, template))
                    if template:
                        self.assertEqual(page, start)
                        self.assertIn('probe=depth-test', template)
                        return [card(100, '3.50', region=region),
                                card(101, '3.49', region=region)], 1000
                    self.assertEqual(page, 1)
                    return [card(1, '4.00', region=region)], 100
                results = []
                with patch.object(scrape_all, 'scrape_list_page', side_effect=fetch):
                    for _mode in ('full', 'bimonth'):
                        results.append(await entry.collect_region(None, region,
                            main_meal_ratio=0, special_urls=special_urls,
                            special_start_pages={region: start}))
                self.assertEqual(len(calls), 4)
                self.assertEqual(calls[0][0:1], calls[2][0:1])
                self.assertEqual(calls[1][0:1], calls[3][0:1])
                self.assertEqual(results[0]['special'], results[1]['special'])
                self.assertEqual({identity(row['detail_url']) for row in results[0]['rows']},
                                 {identity(row['detail_url']) for row in results[1]['rows']})


if __name__ == '__main__':
    unittest.main()
