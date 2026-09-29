"""Canonical restaurant pipeline: one region loop, one set of selection rules.

Every normal/full or bimonth run uses the same stages:
  1. Base rating list: round(N * 1%), capped at 500; fine dining (either meal
     >= JPY 20,000) gets max(round(N * 0.1%), 5) base-selection slots.
  2. Tokyo/Osaka filtered lists: pages 10/8, score >=3.50, at most page 60.
  3. Main-meal coverage: retained + new main meals >= ceil(N * 0.008), with
     300 new IDs in the count-based stage. If the regional minimum remains
     above 3.50, continue qualifying main meals through the whole 3.50 band
     or p60, beyond that count cap. Every page-limit stop prints a warning.
     A region cut short by p60 still commits as complete and is listed in
     page_limited_regions; only real scan failures leave a region incomplete.
     Old-score coverage may continue down to 3.40 without extending new entries.
  4. Details and atomic commit: full mode visits all old/new candidates;
     bimonth skips old details when lists already gave a usable score.
     Confirmed <3.40 or closed restaurants leave the active corpus and are
     archived. Missing/contradictory evidence never proves closure.

Main meals use MEAL_GROUPS['正餐']: sushi/seafood, yakiniku, yakitori, tempura,
Japanese curry, rice bowls, Japanese/regional food, delicacies, teppanyaki,
and other. Udon, soba, ramen and the foreign-cuisine groups are excluded from
supplementation, but may still enter via the base list. Supplement prices
use dinner first, lunch as fallback: <=JPY 3,000 or either >=JPY 20,000 is
excluded; unknown prices retain the existing permissive behavior.

  uv run python main.py aichi --dry-run
  uv run python main.py aichi --no-build
  uv run python main.py --bimonth-update --no-build

--tokyo-osaka-append remains an append-only shortcut; ordinary main runs
already include those lists. SCRAPING-RULES.md is the maintained rulebook.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))

from playwright.async_api import async_playwright
from tabelog.paths import OUTPUT_DIR, TABELOG_CSV, TABELOG_AREAS_TOKYO_JSON, atomic_write_json
from tabelog.scrape import map as map_mod
from tabelog.scrape import scrape_all, scrape_topup, region_update, bimonth_resume
from tabelog.scrape.map_data import MEAL_GROUPS
from tabelog.scrape.region_selection import RegionSelection

# CLI defaults live here; the lower-level modules implement individual stages.
DEFAULTS = dict(top_pct=1.0, hard_cap=500, fine_dine_pct=0.1,
                main_meal_ratio=0.008, main_meal_cap=300)
MAIN_MEAL_CATEGORIES = tuple(MEAL_GROUPS['正餐'])
SPECIAL_LIST_URLS = {'tokyo': scrape_topup.TOKYO_APPEND_LIST_URL,
                     'osaka': scrape_topup.OSAKA_APPEND_LIST_URL}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('regions', nargs='*', help='region slugs; bimonth without regions updates every existing region')
    parser.add_argument('--all-regions', action='store_true', help='explicitly process all regions already in the CSV')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--bimonth-update', action='store_const', const='bimonth', dest='mode',
                       help='same selection, but skip old details when a list score is available')
    modes.add_argument('--tokyo-osaka-append', action='store_const', const='append', dest='mode',
                       help='compatibility shortcut: only add new Tokyo/Osaka filtered-list restaurants')
    modes.add_argument('--tokyo-district', action='store_const', const='district', dest='mode',
                       help='refresh Tokyo area names only; no restaurants or map build')
    parser.set_defaults(mode='full')
    parser.add_argument('--top-pct', type=float, default=DEFAULTS['top_pct'], help='base share in percent (default: 1)')
    parser.add_argument('--hard-cap', type=int, default=DEFAULTS['hard_cap'], help='base-selection cap per region (default: 500)')
    parser.add_argument('--fine-dine-pct', type=float, default=DEFAULTS['fine_dine_pct'], help='fine-dining share in percent (default: 0.1, minimum 5 slots)')
    parser.add_argument('--main-meal-ratio', type=float, default=DEFAULTS['main_meal_ratio'], help='main-meal share as a fraction (default: 0.008 = 0.8%%)')
    parser.add_argument('--main-meal-cap', type=int, default=DEFAULTS['main_meal_cap'], help='count-stage new-ID cap (default: 300); 3.50 depth extension may exceed it; 0 disables ordinary supplementation')
    parser.add_argument('--special-lists', action=argparse.BooleanOptionalAction, default=True,
                        help='include Tokyo/Osaka filtered lists in full/bimonth mode (default: on)')
    for region in ('tokyo', 'osaka'):
        parser.add_argument(f'--{region}-list-url', help=f'override the {region} filtered ranking URL')
        parser.add_argument(f'--{region}-start-page', type=int, help=f'filtered-list start page; default from URL ({10 if region == "tokyo" else 8})')
    parser.add_argument('--translate', action=argparse.BooleanOptionalAction, default=True,
                        help='translate fetched reservation policies into Chinese (default: on)')
    parser.add_argument('--resume', action='store_true', help='continue the latest interrupted bimonth run')
    parser.add_argument('--dry-run', action='store_true', help='local plan only: no browser, writes, translation or build')
    parser.add_argument('--no-build', action='store_true', help='save restaurant updates without geocoding/building the map')
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    args._specified = _specified_options(parser, argv)
    if args.resume and args.mode != 'bimonth':
        parser.error('--resume requires --bimonth-update')
    if (not all(math.isfinite(v) for v in (args.top_pct, args.fine_dine_pct, args.main_meal_ratio))
            or args.top_pct <= 0 or args.hard_cap <= 0 or args.fine_dine_pct < 0
            or args.main_meal_ratio < 0 or args.main_meal_cap < 0):
        parser.error('selection percentages must be finite and quotas nonnegative; top-pct/hard-cap must be positive')
    args.regions = list(dict.fromkeys(region.strip().lower() for region in args.regions))
    if any(not re.fullmatch(r'[a-z][a-z0-9_-]*', region) for region in args.regions):
        parser.error('regions must be Tabelog slugs such as aichi, tokyo or osaka')
    if args.regions and args.all_regions:
        parser.error('choose positional regions or --all-regions, not both')
    if args.mode in ('append', 'district') and (args.regions or args.all_regions):
        parser.error('omit regions/--all-regions with --tokyo-osaka-append or --tokyo-district')
    if args.mode == 'full' and not args.regions and not args.all_regions:
        parser.error('specify a region, --all-regions, or --bimonth-update')
    if args.mode == 'append' and not args.special_lists:
        parser.error('--tokyo-osaka-append requires the special lists')
    for region in ('tokyo', 'osaka'):
        url = getattr(args, f'{region}_list_url') or SPECIAL_LIST_URLS[region]
        try:
            scrape_topup.append_start_page(url, region, getattr(args, f'{region}_start_page'))
        except ValueError as exc:
            parser.error(str(exc))
    return args


async def collect_region(session, region, top_pct=DEFAULTS["top_pct"],
                         hard_cap=DEFAULTS["hard_cap"], fine_dine_pct=DEFAULTS["fine_dine_pct"],
                         *, existing_rows=(), main_meal_ratio=DEFAULTS["main_meal_ratio"],
                         main_meal_cap=DEFAULTS["main_meal_cap"],
                         special_lists=True, special_urls=None, special_start_pages=None,
                         append_only=False):
    """The only orchestration of restaurant selection, used by every run mode."""
    selection = RegionSelection(
        session, region, top_pct, hard_cap, fine_dine_pct, existing_rows=existing_rows,
        main_meal_ratio=main_meal_ratio, main_meal_cap=main_meal_cap,
        special_lists=special_lists, special_urls=special_urls, special_start_pages=special_start_pages,
    )
    if append_only:
        await selection.collect_special()
        return selection.result(append_only=True)

    await selection.collect_base()
    # Include the two special lists before computing the ordinary deficit, so
    # their new restaurants do not consume the ordinary 300-new-ID budget.
    await selection.collect_special()
    await selection.complete_main_meals_and_coverage()
    return selection.result()


def _specified_options(parser: argparse.ArgumentParser, argv: list[str]) -> set[str]:
    return {parser._option_string_actions[token.split('=', 1)[0]].dest
            for token in argv if token.startswith('--')
            and token.split('=', 1)[0] in parser._option_string_actions}


def _record_report(result: dict, region: str, report: dict) -> None:
    result['reports'].append({key: value for key, value in report.items() if key != 'departures'})
    result['warnings'].extend(report.get('selection', {}).get('warnings', []))
    # Page 60 stopped the list before its quotas filled: committed and
    # complete, but the remaining main-meal deficit is worth seeing at the top.
    if (report.get('selection') or {}).get('status') == 'truncated':
        result['page_limited_regions'].append(region)


def _resume_plan(directory: Path, regions: list[str], corpus: list[dict]) -> dict:
    completed, remaining, recovery = [], [], []
    for region in regions:
        report = bimonth_resume.read_report(directory / f'{region}.json')
        if report and report.get('region_after_sha256'):
            if not bimonth_resume.region_matches(corpus, region, report['region_after_sha256']):
                raise ValueError(f'{region}: active CSV differs from this run\'s committed region; refusing resume')
        if bimonth_resume.complete_report(report):
            completed.append(region)
        else:
            remaining.append(region)
            if report and report.get('commit') == 'prepared':
                recovery.append({'region': region, 'state': 'prepared CSV/archive commit'})
            elif report and bimonth_resume.region_incomplete(report):
                recovery.append({'region': region, 'state': 'incomplete selection; rerun region'})
            elif (directory / 'checkpoints' / region).exists():
                available = bimonth_resume.FetchCheckpoints(directory, region).available()
                recovery.append({'region': region, 'state': 'replay completed pages and details',
                                 'saved': available})
            else:
                recovery.append({'region': region, 'state': 'start unfinished region'})
    return {'source_run': str(directory), 'completed': completed,
            'remaining': remaining, 'recovery': recovery}


def _validate_resume_args(args, manifest: dict | None, regions: list[str],
                          csv_path: Path, archive_path: Path, directory: Path) -> tuple[dict, dict]:
    specified = getattr(args, '_specified', set())
    if args.regions and args.regions != regions:
        raise ValueError(f'resume regions differ from interrupted run: {regions}')
    if manifest is None or manifest.get('legacy_adoption'):
        choices = specified & (set(DEFAULTS) | {'translate', 'special_lists',
                          'tokyo_list_url', 'osaka_list_url', 'tokyo_start_page', 'osaka_start_page'})
        if choices:
            raise ValueError('legacy run has no saved parameters; omit selection overrides when resuming')
        if manifest is not None:
            if Path(manifest['csv_path']) != csv_path.resolve() or Path(manifest['archive_path']) != archive_path.resolve():
                raise ValueError('resume CSV/archive paths differ from interrupted run')
            return manifest['parameters'], manifest['special_lists']
        parameters = dict(DEFAULTS, translate=True, special_lists=True, no_build=True)
        run_date = datetime.strptime(directory.name[:15], '%Y%m%dT%H%M%S').replace(
            tzinfo=timezone.utc).astimezone().date()
        special = {region: {'url': scrape_topup.resolve_list_url_date(url, today=run_date),
                            'start_page': scrape_topup.append_start_page(url, region, None)}
                   for region, url in SPECIAL_LIST_URLS.items() if region in regions}
        return parameters, special
    if Path(manifest['csv_path']) != csv_path.resolve() or Path(manifest['archive_path']) != archive_path.resolve():
        raise ValueError('resume CSV/archive paths differ from interrupted run')
    parameters = manifest['parameters']
    for key in set(DEFAULTS) | {'translate', 'special_lists'}:
        if key in specified and getattr(args, key) != parameters[key]:
            raise ValueError(f'resume --{key.replace("_", "-")} differs from interrupted run ({parameters[key]})')
    special = manifest['special_lists']
    for region in ('tokyo', 'osaka'):
        if region not in special:
            if f'{region}_list_url' in specified or f'{region}_start_page' in specified:
                raise ValueError(f'{region} special list was not part of interrupted run')
            continue
        if f'{region}_list_url' in specified:
            saved_date = re.search(r'(?:[?&])svd=(\d{8})(?:&|$)', special[region]['url'])
            reference_date = (datetime.strptime(saved_date.group(1), '%Y%m%d').date()
                              if saved_date else datetime.now().date())
            url = scrape_topup.resolve_list_url_date(getattr(args, f'{region}_list_url'),
                                                     today=reference_date)
            if url != special[region]['url']:
                raise ValueError(f'{region} list URL differs from interrupted run')
        if f'{region}_start_page' in specified:
            if getattr(args, f'{region}_start_page') != special[region]['start_page']:
                raise ValueError(f'{region} list start page differs from interrupted run')
    return parameters, special


async def run_pipeline(args, *, csv_path=TABELOG_CSV, archive_path=None, run_dir=None):
    """One browser and one locked CSV for the requested regions."""
    csv_path = Path(csv_path)
    if args.mode == 'district':
        result = {'ok': True, 'mode': 'district', 'dry_run': args.dry_run, 'regions': ['tokyo']}
        if not args.dry_run:
            await scrape_all.scrape_area_names('tokyo', TABELOG_AREAS_TOKYO_JSON)
        return result

    _, corpus = region_update.read_corpus(csv_path)
    corpus_regions = sorted({(row.get('region') or '').lower() for row in corpus} - {''})
    regions = (['tokyo', 'osaka'] if args.mode == 'append' else args.regions or corpus_regions)
    if not regions and not args.resume:
        raise ValueError('no existing regions: specify a region to initialize the corpus')
    archive_path = Path(archive_path) if archive_path else csv_path.with_name('bimonth_departures.jsonl')
    run_root = Path(run_dir) if run_dir else OUTPUT_DIR / ('bimonth_runs' if args.mode == 'bimonth' else 'pipeline_runs')
    directory = None
    manifest = None
    if args.resume:
        directory, data = bimonth_resume.choose_run(run_root, corpus_regions)
        regions = list(data['regions']) if data else corpus_regions
        if args.all_regions and sorted(regions) != corpus_regions:
            raise ValueError('resume --all-regions differs from interrupted run')
        if not regions:
            raise ValueError('interrupted run has no known regions')
        parameters, special = _validate_resume_args(args, data, regions, csv_path, archive_path, directory)
        manifest = bimonth_resume.RunManifest(directory, data) if data else None
    else:
        parameters = {key: getattr(args, key) for key in DEFAULTS}
        parameters.update(translate=args.translate, special_lists=args.special_lists, no_build=args.no_build)
        special = {region: {'url': scrape_topup.resolve_list_url_date(getattr(args, f'{region}_list_url') or url),
                            'start_page': scrape_topup.append_start_page(getattr(args, f'{region}_list_url') or url,
                                                                          region, getattr(args, f'{region}_start_page'))}
                   for region, url in SPECIAL_LIST_URLS.items() if region in regions and args.special_lists}
    result = {'ok': True, 'mode': args.mode, 'dry_run': args.dry_run, 'regions': regions,
              'existing': {region: sum((row.get('region') or '').lower() == region for row in corpus) for region in regions},
              'parameters': parameters, 'main_meal_categories': list(MAIN_MEAL_CATEGORIES),
              'special_lists': special, 'reports': [], 'errors': [], 'incomplete_regions': [],
              'page_limited_regions': [], 'warnings': []}
    if args.resume:
        plan = _resume_plan(directory, regions, corpus)
        result['resume'] = plan
        result['checkpoint_reuse'] = {}
        if data is None or data.get('legacy_adoption'):
            result['resume']['legacy_special_date_inferred'] = True
            result['resume']['legacy_assumptions'] = (data or {}).get('legacy_adoption')
        result.update(run_id=directory.name, run_dir=str(directory), archive_path=str(archive_path))
        for region in plan['completed']:
            _record_report(result, region, bimonth_resume.read_report(directory / f'{region}.json'))
    if args.dry_run:
        return result

    urls = {region: special[region]['url'] if region in special else url
            for region, url in SPECIAL_LIST_URLS.items()}
    starts = {region: special[region]['start_page'] if region in special else None
              for region in SPECIAL_LIST_URLS}
    with region_update.corpus_lock(csv_path):
        if not args.resume:
            run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
            directory = run_root / run_id
            directory.mkdir(parents=True, exist_ok=False)
            result.update(run_id=run_id, run_dir=str(directory), archive_path=str(archive_path))
            if args.mode == 'bimonth':
                manifest = bimonth_resume.RunManifest.new(
                    directory, regions=regions, parameters=parameters, special_lists=special,
                    csv_path=csv_path, archive_path=archive_path)
                atomic_write_json(directory / 'run.json', result, indent=2)
        elif manifest is None:
            _, locked_rows = region_update.read_corpus(csv_path)
            locked_plan = _resume_plan(directory, regions, locked_rows)
            if (locked_plan['completed'] != result['resume']['completed']
                    or locked_plan['remaining'] != result['resume']['remaining']):
                raise ValueError('legacy run changed before the corpus lock was acquired; retry resume')
            manifest = bimonth_resume.RunManifest.adopt_legacy(
                directory, regions=regions, completed=locked_plan['completed'],
                parameters=parameters, special_lists=special,
                csv_path=csv_path, archive_path=archive_path)
            result['resume']['legacy_assumptions'] = manifest.data['legacy_adoption']
            result['resume']['legacy_adopted'] = True
            atomic_write_json(directory / 'run.json', result, indent=2)
        if args.resume and manifest:
            # Regions a report written under the old page-limit rule left out.
            manifest.mark_completed(result['resume']['completed'])
        run_id = directory.name
        remaining = list(result['resume']['remaining']) if args.resume else regions
        # A prepared report is the commit journal. Resolve it before any new
        # browser work or a new region can change the CSV hash.
        for region in list(remaining):
            path = directory / f'{region}.json'
            prepared = bimonth_resume.read_report(path)
            if prepared and prepared.get('commit') == 'prepared':
                recovered = region_update.recover_prepared(path, csv_path, archive_path)
                if recovered == 'committed':
                    report = bimonth_resume.read_report(path)
                    if manifest:
                        manifest.end(region, report)
                    if not bimonth_resume.region_incomplete(report):
                        remaining.remove(region)
                        _record_report(result, region, report)
        if remaining:
            async with async_playwright() as playwright:
                session = scrape_all.Session(playwright)
                await session.connect()
                for region in remaining:
                    try:
                        _, current_rows = region_update.read_corpus(csv_path)
                        before = bimonth_resume.region_fingerprint(current_rows, region)
                        if manifest:
                            expected = manifest.data.get('region_snapshots', {}).get(region)
                            if expected and not bimonth_resume.region_matches(current_rows, region, expected):
                                raise ValueError('active region changed during interruption; refusing cached replay')
                        if manifest:
                            manifest.begin(region, before)
                            session.resume_checkpoints = bimonth_resume.FetchCheckpoints(directory, region)
                            if args.resume:
                                available = session.resume_checkpoints.available()
                                print(f'[{region}] saved checkpoints: {available["list_pages"]} list pages, '
                                      f'{available["details"]} details; replaying them before live requests')
                        selection = await collect_region(
                            session, region, parameters['top_pct'], parameters['hard_cap'], parameters['fine_dine_pct'],
                            existing_rows=current_rows, main_meal_ratio=parameters['main_meal_ratio'],
                            main_meal_cap=parameters['main_meal_cap'], special_lists=parameters['special_lists'],
                            special_urls=urls, special_start_pages=starts, append_only=args.mode == 'append',
                        )
                        report = await region_update.apply_region(
                            session, region, csv_path, archive_path, directory / f'{region}.json', run_id,
                            selection=selection, translate=parameters['translate'], full_details=args.mode == 'full',
                            append_only=args.mode == 'append',
                        )
                        _record_report(result, region, report)
                        if args.resume and manifest:
                            result['checkpoint_reuse'][region] = session.resume_checkpoints.reused()
                        if bimonth_resume.region_incomplete(report):
                            result['ok'] = False
                            result['incomplete_regions'].append(region)
                        if manifest:
                            manifest.end(region, report)
                    except Exception as exc:
                        result['ok'] = False
                        result['errors'].append({'region': region, 'error': str(exc)})
                        if manifest:
                            manifest.data['errors'] = result['errors']
                            manifest.save()
                        print(f'[{region}] ERROR: {exc}')
                        if args.resume or (directory / f'{region}.json').exists():
                            break
                    finally:
                        atomic_write_json(directory / 'run.json', result, indent=2)
        if manifest:
            manifest.finish(result['ok'], result['errors'])
        atomic_write_json(directory / 'run.json', result, indent=2)
    return result


async def run(argv=None, **paths):
    args = parse_args(argv)
    result = await run_pipeline(args, **paths)
    display = (result if args.dry_run and not args.resume
               else {k: v for k, v in result.items() if k != 'reports'})
    print(json.dumps(display, ensure_ascii=False, indent=2))
    saved_no_build = result.get('parameters', {}).get('no_build', False)
    if result['ok'] and not args.dry_run and not args.no_build and not saved_no_build and args.mode != 'district':
        print('\nRestaurant updates complete. Geocoding new rows and rebuilding map ...\n')
        map_mod.main([])
    return result


async def _run(argv):
    """Compatibility for callers of the old entry helper."""
    result = await run(argv)
    if not result['ok']:
        raise SystemExit('Update incomplete; inspect its run report before building the map')
    return result


def main():
    try:
        asyncio.run(_run(sys.argv[1:]))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == '__main__':
    main()
