"""Offline interruption and recovery checks for the bimonth pipeline."""
import asyncio
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from datetime import date
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('resume_entry', ROOT / 'main.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)

from tabelog.scrape import bimonth_resume, region_update, region_selection, scrape_all  # noqa: E402


def card(region, number, score='3.60'):
    return {'region': region, 'detail_url': f'https://tabelog.com/{region}/A2301/A230101/{number:08d}/',
            'name': f'restaurant {number}', 'rating': score, 'genre': '寿司',
            'address': 'old address', 'source_page': 1}


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


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory(prefix='bimonth-resume-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.csv = self.root / 'tabelog.csv'
        self.archive = self.root / 'departures.jsonl'
        self.runs = self.root / 'runs'

    def save(self, rows):
        fields = list(dict.fromkeys(scrape_all.FIELDS + ['rating_check']))
        with self.csv.open('w', encoding='utf-8-sig', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows([{key: row.get(key, '') for key in fields} for row in rows])

    def report(self, directory, region, *, incomplete=False, commit='complete'):
        value = {'region': region, 'selection': {'status': 'partial' if incomplete else 'quota_reached'},
                 'incomplete': incomplete, 'commit': commit, 'departures': [], 'pending': []}
        (directory / f'{region}.json').write_text(json.dumps(value), encoding='utf-8')
        return value

    def geocode(self):
        """What map.py does after a run: fill missing lat/lon in the active CSV."""
        fields, rows = region_update.read_corpus(self.csv)
        for row in rows:
            if not row.get('lat'):
                row['lat'], row['lon'] = '35.1', '136.9'
        region_update.atomic_write_csv(self.csv, rows, fields)

    def edit_csv(self, **changes):
        fields, rows = region_update.read_corpus(self.csv)
        rows[0].update(changes)
        region_update.atomic_write_csv(self.csv, rows, fields)

    def manifest(self, directory, regions):
        parameters = dict(entry.DEFAULTS, translate=False, special_lists=False, no_build=True)
        return bimonth_resume.RunManifest.new(directory, regions=regions, parameters=parameters,
                                              special_lists={}, csv_path=self.csv, archive_path=self.archive)

    def test_resume_requires_bimonth_and_latest_interrupted_run(self):
        with self.assertRaises(SystemExit):
            entry.parse_args(['--resume'])
        self.save([card('aichi', 23000001)])
        with self.assertRaisesRegex(ValueError, 'no bimonth runs'):
            asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--dry-run']),
                                           csv_path=self.csv, run_dir=self.runs))
        old = self.runs / '20260924T100000Z-older'
        old.mkdir(parents=True)
        self.report(old, 'aichi', incomplete=True)
        newer = self.runs / '20260925T100000Z-newer'
        newer.mkdir()
        self.report(newer, 'aichi')
        (newer / 'run.json').write_text(json.dumps({'ok': True, 'mode': 'bimonth'}))
        with self.assertRaisesRegex(ValueError, 'completed; refusing'):
            asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--dry-run']),
                                           csv_path=self.csv, run_dir=self.runs))
        empty = self.runs / '20260926T100000Z-empty'
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, 'no usable manifest'):
            asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--dry-run']),
                                           csv_path=self.csv, run_dir=self.runs))
        (empty / 'manifest.json').write_text(json.dumps({'version': 2, 'mode': 'bimonth'}))
        with self.assertRaisesRegex(ValueError, 'unsupported manifest'):
            asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--dry-run']),
                                           csv_path=self.csv, run_dir=self.runs))

    def test_legacy_preview_is_read_only_and_retries_only_unfinished_regions(self):
        self.save([card('aichi', 23000001), card('chiba', 23000002), card('ehime', 23000003)])
        directory = self.runs / '20260925T020333Z-legacy'
        directory.mkdir(parents=True)
        self.report(directory, 'aichi')
        self.report(directory, 'chiba', incomplete=True)
        before_csv = self.csv.read_bytes()
        before_files = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(entry, 'async_playwright', side_effect=AssertionError('browser started')):
            result = asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--dry-run']),
                                                   csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))
        self.assertEqual(result['resume']['completed'], ['aichi'])
        self.assertEqual(result['resume']['remaining'], ['chiba', 'ehime'])
        self.assertEqual(result['resume']['source_run'], str(directory))
        self.assertEqual(result['special_lists'], {})
        self.assertEqual(self.csv.read_bytes(), before_csv)
        self.assertEqual(before_files, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        with self.assertRaisesRegex(ValueError, 'no saved parameters'):
            asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--top-pct', '2', '--dry-run']),
                                           csv_path=self.csv, run_dir=self.runs))

    def test_legacy_adoption_checkpoints_survive_a_second_interruption(self):
        old = card('aichi', 23000001, '3.50')
        self.save([old, card('akita', 50000001), card('tokyo', 13000001)])
        directory = self.runs / '20260925T020333Z-legacy'
        directory.mkdir(parents=True)
        self.report(directory, 'akita')
        self.report(directory, 'tokyo')
        (directory / 'run.json').write_text(json.dumps({'ok': False, 'mode': 'bimonth',
                                                        'reports': [{'region': 'akita'}]}))
        original_csv = self.csv.read_bytes()
        first = card('aichi', 23000002, '3.80')
        second = card('aichi', 23000003, '3.70')
        page_calls, detail_calls = [], []
        async def list_page(_session, _region, page, _template):
            page_calls.append(page)
            self.assertEqual(page, 1)
            return [first, second, old], 300
        async def interrupted_detail(_session, url):
            detail_calls.append(url)
            if url == second['detail_url']:
                raise KeyboardInterrupt('second shutdown')
            return {'rating': 3.8, 'address': 'new address', 'operating_status': 'unknown'}
        with patch.object(entry, 'async_playwright', return_value=Playwright()), \
             patch.object(scrape_all, 'Session', Session), \
             patch.object(scrape_all, 'scrape_list_page', list_page), \
             patch.object(scrape_all, 'fetch_detail', interrupted_detail), \
             patch.object(scrape_all, 'translate_reservation_policy', new=AsyncMock()):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume']),
                                               csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))
        manifest = json.loads((directory / 'manifest.json').read_text())
        self.assertEqual(manifest['completed'], ['akita', 'tokyo'])
        self.assertEqual(manifest['current_region'], 'aichi')
        self.assertIn('legacy_adoption', manifest)
        self.assertIn('inferred', manifest['legacy_adoption']['regions'])
        self.assertTrue(manifest['parameters']['no_build'])
        self.assertIn('svd=20260925', manifest['special_lists']['tokyo']['url'])
        self.assertEqual(manifest['special_lists']['tokyo']['start_page'], 10)
        self.assertEqual(bimonth_resume.FetchCheckpoints(directory, 'aichi').available(),
                         {'list_pages': 1, 'details': 1})
        self.assertEqual(self.csv.read_bytes(), original_csv)
        self.assertEqual(page_calls, [1])
        self.assertEqual(detail_calls, [first['detail_url'], second['detail_url']])
        detail_calls.clear()
        async def resumed_detail(_session, url):
            detail_calls.append(url)
            self.assertEqual(url, second['detail_url'])
            return {'rating': 3.7, 'address': 'new address', 'operating_status': 'unknown'}
        with patch.object(entry, 'async_playwright', return_value=Playwright()), \
             patch.object(scrape_all, 'Session', Session), \
             patch.object(scrape_all, 'scrape_list_page', side_effect=AssertionError('list page fetched again')), \
             patch.object(scrape_all, 'fetch_detail', resumed_detail), \
             patch.object(scrape_all, 'translate_reservation_policy', new=AsyncMock()):
            result = asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume']),
                                                    csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))
        self.assertTrue(result['ok'])
        self.assertEqual(result['checkpoint_reuse']['aichi'], {'list_pages': 1, 'details': 1})
        self.assertEqual(detail_calls, [second['detail_url']])
        self.assertEqual(len(region_update.read_corpus(self.csv)[1]), 5)
        self.assertTrue(json.loads((directory / 'manifest.json').read_text())['finished'])

    def test_legacy_all_committed_finishes_without_browser_or_rework(self):
        self.save([card('aichi', 23000001)])
        directory = self.runs / '20260925T020333Z-legacy'
        directory.mkdir(parents=True)
        self.report(directory, 'aichi')
        before = self.csv.read_bytes()
        with patch.object(entry, 'async_playwright', side_effect=AssertionError('browser started')):
            result = asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume']),
                                                    csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))
        self.assertTrue(result['ok'])
        self.assertEqual(result['resume']['remaining'], [])
        self.assertEqual(self.csv.read_bytes(), before)
        self.assertTrue(json.loads((directory / 'run.json').read_text())['ok'])
        with self.assertRaisesRegex(ValueError, 'completed; refusing'):
            asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--dry-run']),
                                           csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))

    def test_manifest_precedes_browser_and_resume_skips_committed_region(self):
        self.save([card('aichi', 23000001), card('akita', 23000002)])
        calls = []
        async def collect(_session, region, *_args, **_kwargs):
            calls.append(region)
            if region == 'akita' and calls.count('akita') == 1:
                raise KeyboardInterrupt('simulated power loss')
            return {'rows': [], 'status': 'quota_reached'}
        async def apply(_session, region, _csv, _archive, path, _run_id, **_kwargs):
            value = {'region': region, 'incomplete': False, 'commit': 'complete',
                     'departures': [], 'selection': {}, 'pending': []}
            entry.atomic_write_json(path, value)
            return value
        with patch.object(entry, 'async_playwright', return_value=Playwright()), \
             patch.object(scrape_all, 'Session', Session), \
             patch.object(entry, 'collect_region', collect), \
             patch.object(region_update, 'apply_region', apply):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--no-build']),
                                               csv_path=self.csv, run_dir=self.runs))
            directory = next(self.runs.iterdir())
            manifest = json.loads((directory / 'manifest.json').read_text())
            self.assertEqual(manifest['completed'], ['aichi'])
            self.assertTrue(manifest['parameters']['no_build'])
            self.assertEqual(manifest['current_region'], 'akita')
            result = asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume']),
                                                    csv_path=self.csv, run_dir=self.runs))
        self.assertTrue(result['ok'])
        self.assertTrue(result['parameters']['no_build'])
        self.assertEqual(calls, ['aichi', 'akita', 'akita'])
        self.assertEqual(result['resume']['completed'], ['aichi'])
        self.assertTrue(json.loads((directory / 'manifest.json').read_text())['finished'])
        with self.assertRaisesRegex(ValueError, 'completed; refusing'):
            asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--dry-run']),
                                           csv_path=self.csv, run_dir=self.runs))

    def test_resume_honors_original_no_build_policy(self):
        with patch.object(entry, 'run_pipeline', new=AsyncMock(return_value={
                'ok': True, 'parameters': {'no_build': True}})), \
             patch.object(entry.map_mod, 'main') as build:
            asyncio.run(entry.run(['--bimonth-update', '--resume']))
        build.assert_not_called()

    def test_modern_config_and_corpus_drift_are_rejected(self):
        self.save([card('aichi', 23000001)])
        directory = self.runs / '20260925T100000Z-fixture'
        directory.mkdir(parents=True)
        parameters = dict(entry.DEFAULTS, translate=False, special_lists=False)
        bimonth_resume.RunManifest.new(directory, regions=['aichi'], parameters=parameters,
                                       special_lists={}, csv_path=self.csv, archive_path=self.archive)
        self.report(directory, 'aichi')
        with self.assertRaisesRegex(ValueError, 'translate differs'):
            asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--translate', '--dry-run']),
                                           csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))
        report = json.loads((directory / 'aichi.json').read_text())
        report['region_after_sha256'] = 'incorrect'
        (directory / 'aichi.json').write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'active CSV differs'):
            asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--dry-run']),
                                           csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))

    def test_page_limited_report_from_old_rule_finishes_without_browser(self):
        self.save([card('tokyo', 13000001), card('aichi', 23000001)])
        directory = self.runs / '20260925T100000Z-fixture'
        directory.mkdir(parents=True)
        self.manifest(directory, ['aichi', 'tokyo'])
        self.report(directory, 'aichi')
        # Written before page-limited regions counted as complete.
        report = self.report(directory, 'tokyo', incomplete=True)
        report['selection'] = {'status': 'truncated', 'warnings': [{'code': 'page_limit', 'region': 'tokyo'}]}
        (directory / 'tokyo.json').write_text(json.dumps(report))
        before = self.csv.read_bytes()
        with patch.object(entry, 'async_playwright', side_effect=AssertionError('browser started')):
            result = asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume']),
                                                    csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))
        self.assertTrue(result['ok'])
        self.assertEqual(result['resume']['remaining'], [])
        self.assertEqual(result['page_limited_regions'], ['tokyo'])
        self.assertEqual(result['warnings'], [{'code': 'page_limit', 'region': 'tokyo'}])
        self.assertEqual(self.csv.read_bytes(), before)
        manifest = json.loads((directory / 'manifest.json').read_text())
        self.assertTrue(manifest['finished'])
        self.assertEqual(manifest['completed'], ['aichi', 'tokyo'])

    def test_map_build_after_commit_does_not_block_resume(self):
        self.save([card('aichi', 23000001), dict(card('aichi', 23000002), scraped_at='2026-09-25T05:00:00Z')])
        directory = self.runs / '20260925T100000Z-fixture'
        directory.mkdir(parents=True)
        self.manifest(directory, ['aichi', 'akita'])
        report = self.report(directory, 'aichi')
        report['region_after_sha256'] = bimonth_resume.region_fingerprint(
            region_update.read_corpus(self.csv)[1], 'aichi')
        (directory / 'aichi.json').write_text(json.dumps(report))
        self.geocode()
        preview = asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--dry-run']),
                                                 csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))
        self.assertEqual(preview['resume']['completed'], ['aichi'])
        self.assertEqual(preview['resume']['remaining'], ['akita'])
        self.edit_csv(rating='3.90')
        with self.assertRaisesRegex(ValueError, 'active CSV differs'):
            asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume', '--dry-run']),
                                           csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))

    def test_legacy_region_digest_tolerates_later_coordinates_only(self):
        old = dict(card('aichi', 23000001), lat='35.0', lon='136.0')
        new = dict(card('aichi', 23000002), scraped_at='2026-09-25T05:00:00Z', lat='', lon='')
        other = dict(card('akita', 50000001), lat='', lon='')
        # The whole-row digest reports carried before content fingerprints.
        selected = [row for row in (old, new, other) if row['region'] == 'aichi']
        recorded = hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True,
                                             separators=(',', ':')).encode('utf-8')).hexdigest()
        geocoded_new = dict(new, lat='35.1', lon='136.9')
        self.assertTrue(bimonth_resume.region_matches([old, new, other], 'aichi', recorded))
        self.assertTrue(bimonth_resume.region_matches(
            [old, geocoded_new, dict(other, lat='39.7', lon='140.1')], 'aichi', recorded))
        self.assertFalse(bimonth_resume.region_matches(
            [old, dict(geocoded_new, rating='3.90'), other], 'aichi', recorded))
        self.assertFalse(bimonth_resume.region_matches(
            [dict(old, lat='35.5'), geocoded_new, other], 'aichi', recorded))

    def test_in_flight_region_snapshot_survives_map_build(self):
        original = card('aichi', 23000001)
        self.save([original])
        directory = self.runs / '20260925T100000Z-fixture'
        directory.mkdir(parents=True)
        manifest = self.manifest(directory, ['aichi'])
        manifest.begin('aichi', bimonth_resume.region_fingerprint(region_update.read_corpus(self.csv)[1], 'aichi'))
        calls = []
        async def collect(_session, region, *_args, **_kwargs):
            calls.append(region)
            return {'rows': [], 'status': 'quota_reached'}
        async def apply(_session, region, _csv, _archive, path, _run_id, **_kwargs):
            value = {'region': region, 'incomplete': False, 'commit': 'complete',
                     'departures': [], 'selection': {}, 'pending': []}
            entry.atomic_write_json(path, value)
            return value
        def resume():
            with patch.object(entry, 'async_playwright', return_value=Playwright()), \
                 patch.object(scrape_all, 'Session', Session), \
                 patch.object(entry, 'collect_region', collect), \
                 patch.object(region_update, 'apply_region', apply):
                return asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume']),
                                                      csv_path=self.csv, archive_path=self.archive,
                                                      run_dir=self.runs))
        self.edit_csv(rating='3.90')
        result = resume()
        self.assertFalse(result['ok'])
        self.assertIn('refusing cached replay', result['errors'][0]['error'])
        self.assertEqual(calls, [])
        self.save([original])
        self.geocode()
        result = resume()
        self.assertTrue(result['ok'])
        self.assertEqual(calls, ['aichi'])

    def test_list_and_detail_checkpoints_replay_successful_results(self):
        directory = self.runs / 'run'
        checkpoint = bimonth_resume.FetchCheckpoints(directory, 'aichi')
        session = Session(None)
        session.resume_checkpoints = checkpoint
        sample = card('aichi', 23000001)
        async def page(*_args):
            return [sample], 100
        with patch.object(scrape_all, 'scrape_list_page', page):
            rows = asyncio.run(region_selection._RankedScan(session, 'aichi').read())
        self.assertEqual(rows, [sample])
        with patch.object(scrape_all, 'scrape_list_page', side_effect=AssertionError('page refetched')):
            rows = asyncio.run(region_selection._RankedScan(session, 'aichi').read())
        self.assertEqual(rows, [sample])
        self.assertEqual(checkpoint.replayed_lists, 1)
        good = {'rating': 3.6, 'address': 'address', 'operating_status': 'unknown'}
        with patch.object(scrape_all, 'fetch_detail', new=AsyncMock(return_value=good)) as fetch:
            self.assertEqual(asyncio.run(checkpoint.detail(session, sample['detail_url'])), good)
            self.assertEqual(asyncio.run(checkpoint.detail(session, sample['detail_url'])), good)
            self.assertEqual(fetch.await_count, 1)
        bad_url = card('aichi', 23000002)['detail_url']
        with patch.object(scrape_all, 'fetch_detail', new=AsyncMock(side_effect=RuntimeError('offline'))) as fetch:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    asyncio.run(checkpoint.detail(session, bad_url))
            self.assertEqual(fetch.await_count, 2)

    def test_interrupted_collection_replays_valid_pages_then_fetches_next(self):
        checkpoint = bimonth_resume.FetchCheckpoints(self.runs / 'run', 'aichi')
        session = Session(None)
        session.resume_checkpoints = checkpoint
        calls = []
        async def interrupted(_session, _region, page, _template):
            calls.append(page)
            if page == 2:
                raise KeyboardInterrupt('list interruption')
            return [card('aichi', 23000001, '3.80')], 200
        with patch.object(scrape_all, 'scrape_list_page', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(entry.collect_region(session, 'aichi', existing_rows=[],
                                                 main_meal_ratio=0, main_meal_cap=0,
                                                 special_lists=False))
        self.assertEqual(calls, [1, 2])
        calls.clear()
        async def resumed(_session, _region, page, _template):
            calls.append(page)
            self.assertEqual(page, 2)
            return [card('aichi', 23000002, '3.70')], 200
        with patch.object(scrape_all, 'scrape_list_page', resumed):
            result = asyncio.run(entry.collect_region(session, 'aichi', existing_rows=[],
                                                      main_meal_ratio=0, main_meal_cap=0,
                                                      special_lists=False))
        self.assertEqual(calls, [2])
        self.assertEqual(result['base_selected'], 2)
        self.assertEqual(checkpoint.replayed_lists, 1)

    def test_resume_inherits_resolved_special_query_and_parameters(self):
        self.save([card('tokyo', 13000001)])
        directory = self.runs / '20260920T100000Z-fixture'
        directory.mkdir(parents=True)
        original_url = entry.SPECIAL_LIST_URLS['tokyo']
        resolved = entry.scrape_topup.resolve_list_url_date(original_url, today=date(2026, 9, 20))
        parameters = dict(entry.DEFAULTS, top_pct=2.0, translate=False, special_lists=True)
        special = {'tokyo': {'url': resolved, 'start_page': 11}}
        bimonth_resume.RunManifest.new(directory, regions=['tokyo'], parameters=parameters,
                                       special_lists=special, csv_path=self.csv, archive_path=self.archive)
        preview = asyncio.run(entry.run_pipeline(entry.parse_args([
            '--bimonth-update', '--resume', '--tokyo-list-url', original_url, '--dry-run']),
            csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))
        self.assertEqual(preview['special_lists']['tokyo']['url'], resolved)
        changed_url = original_url.replace('LstCosT=10', 'LstCosT=11')
        with self.assertRaisesRegex(ValueError, 'list URL differs'):
            asyncio.run(entry.run_pipeline(entry.parse_args([
                '--bimonth-update', '--resume', '--tokyo-list-url', changed_url, '--dry-run']),
                csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))
        calls = []
        async def collect(_session, region, top_pct, *_args, **kwargs):
            calls.append((region, top_pct, kwargs))
            return {'rows': [], 'status': 'quota_reached'}
        async def apply(_session, region, _csv, _archive, path, _run_id, **_kwargs):
            value = {'region': region, 'incomplete': False, 'commit': 'complete',
                     'departures': [], 'selection': {}, 'pending': []}
            entry.atomic_write_json(path, value)
            return value
        with patch.object(entry, 'async_playwright', return_value=Playwright()), \
             patch.object(scrape_all, 'Session', Session), \
             patch.object(entry, 'collect_region', collect), \
             patch.object(region_update, 'apply_region', apply), \
             patch.object(entry.scrape_topup, 'resolve_list_url_date', side_effect=AssertionError('date re-resolved')):
            result = asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', '--resume']),
                                                    csv_path=self.csv, archive_path=self.archive, run_dir=self.runs))
        self.assertTrue(result['ok'])
        self.assertEqual(calls[0][0:2], ('tokyo', 2.0))
        self.assertEqual(calls[0][2]['special_urls']['tokyo'], resolved)
        self.assertEqual(calls[0][2]['special_start_pages']['tokyo'], 11)
        self.assertFalse(result['parameters']['translate'])

    def test_archive_written_before_report_complete_is_not_duplicated(self):
        old = card('aichi', 23000001)
        self.save([old])
        selected = dict(old, rating='3.39')
        selection = {'rows': [selected], 'observed_rows': [selected], 'status': 'quota_reached',
                     'pages': 1, 'issue': '', 'main_meal_target': 0}
        report_path = self.root / 'aichi.json'
        original_write = region_update.atomic_write_json
        def crash_after_archive(path, value, **kwargs):
            if Path(path) == report_path and value.get('commit') == 'complete':
                raise RuntimeError('report interruption')
            return original_write(path, value, **kwargs)
        with patch.object(region_update, 'atomic_write_json', crash_after_archive):
            with self.assertRaisesRegex(RuntimeError, 'report interruption'):
                asyncio.run(region_update.apply_region(None, 'aichi', self.csv, self.archive, report_path,
                                                       'run-1', selection=selection, translate=False))
        self.assertEqual(json.loads(report_path.read_text())['commit'], 'prepared')
        self.assertEqual(len(self.archive.read_text().splitlines()), 1)
        self.assertEqual(region_update.recover_prepared(report_path, self.csv, self.archive), 'committed')
        self.assertEqual(len(self.archive.read_text().splitlines()), 1)

    def test_interrupted_detail_loop_reuses_finished_detail(self):
        first, second = card('aichi', 23000001), card('aichi', 23000002)
        selection = {'rows': [first, second], 'status': 'quota_reached',
                     'pages': 1, 'issue': '', 'main_meal_target': 0}
        session = Session(None)
        session.resume_checkpoints = bimonth_resume.FetchCheckpoints(self.runs / 'run', 'aichi')
        report_path = self.root / 'aichi.json'
        calls = []
        async def interrupted(_session, url):
            calls.append(url)
            if url == second['detail_url']:
                raise KeyboardInterrupt('detail interruption')
            return {'rating': 3.6, 'address': 'address', 'operating_status': 'unknown'}
        with patch.object(scrape_all, 'fetch_detail', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(region_update.apply_region(session, 'aichi', self.csv, self.archive,
                                                       report_path, 'run', selection=selection, translate=False))
        self.assertEqual(calls, [first['detail_url'], second['detail_url']])
        self.assertFalse(self.csv.exists())
        calls.clear()
        async def resumed(_session, url):
            calls.append(url)
            self.assertEqual(url, second['detail_url'])
            return {'rating': 3.6, 'address': 'address', 'operating_status': 'unknown'}
        with patch.object(scrape_all, 'fetch_detail', resumed):
            report = asyncio.run(region_update.apply_region(session, 'aichi', self.csv, self.archive,
                                                            report_path, 'run', selection=selection, translate=False))
        self.assertEqual(calls, [second['detail_url']])
        self.assertEqual(report['counts']['new_added'], 2)
        self.assertEqual(len(region_update.read_corpus(self.csv)[1]), 2)

    def test_prepared_commit_recovers_csv_and_archive_once(self):
        old = card('aichi', 23000001)
        self.save([old])
        selected = dict(old, rating='3.39')
        selection = {'rows': [selected], 'observed_rows': [selected], 'status': 'quota_reached',
                     'pages': 1, 'issue': '', 'main_meal_target': 0}
        report_path = self.root / 'aichi.json'
        original = region_update._append_archive
        with patch.object(region_update, '_append_archive', side_effect=RuntimeError('archive interruption')):
            with self.assertRaisesRegex(RuntimeError, 'archive interruption'):
                asyncio.run(region_update.apply_region(None, 'aichi', self.csv, self.archive, report_path,
                                                       'run-1', selection=selection, translate=False))
        self.assertEqual(json.loads(report_path.read_text())['commit'], 'prepared')
        self.assertEqual(region_update.read_corpus(self.csv)[1], [])
        self.assertEqual(region_update.recover_prepared(report_path, self.csv, self.archive), 'committed')
        self.assertEqual(region_update.recover_prepared(report_path, self.csv, self.archive), 'none')
        self.assertEqual(len(self.archive.read_text().splitlines()), 1)
        self.assertEqual(json.loads(report_path.read_text())['commit'], 'complete')
        self.assertIsNotNone(original)

    def test_prepared_before_csv_retries_and_drift_refuses(self):
        old = card('aichi', 23000001)
        self.save([old])
        selected = dict(old, rating='3.39')
        selection = {'rows': [selected], 'observed_rows': [selected], 'status': 'quota_reached',
                     'pages': 1, 'issue': '', 'main_meal_target': 0}
        report_path = self.root / 'aichi.json'
        before = self.csv.read_bytes()
        with patch.object(region_update, 'atomic_write_csv', side_effect=RuntimeError('CSV interruption')):
            with self.assertRaisesRegex(RuntimeError, 'CSV interruption'):
                asyncio.run(region_update.apply_region(None, 'aichi', self.csv, self.archive, report_path,
                                                       'run-1', selection=selection, translate=False))
        self.assertEqual(self.csv.read_bytes(), before)
        self.assertEqual(region_update.recover_prepared(report_path, self.csv, self.archive), 'retry')
        self.assertFalse(self.archive.exists())
        self.save([dict(old, rating='4.00')])
        with self.assertRaisesRegex(ValueError, 'active CSV changed'):
            region_update.recover_prepared(report_path, self.csv, self.archive)

    def test_prepared_commit_recovers_after_map_build(self):
        low, kept = card('aichi', 23000001), card('aichi', 23000002)
        self.save([low, kept])
        selection = {'rows': [], 'observed_rows': [dict(low, rating='3.39'), kept], 'status': 'quota_reached',
                     'pages': 1, 'issue': '', 'main_meal_target': 0}
        report_path = self.root / 'aichi.json'
        with patch.object(region_update, '_append_archive', side_effect=RuntimeError('archive interruption')):
            with self.assertRaisesRegex(RuntimeError, 'archive interruption'):
                asyncio.run(region_update.apply_region(None, 'aichi', self.csv, self.archive, report_path,
                                                       'run-1', selection=selection, translate=False))
        self.geocode()
        self.assertEqual(region_update.recover_prepared(report_path, self.csv, self.archive), 'committed')
        self.assertEqual(len(self.archive.read_text().splitlines()), 1)
        report = json.loads(report_path.read_text())
        self.assertTrue(bimonth_resume.region_matches(region_update.read_corpus(self.csv)[1], 'aichi',
                                                      report['region_after_sha256']))

    def test_prepared_before_csv_retries_after_map_build(self):
        old = card('aichi', 23000001)
        self.save([old])
        selection = {'rows': [dict(old, rating='3.39')], 'observed_rows': [dict(old, rating='3.39')],
                     'status': 'quota_reached', 'pages': 1, 'issue': '', 'main_meal_target': 0}
        report_path = self.root / 'aichi.json'
        with patch.object(region_update, 'atomic_write_csv', side_effect=RuntimeError('CSV interruption')):
            with self.assertRaisesRegex(RuntimeError, 'CSV interruption'):
                asyncio.run(region_update.apply_region(None, 'aichi', self.csv, self.archive, report_path,
                                                       'run-1', selection=selection, translate=False))
        self.geocode()
        self.assertEqual(region_update.recover_prepared(report_path, self.csv, self.archive), 'retry')
        self.assertFalse(self.archive.exists())
        self.edit_csv(rating='4.00')
        with self.assertRaisesRegex(ValueError, 'active CSV changed'):
            region_update.recover_prepared(report_path, self.csv, self.archive)


if __name__ == '__main__':
    unittest.main()
