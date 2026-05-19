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
        "ちゃんぽん",
        "台湾まぜそば",
    ],
    "乌冬·荞麦": [
        "うどん",
        "そば",
        "うどんすき",
        "カレーうどん",
        "ほうとう",
        "沖縄そば",
    ],
    "寿司·海鲜": [
        "寿司",
        "海鮮",
        "うなぎ",
        "あなご",
        "かに",
        "シーフード",
        "回転寿司",
        "海鮮丼",
        "いなり寿司",
        "かき",
        "オイスターバー",
        "棒寿司",
        "立ち食い寿司",
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
        "シュラスコ",
        "ジンギスカン",
        "バーベキュー",
        "肉バル",
    ],
    "烤鸡·串烧": [
        "焼き鳥",
        "鳥料理",
        "串焼き",
        "からあげ",
        "ろばた焼き",
        "手羽先",
    ],
    "天妇罗·炸物": ["天ぷら", "揚げ物", "串揚げ", "とんかつ", "コロッケ", "牛カツ"],
    "日式咖喱": ["カレー", "スープカレー"],
    "盖饭·亲子丼": [
        "丼",
        "親子丼",
        "天丼",
        "かつ丼",
        "おにぎり",
        "食堂",
        "惣菜・デリ",
        "弁当",
        "牛丼",
        "豚丼",
        "釜飯",
    ],
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
        "ビアガーデン",
        "ビアホール",
        "焼酎バー",
    ],
    "日本料理·乡土": [
        "日本料理",
        "創作料理",
        "郷土料理",
        "イノベーティブ",
        "料理旅館",
        "きりたんぽ",
        "旅館・民宿",
        "沖縄料理",
        "豆腐料理",
        "野菜料理",
        "麦とろ",
    ],
    # Distinctively Japanese specialty cuisines that are rare elsewhere or
    # carry their own culinary culture — fugu (poisonous blowfish), basashi
    # (raw horse), kujira (whale), suppon (snapping turtle), dojo (loach),
    # anko (anglerfish), and gibier (wild game). Pulled out of generic sushi
    # / yakiniku / kyodo buckets so visitors can find them on purpose.
    "猎奇·珍味": [
        "ふぐ",
        "馬肉料理",
        "くじら料理",
        "すっぽん",
        "どじょう",
        "あんこう",
        "ジビエ料理",
    ],
    "御好烧·铁板烧": [
        "お好み焼き",
        "焼きそば",
        "鉄板焼き",
        "たこ焼き",
        "明石焼き",
        "もんじゃ焼き",
    ],
    "关东煮·锅物": ["おでん", "鍋", "ちゃんこ鍋", "水炊き", "もつ鍋"],
    "咖啡·三明治": ["カフェ", "喫茶店", "サンドイッチ", "パンケーキ", "コーヒースタンド"],
    "烘焙·西点": [
        "パン",
        "ケーキ",
        "プリン",
        "スイーツ",
        "ベーグル",
        "ドーナツ",
        "クレープ・ガレット",
        "シュークリーム",
    ],
    "甜品·冰品": [
        "甘味処",
        "かき氷",
        "たい焼き・大判焼き",
        "ジェラート・アイスクリーム",
        "ソフトクリーム",
        "ジューススタンド",
        "フルーツパーラー",
        "焼き芋・大学芋",
    ],
    # Boxed sweets that travelers typically buy as gifts (omiyage), rather
    # than sit-down desserts. Kept separate from 甜品·冰品 because the user
    # workflow for these is different (find one before leaving the city,
    # not while map-browsing for a meal).
    "伴手礼·点心": [
        "和菓子",
        "どら焼き",
        "大福",
        "せんべい",
        "カステラ",
        "バームクーヘン",
        "洋菓子",
        "チョコレート",
        "マカロン",
        "中華菓子",
    ],
    # Tokens listed under 其他 are explicit fallthroughs — categorize_genre
    # also defaults unmatched tokens here, but listing them silences the
    # "unmapped genre tokens" warning at build time.
    "其他": [
        "その他",
        "にんにく料理",
        "オーガニック",
        "スープ",
        "ビュッフェ",
        "ファミレス",
        "ホテル",
        "レストラン",
        "売店",
    ],
    "饺子·中餐": [
        "餃子",
        "中華料理",
        "四川料理",
        "中華粥",
        "小籠包",
        "肉まん",
        "飲茶・点心",
        "台湾料理",
        "火鍋",
        "薬膳",
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
        "アメリカ料理",
        "チーズ料理",
        "ハワイ料理",
        "ブラジル料理",
        "ペルー料理",
        "メキシコ料理",
        "中南米料理",
        "タコス",
    ],
    "南亚·东南亚料理": [
        "インドカレー",
        "インド料理",
        "ネパール料理",
        "スリランカ料理",
        "パキスタン料理",
        "タイ料理",
        "アジア・エスニック",
        "インドネシア料理",
        "ベトナム料理",
        "南アジア料理",
        "東南アジア料理",
    ],
    "中东·非洲": [
        "中東料理",
        "トルコ料理",
        "アフリカ料理",
    ],
}

