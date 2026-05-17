"""Hand-curated data for map.py: cuisine buckets, marker emojis, and
tourist anchors. Split out so it can be edited without touching
rendering code.

ATTRACTIONS is loaded from data/attractions.csv — edit that file (or
run fetch_attractions.py to fill in coords for new rows) instead of
pasting tuples here."""

import csv

from tabelog.paths import ATTRACTIONS_CSV


def _load_attractions() -> list[tuple[str, str, float, float]]:
    """(name_cn, emoji, lat, lon) for every CSV row whose lon and lat are
    both filled. Blank-coord rows are skipped silently — run
    fetch_attractions.py to resolve them."""
    rows: list[tuple[str, str, float, float]] = []
    if not ATTRACTIONS_CSV.exists():
        return rows
    with ATTRACTIONS_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lon, lat = r.get("lon", "").strip(), r.get("lat", "").strip()
            if not lon or not lat:
                continue
            rows.append((r["name_cn"], r["emoji"], float(lat), float(lon)))
    return rows


ATTRACTIONS = _load_attractions()


# Maps Tabelog's JP genre tokens to 20 broad cuisine categories. Dict order
# drives filter dropdown order. Single-tag: each restaurant lands in exactly
# one bucket (the first matching token in the comma-separated genre string).
# The four buckets sitting below 其他 are default-off in the filter panel
# (see DEFAULT_OFF_GENRES) — non-Japanese cuisines you usually don't want
# while map-browsing for Japanese food.
GENRE_CATEGORIES = {
    "拉面·沾面": [
        "ラーメン",
        "つけ麺",
        "油そば・まぜそば",
        "担々麺",
        "汁なし担々麺",
        "麺類",
    ],
    "乌冬·荞麦": ["うどん", "そば", "うどんすき", "カレーうどん"],
    "寿司·海鲜": [
        "寿司",
        "海鮮",
        "うなぎ",
        "ふぐ",
        "あなご",
        "かに",
        "シーフード",
        "回転寿司",
        "海鮮丼",
    ],
    "烤肉·内脏": [
        "焼肉",
        "ホルモン",
        "もつ焼き",
        "牛料理",
        "牛タン",
        "ステーキ",
        "肉料理",
        "しゃぶしゃぶ",
        "豚しゃぶ",
        "すき焼き",
        "豚料理",
    ],
    "烤鸡·串烧": ["焼き鳥", "鳥料理", "串焼き", "からあげ"],
    "天妇罗·炸物": ["天ぷら", "揚げ物", "串揚げ", "とんかつ", "コロッケ"],
    "日式咖喱": ["カレー", "スープカレー"],
    "盖饭·亲子丼": ["丼", "親子丼", "天丼", "かつ丼", "おにぎり", "食堂", "惣菜・デリ"],
    "居酒屋·酒吧": [
        "居酒屋",
        "バー",
        "ダイニングバー",
        "ワインバー",
        "日本酒バー",
        "バル",
        "パブ",
        "立ち飲み",
        "ビアバー",
    ],
    "日本料理·乡土": [
        "日本料理",
        "創作料理",
        "郷土料理",
        "イノベーティブ",
        "すっぽん",
        "料理旅館",
    ],
    "御好烧·铁板烧": ["お好み焼き", "焼きそば", "鉄板焼き", "たこ焼き", "明石焼き"],
    "关东煮·锅物": ["おでん", "鍋", "ちゃんこ鍋", "水炊き"],
    "咖啡·三明治": ["カフェ", "喫茶店", "サンドイッチ", "パンケーキ"],
    "烘焙·西点": [
        "パン",
        "洋菓子",
        "ケーキ",
        "チョコレート",
        "マカロン",
        "プリン",
        "スイーツ",
        "ベーグル",
        "ドーナツ",
    ],
    "和果子·冰品": [
        "和菓子",
        "大福",
        "甘味処",
        "かき氷",
        "たい焼き・大判焼き",
        "ジェラート・アイスクリーム",
        "ソフトクリーム",
        "ジューススタンド",
        "どら焼き",
        "フルーツパーラー",
    ],
    "其他": [],
    "饺子·中餐": [
        "餃子",
        "中華料理",
        "四川料理",
        "中華粥",
        "小籠包",
        "肉まん",
        "飲茶・点心",
    ],
    "韩国料理": ["韓国料理", "冷麺"],
    "西餐/西式料理": [
        "洋食",
        "ハンバーガー",
        "ハンバーグ",
        "フレンチ",
        "ヨーロッパ料理",
        "イタリアン",
        "ピザ",
        "パスタ",
        "オムライス",
        "ビストロ",
        "オーベルジュ",
        "ギリシャ料理",
        "スペイン料理",
        "ドイツ料理",
    ],
    "南亚·东南亚料理": [
        "インドカレー",
        "インド料理",
        "ネパール料理",
        "スリランカ料理",
        "パキスタン料理",
        "タイ料理",
    ],
}

# Buckets shown unchecked in the initial filter panel render and re-applied
# on "重置筛选". 全选 / 全清 buttons still toggle all rows including these.
DEFAULT_OFF_GENRES = {
    "饺子·中餐",
    "韩国料理",
    "西餐/西式料理",
    "南亚·东南亚料理",
}

# One emoji per bucket — rendered inside the map marker on top of the price
# color. Picked to be visually distinct at 13px.
GENRE_EMOJI = {
    "拉面·沾面": "🍜",
    "乌冬·荞麦": "🥣",
    "寿司·海鲜": "🍣",
    "烤肉·内脏": "🥩",
    "烤鸡·串烧": "🍗",
    "天妇罗·炸物": "🍤",
    "日式咖喱": "🍛",
    "盖饭·亲子丼": "🍚",
    "居酒屋·酒吧": "🍺",
    "日本料理·乡土": "🍱",
    "御好烧·铁板烧": "🥞",
    "关东煮·锅物": "🍲",
    "咖啡·三明治": "☕",
    "烘焙·西点": "🥐",
    "和果子·冰品": "🍡",
    "其他": "🍽️",
    "饺子·中餐": "🇨🇳",
    "韩国料理": "🇰🇷",
    "西餐/西式料理": "🇫🇷",
    "南亚·东南亚料理": "🇮🇳",
}
