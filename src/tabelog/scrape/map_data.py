"""Hand-curated data for map.py: cuisine buckets and marker emojis.
Tourist anchors used to live here as ATTRACTIONS (loaded from
data/attractions.csv); they now live in data/favorites_builtin.json
and render through the bookmarks layer."""

import re


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
        "火鍋",
        "薬膳",
    ],
    "台湾料理": ["台湾料理"],
    "韩国料理": ["韓国料理", "冷麺"],
    "法餐": ["フレンチ", "ビストロ", "オーベルジュ"],
    "意餐·披萨·意面": ["イタリアン", "ピザ", "パスタ"],
    "美式料理·汉堡": ["アメリカ料理", "ハンバーガー", "ハワイ料理"],
    # Single bucket for all of Latin America — mostly Mexican-leaning by token
    # count (メキシコ料理 + タコス + 中南米料理), but Brazilian / Peruvian
    # mixed in. Argentina flag chosen as a pan-LatAm representative.
    "拉美料理": [
        "メキシコ料理",
        "タコス",
        "ブラジル料理",
        "ペルー料理",
        "中南米料理",
    ],
    # Catch-all: Yōshoku and other Western specialties that don't justify a
    # dedicated country bucket. Spain / Greece / Germany each have only one
    # Tabelog token, so they live here under the EU flag rather than as
    # one-token buckets.
    "其他西餐": [
        "洋食",
        "ヨーロッパ料理",
        "ハンバーグ",
        "オムライス",
        "チーズ料理",
        "スペイン料理",
        "ギリシャ料理",
        "ドイツ料理",
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
# ---- Genre tokenizer (shared by map.py and the top-up gate) ----
# M-095: container words that say nothing about the food. categorize_genre
# skips them on its first pass so "レストラン、フレンチ" lands in 法餐, not 其他.
GENERIC_GENRE_TOKENS = frozenset(
    {"その他", "レストラン", "ビュッフェ", "ファミレス", "ホテル", "売店"}
)
_GENRE_SPLIT_RE = re.compile(r"[、,，]")
# Tabelog's category-filtered list pages (rstLst/RC/... — what scrape_topup
# --tokyo walks) render every genre as 容器词(菜系): "レストラン(焼肉)、
# レストラン(ホルモン)" where the plain list says "焼肉、ホルモン". The 2026-09
# Tokyo top-up stored 443 rows that way and every one of them fell through to
# 其他, because the whole "レストラン(焼肉)" was looked up as one token.
_GENRE_WRAP_RE = re.compile(
    r"(?:%s)\s*[（(]([^()（）]+)[）)]"
    % "|".join(re.escape(t) for t in sorted(GENERIC_GENRE_TOKENS))
)


def unwrap_genre(genre_str: str) -> str:
    """Strip the 容器词(菜系) wrapper so a row categorizes and displays the
    same way whichever list page it was scraped from. A string without the
    pattern comes back byte-identical."""
    if not genre_str:
        return ""
    return _GENRE_WRAP_RE.sub(lambda m: m.group(1).strip(), genre_str)


def genre_tokens(genre_str: str) -> list[str]:
    """Split a Tabelog genre string into its trimmed tokens, wrapper removed."""
    if not genre_str:
        return []
    return [t.strip() for t in _GENRE_SPLIT_RE.split(unwrap_genre(genre_str)) if t.strip()]


MEAL_GROUPS = {
    "正餐": [
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
    "小吃/面类": [
        "拉面·沾面",
        "乌冬·荞麦",
        "关东煮·锅物",
    ],
    "早餐/咖啡/甜品": [
        "咖啡·三明治",
        "烘焙·西点",
        "甜品·冰品",
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
    "台湾料理",
    "韩国料理",
    "法餐",
    "意餐·披萨·意面",
    "美式料理·汉堡",
    "拉美料理",
    "其他西餐",
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
    "台湾料理": "🇹🇼",
    "韩国料理": "🇰🇷",
    "法餐": "🇫🇷",
    "意餐·披萨·意面": "🇮🇹",
    "美式料理·汉堡": "🇺🇸",
    "拉美料理": "🇦🇷",
    "其他西餐": "🇪🇺",
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


# ---- Tokyo districts (4.2.3) ----
# The second level of the region filter for 東京都. Tabelog files every
# restaurant under a large area (A1303) and a small area (A130302), and both
# codes are in its detail_url, so nothing needs re-scraping. Tabelog's own
# large areas are uneven (4 to 126 rows) and four are named after railway
# companies, so the districts below are regrouped from its small areas.
# Reasoning, sizes and the owner's decisions: audit_outputs/tokyo-areas-plan/.
#
# Tabelog's names for the small areas live in data/tabelog_areas/tokyo.json
# (scrape_all.py --tokyo-district); this table is the curated half. A member
# is either a large-area code, which takes all of its small areas, or a
# small-area code, which overrides its large area's district.
#
# The ids are persisted in tabelog.filterState. Never rename or delete one:
# point the old id at its replacement in TOKYO_ZONE_ALIASES instead.
TOKYO_ZONE_GROUPS = [
    {"id": "toshin", "ja": "都心 · 銀座・東京駅・六本木", "zh": "都心 · 银座・东京站・六本木",
     "en": "Central · Ginza, Tokyo Station, Roppongi"},
    {"id": "fukutoshin", "ja": "副都心 · 新宿・渋谷・池袋", "zh": "副都心 · 新宿・涩谷・池袋",
     "en": "Sub-centres · Shinjuku, Shibuya, Ikebukuro"},
    {"id": "jonan", "ja": "城南 · 目黒・中目黒・世田谷", "zh": "城南 · 目黑・中目黑・世田谷",
     "en": "South · Meguro, Nakameguro, Setagaya"},
    {"id": "johoku", "ja": "城北 · 巣鴨・赤羽・練馬", "zh": "城北 · 巢鸭・赤羽・练马",
     "en": "North · Sugamo, Akabane, Nerima"},
    {"id": "joto", "ja": "城東（下町） · 上野・浅草・押上", "zh": "城东（下町） · 上野・浅草・押上",
     "en": "East (Shitamachi) · Ueno, Asakusa, Oshiage"},
    {"id": "josai", "ja": "城西・多摩 · 中野・吉祥寺", "zh": "城西・多摩 · 中野・吉祥寺",
     "en": "West & Tama · Nakano, Kichijoji"},
]

# English spellings follow Tabelog's small-area names (Shinbashi, Jinbocho,
# Azabujuban) so a district and the neighbourhoods listed under it agree.
TOKYO_ZONES = [
    {"id": "ginza", "group": "toshin", "ja": "銀座・有楽町・新橋", "zh": "银座・有乐町・新桥",
     "en": "Ginza, Yurakucho & Shinbashi", "members": ["A1301"]},
    {"id": "tokyo-station", "group": "toshin", "ja": "東京駅・日本橋・人形町", "zh": "东京站・日本桥・人形町",
     "en": "Tokyo Station, Nihonbashi & Ningyocho", "members": ["A1302"]},
    {"id": "akihabara", "group": "toshin", "ja": "秋葉原・神田・神保町", "zh": "秋叶原・神田・神保町",
     "en": "Akihabara, Kanda & Jinbocho", "members": ["A1310"]},
    {"id": "tsukiji", "group": "toshin", "ja": "築地・月島・門前仲町", "zh": "筑地・月岛・门前仲町",
     "en": "Tsukiji, Tsukishima & Monzennakacho", "members": ["A1313"]},
    {"id": "roppongi", "group": "toshin", "ja": "六本木・麻布十番・広尾", "zh": "六本木・麻布十番・广尾",
     "en": "Roppongi, Azabujuban & Hiroo", "members": ["A1307"]},
    {"id": "akasaka", "group": "toshin", "ja": "赤坂・虎ノ門・永田町", "zh": "赤坂・虎之门・永田町",
     "en": "Akasaka, Toranomon & Nagatacho", "members": ["A1308"]},
    {"id": "kagurazaka", "group": "toshin", "ja": "神楽坂・飯田橋・四ツ谷", "zh": "神乐坂・饭田桥・四谷",
     "en": "Kagurazaka, Iidabashi & Yotsuya", "members": ["A1309"]},
    {"id": "shinagawa", "group": "toshin", "ja": "浜松町・田町・品川", "zh": "滨松町・田町・品川",
     "en": "Hamamatsucho, Tamachi & Shinagawa", "members": ["A1314"]},
    {"id": "shinjuku", "group": "fukutoshin", "ja": "新宿・新大久保", "zh": "新宿・新大久保",
     "en": "Shinjuku & Shin-Okubo", "members": ["A1304"]},
    {"id": "shibuya", "group": "fukutoshin", "ja": "渋谷・恵比寿・代官山", "zh": "涩谷・惠比寿・代官山",
     "en": "Shibuya, Ebisu & Daikanyama",
     "members": ["A1303", "A131807", "A131808", "A131810", "A131811"]},
    {"id": "omotesando", "group": "fukutoshin", "ja": "表参道・原宿・青山", "zh": "表参道・原宿・青山",
     "en": "Omotesando, Harajuku & Aoyama", "members": ["A1306"]},
    {"id": "ikebukuro", "group": "fukutoshin", "ja": "池袋・高田馬場・早稲田", "zh": "池袋・高田马场・早稻田",
     "en": "Ikebukuro, Takadanobaba & Waseda", "members": ["A1305"]},
    {"id": "meguro", "group": "jonan", "ja": "目黒・白金・五反田", "zh": "目黑・白金・五反田",
     "en": "Meguro, Shirokane & Gotanda", "members": ["A1316", "A131710", "A131712", "A131713"]},
    {"id": "nakameguro", "group": "jonan", "ja": "中目黒・学芸大学・自由が丘", "zh": "中目黑・学艺大学・自由之丘",
     "en": "Nakameguro, Gakugei-Daigaku & Jiyugaoka",
     "members": ["A131701", "A131702", "A131703", "A131704", "A131711", "A131714", "A131716"]},
    {"id": "setagaya", "group": "jonan", "ja": "三軒茶屋・下北沢・二子玉川", "zh": "三轩茶屋・下北泽・二子玉川",
     "en": "Sangenjaya, Shimokitazawa & Futako-Tamagawa",
     "members": ["A131705", "A131706", "A131707", "A131708", "A131709", "A131715",
                 "A131801", "A131802", "A131803", "A131804", "A131809", "A131812", "A131813", "A131814"]},
    {"id": "kamata", "group": "jonan", "ja": "大井町・大森・蒲田", "zh": "大井町・大森・蒲田",
     "en": "Oimachi, Omori & Kamata", "members": ["A1315"]},
    {"id": "sugamo", "group": "johoku", "ja": "巣鴨・大塚・赤羽", "zh": "巢鸭・大塚・赤羽",
     "en": "Sugamo, Otsuka & Akabane", "members": ["A1323"]},
    {"id": "nerima", "group": "johoku", "ja": "練馬・板橋・江古田", "zh": "练马・板桥・江古田",
     "en": "Nerima, Itabashi & Ekoda", "members": ["A1321", "A1322"]},
    {"id": "ueno", "group": "joto", "ja": "上野・谷中・根津", "zh": "上野・谷中・根津",
     "en": "Ueno, Yanaka & Nezu", "members": ["A131101", "A131104", "A131105", "A131106"]},
    {"id": "asakusa", "group": "joto", "ja": "浅草・蔵前", "zh": "浅草・藏前",
     "en": "Asakusa & Kuramae", "members": ["A131102", "A131103"]},
    {"id": "oshiage", "group": "joto", "ja": "押上・両国・錦糸町", "zh": "押上・两国・锦糸町",
     "en": "Oshiage, Ryogoku & Kinshicho", "members": ["A1312"]},
    {"id": "kitasenju", "group": "joto", "ja": "北千住・町屋・柴又", "zh": "北千住・町屋・柴又",
     "en": "Kita-Senju, Machiya & Shibamata", "members": ["A1324"]},
    {"id": "nakano", "group": "josai", "ja": "中野・高円寺・荻窪", "zh": "中野・高圆寺・荻洼",
     "en": "Nakano, Koenji & Ogikubo", "members": ["A1319", "A131805", "A131806"]},
    {"id": "kichijoji", "group": "josai", "ja": "吉祥寺・三鷹", "zh": "吉祥寺・三鹰",
     "en": "Kichijoji & Mitaka", "members": ["A1320"]},
    {"id": "tama", "group": "josai", "ja": "多摩地区", "zh": "多摩地区",
     "en": "Tama Area", "members": ["A1325", "A1326", "A1327", "A1328", "A1329", "A1330", "A1331"]},
]

TOKYO_ZONE_ALIASES: dict[str, str] = {}

_ZONE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_ZONE_MEMBER_RE = re.compile(r"^A13\d{2}(\d{2})?$")


def tokyo_zone_of(small_code: str) -> str | None:
    """District id for a Tabelog small-area code: an explicit small-area
    member wins over the large area that contains it."""
    for z in TOKYO_ZONES:
        if small_code in z["members"]:
            return z["id"]
    for z in TOKYO_ZONES:
        if small_code[:5] in z["members"]:
            return z["id"]
    return None


def _validate_tokyo_zones() -> None:
    """Run at import time: the parts that do not need Tabelog's name table.
    Coverage of every small area is checked by map.py against that table."""
    groups = [g["id"] for g in TOKYO_ZONE_GROUPS]
    if len(groups) != len(set(groups)):
        raise RuntimeError(f"map_data.py: duplicate TOKYO_ZONE_GROUPS ids: {groups}")
    seen_ids: set[str] = set()
    seen_members: dict[str, str] = {}
    for z in TOKYO_ZONES:
        zid = z.get("id", "")
        if not _ZONE_ID_RE.match(zid) or zid in seen_ids:
            raise RuntimeError(f"map_data.py: bad or duplicate TOKYO_ZONES id {zid!r}")
        seen_ids.add(zid)
        if z.get("group") not in groups:
            raise RuntimeError(f"map_data.py: TOKYO_ZONES {zid} has unknown group {z.get('group')!r}")
        for lang in ("ja", "zh", "en"):
            if not z.get(lang):
                raise RuntimeError(f"map_data.py: TOKYO_ZONES {zid} is missing its {lang} name")
        for m in z.get("members") or []:
            if not _ZONE_MEMBER_RE.match(m):
                raise RuntimeError(f"map_data.py: TOKYO_ZONES {zid} member {m!r} is not a Tabelog area code")
            if m in seen_members:
                raise RuntimeError(
                    f"map_data.py: area {m} is claimed by both {seen_members[m]} and {zid}"
                )
            seen_members[m] = zid
        if not z.get("members"):
            raise RuntimeError(f"map_data.py: TOKYO_ZONES {zid} has no members")
    for old, new in TOKYO_ZONE_ALIASES.items():
        if old in seen_ids or new not in seen_ids:
            raise RuntimeError(f"map_data.py: TOKYO_ZONE_ALIASES {old!r} -> {new!r} is not a retired id pointing at a live one")


_validate_tokyo_zones()
