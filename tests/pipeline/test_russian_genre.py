"""Russian restaurants use a distinct foreign-cuisine bucket on the map."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from tabelog.scrape.map import categorize_genre, genre_buckets_all  # noqa: E402
from tabelog.scrape.map_data import DEFAULT_OFF_GENRES, GENRE_EMOJI  # noqa: E402
from tabelog.scrape.audit_main_meal_coverage import is_main_meal  # noqa: E402


class RussianGenreTests(unittest.TestCase):
    def test_plain_and_wrapped_russian_restaurants_are_foreign(self):
        for genre in ('ロシア料理', 'レストラン(ロシア料理)'):
            with self.subTest(genre=genre):
                self.assertEqual(categorize_genre(genre), ['俄罗斯料理'])
                self.assertIn('俄罗斯料理', genre_buckets_all(genre))
                self.assertIn('俄罗斯料理', DEFAULT_OFF_GENRES)
                self.assertFalse(is_main_meal(genre))

    def test_existing_singapore_and_sports_bar_decisions_stay(self):
        self.assertEqual(categorize_genre('シンガポール料理、カレー'), ['日式咖喱'])
        self.assertEqual(categorize_genre('串揚げ、居酒屋、スポーツバー'), ['天妇罗·炸物'])

    def test_russian_flag_is_cached_and_translated_for_map(self):
        self.assertEqual(GENRE_EMOJI['俄罗斯料理'], '🇷🇺')
        manifest = json.loads((ROOT / 'docs/emoji/_manifest.json').read_text(encoding='utf-8'))
        stem = manifest['🇷🇺']
        png = ROOT / 'docs/emoji' / f'{stem}.png'
        self.assertTrue(png.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        labels = json.loads((ROOT / 'src/tabelog/ui/i18n/ui-strings.json').read_text(encoding='utf-8'))
        self.assertEqual(labels['俄罗斯料理'], {'en': 'Russian', 'ja': 'ロシア料理'})


if __name__ == '__main__':
    unittest.main()
