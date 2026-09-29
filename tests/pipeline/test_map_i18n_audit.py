"""The UI translation audit ignores landmark data with per-language names."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from tabelog.scrape import map as map_mod  # noqa: E402


class MapI18nAuditTests(unittest.TestCase):
    def test_landmark_names_are_not_ui_translation_gaps(self):
        landmarks = json.loads((ROOT / 'data/favorites_builtin.json').read_text(encoding='utf-8'))
        self.assertTrue(landmarks)
        for name in ('name_sc', 'name_tc', 'name_jp', 'name_en'):
            self.assertTrue(all(item.get(name) for item in landmarks), name)
        missing_copy = '此处新增界面提示'
        html = ('<script>\nvar EMBEDDED_FAVORITES_BUILTIN = '
                + json.dumps(landmarks, ensure_ascii=False)
                + ';\n</script>\n<span>' + missing_copy + '</span>')
        runs = map_mod._scan_cjk_runs(html)
        self.assertEqual(runs, {missing_copy})
        self.assertEqual(map_mod.build_text_en_map(html)[1], [missing_copy])
        self.assertEqual(map_mod.build_text_ja_map(html)[1], [missing_copy])


if __name__ == '__main__':
    unittest.main()
