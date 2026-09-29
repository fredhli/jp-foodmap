"""Offline entry-point checks. Browser, scraper, build and external I/O are mocked."""
import asyncio
import csv
import contextlib
import io
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('update_entry', ROOT / 'main.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)

from tabelog.scrape import bimonth_update as shim, region_update, scrape_all  # noqa: E402


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


class EntryPoints(unittest.TestCase):
    def setUp(self):
        self.quiet_out = contextlib.redirect_stdout(io.StringIO())
        self.quiet_err = contextlib.redirect_stderr(io.StringIO())
        self.quiet_out.__enter__()
        self.quiet_err.__enter__()
        self.addCleanup(self.quiet_err.__exit__, None, None, None)
        self.addCleanup(self.quiet_out.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory(prefix='tabelog-entry-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.csv = self.root / 'tabelog.csv'
        self.archive = self.root / 'departures.jsonl'
        self.run_dir = self.root / 'runs'

    def save(self, rows):
        fields = ['region', 'detail_url', 'genre', 'rating', 'address', 'rank', 'source_page']
        with self.csv.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_bimonth_shim_dry_run_calls_real_main_without_browser_or_writes(self):
        self.save([{'region': 'aichi', 'detail_url': 'https://tabelog.com/aichi/A2301/A230101/23000001/'}])
        before = self.csv.read_bytes()
        project = shim.project_main()
        with patch.object(project, 'async_playwright', side_effect=AssertionError('browser started')), \
             patch.object(project.map_mod, 'main', side_effect=AssertionError('map built')):
            result = asyncio.run(shim.run_bimonth_update('aichi', csv_path=self.csv,
                                                         archive_path=self.archive,
                                                         run_dir=self.run_dir, dry_run=True))
        self.assertEqual(result['mode'], 'bimonth')
        self.assertEqual(result['existing'], {'aichi': 1})
        self.assertEqual(self.csv.read_bytes(), before)
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.run_dir.exists())
        self.assertFalse((self.root / 'tabelog.csv.bimonth.lock').exists())

    def test_full_and_append_dry_runs_are_local(self):
        self.save([{'region': 'aichi', 'detail_url': 'https://tabelog.com/aichi/A2301/A230101/23000001/'}])
        with patch.object(entry, 'async_playwright', side_effect=AssertionError('browser started')), \
             patch.object(entry.map_mod, 'main', side_effect=AssertionError('map built')):
            full = asyncio.run(entry.run(['aichi', '--dry-run'], csv_path=self.csv))
            append = asyncio.run(entry.run(['--tokyo-osaka-append', '--dry-run'], csv_path=self.csv))
        self.assertEqual(full['mode'], 'full')
        self.assertEqual(append['mode'], 'append')
        self.assertEqual(append['regions'], ['tokyo', 'osaka'])
        self.assertFalse((self.root / 'tabelog.csv.bimonth.lock').exists())

    def test_new_corpus_requires_explicit_region_and_dry_run_stays_read_only(self):
        with patch.object(entry, 'async_playwright', side_effect=AssertionError('browser started')):
            with self.assertRaises(SystemExit):
                asyncio.run(entry.run(['--dry-run'], csv_path=self.csv))
            result = asyncio.run(entry.run(['aichi', '--dry-run'], csv_path=self.csv))
        self.assertEqual(result['regions'], ['aichi'])
        self.assertEqual(result['existing'], {'aichi': 0})
        self.assertFalse(self.csv.exists())

    def test_omitted_regions_expand_from_existing_csv(self):
        self.save([{'region': 'tokyo'}, {'region': 'aichi'}, {'region': 'tokyo'}])
        with patch.object(entry, 'async_playwright', side_effect=AssertionError('browser started')):
            result = asyncio.run(entry.run(['--bimonth-update', '--dry-run'], csv_path=self.csv))
        self.assertEqual(result['regions'], ['aichi', 'tokyo'])
        self.assertEqual(result['existing'], {'aichi': 1, 'tokyo': 2})

    def test_full_requires_region_or_explicit_all_regions(self):
        self.save([{'region': 'tokyo'}, {'region': 'aichi'}])
        with self.assertRaises(SystemExit):
            entry.parse_args(['--dry-run'])
        with patch.object(entry, 'async_playwright', side_effect=AssertionError('browser started')):
            result = asyncio.run(entry.run(['--all-regions', '--dry-run'], csv_path=self.csv))
        self.assertEqual(result['mode'], 'full')
        self.assertEqual(result['regions'], ['aichi', 'tokyo'])

    def test_all_selection_flags_reach_the_one_parser(self):
        args = entry.parse_args([
            '--bimonth-update', 'aichi', '--top-pct', '2.5', '--hard-cap', '700',
            '--fine-dine-pct', '0.2', '--main-meal-ratio', '0.01',
            '--main-meal-cap', '123', '--no-special-lists', '--no-translate',
            '--tokyo-list-url', entry.SPECIAL_LIST_URLS['tokyo'],
            '--tokyo-start-page', '11', '--osaka-start-page', '9', '--no-build',
        ])
        self.assertEqual(args.mode, 'bimonth')
        self.assertEqual(args.regions, ['aichi'])
        self.assertEqual((args.top_pct, args.hard_cap, args.fine_dine_pct), (2.5, 700, 0.2))
        self.assertEqual((args.main_meal_ratio, args.main_meal_cap), (0.01, 123))
        self.assertEqual((args.tokyo_start_page, args.osaka_start_page), (11, 9))
        self.assertFalse(args.special_lists)
        self.assertFalse(args.translate)
        self.assertTrue(args.no_build)

    def test_conflicting_modes_and_invalid_flags_fail_before_dispatch(self):
        for flags in (['--bimonth-update', '--tokyo-osaka-append'],
                      ['--main-meal-cap', '-1'], ['--top-pct', 'nan'],
                      ['--tokyo-osaka-append', 'aichi'],
                      ['--all-regions', 'aichi'],
                      ['--tokyo-osaka-append', '--all-regions'],
                      ['--tokyo-district', '--all-regions']):
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                entry.parse_args(flags)

    def test_full_and_bimonth_use_the_same_selection_and_different_detail_policy(self):
        self.save([{'region': 'aichi', 'detail_url': 'https://tabelog.com/aichi/A2301/A230101/23000001/'}])
        selection = {'rows': [], 'status': 'quota_reached'}
        report = {'region': 'aichi', 'incomplete': False, 'departures': [],
                  'selection': {'status': 'quota_reached'}, 'counts': {},
                  'pending': [], 'commit': 'complete'}
        with patch.object(entry, 'async_playwright', return_value=Playwright()), \
             patch.object(scrape_all, 'Session', Session), \
             patch.object(entry, 'collect_region', new=AsyncMock(return_value=selection)) as collect, \
             patch.object(region_update, 'apply_region', new=AsyncMock(return_value=report)) as apply:
            full = asyncio.run(entry.run_pipeline(entry.parse_args(['aichi']), csv_path=self.csv,
                                                  archive_path=self.archive, run_dir=self.run_dir))
            bimonth = asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update', 'aichi']),
                                                     csv_path=self.csv, archive_path=self.archive,
                                                     run_dir=self.run_dir))
        self.assertTrue(full['ok'] and bimonth['ok'])
        self.assertEqual(collect.await_count, 2)
        self.assertEqual(collect.await_args_list[0].args[1:], collect.await_args_list[1].args[1:])
        self.assertEqual(collect.await_args_list[0].kwargs, collect.await_args_list[1].kwargs)
        self.assertIs(apply.await_args_list[0].kwargs['selection'], selection)
        self.assertIs(apply.await_args_list[1].kwargs['selection'], selection)
        self.assertTrue(apply.await_args_list[0].kwargs['full_details'])
        self.assertFalse(apply.await_args_list[1].kwargs['full_details'])
        self.assertFalse(apply.await_args_list[0].kwargs['append_only'])
        self.assertFalse(apply.await_args_list[1].kwargs['append_only'])

    def test_page_limit_warning_survives_the_run_summary(self):
        self.save([{'region': 'aichi'}])
        warning = {'code':'page_limit','region':'aichi','last_page':60,'last_score':3.51,
                   'message':'page 60 reached'}
        report = {'incomplete':False, 'departures':[], 'selection':{'warnings':[warning]},
                  'counts':{}, 'pending':[], 'commit':'complete'}
        with patch.object(entry, 'async_playwright', return_value=Playwright()), \
             patch.object(scrape_all, 'Session', Session), \
             patch.object(entry, 'collect_region', AsyncMock(return_value={'rows':[]})), \
             patch.object(region_update, 'apply_region', AsyncMock(return_value=report)):
            result = asyncio.run(entry.run_pipeline(entry.parse_args(['--bimonth-update','aichi']),
                csv_path=self.csv, archive_path=self.archive, run_dir=self.run_dir))
        self.assertEqual(result['warnings'], [warning])
        persisted = json.loads((Path(result['run_dir'])/'run.json').read_text())
        self.assertEqual(persisted['warnings'], [warning])

    def test_append_uses_shared_loop_with_append_only_commit(self):
        self.save([{'region': 'tokyo'}, {'region': 'osaka'}])
        selection = {'rows': [], 'status': 'page_limit'}
        report = {'incomplete': False, 'departures': [], 'selection': {},
                  'counts': {}, 'pending': [], 'commit': 'complete'}
        with patch.object(entry, 'async_playwright', return_value=Playwright()), \
             patch.object(scrape_all, 'Session', Session), \
             patch.object(entry, 'collect_region', new=AsyncMock(return_value=selection)) as collect, \
             patch.object(region_update, 'apply_region', new=AsyncMock(return_value=report)) as apply:
            result = asyncio.run(entry.run_pipeline(entry.parse_args(['--tokyo-osaka-append']),
                                                    csv_path=self.csv, archive_path=self.archive,
                                                    run_dir=self.run_dir))
        self.assertEqual(result['regions'], ['tokyo', 'osaka'])
        self.assertEqual(collect.await_count, 2)
        self.assertTrue(all(call.kwargs['append_only'] for call in collect.await_args_list))
        self.assertTrue(all(call.kwargs['append_only'] for call in apply.await_args_list))
        self.assertTrue(all(not call.kwargs['full_details'] for call in apply.await_args_list))

    def test_failed_or_dry_run_never_builds_but_successful_full_does(self):
        with patch.object(entry, 'run_pipeline', new=AsyncMock(return_value={'ok': False})), \
             patch.object(entry.map_mod, 'main') as build:
            result = asyncio.run(entry.run(['aichi']))
            self.assertFalse(result['ok'])
            build.assert_not_called()
        with patch.object(entry, 'run_pipeline', new=AsyncMock(return_value={'ok': True})), \
             patch.object(entry.map_mod, 'main') as build:
            asyncio.run(entry.run(['aichi']))
            build.assert_called_once_with([])
        with patch.object(entry, 'run_pipeline', new=AsyncMock(return_value={'ok': True})), \
             patch.object(entry.map_mod, 'main') as build:
            asyncio.run(entry.run(['aichi', '--no-build']))
            build.assert_not_called()

    def test_bimonth_shim_argv_delegates_without_duplicating_selection(self):
        project = shim.project_main()
        with patch.object(project, 'run', new=AsyncMock(return_value={'ok': True})) as call:
            asyncio.run(shim.run_bimonth_update(['aichi'], csv_path=self.csv, dry_run=True,
                                                top_pct=2.0, main_meal_cap=77))
            asyncio.run(shim.main(['aichi', '--dry-run']))
        argv, kwargs = call.await_args_list[0].args[0], call.await_args_list[0].kwargs
        self.assertEqual(argv[:2], ['--bimonth-update', '--no-build'])
        self.assertIn('--dry-run', argv)
        self.assertEqual(argv[argv.index('--top-pct') + 1], '2.0')
        self.assertEqual(argv[argv.index('--main-meal-cap') + 1], '77')
        self.assertEqual(kwargs['csv_path'], self.csv)
        self.assertEqual(call.await_args_list[1].args[0], ['--bimonth-update', '--no-build', 'aichi', '--dry-run'])

    def test_shim_inherits_main_boolean_defaults_and_forwards_explicit_values(self):
        project = shim.project_main()
        with patch.object(project, 'run', new=AsyncMock(return_value={'ok': True})) as call:
            asyncio.run(shim.run_bimonth_update('aichi', csv_path=self.csv))
            asyncio.run(shim.run_bimonth_update('aichi', csv_path=self.csv,
                                                translate=True, special_lists=True))
            asyncio.run(shim.run_bimonth_update('aichi', csv_path=self.csv,
                                                translate=False, special_lists=False))
        default, enabled, disabled = [record.args[0] for record in call.await_args_list]
        self.assertFalse({'--translate','--no-translate','--special-lists','--no-special-lists'} & set(default))
        self.assertIn('--translate', enabled)
        self.assertIn('--special-lists', enabled)
        self.assertIn('--no-translate', disabled)
        self.assertIn('--no-special-lists', disabled)
        self.assertEqual(entry.parse_args(default).translate, entry.DEFAULTS.get('translate', True))
        self.assertTrue(entry.parse_args(enabled).special_lists)
        self.assertFalse(entry.parse_args(disabled).special_lists)

    def test_browser_key_cannot_close_script(self):
        with patch.dict('os.environ', {'GOOGLE_PLACES_UI_API_KEY': 'x</script>y'}):
            text = entry.map_mod.places_ui_config_json()
        self.assertNotIn('<', text)
        self.assertEqual(json.loads(text)['apiKey'], 'x</script>y')


if __name__ == '__main__':
    unittest.main()
