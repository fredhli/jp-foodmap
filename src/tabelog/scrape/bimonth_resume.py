"""Durable bimonth run state and successful fetch checkpoints."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tabelog.paths import atomic_write_json
from tabelog.scrape import scrape_all
from tabelog.scrape.region_selection import identity

MANIFEST = 'manifest.json'
# map.py geocodes rows and writes lat/lon back into the active CSV. Building
# the map between an interruption and --resume must not look like another
# update touched the corpus, so fingerprints leave these columns out.
BUILD_COLUMNS = ('lat', 'lon')
# Marks fingerprints without BUILD_COLUMNS. Bare hex digests in older reports
# and manifests covered every column.
CONTENT_PREFIX = 'content:'


def digest_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _region_rows(rows: list[dict], region: str) -> list[dict]:
    return [row for row in rows if (row.get('region') or '').lower() == region]


def _without_build_columns(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in BUILD_COLUMNS}


def region_fingerprint(rows: list[dict], region: str) -> str:
    return CONTENT_PREFIX + _sha256(_dump([_without_build_columns(row) for row in _region_rows(rows, region)]))


def corpus_fingerprint(fields: list[str], rows: list[dict]) -> str:
    return CONTENT_PREFIX + _sha256(_dump({
        'fields': [field for field in fields if field not in BUILD_COLUMNS],
        'rows': [_without_build_columns(row) for row in rows],
    }))


def region_matches(rows: list[dict], region: str, recorded: str) -> bool:
    """True when the region still holds the recorded scrape data."""
    if recorded.startswith(CONTENT_PREFIX):
        return region_fingerprint(rows, region) == recorded
    return _legacy_region_matches(_region_rows(rows, region), recorded)


def _legacy_region_matches(selected: list[dict], recorded: str) -> bool:
    # The old digest hashed whole rows, coordinates included. A map build
    # after the commit only fills coordinates on rows that had none, and those
    # are the most recently scraped rows. Blank lat/lon from each scrape time
    # onward and compare again: an exact sha256 match proves nothing but those
    # coordinates changed.
    as_is = [_dump(row) for row in selected]
    blanked = [_dump(dict(row, **{column: '' for column in BUILD_COLUMNS if column in row}))
               for row in selected]

    def digest(parts: list[str]) -> str:
        return _sha256('[' + ','.join(parts) + ']')

    if digest(as_is) == recorded:
        return True
    stamps = sorted({row.get('scraped_at') or '' for row in selected} - {''}, reverse=True)
    for stamp in stamps:
        parts = [blank if (row.get('scraped_at') or '') >= stamp else original
                 for row, original, blank in zip(selected, as_is, blanked)]
        if digest(parts) == recorded:
            return True
    return False


def read_report(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def region_incomplete(report: dict) -> bool:
    """Whether a region report needs another run.

    Reaching page 60 before the quotas fill ('truncated') is Tabelog's
    pagination limit, not a failed scan. Reports written before that rule
    carried incomplete=True for it.
    """
    if (report.get('selection') or {}).get('status') == 'truncated':
        return False
    return bool(report.get('incomplete'))


def complete_report(report: dict | None) -> bool:
    return bool(report and report.get('commit') == 'complete' and not region_incomplete(report))


def choose_run(root: Path, regions: list[str]) -> tuple[Path, dict | None]:
    if not root.exists():
        raise ValueError(f'no bimonth runs found in {root}')
    candidates = []
    for directory in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        manifest = read_report(directory / MANIFEST)
        if manifest is not None:
            if manifest.get('version') != 1 or manifest.get('mode') != 'bimonth':
                if not candidates:
                    raise ValueError(f'latest bimonth run {directory.name} has an unsupported manifest; refusing an older run')
                continue
            if manifest.get('finished'):
                if not candidates:
                    raise ValueError(f'latest bimonth run {directory.name} completed; refusing to resume an older run')
                continue
            candidates.append((directory, manifest))
            continue
        summary = read_report(directory / 'run.json')
        if summary and summary.get('ok'):
            if summary.get('mode') == 'bimonth' and not candidates:
                raise ValueError(f'latest bimonth run {directory.name} completed; refusing to resume an older run')
            continue
        reports = list(directory.glob('*.json'))
        reports = [p for p in reports if p.name != 'run.json' and isinstance(read_report(p), dict)]
        if reports and (summary is None or summary.get('mode') == 'bimonth'):
            candidates.append((directory, None))
        elif not candidates:
            raise ValueError(f'latest bimonth run {directory.name} has no usable manifest or region reports; refusing an older run')
    if not candidates:
        raise ValueError(f'no interrupted bimonth run found in {root}')
    directory, manifest = candidates[0]
    if manifest is None and not regions:
        raise ValueError('legacy run has no region manifest and the active CSV has no regions')
    return directory, manifest


class FetchCheckpoints:
    def __init__(self, directory: Path, region: str):
        self.directory = directory / 'checkpoints' / region
        self.region = region
        self.replayed_lists = 0
        self.replayed_details = 0
        self.saved_lists = 0
        self.saved_details = 0

    def available(self) -> dict:
        return {'list_pages': len(list((self.directory / 'lists').glob('*.json'))),
                'details': len(list((self.directory / 'details').glob('*.json')))}

    def reused(self) -> dict:
        return {'list_pages': self.replayed_lists, 'details': self.replayed_details}

    @staticmethod
    def _key(parts: tuple) -> str:
        raw = json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def _path(self, kind: str, parts: tuple) -> Path:
        return self.directory / kind / (self._key(parts) + '.json')

    async def list_page(self, session, region: str, page: int, url_template: str):
        path = self._path('lists', (region, page, url_template))
        if path.exists():
            self.replayed_lists += 1
            print(f'[{region}] replaying saved list page {page}')
            value = read_report(path)
            return value['rows'], value['total']
        return await scrape_all.scrape_list_page(session, region, page, url_template)

    def accept_list_page(self, region: str, page: int, url_template: str, rows: list[dict], total: int | None):
        path = self._path('lists', (region, page, url_template))
        if not path.exists():
            atomic_write_json(path, {'rows': rows, 'total': total})
            self.saved_lists += 1

    async def detail(self, session, url: str):
        key = identity(url, self.region) or url
        path = self._path('details', (key,))
        if path.exists():
            self.replayed_details += 1
            print(f'[{self.region}] replaying saved detail: {url}')
            return read_report(path)
        value = await scrape_all.fetch_detail(session, url)
        # A malformed response should be retried on the next run. Verified
        # closures need no address; an open restaurant needs usable evidence.
        if isinstance(value, dict) and (value.get('operating_status') in ('closed', 'temporarily_closed')
                                        or (value.get('address') and value.get('rating') is not None)):
            atomic_write_json(path, value)
            self.saved_details += 1
        return value


class RunManifest:
    def __init__(self, directory: Path, data: dict):
        self.directory = directory
        self.data = data

    @classmethod
    def new(cls, directory: Path, *, regions: list[str], parameters: dict,
            special_lists: dict, csv_path: Path, archive_path: Path):
        data = {'version': 1, 'mode': 'bimonth', 'run_id': directory.name,
                'regions': regions, 'parameters': parameters,
                'special_lists': special_lists, 'csv_path': str(csv_path.resolve()),
                'archive_path': str(archive_path.resolve()), 'finished': False,
                'completed': [], 'current_region': None, 'region_snapshots': {}, 'errors': []}
        instance = cls(directory, data)
        instance.save()
        return instance

    @classmethod
    def adopt_legacy(cls, directory: Path, *, regions: list[str], completed: list[str],
                     parameters: dict, special_lists: dict, csv_path: Path, archive_path: Path):
        if (directory / MANIFEST).exists():
            raise ValueError(f'{directory}: manifest appeared while adopting legacy run; retry resume')
        data = {'version': 1, 'mode': 'bimonth', 'run_id': directory.name,
                'regions': regions, 'parameters': parameters,
                'special_lists': special_lists, 'csv_path': str(csv_path.resolve()),
                'archive_path': str(archive_path.resolve()), 'finished': False,
                'completed': completed, 'current_region': None,
                'region_snapshots': {}, 'errors': [],
                'legacy_adoption': {
                    'regions': 'inferred from active CSV; original region arguments were not recorded',
                    'selection_parameters': 'assumed CLI defaults; original values were not recorded',
                    'special_list_dates': 'inferred from run start in the local timezone',
                    'no_build': 'assumed true so resuming cannot unexpectedly start a map build',
                }}
        instance = cls(directory, data)
        instance.save()
        return instance

    def save(self):
        atomic_write_json(self.directory / MANIFEST, self.data, indent=2)

    def begin(self, region: str, region_digest: str):
        self.data['current_region'] = region
        self.data.setdefault('region_snapshots', {})[region] = region_digest
        self.save()

    def mark_completed(self, regions: list[str]):
        missing = [region for region in regions if region not in self.data['completed']]
        if missing:
            self.data['completed'].extend(missing)
            self.save()

    def end(self, region: str, report: dict):
        if complete_report(report) and region not in self.data['completed']:
            self.data['completed'].append(region)
        self.data['current_region'] = None
        self.data.setdefault('region_snapshots', {}).pop(region, None)
        self.save()

    def finish(self, ok: bool, errors: list[dict]):
        self.data['finished'] = bool(ok)
        self.data['errors'] = errors
        self.data['current_region'] = None
        self.save()
