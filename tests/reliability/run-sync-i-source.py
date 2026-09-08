"""Run scenario I against source overlays without regenerating formal docs."""
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/sync'))
import mc_lib as L
import scen_i_misc

html = (ROOT / 'docs/index.html').read_text()
source = (ROOT / 'src/tabelog/scrape/map.py').read_text()
for start, end in [
    ('  function fetchAuthed(', '  // Bounded dependency wait.'),
    ('    var pushInFlight = false,', '    // Flash the filter FAB'),
    ('    function adoptDiskSyncBase()', '    // True for the single retry'),
    ('    var pullRetriedAfterSilent = false;', '    // M-027 / B4:'),
    ('    function downloadBackup()', "    impModal.querySelector('.imp-confirm')"),
    ('    function fallbackSilentGIS(cb)', '    // Boot-time restore'),
]:
    a, c = html.index(start), source.index(start)
    b, d = html.index(end, a), source.index(end, c)
    html = html[:a] + source[c:d] + html[b:]
L._HTML = html.replace('https://api.jpfoodmap.com/api', L.API)

# Reuse local dependencies without allowing the harness to download any.
cached = Path.home() / '.cache/jpfoodmap-sync-tests/cdn'
L.SCRATCH = Path(os.environ.get('SYNC_TEST_SCRATCH', '/tmp/jpfoodmap-round1-sync'))
L.CDN = L.SCRATCH / 'cdn'
L.CDN.mkdir(parents=True, exist_ok=True)
for name in L.CDN_SRC:
    shutil.copyfile(cached / name, L.CDN / name)
original_context = L.make_context


def offline_context(*args, **kwargs):
    context = original_context(*args, **kwargs)
    site_origin = urlsplit(L.SITE).netloc
    context.route('**/*', lambda route: route.fallback()
                  if urlsplit(route.request.url).netloc == site_origin else route.abort())
    return context


L.make_context = offline_context
cases = scen_i_misc.run()
print(json.dumps({'source_overlay': True, 'formal_docs_changed': False,
                  'passed': sum(c['pass'] for c in cases), 'total': len(cases)}, indent=2))
sys.exit(0 if all(c['pass'] for c in cases) else 1)
