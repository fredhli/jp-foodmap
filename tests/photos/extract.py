"""Keep real photo URLs and identity deduplication when scraping new rows."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from tabelog.scrape.scrape_all import extract_photo_urls
carousel = 'https://tblg.k-img.com/resize/660x370c/restaurant/images/Rvw/123/abc.jpg?token=one&amp;x=2'
thumb = 'https://tblg.k-img.com/restaurant/images/Rvw/123/320x320_rect_def.jpg'
same = 'https://tblg.k-img.com/restaurant/images/Rvw/123/320x320_rect_abc.jpg'
assert extract_photo_urls(f'<img src="{carousel}"><img src="{same}"><img src="{thumb}">') == [carousel.replace('&amp;', '&'), thumb]
assert extract_photo_urls(f'<img src="{thumb}">') == [thumb]
assert extract_photo_urls(f'<img src="{carousel}"><img src="{thumb}">', 1) == [carousel.replace('&amp;', '&')]
assert extract_photo_urls('no photo') == []
print('Photo extraction preserves resize/query and existing thumbnail URLs, deduplicates identity and respects limit.')
