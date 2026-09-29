"""Compatibility entry for main.py's bimonth mode.

Selection, region iteration and CLI policy live in project main.py. This module
only selects score-first detail behavior and skips the optional map build.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))
from tabelog.paths import PROJECT_ROOT, TABELOG_CSV


def project_main():
    name = 'tabelog_project_main'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / 'main.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


async def run_bimonth_update(regions=None, *, csv_path=TABELOG_CSV, archive_path=None,
                             run_dir=None, dry_run=False, resume=False, translate=None, special_lists=None,
                             top_pct=None, hard_cap=None, fine_dine_pct=None,
                             main_meal_ratio=None, main_meal_cap=None):
    argv = ['--bimonth-update', '--no-build']
    argv += [regions] if isinstance(regions, str) else list(regions or [])
    for key, value in dict(top_pct=top_pct, hard_cap=hard_cap, fine_dine_pct=fine_dine_pct,
                           main_meal_ratio=main_meal_ratio, main_meal_cap=main_meal_cap).items():
        if value is not None:
            argv += ['--' + key.replace('_', '-'), str(value)]
    if dry_run:
        argv.append('--dry-run')
    if resume:
        argv.append('--resume')
    for flag, value in (('translate', translate), ('special-lists', special_lists)):
        if value is not None:
            argv.append('--' + ('' if value else 'no-') + flag)
    return await project_main().run(argv, csv_path=csv_path, archive_path=archive_path, run_dir=run_dir)


async def main(argv=None):
    return await project_main().run(['--bimonth-update', '--no-build'] + list(sys.argv[1:] if argv is None else argv))


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main())['ok'] else 1)