# Super-categories that group each bucket by meal occasion. Renders as
# section headers in the filter panel ("正餐", "早餐/咖啡/甜品", ...).
# Foreign-cuisine buckets (DEFAULT_OFF_GENRES) sit under a separate
# "隐藏外国料理" toggle, so they intentionally don't appear here.
MEAL_GROUPS = {
    "正餐": [
        "拉面·沾面",
        "乌冬·荞麦",
        "寿司·海鲜",
        "烤肉·内脏",
        "烤鸡·串烧",
        "天妇罗·炸物",
        "日式咖喱",
        "盖饭·亲子丼",
        "日本料理·乡土",
        "猎奇·珍味",
        "御好烧·铁板烧",
        "其他",
    ],
    "早餐/咖啡/甜品": [
        "咖啡·三明治",
        "烘焙·西点",
        "甜品·冰品",
    ],
    "零食/纪念品": [
        "关东煮·锅物",
        "伴手礼·点心",
    ],
    "酒吧/居酒屋": [
        "居酒屋·酒吧",
    ],
}

# Buckets shown unchecked in the initial filter panel render and re-applied
# on "重置筛选". 全选 / 全清 buttons still toggle all rows including these.
DEFAULT_OFF_GENRES = {
    "饺子·中餐",
    "韩国料理",
    "西餐/西式料理",
    "南亚·东南亚料理",
    "中东·非洲",
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
    "猎奇·珍味": "🐡",
    "御好烧·铁板烧": "🥞",
    "关东煮·锅物": "🍲",
    "咖啡·三明治": "☕",
    "烘焙·西点": "🥐",
    "甜品·冰品": "🍧",
    "伴手礼·点心": "🍡",
    "其他": "🍽️",
    "饺子·中餐": "🇨🇳",
    "韩国料理": "🇰🇷",
    "西餐/西式料理": "🇫🇷",
    "南亚·东南亚料理": "🇮🇳",
    "中东·非洲": "🇱🇧",
}


def _validate_bucket_coverage() -> None:
    """Run at import time. Bucket lookup tables drift easily — a bucket
    added to GENRE_CATEGORIES but forgotten in MEAL_GROUPS silently
    disappears from the filter UI (markers still render, but the user
    can't toggle them). Fail loudly instead."""
    all_buckets = set(GENRE_CATEGORIES)
    grouped = [b for buckets in MEAL_GROUPS.values() for b in buckets]
    grouped_set = set(grouped)

    # Every bucket must live in exactly one of MEAL_GROUPS or DEFAULT_OFF_GENRES.
    unassigned = all_buckets - grouped_set - DEFAULT_OFF_GENRES
    if unassigned:
        raise RuntimeError(
            f"map_data.py: buckets in GENRE_CATEGORIES but neither in "
            f"MEAL_GROUPS nor DEFAULT_OFF_GENRES: {sorted(unassigned)}"
        )

    # MEAL_GROUPS shouldn't reference buckets that don't exist.
    stale = grouped_set - all_buckets
    if stale:
        raise RuntimeError(
            f"map_data.py: MEAL_GROUPS references unknown buckets: "
            f"{sorted(stale)}"
        )

    # A bucket can't be both grouped and marked foreign.
    overlap = grouped_set & DEFAULT_OFF_GENRES
    if overlap:
        raise RuntimeError(
            f"map_data.py: buckets in both MEAL_GROUPS and "
            f"DEFAULT_OFF_GENRES: {sorted(overlap)}"
        )

    # No duplicates across MEAL_GROUPS groups.
    if len(grouped) != len(grouped_set):
        from collections import Counter
        dupes = [b for b, n in Counter(grouped).items() if n > 1]
        raise RuntimeError(
            f"map_data.py: buckets appearing in multiple MEAL_GROUPS "
            f"groups: {sorted(dupes)}"
        )

    # Every bucket needs an emoji for marker rendering.
    missing_emoji = all_buckets - set(GENRE_EMOJI)
    if missing_emoji:
        raise RuntimeError(
            f"map_data.py: buckets missing from GENRE_EMOJI: "
            f"{sorted(missing_emoji)}"
        )


_validate_bucket_coverage()
