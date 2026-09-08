"""Build a disposable UX preview without writing production outputs or data."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'audit_outputs/3.1.0-implementation/implementation/web-ux/preview-build'
OUT.mkdir(parents=True, exist_ok=True)
for source in (ROOT / 'docs').iterdir():
    if source.name in {'index.html', 'sw.js', 'data'}:
        continue
    target = OUT / source.name
    if not target.exists():
        target.symlink_to(source, target_is_directory=source.is_dir())

spec = importlib.util.spec_from_file_location('ux_map_preview', ROOT / 'src/tabelog/scrape/map.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.OUT_HTML = OUT / 'index.html'
module.DOCS_DATA_DIR = OUT / 'data'

def target(path):
    path = Path(path)
    if path.is_relative_to(OUT):
        return path
    if path.is_relative_to(ROOT / 'docs'):
        return OUT / path.relative_to(ROOT / 'docs')
    return OUT / 'side-effects' / path.name

for name in ['atomic_write_text','atomic_write_bytes','atomic_write_json','atomic_write_csv']:
    original = getattr(module, name)
    def redirected(path, *args, _write=original, **kwargs):
        destination = target(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        return _write(destination, *args, **kwargs)
    setattr(module, name, redirected)
sys.argv = [str(ROOT / 'src/tabelog/scrape/map.py')]
module.main()
