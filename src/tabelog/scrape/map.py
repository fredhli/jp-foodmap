"""
Plot data/tabelog/tabelog.csv on an interactive Japan-wide map with a
client-side filter panel: rating threshold, dinner-price bucket, cuisine,
Tabelog bookable. Each row carries a `region` column so rows from
different regions (osaka, kobe, okayama, tottori, ...) all coexist in
one file.

Geocoding via GSI AddressSearch; results cached to data/cache/geocode_cache.json.

CSV is utf-8-sig so Japanese addresses round-trip through Excel cleanly.

Output: docs/index.html  (single file, open in any browser).
"""

import argparse
import calendar  # M-096: UTC-correct epoch for scraped_at quantiles
import csv
import hashlib
import html as _html
import json
import math
import os
import re
import shutil    # M-020: scratch dir cleanup for the atomic index.html write
import sys
import tempfile  # M-020: render folium's HTML outside the Dropbox tree
import time
from pathlib import Path
from urllib.parse import quote

import folium
import httpx
from dotenv import load_dotenv
from folium.plugins import MarkerCluster

# Force UTF-8 stdout — Windows console defaults to cp1252 and chokes on
# the Japanese restaurant names printed during the geocode pass.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tabelog.paths import (
    TABELOG_CSV,
    GEOCODE_CACHE,
    MAP_HTML,
    RESTAURANTS_JSON,
    POPUPS_JSON,
    POPUPS_TW_JSON,
    POPUPS_EN_JSON,
    POPUPS_JA_JSON,
    POLICY_EN_JSON,
    GOOGLE_PLACES_CSV,
    TABELOG_CSV,
    SW_JS,
    DROPPED_JSON,
    BUILD_REPORT_JSON,
    atomic_write_bytes,
    atomic_write_csv,
    atomic_write_json,
    atomic_write_text,
    DOCS_DIR,
    DOCS_DATA_DIR,
    FAVORITES_JSON,
    BLACKLIST_JSON,
    BOOKMARKS_JSON,
    FAVORITES_BUILTIN_JSON,
    OUTPUT_DIR,
    CACHE_DIR,
    I18N_EN_JSON,
    I18N_JA_JSON,
)
from tabelog.scrape.map_data import (
    DEFAULT_OFF_GENRES,
    GENRE_CATEGORIES,
    GENRE_EMOJI,
    MEAL_GROUPS,
)
from tabelog.scrape.search_norm import build_han_variants, canon_str

from opencc import OpenCC

# Build-time Simplified -> Traditional pass. We use the full OpenCC s2t
# config (with its multi-char phrase rules) so context-sensitive cases
# like 拉面->拉麵 / 内脏->內臟 come out right — char-level mapping picks
# the wrong default for ambiguous chars (面 can be 面 or 麵, 后 can be
# 後 or 后, etc.). The trade-off: we can't ship the full OpenCC engine
# to the browser (~hundreds of KB), so we precompute every CJK run that
# appears on the rendered page and ship the {simp:trad} lookup table.
_S2T = OpenCC("s2t")
# Matches a maximal contiguous run of CJK ideographs (BMP + Ext A + the
# compatibility block). Excludes kana / punctuation / latin so the runs
# we look up at build time match exactly what the JS regex finds at run
# time inside text nodes.
_CJK_RUN_RE = re.compile(r"[㐀-鿿豈-﫿]+")


# The HAN_VARIANTS dict and the KNOWN_LOCS array are JS data tables used
# only by the search canonicalizer — they hold thousands of CJK chars
# that never enter a text node. Strip them before scanning so the map
# doesn't fill up with entries we'll never use.
_HAN_VARIANTS_LITERAL_RE = re.compile(r"var HAN_VARIANTS\s*=\s*[^;]+;")
_KNOWN_LOCS_LITERAL_RE = re.compile(r"var KNOWN_LOCS\s*=\s*[^;]+;")
# The runtime tokenizer regex `/[㐀-鿿豈-﫿]+/g` literally contains the four
# CJK boundary characters that define its character class — U+3400, U+9FFF,
# U+F900, U+FAFF. Spelled with explicit \u escapes so the source character
# encoding can't substitute a visually-identical sibling (e.g. the basic
# 豈 U+8C48 vs the compatibility 豈 U+F900, which renders the same but
# fails to match).
_CJK_RUN_RE_LITERAL_RE = re.compile(r"/\[㐀-鿿豈-﫿\]\+/g")


def _scan_cjk_runs(html: str) -> set[str]:
    scanned = _HAN_VARIANTS_LITERAL_RE.sub("", html)
    scanned = _KNOWN_LOCS_LITERAL_RE.sub("", scanned)
    scanned = _CJK_RUN_RE_LITERAL_RE.sub("", scanned)
    return set(_CJK_RUN_RE.findall(scanned))


def build_text_trad_map(html: str) -> dict[str, str]:
    """Scan the rendered page for every distinct CJK run, run each one
    through full OpenCC s2t, and keep only the runs whose translation
    differs from the source. Keys / values are unicode strings."""
    out: dict[str, str] = {}
    for run in _scan_cjk_runs(html):
        conv = _S2T.convert(run)
        if conv != run:
            out[run] = conv
    return out


def build_text_en_map(html: str) -> tuple[dict[str, str], list[str]]:
    """Intersect the hand-curated data/i18n/en.json with the CJK runs
    that actually appear on the page. Returns (map, missing) where
    missing is the sorted list of runs the page needs but en.json
    doesn't translate yet — printed during the build so untranslated
    bits show up as a punch list."""
    if not I18N_EN_JSON.exists():
        return {}, []
    raw = json.loads(I18N_EN_JSON.read_text(encoding="utf-8"))
    en = {k: v for k, v in raw.items() if not k.startswith("__")}
    runs = _scan_cjk_runs(html)
    out = {run: en[run] for run in runs if run in en}
    missing = sorted(r for r in runs if r not in en)
    return out, missing


def build_text_ja_map(html: str) -> tuple[dict[str, str], list[str]]:
    """Same shape as build_text_en_map but reads data/i18n/ja.json. JA
    translations are mostly natural Japanese forms (東京タワー, ラーメン,
    お気に入り). Reservation policy strings are intentionally not in this
    table — those come from data/tabelog/tabelog.csv via popups-ja.json."""
    if not I18N_JA_JSON.exists():
        return {}, []
    raw = json.loads(I18N_JA_JSON.read_text(encoding="utf-8"))
    ja = {k: v for k, v in raw.items() if not k.startswith("__")}
    runs = _scan_cjk_runs(html)
    out = {run: ja[run] for run in runs if run in ja}
    missing = sorted(r for r in runs if r not in ja)
    return out, missing


def trad_popup_array(arr: list) -> list:
    """popups.json positional layout — only index 6 (Chinese reservation
    policy) and index 8 (award ribbon HTML, also Chinese) need trad
    conversion. Japanese fields (genre / station / address) are
    passed through unchanged."""
    a = list(arr)
    if len(a) > 6 and isinstance(a[6], str) and a[6]:
        a[6] = _S2T.convert(a[6])
    if len(a) > 8 and isinstance(a[8], str) and a[8]:
        a[8] = _S2T.convert(a[8])
    return a


def en_popup_array(arr: list, policy_en: str | None, en_addr: str | None = None) -> list:
    """Same shape as the simp popup row, with the policy field swapped
    for the English translation when one is available. Ribbons (index 8)
    are left in Chinese — the runtime localizer rewrites their few CJK
    runs (百名店, 受賞店, etc.) via TEXT_EN_MAP at insertion time, so we
    don't need to pre-process them here. `en_addr`, when supplied (Google-
    calibrated rows), overlays the address slot with Google's English form."""
    a = list(arr)
    if policy_en and len(a) > 6:
        a[6] = policy_en
    if en_addr and len(a) > 5:
        a[5] = en_addr
    return a


def ja_popup_array(arr: list, policy_ja: str | None) -> list:
    """JA variant — overlay the policy field with the original Japanese
    text from data/tabelog/tabelog.csv `reservation_policy`. Same fallback
    convention as en_popup_array. The address slot already holds the
    Japanese form (Google-calibrated 日本語 address, or the Tabelog one)."""
    a = list(arr)
    if policy_ja and len(a) > 6:
        a[6] = policy_ja
    return a


def load_google_places() -> dict[str, dict]:
    """Map detail_url -> Google-calibrated fields, for status=='accepted'
    rows only. review / unmatched are not trusted for auto-replacement (see
    scrape/google_enrich.py). Missing file => empty map (no calibration, the
    build falls back to GSI coords + the Tabelog address everywhere)."""
    if not GOOGLE_PLACES_CSV.exists():
        return {}
    out: dict[str, dict] = {}
    with GOOGLE_PLACES_CSV.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("status") or "") != "accepted":
                continue
            url = r.get("detail_url") or ""
            if not url:
                continue
            try:
                lat, lon = float(r["g_lat"]), float(r["g_lon"])
            except (KeyError, TypeError, ValueError):
                continue
            out[url] = {
                "lat": lat,
                "lon": lon,
                "addr_ja": (r.get("g_address_ja") or "").strip(),
                "addr_en": (r.get("g_address_en") or "").strip(),
                "place_id": (r.get("place_id") or "").strip(),
            }
    return out


# In-page help-popover copy. Authored per-language as full prose
# because the CJK-run substitution pipeline (which handles every
# other piece of static UI text) can't reorder words, fix
# capitalisation, or swap fullwidth punctuation — fine for short
# subtitle labels, bad for sentences. zh-TW is auto-derived from
# zh-CN via OpenCC s2t at build time. The JS bundle gets the whole
# table inlined via the __HELP_COPY__ placeholder, and each
# `.ff-help-section[data-help-for=...]` overrides its textContent
# from this table on boot (bypassing the localizer).
HELP_COPY: dict[str, dict[str, str]] = {
    "blacklist": {
        "zh-CN": "弃用名单 = 你研究后决定不会去的餐厅，会从地图上自动隐藏；和收藏一起通过你的 Google 账号跨设备同步。",
        "en": "The Discard list holds restaurants you've researched and decided not to visit. They auto-hide from the map and sync across devices alongside your Saved list, via your Google account.",
        "ja": "非表示リストは、調べた結果「行かない」と決めたレストランの置き場です。地図から自動的に非表示になり、お気に入りと一緒に Google アカウントで端末間同期されます。",
    },
    "kind-bookmark": {
        "zh-CN": "收藏 = 你自己想标注的地点或建筑物；和景点一起通过你的 Google 账号跨设备同步。",
        "en": "Saved is for places or buildings you want to mark yourself — hotels, shops, points of interest. Synced across devices alongside Sights, via your Google account.",
        "ja": "お気に入りは、自分で印を付けたい場所や建物（ホテル、お店、気になるスポット）のためのカテゴリです。観光スポットと一緒に Google アカウントで端末間同期されます。",
    },
    "kind-attraction": {
        "zh-CN": "景点 = 系统默认旅游锚点之外你自己加的去处，与默认景点一起在地图上显示，可随时删除；和收藏一起通过你的 Google 账号跨设备同步。",
        "en": "Sights is for destinations you add on top of the built-in tourist anchors. Custom Sights show on the map next to the built-ins and can be removed anytime. Synced across devices alongside Saved, via your Google account.",
        "ja": "観光スポットは、デフォルトの観光スポットに加えて自分で追加した行き先のためのカテゴリです。デフォルトと並んで地図に表示され、いつでも削除できます。お気に入りと一緒に Google アカウントで端末間同期されます。",
    },
    "price-curation": {
        "zh-CN": "本站只收录每个区域评分前 1% 的餐厅；其中高价位 fine-dining 进一步压到 0.1%；和果子、咖啡厅、面包店等非正餐也按比例控量。这样，在地图上显示出来的 20000 日元以下的高分正餐餐厅，多数可以在一个合理的天数内提前预订，甚至 walk-in。",
        "en": "Each region keeps only its top 1% by rating. High-end fine-dining (¥20,000+) is further capped at 0.1%, and non-meal categories — wagashi, cafés, bakeries — are volume-controlled too. The upshot: most of the under-¥20,000 proper-meal restaurants you see on the map can be booked a few days out, and some allow same-day walk-ins.",
        "ja": "各エリアで評価上位 1% のレストランのみを掲載しています。¥20,000 以上の高単価ファインダイニングはさらに 0.1% まで絞り込み、和菓子・カフェ・ベーカリーなど食事以外のジャンルも比率で制限しています。結果として、地図に表示される ¥20,000 以下の高評価レストランは、多くが数日前から予約可能で、当日のウォークインで入れる店もあります。",
    },
}


def build_help_copy_json() -> str:
    """Inflate the per-popover language map: take zh-CN as the canonical
    source, derive zh-TW via OpenCC s2t (same engine the rest of the
    page uses), pass en / ja through untouched. Serialised compactly
    for inlining into the FILTER_JS_TEMPLATE bundle."""
    out: dict[str, dict[str, str]] = {}
    for key, langs in HELP_COPY.items():
        out[key] = dict(langs)
        cn = langs.get("zh-CN")
        if cn and "zh-TW" not in langs:
            out[key]["zh-TW"] = _S2T.convert(cn)
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


# Relative image refs in the help markdown — `![](foo.png)` — without a
# scheme or a leading slash. Rewritten at build time to `help/foo.png` so
# they resolve correctly against docs/index.html (the help/ dir sits next
# to the rendered HTML at deploy time). External / absolute paths are
# passed through untouched.
def load_policy_en() -> dict[str, str]:
    if not POLICY_EN_JSON.exists():
        return {}
    try:
        return json.loads(POLICY_EN_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  WARN: {POLICY_EN_JSON} is invalid JSON ({e}); treating as empty")
        return {}


def load_policy_ja() -> dict[str, str]:
    """Read the original Japanese reservation_policy column from tabelog.csv,
    keyed by detail_url. This is the literal text Tabelog publishes — no
    translation pass. Used to overlay popups-ja.json so JA users see the
    same wording they'd see on Tabelog itself."""
    if not TABELOG_CSV.exists():
        return {}
    out: dict[str, str] = {}
    try:
        with TABELOG_CSV.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                url = (row.get("detail_url") or "").strip()
                policy = (row.get("reservation_policy") or "").strip()
                if url and policy:
                    out[url] = policy
    except OSError as e:
        print(f"  WARN: could not read {TABELOG_CSV} ({e}); JA policies empty")
        return {}
    return out


# Japan's 47 都道府県. Used as a whitelist for parse_admin_prefix because the
# regex approach trips on 都/府/県 also appearing INSIDE prefecture names
# (e.g. "京都府京都市..." has 都 inside 京都府 not as a suffix).
_PREFECTURES = (
    "北海道",
    "青森県",
    "岩手県",
    "宮城県",
    "秋田県",
    "山形県",
    "福島県",
    "茨城県",
    "栃木県",
    "群馬県",
    "埼玉県",
    "千葉県",
    "神奈川県",
    "東京都",
    "新潟県",
    "富山県",
    "石川県",
    "福井県",
    "山梨県",
    "長野県",
    "岐阜県",
    "静岡県",
    "愛知県",
    "三重県",
    "滋賀県",
    "京都府",
    "大阪府",
    "兵庫県",
    "奈良県",
    "和歌山県",
    "鳥取県",
    "島根県",
    "岡山県",
    "広島県",
    "山口県",
    "徳島県",
    "香川県",
    "愛媛県",
    "高知県",
    "福岡県",
    "佐賀県",
    "長崎県",
    "熊本県",
    "大分県",
    "宮崎県",
    "鹿児島県",
    "沖縄県",
)
# After the prefecture is stripped, repeated tokens of the form "<name><suffix>"
# where suffix ∈ {市,郡,区,町,村}. The negation excludes digits so we stop
# cleanly at the 番地 portion of the address.
_ADMIN_RE = re.compile(r"^([^市郡区町村\d]{1,6}[市郡区町村])")


def parse_admin_prefix(addr: str) -> str:
    """Address → 'prefecture + city + ward' substring (no street/lot). Drives
    the location-aware search: searching '眺游楼 横浜' boosts restaurants
    whose admin prefix contains 横浜 to the top."""
    if not addr:
        return ""
    parts: list[str] = []
    for p in _PREFECTURES:
        if addr.startswith(p):
            parts.append(p)
            addr = addr[len(p) :]
            break
    for _ in range(3):
        m = _ADMIN_RE.match(addr)
        if not m:
            break
        parts.append(m.group(1))
        addr = addr[m.end() :]
    return "".join(parts)


def extract_city(addr: str) -> str:
    """City label shown in search rows: first non-郡 admin token after the
    prefecture, stripped of its 市/区/町/村 suffix. '長野県松本市...' → 松本,
    '東京都港区...' → 港, '北海道札幌市中央区...' → 札幌,
    '長野県北佐久郡軽井沢町...' → 軽井沢 (skips 郡)."""
    if not addr:
        return ""
    for p in _PREFECTURES:
        if addr.startswith(p):
            addr = addr[len(p) :]
            break
    for _ in range(3):
        m = _ADMIN_RE.match(addr)
        if not m:
            return ""
        tok = m.group(1)
        if tok.endswith("郡"):
            addr = addr[m.end() :]
            continue
        return tok[:-1]
    return ""


def admin_tokens_with_suffix(addr: str) -> list[str]:
    """All admin tokens, in both full and stem form. Drives the KNOWN_LOCS
    whitelist the search box uses to decide whether the trailing query token
    is a location filter. '長野県松本市中央...' → ['長野県','長野','松本市','松本']."""
    if not addr:
        return []
    out: list[str] = []
    pref = None
    for p in _PREFECTURES:
        if addr.startswith(p):
            pref = p
            addr = addr[len(p) :]
            break
    if pref:
        out.append(pref)
        if pref != "北海道":
            out.append(pref[:-1])
    for _ in range(3):
        m = _ADMIN_RE.match(addr)
        if not m:
            break
        tok = m.group(1)
        addr = addr[m.end() :]
        out.append(tok)
        if not tok.endswith("郡"):
            out.append(tok[:-1])
    return out


GSI_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"

CSV_PATH = TABELOG_CSV
CACHE_PATH = GEOCODE_CACHE
OUT_HTML = MAP_HTML

# Baseline lists — JSON arrays of detail_url strings. Rebuilding the map
# re-reads these files; in-browser ⭐ / 🚫 clicks live in localStorage as a
# diff against the baseline and can be exported back to overwrite the file.
FAVORITES_PATH = FAVORITES_JSON
BLACKLIST_PATH = BLACKLIST_JSON


def _load_url_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return set()


def load_favorites() -> set[str]:
    return _load_url_set(FAVORITES_PATH)


def load_blacklist() -> set[str]:
    return _load_url_set(BLACKLIST_PATH)


def load_bookmarks() -> list[dict]:
    """User-named map pins. Two name schemas coexist:
      - new (post i18n): {name_src, name_sc, name_en} — the browser's save
        path translates the user's input to zh-CN + en at commit time.
        zh-TW is derived at runtime via the existing localizer.
      - legacy: {name} — single string, pre-i18n entries.
    Both shapes round-trip through this loader unchanged; the JS picks the
    right field for the active UI language. Rows missing coords are dropped
    silently — the in-browser editor is the source of truth, this file is
    just the build-time seed."""
    if not BOOKMARKS_JSON.exists():
        return []
    try:
        raw = json.loads(BOOKMARKS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        cat = str(item.get("category") or "bookmark")
        if cat not in ("bookmark", "attraction"):
            cat = "bookmark"
        entry: dict = {
            "id": str(item.get("id") or f"{lat:.6f},{lon:.6f}"),
            "emoji": str(item.get("emoji") or "📍"),
            "lat": lat,
            "lon": lon,
            "category": cat,
        }
        has_new = any(
            k in item for k in ("name_src", "name_sc", "name_tc", "name_en", "name_jp")
        )
        if has_new:
            src = str(item.get("name_src") or "").strip()
            sc = str(item.get("name_sc") or "").strip()
            tc = str(item.get("name_tc") or "").strip()
            en = str(item.get("name_en") or "").strip()
            jp = str(item.get("name_jp") or "").strip()
            if not (src or sc or tc or en or jp):
                src = "未命名"
            entry["name_src"] = src
            entry["name_sc"] = sc
            entry["name_en"] = en
            if tc:
                entry["name_tc"] = tc
            if jp:
                entry["name_jp"] = jp
        else:
            entry["name"] = str(item.get("name") or "").strip() or "未命名"
        out.append(entry)
    return out


def load_favorites_builtin() -> list[dict]:
    """Repo-shipped landmark set — read every page load (no localStorage
    hydration), so updating the JSON file + redeploying immediately
    reflects on every visitor's map regardless of their sync state. Same
    parser as load_bookmarks(); the only difference is the file path."""
    if not FAVORITES_BUILTIN_JSON.exists():
        return []
    try:
        raw = json.loads(FAVORITES_BUILTIN_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        cat = str(item.get("category") or "bookmark")
        if cat not in ("bookmark", "attraction"):
            cat = "bookmark"
        src = str(item.get("name_src") or "").strip()
        sc = str(item.get("name_sc") or "").strip()
        tc = str(item.get("name_tc") or "").strip()
        en = str(item.get("name_en") or "").strip()
        jp = str(item.get("name_jp") or "").strip()
        if not (src or sc or tc or en or jp):
            src = "未命名"
        entry: dict = {
            "id": str(item.get("id") or f"{lat:.6f},{lon:.6f}"),
            "emoji": str(item.get("emoji") or "📍"),
            "lat": lat,
            "lon": lon,
            "category": cat,
            "name_src": src,
            "name_sc": sc,
            "name_en": en,
        }
        if tc:
            entry["name_tc"] = tc
        if jp:
            entry["name_jp"] = jp
        out.append(entry)
    return out


JAPAN_CENTER = (36.2048, 138.2529)


def load_cache() -> dict[str, dict | None]:
    # M-020: a truncated / corrupt cache used to raise JSONDecodeError out of
    # main (or, worse in google_enrich, be swallowed into {}). Swallowing it
    # here would silently re-issue ~9,900 GSI requests in one run, so refuse
    # to continue and let the operator decide.
    if CACHE_PATH.exists():
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise SystemExit(
                f"{CACHE_PATH} is not valid JSON ({e}). Refusing to run with an "
                f"empty cache — that would re-issue one GSI request per row. "
                f"Inspect the file (a '.tmp' sibling may hold the interrupted "
                f"write), restore it, or delete it deliberately to re-geocode "
                f"from scratch."
            )
        if not isinstance(data, dict):
            raise SystemExit(
                f"{CACHE_PATH} does not contain a JSON object (got "
                f"{type(data).__name__}). Refusing to continue."
            )
        return data
    return {}


def save_cache(cache: dict) -> None:
    # M-020: tmp + fsync + rename. The old write_text truncated a 2.4 MB file
    # in place ~1,960 times per --fillall run; any interruption inside that
    # window left an unparseable cache.
    atomic_write_json(CACHE_PATH, cache, indent=2)


# --- geocode cache entries (M-094) ------------------------------------------
# Value shapes, newest first:
#   {"status": "hit",  "lat":…, "lon":…, "matched_query":…, "display":…}
#   {"status": "miss", "at": "<iso>"}          — GSI answered, found nothing
#   {"lat":…, "lon":…, …}                      — legacy positive (no status)
#   null                                        — legacy negative
# "error" (network / HTTP failure) is deliberately never persisted: the old
# code cached it forever as `null`, indistinguishable from a real miss, and
# --fillall could not get past it because geocode() checked the cache first.


def _cache_lookup(cache: dict, addr: str) -> tuple[bool, dict | None]:
    """Returns (resolved, loc). resolved=False means "ask GSI"."""
    if addr not in cache:
        return False, None
    v = cache[addr]
    if v is None:  # legacy negative
        return True, None
    if isinstance(v, dict):
        status = v.get("status")
        if status == "hit" or ("lat" in v and "lon" in v):  # legacy positive
            return True, v
        if status == "miss":
            return True, None
    return False, None  # unknown / "error" shape -> re-query


def _cache_store_hit(cache: dict, addr: str, loc: dict) -> None:
    cache[addr] = {"status": "hit", **loc}


def _cache_store_miss(cache: dict, addr: str) -> None:
    cache[addr] = {
        "status": "miss",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


_FLOOR_RE = re.compile(r"\s*[BbＢ]?[\d０-９]{1,2}\s*(?:[FfＦ]|階).*$")
_BUILDING_KW = ("ビル", "メゾン", "ハイツ", "マンション", "別邸", "アネックス")


def simplify_address(addr: str) -> str:
    """Strip floor / building suffix to give the geocoder a fighting chance.
    Handles ASCII and full-width floor markers (1F / ２Ｆ / 1 階) and trailing
    building names — the building strip is unconditional (Sapporo's grid
    addresses like 南5西3 don't carry hyphenated 番地 but still trip on a
    ビル suffix)."""
    if not addr:
        return ""
    s = addr.strip()
    s = _FLOOR_RE.sub("", s).rstrip()
    for kw in _BUILDING_KW:
        i = s.find(kw)
        if i > 5:
            space = s.rfind(" ", 0, i)
            if space > 0:
                s = s[:space]
                break
    return s.strip()


# Kyoto's old-town addresses encode the nearest street intersection rather
# than just block numbers — e.g. "松原通大和大路東入2丁目轆轤町101". The
# 町名+番地 at the tail (轆轤町101) is what GSI can actually resolve; the
# 通り prefix confuses the matcher. This helper carves out <区><町名><番地>
# by finding the last cross-street marker and keeping only what follows.
_KYOTO_KU_RE = re.compile(r"^(京都府京都市\S+?区)")
_KYOTO_CROSS_RE = re.compile(r"(?:西入ル|東入ル|西入|東入|上ル|下ル|上る|下る)")
_KYOTO_TAIL_PREFIX_RE = re.compile(r"^(?:\d+丁目|(?:南側|北側|東側|西側)(?!町))+")


def kyoto_simplify(addr: str) -> str | None:
    if not addr or "京都府京都市" not in addr:
        return None
    m = _KYOTO_KU_RE.match(addr.strip())
    if not m:
        return None
    ku = m.group(1)
    rest = addr[m.end() :]
    last_end = -1
    for c in _KYOTO_CROSS_RE.finditer(rest):
        last_end = c.end()
    if last_end < 0:
        return None
    tail = _KYOTO_TAIL_PREFIX_RE.sub("", rest[last_end:].lstrip())
    tail = simplify_address(tail).strip()
    if not tail:
        return None
    return ku + tail


# Sapporo's grid is officially '南N条西M丁目' but Tabelog (and locals)
# write '南N西M'. GSI's index uses the long form, so splice 条 in to match.
_SAPPORO_GRID_RE = re.compile(r"([東西南北])(\d+)\s*([東西南北])(\d+)")


def sapporo_simplify(addr: str) -> str | None:
    if not addr or "札幌" not in addr:
        return None
    s = simplify_address(addr)
    new, n = _SAPPORO_GRID_RE.subn(r"\1\2条\3\4", s, count=1)
    return new if n else None


# Some Tabelog Kyoto addresses drop the trailing 町 ('樋之口467-2' for
# what GSI indexes as '樋之口町'). When the tail is a bare <name><番地>
# with no 通 / 町, splice 町 in front of the number.
_KYOTO_BARE_RE = re.compile(
    r"^(京都府京都市\S+?区)([^\d\s通町]+?)(\d+(?:[-－]\d+)*)\s*$"
)


def kyoto_append_chome(addr: str) -> str | None:
    if not addr or "京都府京都市" not in addr:
        return None
    m = _KYOTO_BARE_RE.match(simplify_address(addr).strip())
    if not m:
        return None
    ku, tail, num = m.groups()
    return f"{ku}{tail}町{num}"


# Final-fallback for Kyoto addresses that omit the 入ル/西入 anchor
# entirely (e.g. "高辻通高倉泉正寺町465-2", "東大路安井北門通月見町13").
# Scans for the rightmost <町名><番地> token after the 区 prefix —
# [^\s通]+? can't span 通 so the match auto-stops at the last 通り
# boundary, giving us just the residential tail.
_KYOTO_CHOME_RE = re.compile(r"[^\s通]+?町\d+(?:[-－]\d+)*")


def kyoto_extract_chome(addr: str) -> str | None:
    if not addr or "京都府京都市" not in addr:
        return None
    m = _KYOTO_KU_RE.match(addr.strip())
    if not m:
        return None
    matches = list(_KYOTO_CHOME_RE.finditer(addr[m.end() :]))
    if not matches:
        return None
    return m.group(1) + matches[-1].group(0)


def gsi_geocode(query: str, client: httpx.Client) -> tuple[str, dict | None]:
    """Hit GSI AddressSearch.

    M-094: returns ("hit", loc) / ("miss", None) / ("error", None) instead of
    a bare None, so the caller can tell "GSI says this address does not exist"
    apart from "the request blew up" and stop caching the second one forever.
    """
    try:
        r = client.get(GSI_URL, params={"q": query}, timeout=30.0)
        r.raise_for_status()
        hits = r.json()
    except Exception as e:
        print(f"  GSI error on {query!r}: {e}")
        return "error", None
    if not hits:
        return "miss", None
    top = hits[0]
    geom = (top.get("geometry") or {}).get("coordinates") or []
    if len(geom) != 2:
        return "miss", None
    lon, lat = geom  # GSI returns [lon, lat]
    return "hit", {
        "lat": float(lat),
        "lon": float(lon),
        "matched_query": query,
        "display": (top.get("properties") or {}).get("title", ""),
    }


def geocode(
    addr: str,
    client: httpx.Client,
    cache: dict,
    ignore_cache: bool = False,
) -> dict | None:
    """Resolve one address, consulting / updating the geocode cache.

    M-094: `ignore_cache` (the --ignore-cache flag) re-queries GSI even for
    addresses already in the cache — the only way to retry an old negative
    entry, since the pre-2.0 code checked the cache on the very first line and
    `--fillall` therefore could not get past it.
    """
    if not ignore_cache:
        resolved, cached = _cache_lookup(cache, addr)
        if resolved:
            return cached
    candidates = [
        addr,
        simplify_address(addr),
        kyoto_simplify(addr),
        kyoto_extract_chome(addr),
        kyoto_append_chome(addr),
        sapporo_simplify(addr),
    ]
    candidates = [c for c in candidates if c]
    seen = set()
    saw_error = False
    for q in candidates:
        if q in seen:
            continue
        seen.add(q)
        status, loc = gsi_geocode(q, client)
        if status == "error":
            saw_error = True
            continue
        if status == "hit" and loc is not None:
            _cache_store_hit(cache, addr, loc)
            return loc
    if saw_error:
        # Transient failure: leave the cache untouched. Writing a negative here
        # is exactly the bug — one flaky minute would permanently delete the
        # restaurant from the published map.
        return None
    if ignore_cache:
        # A forced re-query that now misses must not throw away a coordinate we
        # already had; --ignore-cache exists to retry failures, not to lose hits.
        _, previous = _cache_lookup(cache, addr)
        if previous is not None:
            print(f"  GSI now misses {addr!r}; keeping the cached coordinate")
            return previous
    _cache_store_miss(cache, addr)
    return None


_GENRE_TO_CAT = {tok: cat for cat, toks in GENRE_CATEGORIES.items() for tok in toks}
_GENRE_SPLIT_RE = re.compile(r"[、,，]")


def categorize_genre(genre_str: str) -> list[str]:
    """Single-tag: scan tokens left-to-right, return the first that maps to a
    known bucket. Falls through to '其他' if no token matches. Returns a list
    (length 0 or 1) so downstream iteration keeps working."""
    if not genre_str:
        return []
    for tok in (t.strip() for t in _GENRE_SPLIT_RE.split(genre_str) if t.strip()):
        cat = _GENRE_TO_CAT.get(tok)
        if cat:
            return [cat]
    return ["其他"]


# Filterable award tags — slug, label, emoji. Order = display order in the
# filter panel. Tag slugs are the JS-side filter values and the strings that
# appear in each row's `awards` payload array.
AWARD_TAGS = [
    ("gold", "Gold", "🥇"),
    ("silver", "Silver", "🥈"),
    ("bronze", "Bronze", "🥉"),
    ("hyaku", "百名店", "💯"),
    ("hot", "热门餐厅 2026", "🔥"),
]
_AWARD_ORDER = {slug: i for i, (slug, _, _) in enumerate(AWARD_TAGS)}


def _award_ribbons_html(awards_json: str) -> str:
    """Pre-render the ribbon strip shown above the restaurant name in the
    bottom-sheet card. Returns '' when there are no awards; otherwise a
    <div class="rst-ribbons">…</div> block of flat dark badges.

    Order: medals (gold→silver→bronze) → 百名店 (newest year first) → hot.
    Labels stay short ('2026 GOLD', '百名店 2025') to match the Tabelog
    site style. Within 百名店, multiple lists in the same year (e.g.
    ラーメン EAST + ラーメン TOKYO) collapse into a single chip — the
    individual list names show up in the hover tooltip.
    """
    if not awards_json or not awards_json.strip():
        return ""
    try:
        arr = json.loads(awards_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    medal_order = {"gold": 0, "silver": 1, "bronze": 2}
    medals: list[tuple[int, str, str, str]] = []  # (sort, cls, label, tip)
    hyaku_by_year: dict[str, list[str]] = {}  # year -> [long labels]
    hot_tips: list[str] = []
    seen_medals: set[str] = set()

    for a in arr:
        if not isinstance(a, dict):
            continue
        kind = a.get("kind", "")
        variant = a.get("variant") or ""
        short = (a.get("short") or "").strip()
        long_ = (a.get("long") or "").strip()

        if kind == "award":
            medal = next(
                (m for m in ("gold", "silver", "bronze") if variant.endswith(m)), None
            )
            if not medal or medal in seen_medals:
                continue
            seen_medals.add(medal)
            label = f"2026 {medal.upper()}"
            medals.append(
                (medal_order[medal], f"rst-ribbon-{medal}", label, long_ or short)
            )
        elif kind == "hyakumeiten":
            # variant is e.g. "2025ramen" — first 4 chars are the year.
            m = re.match(r"^(\d{4})", variant)
            year = m.group(1) if m else ""
            if not year:
                continue
            hyaku_by_year.setdefault(year, []).append(long_ or short)
        elif kind == "other":
            if hot_tips:
                continue
            hot_tips.append(long_ or short)

    medals.sort(key=lambda t: t[0])
    parts: list[str] = []
    for _, cls, label, tip in medals:
        parts.append(
            f'<span class="rst-ribbon {cls}" title="{_html.escape(tip)}">'
            f"{_html.escape(label)}</span>"
        )
    # Newest year first.
    for year in sorted(hyaku_by_year, reverse=True):
        cls = f"rst-ribbon-hyaku-{year}"
        # Browser tooltip respects \n as a line break — useful when a
        # restaurant is on multiple 百名店 lists in the same year.
        tip = "\n".join(hyaku_by_year[year])
        parts.append(
            f'<span class="rst-ribbon {cls}" title="{_html.escape(tip)}">'
            f"{_html.escape(f'百名店 {year}')}</span>"
        )
    for tip in hot_tips:
        parts.append(
            f'<span class="rst-ribbon rst-ribbon-hot" '
            f'title="{_html.escape(tip)}">热门 2026</span>'
        )
    if not parts:
        return ""
    return f'<div class="rst-ribbons">{"".join(parts)}</div>'


def parse_awards(awards_json: str) -> list[str]:
    """Normalize the awards CSV column (a JSON array of
    {kind, variant, short, long}) into a deduped, ordered list of tag slugs
    drawn from AWARD_TAGS. Returns [] for empty / malformed values.

    Mapping:
      kind=award + variant ending in gold/silver/bronze → that medal
      kind=hyakumeiten                                  → 'hyaku'
      kind=other                                        → 'hot'
    """
    if not awards_json or not awards_json.strip():
        return []
    try:
        arr = json.loads(awards_json)
    except (json.JSONDecodeError, TypeError):
        return []
    tags: set[str] = set()
    for a in arr:
        if not isinstance(a, dict):
            continue
        kind = a.get("kind", "")
        variant = a.get("variant", "") or ""
        if kind == "award":
            for medal in ("gold", "silver", "bronze"):
                if variant.endswith(medal):
                    tags.add(medal)
                    break
        elif kind == "hyakumeiten":
            tags.add("hyaku")
        elif kind == "other":
            tags.add("hot")
    return sorted(tags, key=lambda t: _AWARD_ORDER.get(t, 99))


# Price buckets — keys must match the JS filter values below.
# (key, label, color, lower_inclusive, upper_exclusive)
PRICE_BUCKETS = [
    ("lt1k", "< ¥1,000", "#15803d", None, 1000),
    ("1to3k", "¥1,000 – 3,000", "#16a34a", 1000, 3000),
    ("3to5k", "¥3,000 – 5,000", "#84cc16", 3000, 5000),
    ("5to10k", "¥5,000 – 10,000", "#eab308", 5000, 10000),
    ("10to20k", "¥10,000 – 20,000", "#f97316", 10000, 20000),
    ("ge20k", "¥20,000+", "#dc2626", 20000, None),
    ("na", "价格 NA", "#9ca3af", None, None),
]


def _as_price_int(v) -> int | None:
    try:
        return int(v) if v not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


def price_bucket(row: dict) -> tuple[str, str, str]:
    """Return (key, label, hex_color) for the marker.

    Bucket by dinner_upper; when dinner is NA, fall back to lunch_upper so
    lunch-only shops (ラーメン, うどん, 定食…) still get coloured."""
    n = _as_price_int(row.get("dinner_upper"))
    if n is None:
        n = _as_price_int(row.get("lunch_upper"))
    if n is None:
        return ("na", "价格 NA", "#9ca3af")
    for key, label, color, lo, hi in PRICE_BUCKETS:
        if key == "na":
            continue
        if (lo is None or n >= lo) and (hi is None or n < hi):
            return (key, label, color)
    return ("na", "价格 NA", "#9ca3af")


def build_filter_panel_html(
    cat_counts: dict[str, int],
    award_counts: dict[str, int],
    gcal_count: int = 0,
) -> str:
    price_rows = "\n".join(
        f'      <label style="display:block;margin:1px 0;">'
        f'<input type="checkbox" name="ff-price" value="{key}" checked> '
        f'<span style="display:inline-block;width:11px;height:11px;background:{color};'
        f'border-radius:50%;margin:0 4px;vertical-align:middle;"></span>{label}</label>'
        for key, label, color, _, _ in PRICE_BUCKETS
    )
    award_rows = "\n".join(
        f'    <label style="display:inline-flex;align-items:center;gap:4px;'
        f'margin:0 8px 4px 0;font-size:12px;white-space:nowrap;">'
        f'<input type="checkbox" name="ff-award" value="{slug}"> '
        f"{emoji} {label} "
        f'<span style="color:#6b7280;font-size:11px;">({award_counts.get(slug, 0)})</span></label>'
        for slug, label, emoji in AWARD_TAGS
    )

    # DEFAULT_OFF_GENRES (中/韩/西/南亚/中东·非洲) are not shown in the
    # cuisine filter — they're controlled by the standalone "隐藏非日本料理"
    # toggle below. Remaining buckets are grouped by MEAL_GROUPS with a
    # section header above each cluster.
    def _genre_section(group: str, buckets: list[str]) -> str:
        visible = [cat for cat in buckets if cat not in DEFAULT_OFF_GENRES]
        if not visible:
            return ""
        rows = "\n".join(
            f'        <label style="display:block;margin:1px 0;line-height:1.4;">'
            f'<input type="checkbox" name="ff-genre" value="{cat}" checked> '
            f'{cat} <span style="color:#6b7280;">({cat_counts.get(cat, 0)})</span></label>'
            for cat in visible
        )
        # Per-group 全选/全清 chips: same wiring as the section-wide ones,
        # scoped to checkboxes inside this wrapper via the data attribute.
        header = (
            f'        <div style="display:flex;justify-content:space-between;'
            f'align-items:baseline;margin:6px 0 2px;">'
            f'<span style="font-weight:600;color:#374151;font-size:11px;'
            f'letter-spacing:0.5px;">{group}</span>'
            f'<span style="font-size:10px;">'
            f'<a href="#" class="ff-group-all" style="color:#2563eb;text-decoration:none;">全选</a>'
            f'<span style="color:#d1d5db;"> | </span>'
            f'<a href="#" class="ff-group-none" style="color:#2563eb;text-decoration:none;">全清</a>'
            f"</span>"
            f"</div>"
        )
        return f'      <div data-genre-group="{group}">\n{header}\n{rows}\n      </div>'

    genre_rows = "\n".join(
        _genre_section(group, buckets) for group, buckets in MEAL_GROUPS.items()
    )
    foreign_count = sum(cat_counts.get(c, 0) for c in DEFAULT_OFF_GENRES)
    return f"""
<style>
  /* Filter bottom-sheet — same visual treatment as the restaurant detail
     sheet (#bs-sheet), with parallel width breakpoints. The two sheets are
     mutually exclusive (opening one closes the other), so they share the
     same vertical slot at the bottom of the viewport. */
  #ff-backdrop {{
    position: fixed; inset: 0; z-index: 10001;
    background: rgba(0,0,0,0.35);
    opacity: 0; pointer-events: none;
    transition: opacity 0.22s ease-out;
  }}
  #ff-backdrop.ff-open {{ opacity: 1; pointer-events: auto; }}
  #ff-sheet {{
    position: fixed; left: 0; right: 0; bottom: 0;
    z-index: 10002;
    max-height: 75vh; max-height: 75dvh;
    background: #fff;
    border-radius: 14px 14px 0 0;
    box-shadow: 0 -8px 24px rgba(0,0,0,0.18);
    transform: translateY(100%);
    transition: transform 0.25s ease-out;
    display: flex; flex-direction: column;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    padding-bottom: env(safe-area-inset-bottom);
  }}
  #ff-sheet.ff-open {{ transform: translateY(0); }}
  @media (min-width: 700px) {{
    #ff-sheet {{ left: 50%; transform: translate(-50%, 100%);
                 width: min(560px, calc(100vw - 32px)); right: auto;
                 max-height: 80vh; max-height: 80dvh;
                 border-radius: 14px 14px 0 0; }}
    #ff-sheet.ff-open {{ transform: translate(-50%, 0); }}
  }}
  @media (min-width: 1100px) {{
    #ff-sheet {{ width: min(640px, calc(100vw - 32px));
                 max-height: 85vh; max-height: 85dvh; }}
  }}
  #ff-grip {{
    position: relative;
    padding: 9px 0 6px; flex-shrink: 0;
    cursor: grab; touch-action: none;
  }}
  #ff-grip::before {{
    content: ''; display: block;
    width: 38px; height: 4px; margin: 0 auto;
    background: #d1d5db; border-radius: 2px;
  }}
  #ff-sheet-content {{
    overflow-y: auto;
    padding: 0 16px 16px;
    flex: 1 1 auto;
    -webkit-overflow-scrolling: touch;
    /* M-016 sibling fix M-170: stop the scroll chaining into the map once
       the panel hits its end — a flick that overshoots used to pan the map
       underneath the sheet. */
    overscroll-behavior: contain;
    font-size: 13px; line-height: 1.5; color: #111827;
  }}
  /* M-087: the panel's only live feedback ("筛选 · 显示 N / 9807") used to
     scroll away with the content while #ff-fab (which carries the same
     count) is hidden for as long as the sheet is open — so the second half
     of the controls was operated with no number on screen at all. The
     parent is already overflow-y:auto, so one sticky rule pins it. */
  #ff-sheet-content .ff-sheet-head {{
    position: sticky; top: 0; z-index: 1;
    background: #fff;
  }}
  /* Bottom-left FAB that opens the sheet. Matches the right-side .map-fab
     style but stands alone — labelled with the live filter count so the
     "how many results match" feedback survives the collapse to a sheet. */
  #ff-fab {{
    position: fixed;
    bottom: calc(18px + env(safe-area-inset-bottom)); left: 14px;
    z-index: 9995;
    background: #fff; color: #374151;
    border: 1px solid #d1d5db;
    border-radius: 999px;
    padding: 12px 18px;
    font-size: 15px; font-weight: 600;
    cursor: pointer;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    display: inline-flex; align-items: center; gap: 10px;
    user-select: none;
    transition: background 0.15s ease-out, box-shadow 0.15s ease-out;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    line-height: 1;
  }}
  #ff-fab:hover {{ background: #f9fafb;
                   box-shadow: 0 4px 10px rgba(0,0,0,0.18); }}
  #ff-fab[hidden] {{ display: none; }}
  #ff-fab .ff-fab-ic {{ display: inline-flex; align-items: center; }}
  #ff-fab .ff-fab-ic svg {{ display: block; }}
  #ff-fab .ff-fab-count {{ font-variant-numeric: tabular-nums; }}
  #ff-fab .ff-fab-count b {{ color: #2563eb; }}
  /* Unsynced-changes alerts. Two flavours so the colour matches the
     user's actual situation:
       .needs-sync         — red. Not signed in, so edits live only in
                              this browser. Urgent.
       .needs-sync-pending — blue. Signed in; the push just hasn't landed
                              yet (or last attempt failed, but the status
                              text covers that channel). Informational. */
  /* The "breathing" is an ::after overlay in the light shade fading over a
     static solid base — animating opacity alone stays on the compositor,
     unlike the old background/box-shadow keyframes which repainted the
     button on the main thread every frame for as long as the state lasted
     (signed-out-with-edits pulses for the whole session — that was a
     measurable battery cost on phones). Visual result is identical:
     overlay at 1 shows the light shade, at 0 the solid base. */
  @keyframes ff-fab-breathe {{
    0%, 100% {{ opacity: 1; }}
    50%      {{ opacity: 0; }}
  }}
  #ff-fab.needs-sync {{
    color: #fff; border-color: #dc2626; background: #dc2626;
    box-shadow: 0 3px 10px rgba(220,38,38,0.45);
  }}
  #ff-fab.needs-sync-pending {{
    color: #fff; border-color: #2563eb; background: #2563eb;
    box-shadow: 0 3px 10px rgba(37,99,235,0.45);
  }}
  /* M-057: the pulse used to be `infinite`, so a signed-out user with
     unsynced edits kept the compositor drawing for the whole session (the
     dirty flag is persisted, so it survived reloads too). Six blinks are
     plenty to catch the eye; the overlay then rests at opacity 0, i.e. the
     solid red/blue base — still unmistakable, and burning nothing. Base
     opacity 0 (not 1) is what makes the resting state the solid colour and
     keeps the white label legible; the animation itself starts at 1. */
  #ff-fab.needs-sync::after, #ff-fab.needs-sync-pending::after {{
    content: ''; position: absolute; inset: 0;
    border-radius: inherit; pointer-events: none;
    opacity: 0;
    animation: ff-fab-breathe 1.4s ease-in-out 6;
  }}
  #ff-fab.needs-sync::after {{ background: #fee2e2; }}
  #ff-fab.needs-sync-pending::after {{ background: #dbeafe; }}
  /* Keep the label above the overlay (matches the old white-on-color look
     through the whole cycle). */
  #ff-fab.needs-sync > *, #ff-fab.needs-sync-pending > * {{
    position: relative; z-index: 1;
  }}
  #ff-fab.needs-sync:hover::after, #ff-fab.needs-sync-pending:hover::after {{
    animation-play-state: paused; opacity: 0;
  }}
  #ff-fab.needs-sync:hover {{ background: #dc2626; }}
  #ff-fab.needs-sync-pending:hover {{ background: #2563eb; }}
  #ff-fab.needs-sync .ff-fab-count b {{ color: #fff; }}
  #ff-fab.needs-sync .ff-fab-count {{ color: #fff; }}
  #ff-fab.needs-sync-pending .ff-fab-count b {{ color: #fff; }}
  #ff-fab.needs-sync-pending .ff-fab-count {{ color: #fff; }}
  /* Small "?" badge next to bold subtitles inside the filter sheet.
     Inline-flex centers the glyph; cursor:help advertises that nothing
     destructive is going to happen on click. */
  .ff-help-trigger {{
    display: inline-flex;
    align-items: center; justify-content: center;
    width: 15px; height: 15px;
    padding: 0;
    border: none;
    border-radius: 50%;
    background: #e5e7eb;
    color: #6b7280;
    font: 700 10px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    cursor: help;
    user-select: none;
    margin-left: 5px;
    vertical-align: 1px;
    transition: background 0.12s ease-out, color 0.12s ease-out;
  }}
  .ff-help-trigger:hover,
  .ff-help-trigger:focus-visible {{
    background: #d1d5db; color: #111827; outline: none;
  }}
</style>
<!-- M-089: no static aria-label — it wins over the element's own content,
     so the visible "1234 / 9807" count was invisible to screen readers.
     updateFabAria() writes an aria-label that carries the count. -->
<button id="ff-fab" type="button" title="筛选">
  <span class="ff-fab-ic" aria-hidden="true"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg></span>
  <span class="ff-fab-count"><b class="ff-count">–</b> / <span class="ff-total">–</span></span>
</button>
<div id="ff-backdrop"></div>
<div id="ff-sheet" role="dialog" aria-modal="true" aria-hidden="true"
     aria-labelledby="ff-sheet-title">
  <div id="ff-grip"></div>
  <div id="ff-sheet-content">
  <div class="ff-sheet-head"
       style="display:flex;justify-content:space-between;align-items:center;
              border-bottom:1px solid #e5e7eb;padding:2px 0 8px;margin-bottom:10px;">
    <span id="ff-sheet-title" style="font-weight:700;font-size:15px;">筛选</span>
    <span style="font-size:12px;color:#6b7280;">
      显示 <b class="ff-count">–</b> / <span class="ff-total">–</span>
    </span>
  </div>

  <!-- M-028: filtering down to zero used to change nothing but the number —
       the restaurants vanished, the 219 sights stayed, and the page said
       nothing. Twin of #ff-empty-map (SYNC_UI_HTML); recompute() toggles
       both. Empty by default so it costs nothing until it is needed. -->
  <div id="ff-empty-panel" hidden>
    <div class="ffe-title">没有符合条件的餐厅</div>
    <div class="ffe-sub">筛选条件太严格了。放宽条件或重置筛选。</div>
    <button id="ff-empty-panel-reset" class="ffe-reset" type="button">重置筛选</button>
  </div>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">
    <span style="font-weight:600;">评分</span>
    <span style="font-size:11px;color:#374151;">≥ <b id="ff-rating-val">3.4</b></span>
  </div>
  <input type="range" id="ff-rating" min="3.4" max="4.5" step="0.05" value="3.4"
         style="width:100%;margin-bottom:6px;">

  <div style="display:flex;justify-content:space-between;align-items:baseline;">
    <span style="font-weight:600;">晚餐价格<button type="button" class="ff-help-trigger"
            data-help-for="price-curation" aria-label="说明" aria-haspopup="dialog">?</button></span>
    <span style="font-size:11px;">
      <a href="#" id="ff-price-all" style="color:#2563eb;text-decoration:none;">全选</a>
      <span style="color:#d1d5db;">|</span>
      <a href="#" id="ff-price-none" style="color:#2563eb;text-decoration:none;">全清</a>
    </span>
  </div>
{price_rows}

  <details id="ff-genre-box" style="margin-top:6px;margin-bottom:6px;">
    <summary style="cursor:pointer;list-style:none;display:flex;
                    justify-content:space-between;align-items:center;
                    padding:4px 6px;border:1px solid #d1d5db;border-radius:4px;
                    background:#f9fafb;font-size:12px;">
      <span style="font-weight:600;">菜系
        <span id="ff-genre-summary" style="font-weight:400;color:#6b7280;
              font-size:11px;margin-left:4px;">全部</span>
      </span>
      <span style="color:#6b7280;font-size:10px;">▾</span>
    </summary>
    <div style="margin-top:4px;font-size:11px;">
      <div style="display:flex;justify-content:flex-end;gap:6px;margin-bottom:3px;">
        <a href="#" id="ff-genre-all" style="color:#2563eb;text-decoration:none;">全选</a>
        <span style="color:#d1d5db;">|</span>
        <a href="#" id="ff-genre-none" style="color:#2563eb;text-decoration:none;">全清</a>
      </div>
      <div style="max-height:180px;overflow-y:auto;border:1px solid #e5e7eb;
                  border-radius:4px;padding:4px 6px;background:#fff;">
{genre_rows}
      </div>
    </div>
  </details>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px;margin-bottom:2px;">
    <span style="font-weight:600;">获奖</span>
    <span style="font-size:11px;">
      <a href="#" id="ff-award-none" style="color:#2563eb;text-decoration:none;">全清</a>
    </span>
  </div>
  <div style="font-size:10px;color:#6b7280;margin-bottom:4px;">
    勾选后只看对应获奖店；多选取并集，不勾则不限制
  </div>
  <div style="display:flex;flex-wrap:wrap;margin-bottom:6px;">
{award_rows}
  </div>

  <div style="font-weight:600;margin-top:6px;margin-bottom:2px;">Tabelog 预约</div>
  <label style="display:block;margin-bottom:6px;">
    <input type="checkbox" id="ff-bookable-only"> 只显示可以通过 Tabelog 预约
  </label>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px;margin-bottom:2px;">
    <span style="font-weight:600;">收藏</span>
    <span style="font-size:11px;color:#6b7280;">⭐ <b id="ff-fav-count">0</b></span>
  </div>
  <label style="display:block;margin-bottom:4px;">
    <input type="checkbox" id="ff-only-fav"> 只显示已收藏
  </label>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px;margin-bottom:2px;">
    <span style="font-weight:600;">弃用名单<button type="button" class="ff-help-trigger"
            data-help-for="blacklist" aria-label="说明" aria-haspopup="dialog">?</button></span>
    <span style="font-size:11px;color:#6b7280;">🚫 <b id="ff-black-count">0</b></span>
  </div>
  <label style="display:block;margin-bottom:6px;">
    <input type="checkbox" id="ff-hide-black" checked> 隐藏弃用名单
  </label>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px;margin-bottom:2px;">
    <span style="font-weight:600;">非日本料理</span>
    <span style="font-size:11px;color:#6b7280;">🌏 <b>{foreign_count}</b></span>
  </div>
  <label style="display:block;margin-bottom:6px;">
    <input type="checkbox" id="ff-hide-foreign" checked> 隐藏非日本料理（中餐、韩餐、西餐、南亚、中东菜等）
  </label>

  <!-- TEMP: Google-calibration filter. Delete this whole section (plus the
       ff-gcal-only wiring in FILTER_JS_TEMPLATE) once every restaurant is
       calibrated — see scrape/google_enrich.py. -->
  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px;margin-bottom:2px;">
    <span style="font-weight:600;">定位校准</span>
    <span style="font-size:11px;color:#6b7280;">🛰️ <b>{gcal_count}</b></span>
  </div>
  <label style="display:block;margin-bottom:6px;">
    <input type="checkbox" id="ff-gcal-only"> 只看谷歌地图校准过坐标的餐厅
  </label>

  <!-- M-028: a copy of the avatar menu's 重置筛选, at the bottom of the panel
       where a user who has just over-filtered is actually looking. The one in
       the avatar dropdown stays — same handler, two entry points. -->
  <button id="ff-panel-reset" type="button">↻ 重置筛选</button>

  <!-- Reset / Sync / Language used to live here as a 3-up grid; they moved
       to the avatar dropdown rooted in the search box so the filter sheet
       only carries filter controls. ff-sync-status follows them up there. -->
  </div>
</div>

<!-- The cloud-sync modal used to live here. Removed: Google's official
     sign-in button now renders directly inside the avatar dropdown (see
     #ssm-signin-btn in SEARCH_BOX_HTML), so there's no intermediate step
     to "open the modal" for anymore. -->

"""


# M-150: the manifest's cache-buster string used to be typed out twice (the
# page <link> and the SW app-shell list) and had to be kept in sync by hand.
# It lives here now; both references substitute __MANIFEST_V__.
MANIFEST_VERSION = "no-theme-color-1"

# Page title, install metadata, and launcher icons. The browser tab keeps the
# original inline 🗾 emoji favicon; the raster launcher icons use the same
# emoji at the sizes required by Chromium and Apple Home Screen installs.
HEAD_BRANDING = """
<title>Japan Foodmap</title>
<link rel="manifest" href="manifest.webmanifest?v=__MANIFEST_V__">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<!-- M-076: the page is a light-only UI (see `color-scheme: only light` in
     MOBILE_UX_ASSETS), so tell the browser chrome to match instead of
     picking its own tint. White, deliberately: commit 84835a8 removed the
     old red theme-color on purpose — do not put it back. -->
<meta name="theme-color" content="#ffffff">
<meta name="apple-mobile-web-app-title" content="Japan Foodmap">
<link rel="icon" type="image/svg+xml" href='data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🗾</text></svg>'>
<link rel="apple-touch-icon" sizes="180x180" href="apple-touch-icon-japan-emoji-v2.png">
<!-- Pre-warm TCP/TLS to the cross-origin hosts the page hits early.
     A preconnect only matches requests of the same CORS-ness: the
     `crossorigin` ones cover CORS fetches (GIS, the sync API), the bare
     ones cover classic tags — leaflet.js/.css from jsDelivr and
     MarkerCluster from cdnjs are render-blocking and were paying a cold
     TCP+TLS handshake before first paint. (No crossorigin jsDelivr
     preconnect: its only CORS consumer, emoji-picker-element, is now a
     lazy import.) Tile subdomains get dns-prefetch only: four extra
     sockets up front would compete with the critical path on slow links,
     but resolving the DNS early is free. -->
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<link rel="preconnect" href="https://cdnjs.cloudflare.com">
<link rel="preconnect" href="https://emojicdn.elk.sh" crossorigin>
<link rel="preconnect" href="https://accounts.google.com" crossorigin>
<link rel="preconnect" href="https://api.jpfoodmap.com" crossorigin>
<link rel="preconnect" href="https://tblg.k-img.com">
<link rel="dns-prefetch" href="https://a.basemaps.cartocdn.com">
<link rel="dns-prefetch" href="https://b.basemaps.cartocdn.com">
<link rel="dns-prefetch" href="https://c.basemaps.cartocdn.com">
<link rel="dns-prefetch" href="https://d.basemaps.cartocdn.com">
<!-- The 3MB marker payload used to start downloading only when the boot
     script ran at DOMContentLoaded — seconds after the HTML arrived. The
     preload starts it with the document; boot()'s fetch then picks the
     response out of the preload/HTTP cache (same-origin + same
     credentials mode, so it matches). -->
<link rel="preload" href="data/restaurants.json" as="fetch">
<script src="https://accounts.google.com/gsi/client" async defer></script>
"""
HEAD_BRANDING = HEAD_BRANDING.replace("__MANIFEST_V__", MANIFEST_VERSION)  # M-150

# Web OAuth client ID for jpfoodmap (Google Cloud project: tabelog-map).
# Public by design — gets inlined into the page JS so the GIS library knows
# which app is asking for a sign-in. Not a secret; safe in git.
GOOGLE_CLIENT_ID = "536198170238-me7dpu2og75tseuekl3pu8rjjgo2ig2p.apps.googleusercontent.com"


LOCATE_ASSETS = """
<meta name="robots" content="noindex,nofollow,noarchive,nosnippet">
<meta name="googlebot" content="noindex,nofollow,noarchive,nosnippet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet.locatecontrol@0.79.0/dist/L.Control.Locate.min.css"/>
<script defer src="https://cdn.jsdelivr.net/npm/leaflet.locatecontrol@0.79.0/dist/L.Control.Locate.min.js"></script>
<script defer src="transit-layer.js"></script>
<style>
  /* Suppress iOS long-press callout + text-selection on the map so the
     contextmenu handler fires cleanly on touch. The font-size on
     .leaflet-container is patched separately in the post-save pass
     below, where folium's own `font-size: 1rem` rule is replaced — a
     <style> rule there always wins over anything we inject here
     because it renders later in source order. */
  .leaflet-container {
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
  }
  /* The locate plugin renders its own top-left button; we drive it from
     the bottom-right FAB stack instead, so suppress the default UI. The
     control instance stays alive for its .start() / .stop() methods. */
  .leaflet-control-locate { display: none !important; }
</style>
"""


# Page-level zoom lock + iOS Safari bounce kill. The viewport meta folium
# emits already has user-scalable=no, but iOS Safari has ignored that
# since iOS 10 for accessibility, so we need event listeners too.
# Leaflet uses raw touch events for its own map gestures, not iOS gesture*
# events, so blocking gesture* on the document does NOT break map pinch.
MAP_FAB_HTML = """
<style>
  /* Floating layer-control replacement (Google-Maps-style pills, bottom-right).
     Container is pointer-events:none so the gaps don't block map drags;
     each button re-enables pointer events. */
  .map-fab-stack {
    position: fixed;
    bottom: calc(18px + env(safe-area-inset-bottom)); right: 14px;
    z-index: 9995;
    display: flex; flex-direction: column; gap: 8px;
    pointer-events: none;
  }
  .map-fab {
    pointer-events: auto;
    background: #fff; color: #374151;
    border: 1px solid #d1d5db;
    border-radius: 999px;
    padding: 8px 14px;
    font-size: 13px; font-weight: 600;
    cursor: pointer;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    display: inline-flex; align-items: center; gap: 6px;
    user-select: none;
    transition: background 0.15s ease-out, box-shadow 0.15s ease-out,
                color 0.15s ease-out, border-color 0.15s ease-out;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    line-height: 1;
  }
  .map-fab:hover { background: #f9fafb;
                   box-shadow: 0 4px 10px rgba(0,0,0,0.18); }
  .map-fab.active { background: #2563eb; color: #fff;
                    border-color: #2563eb; }
  .map-fab.active:hover { background: #1d4ed8; }
  /* Third state for fab-attractions only — "show all including hidden".
     Amber/orange signals "extra/special mode" without alarming like red. */
  .map-fab.show-all { background: #f59e0b; color: #fff;
                      border-color: #f59e0b; }
  .map-fab.show-all:hover { background: #d97706; }
  /* Built-in landmarks the user has hidden via the popup. Default state:
     hidden entirely. When the body carries .attr-show-all (fab-attractions
     in its third state) they re-appear ghosted so they can be un-hidden. */
  .leaflet-marker-icon.bm-mk-hidden { display: none; }
  body.attr-show-all .leaflet-marker-icon.bm-mk-hidden {
    display: block;
    opacity: 0.45;
    filter: grayscale(1);
  }
  /* Low-zoom collapsed marker. At zoom < ZOOM_LOW_THRESHOLD (Python side,
     mirrored in JS) the full emoji+label hides and a 16px bare emoji
     stands in — same size as the restaurant cluster icons, but with no
     halo / no label / no drop-shadow circle, so 200+ markers stay
     readable at Japan-wide zoom while preserving the category cue
     (寺 / ⛩️ / ♨️ / 🗼 etc.). */
  .bm-mk-dot {
    display: none;
    position: relative;
    width: 16px; height: 16px;
    transform: translate(-50%, -50%);
    line-height: 0;
  }
  .bm-mk-dot img { width: 16px; height: 16px; display: block; }
  body.zoom-low .bm-mk .bm-mk-full { display: none; }
  body.zoom-low .bm-mk .bm-mk-dot  { display: block; }
  .map-fab-ic { font-size: 15px; line-height: 1; }
  /* Locate button is icon-only on every viewport — round, no label. The
     SVG keeps it visually distinct from the pill-shaped layer toggles. */
  .map-fab.map-fab-circle {
    width: 40px; height: 40px; padding: 0;
    border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
    align-self: flex-end;       /* line up flush with the pill stack edge */
  }
  .map-fab .map-fab-svg {
    width: 20px; height: 20px; display: block;
    stroke: currentColor;
  }
  /* While the plugin is following the user, paint the FAB blue. The class
     is added/removed by locateactivate/locatedeactivate map events. */
  .map-fab.map-fab-circle.locating { background: #2563eb; color: #fff;
                                     border-color: #2563eb; }
  /* Tighten on narrow screens — drop the label, keep just the icon. */
  @media (max-width: 480px) {
    .map-fab { padding: 9px 10px; }
    .map-fab-label { display: none; }
    .map-fab-ic { font-size: 17px; }
  }
</style>
<div class="map-fab-stack" role="group" aria-label="图层切换">
  <button id="fab-locate" class="map-fab map-fab-circle" type="button"
          title="定位到我的位置" aria-label="定位到我的位置">
    <svg class="map-fab-svg" viewBox="0 0 24 24" fill="none"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
         aria-hidden="true">
      <circle cx="12" cy="12" r="2.5" fill="currentColor" stroke="none"></circle>
      <circle cx="12" cy="12" r="7.5"></circle>
      <line x1="12" y1="1.5" x2="12" y2="4"></line>
      <line x1="12" y1="20" x2="12" y2="22.5"></line>
      <line x1="1.5" y1="12" x2="4" y2="12"></line>
      <line x1="20" y1="12" x2="22.5" y2="12"></line>
    </svg>
  </button>
  <button id="fab-transit-long" class="map-fab" type="button"
          aria-pressed="false" title="新干线 / JR 长途线路">
    <span class="map-fab-ic">🚄</span><span class="map-fab-label">长途</span>
  </button>
  <button id="fab-transit-city" class="map-fab" type="button"
          aria-pressed="false" title="地铁 / 私铁 / 城市轨道">
    <span class="map-fab-ic">🚇</span><span class="map-fab-label">市内</span>
  </button>
  <button id="fab-attractions" class="map-fab active" type="button"
          aria-pressed="true" title="景点锚点">
    <span class="map-fab-ic">🗾</span><span class="map-fab-label">景点</span>
  </button>
  <button id="fab-bookmarks" class="map-fab active" type="button"
          aria-pressed="true" title="我的收藏">
    <span class="map-fab-ic">⭐</span><span class="map-fab-label">收藏</span>
  </button>
</div>
"""


# Top-center floating search box. Hits Nominatim (OSM) and drops the
# results into a clickable dropdown. CORS-safe from the browser; rate
# limited by the JS-side debounce (~300ms per keystroke).
SEARCH_BOX_HTML = """
<style>
  #ss-box {
    position: fixed;
    top: max(12px, env(safe-area-inset-top)); left: 50%;
    transform: translateX(-50%);
    z-index: 9996;
    width: min(calc(100vw - 32px), 380px);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }
  /* M-073: the search box sits *below* the two bottom sheets (10002), so an
     open restaurant card covered the result dropdown and the account menu —
     on a short window every single row was unreachable. Lift the whole box
     while something is dropped out of it, and only then: 10003 clears both
     sheets but stays under #bm-backdrop (10010), so the bookmark modal's
     scrim still means "nothing else is clickable".
     Two selectors, deliberately not one list: the class is toggled by
     ssFinalize/ssCloseDropdown for the search dropdown, and the :has()
     rule covers the account menu without reaching into its handler. A
     browser without :has() simply drops that second rule. */
  #ss-box.ss-open {
    z-index: 10003;
  }
  #ss-box:has(#ss-menu.open) {
    z-index: 10003;
  }
  #ss-input-wrap {
    position: relative; display: flex; align-items: center;
    background: #fff; border: 1px solid #d1d5db; border-radius: 22px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    transition: box-shadow 0.15s ease-out;
  }
  #ss-input-wrap:focus-within {
    box-shadow: 0 4px 14px rgba(0,0,0,0.22);
    border-color: #93c5fd;
  }
  #ss-icon {
    padding-left: 14px; color: #6b7280; font-size: 14px;
    line-height: 1; user-select: none;
  }
  #ss-input {
    flex: 1; min-width: 0;
    padding: 9px 6px 9px 8px;
    border: none; outline: none; background: transparent;
    font-size: 14px; color: #1f2937;
    font-family: inherit;
  }
  #ss-input::placeholder { color: #9ca3af; }
  #ss-clear {
    border: none; background: none; cursor: pointer;
    color: #6b7280; font-size: 20px; line-height: 1;
    padding: 8px 14px 8px 8px;
    display: none;
    -webkit-tap-highlight-color: transparent;
  }
  #ss-clear:hover, #ss-clear:active { color: #1f2937; }
  #ss-input-wrap.has-text #ss-clear,
  #ss-input-wrap.searching #ss-clear { display: block; }
  #ss-spinner {
    display: none;
    width: 14px; height: 14px;
    border: 2px solid #e5e7eb; border-top-color: #2563eb;
    border-radius: 50%;
    margin-right: 12px;
    animation: ss-spin 0.8s linear infinite;
  }
  #ss-input-wrap.busy #ss-spinner { display: block; }
  /* M-082: the spinner used to REPLACE the ×, so the worse the network,
     the harder it was to get out of a search — and a phone has no Escape
     key. They sit side by side now; the spinner just gives back some of
     its right margin when both are on screen. */
  #ss-input-wrap.busy.has-text #ss-spinner,
  #ss-input-wrap.busy.searching #ss-spinner { margin-right: 4px; }
  @keyframes ss-spin { to { transform: rotate(360deg); } }
  #ss-list {
    margin-top: 6px;
    background: #fff;
    border: 1px solid #d1d5db; border-radius: 10px;
    box-shadow: 0 6px 16px rgba(0,0,0,0.16);
    overflow: hidden;
    display: none;
    /* Cap at ~6 rows + 2 section headers; scroll inside when there are
       more matches. Hard-clamped to viewport on small screens so we
       never overflow the mobile bottom edge. */
    max-height: min(360px, 70dvh);
    overflow-y: auto;
    /* M-170: keep an overscrolling flick inside the list instead of
       chaining it into a map pan. */
    overscroll-behavior: contain;
    -webkit-overflow-scrolling: touch;
  }
  #ss-list.open { display: block; }
  #ss-list .ss-row {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 10px;
    border-bottom: 1px solid #f3f4f6;
    cursor: pointer;
    transition: background 0.1s ease-out;
  }
  #ss-list .ss-row:last-child { border-bottom: none; }
  #ss-list .ss-row:hover { background: #f9fafb; }
  /* Keyboard-highlighted row — slightly stronger than hover so both can
     coexist without ambiguity about which one Enter will pick. */
  #ss-list .ss-row.ss-active { background: #eff6ff; }
  #ss-list .ss-row.ss-empty {
    cursor: default; color: #6b7280; font-size: 12px;
    justify-content: center; padding: 14px 10px;
  }
  #ss-list .ss-row.ss-empty:hover { background: transparent; }
  #ss-list .ss-row.ss-error { color: #b91c1c; }
  #ss-list .ss-text { flex: 1; min-width: 0; }
  #ss-list .ss-name {
    font-size: 13px; font-weight: 600; color: #1f2937;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  #ss-list .ss-addr {
    font-size: 11px; color: #6b7280;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    margin-top: 1px;
  }
  #ss-list .ss-fav {
    flex-shrink: 0;
    background: #f9fafb; border: 1px solid #d1d5db;
    border-radius: 6px; cursor: pointer;
    font-size: 14px; line-height: 1;
    padding: 5px 8px; color: #374151;
  }
  #ss-list .ss-fav:hover { background: #fef3c7; border-color: #facc15; }
  #ss-list .ss-icon {
    flex-shrink: 0; font-size: 16px; line-height: 1; width: 20px;
    text-align: center; color: #6b7280;
  }
  #ss-list .ss-icon img { width: 16px; height: 16px; vertical-align: -3px; }
  #ss-list .ss-section-head {
    padding: 4px 12px;
    font-size: 10px; font-weight: 700; letter-spacing: 0.04em;
    color: #6b7280; background: #f3f4f6;
    text-transform: uppercase;
  }
  /* Viewport-bias sub-header — visible only when zoomed in enough that
     splitting "屏幕内 / 其他区域" inside the 餐厅库 section is useful. */
  #ss-list .ss-subsection-head {
    padding: 2px 18px;
    font-size: 10px; font-weight: 600;
    color: #6b7280; background: #fafafa;
    border-bottom: 1px solid #f3f4f6;
  }
  #ss-list .ss-rating {
    flex-shrink: 0; font-size: 11px; font-weight: 600;
    color: #b45309; padding: 0 6px;
  }
  /* M-081 (hard prerequisite for M-018): iOS Safari zooms the page in on
     focus whenever an input renders below 16px, and never zooms back out.
     The rule used to live inside the 480px block, which excluded every
     touch device wider than a phone — iPad mini (744), iPad Air (1180),
     iPhone landscape (852), Fold 8 inner screen (616/816). Keyed off the
     pointer instead, it now covers all of them. Kept separate from the
     layout block below on purpose: widening #ss-box to calc(100vw - 16px)
     on an iPad would stretch the search box across the whole screen. */
  @media (max-width: 480px), (hover: none) and (pointer: coarse) {
    #ss-input { font-size: 16px; }       /* iOS no-zoom */
  }
  @media (max-width: 480px) {
    #ss-box { top: 8px; width: calc(100vw - 16px); }
  }
  /* Top row: search input + avatar side by side. Restructured from a single
     input-wrap so the avatar can anchor a Google-style account dropdown on
     its right (#ss-menu below), without disturbing the result list layout. */
  #ss-top { display: flex; align-items: center; gap: 8px; position: relative; }
  #ss-input-wrap { flex: 1; min-width: 0; }
  #ss-avatar {
    flex-shrink: 0;
    width: 36px; height: 36px;
    padding: 0; border: 1px solid #d1d5db; border-radius: 50%;
    background: #fff; box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    cursor: pointer; overflow: hidden;
    display: inline-flex; align-items: center; justify-content: center;
    -webkit-tap-highlight-color: transparent;
    transition: box-shadow 0.15s ease-out;
  }
  #ss-avatar:hover { box-shadow: 0 4px 14px rgba(0,0,0,0.22); }
  #ss-avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
  /* Account / settings dropdown opened from #ss-avatar. Anchored to the
     search box's right edge so it lines up under the avatar on both desktop
     and mobile (the box is centered, so 'right:0' tracks correctly). */
  #ss-menu {
    position: absolute; top: 48px; right: 0;
    /* M-072: the menu had no max-height and no overflow, and its top edge
       is pinned 48px under a fixed box — so on a short window (Fold 8 outer
       screen, phone landscape) its lower rows, sync status first, were
       simply cut off with no way to reach them, and on a narrow one the
       fixed 260px pushed off the left edge. Cap it to the viewport and let
       it scroll. The vh pair is a fallback for the dvh pair; the GIS
       sign-in button is an iframe and is happy inside a scroll container. */
    width: min(260px, calc(100vw - 24px));
    max-height: calc(100vh - 72px);
    max-height: calc(100dvh - max(12px, env(safe-area-inset-top)) - 60px);
    overflow-y: auto;
    overscroll-behavior: contain;
    -webkit-overflow-scrolling: touch;
    background: #fff;
    border: 1px solid #e5e7eb; border-radius: 12px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.20);
    padding: 8px;
    font-size: 13px; color: #1f2937;
    display: none;
  }
  #ss-menu.open { display: block; }
  /* Account info row — display-only now that sign-out lives below it as its
     own row, so no pointer / hover. */
  .ssm-acct {
    display: flex; align-items: center; gap: 10px;
    padding: 8px; border-radius: 8px;
    color: #1f2937; font: inherit;
  }
  .ssm-row {
    display: flex; align-items: center; gap: 10px;
    padding: 8px; border-radius: 8px; cursor: pointer;
    border: none; background: none; color: #1f2937;
    font: inherit; text-align: left; width: 100%;
    -webkit-tap-highlight-color: transparent;
  }
  .ssm-row:hover { background: #f3f4f6; }
  .ssm-acct img {
    width: 32px; height: 32px; border-radius: 50%;
    flex-shrink: 0; background: #e5e7eb; object-fit: cover;
  }
  .ssm-acct-text { min-width: 0; flex: 1; line-height: 1.3; display: block; }
  .ssm-acct-name {
    display: block; font-weight: 600;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .ssm-acct-email {
    display: block; font-size: 11px; color: #6b7280;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  /* Signed-out pane — Google's GIS button gets injected into #ssm-signin-btn
     at runtime; the help paragraph below explains what signing in does. */
  #ssm-signed-out { padding: 6px 4px 2px; }
  #ssm-signin-btn { display: flex; justify-content: center; min-height: 40px; }
  #ssm-cfg-msg {
    font-size: 11px; text-align: center; color: #6b7280;
    min-height: 14px; padding: 4px 0;
  }
  .ssm-signin-help {
    font-size: 11px; color: #6b7280; line-height: 1.5;
    margin: 4px 8px 6px; padding: 0;
    text-align: center;
  }
  #ssm-signout { color: #b91c1c; }
  #ssm-signout:hover { background: #fef2f2; }
  .ssm-divider { height: 1px; background: #f3f4f6; margin: 6px 0; }
  .ssm-section-lbl { font-size: 11px; color: #6b7280; padding: 4px 8px 2px; }
  .ssm-langs { display: flex; gap: 4px; padding: 0 4px 4px; }
  .ssm-langs button {
    flex: 1; padding: 6px 0;
    border: 1px solid #d1d5db; background: #f9fafb;
    border-radius: 6px; cursor: pointer;
    font: inherit; font-size: 11px; color: #374151;
  }
  .ssm-langs button:hover { background: #eef2ff; }
  .ssm-langs button.on { background: #2563eb; color: #fff; border-color: #2563eb; }
  /* Keep ff-sync-status's id so the existing JS that mutates its text
     ("本地模式" / "已同步" / etc.) doesn't have to be rewired — only its
     parent moved from the filter sheet up here into the avatar menu. */
  #ss-menu #ff-sync-status {
    font-size: 10px; color: #6b7280; text-align: center;
    padding: 6px 0 2px; min-height: 13px;
  }
</style>
<div id="ss-box">
  <div id="ss-top">
    <div id="ss-input-wrap">
      <span id="ss-icon">🔍</span>
      <input id="ss-input" type="text" autocomplete="off"
             role="combobox" aria-expanded="false" aria-controls="ss-list"
             aria-autocomplete="list"
             placeholder="搜索餐厅 / 景点 / 地址 ...">
      <div id="ss-spinner"></div>
      <button id="ss-clear" type="button" aria-label="清空">×</button>
    </div>
    <button id="ss-avatar" type="button" aria-label="账户">
      <img id="ss-avatar-pic" src="img/default-avatar-v2.png" alt=""
           referrerpolicy="no-referrer">
    </button>
    <!-- M-034: unsynced / failing state gets a badge on the avatar, since
         that's where the sync UI lives. Sibling rather than a child because
         #ss-avatar is overflow:hidden (round crop). Styled in SYNC_UI_HTML;
         purely decorative — the words are in #sync-sr / #sync-banner. -->
    <span id="ss-avatar-dot" hidden aria-hidden="true"></span>
    <div id="ss-menu" role="menu" hidden>
      <!-- Signed-in pane: account info (info-only) + sign-out. -->
      <div id="ssm-signed-in" hidden>
        <div class="ssm-acct">
          <img id="ssm-acct-pic" alt="" referrerpolicy="no-referrer">
          <span class="ssm-acct-text">
            <span class="ssm-acct-name" id="ssm-acct-name">—</span>
            <span class="ssm-acct-email" id="ssm-acct-email">—</span>
          </span>
        </div>
        <button class="ssm-row" id="ssm-signout" type="button">
          <span aria-hidden="true">⎋</span><span>退出登录</span>
        </button>
      </div>
      <!-- Signed-out pane: Google's official sign-in button is rendered into
           #ssm-signin-btn by GIS, then a one-line status slot, then a help
           paragraph explaining what signing in actually does. -->
      <div id="ssm-signed-out" hidden>
        <div id="ssm-signin-btn"></div>
        <div id="ssm-cfg-msg"></div>
        <p class="ssm-signin-help" id="ssm-signin-help">登录后，收藏 / 弃用 / 景点 会跨设备同步。未登录则只存在当前浏览器。</p>
      </div>
      <div class="ssm-divider"></div>
      <button class="ssm-row" id="ssm-reset" type="button">
        <span aria-hidden="true">↻</span><span>重置筛选</span>
      </button>
      <div class="ssm-divider"></div>
      <div class="ssm-section-lbl">备份 / 迁移</div>
      <button class="ssm-row" id="ssm-export" type="button">
        <span aria-hidden="true">↓</span><span>导出 favorites.json</span>
      </button>
      <button class="ssm-row" id="ssm-import" type="button">
        <span aria-hidden="true">↑</span><span>导入 favorites.json</span>
      </button>
      <input id="ssm-import-file" type="file" accept="application/json,.json" hidden>
      <div class="ssm-divider"></div>
      <!-- M-008: the site stores a Google sub / email / name / avatar URL in
           Cloudflare KV; until now there was no policy page and no way to ask
           for it back. The delete button is the ONLY place in this codebase
           allowed to clear localStorage, and only after the Worker confirms
           the cloud copy is gone (204). -->
      <div class="ssm-section-lbl">隐私与数据</div>
      <a class="ssm-row" id="ssm-privacy-link" href="privacy.html"
         target="_blank" rel="noopener">
        <span aria-hidden="true">↗</span><span>隐私政策</span>
      </a>
      <button class="ssm-row" id="ssm-delete-cloud" type="button">
        <span aria-hidden="true">✕</span><span id="ssm-delete-label">删除我的云端数据</span>
      </button>
      <div id="ssm-delete-msg" role="status"></div>
      <div class="ssm-divider"></div>
      <div class="ssm-section-lbl">语言</div>
      <div class="ssm-langs" role="group">
        <button type="button" data-lang="zh-CN">简体</button>
        <button type="button" data-lang="zh-TW">繁體</button>
        <button type="button" data-lang="en">EN</button>
        <button type="button" data-lang="ja">日本語</button>
      </div>
      <!-- M-089: role=status so sync state changes are announced. -->
      <div id="ff-sync-status" role="status">本地模式</div>
    </div>
  </div>
  <!-- Two sub-containers so the async Nominatim response only rewrites its
       own section — the local restaurant rows (and the list's scroll
       position) survive untouched. -->
  <div id="ss-list" role="listbox"><div id="ss-local"></div><div id="ss-api"></div></div>
</div>
"""


# Right-click → 加入收藏 modal. Two inputs (name + emoji) and a row of
# preset emoji chips, plus a full emoji picker (Web Component from
# CDN). The picker uses Shadow DOM, so the page-wide MutationObserver
# that swaps emoji glyphs for Apple-CDN images can't reach inside it —
# the picker renders system glyphs natively, which is exactly what its
# search index expects.
# Help popover for the "?" badges that sit next to subtitles inside the
# filter sheet. The element lives at body level (not inside #ff-sheet) so
# position:fixed isn't trapped by the sheet's transform context. Help
# copy for each subject lives as a sibling <div data-help-for="...">; the
# JS just toggles which section is visible and where the popover floats.
# Keeping the text inline (rather than templated / fetched) makes sure
# the boot-time localizeTree pass picks up the CJK runs and translates
# them via TEXT_EN_MAP / TEXT_JA_MAP / TEXT_TRAD_MAP without us having
# to wire anything language-specific into the popover JS.
HELP_POPOVER_HTML = """
<style>
  #ff-help-pop {
    position: fixed;
    z-index: 10030;
    max-width: 260px;
    background: #1f2937; color: #f3f4f6;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 12px; line-height: 1.55;
    padding: 9px 11px;
    border-radius: 7px;
    box-shadow: 0 6px 18px rgba(0,0,0,0.32);
    opacity: 0;
    transform: translateY(-2px);
    transition: opacity 0.13s ease-out, transform 0.13s ease-out;
  }
  #ff-help-pop[hidden] { display: none; }
  #ff-help-pop.ff-help-show {
    opacity: 1;
    transform: translateY(0);
  }
  #ff-help-pop::before {
    content: '';
    position: absolute;
    top: -5px; left: 14px;
    border-style: solid;
    border-width: 0 5px 5px 5px;
    border-color: transparent transparent #1f2937 transparent;
  }
  .ff-help-section[hidden] { display: none; }
</style>
<div id="ff-help-pop" role="tooltip" aria-live="polite" hidden>
  <div class="ff-help-section" data-help-for="blacklist" hidden>弃用名单 = 你研究后决定不会去的餐厅，会从地图上自动隐藏；和收藏一起通过你的 Google 账号跨设备同步。</div>
  <div class="ff-help-section" id="help-kind-bookmark" data-help-for="kind-bookmark" hidden>收藏 = 你自己想标注的地点或建筑物；和景点一起通过你的 Google 账号跨设备同步。</div>
  <div class="ff-help-section" id="help-kind-attraction" data-help-for="kind-attraction" hidden>景点 = 系统默认旅游锚点之外你自己加的去处，与默认景点一起在地图上显示，可随时删除；和收藏一起通过你的 Google 账号跨设备同步。</div>
  <div class="ff-help-section" data-help-for="price-curation" hidden>本站只收录每个区域评分前 1% 的餐厅；其中高价位 fine-dining 进一步压到 0.1%；和果子、咖啡厅、面包店等非正餐也按比例控量。这样，在地图上显示出来的 20000 日元以下的高分正餐餐厅，多数可以在一个合理的天数内提前预订，甚至 walk-in。</div>
</div>
"""


BOOKMARKS_MODAL_HTML = """
<!-- emoji-picker-element is dynamically import()ed on first picker expand
     (see ensurePickerModule in FILTER_JS) — the eager module tag here cost
     every visitor a ~2.6s CDN fetch for a feature almost nobody opens. -->
<style>
  #bm-backdrop {
    position: fixed; inset: 0; z-index: 10010;
    background: rgba(0,0,0,0.35);
    opacity: 0; pointer-events: none;
    transition: opacity 0.2s ease-out;
  }
  #bm-backdrop.bm-open { opacity: 1; pointer-events: auto; }
  #bm-modal {
    position: fixed; left: 50%; top: 50%;
    transform: translate(-50%, -50%) scale(0.96);
    z-index: 10011;
    width: min(92vw, 340px);
    max-height: 90vh; max-height: 90dvh;
    background: #fff;
    border-radius: 10px;
    box-shadow: 0 12px 32px rgba(0,0,0,0.25);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: #1f2937;
    opacity: 0; pointer-events: none;
    transition: opacity 0.18s ease-out, transform 0.18s ease-out;
    display: flex; flex-direction: column;
  }
  #bm-modal.bm-open {
    opacity: 1; pointer-events: auto;
    transform: translate(-50%, -50%) scale(1);
  }
  #bm-modal .bm-head {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid #e5e7eb;
    flex-shrink: 0;
  }
  #bm-modal .bm-title { font-weight: 700; font-size: 14px; }
  #bm-modal .bm-close {
    background: none; border: none; cursor: pointer;
    font-size: 20px; line-height: 1; color: #9ca3af;
    padding: 2px 6px;
  }
  #bm-modal .bm-close:hover { color: #374151; }
  #bm-modal .bm-body {
    padding: 12px 14px; font-size: 13px;
    overflow-y: auto; flex: 1 1 auto; min-height: 0;
    -webkit-overflow-scrolling: touch;
  }
  #bm-modal .bm-coord {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color: #6b7280; font-size: 12px;
    margin-bottom: 10px; text-align: center;
  }
  #bm-modal .bm-row {
    display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
  }
  #bm-modal .bm-row > label {
    width: 42px; flex-shrink: 0; color: #6b7280; font-size: 12px;
  }
  #bm-modal .bm-row > input {
    flex: 1; min-width: 0;
    padding: 6px 8px;
    border: 1px solid #d1d5db; border-radius: 5px;
    font-size: 13px; font-family: inherit;
    box-sizing: border-box;
  }
  #bm-modal #bm-emoji {
    flex: 1; min-width: 0;
    text-align: left; font-size: 18px;
  }
  /* Phones: 16px inputs suppress iOS focus-zoom (same fix #ss-input got),
     and the modal anchors to the upper part of the screen instead of
     center — fixed centering is relative to the layout viewport, so with
     the keyboard up the lower half (emoji row, 保存/取消) sat behind it. */
  /* M-081: the 16px anti-zoom rule applies to every touch device, not just
     the ≤480px ones (see the same split in SEARCH_BOX_HTML); the keyboard-
     avoidance repositioning stays phone-only. */
  @media (max-width: 480px), (hover: none) and (pointer: coarse) {
    #bm-modal .bm-row > input { font-size: 16px; }
    #bm-modal #bm-emoji { font-size: 18px; }
  }
  @media (max-width: 480px) {
    #bm-modal { top: 7dvh; transform: translate(-50%, 0) scale(0.96); }
    #bm-modal.bm-open { transform: translate(-50%, 0) scale(1); }
  }
  /* "常用" quick-pick row — small label + chip buttons, on a tinted
     panel so the section is visually separate from the typed input. */
  #bm-modal .bm-quick {
    display: flex; align-items: center; gap: 10px;
    padding: 7px 10px; margin-bottom: 8px;
    background: #f9fafb; border: 1px solid #f3f4f6; border-radius: 6px;
  }
  #bm-modal .bm-quick-label {
    font-size: 11px; color: #6b7280; font-weight: 600;
    flex-shrink: 0;
  }
  #bm-modal .bm-quick-list {
    display: flex; gap: 6px; flex-wrap: wrap;
  }
  #bm-modal .bm-quick-list button {
    background: #fff; border: 1px solid #d1d5db; border-radius: 5px;
    cursor: pointer; font-size: 16px; line-height: 1;
    padding: 4px 7px;
    font-family: inherit;
  }
  #bm-modal .bm-quick-list button:hover {
    background: #eff6ff; border-color: #93c5fd;
  }
  /* Full-picker toggle sits inside .bm-quick-list as a 7th chip, but
     tinted blue so it reads as "open a different surface" rather than
     "another preset". */
  #bm-modal #bm-emoji-more {
    background: #eff6ff; border-color: #bfdbfe;
  }
  #bm-modal #bm-emoji-more:hover {
    background: #dbeafe; border-color: #93c5fd;
  }
  #bm-modal #bm-emoji-picker {
    display: none;
    width: 100%;
    height: 280px;
    margin-top: 8px;
    /* Tokens consumed by emoji-picker-element's shadow DOM. */
    --background: #fff;
    --border-color: #e5e7eb;
    --border-radius: 8px;
    --emoji-size: 1.15rem;
    --num-columns: 8;
  }
  #bm-modal #bm-emoji-picker.bm-show { display: block; }
  #bm-modal .bm-error {
    color: #b91c1c; font-size: 12px;
    min-height: 16px; margin-top: 8px;
  }
  #bm-modal .bm-foot {
    display: flex; justify-content: flex-end; gap: 8px;
    padding: 8px 14px 12px;
    flex-shrink: 0;
    border-top: 1px solid #f3f4f6;
  }
  #bm-modal .bm-foot button {
    padding: 6px 14px; border-radius: 5px; cursor: pointer;
    font-size: 13px; font-weight: 600; border: 1px solid #d1d5db;
    background: #f9fafb; color: #1f2937;
  }
  #bm-modal .bm-foot button.bm-save {
    background: #2563eb; border-color: #2563eb; color: #fff;
  }
  #bm-modal .bm-foot button.bm-save:hover { background: #1d4ed8; }
  #bm-modal .bm-foot button.bm-cancel:hover { background: #f3f4f6; }
  /* 类型 segmented control: two buttons sharing one rounded shell, the
     active one paints blue. Same shell width as a single text input so
     it lines up with the rest of the form. */
  #bm-modal .bm-kind-seg {
    flex: 1; min-width: 0;
    display: flex;
    border: 1px solid #d1d5db; border-radius: 6px;
    overflow: hidden;
    background: #fff;
  }
  #bm-modal .bm-kind-seg button {
    flex: 1; min-width: 0;
    background: transparent; border: none; cursor: pointer;
    font-family: inherit; font-size: 13px; font-weight: 600;
    color: #6b7280;
    padding: 7px 8px;
    transition: background 0.12s ease-out, color 0.12s ease-out;
  }
  #bm-modal .bm-kind-seg button + button {
    border-left: 1px solid #d1d5db;
  }
  #bm-modal .bm-kind-seg button:hover:not(.active) {
    background: #f9fafb; color: #374151;
  }
  #bm-modal .bm-kind-seg button.active {
    background: #2563eb; color: #fff;
  }
</style>
<div id="bm-backdrop"></div>
<div id="bm-modal" role="dialog" aria-modal="true" aria-hidden="true"
     aria-labelledby="bm-modal-title">
  <div class="bm-head">
    <span class="bm-title" id="bm-modal-title">加入收藏</span>
    <button class="bm-close" aria-label="关闭">×</button>
  </div>
  <div class="bm-body">
    <div class="bm-coord" id="bm-coord" lang="en"></div>
    <div class="bm-row">
      <label>类型</label>
      <!-- The "?" spans are decorative for AT (a focusable role=button
           nested inside a role=radio is invalid ARIA and read
           unpredictably); the help text is instead attached to each radio
           via aria-describedby, so screen readers hear it with the
           control. Sighted users keep the tap target — its click handler
           stopPropagation()s so it never toggles the kind. -->
      <div class="bm-kind-seg" role="radiogroup" aria-label="类型">
        <button type="button" data-kind="bookmark" class="active"
                role="radio" aria-checked="true"
                aria-describedby="help-kind-bookmark">⭐ 收藏<span class="ff-help-trigger"
                data-help-for="kind-bookmark" aria-hidden="true">?</span></button>
        <button type="button" data-kind="attraction"
                role="radio" aria-checked="false"
                aria-describedby="help-kind-attraction">🗾 景点<span class="ff-help-trigger"
                data-help-for="kind-attraction" aria-hidden="true">?</span></button>
      </div>
    </div>
    <div class="bm-row">
      <label for="bm-name">名称</label>
      <input type="text" id="bm-name" maxlength="40"
             placeholder="例如：东京塔" autocomplete="off">
    </div>
    <div class="bm-row">
      <label for="bm-emoji">Emoji</label>
      <input type="text" id="bm-emoji" maxlength="8" value="📍"
             placeholder="可粘贴任意 emoji" autocomplete="off">
    </div>

    <div class="bm-quick">
      <span class="bm-quick-label">常用</span>
      <div class="bm-quick-list">
        <button type="button" data-emoji="🏠">🏠</button>
        <button type="button" data-emoji="🏨">🏨</button>
        <button type="button" data-emoji="🍽️">🍽️</button>
        <button type="button" data-emoji="⭐">⭐</button>
        <button type="button" data-emoji="❤️">❤️</button>
        <button type="button" data-emoji="🛍️">🛍️</button>
        <button type="button" id="bm-emoji-more"
                aria-expanded="false" aria-controls="bm-emoji-picker"
                title="打开完整 emoji 选择器">🔽</button>
      </div>
    </div>

    <emoji-picker id="bm-emoji-picker"></emoji-picker>

    <div class="bm-error" id="bm-error" aria-live="polite"></div>
  </div>
  <div class="bm-foot">
    <button type="button" class="bm-cancel">取消</button>
    <button type="button" class="bm-save">保存</button>
  </div>
</div>

<style>
  /* Import-picker modal: opened from the avatar menu's "导入 favorites.json".
     Lets the user choose which of the three corpora in the backup file to
     merge, with a per-corpus item count shown alongside each checkbox.
     Lives in this constant (not SEARCH_BOX_HTML) so it rides the same
     body-level injection as #bm-modal — confirmed-safe for position:fixed. */
  #imp-backdrop {
    position: fixed; inset: 0; z-index: 11000;
    background: rgba(0,0,0,0.35); display: none;
  }
  #imp-backdrop.imp-open { display: block; }
  #imp-modal {
    position: fixed; z-index: 11001;
    left: 50%; top: 50%; transform: translate(-50%, -50%);
    width: min(360px, calc(100vw - 32px));
    background: #fff; border-radius: 14px;
    box-shadow: 0 16px 40px rgba(0,0,0,0.25);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    display: none;
  }
  #imp-modal.imp-open { display: block; }
  #imp-modal .imp-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 14px; border-bottom: 1px solid #f3f4f6;
  }
  #imp-modal .imp-title { font-weight: 600; font-size: 15px; color: #1f2937; }
  #imp-modal .imp-close {
    border: none; background: none; cursor: pointer;
    font-size: 20px; line-height: 1; color: #6b7280; padding: 0 4px;
  }
  #imp-modal .imp-body { padding: 12px 14px; }
  #imp-modal .imp-sub {
    margin: 0 0 10px; font-size: 12px; color: #6b7280; line-height: 1.5;
  }
  #imp-modal .imp-opt {
    display: flex; align-items: center; gap: 10px;
    padding: 9px 10px; margin-bottom: 6px;
    border: 1px solid #e5e7eb; border-radius: 8px; cursor: pointer;
  }
  #imp-modal .imp-opt:hover { background: #f9fafb; }
  #imp-modal .imp-opt input { width: 16px; height: 16px; flex-shrink: 0; cursor: pointer; }
  #imp-modal .imp-opt-label { flex: 1; font-size: 13px; color: #1f2937; font-weight: 600; }
  #imp-modal .imp-opt-count { font-size: 12px; color: #6b7280; flex-shrink: 0; }
  #imp-modal .imp-opt input:disabled { cursor: default; }
  #imp-modal .imp-opt input:disabled ~ .imp-opt-label,
  #imp-modal .imp-opt input:disabled ~ .imp-opt-count { color: #9ca3af; }
  #imp-modal .imp-error { color: #b91c1c; font-size: 12px; min-height: 14px; margin-top: 4px; }
  #imp-modal .imp-foot {
    display: flex; justify-content: flex-end; gap: 8px;
    padding: 10px 14px 14px; border-top: 1px solid #f3f4f6;
  }
  #imp-modal .imp-foot button {
    padding: 7px 16px; border-radius: 6px; cursor: pointer;
    font-size: 13px; font-weight: 600; border: 1px solid #d1d5db;
    background: #f9fafb; color: #1f2937; font-family: inherit;
  }
  #imp-modal .imp-foot button.imp-confirm { background: #2563eb; border-color: #2563eb; color: #fff; }
  #imp-modal .imp-foot button.imp-confirm:hover { background: #1d4ed8; }
  #imp-modal .imp-foot button.imp-cancel:hover { background: #f3f4f6; }
</style>
<div id="imp-backdrop"></div>
<div id="imp-modal" role="dialog" aria-modal="true" aria-hidden="true"
     aria-labelledby="imp-title">
  <div class="imp-head">
    <span class="imp-title" id="imp-title">导入备份</span>
    <button class="imp-close" type="button" aria-label="关闭">×</button>
  </div>
  <div class="imp-body">
    <p class="imp-sub">选择要导入的内容，将合并到现有数据并自动去重（不会覆盖现有项）：</p>
    <label class="imp-opt">
      <input type="checkbox" id="imp-fav" checked>
      <span class="imp-opt-label">收藏</span>
      <span class="imp-opt-count" id="imp-fav-n">—</span>
    </label>
    <label class="imp-opt">
      <input type="checkbox" id="imp-black" checked>
      <span class="imp-opt-label">弃用名单</span>
      <span class="imp-opt-count" id="imp-black-n">—</span>
    </label>
    <label class="imp-opt">
      <input type="checkbox" id="imp-bm" checked>
      <span class="imp-opt-label">书签 / 景点</span>
      <span class="imp-opt-count" id="imp-bm-n">—</span>
    </label>
    <div class="imp-error" id="imp-error" aria-live="polite"></div>
  </div>
  <div class="imp-foot">
    <button type="button" class="imp-cancel">取消</button>
    <button type="button" class="imp-confirm">导入</button>
  </div>
</div>
"""


MOBILE_UX_ASSETS = """
<style>
  /* M-076/M-077: declare the page light-only. Without a color-scheme
     declaration Chrome's Auto Dark Theme algorithmically inverts the whole
     UI on an Android phone in dark mode — the search box, both sheets and
     the FAB pills all flip to near-black while the map tiles stay light,
     and the two "light plate / dark text" 百名店 ribbons collapse to a
     1.35:1 contrast smear. `only light` was measured (Chromium 147, forced
     dark + prefers dark) to be the variant that actually stops it; plain
     `light` does not. Not `light dark`: there is no dark palette here, so
     opting into one would only hand the native controls a black skin.
     ⚠ Needs one real-device confirmation on a Fold 8 in system dark mode. */
  html { color-scheme: only light; }
  html, body { overscroll-behavior: none; }
  /* Stop iOS Safari's "text size adjust" algorithm from inflating any
     unstyled text on the page. Bootstrap's reset used to set this on
     html; we dropped Bootstrap as a folium-injected dead dep, so own
     the rule here instead. Affects mobile rendering only. */
  html { -webkit-text-size-adjust: 100%; }
  /* Use border-box globally so element widths include their padding
     and border. Also a Bootstrap reset we used to inherit; making it
     explicit means custom UI that sets width + padding behaves the
     way the rest of the codebase already assumes. */
  *, *::before, *::after { box-sizing: border-box; }
  /* M-085: honour "reduce motion" globally. Everything decorative collapses
     to a single ~instant frame; the exceptions below keep the two elements
     that carry meaning readable in their resting state.
       .mk-pulse-ring          — resting state is the static blue halo (the
                                 keyframes only scale/fade it), so the
                                 selected marker is still findable.
       #ff-fab.needs-sync      — resting state is the solid red/blue pill.
       #ss-spinner             — progress feedback for an in-flight search,
                                 not decoration. A stopped rotation reads as
                                 a broken widget, so it becomes an explicit
                                 static two-tone ring instead. (Higher
                                 specificity than `*`, so it wins even with
                                 both marked !important.) */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: .01ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: .01ms !important;
    }
    #ss-spinner {
      animation: none !important;
      border-color: #bfdbfe; border-top-color: #2563eb;
    }
  }
</style>
<script>
(function() {
  // M-018 / M-143: page zoom is the user's, not ours. These guards used to
  // sit on `document` — which (a) blocked browser zoom over the whole page,
  // not just the map, and (b) made every wheel event on the page wait for a
  // non-passive listener before the browser could scroll. They now bind to
  // the Leaflet container only, so the map keeps its pinch/ctrl+wheel
  // behaviour while the rest of the document scrolls passively and zooms
  // normally. The keyboard ctrl +/-/0 interception is gone entirely: there
  // is no map-vs-page ambiguity for a keystroke, so it was pure a11y tax.
  // (The viewport meta stopped pinning the scale too — see main().)
  function bindZoomGuards(el) {
    // iOS Safari pinch over the map. Leaflet drives its own touch zoom from
    // raw touch events and never listens for gesture*, so preventing these
    // stops the *page* zooming while the map still pinches.
    el.addEventListener('gesturestart',  function(e){ e.preventDefault(); });
    el.addEventListener('gesturechange', function(e){ e.preventDefault(); });
    el.addEventListener('gestureend',    function(e){ e.preventDefault(); });
    // Desktop ctrl/cmd + wheel — this is also what a macOS trackpad pinch
    // sends, so it has to stay. Leaflet's wheel zoom doesn't use ctrlKey.
    el.addEventListener('wheel', function(e){
      if (e.ctrlKey || e.metaKey) e.preventDefault();
    }, { passive: false });
  }

  // iOS double-tap zoom on the UI chrome is handled by CSS
  // `touch-action: manipulation` (style block below) instead of the old
  // 350ms touchend-preventDefault hack. preventDefault on touchend also
  // suppressed the synthesized click, so the second of any two fast taps
  // outside the map (rapid checkbox toggles, 全选 then 全清, double-tap
  // on a star) silently did nothing. The map is unaffected either way:
  // Leaflet sets touch-action on .leaflet-container itself and the
  // ancestor intersection can only further restrict, never loosen.

  // The container is created by folium's own script at the end of <body>,
  // long after this head script runs — poll briefly, then give up.
  var tries = 0;
  (function waitForMap() {
    var el = document.querySelector('.leaflet-container');
    if (el) { bindZoomGuards(el); return; }
    if (++tries > 150) return;
    setTimeout(waitForMap, 100);
  })();
})();
</script>
<style>
  /* Kills double-tap-to-zoom (and the legacy 300ms click delay) on all UI
     chrome without eating fast second taps. Pinch stays governed by the
     gesture handlers above; Leaflet overrides this on its own container. */
  html, body { touch-action: manipulation; }
  /* Bottom-sheet popup replacement. The markup lives near </body>; the
     filter JS controls open/close. Default Leaflet popups got cut off at
     mobile viewport edges; this sheet always docks to the bottom and
     scrolls internally. */
  /* Backdrop intentionally inert — Google-Maps-style sheet: the map stays
     pannable / zoomable behind every sheet state. The element is kept so
     legacy JS references (bsBackdrop.classList.add(...)) don't have to be
     ripped out; the .bs-open class is now a no-op. */
  #bs-backdrop {
    position: fixed; inset: 0; z-index: 10001;
    pointer-events: none;
    background: transparent;
  }
  #bs-sheet {
    position: fixed; left: 0; right: 0; bottom: 0;
    z-index: 10002;
    max-height: 75vh; max-height: 75dvh;
    background: #fff;
    border-radius: 14px 14px 0 0;
    box-shadow: 0 -8px 24px rgba(0,0,0,0.18);
    transform: translateY(100%);
    transition: transform 0.25s ease-out;
    display: flex; flex-direction: column;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    padding-bottom: env(safe-area-inset-bottom);
  }
  #bs-sheet.bs-open { transform: translateY(0); }
  /* Tablet: cap width and center; still bottom-anchored. The explicit
     width keeps the sheet at a stable size regardless of content — without
     it, `left: 50%; right: auto` makes the sheet shrink-to-fit, so swapping
     the bottom-sheet content (e.g., loading placeholder → full card) would
     make it jump wider. */
  @media (min-width: 700px) {
    #bs-sheet { left: 50%; transform: translate(-50%, 100%);
                width: min(680px, calc(100vw - 32px)); right: auto;
                max-height: 80vh; max-height: 80dvh;
                border-radius: 14px 14px 0 0; }
    #bs-sheet.bs-open { transform: translate(-50%, 0); }
  }
  /* Desktop: roomier sheet so the 2-column popup layout has space. */
  @media (min-width: 1100px) {
    #bs-sheet { width: min(880px, calc(100vw - 32px));
                max-height: 85vh; max-height: 85dvh; }
  }
  #bs-grip {
    position: relative;
    padding: 9px 0 6px; flex-shrink: 0;
    cursor: grab; touch-action: none;
  }
  #bs-grip::before {
    content: ''; display: block;
    width: 38px; height: 4px; margin: 0 auto;
    background: #d1d5db; border-radius: 2px;
  }
  #bs-content {
    overflow-y: auto;
    padding: 0 14px 14px;
    flex: 1 1 auto;
    /* M-170: a flick that runs past the end of the card used to chain into
       a map pan behind it. */
    overscroll-behavior: contain;
    -webkit-overflow-scrolling: touch;
  }
  /* Peek mode — entered when the sheet is opened from a search-result tap.
     The sheet only shows the ribbons + title + rating + ⭐/🚫 actions, so
     the highlighted marker on the map below stays visible. User swipes up
     on the grip (or taps it) to reveal the rest. */
  #bs-sheet.bs-peek .rst-photos,
  #bs-sheet.bs-peek .rst-genre,
  #bs-sheet.bs-peek .rst-info,
  #bs-sheet.bs-peek .rst-policy,
  #bs-sheet.bs-peek .rst-footer { display: none; }
  #bs-sheet.bs-peek #bs-grip { padding-bottom: 2px; }
  #bs-sheet.bs-peek #bs-grip::before { background: #9ca3af; }
  #bs-sheet.bs-peek #bs-grip::after {
    content: '上滑查看详情';
    display: block; text-align: center;
    font-size: 10px; color: #6b7280;
    margin-top: 3px; letter-spacing: 0.5px;
  }
  /* The whole peek card is a tap-to-expand surface (handled in JS). The
     cursor hint is for desktop; buttons inside override it back to pointer
     via the default UA stylesheet, links inherit their own. */
  #bs-sheet.bs-peek #bs-content { cursor: pointer; }
  /* ===== Restaurant detail card (lives inside #bs-content) ===== */
  .rst-card { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
              font-size: 13px; color: #1f2937; }
  /* Award ribbons sit above the title — flat dark badges matching the
     Tabelog site style. Order in the JSX matches visual order:
       medals (gold→silver→bronze) → 百名店 (newest year first) → hot.
     Medals each get their own metallic-toned solid; hot gets a distinct
     red so it doesn't blend with the gold family; 百名店 is a warm olive
     ramp keyed off the year so 2026 is the deepest shade. */
  .rst-ribbons { display: flex; flex-wrap: wrap; gap: 4px;
                 margin: 0 0 6px; }
  .rst-ribbon { display: inline-block;
                font-size: 11px; font-weight: 700; letter-spacing: 0.4px;
                padding: 3px 8px; border-radius: 2px; line-height: 1.45;
                color: #fff; white-space: nowrap; user-select: none; }
  .rst-ribbon-gold       { background: #a08a55; }
  .rst-ribbon-silver     { background: #8e9398; }
  .rst-ribbon-bronze     { background: #8c6239; }
  .rst-ribbon-hot        { background: #c0392b; }
  .rst-ribbon-hyaku      { background: #6a5a3a; }
  .rst-ribbon-hyaku-2026 { background: #5a4a26; }
  .rst-ribbon-hyaku-2025 { background: #7a6a3a; }
  .rst-ribbon-hyaku-2024 { background: #998860; }
  /* M-076: these two used to be the only "pale plate / dark ink" ribbons of
     the eleven. Chrome's Auto Dark Theme lightens text but keeps a pale
     background, so they measured 1.35:1 and 1.89:1 on an Android phone in
     dark mode (against 8.74 / 6.29 normally) — unreadable. Brought into the
     same dark-plate / white-ink family as the other nine, still a rung
     lighter per year so the ramp still reads oldest → palest, and still in
     the same 3.0-3.5:1 band as the gold/silver ribbons. */
  .rst-ribbon-hyaku-2023 { background: #a08d63; }
  .rst-ribbon-hyaku-2022 { background: #a49373; }
  .rst-header { display: flex; justify-content: space-between;
                align-items: flex-start; gap: 8px; margin-bottom: 8px; }
  .rst-title { font-weight: 700; font-size: 16px; flex: 1; min-width: 0;
               line-height: 1.3; }
  .rst-title .rst-rating { color: #c33; margin-left: 4px; font-weight: 700; }
  /* Match .rst-btn exactly so the trio (gmaps / 收藏 / 弃用) renders at one
     consistent height; gmaps is the square sibling. */
  .rst-gmaps { display: inline-flex; align-items: center; justify-content: center;
               box-sizing: border-box;
               width: 28px; height: 28px;
               text-decoration: none;
               border: 1px solid #d1d5db; border-radius: 5px;
               background: #f9fafb;
               transition: background 0.15s; }
  .rst-gmaps:hover { background: #f3f4f6; }
  .rst-gmaps img { width: 18px; height: 18px; display: block; }
  /* Square × that mirrors the search box's clear control, sitting after
     收藏/弃用 in the card header so the sheet can be dismissed without
     reaching for the grip. Same 28px footprint as .rst-gmaps. */
  .rst-close { display: inline-flex; align-items: center; justify-content: center;
               box-sizing: border-box;
               width: 28px; height: 28px;
               padding: 0; border: 1px solid #d1d5db; border-radius: 5px;
               background: #f9fafb; color: #6b7280;
               font: 700 20px/1 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               cursor: pointer; -webkit-tap-highlight-color: transparent; }
  .rst-close:hover, .rst-close:active { background: #f3f4f6; color: #1f2937; }
  .rst-actions { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }
  .rst-photos { display: grid; grid-template-columns: repeat(3, 1fr);
                gap: 6px; margin-bottom: 10px; }
  .rst-photos a { display: block; min-width: 0; position: relative;
                  overflow: hidden; border-radius: 6px;
                  aspect-ratio: 1 / 1; background: #f3f4f6; }
  .rst-photos img { width: 100%; aspect-ratio: 1 / 1; object-fit: cover;
                    border-radius: 6px; display: block; background: #f3f4f6; }
  /* Loading shimmer — a translated gradient strip (transform-only, stays
     on the compositor). The img's inline onload adds .ld to the anchor,
     which removes the strip so nothing keeps animating under the photo. */
  .rst-photos a::after {
    content: ''; position: absolute; inset: 0;
    background: linear-gradient(100deg, transparent 30%,
                rgba(255,255,255,0.7) 50%, transparent 70%);
    transform: translateX(-100%);
    /* M-141: bounded, not infinite. The strip only stops on the img's
       onload/onerror, and a photo request that neither completes nor fails
       (captive portal, dead cell edge) fired neither — so the shimmer ran
       forever, including after the card was dismissed. 10 passes ≈ 11s is
       longer than any photo that is ever going to arrive. */
    animation: rst-shimmer 1.1s ease-in-out 10;
  }
  .rst-photos a.ld::after { content: none; }
  @keyframes rst-shimmer { to { transform: translateX(100%); } }
  .rst-genre { color: #4b5563; margin-bottom: 8px; }
  .rst-info { display: grid; grid-template-columns: 1fr;
              gap: 4px 16px; margin-bottom: 8px; }
  .rst-info-row { display: flex; gap: 6px; align-items: baseline;
                  line-height: 1.45; }
  .rst-info-row .rst-label { color: #9ca3af; flex-shrink: 0;
                             font-size: 12px; min-width: 38px; }
  .rst-info-row .rst-value { color: #1f2937; min-width: 0;
                             overflow-wrap: anywhere; }
  .rst-tx-btn { background: none; border: none; padding: 0;
                margin-left: 6px; color: #2563eb; cursor: pointer;
                font-family: inherit; font-size: 12px; line-height: 1.45;
                flex-shrink: 0; }
  .rst-tx-btn:hover { color: #1d4ed8; text-decoration: underline; }
  .rst-tx-btn:disabled { color: #9ca3af; cursor: default;
                         text-decoration: none; }
  .rst-policy { font-size: 12px; color: #6b7280; line-height: 1.5;
                margin-bottom: 8px; }
  .rst-footer { display: flex; justify-content: space-between;
                align-items: center; gap: 8px; flex-wrap: wrap; }
  .rst-footer a { color: #2563eb; text-decoration: none; font-size: 13px; }
  .rst-footer a:hover { text-decoration: underline; }
  .rst-chip { background: #3b9c4f; color: #fff; padding: 2px 8px;
              border-radius: 4px; font-size: 11px; font-weight: 500; }
  .rst-chip.rst-chip-off { background: #9ca3af; }
  .rst-btn { padding: 4px 10px; font-size: 12px; cursor: pointer;
             border: 1px solid #d1d5db; border-radius: 5px;
             background: #f9fafb; color: #1f2937; }
  .rst-btn:hover { background: #f3f4f6; }
  /* Tablet+: tighter title, larger photos */
  @media (min-width: 700px) {
    .rst-card { font-size: 14px; }
    .rst-title { font-size: 18px; }
    .rst-photos { gap: 8px; }
  }
  /* Desktop: 2-column info grid; photos still 3-up but bigger */
  @media (min-width: 1100px) {
    #bs-content { padding: 0 22px 22px; }
    .rst-card { font-size: 14px; }
    .rst-title { font-size: 20px; }
    .rst-info { grid-template-columns: 1fr 1fr; column-gap: 24px; }
  }
  /* Banner shown above the restaurant card when the active result is
     currently filtered out of the map. */
  #bs-banner {
    padding: 8px 14px;
    background: #fef3c7; border-bottom: 1px solid #fcd34d;
    color: #78350f; font-size: 12px; line-height: 1.45;
    flex-shrink: 0;
  }
  #bs-banner[hidden] { display: none; }
  /* Highlighted marker (pulsing blue halo) + ghost marker (gray, no
     emoji). The marker HTML lives in divIcons built by makeIcon /
     makeGhostIcon — these classes hook the pulse animation. */
  @keyframes mk-pulse {
    0%   { transform: scale(0.9); opacity: 0.85; }
    100% { transform: scale(1.9); opacity: 0; }
  }
  .mk-pulse-ring {
    position: absolute; inset: 0; border-radius: 50%;
    border: 2px solid #2563eb;
    /* M-140: five pulses (8s) instead of `infinite`. Reading a card takes
       30-120s, and the ring kept the compositor drawing for all of it. Once
       the animation ends the element rests at scale 1 / opacity 1, i.e. a
       static blue halo — the marker stays just as findable. */
    animation: mk-pulse 1.6s ease-out 5;
    pointer-events: none;
  }
</style>"""


# M-033 / M-034 / M-028 / M-089: everything the sync + empty-state UX needs
# that isn't already somewhere else. Kept as its own constant (own <style>,
# own nodes) so it can be reasoned about independently of the search box and
# the filter panel, both of which are crowded already.
#
#   #sync-banner   top, error-only, aria-live=assertive (M-034). Success
#                  stays quiet — it lives in #ff-sync-status as before.
#   #sync-stack    bottom stack: transient toasts + the dismissible
#                  "sign in to sync" hint (M-033). Sits above the two bottom
#                  sheets (10002) so a toast fired from an open restaurant
#                  card is actually visible, but below #bm-backdrop (10010).
#   #ff-empty-map  centred "nothing matches" card over the map (M-028).
#   #sync-sr       the polite live region every non-visual announcement
#                  funnels through (M-089).
#
# The FAB's own needs-sync animation is NOT here — it lives with #ff-fab in
# build_filter_panel_html() and is deliberately left alone.
SYNC_UI_HTML = """
<style>
  /* These nodes set an explicit `display`, which outranks the UA's
     `[hidden] { display: none }` — without this rule `el.hidden = true`
     would leave them on screen. */
  #sync-banner[hidden], #sync-hint[hidden], #ff-empty-map[hidden],
  #ff-empty-panel[hidden], #ss-avatar-dot[hidden] { display: none; }

  /* Visually hidden but readable by assistive tech. */
  .sr-only {
    position: absolute; width: 1px; height: 1px;
    padding: 0; margin: -1px; overflow: hidden;
    clip: rect(0 0 0 0); clip-path: inset(50%);
    white-space: nowrap; border: 0;
  }

  /* ---- M-034: failure banner, top, under the search box ---- */
  #sync-banner {
    position: fixed;
    top: calc(max(12px, env(safe-area-inset-top)) + 48px);
    left: 50%; transform: translateX(-50%);
    z-index: 10005;
    width: min(calc(100vw - 24px), 460px);
    box-sizing: border-box;
    display: flex; align-items: flex-start; gap: 8px;
    padding: 9px 10px 9px 12px;
    border: 1px solid #fca5a5; border-left: 4px solid #dc2626;
    border-radius: 8px;
    background: #fef2f2; color: #7f1d1d;
    font: 500 13px/1.45 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    box-shadow: 0 6px 20px rgba(0,0,0,0.16);
  }
  #sync-banner-msg { flex: 1; min-width: 0; }
  #sync-banner-x {
    flex-shrink: 0; width: 22px; height: 22px; padding: 0;
    border: 0; border-radius: 5px; background: transparent;
    color: #991b1b; font: 700 18px/1 -apple-system, sans-serif;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  #sync-banner-x:hover { background: rgba(220,38,38,0.12); }

  /* ---- M-033: bottom stack (toasts + sign-in hint) ---- */
  #sync-stack {
    position: fixed;
    left: 0; right: 0;
    bottom: calc(76px + env(safe-area-inset-bottom));
    z-index: 10005;
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    padding: 0 12px; pointer-events: none;
  }
  #sync-stack > * {
    pointer-events: auto;
    width: 100%; max-width: 460px; box-sizing: border-box;
  }
  .sync-toast, #sync-hint {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 12px;
    border-radius: 10px;
    background: #111827; color: #f9fafb;
    font: 500 13px/1.45 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    box-shadow: 0 8px 24px rgba(0,0,0,0.28);
  }
  .sync-toast-msg, #sync-hint-msg { flex: 1; min-width: 0; }
  .sync-toast img.emoji-img, #sync-hint img.emoji-img {
    height: 1.05em; width: 1.05em; vertical-align: -0.15em;
  }
  .sync-btn {
    flex-shrink: 0;
    padding: 6px 11px; border-radius: 999px;
    border: 1px solid rgba(255,255,255,0.35);
    background: transparent; color: #f9fafb;
    font: 600 12px/1 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  .sync-btn:hover { background: rgba(255,255,255,0.14); }
  .sync-btn.primary {
    border-color: #60a5fa; background: #2563eb; color: #fff;
  }
  .sync-btn.primary:hover { background: #1d4ed8; }
  #sync-hint-btns { display: flex; gap: 6px; flex-shrink: 0; }
  @media (prefers-reduced-motion: no-preference) {
    .sync-toast, #sync-hint { animation: sync-rise 0.18s ease-out; }
  }
  @keyframes sync-rise {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: none; }
  }

  /* ---- M-034: red dot on the avatar while state is unsynced / failing ---- */
  #ss-avatar-dot {
    position: absolute; top: -1px; right: -1px;
    width: 10px; height: 10px; border-radius: 50%;
    background: #dc2626; border: 2px solid #fff;
    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    pointer-events: none;
  }

  /* ---- M-028: empty state ---- */
  #ff-empty-map {
    position: fixed;
    top: 50%; left: 50%; transform: translate(-50%, -50%);
    z-index: 9994;
    width: min(calc(100vw - 48px), 320px);
    box-sizing: border-box;
    padding: 16px 18px;
    border: 1px solid #e5e7eb; border-radius: 12px;
    background: rgba(255,255,255,0.96);
    box-shadow: 0 10px 30px rgba(0,0,0,0.18);
    text-align: center;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    pointer-events: none;   /* never steal a map drag */
  }
  .ffe-title { font-size: 15px; font-weight: 700; color: #111827; }
  .ffe-sub   { font-size: 12.5px; line-height: 1.5; color: #6b7280;
               margin-top: 5px; }
  .ffe-reset {
    pointer-events: auto;
    margin-top: 12px; padding: 8px 16px;
    border: 1px solid #2563eb; border-radius: 999px;
    background: #2563eb; color: #fff;
    font: 600 13px/1 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  .ffe-reset:hover { background: #1d4ed8; }
  /* Panel-top twin: same copy, flat card, no shadow. */
  #ff-empty-panel {
    margin: 0 0 10px; padding: 10px 12px;
    border: 1px solid #fecaca; border-radius: 8px;
    background: #fef2f2; text-align: left;
  }
  #ff-empty-panel .ffe-title { font-size: 13px; color: #7f1d1d; }
  #ff-empty-panel .ffe-sub   { font-size: 12px; color: #991b1b; }
  #ff-empty-panel .ffe-reset { margin-top: 8px; padding: 6px 12px;
                               font-size: 12px; }
  /* M-028: the live count turns red at zero so the number itself carries
     the signal, not just the card. */
  .ff-count.is-zero { color: #dc2626; }

  /* ---- M-028: reset copied into the panel footer ---- */
  #ff-panel-reset {
    display: block; width: 100%;
    margin: 10px 0 2px; padding: 9px 12px;
    border: 1px solid #d1d5db; border-radius: 8px;
    background: #f9fafb; color: #374151;
    font: 600 13px/1 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  #ff-panel-reset:hover { background: #f3f4f6; }

  /* ---- M-008: privacy + delete-my-cloud-data rows in the account menu ---- */
  #ssm-privacy-link { color: #2563eb; text-decoration: none; }
  #ssm-privacy-link:hover { text-decoration: underline; }
  #ssm-delete-cloud { color: #b91c1c; }
  #ssm-delete-cloud[data-armed="1"] { background: #fef2f2; font-weight: 700; }
  #ssm-delete-msg {
    font-size: 10px; line-height: 1.5; color: #6b7280;
    padding: 2px 0 4px; min-height: 0;
  }
</style>
<!-- M-034: only failures reach this banner; success stays in the quiet
     status line inside the account menu. -->
<div id="sync-banner" hidden role="alert" aria-live="assertive">
  <span id="sync-banner-msg"></span>
  <button id="sync-banner-x" type="button" aria-label="关闭">×</button>
</div>
<div id="sync-stack">
  <!-- M-033: the sync warning used to be a title= on the filter FAB, which
       no touch device can ever show. Its own banner, its own dismissal. -->
  <div id="sync-hint" hidden>
    <span id="sync-hint-msg">收藏只存在这台设备。登录后可在其它设备看到。</span>
    <span id="sync-hint-btns">
      <button id="sync-hint-signin" class="sync-btn primary" type="button">登录</button>
      <button id="sync-hint-ok" class="sync-btn" type="button">知道了</button>
    </span>
  </div>
</div>
<!-- M-028 -->
<div id="ff-empty-map" hidden>
  <div class="ffe-title">没有符合条件的餐厅</div>
  <div class="ffe-sub">筛选条件太严格了。放宽条件或重置筛选。</div>
  <button id="ff-empty-reset" class="ffe-reset" type="button">重置筛选</button>
</div>
<!-- M-089: every announcement that has no visible-text equivalent (or whose
     visible text lives in a container screen readers don't watch) is written
     here instead of being lost. -->
<div id="sync-sr" class="sr-only" role="status" aria-live="polite"></div>
"""


# Bottom-sheet DOM. Injected into <body>; populated by openSheet() in the
# filter JS. Backdrop is a sibling so taps fall through to it.
BOTTOM_SHEET_HTML = """
<div id="bs-backdrop"></div>
<!-- Deliberately NOT aria-modal: the backdrop is inert and the map stays
     pannable behind every sheet state, so claiming modality would tell
     screen readers the rest of the page is unreachable when it isn't. -->
<div id="bs-sheet" role="dialog" aria-hidden="true">
  <div id="bs-grip"></div>
  <div id="bs-banner" hidden></div>
  <div id="bs-content"></div>
</div>
"""


# Service worker source. Written to docs/sw.js at build time after the
# placeholders are substituted. Strategy by request type:
#   same-origin HTML / nav          → network-first with a 3.5s cache race
#   same-origin data / JS / emoji   → cache-first in a PERSISTENT cache;
#     the page requests payloads as  data/foo.json?v=<content-hash>, so an
#     unchanged file survives any number of deploys with zero re-download
#     (the old scheme wiped everything and re-fetched ~10MB per deploy)
#   /transit/*.geojson              → stale-while-revalidate (huge, rarely
#     changes, not ?v-addressed because its URL lives inside transit-layer.js)
#   third-party CDN (emojicdn, jsdelivr) → stale-while-revalidate, LRU-capped
#   anything else (tiles, Nominatim, jpfoodmap API) → browser default
SW_JS_TEMPLATE = r"""// Auto-generated by src/tabelog/scrape/map.py — do not edit by hand.
// Build version: __BUILD_VERSION__
const VERSION = '__BUILD_VERSION__';
// Shell cache is per-deploy (holds the HTML); the other two persist across
// deploys — their entries are content-addressed (?v= hash) or immutable.
const SHELL_CACHE = 'tabelog-shell-' + VERSION;
const DATA_CACHE  = 'tabelog-data-v1';
const EXT_CACHE   = 'tabelog-ext-v1';
const KEEP = [SHELL_CACHE, DATA_CACHE, EXT_CACHE];

// Versioned with SHELL_CACHE so launcher metadata and icons update with a
// deploy. Caching the root document during install makes the very first
// installed-app launch work even if the phone has already gone offline.
// M-061: the launcher icons (142 KB) are only ever painted by an installed
// app, and the overwhelming majority of visitors never install — so they
// moved out of the unconditional set and are warmed only when this really
// is (or has just become) an installed launch.
const APP_SHELL_URLS = [
  './',
  './manifest.webmanifest?v=__MANIFEST_V__',
];
const LAUNCHER_ICON_URLS = [
  './icons/icon-japan-emoji-v2-192.png',
  './icons/icon-japan-emoji-v2-512.png',
  './icons/icon-japan-emoji-v2-maskable-512.png',
  './apple-touch-icon-japan-emoji-v2.png',
];

// What install warms (subset: the boot-critical payloads for the default
// language) and everything the current build references with a ?v= hash
// (used by activate's garbage collection — entries outside this list are
// superseded versions and get dropped so 6MB payloads don't pile up).
const PRECACHE_URLS = __PRECACHE_URLS__;
const CURRENT_VERSIONED_URLS = __ALL_VERSIONED_URLS__;

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    try {
      const shell = await caches.open(SHELL_CACHE);
      await Promise.all(APP_SHELL_URLS.map(async (u) => {
        try {
          // M-061: no cache:'reload' for './' — the navigation that just
          // registered this worker already wrote the very same HTML into
          // this very same cache via networkFirst, and 'reload' explicitly
          // bypasses the HTTP cache, so it was a guaranteed second full
          // download of the document on every deploy.
          const resp = await fetch(u);
          if (resp && (resp.ok || resp.type === 'opaque')) await shell.put(u, resp);
        } catch (_) {}
      }));
    } catch (_) { /* opening the shell cache failed — install still proceeds */ }
    try {
      const cache = await caches.open(DATA_CACHE);
      // Content-addressed: if the hash matches an entry we already hold,
      // the file didn't change — skip the network entirely. Fail-soft per
      // URL; a miss just falls back to cacheFirst on first fetch.
      await Promise.all(PRECACHE_URLS.map(async (u) => {
        try {
          if (await cache.match(u)) return;
          const resp = await fetch(u);
          if (resp && (resp.ok || resp.type === 'opaque')) await cache.put(u, resp);
        } catch (_) {}
      }));
    } catch (_) { /* opening the cache failed — install still proceeds */ }
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter(k => KEEP.indexOf(k) === -1).map(k => caches.delete(k)));
    // GC superseded ?v= entries. Plain-URL entries (emoji PNGs, geojson,
    // requests from a not-yet-refreshed old page) are left alone.
    try {
      const wanted = new Set(CURRENT_VERSIONED_URLS.map(
        u => new URL(u, self.location.href).href));
      const cache = await caches.open(DATA_CACHE);
      for (const req of await cache.keys()) {
        const url = new URL(req.url);
        if (!/^\?v=/.test(url.search)) continue;
        if (!wanted.has(url.href)) await cache.delete(req);
      }
    } catch (_) {}
    await self.clients.claim();
  })());
});

// Page-driven warmups. The service worker can't know two things the page
// knows: which UI language is active (M-062 — popups is one 6.4MB file per
// language and install-time precaching guessed zh-CN for everyone), and
// whether this is an installed launch (M-061 — launcher icons). Both arrive
// as messages. Only URLs this build actually references are honoured, so a
// stray postMessage can't turn the cache into an open proxy.
function versionedHrefs() {
  const out = new Set();
  for (const u of CURRENT_VERSIONED_URLS) {
    try { out.add(new URL(u, self.location.href).href); } catch (_) {}
  }
  return out;
}
async function warmInto(cacheName, hrefs) {
  const cache = await caches.open(cacheName);
  await Promise.all(hrefs.map(async (h) => {
    try {
      if (await cache.match(h)) return;
      const resp = await fetch(h);
      if (resp && (resp.ok || resp.type === 'opaque')) await cache.put(h, resp);
    } catch (_) {}
  }));
}
self.addEventListener('message', (event) => {
  const d = event.data;
  if (!d || typeof d !== 'object') return;
  if (d.type === 'WARM_DATA' && typeof d.url === 'string') {
    event.waitUntil((async () => {
      try {
        const href = new URL(d.url, self.location.href).href;
        if (!versionedHrefs().has(href)) return;   // not part of this build
        await warmInto(DATA_CACHE, [href]);
      } catch (_) {}
    })());
    return;
  }
  if (d.type === 'WARM_LAUNCHER_ICONS') {
    event.waitUntil(warmInto(SHELL_CACHE, LAUNCHER_ICON_URLS).catch(() => {}));
  }
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    // sw.js itself must always come from the network so updates land.
    if (url.pathname.endsWith('/sw.js')) return;
    if (req.mode === 'navigate' || url.pathname.endsWith('.html')) {
      event.respondWith(networkFirst(req, event));
      return;
    }
    if (url.pathname.endsWith('/manifest.webmanifest') ||
        url.pathname.indexOf('/apple-touch-icon') !== -1 ||
        url.pathname.indexOf('/icons/') !== -1) {
      event.respondWith(cacheFirst(req, SHELL_CACHE));
      return;
    }
    if (url.pathname.indexOf('/transit/') !== -1) {
      event.respondWith(staleWhileRevalidate(req, DATA_CACHE, false));
      return;
    }
    event.respondWith(cacheFirst(req));
    return;
  }

  // CDN assets we serve from cache on revisit but refresh in the
  // background, with an LRU cap so opaque entries (which Chrome pads to
  // ~7MB each for quota accounting) can't silently exhaust origin quota.
  // Anything outside this list (map tiles, Nominatim search, jpfoodmap
  // sync API) passes straight through to the browser default.
  // M-011: cdnjs serves MarkerCluster's JS + both its stylesheets, and a
  // host that isn't listed here never reaches respondWith at all — so an
  // offline/blocked cdnjs left the boot hard-stuck on L.markerClusterGroup
  // with nothing in Cache Storage to fall back to.
  if (/(\.tile\.openstreetmap\.org$)|(^[a-c]\.tile\.)/i.test(url.hostname) ||
      url.hostname === 'emojicdn.elk.sh' ||
      url.hostname === 'cdn.jsdelivr.net' ||
      url.hostname === 'cdnjs.cloudflare.com' ||
      url.hostname === 'unpkg.com') {
    event.respondWith(staleWhileRevalidate(req, EXT_CACHE, true));
  }
});

// Opaque (cross-origin no-cors) responses report ok=false / status=0 but
// we still want them cached for revisits.
function cacheable(resp) {
  return !!resp && (resp.ok || resp.type === 'opaque');
}

const EXT_CACHE_MAX_ENTRIES = 200;
async function trimCache(cacheName, max) {
  try {
    const cache = await caches.open(cacheName);
    const keys = await cache.keys();   // insertion order — oldest first
    for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
  } catch (_) {}
}

async function cacheFirst(req, cacheName = DATA_CACHE) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  if (cached) return cached;
  const fresh = await fetch(req);
  if (cacheable(fresh)) cache.put(req, fresh.clone());
  return fresh;
}

// Navigations prefer fresh HTML, but a returning user with a good cached
// copy shouldn't stare at a blank page while a flaky connection crawls to
// the browser's own timeout (roaming travelers are this site's core
// audience). If the network hasn't answered within NAV_TIMEOUT_MS and a
// cached copy exists, serve it; the fetch keeps running via waitUntil and
// still refreshes the cache for next time.
const NAV_TIMEOUT_MS = 3500;
async function networkFirst(req, event) {
  const cache = await caches.open(SHELL_CACHE);
  const fetchP = fetch(req).then((fresh) => {
    if (cacheable(fresh)) cache.put(req, fresh.clone());
    return fresh;
  });
  if (event) event.waitUntil(fetchP.catch(() => {}));
  // Root is pre-cached during installation. It is also the safe fallback
  // for an offline /index.html navigation or an unexpected same-scope URL.
  const cached = await cache.match(req) || await cache.match('./');
  if (!cached) return fetchP;
  let timer;
  try {
    return await Promise.race([
      fetchP.catch(() => cached),
      new Promise((res) => { timer = setTimeout(() => res(cached), NAV_TIMEOUT_MS); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function staleWhileRevalidate(req, cacheName, capped) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  const fresh = fetch(req).then(resp => {
    if (cacheable(resp)) {
      cache.put(req, resp.clone());
      // Amortized trim — keys() on every put would be pure overhead.
      if (capped && Math.random() < 0.02) trimCache(cacheName, EXT_CACHE_MAX_ENTRIES);
    }
    return resp;
  }).catch(() => cached);
  return cached || fresh;
}
"""


FILTER_JS_TEMPLATE = r"""
<script>
(function() {
  var EMBEDDED_BOOKMARKS = __BOOKMARKS__;
  // Repo-shipped landmarks. Rendered fresh on every page load (not stored
  // in localStorage), so an updated favorites_builtin.json + redeploy
  // immediately reaches every visitor regardless of their sync state.
  // Read-only from the UI — the per-pin popup omits the delete button.
  var EMBEDDED_FAVORITES_BUILTIN = __FAVORITES_BUILTIN__;
  // bucket → marker halo color, and bucket name → emoji glyph. Inlined from
  // PRICE_BUCKETS / GENRE_EMOJI in map_data.py so each restaurants.json row
  // only needs to carry the small keys (bucket / categories[0]), not the
  // resolved color / emoji. ~30 bytes saved per row × 9800 rows ≈ 300 KB
  // off restaurants.json on every cold load.
  var BUCKET_COLOR = __BUCKET_COLORS__;
  var GENRE_EMOJI  = __GENRE_EMOJI__;
  // Per-char variant → canonical (simplified Chinese) table. Used by the
  // search box to normalize the query so "烧" matches names containing the
  // JP shinjitai 焼, etc. Restaurant names are canonicalized on demand
  // (lazily, cached on the row object) so the payload stays lean.
  var HAN_VARIANTS = __HAN_VARIANTS__;
  // City / ward / prefecture stems (canonical form) harvested from the
  // dataset's addresses. The search box uses this as a strict whitelist:
  // the trailing token of a multi-word query is only treated as a location
  // filter when it matches one of these exactly. Otherwise the whole query
  // collapses into a single restaurant-name search. Keeps disambiguators
  // like "炭火烧鸟 正" working as name search even though "正" looks like
  // it could be a location token.
  var KNOWN_LOCS = new Set(__KNOWN_LOCS__);
  // Google OAuth Web Client ID — inlined at build time. Public by design;
  // the GIS library uses it to know which app is asking for sign-in.
  var GOOGLE_CLIENT_ID = '__GOOGLE_CLIENT_ID__';
  // Build-time Simplified -> Traditional lookup. Keys are exact CJK
  // runs that appear anywhere on the rendered page; values are their
  // OpenCC s2t conversion (full multi-char rules applied at build, so
  // 拉面->拉麵 and 内脏->內臟 land correctly). localizeTree() walks text
  // nodes and runs the CJK-run regex over each, replacing matched runs
  // via this table; runs without an entry are passed through unchanged.
  // Subtrees marked lang="ja" are skipped wholesale so Japanese names,
  // addresses and shinjitai genre tokens stay in their source form.
  var TEXT_TRAD_MAP = __TEXT_TRAD_MAP__;
  // English lookup, same shape — keys are CJK runs in the rendered
  // page, values come from data/i18n/en.json. Runs without an entry
  // stay in Chinese at runtime (the build log lists what's missing).
  var TEXT_EN_MAP = __TEXT_EN_MAP__;
  // Japanese lookup, same shape. Values come from data/i18n/ja.json.
  // Runs without an entry stay in Chinese at runtime.
  var TEXT_JA_MAP = __TEXT_JA_MAP__;
  function normalizeForSearch(s) {
    // Strip whitespace so "中国料理眺游楼" matches names that carry spaces
    // ("中国料理 眺遊楼..."). NFKC already folds full-width U+3000 to a
    // regular space, so the post-NFKC \s+ rip catches both.
    s = (s == null ? '' : String(s)).normalize('NFKC').toLowerCase()
         .replace(/\s+/g, '');
    var out = '';
    for (var i = 0; i < s.length; i++) {
      var c = s[i];
      out += HAN_VARIANTS[c] || c;
    }
    return out;
  }
  function rowNameNorm(d) {
    if (d._nm == null) d._nm = normalizeForSearch(d.name || '');
    return d._nm;
  }
  // Service worker registration. Caches restaurants.json, popups.json,
  // transit GeoJSON, map tiles + emoji CDN on first fetch so repeat visits
  // (and second-tab loads) skip the network for the heavy bits. Failures
  // are non-fatal — the page works without it (e.g. file:// preview).
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function() {
      navigator.serviceWorker.register('./sw.js').catch(function(err) {
        console.warn('[tabelog] SW registration failed:', err);
      });
      // M-062 / M-061: two things only the page knows. The popups variant
      // depends on activeLang (resolved further down, before 'load' fires),
      // and launcher icons are worth 142KB only to an installed app.
      try {
        navigator.serviceWorker.ready.then(function(reg) {
          var sw = reg && (reg.active || navigator.serviceWorker.controller);
          if (!sw) return;
          try { sw.postMessage({type: 'WARM_DATA', url: popupsUrlForLang()}); }
          catch (_) {}
          var installed = false;
          try {
            installed = (window.matchMedia
                         && matchMedia('(display-mode: standalone)').matches)
                        || navigator.standalone === true;
          } catch (_) {}
          if (installed) {
            try { sw.postMessage({type: 'WARM_LAUNCHER_ICONS'}); } catch (_) {}
          }
        }).catch(function() {});
      } catch (_) {}
    });
    window.addEventListener('appinstalled', function() {
      try {
        navigator.serviceWorker.ready.then(function(reg) {
          var sw = reg && (reg.active || navigator.serviceWorker.controller);
          if (sw) sw.postMessage({type: 'WARM_LAUNCHER_ICONS'});
        }).catch(function() {});
      } catch (_) {}
    });
  }

  // ===== Lazy popup loader =====
  // The rendered popup HTML for all restaurants lives in docs/data/popups.json
  // (one entry per Tabelog detail_url). It's ~4 MB gzipped, so we don't pull
  // it on boot — only when the user taps the first marker. After that the
  // map serves popups instantly from memory. The single shared promise means
  // a second tap during the first fetch reuses it rather than racing.
  var popupsMap = null;
  var popupsPromise = null;
  // M-062: split out of loadPopups so the SW warmup message can name the
  // exact same (content-hashed) URL the first marker tap will request.
  function popupsUrlForLang() {
    // Each UI language gets its own popups file:
    //   zh-TW -> popups-tw.json (policy + ribbons via OpenCC s2t)
    //   en    -> popups-en.json (policy overlaid from policy_en.json,
    //            falls back to Chinese for untranslated entries)
    //   zh-CN -> popups.json (the default)
    // Japanese fields (genre/station/address) are byte-identical across
    // all three variants — the picker only swaps the Chinese parts.
    var popupsUrl = 'data/popups.json';
    if (typeof activeLang !== 'undefined') {
      if (activeLang === 'zh-TW') popupsUrl = 'data/popups-tw.json';
      else if (activeLang === 'en') popupsUrl = 'data/popups-en.json';
      else if (activeLang === 'ja') popupsUrl = 'data/popups-ja.json';
    }
    return popupsUrl;
  }
  function loadPopups() {
    if (popupsMap) return Promise.resolve(popupsMap);
    if (popupsPromise) return popupsPromise;
    popupsPromise = fetch(popupsUrlForLang(), {cache: 'force-cache'})
      .then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function(j) { popupsMap = j; return j; })
      .catch(function(e) {
        console.error('[tabelog] popups.json load failed:', e);
        popupsPromise = null;
        throw e;
      });
    return popupsPromise;
  }

  // ===== Apple-style emoji rendering \u2014 local PNGs with emojicdn fallback ====
  // Windows ships no flag glyphs and Linux/Android render emoji inconsistently,
  // so we serve everything as Apple-style PNGs. The glyphs we know about
  // upfront (genre buckets, attraction pins, UI labels) are pre-downloaded to
  // docs/emoji/<hex>.png by build_emoji_cache.py and looked up via EMOJI_MAP.
  // Anything not pre-cached (e.g. a user typing an arbitrary emoji into a
  // bookmark name) falls back to emojicdn.elk.sh, so visuals stay Apple-style
  // either way \u2014 same PNG source, just one-shot at build time vs per-visitor
  // at runtime for the bulk of marker glyphs.
  var EMOJI_RE = /[\u{1F1E6}-\u{1F1FF}][\u{1F1E6}-\u{1F1FF}]|\p{Emoji_Presentation}|\p{Emoji}\uFE0F/gu;
  var EMOJI_MAP = __EMOJI_MANIFEST__;
  // M-133: alt="" used to be the one attribute that took its argument raw.
  // Every current call site launders the glyph through sanitizeBookmarkEmoji
  // first, so nothing is known to reach here dirty — this is the second
  // layer, so a future call site can't quietly turn it into an injection.
  function escAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function emojiImg(m, extraStyle) {
    var key = EMOJI_MAP[m];
    var src = key
      ? 'emoji/' + key + '.png'
      : 'https://emojicdn.elk.sh/' + encodeURIComponent(m) + '?style=apple';
    return '<img src="' + src + '" alt="' + escAttr(m) + '" draggable="false" ' +
           'style="height:1em;width:1em;vertical-align:-0.15em;' +
           'display:inline-block;' + (extraStyle || '') + '">';
  }
  function emojiHtml(s) {
    if (!s) return '';
    return String(s).replace(EMOJI_RE, function(m) { return emojiImg(m); });
  }
  function setEmojiHtml(el, text) {
    if (el) el.innerHTML = emojiHtml(text);
  }
  // DOM-building twin of emojiImg(). emojify() needs real <img> elements
  // rather than HTML strings — a text node's nodeValue holds decoded
  // characters (literal '<', '&', etc.), so going back through innerHTML
  // would reparse them as markup and turn previously-escaped user content
  // like "&lt;img onerror=...&gt;" into a live tag.
  function emojiImgNode(m) {
    var key = EMOJI_MAP[m];
    var src = key
      ? 'emoji/' + key + '.png'
      : 'https://emojicdn.elk.sh/' + encodeURIComponent(m) + '?style=apple';
    var img = document.createElement('img');
    img.src = src;
    img.alt = m;
    img.draggable = false;
    img.style.cssText = 'height:1em;width:1em;vertical-align:-0.15em;display:inline-block;';
    return img;
  }
  function emojify(root) {
    if (!root || root.nodeType !== 1) return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function(n) {
        var p = n.parentNode;
        if (!p) return NodeFilter.FILTER_REJECT;
        var t = p.tagName;
        if (t === 'SCRIPT' || t === 'STYLE' || t === 'TEXTAREA' || t === 'INPUT') {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var nodes = [], n;
    while ((n = walker.nextNode())) nodes.push(n);
    nodes.forEach(function(tn) {
      var v = tn.nodeValue;
      if (!v) return;
      EMOJI_RE.lastIndex = 0;
      if (!EMOJI_RE.test(v)) return;
      var span = document.createElement('span');
      var last = 0;
      EMOJI_RE.lastIndex = 0;
      var m;
      while ((m = EMOJI_RE.exec(v)) !== null) {
        if (m.index > last) {
          span.appendChild(document.createTextNode(v.slice(last, m.index)));
        }
        span.appendChild(emojiImgNode(m[0]));
        last = m.index + m[0].length;
      }
      if (last < v.length) {
        span.appendChild(document.createTextNode(v.slice(last)));
      }
      tn.replaceWith(span);
    });
  }
  // The emoji observers are merged with the i18n ones — one MutationObserver
  // per container running both passes; see observeDynamic below (defined
  // after I18N_MAP exists). The one-shot emojify(document.body) runs there
  // too, same synchronous eval, so nothing paints un-swapped.

  // ===== Runtime Simplified -> Traditional conversion =====
  // Page is authored in Simplified Chinese. When the user opts into 繁體
  // (via ?lang=tw or the picker), tradifyTree() walks text nodes and
  // applies S2T_MAP per character. Subtrees with lang="ja" are skipped
  // wholesale — that's how restaurant names / addresses / Japanese genre
  // strings stay in shinjitai instead of getting mangled into kyūjitai.
  //
  // langActive is set once on boot from URL+localStorage and never flips
  // mid-session — the picker triggers a reload so the page boots fresh
  // in the new language.
  var LANG_KEY = 'tabelog.lang';
  function readLangParam() {
    try {
      var p = new URLSearchParams(window.location.search).get('lang');
      if (p === 'tw' || p === 'zh-TW') return 'zh-TW';
      if (p === 'cn' || p === 'zh-CN') return 'zh-CN';
      if (p === 'en') return 'en';
      if (p === 'ja' || p === 'jp') return 'ja';
    } catch (_) {}
    return null;
  }
  var urlLang = readLangParam();
  var storedLang = null;
  try { storedLang = localStorage.getItem(LANG_KEY); } catch (_) {}
  var activeLang = urlLang || storedLang || 'zh-CN';
  // If the URL pinned a lang, persist it so subsequent visits without the
  // param keep the same setting.
  if (urlLang) {
    try { localStorage.setItem(LANG_KEY, urlLang); } catch (_) {}
  }
  // Pick the active translation table. zh-CN (or anything unknown) gets
  // no map -> the localization pass is a no-op.
  var I18N_MAP = null;
  if (activeLang === 'zh-TW' && Object.keys(TEXT_TRAD_MAP).length) {
    I18N_MAP = TEXT_TRAD_MAP;
  } else if (activeLang === 'en' && Object.keys(TEXT_EN_MAP).length) {
    I18N_MAP = TEXT_EN_MAP;
  } else if (activeLang === 'ja' && Object.keys(TEXT_JA_MAP).length) {
    I18N_MAP = TEXT_JA_MAP;
  }

  // Matches a maximal CJK ideograph run — BMP unified ideographs +
  // Extension A + the compatibility block. Mirrors the Python-side
  // _CJK_RUN_RE so build-time and runtime tokenize identically.
  var CJK_RUN_RE = /[㐀-鿿豈-﫿]+/g;
  function localizeText(s) {
    if (!s || !I18N_MAP) return s;
    return s.replace(CJK_RUN_RE, function(m) {
      var t = I18N_MAP[m];
      return t === undefined ? m : t;
    });
  }
  // M-104: CJK punctuation looks wrong once the surrounding run has been
  // translated into English ("Signed in，reloading"). Latin scripts get the
  // ASCII form; the CJK languages keep the full-width one.
  function l10nComma() { return activeLang === 'en' ? ', ' : '，'; }
  function l10nParen(inner) {
    return activeLang === 'en' ? ' (' + inner + ')' : '（' + inner + '）';
  }
  // Join several short zh-CN clauses into one sentence. Each clause is a
  // whole CJK run, so it survives the tokenizer intact and gets one
  // hand-written translation in data/i18n/*.json — the alternative (one long
  // string with punctuation inside) is exactly what produced
  // "You booked 25 meters In range" in M-103.
  function l10nSentence(parts) {
    var sep = activeLang === 'en' ? '. ' : '，';
    var out = [];
    for (var i = 0; i < parts.length; i++) out.push(localizeText(parts[i]));
    return out.join(sep) + (activeLang === 'en' ? '.' : '。');
  }
  function localizeTree(root) {
    if (!root || !I18N_MAP) return;
    if (root.nodeType !== 1) return;
    // The root may itself sit inside a skipped scope — check its ancestor
    // chain once here; below, rejected ELEMENTS prune their whole subtree
    // in the walker, so the per-text-node cost is O(1) instead of the old
    // walk-every-ancestor-per-text-node O(depth).
    for (var p = root; p && p.nodeType === 1; p = p.parentNode) {
      var pt = p.tagName;
      if (pt === 'SCRIPT' || pt === 'STYLE' || pt === 'TEXTAREA' || pt === 'INPUT') return;
      // M-098: lang="ja" is the "this is Japanese source text, don't
      // translate" sentinel — but in ja mode we also stamp lang="ja" onto
      // the root element, which made this guard return on the very first
      // hop and killed every MutationObserver-driven localizeTree. Skip the
      // root itself; real sentinel subtrees still prune.
      if (p !== document.documentElement && p.getAttribute && p.getAttribute('lang') === 'ja') return;
    }
    var walker = document.createTreeWalker(
      root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: function(n) {
        if (n.nodeType === 1) {
          var t = n.tagName;
          if (t === 'SCRIPT' || t === 'STYLE' || t === 'TEXTAREA' || t === 'INPUT') {
            return NodeFilter.FILTER_REJECT;   // prunes the subtree
          }
          if (n.getAttribute('lang') === 'ja') return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_SKIP;       // descend, don't emit
        }
        return NodeFilter.FILTER_ACCEPT;       // text node
      }
    });
    var nodes = [], n;
    while ((n = walker.nextNode())) nodes.push(n);
    nodes.forEach(function(tn) {
      var v = tn.nodeValue;
      if (!v) return;
      var out = localizeText(v);
      if (out !== v) tn.nodeValue = out;
    });
  }
  // One MutationObserver per container running both dynamic passes (emoji
  // PNG swap + CJK localization). The old scheme attached two observers to
  // each container, so every insertion was walked twice — and each emoji
  // text→<img> replacement re-triggered both for a settle pass.
  function observeDynamic(root, doEmoji, doI18n) {
    if (!root) return;
    var i18nOn = doI18n && !!I18N_MAP;
    if (!doEmoji && !i18nOn) return;
    new MutationObserver(function(muts) {
      for (var i = 0; i < muts.length; i++) {
        var added = muts[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var nd = added[j];
          var el = nd.nodeType === 1 ? nd
                 : (nd.nodeType === 3 ? nd.parentNode : null);
          if (!el) continue;
          if (doEmoji) emojify(el);
          if (i18nOn) { localizeTree(el); applyAttrL10n(el); }  // M-099
        }
      }
    }).observe(root, {childList: true, subtree: true});
  }
  // M-099: localizeTree walks TEXT nodes only, so every title / aria-label
  // stayed in zh-CN in all four languages — 17 of them, including the whole
  // FAB stack and both modal close buttons. Same shape as PLACEHOLDER_L10N
  // below (which does the same job for placeholders), except the value is
  // the zh-CN source string and the per-language text comes from the same
  // TEXT_*_MAP tables the text pass uses — so a translation lands in exactly
  // one place (data/i18n/*.json) instead of being duplicated per language.
  // Selector-keyed (not id-keyed) because several of these are classes with
  // more than one instance on the page.
  var ATTR_L10N = [
    ['#ff-fab',                        'title',      '筛选'],
    ['.ff-help-trigger[aria-label]',   'aria-label', '说明'],
    ['.map-fab-stack',                 'aria-label', '图层切换'],
    ['#fab-locate',                    'title',      '定位到我的位置'],
    ['#fab-locate',                    'aria-label', '定位到我的位置'],
    ['#fab-transit-long',              'title',      '新干线 / JR 长途线路'],
    ['#fab-transit-city',              'title',      '地铁 / 私铁 / 城市轨道'],
    ['#fab-attractions',               'title',      '景点锚点'],
    ['#fab-bookmarks',                 'title',      '我的收藏'],
    ['#ss-clear',                      'aria-label', '清空'],
    ['#ss-avatar',                     'aria-label', '账户'],
    ['.bm-close',                      'aria-label', '关闭'],
    ['.bm-kind-seg',                   'aria-label', '类型'],
    ['#bm-emoji-more',                 'title',      '打开完整 emoji 选择器'],
    ['.imp-close',                     'aria-label', '关闭'],
    ['#ss-menu .ssm-langs',            'aria-label', '语言'],
    // Built at runtime — reached through observeDynamic below, which runs
    // this pass over every inserted subtree for exactly these two.
    ['.rst-close',                     'aria-label', '关闭'],
    ['.ss-fav',                        'title',      '加入收藏']
  ];
  function applyAttrL10n(root) {
    var scope = root || document;
    for (var i = 0; i < ATTR_L10N.length; i++) {
      var sel = ATTR_L10N[i][0], attr = ATTR_L10N[i][1];
      var txt = localizeText(ATTR_L10N[i][2]);
      var hits;
      try {
        if (scope.nodeType === 1 && scope.matches && scope.matches(sel)) {
          scope.setAttribute(attr, txt);
        }
        hits = scope.querySelectorAll(sel);
      } catch (_) { continue; }
      for (var j = 0; j < hits.length; j++) hits[j].setAttribute(attr, txt);
    }
  }
  // M-103: the leaflet-locate popup template used to be a single zh-CN
  // string whose CJK runs ('你在约' / '范围内') were tokenized and looked up
  // independently — both tables read the first one as "预约", so English
  // users got "You booked 25 meters In range". Whole-sentence per-language
  // override, same pattern as chipText / gcalTxt, plus the unit strings the
  // plugin otherwise leaves as English "meters" / "feet". The source runs
  // here are unambiguous ones translated in data/i18n/*.json.
  var LOCATE_STRINGS = {
    title:               '显示我的位置',
    popup:               '距离约 {distance} {unit}',
    outsideMapBoundsMsg: '当前位置在地图范围之外',
    metersUnit:          '米',
    feetUnit:            '英尺'
  };
  Object.keys(LOCATE_STRINGS).forEach(function(k) {
    LOCATE_STRINGS[k] = localizeText(LOCATE_STRINGS[k]);
  });
  function startDynamicObservers() {
    // One-shot passes over the static page (filter panel, FAB labels,
    // modal titles…) — these never change after load.
    emojify(document.body);
    if (I18N_MAP) localizeTree(document.body);
    applyAttrL10n();   // M-099
    // Ongoing observers only on the containers that mutate with
    // emoji/CJK-bearing HTML at runtime. Marker divIcons pre-swap their
    // emoji at construction (makeIcon → emojiImg) so they stay unobserved;
    // the Leaflet popup pane is hooked by initMap() once it exists.
    observeDynamic(document.getElementById('bs-content'), true, true);
    observeDynamic(document.getElementById('bm-modal'), true, true);
    observeDynamic(document.getElementById('ss-list'), true, true);
    // Filter sheet has dynamic textContent rewrites (cuisine summary
    // "全部"/"无"/"已选 N / M", live count chips) but no runtime emoji.
    observeDynamic(document.getElementById('ff-sheet-content'), false, true);
    // Reflect onto <html lang> — browsers use it for hyphenation and
    // accessibility (screen readers, especially).
    // M-083: unconditional. zh-CN has no I18N_MAP, so the old `if (I18N_MAP)`
    // guard meant the default page (most visits) never got a lang at all.
    // The static <html lang="zh-CN"> from main()'s post-processing covers
    // first paint; this keeps it in sync when a language is selected.
    try { document.documentElement.lang = activeLang; } catch (_) {}
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startDynamicObservers);
  } else {
    startDynamicObservers();
  }
  // Exposed so initMap() can hook the Leaflet popup pane once Leaflet has
  // built its panes.
  window.__observeDynamic = observeDynamic;
  window.__localizeTree = localizeTree;
  window.__activeLang = activeLang;

  // ===== Cloud sync layer (Google OAuth + Cloudflare Worker + KV) =====
  //
  // Auth is a Worker-issued session cookie scoped to .jpfoodmap.com, 90-day
  // lifetime. POST /api/session exchanges a Google id_token for the cookie;
  // afterwards every /api/state call rides the cookie via credentials:'include'
  // and we never touch Google again until the session expires or is revoked.
  // localStorage 'tabelog.auth' mirrors the server-side exp + profile so the
  // page knows whether it's signed in before any network call. The state
  // blob (favorites/blacklist + bookmarks) is cached locally so the page
  // works offline / before the first pull.
  //
  // Legacy: pages loaded against the old Worker stored {id_token, exp:1h}
  // and used Authorization:Bearer. The new Worker still accepts Bearer as a
  // fallback path, and the boot logic below upgrades any legacy id_token to
  // a cookie session silently on next load.
  var AUTH_KEY = 'tabelog.auth';
  var CACHE_KEY = 'omakase_state_cache_v2';
  var API_BASE = 'https://api.jpfoodmap.com/api';
  var API = API_BASE + '/state';
  var API_SESSION = API_BASE + '/session';
  var API_ME = API_BASE + '/me';

  // M-054: every loader below used to trust anything that merely parsed as
  // JSON. 'null', a number, an object where an array belongs — all parse
  // fine and then blow up at the first property access or new Set(), taking
  // the whole boot with them. Each loader now type-checks, and stashes the
  // unreadable original under "<key>.corrupt" (new key, write-once) so a
  // hand-recoverable blob isn't silently overwritten by the next save.
  function keepCorrupt(key, raw) {
    if (raw == null) return;
    var ck = (key.indexOf('tabelog.') === 0 ? key : 'tabelog.' + key) + '.corrupt';
    try {
      if (localStorage.getItem(ck) === null) localStorage.setItem(ck, raw);
    } catch (_) { /* private mode / quota — nothing we can do, don't throw */ }
    console.warn('[tabelog] unreadable ' + key + ' preserved at ' + ck);
  }
  function isPlainObject(x) {
    return !!x && typeof x === 'object' && !Array.isArray(x);
  }
  function loadAuth() {
    var raw = null;
    try {
      raw = localStorage.getItem(AUTH_KEY);
      var a = JSON.parse(raw || '{}');
      if (!isPlainObject(a)) { keepCorrupt(AUTH_KEY, raw); return {}; }
      return a;
    }
    catch (_) { keepCorrupt(AUTH_KEY, raw); return {}; }
  }
  function saveAuth(a) {
    // Safari private mode and "block all cookies" both throw here; a dead
    // write must not take down the caller (sign-in, silent re-auth, pull).
    try { localStorage.setItem(AUTH_KEY, JSON.stringify(a)); } catch (_) {}
  }
  // Async sign-out: revoke the server cookie first, then clear local state.
  // Survives network errors — we always reload so the page rebuilds in the
  // signed-out state regardless of what the Worker said.
  // M-041: opts.clearLocal also wipes this device's favorites / blacklist /
  // bookmarks / merge base. That is one of the very few places allowed to
  // remove those keys, and only on an explicit user choice: callers that
  // pass no opts (the legacy account-menu handler) get a confirm() so the
  // next person to sign in on a shared browser doesn't inherit — and
  // upload — the previous account's data.
  function signOut(opts) {
    var clearLocal = false;
    if (opts && typeof opts === 'object') {
      clearLocal = !!opts.clearLocal;
    } else {
      try {
        clearLocal = confirm(localizeText(
          '同时清除本设备数据？（收藏、弃用、书签会从本设备删除；云端数据不受影响，下次登录会重新下载）'));
      } catch (_) { clearLocal = false; }
    }
    var done = false;
    function wipe() {
      try { localStorage.removeItem(AUTH_KEY); } catch (_) {}
      if (clearLocal) {
        // Literal key for bookmarks: BM_KEY is declared inside initMap.
        try { localStorage.removeItem(CACHE_KEY); } catch (_) {}
        try { localStorage.removeItem('tabelog.bookmarks'); } catch (_) {}
        try { localStorage.removeItem(SYNC_BASE_KEY); } catch (_) {}
      }
    }
    function finish() {
      if (done) return; done = true;
      wipe();
      // location.reload() only schedules the navigation: a response that
      // is already in flight (the boot-time /api/me probe, a pull) can
      // still run before the document unloads and write the profile /
      // state back, so the reloaded page would boot signed in again (and
      // then even try GIS auto-select). Wipe once more at the last
      // possible moment.
      window.addEventListener('pagehide', wipe);
      location.reload();
    }
    try {
      fetch(API_SESSION, {method: 'DELETE', credentials: 'include'})
        .catch(function(){}).finally(finish);
    } catch (_) { finish(); }
    // Hard timeout in case the Worker hangs.
    setTimeout(finish, 1500);
  }

  // Shape contract (relied on by the sync layer): always returns
  // {fav: array|null, black: array|null, dirty: bool}. M-054 only tightens
  // what can come out of it — a non-array fav/black now degrades to null
  // (same as "no cached state") instead of reaching new Set() and throwing.
  function loadCache() {
    var raw = null;
    try {
      raw = localStorage.getItem(CACHE_KEY);
      var d = JSON.parse(raw || '{}');
      if (!isPlainObject(d)) { keepCorrupt(CACHE_KEY, raw); return {fav: null, black: null, dirty: false}; }
      var fav   = Array.isArray(d.fav)   ? d.fav   : null;
      var black = Array.isArray(d.black) ? d.black : null;
      if ((d.fav != null && fav === null) || (d.black != null && black === null)) {
        keepCorrupt(CACHE_KEY, raw);
      }
      return {fav: fav, black: black, dirty: !!d.dirty};
    } catch (_) { keepCorrupt(CACHE_KEY, raw); return {fav: null, black: null, dirty: false}; }
  }
  // Three-way merge, keep = (ours ∩ theirs) ∪ (ours − base) ∪ (theirs − base).
  // Adds from both sides survive; a delete on either side wins unless the
  // other side re-added. With an empty base (fresh upgrade, cleared
  // storage) this degrades to a pure union — it may resurrect a
  // concurrently-deleted entry once, but can never lose one. Pure and
  // top-level because both the cloud paths (base = syncBase) and the
  // localStorage paths (base = what this page last wrote) use it.
  function mergeSets(baseArr, oursSet, theirsArr) {
    var base   = new Set(Array.isArray(baseArr) ? baseArr : []);
    var theirs = new Set(Array.isArray(theirsArr) ? theirsArr : []);
    var out = new Set();
    oursSet.forEach(function(u) { if (theirs.has(u) || !base.has(u)) out.add(u); });
    theirs.forEach(function(u) { if (!base.has(u)) out.add(u); });
    return out;
  }
  // Same rule keyed by bookmark id. When both sides carry an id, this
  // device's object wins — unless we left it byte-identical to the base and
  // the other side changed it (a Wikidata backfill on another tab, say), in
  // which case theirs is the edit and ours is just stale. Legacy id-less
  // entries can't be tracked through the base, so ours are always kept and
  // theirs are kept unless byte-identical to one of ours — duplication risk
  // over data loss.
  function mergeBookmarks(baseArr, oursArr, theirsArr) {
    var baseById = {};
    (Array.isArray(baseArr) ? baseArr : []).forEach(function(b) {
      if (b && b.id) baseById[b.id] = JSON.stringify(b);
    });
    var theirsList = Array.isArray(theirsArr) ? theirsArr : [];
    var theirsById = {};
    theirsList.forEach(function(b) { if (b && b.id) theirsById[b.id] = b; });
    var has = function(o, k) { return Object.prototype.hasOwnProperty.call(o, k); };
    var out = [], seen = new Set();
    oursArr.forEach(function(b) {
      if (!b) return;
      if (!b.id) { out.push(b); return; }
      var inTheirs = has(theirsById, b.id), inBase = has(baseById, b.id);
      if (inTheirs || !inBase) {
        var pick = b;
        if (inTheirs && inBase && baseById[b.id] === JSON.stringify(b)) {
          pick = theirsById[b.id];
        }
        out.push(pick);
        seen.add(b.id);
      }
    });
    theirsList.forEach(function(b) {
      if (!b) return;
      if (!b.id) {
        var s = JSON.stringify(b);
        var dup = oursArr.some(function(o) {
          return o && !o.id && JSON.stringify(o) === s;
        });
        if (!dup) out.push(b);
        return;
      }
      if (seen.has(b.id)) return;
      if (!has(baseById, b.id)) out.push(b);
    });
    return out;
  }
  function sameStrList(a, b) {
    if (!Array.isArray(a) || !Array.isArray(b)) return a == null && b == null;
    if (a.length !== b.length) return false;
    var s = new Set(a);
    for (var i = 0; i < b.length; i++) if (!s.has(b[i])) return false;
    return true;
  }
  function setEqualsList(set, arr) {
    if (!Array.isArray(arr) || set.size !== arr.length) return false;
    for (var i = 0; i < arr.length; i++) if (!set.has(arr[i])) return false;
    return true;
  }

  // M-046: a storage write that throws (private mode, quota, site data
  // blocked) must never break the caller's chain — schedulePush used to
  // lose its push timer that way. The sync engine installs the hook that
  // tells the user; until then failures are silent.
  var storageBlockedHook = null;
  function storageBlocked(e) {
    try { if (storageBlockedHook) storageBlockedHook(e); } catch (_) {}
  }
  // M-003 / M-004: the localStorage cache is shared by every tab of this
  // origin, and each tab holds its own in-memory copy. Writes used to be
  // whole-blob overwrites, so the later tab silently erased the earlier
  // tab's edits (and its dirty flag). Now every write is a three-way merge:
  //   base   = what THIS page last wrote or last read (memory only — no new
  //            localStorage key),
  //   ours   = this page's state,
  //   theirs = what is on disk right now.
  // Not a union: with a base, an entry we removed stays removed instead of
  // being resurrected by the other tab's older list. dirty is ours alone
  // while the disk still holds exactly what we wrote; once someone else has
  // written, dirty = ours || theirs so their unsent edit keeps its mark. The
  // merged result is adopted into `state` as well — if only the disk got it,
  // the next write would read the other tab's additions (now in the base but
  // not in memory) as deletions of ours. Returns true when `state` changed
  // so the caller can repaint.
  var lastWrittenCache = null;   // {fav: [], black: [], dirty: bool} | null
  function saveCache(state, dirtyFlag) {
    var dirty = !!dirtyFlag, changed = false;
    var disk = loadCache();
    var base = lastWrittenCache;
    if (disk.fav && disk.black && base) {
      var untouched = sameStrList(disk.fav, base.fav)
                   && sameStrList(disk.black, base.black)
                   && !!disk.dirty === !!base.dirty;
      if (!untouched) {
        var fav   = mergeSets(base.fav   || [], state.fav,   disk.fav);
        var black = mergeSets(base.black || [], state.black, disk.black);
        if (!setEqualsList(state.fav,   fav))   { state.fav   = fav;   changed = true; }
        if (!setEqualsList(state.black, black)) { state.black = black; changed = true; }
        dirty = dirty || !!disk.dirty;
      }
    }
    var rec = {fav: Array.from(state.fav), black: Array.from(state.black), dirty: dirty};
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(rec));
      lastWrittenCache = rec;
    } catch (e) { storageBlocked(e); }
    return changed;
  }

  // Last server state this device has seen, plus the blob version it carried.
  // This is the common ancestor for the three-way merge that runs when a PUT
  // comes back 409 (another device wrote in between): with it we can tell an
  // add on one side from a delete on the other. Its own key — the state cache
  // above predates it and old pages must keep reading that cache unchanged.
  // M-002: `w` is the random write id our last PUT carried (or the one we
  // read back from the server); same v but a different w means our write
  // was silently overwritten by a concurrent PUT.
  var SYNC_BASE_KEY = 'tabelog.syncBase';
  function emptySyncBase(sub) {
    return {v: 0, w: '', sub: sub || '', favorites: [], blacklist: [], bookmarks: []};
  }
  function loadSyncBase() {
    try {
      var d = JSON.parse(localStorage.getItem(SYNC_BASE_KEY) || '{}');
      if (!d || typeof d !== 'object') d = {};
      return {
        v: typeof d.v === 'number' ? d.v : 0,
        w: typeof d.w === 'string' ? d.w : '',
        // Which Google account this snapshot belongs to. If the user signs
        // into a different account, the base must be discarded (see push) —
        // merging against another account's snapshot would misread all of
        // its entries as "deleted by the other side".
        sub: typeof d.sub === 'string' ? d.sub : '',
        favorites: Array.isArray(d.favorites) ? d.favorites : [],
        blacklist: Array.isArray(d.blacklist) ? d.blacklist : [],
        bookmarks: Array.isArray(d.bookmarks) ? d.bookmarks : [],
      };
    } catch (_) {
      return emptySyncBase('');
    }
  }
  function saveSyncBase(base) {
    try { localStorage.setItem(SYNC_BASE_KEY, JSON.stringify(base)); }
    catch (_) { /* quota — merge degrades to union, never loses data */ }
  }

  // Single source of truth for "are we currently signed in". The mirrored
  // session info (sub OR legacy id_token) must exist and the cached server-
  // side exp must still be in the future. Returns the auth object (with
  // email/name/picture) on success, null otherwise.
  function configured() {
    var a = loadAuth();
    if (!a.sub && !a.id_token) return null;
    if (a.exp && Date.now() >= a.exp) return null;
    return a;
  }
  // Helper for every authed fetch. credentials:'include' lets the cookie
  // ride; Bearer is only appended when a legacy id_token is still around
  // (Worker prefers cookie when both are present).
  function fetchAuthed(url, opts) {
    opts = opts || {};
    opts.credentials = 'include';
    var a = loadAuth();
    if (a && a.id_token) {
      opts.headers = Object.assign({}, opts.headers || {}, {
        'Authorization': 'Bearer ' + a.id_token
      });
    }
    return fetch(url, opts);
  }
  // Trade a Google id_token for a 90-day Worker session cookie. The Worker
  // verifies the id_token via Google tokeninfo and returns the signed-in
  // profile + server-side exp. cb(true, profile) on success.
  function exchangeForSession(idToken, cb) {
    cb = cb || function(){};
    fetch(API_SESSION, {
      method: 'POST',
      credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id_token: idToken})
    }).then(function(r) {
      if (!r.ok) { cb(false); return; }
      return r.json().then(function(p) { cb(true, p); });
    }).catch(function() { cb(false); });
  }
  // Helper used by all "we just got a fresh server session" paths. Stores
  // the profile without an id_token — the cookie is the source of truth
  // for sync; localStorage just mirrors the visible profile + exp.
  function saveSessionProfile(p) {
    saveAuth({
      sub: p.sub,
      email: p.email || '',
      name: p.name || '',
      picture: p.picture || '',
      exp: p.exp || 0
    });
  }
  // Cheap "am I still signed in" probe — the Worker reads the cookie and
  // returns the profile, no Google round-trip. cb(true) on success and
  // localStorage is refreshed with the latest server exp.
  function tryMe(cb) {
    cb = cb || function(){};
    fetch(API_ME, {credentials: 'include'})
      .then(function(r) {
        if (!r.ok) { cb(false); return; }
        return r.json().then(function(p) {
          saveSessionProfile(p);
          cb(true);
        });
      })
      .catch(function() { cb(false); });
  }

  // Bounded dependency wait. The old unbounded 50ms poll meant that if an
  // ad blocker or captive Wi-Fi portal ate one plugin script, the user got
  // a dead basemap with zero explanation while a 20 Hz timer spun forever.
  // ~10s covers even a very slow CDN; past that we say so, visibly.
  var initMapAttempts = 0;
  // M-011 / M-075 / M-168: the one visible failure surface for boot.
  //   kind 'deps' — Leaflet/MarkerCluster/locate never showed up
  //   kind 'data' — restaurants.json failed or timed out
  // Wording splits on navigator.onLine: blaming an ad blocker is a bad
  // guess when the phone is simply in a tunnel, which is the common case
  // for this site's on-the-road half. role="alert" makes it reach screen
  // readers, and the retry button matters most in an installed PWA, which
  // has no address bar to reload from.
  function hideBootFailure() {
    var old = document.getElementById('boot-fail-banner');
    if (old && old.parentNode) old.parentNode.removeChild(old);
  }
  function bootFailureText(kind) {
    var offline = (typeof navigator !== 'undefined' && navigator.onLine === false);
    if (kind === 'data') {
      return offline
        ? '当前处于离线状态 — 餐厅数据无法加载，恢复网络后请重试'
        : '餐厅数据加载失败 — 请检查网络后刷新';
    }
    return offline
      ? '当前处于离线状态 — 地图组件无法加载，恢复网络后请重试'
      : '地图组件加载失败（网络问题或广告拦截插件）— 请刷新重试';
  }
  function showBootFailure(kind, onRetry) {
    hideBootFailure();
    var el = document.createElement('div');
    el.id = 'boot-fail-banner';
    el.setAttribute('role', 'alert');
    el.style.cssText =
      'position:fixed;top:12px;left:50%;transform:translateX(-50%);' +
      'z-index:99999;background:#dc2626;color:#fff;padding:10px 16px;' +
      'border-radius:8px;font:13px -apple-system,BlinkMacSystemFont,' +
      "'Segoe UI',sans-serif;box-shadow:0 4px 12px rgba(0,0,0,0.3);" +
      'max-width:calc(100vw - 32px);text-align:center;';
    var msg = document.createElement('span');
    msg.textContent = localizeText(bootFailureText(kind));
    el.appendChild(msg);
    if (typeof onRetry === 'function') {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = localizeText('重试');
      btn.style.cssText =
        'margin-left:10px;padding:3px 12px;border:1px solid #fff;' +
        'border-radius:6px;background:transparent;color:#fff;font:inherit;' +
        'cursor:pointer;';
      btn.addEventListener('click', function() {
        hideBootFailure();
        try { onRetry(); } catch (e) { console.error('[tabelog] retry failed:', e); }
      });
      el.appendChild(btn);
    }
    document.body.appendChild(el);
  }
  function initMap(data) {
    function again() {
      // M-075: a failed dependency can only be retried by reloading (the
      // script tags are already spent), but an installed PWA has no
      // address bar — so the banner carries the button.
      if (++initMapAttempts > 200) {
        showBootFailure('deps', function() { location.reload(); });
        return;
      }
      setTimeout(function(){ initMap(data); }, 50);
    }
    var mapEl = document.querySelector('.folium-map');
    if (!mapEl) { again(); return; }
    // Find the leaflet map object that folium attached to this element.
    var mapId = mapEl.id;
    var map = window[mapId];
    if (!map) { again(); return; }
    if (typeof L === 'undefined' || !L.markerClusterGroup) { again(); return; }
    if (!L.control.locate) { again(); return; }

    // M-085: Leaflet's zoom/fade/marker animations and every flyTo ran at
    // full tilt even for a user who asked the OS for reduced motion — a
    // full-screen map that swoops is exactly the kind of movement that
    // triggers vestibular symptoms. The map object is built by folium, so
    // the options are patched here rather than passed at construction;
    // _zoomAnimated is the flag Leaflet actually consults at zoom time.
    // flyTo/flyToBounds are swapped for their instant equivalents so every
    // existing call site (search results, the locate control) lands the
    // same way without touching any of them. moveend still fires, so the
    // arrival handlers keep working.
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      map.options.zoomAnimation = false;
      map.options.fadeAnimation = false;
      map.options.markerZoomAnimation = false;
      map._zoomAnimated = false;
      var noAnim = function(options) {
        var o = {};
        if (options) { for (var k in options) o[k] = options[k]; }
        o.animate = false;
        return o;
      };
      map.flyTo = function(latlng, zoom, options) {
        return this.setView(latlng, zoom, noAnim(options));
      };
      map.flyToBounds = function(bounds, options) {
        return this.fitBounds(bounds, noAnim(options));
      };
    }

    // Late-bound emoji observer for Leaflet popups: search-result temp
    // marker and the right-click "加入收藏" popup both inject HTML into
    // .leaflet-popup-pane, which only exists after the map initializes.
    var popupPane = map.getPane && map.getPane('popupPane');
    if (popupPane && window.__observeDynamic) {
      window.__observeDynamic(popupPane, true, true);
    }

    // ===== Persisted map view =====
    // Restore last center+zoom before any tiles render, then track every
    // moveend (fires once per pan/zoom gesture, not per frame).
    var STATE_KEY_VIEW = 'tabelog.mapView';
    try {
      var savedView = JSON.parse(localStorage.getItem(STATE_KEY_VIEW) || 'null');
      if (savedView && typeof savedView.lat === 'number'
                    && typeof savedView.lon === 'number'
                    && typeof savedView.zoom === 'number') {
        map.setView([savedView.lat, savedView.lon], savedView.zoom, {animate: false});
      }
    } catch (e) {}
    var viewSaveTimer = 0;
    function saveViewNow() {
      try {
        var c = map.getCenter();
        localStorage.setItem(STATE_KEY_VIEW, JSON.stringify({
          lat: c.lat, lon: c.lng, zoom: map.getZoom()
        }));
      } catch (e) {}
    }
    // Debounced — animated flights fire moveend several times and each
    // write is synchronous storage I/O on the gesture-end path. pagehide
    // flushes so closing the tab right after a pan still persists.
    map.on('moveend', function() {
      clearTimeout(viewSaveTimer);
      viewSaveTimer = setTimeout(saveViewNow, 250);
    });
    window.addEventListener('pagehide', saveViewNow);
    // iOS Safari bfcache restore: the page comes back with stale container
    // dimensions, so tiles render at the wrong size (often a gray band on
    // the right edge or below the address bar). invalidateSize() forces
    // Leaflet to re-measure; if bounds shift, moveend fires naturally and
    // the marker recompute follows.
    window.addEventListener('pageshow', function(e) {
      if (e.persisted) map.invalidateSize();
    });

    // Live geolocation: click once to fly to current position, click again
    // to stop. The plugin's own top-left button is hidden via CSS in
    // LOCATE_ASSETS; we drive it from the bottom-right locate FAB so the
    // control sits alongside the layer toggles instead of being a stray
    // Leaflet UI in the corner.
    // maximumAge 10min lets the OS hand back a recent cached fix without
    // re-summoning the GPS subsystem — on iOS Safari this is what
    // suppresses the "Allow location" prompt on every reopen.
    var locateCtl = L.control.locate({
      position: 'topleft',
      flyTo: true,
      setView: 'untilPan',
      initialZoomLevel: 16,
      keepCurrentZoomLevel: false,
      cacheLocation: true,
      showCompass: true,
      drawCircle: true,
      drawMarker: true,
      locateOptions: {enableHighAccuracy: true, maximumAge: 600000, watch: false},
      strings: LOCATE_STRINGS
    }).addTo(map);
    // Persist every successful fix so a reopen can paint the last position
    // immediately (before the live fix arrives).
    var LAST_LOC_KEY = 'tabelog.lastLocation';
    map.on('locationfound', function(e) {
      try {
        localStorage.setItem(LAST_LOC_KEY, JSON.stringify({
          lat: e.latlng.lat,
          lon: e.latlng.lng,
          acc: e.accuracy || null,
          ts: Date.now()
        }));
      } catch (_) {}
    });
    var locateFab = document.getElementById('fab-locate');
    if (locateFab) {
      locateFab.addEventListener('click', function() {
        // _active is the plugin's "currently tracking" flag. Toggle so a
        // second tap turns it off, matching Google Maps' behavior.
        if (locateCtl._active) locateCtl.stop(); else locateCtl.start();
      });
      // Paint the FAB blue while the plugin is tracking. The plugin emits
      // these events on the map; locatedeactivate fires on .stop() and on
      // permission denial.
      map.on('locateactivate',   function() { locateFab.classList.add('locating'); });
      map.on('locatedeactivate', function() { locateFab.classList.remove('locating'); });
    }

    // Permissions API: surface a tooltip hint when location is denied so the
    // FAB explains itself instead of silently failing. We deliberately don't
    // auto-start the locate plugin on 'granted' — that would flyTo the user's
    // position and override the restored tabelog.mapView. The user taps the
    // FAB when they want to be located; the maximumAge bump above ensures
    // that tap reuses a cached OS fix without re-prompting.
    (function checkGeoPermission() {
      if (!navigator.permissions || !navigator.permissions.query) return;
      try {
        var p = navigator.permissions.query({name: 'geolocation'});
        if (!p || typeof p.then !== 'function') return;
        p.then(function(status) {
          if (status && status.state === 'denied' && locateFab) {
            locateFab.title = localizeText('定位被浏览器禁用')
                            + l10nComma() + localizeText('请在浏览器设置中开启');  // M-099
          }
        }).catch(function() {});
      } catch (_) {}
    })();

    // ===== FAB layer toggles: transit overlay + attractions =====
    // Vector transit layer rendered from precomputed docs/transit/japan.geojson
    // (extract_japan_transit.py + transit_postprocess.py from a Geofabrik
    // OSM extract). One layer instance, two FABs: 长途 (新干线 + JR 长途)
    // and 市内 (subway + 私铁 + tram + ...) — each toggles a bucket on the
    // same layer via setVisibleBuckets. Loaded lazily on first toggle-on.
    // LOD-aware loading: 'low' (long-haul only, ~1 MB gzipped) for the
    // country-scale view, 'mid' (~2 MB) at regional zoom, 'high' (~4 MB,
    // full detail) once the user is at street-level. Breaks line up with
    // the minZ thresholds in transit-layer.js — subway / tram / monorail
    // only appear at z>=11-12, so the 'mid' file kicks in just before
    // they become visible.
    var transitLayer = (typeof L.transitLayer === 'function')
      ? L.transitLayer({
          lodUrls: {
            // M-009: content-hashed, gzip-encoded R2 objects — 1.06 / 2.55 / 4.2 MB on
            // the wire instead of 18.9 / 36.9 / 42.4 (old plain objects kept in the bucket).
            low:  'https://assets.jpfoodmap.com/japan-low.3df7fc5442.geojson',
            mid:  'https://assets.jpfoodmap.com/japan-mid.b12465fa7b.geojson',
            high: 'https://assets.jpfoodmap.com/japan.0f546984b1.geojson'
          },
          lodBreaks: { mid: 9, high: 14 },
          opacity: 0.4,
          casingOpacity: 0.2
        })
      : null;
    if (!transitLayer) {
      console.warn('[tabelog] L.transitLayer unavailable — transit-layer.js failed to load');
    }
    // The bucket-toggle FABs share the layer's add/remove lifecycle. The
    // layer must be on the map when EITHER bucket is on (so we don't pay
    // re-load cost flipping them); the layer is removed only when both are
    // off. setVisibleBuckets handles intra-layer culling.
    var transitBuckets = { long: false, city: false };
    function applyTransitBucket(btn, key, on) {
      if (!btn) return;
      transitBuckets[key] = on;
      if (btn) {
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
      if (!transitLayer) return;
      var anyOn = transitBuckets.long || transitBuckets.city;
      if (anyOn) {
        transitLayer.setVisibleBuckets({ long: transitBuckets.long, city: transitBuckets.city });
        if (!map.hasLayer(transitLayer)) map.addLayer(transitLayer);
      } else {
        if (map.hasLayer(transitLayer)) map.removeLayer(transitLayer);
      }
    }
    function wireTransitFab(btnId, key, storageKey, defaultOn) {
      var btn = document.getElementById(btnId);
      if (!btn) return;
      var on = defaultOn;
      try {
        var v = localStorage.getItem(storageKey);
        if (v !== null) on = (v === '1');
      } catch (e) {}
      applyTransitBucket(btn, key, on);
      btn.addEventListener('click', function() {
        on = !on;
        applyTransitBucket(btn, key, on);
        try { localStorage.setItem(storageKey, on ? '1' : '0'); } catch (e) {}
      });
    }
    wireTransitFab('fab-transit-long', 'long', 'tabelog.showTransitLong', false);
    wireTransitFab('fab-transit-city', 'city', 'tabelog.showTransitCity', false);

    function applyToggle(btn, layers, on) {
      if (!btn) return;
      var arr = Array.isArray(layers) ? layers : [layers];
      arr.forEach(function(layer) {
        if (!layer) return;
        if (on) {
          if (!map.hasLayer(layer)) map.addLayer(layer);
        } else {
          if (map.hasLayer(layer)) map.removeLayer(layer);
        }
      });
      if (on) {
        btn.classList.add('active');
        btn.setAttribute('aria-pressed', 'true');
      } else {
        btn.classList.remove('active');
        btn.setAttribute('aria-pressed', 'false');
      }
    }

    // `layers` can be a single layer or an array. Historically used to
    // co-toggle a folium-built static attractions FeatureGroup alongside
    // the user-added layer; now there's only one layer per FAB but the
    // array shape stays so re-introducing a second layer later is cheap.
    function wireFab(btnId, layers, storageKey, defaultOn) {
      var btn = document.getElementById(btnId);
      var arr = Array.isArray(layers) ? layers : [layers];
      if (!btn || arr.every(function(l){ return !l; })) return;
      var on = defaultOn;
      try {
        var v = localStorage.getItem(storageKey);
        if (v !== null) on = (v === '1');
      } catch (e) {}
      applyToggle(btn, arr, on);
      btn.addEventListener('click', function() {
        on = !on;
        applyToggle(btn, arr, on);
        try { localStorage.setItem(storageKey, on ? '1' : '0'); } catch (e) {}
      });
    }
    // ===== 我的收藏 + 用户自添景点 (user-pinned places) =====
    // One JSON store (tabelog.bookmarks locally, `bookmarks` field on the
    // server) for both kinds; each entry carries a `category` field — 'bookmark' (under
    // the ⭐收藏 FAB) or 'attraction' (under the 🗾景点 FAB, alongside the
    // curated data/attractions.csv set). The 景点 FAB toggles both layers
    // together; the 收藏 FAB toggles only the bookmarks layer.
    var BM_KEY = 'tabelog.bookmarks';
    // The bookmark modal validates emoji input via isPureEmoji, but the
    // field can still arrive as arbitrary data via direct localStorage
    // edits, a cloud blob authored before that validator existed, or any
    // future shape drift. Normalize at every ingress so the raw emoji
    // string never reaches an HTML-string concat site (openBookmarkPopup
    // / emojiImg's alt attribute / bookmarkIconHtml).
    // M-042: `bm.emoji` used to be tested for truthiness only, so a number
    // or an object went straight into isPureEmoji → s.replace is not a
    // function → the exception escaped the enclosing forEach and took every
    // bookmark AND all 219 built-in landmarks off the map. typeof first.
    function sanitizeBookmarkEmoji(bm) {
      if (bm && bm.emoji != null
          && (typeof bm.emoji !== 'string' || !isPureEmoji(bm.emoji))) {
        bm.emoji = '📍';
      }
      return bm;
    }
    // M-042: one bad entry must cost that entry, never the batch. Objects
    // are repaired in place where the value is recoverable (numeric strings
    // for coords, a numeric id) and only entries that carry nothing at all
    // (null, a bare string/number, a nested array) are dropped — `bookmarks`
    // is also the push source, so dropping a repairable entry here would
    // delete it from every other device on the next sync.
    function sanitizeBookmarkEntry(bm) {
      if (!bm || typeof bm !== 'object' || Array.isArray(bm)) return null;
      try {
        if (typeof bm.id === 'number' && isFinite(bm.id)) bm.id = String(bm.id);
        if (bm.category != null && typeof bm.category !== 'string') delete bm.category;
        ['lat', 'lon'].forEach(function(k) {
          if (typeof bm[k] === 'number') return;
          var n = (typeof bm[k] === 'string' && bm[k].trim() !== '')
                ? Number(bm[k]) : NaN;
          if (isFinite(n)) bm[k] = n; else if (bm[k] != null) delete bm[k];
        });
        return sanitizeBookmarkEmoji(bm);
      } catch (_) { return null; }
    }
    function sanitizeBookmarkArray(arr) {
      if (!Array.isArray(arr)) return [];
      var out = [];
      for (var i = 0; i < arr.length; i++) {
        var e = sanitizeBookmarkEntry(arr[i]);
        if (e) out.push(e);
      }
      if (out.length !== arr.length) {
        console.warn('[tabelog] dropped ' + (arr.length - out.length) +
                     ' unusable bookmark entr(ies)');
      }
      return out;
    }
    var bookmarks = (function() {
      var raw = null;
      try {
        raw = localStorage.getItem(BM_KEY);
        if (raw === null) {
          // First visit on this device — seed from the embedded baseline
          // and persist it so subsequent edits anchor against that copy.
          var seed = sanitizeBookmarkArray(EMBEDDED_BOOKMARKS.slice());
          try { localStorage.setItem(BM_KEY, JSON.stringify(seed)); } catch (_) {}
          return seed;
        }
        var arr = JSON.parse(raw);
        // M-054: a stored blob that isn't an array used to fall through to
        // [] (or the baseline) and get overwritten by the first save — the
        // original is stashed instead so it stays recoverable by hand.
        if (!Array.isArray(arr)) {
          keepCorrupt(BM_KEY, raw);
          return sanitizeBookmarkArray(EMBEDDED_BOOKMARKS.slice());
        }
        return sanitizeBookmarkArray(arr);
      } catch (_) {
        keepCorrupt(BM_KEY, raw);
        return sanitizeBookmarkArray(EMBEDDED_BOOKMARKS.slice());
      }
    })();

    var bookmarksLayer = L.featureGroup();        // category === 'bookmark'
    var userAttractionsLayer = L.featureGroup();  // category === 'attraction'
    // Stores both the marker and its parent layer so removal works without
    // re-checking the entry's category (which the user could have changed
    // by deleting + re-adding, etc.).
    var bmMarkerById = {};
    // Built-in landmarks the user has hidden. Lives as { id, category:
    // "hidden" } entries inside the bookmarks array so it rides the same
    // cloud sync as personal pins — no extra file or storage key. Only
    // builtin IDs (fb-*) ever land in here; personal pins have their own
    // delete flow.
    var hiddenBuiltinIds = new Set();
    function rebuildHiddenIds() {
      hiddenBuiltinIds.clear();
      bookmarks.forEach(function(bm) {
        if (bm && bm.category === 'hidden'
            && typeof bm.id === 'string'
            && bm.id.indexOf('fb-') === 0) {
          hiddenBuiltinIds.add(bm.id);
        }
      });
    }

    // M-003: same three-way discipline as saveCache, keyed by bookmark id,
    // base = the JSON this page last wrote/read (memory only). If the disk
    // moved under us, the merged list is adopted in place and the layers are
    // rebuilt — never a union, so a pin deleted here stays deleted even when
    // another tab still holds it. null base = "don't merge, overwrite"
    // (used once, right after an account switch, so the previous account's
    // pins can't leak into the new one).
    var lastWrittenBookmarks = JSON.stringify(bookmarks);
    function rebuildBookmarkLayers() {
      bookmarksLayer.clearLayers();
      userAttractionsLayer.clearLayers();
      bmMarkerById = {};
      bookmarks.forEach(function(bm) {
        // M-042: one malformed entry must not abort the rest of the render.
        try { renderBookmark(bm); } catch (_) {}
      });
      rebuildHiddenIds();
      renderFavoritesBuiltin();
    }
    // Merges the on-disk bookmarks into memory (no write). Returns true when
    // memory changed. Shared by saveBookmarks and the cross-tab reconcile.
    function mergeDiskBookmarksIntoMemory() {
      var raw = null;
      try { raw = localStorage.getItem(BM_KEY); } catch (_) {}
      if (raw === null || raw === lastWrittenBookmarks || lastWrittenBookmarks === null) return false;
      var disk = null;
      try { disk = JSON.parse(raw); } catch (_) {}
      if (!Array.isArray(disk)) return false;
      var base = [];
      try { base = JSON.parse(lastWrittenBookmarks) || []; } catch (_) {}
      var merged = mergeBookmarks(base, bookmarks.slice(), sanitizeBookmarkArray(disk));
      lastWrittenBookmarks = raw;
      if (JSON.stringify(merged) === JSON.stringify(bookmarks)) return false;
      bookmarks.length = 0;
      merged.forEach(function(b) { bookmarks.push(b); });
      rebuildBookmarkLayers();
      return true;
    }
    function saveBookmarks() {
      mergeDiskBookmarksIntoMemory();
      try {
        var out = JSON.stringify(bookmarks);
        localStorage.setItem(BM_KEY, out);
        lastWrittenBookmarks = out;
      } catch (e) { storageBlocked(e); }   // M-046
    }
    // Pick the right name field for the active UI language.
    //   full schema: { name_src, name_sc, name_tc, name_jp, name_en, ... }
    //   legacy:      { name, ... }                          — pre-i18n entries
    // For zh-TW, an explicit name_tc wins. Without it we fall back through
    // localizeText (the runtime CJK localizer used for static page text),
    // which only covers runs already in TEXT_TRAD_MAP — chars outside the
    // map stay simplified. That's the accepted tradeoff for not shipping a
    // full OpenCC pass to the browser.
    function bmDisplayName(bm) {
      if (!bm) return '';
      if (bm.name && !bm.name_src && !bm.name_sc && !bm.name_tc
                  && !bm.name_en  && !bm.name_jp) {
        return bm.name;  // legacy single-name entry
      }
      var sc  = bm.name_sc  || '';
      var tc  = bm.name_tc  || '';
      var en  = bm.name_en  || '';
      var src = bm.name_src || '';
      var jp  = bm.name_jp  || '';
      if (activeLang === 'zh-CN') return sc  || src || jp  || tc || en;
      if (activeLang === 'zh-TW') return tc  || localizeText(sc || src || jp || en);
      if (activeLang === 'en')    return en  || sc  || src || jp || tc;
      if (activeLang === 'ja')    return jp  || src || sc  || tc || en;
      return src || sc || jp || tc || en;
    }
    function bookmarkIconHtml(emoji, name, emojiSize, labelSize) {
      // 22px / 10px for 收藏, 30px / 11px for user-added 景点 — the latter
      // matches the build-time curated attractions so user additions blend
      // visually with the existing tourist anchors.
      // Emoji goes through emojiImg() so the marker uses an Apple-style PNG;
      // marker divIcons aren't covered by the MutationObserver, so the pre-
      // swap has to happen here at construction (same pattern as makeIcon).
      // The .bm-mk-dot sibling stays display:none by default; CSS flips it
      // in/out vs .bm-mk-full when body.zoom-low toggles (see zoomend
      // listener). Two siblings cohabit the same Leaflet wrapper without
      // affecting each other since both use position:relative + transform.
      var es = emojiSize || 22;
      var ls = labelSize || 10;
      return '<div class="bm-mk-dot">' +
                emojiImg(emoji || '📍', 'width:16px;height:16px;') +
             '</div>' +
             '<div class="bm-mk-full" ' +
                  'style="position:relative;transform:translate(-50%,-100%);' +
                         'text-align:center;width:max-content;">' +
               '<div style="font-size:' + es + 'px;line-height:1;' +
                          'filter:drop-shadow(0 1px 2px rgba(0,0,0,0.45));">' +
                 emojiImg(emoji || '📍', 'vertical-align:top;') +
               '</div>' +
               '<div style="font-size:' + ls + 'px;font-weight:700;color:#1f2937;' +
                          'background:rgba(255,255,255,0.92);' +
                          'padding:1px 5px;border-radius:4px;margin-top:1px;' +
                          'white-space:nowrap;' +
                          'box-shadow:0 1px 2px rgba(0,0,0,0.2);">' +
                 escapeHtml(name || '') +
               '</div>' +
             '</div>';
    }
    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, function(c) {
        return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
      });
    }
    // Render the restaurant card from structured data. Mirrors the old
    // Python fmt_popup() layout. `d` is the restaurants.json entry (name,
    // rating, categories, bookable, detail_url already there); `p` is the
    // popups.json positional array:
    //   [genre, dinner, lunch, seat, station, address, policy, photos]
    function renderPopup(d, p) {
      if (!d || !p) return '';
      var genre   = p[0], dinner  = p[1], lunch   = p[2], seat    = p[3],
          station = p[4], addr    = p[5], policy  = p[6], photos  = p[7] || [];
      // Slot 8 is server-rendered ribbon HTML — safe to inline as-is
      // (only static class names + escaped award labels).
      var ribbons = p[8] || '';
      var name    = escapeHtml(d.name || '');
      var rating  = d.rating == null ? '' : escapeHtml(d.rating);
      var bucket  = (d.categories && d.categories[0]) || '';
      var url     = escapeHtml(d.detail_url || '');
      var dinnerS = (dinner != null) ? '¥' + dinner : 'NA';
      var lunchS  = (lunch  != null) ? '¥' + lunch  : 'NA';
      var photoHtml = '';
      if (photos.length) {
        // 150x150_square (the old thumb) upscaled 2-3x on any hidpi
        // screen — visibly blurry. 320x320_square covers phone-sized boxes
        // through dpr≈2.5; wide hidpi screens use the 640 rect directly —
        // it's the same URL as the full-view link, so the bytes get reused
        // when the user taps through. aspect-ratio + object-fit:cover in
        // the CSS makes square and rect sources crop identically.
        var thumbToken = (window.innerWidth >= 700
                          && (window.devicePixelRatio || 1) > 1.2)
                         ? '640x640_rect_' : '320x320_square_';
        photoHtml = '<div class="rst-photos">' + photos.map(function(big) {
          var thumb = String(big).replace('640x640_rect_', thumbToken);
          return '<a href="' + escapeHtml(big) + '" target="_blank" rel="noopener">'
               + '<img src="' + escapeHtml(thumb) + '" loading="lazy" alt="" '
               + 'onload="this.parentElement.classList.add(\'ld\')" '
               + 'onerror="this.parentElement.style.display=\'none\'"></a>';
        }).join('') + '</div>';
      }
      // Locale used for chip text + per-field translate-button gating below.
      // Declared up front because EN / JA both need a hand-tuned chip phrase
      // — relying on the runtime CJK localizer would split "可Tabelog预约"
      // into independent runs and produce "AvailableTabelogBooking", which
      // is what we're avoiding.
      var _lang = (typeof activeLang === 'undefined') ? 'zh-CN' : activeLang;
      var chipText = d.bookable ? '可Tabelog预约' : '不可Tabelog预约';
      if (_lang === 'en') {
        chipText = d.bookable ? 'Bookable via Tabelog' : 'Not bookable via Tabelog';
      } else if (_lang === 'ja') {
        chipText = d.bookable ? 'Tabelog 予約可' : 'Tabelog 予約不可';
      }
      var chipCls = d.bookable ? 'rst-chip' : 'rst-chip rst-chip-off';
      var chip = '<span class="' + chipCls + '">' + chipText + '</span>';
      // Per-field translate button. Hidden on the Japanese UI (source
      // text is already Japanese). The Chinese UIs (zh-CN / zh-TW) only
      // show the button for `seat` and `genre` — station and address
      // are mostly kanji Chinese readers can read directly, but Tabelog
      // genre strings (そば, 串揚げ, etc.) are kana-heavy and worth
      // translating. English shows all four. The label "翻译" rides the
      // CJK localizer: zh-TW gets 翻譯 via OpenCC, en gets "Translate"
      // via TEXT_EN_MAP.
      function txBtn(field, val) {
        if (!val || val === '—') return '';
        if (_lang === 'ja') return '';
        if ((_lang === 'zh-CN' || _lang === 'zh-TW')
            && field !== 'seat' && field !== 'genre') return '';
        return '<button type="button" class="rst-tx-btn" data-tx="' + field + '">翻译</button>';
      }
      // Quick-jump to Google Maps. Calibrated rows carry the place_id (the one
      // field Google's terms let us store) — append &query_place_id= to land
      // directly on that exact place page, per the documented Maps URLs API.
      // Uncalibrated rows fall back to a "<name> <address>" search, which lands
      // on the results list so the user can pick the right pin if there are
      // dupes. Title stays English (emoji's universal) so the build-time CJK
      // scan doesn't pick up phantom runs from JS string literals.
      var gmapsQ = encodeURIComponent(
        ((d.name || '') + ' ' + (addr || '')).trim()
      );
      var gmapsUrl = 'https://www.google.com/maps/search/?api=1&query=' + gmapsQ
                   + (d.gpid ? '&query_place_id=' + encodeURIComponent(d.gpid) : '');
      var gmapsBtn = '<a class="rst-gmaps" href="' + gmapsUrl
                   + '" target="_blank" rel="noopener" '
                   + 'aria-label="Open in Google Maps" '
                   + 'title="Open in Google Maps">'
                   + '<img src="img/google-maps-v2.png" alt="Google Maps" '
                   + 'width="18" height="18" loading="lazy"></a>';
      // TEMP-ish: "location calibrated by Google" note, shown under the
      // address when this row was Google-calibrated (d.gcal). Hand-tuned per
      // language like chipText, so the runtime CJK localizer doesn't fragment
      // the mixed Latin+CJK string into "already Google map-calibrated".
      var gcalNote = '';
      if (d.gcal) {
        var gcalTxt = '已被 Google 地图校准';
        if (_lang === 'en') gcalTxt = 'Location verified by Google Maps';
        else if (_lang === 'ja') gcalTxt = 'Google マップで位置補正済み';
        else if (_lang === 'zh-TW') gcalTxt = '已被 Google 地圖校準';
        gcalNote = '<div class="rst-gcal" style="font-size:11px;color:#16a34a;'
                 + 'margin:4px 0 0;">🛰️ ' + gcalTxt + '</div>';
      }
      return '<div class="rst-card">'
        + ribbons
        + '<div class="rst-header">'
          + '<div class="rst-title"><span lang="ja">' + name + '</span>'
            + '<span class="rst-rating">★' + rating + '</span></div>'
          + '<div class="rst-actions">'
            + gmapsBtn
            + '<button class="ff-fav-btn rst-btn" data-url="' + url + '">'
              + '<span class="ff-fav-label">☆ 收藏</span></button>'
            + '<button class="ff-black-btn rst-btn" data-url="' + url + '">'
              + '<span class="ff-black-label">🚫 弃用</span></button>'
            + '<button class="rst-close" type="button" aria-label="关闭">×</button>'
          + '</div>'
        + '</div>'
        + photoHtml
        + '<div class="rst-genre"><span class="rst-value"><span lang="ja">' + escapeHtml(genre) + '</span></span>' + txBtn('genre', genre) + ' / ' + escapeHtml(bucket) + '</div>'
        + '<div class="rst-info">'
          + '<div class="rst-info-row"><span class="rst-label">晚</span><span class="rst-value">' + escapeHtml(dinnerS) + '</span></div>'
          + '<div class="rst-info-row"><span class="rst-label">车站</span><span class="rst-value">📍 <span lang="ja">' + escapeHtml(station) + '</span></span>' + txBtn('station', station) + '</div>'
          + '<div class="rst-info-row"><span class="rst-label">午</span><span class="rst-value">' + escapeHtml(lunchS) + '</span></div>'
          + '<div class="rst-info-row"><span class="rst-label">座位</span><span class="rst-value">' + (seat ? escapeHtml(seat) : '—') + '</span>' + txBtn('seat', seat) + '</div>'
          + '<div class="rst-info-row"><span class="rst-label">地址</span><span class="rst-value" lang="ja">' + escapeHtml(addr) + '</span>' + txBtn('addr', addr) + '</div>'
        + '</div>'
        + gcalNote
        + (policy ? '<div class="rst-policy">' + escapeHtml(policy) + '</div>' : '')
        + '<div class="rst-footer">'
          + chip
          + '<a href="' + url + '" target="_blank" rel="noopener">Tabelog 详情 ↗</a>'
        + '</div>'
      + '</div>';
    }
    function renderBookmark(bm) {
      // Metadata-only entries (category: "hidden") carry just an id —
      // they're a flag telling us a builtin should not render, not a pin
      // of their own. Same defensive check for entries missing coords.
      if (!bm || bm.category === 'hidden') return;
      if (typeof bm.lat !== 'number' || typeof bm.lon !== 'number') return;
      var isAttraction = bm.category === 'attraction';
      var targetLayer = isAttraction ? userAttractionsLayer : bookmarksLayer;
      // CSS hooks on the wrapper div Leaflet creates around the divIcon:
      //   bm-mk             — every bookmark/attraction marker
      //   bm-mk-attraction  — category=attraction (built-ins + user景点)
      //   bm-mk-bookmark    — category=bookmark   (personal pins)
      //   bm-mk-builtin     — repo-shipped landmark (read-only source)
      //   bm-mk-hidden      — user hid this builtin (display:none unless
      //                       the body carries .attr-show-all)
      var cls = 'empty bm-mk';
      cls += isAttraction ? ' bm-mk-attraction' : ' bm-mk-bookmark';
      if (bm._builtin) cls += ' bm-mk-builtin';
      if (bm._hidden)  cls += ' bm-mk-hidden';
      var icon = L.divIcon({
        className: cls,
        iconSize: [0, 0],
        iconAnchor: [0, 0],
        html: bookmarkIconHtml(bm.emoji, bmDisplayName(bm),
                               isAttraction ? 30 : 22,
                               isAttraction ? 11 : 10)
      });
      var m = L.marker([bm.lat, bm.lon], {icon: icon});
      // bindTooltip with a string sets the tooltip content via innerHTML
      // (Leaflet 1.9.3 Popup/Tooltip share _updateContent: node.innerHTML
      // = content when typeof string). Escape the name before it lands
      // there — Wikidata labels can carry literal HTML if a vandal edits
      // the label of a popular Q-ID while a user adds it as a bookmark.
      m.bindTooltip(escapeHtml(bmDisplayName(bm)), {sticky: true});
      m.on('click', function() { openBookmarkPopup(bm, m); });
      m.addTo(targetLayer);
      bmMarkerById[bm.id] = {marker: m, layer: targetLayer};
    }
    function removeBookmarkMarker(bm) {
      var entry = bmMarkerById[bm.id];
      if (entry) {
        entry.layer.removeLayer(entry.marker);
        delete bmMarkerById[bm.id];
      }
    }
    rebuildHiddenIds();
    bookmarks.forEach(renderBookmark);
    // Repo-shipped landmarks render alongside the personal layer but live
    // outside the localStorage/cloud sync — so a push doesn't carry them,
    // and a redeploy with an updated favorites_builtin.json shows up
    // immediately on every visitor's next reload. Pulled out as a function
    // because the cloud-pull path wipes both leaflet layers before
    // re-rendering personal bookmarks; that wipe also kills our builtin
    // markers, so we re-run this after each pull. ID collision with a
    // personal pin (unlikely; builtin IDs use the 'fb-' prefix) is resolved
    // by letting the personal entry win.
    function renderFavoritesBuiltin() {
      EMBEDDED_FAVORITES_BUILTIN.forEach(function(bm) {
        if (bmMarkerById[bm.id]) return;
        sanitizeBookmarkEmoji(bm);
        bm._builtin = true;
        bm._hidden  = hiddenBuiltinIds.has(bm.id);
        renderBookmark(bm);
      });
    }
    renderFavoritesBuiltin();

    function openBookmarkPopup(bm, marker) {
      var coord = bm.lat.toFixed(6) + ', ' + bm.lon.toFixed(6);
      // Popup offset depends on what's actually showing:
      //   dot mode (low zoom) → small offset to clear the 16px mini emoji
      //                         (centred on the anchor, so top is 8px up)
      //   full marker         → bigger so the speech bubble tip sits above
      //                          the emoji (30px for 景点, 22px for 收藏)
      var inDotMode = (typeof map !== 'undefined') && map.getZoom() < 11;
      var offY = inDotMode ? -10
                : (bm.category === 'attraction') ? -30 : -22;
      // Action button varies by entry type:
      //   personal pin       → 删除  (one-shot, removes from bookmarks)
      //   built-in (visible) → 隐藏  (adds metadata entry to bookmarks)
      //   built-in (hidden)  → 恢复显示 (removes metadata entry)
      // Built-ins themselves are never edited — the metadata flag in
      // bookmarks is the only thing that changes, so a redeploy of
      // favorites_builtin.json can still update names/coords and the
      // user's hide list survives.
      var actionBtn = '';
      if (bm._builtin) {
        if (bm._hidden) {
          actionBtn = '<button id="bm-unhide" ' +
              'style="padding:4px 12px;font-size:12px;cursor:pointer;' +
                     'border:1px solid #bfdbfe;border-radius:4px;' +
                     'background:#eff6ff;color:#1d4ed8;font-weight:600;">' +
              '恢复显示</button>';
        } else {
          actionBtn = '<button id="bm-hide" ' +
              'style="padding:4px 12px;font-size:12px;cursor:pointer;' +
                     'border:1px solid #e5e7eb;border-radius:4px;' +
                     'background:#f9fafb;color:#374151;font-weight:600;">' +
              '隐藏</button>';
        }
      } else {
        var delLabel = (bm.category === 'attraction') ? '删除景点' : '删除收藏';
        actionBtn = '<button id="bm-del" ' +
            'style="padding:4px 12px;font-size:12px;cursor:pointer;' +
                   'border:1px solid #fecaca;border-radius:4px;' +
                   'background:#fef2f2;color:#b91c1c;font-weight:600;">' +
            delLabel + '</button>';
      }
      var html =
        '<div style="font:13px sans-serif;text-align:center;min-width:160px;">' +
          '<div style="font-weight:700;margin-bottom:4px;">' +
            (bm.emoji || '📍') + ' ' + escapeHtml(bmDisplayName(bm)) +
          '</div>' +
          '<div lang="en" style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:#6b7280;' +
                      'margin-bottom:8px;">' + coord + '</div>' +
          actionBtn +
        '</div>';
      // Run the popup HTML through the CJK localizer up front instead of
      // relying on the MutationObserver hooked to .leaflet-popup-pane —
      // the observer has been flaky on Leaflet's popup insertion path, and
      // localizeText is a no-op in zh-CN mode so there's no downside.
      L.popup({offset: [0, offY]})
        .setLatLng([bm.lat, bm.lon])
        .setContent(localizeText(html))
        .openOn(map);
      // Wire whichever button ended up in the popup. setTimeout(0) gives
      // Leaflet a frame to actually insert the popup HTML into the DOM.
      setTimeout(function() {
        if (bm._builtin) {
          var hideBtn = document.getElementById('bm-hide');
          if (hideBtn) hideBtn.addEventListener('click', function() {
            bookmarks.push({id: bm.id, category: 'hidden'});
            hiddenBuiltinIds.add(bm.id);
            bm._hidden = true;
            // Re-render so the new bm-mk-hidden class lands on the
            // wrapper; the user sees the marker either vanish (state '1')
            // or ghost-out (state '2') without a page reload.
            removeBookmarkMarker(bm);
            renderBookmark(bm);
            saveBookmarks();
            schedulePush();
            map.closePopup();
          });
          var unhideBtn = document.getElementById('bm-unhide');
          if (unhideBtn) unhideBtn.addEventListener('click', function() {
            var i = bookmarks.findIndex(function(x) {
              return x && x.id === bm.id && x.category === 'hidden';
            });
            if (i >= 0) bookmarks.splice(i, 1);
            hiddenBuiltinIds.delete(bm.id);
            bm._hidden = false;
            removeBookmarkMarker(bm);
            renderBookmark(bm);
            saveBookmarks();
            schedulePush();
            map.closePopup();
          });
          return;
        }
        var del = document.getElementById('bm-del');
        if (!del) return;
        del.addEventListener('click', function() {
          var i = bookmarks.findIndex(function(x){ return x.id === bm.id; });
          if (i < 0) { map.closePopup(); return; }
          // M-035: this used to be one irreversible tap on a red button
          // that also pushed the deletion to every other device. The row is
          // still removed immediately (no modal in the way), but the exact
          // object and its position are held for the length of the snackbar
          // so 撤销 puts it back byte-for-byte — same id, so the other
          // devices' merge never sees it leave.
          var removed = bookmarks[i], at = i;
          bookmarks.splice(i, 1);
          removeBookmarkMarker(bm);
          saveBookmarks();
          schedulePush();
          map.closePopup();
          showToast(localizeText('已删除') + ' ' + bmDisplayName(bm), {
            actionLabel: localizeText('撤销'),
            ms: 9000,
            onAction: function() {
              var back = bookmarks.some(function(x) {
                return x && x.id === removed.id;
              });
              if (back) return;   // re-added by a sync while the toast was up
              bookmarks.splice(Math.min(at, bookmarks.length), 0, removed);
              renderBookmark(removed);
              saveBookmarks();
              schedulePush();
              announce(localizeText('已恢复') + ' ' + bmDisplayName(removed));
            }
          });
        });
      }, 0);
    }

    // Low-zoom dot collapse for bookmarks + attractions. At zoom < 11 the
    // full emoji+label hides and a coloured dot stands in (CSS in the
    // page <style>). Threshold mirrors the JS check in openBookmarkPopup
    // so popup offset and visible marker stay in sync.
    var ZOOM_LOW_THRESHOLD = 11;
    function syncZoomBucket() {
      var low = map.getZoom() < ZOOM_LOW_THRESHOLD;
      document.body.classList.toggle('zoom-low', low);
    }
    map.on('zoomend', syncZoomBucket);
    syncZoomBucket();

    // fab-attractions is a tri-state: off / on (blue) / show-all (orange).
    // The third state reveals built-ins the user has hidden, ghost-styled,
    // so they can be un-hidden via the popup. CSS does the heavy lifting
    // — we just toggle a body class for show-all and an .active vs
    // .show-all class on the FAB itself.
    (function wireAttractionsFab() {
      var btn = document.getElementById('fab-attractions');
      if (!btn) return;
      var KEY = 'tabelog.showAttractions';
      var state = '1';   // off / on / show-all
      try {
        var v = localStorage.getItem(KEY);
        if (v === '0' || v === '1' || v === '2') state = v;
      } catch (_) {}
      function apply(s) {
        if (s === '0') {
          if (map.hasLayer(userAttractionsLayer)) {
            map.removeLayer(userAttractionsLayer);
          }
          btn.classList.remove('active', 'show-all');
          btn.setAttribute('aria-pressed', 'false');
        } else {
          if (!map.hasLayer(userAttractionsLayer)) {
            map.addLayer(userAttractionsLayer);
          }
          btn.classList.toggle('active',   s === '1');
          btn.classList.toggle('show-all', s === '2');
          btn.setAttribute('aria-pressed', 'true');
        }
        document.body.classList.toggle('attr-show-all', s === '2');
      }
      apply(state);
      btn.addEventListener('click', function() {
        state = (state === '0') ? '1' : (state === '1') ? '2' : '0';
        apply(state);
        try { localStorage.setItem(KEY, state); } catch (_) {}
      });
    })();
    wireFab('fab-bookmarks',   bookmarksLayer,
            'tabelog.showBookmarks',   true);

    // ----- 加入收藏 modal -----
    var bmModal      = document.getElementById('bm-modal');
    var bmBackdrop   = document.getElementById('bm-backdrop');
    var bmCoordEl    = document.getElementById('bm-coord');
    var bmTitleEl    = document.getElementById('bm-modal-title');
    var bmNameInput  = document.getElementById('bm-name');
    var bmEmojiInput = document.getElementById('bm-emoji');
    var bmEmojiMore  = document.getElementById('bm-emoji-more');
    var bmPicker     = document.getElementById('bm-emoji-picker');
    var bmError      = document.getElementById('bm-error');
    var bmKindBtns   = bmModal.querySelectorAll('.bm-kind-seg button');
    var bmPending    = null;          // {lat, lng}
    var bmKind       = 'bookmark';    // 'bookmark' | 'attraction'

    function bmSetKind(kind) {
      bmKind = (kind === 'attraction') ? 'attraction' : 'bookmark';
      bmKindBtns.forEach(function(b) {
        var on = b.getAttribute('data-kind') === bmKind;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
      });
      bmTitleEl.textContent = (bmKind === 'attraction') ? '加入景点' : '加入收藏';
    }
    bmKindBtns.forEach(function(btn) {
      btn.addEventListener('click', function() {
        bmSetKind(btn.getAttribute('data-kind'));
      });
    });

    // EMOJI_RE matches single emoji codepoints; ZWJ / VS / skin-tone
    // modifiers stitch sequences together (family, profession, tone). To
    // judge "is the whole string emoji-only", strip both and check nothing
    // remains. Pure-ASCII / Han / random punctuation will leave residue.
    function isPureEmoji(s) {
      if (!s) return false;
      // ZWJ U+200D, variation selectors U+FE0E/FE0F, Fitzpatrick skin tones
      // U+1F3FB–U+1F3FF. EMOJI_RE catches the base glyphs; this regex
      // catches the glue and modifiers that sit between/after them.
      var stripped = s.replace(EMOJI_RE, '')
                      .replace(/[‍︎️\u{1F3FB}-\u{1F3FF}]/gu, '');
      return stripped.trim() === '';
    }
    function bmShowError(msg) { bmError.textContent = msg || ''; }
    function bmFlagInput(input) {
      input.focus();
      input.style.borderColor = '#dc2626';
      setTimeout(function(){ input.style.borderColor = ''; }, 1200);
    }

    function bmCollapsePicker() {
      bmPicker.classList.remove('bm-show');
      bmEmojiMore.setAttribute('aria-expanded', 'false');
      bmEmojiMore.textContent = '🔽';
    }

    var bmInitialName = '';
    function openBookmarkModal(latlng, prefillName) {
      bmPending = {lat: latlng.lat, lng: latlng.lng};
      bmCoordEl.textContent = latlng.lat.toFixed(6) + ', ' + latlng.lng.toFixed(6);
      bmNameInput.value = prefillName || '';
      bmInitialName = bmNameInput.value;
      bmEmojiInput.value = '📍';
      bmSetKind('bookmark');           // reset default each open
      bmShowError('');
      bmCollapsePicker();
      bmBackdrop.classList.add('bm-open');
      bmModal.classList.add('bm-open');
      bmModal.setAttribute('aria-hidden', 'false');
      // Pull focus into the name field after the open transition starts so
      // mobile keyboards pop up immediately.
      setTimeout(function(){ bmNameInput.focus(); }, 50);
    }
    function closeBookmarkModal() {
      bmBackdrop.classList.remove('bm-open');
      bmModal.classList.remove('bm-open');
      bmModal.setAttribute('aria-hidden', 'true');
      bmCollapsePicker();
      bmShowError('');
      bmPending = null;
    }
    function commitBookmark() {
      if (!bmPending) return;
      var name = (bmNameInput.value || '').trim();
      var emojiRaw = (bmEmojiInput.value || '').trim();
      if (!name) {
        bmShowError('名称不能为空');
        bmFlagInput(bmNameInput);
        return;
      }
      if (emojiRaw && !isPureEmoji(emojiRaw)) {
        bmShowError('图标必须是 emoji（试试 😀 按钮里的选择器）');
        bmFlagInput(bmEmojiInput);
        return;
      }
      var emoji = emojiRaw || '📍';
      bmShowError('');
      var pendingLat = bmPending.lat;
      var pendingLng = bmPending.lng;
      var pendingCat = bmKind;
      // Save the pin immediately. The schema tolerates empty translated
      // names (bmDisplayName falls back to name_src), so there's no
      // reason to hold the modal hostage to up to four sequential
      // cross-origin Wikipedia/Wikidata round trips on a slow network —
      // that used to lock 保存/取消 behind a "…" spinner with no timeout.
      var bm = {
        id: 'bm-' + Date.now().toString(36) + '-' +
            Math.random().toString(36).slice(2, 7),
        name_src: name,
        name_sc: '', name_tc: '', name_jp: '', name_en: '',
        emoji: emoji,
        lat: pendingLat,
        lon: pendingLng,
        category: pendingCat
      };
      bookmarks.push(bm);
      renderBookmark(bm);
      saveBookmarks();
      schedulePush();
      // If a search-temp 📍 sits at this exact spot it's the one being
      // bookmarked — drop it so the bookmark emoji doesn't stack on top.
      // Coord match instead of a "source" flag keeps the right-click path
      // from accidentally clearing an unrelated search pin elsewhere.
      if (ssTempMarker) {
        var t = ssTempMarker.getLatLng();
        if (Math.abs(t.lat - bm.lat) < 1e-7 && Math.abs(t.lng - bm.lon) < 1e-7) {
          ssRemoveTempMarker();
        }
      }
      // Make sure the right layer is visible after adding — if the user
      // has the corresponding FAB toggled off, surface the pin by
      // re-enabling it.
      var fabId = (bm.category === 'attraction') ? 'fab-attractions' : 'fab-bookmarks';
      var fab = document.getElementById(fabId);
      if (fab && fab.getAttribute('aria-pressed') !== 'true') fab.click();
      closeBookmarkModal();
      // Wikidata backfill, fully async: hit fills sc/tc/jp/en with
      // community-curated labels, miss leaves them empty. We deliberately
      // don't fall back to MT; honest empty fields beat literal
      // translations of proper nouns ("新世界" → "new world" was the
      // cautionary example). Refetch the entry by id before mutating —
      // a pull/merge may have replaced the object, or the user may have
      // deleted the pin while the lookup was in flight.
      wikidataLookup(name, pendingLat, pendingLng).then(function(wd) {
        if (!wd || !(wd.sc || wd.tc || wd.jp || wd.en)) return;
        var cur = null;
        for (var i = 0; i < bookmarks.length; i++) {
          if (bookmarks[i] && bookmarks[i].id === bm.id) { cur = bookmarks[i]; break; }
        }
        if (!cur) return;   // deleted meanwhile — don't resurrect
        cur.name_sc = wd.sc || '';
        cur.name_tc = wd.tc || '';
        cur.name_jp = wd.jp || '';
        cur.name_en = wd.en || '';
        removeBookmarkMarker(cur);
        renderBookmark(cur);
        saveBookmarks();
        schedulePush();
      });
    }
    // A stray tap on the dimmed backdrop is easy on phones (the keyboard
    // shoves the modal around) — don't let it eat a typed name silently.
    // Explicit 取消 / × still close without asking.
    bmBackdrop.addEventListener('click', function() {
      var typed = (bmNameInput.value || '').trim();
      if (typed && typed !== bmInitialName
          && !confirm(localizeText('放弃当前输入？'))) return;
      closeBookmarkModal();
    });
    bmModal.querySelector('.bm-close').addEventListener('click', closeBookmarkModal);
    bmModal.querySelector('.bm-cancel').addEventListener('click', closeBookmarkModal);
    bmModal.querySelector('.bm-save').addEventListener('click', commitBookmark);
    // Quick-pick chips live in their own block; the 😀 picker button is
    // a sibling of that block and is wired separately to toggle bmPicker.
    bmModal.querySelectorAll('.bm-quick-list button[data-emoji]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        bmEmojiInput.value = btn.getAttribute('data-emoji') || '📍';
        bmShowError('');
      });
    });
    // The full picker module loads on first expand only. Pinned to the
    // exact version the old `@^1` range URL was serving (immutable CDN
    // path, no redirect); the <emoji-picker> tag upgrades in place once
    // the module registers the custom element. Failure allows retry on
    // the next click — the six quick chips keep working regardless.
    var bmPickerLoaded = null;
    function ensurePickerModule() {
      if (!bmPickerLoaded) {
        bmPickerLoaded =
          import('https://cdn.jsdelivr.net/npm/emoji-picker-element@1.27.0/index.js')
            .catch(function(e) {
              bmPickerLoaded = null;
              console.warn('[tabelog] emoji picker load failed:', e);
            });
      }
      return bmPickerLoaded;
    }
    bmEmojiMore.addEventListener('click', function() {
      var nowOpen = !bmPicker.classList.contains('bm-show');
      if (nowOpen) ensurePickerModule();
      bmPicker.classList.toggle('bm-show', nowOpen);
      bmEmojiMore.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
      bmEmojiMore.textContent = nowOpen ? '🔼' : '🔽';
    });
    // emoji-picker-element fires 'emoji-click' with detail.unicode as the
    // rendered glyph. Setting the input + closing the picker mirrors what
    // most users expect after a pick.
    bmPicker.addEventListener('emoji-click', function(ev) {
      var u = ev && ev.detail && ev.detail.unicode;
      if (!u) return;
      bmEmojiInput.value = u;
      bmShowError('');
      bmEmojiInput.style.borderColor = '';
      bmCollapsePicker();
    });
    bmNameInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); commitBookmark(); }
      else if (e.key === 'Escape') { e.preventDefault(); closeBookmarkModal(); }
    });
    bmEmojiInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); commitBookmark(); }
      else if (e.key === 'Escape') { e.preventDefault(); closeBookmarkModal(); }
    });

    // Right-click (desktop) / long-press (mobile) on any blank spot of the
    // map -> popup with the coords + a copy button + a 加入收藏 button.
    // Leaflet fires 'contextmenu' for both gestures so we only need one handler.
    map.on('contextmenu', function(e) {
      var s = e.latlng.lat.toFixed(6) + ', ' + e.latlng.lng.toFixed(6);
      var html =
        '<div style="font:13px sans-serif;text-align:center;min-width:160px;">' +
          '<div lang="en" style="font-family:ui-monospace,Menlo,Consolas,monospace;margin-bottom:8px;">' + s + '</div>' +
          '<div style="display:flex;gap:6px;justify-content:center;">' +
            '<button id="ff-copy-coord" ' +
                    'style="padding:4px 10px;font-size:12px;cursor:pointer;' +
                    'border:1px solid #d1d5db;border-radius:4px;background:#f9fafb;">' +
                    '复制</button>' +
            '<button id="ff-add-bm" ' +
                    'style="padding:4px 10px;font-size:12px;cursor:pointer;' +
                    'border:1px solid #2563eb;border-radius:4px;' +
                    'background:#2563eb;color:#fff;font-weight:600;">' +
                    '⭐ 加入收藏</button>' +
          '</div>' +
        '</div>';
      L.popup().setLatLng(e.latlng).setContent(localizeText(html)).openOn(map);
      setTimeout(function() {
        var btn = document.getElementById('ff-copy-coord');
        if (btn) {
          btn.addEventListener('click', function() {
            var done = function() { btn.textContent = '已复制 ✓'; };
            var fallback = function() {
              var t = document.createElement('textarea');
              t.value = s; t.style.position = 'fixed'; t.style.opacity = '0';
              document.body.appendChild(t); t.select();
              try { document.execCommand('copy'); } catch (_) {}
              document.body.removeChild(t); done();
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(s).then(done).catch(fallback);
            } else {
              fallback();
            }
          });
        }
        var add = document.getElementById('ff-add-bm');
        if (add) {
          add.addEventListener('click', function() {
            map.closePopup();
            openBookmarkModal(e.latlng);
          });
        }
      }, 0);
    });

    // ===== Nominatim search =====
    // Browser → https://nominatim.openstreetmap.org/search?...&format=jsonv2
    // CORS-open. Policy is ≤1 req/s — JS-side 300ms debounce keeps us well
    // under that for normal typing. viewbox bounds Japan so "Tokyo" doesn't
    // match the US street. zh/ja in accept-language for nicer labels.
    var ssBox     = document.getElementById('ss-box');
    var ssWrap    = document.getElementById('ss-input-wrap');
    var ssInput   = document.getElementById('ss-input');
    var ssClear   = document.getElementById('ss-clear');
    var ssList    = document.getElementById('ss-list');
    var ssLocal   = document.getElementById('ss-local');
    var ssApi     = document.getElementById('ss-api');
    // While a render helper is filling a DocumentFragment, the ssAppend*
    // builders write here instead of straight into the live list — one
    // batched insertion per section instead of a reflow-observable append
    // per row.
    var ssTarget  = null;

    // Placeholders are attributes, not text nodes, so localizeTree never
    // touches them — dispatch each one here on activeLang. zh-CN is the
    // canonical fallback for any language we haven't translated yet.
    // Centralized so adding a new translated input is one entry, not a
    // sprinkle of dispatch code near each input declaration.
    var PLACEHOLDER_L10N = {
      'ss-input': {
        'zh-CN': '搜索餐厅 / 景点 / 地址 ...',
        'zh-TW': '搜尋餐廳 / 景點 / 地址 ...',
        'en':    'Search restaurants / sights / address ...',
        'ja':    'レストラン / スポット / 住所を検索 ...'
      },
      'bm-name': {
        'zh-CN': '例如：东京塔',
        'zh-TW': '例如：東京塔',
        'en':    'e.g. Tokyo Tower',
        'ja':    '例: 東京タワー'
      },
      'bm-emoji': {
        'zh-CN': '可粘贴任意 emoji',
        'zh-TW': '可貼上任意 emoji',
        'en':    'Paste any emoji',
        'ja':    '任意の絵文字を貼り付け可'
      }
    };
    Object.keys(PLACEHOLDER_L10N).forEach(function(id) {
      var el = document.getElementById(id);
      if (!el) return;
      var by_lang = PLACEHOLDER_L10N[id];
      el.placeholder = by_lang[activeLang] || by_lang['zh-CN'];
    });
    var ssDebounce = null;
    var ssReqSeq  = 0;
    var ssTempMarker = null;

    function ssRemoveTempMarker() {
      if (ssTempMarker) { map.removeLayer(ssTempMarker); ssTempMarker = null; }
    }

    // Restaurant-library matching. Scans the in-memory `data` array
    // (closure over initMap), canonicalizes the query once, and ranks by
    // relevance: earlier match position first, then shorter name (less
    // noise around the match), then rating as final tiebreaker.
    //
    // Supports the "name location" pattern: if the query has whitespace,
    // the trailing token is treated as a location candidate (e.g.
    // "眺游楼 横浜"). Restaurants whose loc_norm contains that token are
    // boosted above non-location-matched ones. Location miss = silent
    // fallback to name-only matching (no error, no banner).
    //
    // The dropdown scrolls internally; SS_RESTAURANT_LIMIT only kicks in
    // for pathologically broad queries ("の" etc.).
    var SS_RESTAURANT_LIMIT = 50;
    // Above this zoom level the search results split into "屏幕内" and
    // "其他区域" sub-sections so the user sees nearby matches first. Below
    // it (regional / country-wide view) the bias is moot and we render a
    // single flat list. 10 ≈ city-sized viewport.
    var VIEWPORT_BIAS_ZOOM = 10;
    function relevanceSort(a, b) {
      if (a.idx !== b.idx) return a.idx - b.idx;
      if (a.len !== b.len) return a.len - b.len;
      return (b.d.rating || 0) - (a.d.rating || 0);
    }
    function ssMatchLocal(q) {
      // Tokenize on raw whitespace BEFORE canonicalization (canon strips
      // whitespace, so we'd lose the split point otherwise).
      var tokens = q.split(/\s+/).filter(function(t) { return t.length > 0; });
      if (tokens.length === 0) return {items: [], total: 0};
      var nameQN = '', locQN = '';
      if (tokens.length >= 2) {
        // Location mode is opt-in: only entered when the trailing token is
        // a known city/ward/prefecture stem. Otherwise the spaces are noise
        // and we treat the whole input as one restaurant-name query — that
        // way "炭火烧鸟 正" looks for the literal "炭火烧鸟正" in names and
        // 炭火焼鳥正ざわ stays #1.
        var lastCanon = normalizeForSearch(tokens[tokens.length - 1]);
        if (lastCanon && KNOWN_LOCS.has(lastCanon)) {
          locQN  = lastCanon;
          nameQN = normalizeForSearch(tokens.slice(0, -1).join(''));
        } else {
          nameQN = normalizeForSearch(tokens.join(''));
        }
      } else {
        nameQN = normalizeForSearch(tokens[0]);
      }
      if (!nameQN) return {items: [], total: 0};

      // Pass 1: name-match candidates.
      var candidates = [];
      for (var i = 0; i < data.length; i++) {
        var d = data[i];
        var nm = rowNameNorm(d);
        var idx = nm.indexOf(nameQN);
        if (idx >= 0) candidates.push({d: d, idx: idx, len: nm.length});
      }

      if (locQN) {
        // Partition by location match; boost matched to top.
        var hit = [], miss = [];
        for (var j = 0; j < candidates.length; j++) {
          var lv = candidates[j].d.loc_norm || '';
          (lv.indexOf(locQN) >= 0 ? hit : miss).push(candidates[j]);
        }
        hit.sort(relevanceSort);
        miss.sort(relevanceSort);
        candidates = hit.length > 0 ? hit.concat(miss) : miss;
      } else {
        candidates.sort(relevanceSort);
      }

      // Viewport bias: at city-or-tighter zoom, partition the ordered list
      // into rows currently inside the visible map bounds vs everywhere
      // else. The split is stable, so each bucket keeps the relevance order
      // from the tier sort above. Below the zoom threshold this is a no-op
      // and inViewportCount stays null — the renderer falls back to one
      // flat 餐厅库 list.
      var inViewportCount = null;
      if (map.getZoom() >= VIEWPORT_BIAS_ZOOM && candidates.length > 0) {
        var b = map.getBounds().pad(0.1);
        var W = b.getWest(), E = b.getEast(),
            S = b.getSouth(), N = b.getNorth();
        var inRows = [], outRows = [];
        for (var p = 0; p < candidates.length; p++) {
          var rd = candidates[p].d;
          if (rd.lon >= W && rd.lon <= E && rd.lat >= S && rd.lat <= N) {
            inRows.push(candidates[p]);
          } else {
            outRows.push(candidates[p]);
          }
        }
        candidates = inRows.concat(outRows);
        inViewportCount = inRows.length;
      }

      var items = [];
      for (var k = 0; k < Math.min(candidates.length, SS_RESTAURANT_LIMIT); k++) {
        items.push(candidates[k].d);
      }
      // Clip the in-viewport count to the items we actually emit so the
      // renderer can use it as a contiguous-prefix length without
      // overshooting when SS_RESTAURANT_LIMIT cuts in.
      if (inViewportCount != null) {
        inViewportCount = Math.min(inViewportCount, items.length);
      }
      return {items: items, total: candidates.length,
              inViewportCount: inViewportCount};
    }

    // Two-section render. `localItems` are restaurant-library hits (row
    // objects from `data`), `apiItems` are Nominatim places. Either can be
    // null (= section hidden); apiPending=true draws a "搜索中…" placeholder
    // under the API header while the fetch is in flight.
    function ssAppendSectionHead(label) {
      var h = document.createElement('div');
      h.className = 'ss-section-head';
      h.textContent = label;
      (ssTarget || ssList).appendChild(h);
    }
    function ssAppendSubSectionHead(label) {
      var h = document.createElement('div');
      h.className = 'ss-subsection-head';
      h.textContent = label;
      (ssTarget || ssList).appendChild(h);
    }
    // Render the restaurant section, splitting into 屏幕内 / 其他区域 when
    // ssMatchLocal flagged a viewport bias. Used by both ssRender (full
    // dropdown) and ssShowError (Nominatim-unreachable variant).
    function ssAppendRestaurantSection(items, ivc) {
      if (ivc == null) {
        items.forEach(ssAppendRestaurantRow);
        return;
      }
      if (ivc > 0) {
        ssAppendSubSectionHead('屏幕内 (' + ivc + ')');
        for (var i = 0; i < ivc; i++) ssAppendRestaurantRow(items[i]);
      }
      if (ivc < items.length) {
        ssAppendSubSectionHead('其他区域 (' + (items.length - ivc) + ')');
        for (var j = ivc; j < items.length; j++) ssAppendRestaurantRow(items[j]);
      }
    }
    function ssAppendRestaurantRow(d) {
      var row = document.createElement('div');
      row.className = 'ss-row';
      row.setAttribute('role', 'option');
      var cat = d.categories && d.categories[0];
      var emojiChar = (cat && GENRE_EMOJI[cat]) || '🍽️';
      var icon = document.createElement('span');
      icon.className = 'ss-icon';
      icon.innerHTML = emojiImg(emojiChar);
      var text = document.createElement('div');
      text.className = 'ss-text';
      var n = document.createElement('div');
      n.className = 'ss-name';
      n.setAttribute('lang', 'ja');
      n.textContent = d.name || '';
      var a = document.createElement('div');
      a.className = 'ss-addr';
      // d.city is parsed from a Japanese address — keep it in lang="ja"
      // so the trad converter leaves it alone; cat is the Chinese bucket
      // label and *does* want conversion, so it sits in a bare text node.
      if (d.city) {
        var citySpan = document.createElement('span');
        citySpan.setAttribute('lang', 'ja');
        citySpan.textContent = d.city;
        a.appendChild(citySpan);
      }
      if (cat) {
        if (a.childNodes.length) a.appendChild(document.createTextNode(' | '));
        a.appendChild(document.createTextNode(cat));
      }
      text.appendChild(n); text.appendChild(a);
      var rating = document.createElement('span');
      rating.className = 'ss-rating';
      rating.textContent = (d.rating == null) ? '★ –' : ('★ ' + d.rating);
      row.appendChild(icon);
      row.appendChild(text);
      row.appendChild(rating);
      row.addEventListener('click', function() { ssGotoRestaurant(d); });
      (ssTarget || ssList).appendChild(row);
    }
    function ssAppendApiRow(it) {
      var row = document.createElement('div');
      row.className = 'ss-row';
      row.setAttribute('role', 'option');
      var icon = document.createElement('span');
      icon.className = 'ss-icon';
      icon.textContent = '📍';
      var text = document.createElement('div');
      text.className = 'ss-text';
      var n = document.createElement('div');
      n.className = 'ss-name';
      // Nominatim returns the name in zh/ja preference order — could be
      // either form. Mark it lang="ja" so trad mode doesn't try to
      // s2t-convert what might already be Japanese.
      n.setAttribute('lang', 'ja');
      n.textContent = it.name;
      var a = document.createElement('div');
      a.className = 'ss-addr';
      a.setAttribute('lang', 'ja');
      a.textContent = it.address;
      text.appendChild(n); text.appendChild(a);
      var favBtn = document.createElement('button');
      favBtn.className = 'ss-fav';
      favBtn.type = 'button';
      favBtn.title = '加入收藏';
      favBtn.textContent = '⭐';
      row.appendChild(icon);
      row.appendChild(text);
      row.appendChild(favBtn);
      row.addEventListener('click', function(ev) {
        if (ev.target === favBtn) return;
        ssGoto(it);
      });
      favBtn.addEventListener('click', function(ev) {
        ev.stopPropagation();
        ssCloseDropdown();
        openBookmarkModal({lat: it.lat, lng: it.lon}, it.name);
      });
      (ssTarget || ssList).appendChild(row);
    }
    // Keyboard highlight over the dropdown's actionable rows. Reset on
    // every re-render (the rows are new nodes); Enter falls back to the
    // first row when nothing is highlighted, matching the old behavior.
    var ssActiveIdx = -1;
    function ssNavRows() {
      return ssList.querySelectorAll('.ss-row:not(.ss-empty)');
    }
    function ssResetActive() {
      ssActiveIdx = -1;
      ssInput.removeAttribute('aria-activedescendant');
    }
    function ssSetActive(idx) {
      var rows = ssNavRows();
      if (!rows.length) { ssResetActive(); return; }
      if (idx < 0) idx = rows.length - 1;
      if (idx >= rows.length) idx = 0;
      for (var i = 0; i < rows.length; i++) {
        rows[i].classList.toggle('ss-active', i === idx);
      }
      ssActiveIdx = idx;
      if (!rows[idx].id) rows[idx].id = 'ss-opt-' + idx;
      ssInput.setAttribute('aria-activedescendant', rows[idx].id);
      rows[idx].scrollIntoView({block: 'nearest'});
    }
    // Section renderers. Each builds its rows into a DocumentFragment and
    // swaps its own sub-container in one insertion — and, crucially, the
    // Nominatim response only ever calls ssRenderApi, so the local
    // restaurant rows (and the list's scroll position) are no longer
    // rebuilt a second time when the network answer lands.
    function ssRenderLocal(localMatch) {
      ssResetActive();
      var frag = document.createDocumentFragment();
      ssTarget = frag;
      var items = (localMatch && localMatch.items) || [];
      var total = (localMatch && localMatch.total) || 0;
      if (items.length > 0) {
        ssAppendSectionHead('餐厅库');
        ssAppendRestaurantSection(items, localMatch.inViewportCount);
        if (total > items.length) {
          var more = document.createElement('div');
          more.className = 'ss-row ss-empty';
          more.textContent = '+' + (total - items.length) +
                             ' 个其他匹配 · 输入更多字以缩小范围';
          frag.appendChild(more);
        }
      }
      ssTarget = null;
      ssLocal.innerHTML = '';
      ssLocal.appendChild(frag);
    }
    function ssRenderApi(apiItems, apiPending, errMsg) {
      ssResetActive();
      var frag = document.createDocumentFragment();
      ssTarget = frag;
      var hasApi = apiItems && apiItems.length > 0;
      if (hasApi || apiPending || errMsg) {
        ssAppendSectionHead('地图搜索');
        if (hasApi) {
          apiItems.forEach(ssAppendApiRow);
        } else {
          var r = document.createElement('div');
          r.className = 'ss-row ss-empty' + (errMsg ? ' ss-error' : '');
          r.textContent = errMsg || '搜索中…';
          frag.appendChild(r);
        }
      }
      ssTarget = null;
      ssApi.innerHTML = '';
      ssApi.appendChild(frag);
    }
    // Opens the dropdown and, when both sections came up genuinely empty,
    // shows the no-results placeholder. Every render path ends here.
    function ssFinalize() {
      if (!ssList.querySelector('.ss-row')) {
        var empty = document.createElement('div');
        empty.className = 'ss-row ss-empty';
        empty.textContent = '没有匹配的结果';
        ssApi.appendChild(empty);
      }
      ssList.classList.add('open');
      // M-073: lift the whole search box above the bottom sheets while the
      // dropdown is out (see #ss-box.ss-open).
      if (ssBox) ssBox.classList.add('ss-open');
      ssInput.setAttribute('aria-expanded', 'true');
    }
    function ssRender(localMatch, apiItems, apiPending) {
      ssRenderLocal(localMatch);
      ssRenderApi(apiItems, apiPending, null);
      ssFinalize();
    }
    function ssCloseDropdown() {
      ssList.classList.remove('open');
      if (ssBox) ssBox.classList.remove('ss-open');   // M-073
      ssInput.setAttribute('aria-expanded', 'false');
      ssResetActive();
    }
    // Pan to a restaurant in the library and open its bottom sheet — the
    // same code path a marker click triggers. Keeps a temp marker out of
    // the way; the actual restaurant marker is already on the map.
    function ssGotoRestaurant(d) {
      ssCloseDropdown();
      ssInput.value = d.name || '';
      ssWrap.classList.add('has-text');
      ssTitleMode = true;
      ssInput.blur();
      ssRemoveTempMarker();

      // The hit can be anywhere in Japan, possibly far outside the current
      // viewport — the grid-based recompute() only materializes markers
      // for visible rows, so we need to poke this one into the cluster
      // manually before cluster.zoomToShowLayer can find it.
      var marker = ensureMarker(d);
      if (!onMap.has(d)) {
        cluster.addLayer(marker);
        onMap.add(d);
      }
      // Pin so the intermediate moveend recomputes (zoomToShowLayer
      // animates through several zoom levels on long flights) don't
      // reap the marker before the cluster has spiderfied it.
      if (pinnedRow && pinnedRow !== d) pinnedRow = null;
      pinnedRow = d;

      function reveal() {
        if (pinnedRow === d) pinnedRow = null;
        // Peek mode: only the header + ribbons are shown so the highlighted
        // marker on the map stays visible. User swipes up on the grip to
        // promote the sheet to its full height. setHighlight inside
        // openSheet repaints the icon — by now the marker is individual
        // (not buried under a child-count badge), so the blue halo +
        // pulse-ring actually render.
        openSheet(d, {peek: true});
      }

      // We *don't* use cluster.zoomToShowLayer here: its panTo-only branch
      // fires whenever the marker is already rendered as an individual
      // icon at the current zoom, which happens immediately for any
      // restaurant in a sparse area (no nearby markers in the cluster
      // group means addLayer plops it down as a free icon, not a cluster
      // child). The user's expectation is "zoom in on the result", so
      // we flyTo a guaranteed-uncluster zoom unconditionally. 17 matches
      // the cluster's disableClusteringAtZoom, so the marker is sure to
      // render as a standalone icon when we land. Math.max preserves a
      // deeper zoom if the user is already zoomed in further.
      var TARGET_ZOOM = 17;
      var latlng = L.latLng(d.lat, d.lon);
      var targetZoom = Math.max(map.getZoom(), TARGET_ZOOM);
      var onArrive = function() {
        map.off('moveend', onArrive);
        if (ssFlightArrive === onArrive) ssFlightArrive = null;
        // Only reveal if we actually landed — a drag that interrupts the
        // flyTo also ends in a moveend, but nowhere near the target, and
        // popping the card there would be the "ghost card" bug.
        var c = map.getCenter();
        var arrived = map.getZoom() >= targetZoom - 0.5
                   && Math.abs(c.lat - d.lat) < 0.005
                   && Math.abs(c.lng - d.lon) < 0.005;
        if (arrived) {
          reveal();
        } else if (pinnedRow === d) {
          pinnedRow = null;   // interrupted — release the reaper pin
        }
      };
      if (ssFlightArrive) map.off('moveend', ssFlightArrive);
      ssFlightArrive = onArrive;
      map.on('moveend', onArrive);
      map.flyTo(latlng, targetZoom, {duration: 0.8});
    }
    function ssGoto(it) {
      ssCloseDropdown();
      ssInput.value = it.name;
      ssWrap.classList.add('has-text');
      ssTitleMode = true;
      ssInput.blur();        // dismiss the on-screen keyboard on mobile
      var latlng = L.latLng(it.lat, it.lon);
      // 16 is tight enough to read shop signs without losing context. flyTo
      // animates; Leaflet caps the duration so it's never jarring.
      map.flyTo(latlng, Math.max(map.getZoom(), 16), {duration: 0.8});
      ssRemoveTempMarker();
      var iconHtml =
        '<div style="position:relative;transform:translate(-50%,-100%);' +
                    'text-align:center;width:max-content;">' +
          '<div style="font-size:26px;line-height:1;' +
                      'filter:drop-shadow(0 1px 3px rgba(0,0,0,0.5));">📍</div>' +
          '<div style="font-size:11px;font-weight:700;color:#1f2937;' +
                      'background:rgba(255,255,255,0.95);' +
                      'padding:1px 6px;border-radius:4px;margin-top:1px;' +
                      'white-space:nowrap;max-width:220px;overflow:hidden;' +
                      'text-overflow:ellipsis;' +
                      'box-shadow:0 1px 2px rgba(0,0,0,0.2);">' +
            escapeHtml(it.name) + '</div>' +
        '</div>';
      ssTempMarker = L.marker(latlng, {
        icon: L.divIcon({className: 'empty', iconSize: [0,0], iconAnchor: [0,0], html: iconHtml})
      }).addTo(map);
      // Small popup attached so the user can immediately bookmark the
      // searched place without scrolling back to the dropdown.
      var popHtml =
        '<div style="font:13px sans-serif;text-align:center;min-width:170px;">' +
          '<div style="font-weight:700;margin-bottom:4px;">' +
            escapeHtml(it.name) + '</div>' +
          '<div lang="en" style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:#6b7280;' +
                      'margin-bottom:8px;">' +
            it.lat.toFixed(6) + ', ' + it.lon.toFixed(6) + '</div>' +
          '<button id="ss-add-bm" ' +
                  'style="padding:4px 12px;font-size:12px;cursor:pointer;' +
                  'border:1px solid #2563eb;border-radius:4px;' +
                  'background:#2563eb;color:#fff;font-weight:600;">' +
                  '⭐ 加入收藏</button>' +
        '</div>';
      L.popup({offset: [0, -26]}).setLatLng(latlng).setContent(localizeText(popHtml)).openOn(map);
      setTimeout(function() {
        var btn = document.getElementById('ss-add-bm');
        if (!btn) return;
        btn.addEventListener('click', function() {
          map.closePopup();
          openBookmarkModal({lat: it.lat, lng: it.lon}, it.name);
        });
      }, 0);
    }
    function ssParseResult(r) {
      // Nominatim returns name/display_name as strings, lat/lon as
      // stringified floats. namedetails.name:zh / :ja if present is a
      // nicer label than the addressy display_name.
      var nd = r.namedetails || {};
      var name = nd['name:zh'] || nd['name:zh-Hans'] || nd['name:zh-Hant']
              || nd['name:ja'] || r.name || nd.name
              || (r.display_name || '').split(',')[0].trim();
      return {
        lat: parseFloat(r.lat),
        lon: parseFloat(r.lon),
        name: name || '未命名',
        address: r.display_name || ''
      };
    }
    // Latest local match for the current query; held in module scope so the
    // API callback can re-render with the same restaurant section on top.
    var ssLocalMatch = {items: [], total: 0};
    // The input doubles as the selected restaurant's title bar. ssQuery is
    // the user's own typed text, tracked separately so selections can't eat
    // it: openSheet switches the display to the name (title mode),
    // closeSheet switches the query back, and refocusing the input while a
    // title is shown resumes the search where it left off. Only the user
    // (typing, ×, Escape) ever changes ssQuery.
    var ssQuery = '';
    var ssTitleMode = false;
    // Last Nominatim result list, so a focus-resume can restore the full
    // dropdown without refetching.
    var ssLastApi = null;
    // The in-flight search flight's moveend handler; tracked so a second
    // result click (or any interruption) detaches the stale one instead of
    // leaving it armed to pop the wrong card on a later pan.
    var ssFlightArrive = null;
    var ssAbort = null;
    var ssAbortTimer = 0;
    // M-176: Nominatim labels come back in whatever accept-language asks
    // for; hard-coding zh-CN handed a simplified-Chinese station name to
    // every English and Japanese user. Japanese is kept as a fallback
    // everywhere — OSM's Japan coverage is far denser in ja than in en.
    function ssAcceptLanguage() {
      var l = (typeof activeLang === 'undefined') ? 'zh-CN' : activeLang;
      if (l === 'zh-TW') return 'zh-TW,zh-Hant,zh,ja,en';
      if (l === 'en')    return 'en,ja';
      if (l === 'ja')    return 'ja,en';
      return 'zh-CN,zh,ja,en';
    }
    // M-082: Nominatim occasionally accepts a connection and then answers
    // nothing. fetch never settles, .busy is never cleared, and the spinner
    // spins until the tab is closed. 9s, then a visible timeout row.
    var SS_TIMEOUT_MS = 9000;
    function ssSearch(q) {
      var seq = ++ssReqSeq;
      ssWrap.classList.add('busy');
      // The request is now genuinely in flight — this is where the 地图搜索
      // section earns its 搜索中… row.
      if (!ssTitleMode) { ssRenderApi(null, true, null); ssFinalize(); }
      // Abort the superseded request instead of letting it run to
      // completion — ssReqSeq already guards the UI, but the dead fetch
      // was still costing radio time and Nominatim quota.
      if (ssAbort) ssAbort.abort();
      if (ssAbortTimer) { clearTimeout(ssAbortTimer); ssAbortTimer = 0; }
      var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      ssAbort = ctrl;
      // Distinguishes "the user typed something newer" (silent) from "we
      // gave up waiting" (must be shown) — both surface as AbortError.
      var timedOut = false;
      ssAbortTimer = setTimeout(function() {
        ssAbortTimer = 0;
        timedOut = true;
        if (ctrl) ctrl.abort();
      }, SS_TIMEOUT_MS);
      // Japan bbox: lon 122-154, lat 24-46. viewbox order:
      //   x1 (left lon), y1 (top lat), x2 (right lon), y2 (bottom lat)
      // bounded=1 forbids matches outside the box; otherwise OSM happily
      // returns "Osaka, Texas" before the city in Japan.
      var url = 'https://nominatim.openstreetmap.org/search?'
              + 'format=jsonv2&limit=6&addressdetails=0&namedetails=1'
              + '&viewbox=122,46,154,24&bounded=1'
              + '&accept-language=' + ssAcceptLanguage()
              + '&q=' + encodeURIComponent(q);
      fetch(url, {headers: {'Accept': 'application/json'},
                  signal: ctrl && ctrl.signal})
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(arr) {
          if (ssAbortTimer) { clearTimeout(ssAbortTimer); ssAbortTimer = 0; }
          if (seq !== ssReqSeq) return;       // stale response
          ssWrap.classList.remove('busy');
          var items = (Array.isArray(arr) ? arr : [])
            .map(ssParseResult)
            .filter(function(x){ return !isNaN(x.lat) && !isNaN(x.lon); });
          ssLastApi = items;
          // The user moved on to a card while this was in flight — cache
          // the results for focus-resume, but don't pop the dropdown open
          // over the sheet they're reading.
          if (ssTitleMode) return;
          ssRenderApi(items, false, null);
          ssFinalize();
        })
        .catch(function(err) {
          if (ssAbortTimer) { clearTimeout(ssAbortTimer); ssAbortTimer = 0; }
          var aborted = err && err.name === 'AbortError';
          // A superseded request stays silent; a timed-out one must clear
          // the spinner and say so, or the box spins forever (M-082).
          if (aborted && !timedOut) return;
          if (seq !== ssReqSeq) return;
          ssWrap.classList.remove('busy');
          if (ssTitleMode) return;
          // Raw Chinese on purpose: #ss-list is under the localizeTree
          // MutationObserver, same as 搜索中… and 没有匹配的结果.
          ssRenderApi(null, false, timedOut
            ? '搜索超时'
            : '搜索失败: ' + err.message);
          ssFinalize();
        });
    }
    function ssOnInput() {
      var v = ssInput.value.trim();
      ssTitleMode = false;   // typing makes the input a live query again
      ssQuery = v;
      ssLastApi = null;
      if (v) ssWrap.classList.add('has-text');
      else   ssWrap.classList.remove('has-text');
      clearTimeout(ssDebounce);
      if (!v) {
        ssReqSeq++;
        if (ssAbort) { ssAbort.abort(); ssAbort = null; }
        if (ssAbortTimer) { clearTimeout(ssAbortTimer); ssAbortTimer = 0; }
        ssLocalMatch = {items: [], total: 0};
        ssWrap.classList.remove('busy');
        ssCloseDropdown();
        ssRemoveTempMarker();
        return;
      }
      // Restaurant-library match runs synchronously — paint it first so the
      // user sees results in the same frame, no debounce wait. The Nominatim
      // call still goes through the debounce, and the 搜索中… row is only
      // painted when that fetch actually fires (ssSearch) — showing it per
      // keystroke advertised a request that kept being cancelled before it
      // was sent, making map-search look perpetually slow. Exception: with
      // zero local hits the dropdown would be blank (or claim "no
      // matches") during the debounce, so the pending row stands in.
      ssLocalMatch = ssMatchLocal(v);
      var willQuery = ssShouldQueryApi(v);
      ssRender(ssLocalMatch, null, willQuery && ssLocalMatch.items.length === 0);
      // M-055: 300ms per keystroke with no length floor is exactly the
      // client-side autocomplete pattern the OSM usage policy forbids, and
      // getting the shared Nominatim instance to block us shows up as
      // "map search is permanently broken". 800ms + a length floor keeps
      // one typed query to roughly one request; the local restaurant match
      // above is unaffected and still paints on every keystroke.
      if (!willQuery) return;
      ssDebounce = setTimeout(function() { ssSearch(v); }, 800);
    }
    // CJK/kana/hangul are dense enough that 2 characters make a real
    // query; Latin scripts need 3 before the result set means anything.
    // The range is spelled with \u escapes rather than literal characters
    // so the build's CJK-run scanner doesn't read it as untranslated UI.
    var SS_CJK_RE = /[\u3040-\u30FF\u3400-\u9FFF\uF900-\uFAFF\uAC00-\uD7AF]/;
    function ssShouldQueryApi(v) {
      if (!v) return false;
      return SS_CJK_RE.test(v) ? v.length >= 2 : v.length >= 3;
    }
    // exitSearch: full bail-out. Used by the × button and Escape — clears
    // text, drops the temp marker + popup, closes the dropdown, and blurs
    // the input so the mobile keyboard goes away.
    function ssExitSearch() {
      ssReqSeq++;
      if (ssAbort) { ssAbort.abort(); ssAbort = null; }
      ssLocalMatch = {items: [], total: 0};
      ssQuery = '';
      ssTitleMode = false;
      ssLastApi = null;
      ssInput.value = '';
      ssWrap.classList.remove('has-text');
      ssWrap.classList.remove('busy');
      ssWrap.classList.remove('searching');
      ssCloseDropdown();
      if (ssTempMarker) map.closePopup();
      ssRemoveTempMarker();
      ssInput.blur();
    }
    ssInput.addEventListener('input', ssOnInput);
    ssInput.addEventListener('focus', function() {
      ssWrap.classList.add('searching');
      // Focusing while the box shows a selection title resumes the search:
      // the typed query comes back along with its cached results. With a
      // title but no prior query, select-all so typing replaces wholesale.
      if (ssTitleMode && ssQuery) {
        ssTitleMode = false;
        ssInput.value = ssQuery;
        ssRender(ssLocalMatch, ssLastApi, false);
      } else if (ssTitleMode) {
        ssInput.select();
      }
      // (.ss-row, not children — the two section sub-containers are
      // always present, so children.length would always be truthy.)
      if (ssList.querySelector('.ss-row')) {
        ssList.classList.add('open');
        ssInput.setAttribute('aria-expanded', 'true');
      }
    });
    ssInput.addEventListener('keydown', function(e) {
      var open = ssList.classList.contains('open');
      if (e.key === 'ArrowDown' && open) {
        e.preventDefault();
        ssSetActive(ssActiveIdx + 1);
      } else if (e.key === 'ArrowUp' && open) {
        e.preventDefault();
        ssSetActive(ssActiveIdx - 1);
      } else if (e.key === 'Escape') {
        if (open && ssInput.value) {
          ssCloseDropdown();
        } else {
          ssExitSearch();
        }
      } else if (e.key === 'Enter') {
        // Enter picks the keyboard-highlighted row; with no highlight,
        // the first result (the previous behavior).
        var rows = ssNavRows();
        var target = (ssActiveIdx >= 0 && rows[ssActiveIdx])
                   ? rows[ssActiveIdx]
                   : ssList.querySelector('.ss-row:not(.ss-empty)');
        if (target) target.click();
      }
    });
    // Mousedown preventDefault keeps focus on the input until our click
    // handler runs — otherwise on desktop the blur fires first, strips the
    // 'searching' class, and the × button vanishes mid-tap.
    ssClear.addEventListener('mousedown', function(e) { e.preventDefault(); });
    // × is the user's universal "back out" button: if a restaurant is
    // selected (sheet open in either peek or full), it deselects first;
    // then the regular ssExitSearch tears down any leftover search UI
    // state (busy spinner, dropdown, mobile keyboard). When nothing's
    // selected, behavior is identical to before.
    ssClear.addEventListener('click', function() {
      if (bsActive) closeSheet();
      ssExitSearch();
    });
    // Click outside the search box closes the dropdown AND drops the
    // 'searching' state so the × button hides once the user is back on the
    // map. (Text, if any, is kept so they can refine on re-focus.)
    document.addEventListener('click', function(e) {
      if (ssBox.contains(e.target)) return;
      ssCloseDropdown();
      ssWrap.classList.remove('searching');
    });

    // chunkedLoading: true splits the initial addLayers() of ~9800 markers
    // into async batches (defaults: 200ms work / 50ms pause) instead of one
    // synchronous pass that would jank the main thread for 1-2s on mobile.
    var cluster = L.markerClusterGroup({
      maxClusterRadius: 40,
      disableClusteringAtZoom: 17,
      chunkedLoading: true
    });
    map.addLayer(cluster);

    // ----- Build initial state. Baked-in baseline (from the JSON files at
    // build time) is the floor; localStorage cache overrides it (last known
    // state on this device); the cloud pull (if signed in) overrides both.
    var state = {fav: new Set(), black: new Set()};
    data.forEach(function(d) {
      if (d.favorited) state.fav.add(d.detail_url);
      if (d.blacklisted) state.black.add(d.detail_url);
    });
    var cache = loadCache();
    if (cache.fav) state.fav = new Set(cache.fav);
    if (cache.black) state.black = new Set(cache.black);

    function isFav(d)   { return state.fav.has(d.detail_url); }
    function isBlack(d) { return state.black.has(d.detail_url); }

    // ----- Sync engine: pull on load/visible/poll, debounced push on toggle.
    var statusEl = document.getElementById('ff-sync-status');
    // Single place that knows the sync UI's meaning: text + kind (''/ok/
    // busy/err) + dirty + signedIn. setStatus and updateNeedsSyncIndicator
    // only record + paint; anything richer (banner, aria-live) reads this.
    var syncStatus = {text: '', kind: '', dirty: false, signedIn: false};
    var STORAGE_BLOCKED_TEXT = '浏览器不允许本站保存数据，收藏只在本次会话有效';
    var storageBlockedSeen = false;
    function setStatus(text, kind) {
      // M-046: once a storage write has failed, the resting status stays on
      // the warning instead of a reassuring "已同步" / "本地模式".
      if (storageBlockedSeen && kind !== 'err' && kind !== 'busy') {
        text = STORAGE_BLOCKED_TEXT; kind = 'err';
      }
      syncStatus.text = text; syncStatus.kind = kind || '';
      if (!statusEl) return;
      // #ff-sync-status sits outside the i18n MutationObserver's containers,
      // so dynamic writes must localize explicitly (same for every sink in
      // this file that writes user-visible zh-CN outside those containers).
      statusEl.textContent = localizeText(text);
      statusEl.style.color = kind === 'err' ? '#dc2626'
                           : kind === 'ok'  ? '#16a34a'
                           : kind === 'busy'? '#2563eb' : '#6b7280';
    }
    storageBlockedHook = function() {
      storageBlockedSeen = true;
      setStatus(STORAGE_BLOCKED_TEXT, 'err');
    };
    // Not signed in: the resting text is "local mode" — unless writes are
    // failing, in which case "local" is exactly what the user doesn't have.
    function setLocalModeStatus() {
      if (storageBlockedSeen) { setStatus(STORAGE_BLOCKED_TEXT, 'err'); return; }
      setStatus(dirty ? '本地模式（改动仅存浏览器）' : '本地模式', dirty ? 'err' : '');
    }
    // M-003: the merge base for localStorage writes is what this page read
    // at boot (see saveCache). null fav/black = no cache key yet → union.
    lastWrittenCache = {
      fav:   cache.fav   ? cache.fav.slice()   : null,
      black: cache.black ? cache.black.slice() : null,
      dirty: !!cache.dirty
    };
    // dirty is restored from cache so an unpushed change survives a refresh.
    var dirty = cache.dirty || false;
    var pushTimer = null, pollTimer = null;
    // Bumped on every local edit (schedulePush). A pull snapshots it before
    // the fetch and discards the response if it moved while in flight —
    // otherwise a star tapped during the round-trip would be visibly
    // reverted by the stale remote blob and then re-uploaded as reverted.
    var stateGen = 0;
    // Server snapshot backing the 409 three-way merge; see loadSyncBase.
    var syncBase = loadSyncBase();
    // M-002: the base from just before our last successful PUT. If a pull
    // later finds our version number with someone else's write id, the
    // concurrent writer built on THIS, not on syncBase.
    var prevSyncBase = null;
    // Serialize PUTs. Without this, push A (stale) can land after push B
    // (fresh) and the server silently keeps A's state while the client
    // thinks everything is synced. A push that fires while one is in
    // flight just queues; the queued run re-snapshots state, so it always
    // uploads the latest. pushQueuedDepth carries the 409-retry depth.
    var pushInFlight = false, pushQueued = false, pushQueuedDepth = 0;

    // Flash the filter FAB when there are local changes that haven't
    // landed on the server. Colour depends on whether the user is signed in:
    //   not signed in → red pulse (urgent — edits live only in this browser)
    //   signed in     → blue pulse (just informational — push will arrive)
    var fabEl = document.getElementById('ff-fab');
    function updateNeedsSyncIndicator() {
      var signedIn = !!configured();
      var d = !!dirty;
      syncStatus.dirty = d; syncStatus.signedIn = signedIn;
      renderSyncUi();   // M-033/M-034: banners + toast + avatar badge
      if (!fabEl) return;
      fabEl.classList.toggle('needs-sync',         d && !signedIn);
      fabEl.classList.toggle('needs-sync-pending', d &&  signedIn);
      // M-033: this used to read "点击登录以跨设备同步" — but clicking opens
      // the filter sheet, and a title= is unreachable on touch anyway. The
      // call to action moved to #sync-hint; the tooltip now describes what
      // the button actually is.
      fabEl.title = localizeText('筛选') + (d
        ? l10nParen(localizeText(signedIn ? '改动待同步到云端'
                                          : '仅存于本地浏览器'))
        : '');
      updateFabAria();
    }

    // ===== M-033 / M-034 / M-089: sync UX surfaces =====
    // Presentation only. Nothing here decides sync policy, writes the state
    // blob, or touches the KV contract; the single source of truth is the
    // syncStatus object maintained by setStatus / updateNeedsSyncIndicator.
    // One new localStorage key (tabelog.syncHintDismissed) — nothing else in
    // this block reads or writes persisted state.
    var SYNC_HINT_KEY = 'tabelog.syncHintDismissed';
    var srEl        = document.getElementById('sync-sr');
    var bannerEl    = document.getElementById('sync-banner');
    var bannerMsgEl = document.getElementById('sync-banner-msg');
    var bannerXEl   = document.getElementById('sync-banner-x');
    var hintEl      = document.getElementById('sync-hint');
    var stackEl     = document.getElementById('sync-stack');
    var avatarDotEl = document.getElementById('ss-avatar-dot');
    var syncUiInit  = false;

    // M-089: the one live region every otherwise-silent state change goes
    // through. #ff-sync-status carries its own role=status, so anything it
    // already says is NOT repeated here.
    function announce(text) { if (srEl) srEl.textContent = text; }

    // ---- transient toasts / snackbars -------------------------------------
    function showToast(text, opts) {
      opts = opts || {};
      if (!stackEl) return null;
      var el = document.createElement('div');
      el.className = 'sync-toast';
      var msg = document.createElement('span');
      msg.className = 'sync-toast-msg';
      msg.textContent = text;
      el.appendChild(msg);
      var timer = null;
      function dismiss() {
        if (timer) { clearTimeout(timer); timer = null; }
        if (el.parentNode) el.parentNode.removeChild(el);
      }
      if (opts.actionLabel) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'sync-btn';
        btn.textContent = opts.actionLabel;
        btn.addEventListener('click', function() {
          dismiss();
          if (opts.onAction) opts.onAction();
        });
        el.appendChild(btn);
      }
      stackEl.insertBefore(el, stackEl.firstChild);
      // #sync-stack isn't one of the four observed containers, so a bookmark
      // name carrying an emoji has to be swapped here (CLAUDE.md).
      try { emojify(el); } catch (_) {}
      announce(text);
      timer = setTimeout(dismiss, opts.ms || 6000);
      return {close: dismiss};
    }

    // ---- M-034: failures get a banner, successes stay quiet ---------------
    // syncStatus.text is the raw zh-CN string the sync engine recorded; map
    // it onto plain language plus the one thing the user can do next. Three
    // buckets, matching the three ways a sync actually fails.
    function syncErrorCopy(text) {
      if (!text) return null;
      // Anonymous local-mode is not a failure — that's #sync-hint's job.
      if (text.indexOf('本地模式') === 0) return null;
      if (text.indexOf(STORAGE_BLOCKED_TEXT) === 0) return text;
      if (text.indexOf('登录已过期') === 0) {
        return l10nSentence(['登录已过期', '请重新登录']);
      }
      if (/HTTP 5\d\d/.test(text) || text.indexOf('同步冲突') === 0) {
        return l10nSentence(['服务器出错', '改动已存在本机', '稍后自动重试']);
      }
      if (text.indexOf('同步失败') === 0 || text.indexOf('保存失败') === 0) {
        return l10nSentence(['网络不通', '改动已存在本机', '恢复后会自动上传']);
      }
      if (text.indexOf('同步数据') === 0) return localizeText(text);
      return null;
    }
    var bannerMuted = '';   // copy the user explicitly dismissed
    function renderSyncBanner() {
      if (!bannerEl) return;
      var copy = syncStatus.kind === 'err' ? syncErrorCopy(syncStatus.text) : null;
      if (!copy) { bannerMuted = ''; bannerEl.hidden = true; return; }
      if (copy === bannerMuted) { bannerEl.hidden = true; return; }
      // Unhide first, then write: role=alert announces content that changes
      // while the region is already rendered.
      bannerEl.hidden = false;
      if (bannerMsgEl && bannerMsgEl.textContent !== copy) {
        bannerMsgEl.textContent = copy;
      }
    }
    if (bannerXEl) bannerXEl.addEventListener('click', function() {
      bannerMuted = bannerMsgEl ? bannerMsgEl.textContent : '';
      bannerEl.hidden = true;
    });

    // ---- M-033: the "sign in to sync" hint --------------------------------
    function hintDismissed() {
      try { return localStorage.getItem(SYNC_HINT_KEY) === '1'; }
      catch (_) { return false; }
    }
    var hintSuppressUntil = 0, hintRetryTimer = null;
    function renderSyncHint() {
      if (!hintEl) return;
      var want = syncStatus.dirty && !syncStatus.signedIn && !hintDismissed();
      // Don't stack the banner on top of the toast that just said the same
      // thing — let the toast finish, then show it.
      if (want && Date.now() < hintSuppressUntil) {
        if (!hintRetryTimer) {
          hintRetryTimer = setTimeout(function() {
            hintRetryTimer = null; renderSyncHint();
          }, (hintSuppressUntil - Date.now()) + 120);
        }
        want = false;
      }
      hintEl.hidden = !want;
    }
    var hintOkEl = document.getElementById('sync-hint-ok');
    if (hintOkEl) hintOkEl.addEventListener('click', function() {
      try { localStorage.setItem(SYNC_HINT_KEY, '1'); } catch (_) {}
      if (hintEl) hintEl.hidden = true;
    });
    var hintSigninEl = document.getElementById('sync-hint-signin');
    if (hintSigninEl) hintSigninEl.addEventListener('click', function() {
      if (hintEl) hintEl.hidden = true;
      try { openAvatarMenu(); } catch (_) {}
    });

    // First anonymous edit of this page load gets an immediate, quiet
    // explanation instead of a red pill with a tooltip nobody can reach.
    var anonToastShown = false;
    var lastDirtySeen = !!dirty;
    function maybeAnonToast() {
      if (anonToastShown || lastDirtySeen) return;
      if (!syncStatus.dirty || syncStatus.signedIn) return;
      anonToastShown = true;
      hintSuppressUntil = Date.now() + 6500;
      showToast(l10nSentence(['已保存在此浏览器', '登录可在其他设备恢复']), {
        actionLabel: localizeText('登录'),
        ms: 6000,
        onAction: function() { try { openAvatarMenu(); } catch (_) {} }
      });
    }

    // ---- M-089: the FAB's accessible name has to carry the visible count --
    function updateFabAria() {
      if (!fabEl) return;
      var cnt = fabEl.querySelector('.ff-count');
      var tot = fabEl.querySelector('.ff-total');
      var shown = cnt ? cnt.textContent : '';
      var total = tot ? tot.textContent : '';
      fabEl.setAttribute('aria-label',
        localizeText('筛选结果') + ' ' + shown + ' / ' + total);
    }

    function renderSyncUi() {
      if (!syncUiInit) return;
      renderSyncBanner();
      maybeAnonToast();
      renderSyncHint();
      if (avatarDotEl) {
        avatarDotEl.hidden = !(syncStatus.dirty || syncStatus.kind === 'err');
      }
      lastDirtySeen = syncStatus.dirty;
    }

    // ---- M-028: empty state ----------------------------------------------
    var emptyMapEl   = document.getElementById('ff-empty-map');
    var emptyPanelEl = document.getElementById('ff-empty-panel');
    var emptyAnnounced = false;
    function updateEmptyState(n) {
      var zero = n === 0;
      if (emptyMapEl)   emptyMapEl.hidden = !zero;
      if (emptyPanelEl) emptyPanelEl.hidden = !zero;
      var nodes = document.querySelectorAll('.ff-count');
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].classList.toggle('is-zero', zero);
      }
      updateFabAria();
      if (zero && !emptyAnnounced) {
        emptyAnnounced = true;
        announce(localizeText('没有符合条件的餐厅'));
      } else if (!zero) {
        emptyAnnounced = false;
      }
    }
    ['ff-empty-reset', 'ff-empty-panel-reset', 'ff-panel-reset']
      .forEach(function(id) {
        var b = document.getElementById(id);
        if (b) b.addEventListener('click', function() { resetFilters(); });
      });

    // ---- M-008: delete my cloud data --------------------------------------
    // The only place in this file allowed to clear localStorage, and only
    // after the Worker has confirmed the cloud copy is gone (204). Anything
    // else — including today's 405 from the not-yet-deployed Worker — leaves
    // every byte of local state exactly where it is.
    var delCloudBtn   = document.getElementById('ssm-delete-cloud');
    var delCloudLabel = document.getElementById('ssm-delete-label');
    var delCloudMsg   = document.getElementById('ssm-delete-msg');
    var delArmTimer = null, delBusy = false;
    function setDelMsg(text, isErr) {
      if (!delCloudMsg) return;
      delCloudMsg.textContent = text || '';
      delCloudMsg.style.color = isErr ? '#b91c1c' : '#6b7280';
    }
    function disarmDeleteCloud() {
      if (delArmTimer) { clearTimeout(delArmTimer); delArmTimer = null; }
      if (!delCloudBtn) return;
      delCloudBtn.removeAttribute('data-armed');
      if (delCloudLabel) delCloudLabel.textContent = localizeText('删除我的云端数据');
    }
    if (delCloudBtn) delCloudBtn.addEventListener('click', function() {
      if (delBusy) return;
      if (!configured()) {
        setDelMsg(localizeText('请先登录'), true);
        return;
      }
      if (delCloudBtn.getAttribute('data-armed') !== '1') {
        // First click arms; second click within 6s commits. Deliberately a
        // two-step in the menu rather than a confirm() — the destructive
        // wording has to be readable in the user's own language, and
        // confirm() text can't be styled or dismissed by Escape on iOS.
        delCloudBtn.setAttribute('data-armed', '1');
        if (delCloudLabel) {
          delCloudLabel.textContent =
            localizeText('确认删除') + '？' + localizeText('此操作不可撤销');
        }
        setDelMsg('', false);
        delArmTimer = setTimeout(disarmDeleteCloud, 6000);
        return;
      }
      disarmDeleteCloud();
      delBusy = true;
      setDelMsg(localizeText('正在删除') + '…', false);
      fetchAuthed(API, {method: 'DELETE'}).then(function(r) {
        delBusy = false;
        if (r.status !== 204 && r.status !== 200) {
          setDelMsg(l10nSentence(['服务暂不可用', '请稍后再试'])
                    + ' (HTTP ' + r.status + ')', true);
          return;
        }
        setDelMsg(localizeText('云端数据已删除'), false);
        // Cloud copy is gone — now, and only now, drop the local mirrors.
        // syncBase goes too: keeping a base for a blob that no longer
        // exists would make the next pull look like a remote deletion of
        // data we still hold.
        function wipe() {
          ['omakase_state_cache_v2', 'tabelog.auth',
           'tabelog.bookmarks', 'tabelog.syncBase'].forEach(function(k) {
            try { localStorage.removeItem(k); } catch (_) {}
          });
        }
        wipe();
        // A pull/push already in flight can write a key back between here
        // and the actual unload (the same race signOut() hits), so wipe
        // once more at the last possible moment.
        window.addEventListener('pagehide', wipe);
        setTimeout(function() { location.reload(); }, 700);
      }).catch(function() {
        delBusy = false;
        setDelMsg(l10nSentence(['服务暂不可用', '请稍后再试']), true);
      });
    });

    syncUiInit = true;
    // Seed the status object from the values loaded at boot BEFORE the first
    // render: renderSyncUi ends by latching lastDirtySeen, and latching a
    // stale `false` here would make the first updateNeedsSyncIndicator look
    // like a fresh user edit and fire the "saved in this browser" toast on
    // every page load of an already-dirty device.
    syncStatus.dirty = !!dirty;
    syncStatus.signedIn = !!configured();
    renderSyncUi();
    updateFabAria();

    function refreshAllMarkers() {
      // Only the markers that have actually been materialized can have their
      // icon updated. Rows outside the viewport will pick up the new state
      // the next time they're added to the cluster (makeIcon reads fav/black
      // state at construction time).
      for (var i = 0; i < data.length; i++) {
        var d = data[i];
        if (d._m) d._m.setIcon(makeIcon(d));
      }
      updateFavCount();
      updateBlackCount();
      apply();
    }
    // Wipe + re-render the bookmarks store in place — closures hold the
    // same array reference, so we mutate rather than reassign. Both layers
    // (bookmarks + user-added attractions) get cleared so the rebuild
    // starts from a clean slate. The builtin landmarks share the same
    // layers, so the wipe takes them out too — renderFavoritesBuiltin puts
    // them back (after rebuildHiddenIds, so a hide flag set on another
    // device takes effect now). Used by pull() and the 409 merge path.
    // M-042: build the whole replacement first; a throw while sanitizing
    // leaves the old array and layers untouched instead of a half-replaced
    // screen (rebuildBookmarkLayers isolates per-entry render failures).
    function replaceBookmarksArray(arr) {
      var next = [];
      (Array.isArray(arr) ? arr : []).forEach(function(bm) {
        if (!bm || typeof bm !== 'object') return;
        sanitizeBookmarkEmoji(bm);
        next.push(bm);
      });
      bookmarks.length = 0;
      next.forEach(function(bm) { bookmarks.push(bm); });
      rebuildBookmarkLayers();
      saveBookmarks();
    }
    // Deep-copied server snapshot for the merge base. Bookmark objects are
    // edited in place elsewhere, so the base must not share references.
    function currentSub() {
      var a = configured();
      return (a && a.sub) || '';
    }
    function snapshotRemote(remote) {
      return {
        v: typeof remote.v === 'number' ? remote.v : 0,
        w: typeof remote.w === 'string' ? remote.w : '',
        sub: currentSub(),
        favorites: Array.isArray(remote.favorites) ? remote.favorites.slice() : [],
        blacklist: Array.isArray(remote.blacklist) ? remote.blacklist.slice() : [],
        bookmarks: Array.isArray(remote.bookmarks)
          ? JSON.parse(JSON.stringify(remote.bookmarks)) : [],
      };
    }
    function arrOr(x, fallback) { return Array.isArray(x) ? x : fallback; }
    // Does memory hold anything the last known server state lacks (or vice
    // versa)? True after another tab's unsent edit was merged in via the
    // storage event, or after the M-001 rebase — i.e. content the server
    // may not have. push() uses the same test to skip a no-op PUT.
    function contentMatchesBase() {
      return setEqualsList(state.fav,   syncBase.favorites)
          && setEqualsList(state.black, syncBase.blacklist)
          && JSON.stringify(bookmarks) === JSON.stringify(syncBase.bookmarks);
    }
    // M-001 (the P0): tabelog.syncBase is shared by every tab, so the base
    // on disk can be newer than the one in memory — another tab pushed and
    // rewrote it. Before we build a PUT we adopt it, but ONLY by merging its
    // CONTENT first (base = our old snapshot, theirs = the disk snapshot).
    // Taking just its version number would let this tab's stale list pass
    // the Worker's baseV check with no 409 and no merge, deleting the other
    // tab's entries from the cloud — never do that; it is M-001 in another
    // shape. A base that belongs to a different account is never adopted;
    // an unowned ('' sub, never synced) base merges as a union.
    function adoptDiskSyncBase() {
      var disk = loadSyncBase();
      var sub = currentSub();
      if (!sub || disk.sub !== sub) return false;
      if (syncBase.sub === sub && disk.v <= syncBase.v) return false;
      var base = (syncBase.sub === sub) ? syncBase : emptySyncBase(sub);
      state.fav   = mergeSets(base.favorites, state.fav,   disk.favorites);
      state.black = mergeSets(base.blacklist, state.black, disk.blacklist);
      var bms = mergeBookmarks(base.bookmarks, bookmarks.slice(),
                               JSON.parse(JSON.stringify(disk.bookmarks)));
      if (JSON.stringify(bms) !== JSON.stringify(bookmarks)) {
        bookmarks.length = 0;
        bms.forEach(function(b) { bookmarks.push(b); });
        rebuildBookmarkLayers();
      }
      syncBase = disk;
      refreshAllMarkers();
      return true;
    }
    // True for the single retry that follows a successful silentReAuth.
    // Prevents an infinite loop in the (rare) case the Worker rejects a
    // freshly-minted token too.
    var pullRetriedAfterSilent = false;
    // M-138 / M-147: one GET at a time, and interval / focus / online /
    // pageshow can't stack requests within a second of each other.
    var pullInFlight = false, lastPullAt = 0;
    function dropAuth() {
      try { localStorage.removeItem(AUTH_KEY); } catch (_) {}
    }
    // M-041: a silent re-auth that lands on a different Google account is
    // called out; the pull that follows switches to that account's cloud.
    function silentReAuthWatched(cb) {
      var before = currentSub();
      silentReAuth(function(ok) {
        if (ok && before && currentSub() && currentSub() !== before) {
          setStatus('已切换账号，已改用云端数据', 'err');
        }
        cb(ok);
      });
    }
    // Runs once a GET has been applied (or found nothing new): anything
    // dirty — or merged in from another tab and not yet on the server —
    // goes up now. The M-053 fix is exactly that a dirty device keeps
    // pulling AND re-pushes; push() skips the PUT when there is nothing new.
    function afterPullSettled() {
      if (!dirty && !contentMatchesBase()) dirty = true;
      if (dirty) { push(); return; }
      setStatus('已同步 ' + new Date().toLocaleTimeString(), 'ok');
    }
    function pull(force) {
      if (!configured()) {
        setLocalModeStatus();
        updateNeedsSyncIndicator();
        return;
      }
      // M-053: a dirty device used to bail here, so one whose push kept
      // failing (413, 5xx, no network at the time) never received anyone
      // else's changes again. Now the GET always goes out; the response is
      // three-way merged (base = syncBase, so a remote delete still wins)
      // and whatever is dirty is re-pushed on top — the 409 path's merge.
      if (pullInFlight || pushInFlight) return;
      if (!force && Date.now() - lastPullAt < 1000) return;
      pullInFlight = true;
      lastPullAt = Date.now();
      // Snapshot the edit counter: if it moves while the GET is in flight,
      // the response below is stale by definition and gets discarded.
      var genAtStart = stateGen;
      // For the M-042 rollback: what was on screen before the apply.
      var pre = null;
      setStatus('同步中…', 'busy');
      fetchAuthed(API)
        .then(function(r) {
          if (r.status === 401) {
            // If we already retried after a silent refresh and STILL got
            // 401, the Worker is rejecting fresh tokens — don't loop;
            // bail to the manual sign-in flow.
            if (pullRetriedAfterSilent) {
              pullRetriedAfterSilent = false;
              setStatus('登录已过期，请重新登录', 'err');
              dropAuth();
              updateNeedsSyncIndicator();
              return null;
            }
            // Token rejected by Worker — expired between our cached exp
            // check and the request. Try silent re-auth; on success the
            // next pull picks up the fresh token. Only clear stored auth
            // (forcing the user to re-sign-in) if silent re-auth also
            // fails — that's the genuine "Google session is gone" case.
            setStatus('重新连接中…', 'busy');
            silentReAuthWatched(function(ok) {
              if (ok) {
                pullRetriedAfterSilent = true;
                setTimeout(function() { pull(true); }, 100);
                return;
              }
              setStatus('登录已过期，请重新登录', 'err');
              dropAuth();
              updateNeedsSyncIndicator();
            });
            return null;
          }
          pullRetriedAfterSilent = false;
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(remote) {
          if (!remote) return;
          // A local edit landed while this GET was in flight — applying the
          // response would revert it on screen. Drop it; the edit's pending
          // push flushes shortly and the next poll re-pulls fresh state.
          if (stateGen !== genAtStart) return;
          if (typeof remote !== 'object' || Array.isArray(remote)) remote = {};
          var sub = currentSub();
          // M-041: the local data belongs to another account → the cloud
          // copy of the account signed in now replaces it (never a union:
          // that uploaded the previous person's favorites into this one).
          if (syncBase.sub && syncBase.sub !== sub) {
            adoptRemoteWholesale(remote);
            setStatus('已切换账号，已改用云端数据', 'ok');
            return;
          }
          var remoteV = typeof remote.v === 'number' ? remote.v : 0;
          var remoteW = typeof remote.w === 'string' ? remote.w : '';
          var base = syncBase;
          if (syncBase.sub === sub && remoteV === syncBase.v) {
            if (remoteW === syncBase.w && (remoteW || !dirty)) {
              // Same version, same write id → nothing changed remotely; skip
              // the full apply (layer wipe + setIcon on every materialized
              // marker + filter recompute + storage writes). The 60s poll
              // and every refocus land here in the common case.
              afterPullSettled();
              return;
            }
            if (!remoteW && !syncBase.w) {
              // Both w-less while we hold unsent edits: this base was last
              // confirmed by a pre-2.0 client, and pre-2.0 tabs could leave
              // the cache and the base inconsistent on disk (M-001's boot
              // shape: base new, list old). Indistinguishable from a real
              // local delete, so union once — may resurrect a pending
              // delete, can never drop the other tab's entry. Never
              // recurs once a 2.0 write (with w) has landed.
              base = emptySyncBase(sub);
            } else {
              // M-002: same version, different write id → the PUT that gave
              // us this version was overwritten by a concurrent one (the
              // Worker's get→put is not atomic) or by an old client that
              // sends no w. Our base never reached the server, so it is not
              // an ancestor of what is there: merge against the base from
              // before our push when it fits (both writers built on it),
              // otherwise void the base — union may resurrect, never loses.
              base = (prevSyncBase && prevSyncBase.sub === sub
                      && prevSyncBase.v === remoteV - 1)
                ? prevSyncBase : emptySyncBase(sub);
            }
          } else if (syncBase.sub !== sub) {
            // First sign-in on this device ('' base): the local state is
            // the seed — union with whatever the account already has.
            base = emptySyncBase(sub);
          } else if (typeof remote.v !== 'number' && syncBase.v > 0) {
            // A blob with no version where we remember one: the server
            // state was wiped or never written. Merging against our base
            // would read that as "everything deleted" — union instead.
            base = emptySyncBase(sub);
          }
          pre = {fav: new Set(state.fav), black: new Set(state.black),
                 bm: JSON.stringify(bookmarks)};
          mergeRemoteIntoLocal(remote, base);   // renders first (M-046)…
          syncBase = snapshotRemote(remote);
          saveSyncBase(syncBase);               // …persists after
          if (saveCache(state, dirty)) refreshAllMarkers();
          pre = null;
          afterPullSettled();
        })
        .catch(function(e) {
          setStatus('同步失败: ' + e.message, 'err');
          // M-042: the apply blew up midway → put back exactly what was on
          // screen before it, not a half-applied mix.
          if (pre) {
            try {
              state.fav = pre.fav; state.black = pre.black;
              if (JSON.stringify(bookmarks) !== pre.bm) {
                replaceBookmarksArray(JSON.parse(pre.bm));
              }
              refreshAllMarkers();
            } catch (_) {}
          }
        })
        .finally(function() {
          pullInFlight = false;
          updateNeedsSyncIndicator();
        });
    }
    // Three-way merge of a server blob into memory (mergeSets/mergeBookmarks
    // are top-level now — the localStorage writers use them too). base
    // defaults to syncBase; the callers that know the base is untrustworthy
    // (wiped server, overwritten write, first sign-in) pass an empty one so
    // the merge unions. A missing array on their side means "unknown", not
    // "empty" — treated as unchanged relative to the base, i.e. ours stands.
    // Renders before anything is persisted (M-046).
    function mergeRemoteIntoLocal(theirs, base) {
      base = base || syncBase;
      var fav   = mergeSets(base.favorites, state.fav,
                            arrOr(theirs.favorites, base.favorites));
      var black = mergeSets(base.blacklist, state.black,
                            arrOr(theirs.blacklist, base.blacklist));
      var bms   = mergeBookmarks(base.bookmarks, bookmarks.slice(),
                    JSON.parse(JSON.stringify(arrOr(theirs.bookmarks, base.bookmarks))));
      state.fav = fav; state.black = black;
      replaceBookmarksArray(bms);
      refreshAllMarkers();
    }
    // M-041: signed into a different account than the local data belongs
    // to → the cloud copy replaces local state outright. The previous
    // account's favorites/pins must not be merged (= uploaded) into this
    // one, so both localStorage merge bases are voided before the write.
    function adoptRemoteWholesale(remote) {
      state.fav   = new Set(arrOr(remote.favorites, []));
      state.black = new Set(arrOr(remote.blacklist, []));
      var bms = Array.isArray(remote.bookmarks)
        ? JSON.parse(JSON.stringify(remote.bookmarks))
        : JSON.parse(JSON.stringify(EMBEDDED_BOOKMARKS));   // fresh-device seed
      dirty = false;
      lastWrittenCache = null;
      lastWrittenBookmarks = null;
      replaceBookmarksArray(bms);
      refreshAllMarkers();
      syncBase = snapshotRemote(remote);
      saveSyncBase(syncBase);
      saveCache(state, false);
    }
    var pushRetriedAfterSilent = false;
    // M-002: 12 hex chars of randomness per PUT. The Worker stores the body
    // as-is, so `w` rides along in the blob and comes back on GET.
    function randomWriteId() {
      try {
        var a = new Uint8Array(6);
        crypto.getRandomValues(a);
        var s = '';
        for (var i = 0; i < a.length; i++) s += ('0' + a[i].toString(16)).slice(-2);
        return s;
      } catch (_) {
        return (Date.now().toString(16) + Math.random().toString(16).slice(2)).slice(0, 12);
      }
    }
    // The PUT body is always built from scratch: the three arrays the
    // Worker stores, the version we're building on, and the write id.
    // Nothing else may be added at the top level — the Worker replaces the
    // blob wholesale, so any extra field would need M-044 first.
    function buildBody(w) {
      return JSON.stringify({
        favorites: Array.from(state.fav),
        blacklist: Array.from(state.black),
        bookmarks: bookmarks,
        baseV: syncBase.v,
        w: w,
      });
    }
    var BODY_WARN_CHARS = 180000;   // the Worker rejects > 200,000 (M-053)
    function push(conflictDepth) {
      conflictDepth = conflictDepth || 0;
      // Local mode (not signed in): nothing to push, but keep dirty=true so
      // the FAB keeps flashing — the whole point is for the user to notice
      // they haven't enabled sync. Indicator clears once they sign in and
      // a real push succeeds.
      if (!configured()) {
        if (saveCache(state, dirty)) refreshAllMarkers();
        setLocalModeStatus();
        updateNeedsSyncIndicator();
        return;
      }
      if (pushInFlight) {
        pushQueued = true;
        pushQueuedDepth = Math.max(pushQueuedDepth, conflictDepth);
        return;
      }
      var sub = currentSub();
      // M-041: the base belongs to another account → this device still
      // holds that account's data. Don't upload it; pull() swaps in the
      // cloud copy of the account that is signed in now.
      if (syncBase.sub && syncBase.sub !== sub) { pull(true); return; }
      // M-001: rebase onto a newer base another tab left on disk — by
      // merging its content, see adoptDiskSyncBase.
      adoptDiskSyncBase();
      if (syncBase.sub !== sub) {
        // Never synced ('' sub): an empty base can't propagate deletions,
        // and if this account already has a blob the version mismatch 409s
        // into a union merge — the first-sign-in seed upload.
        syncBase = emptySyncBase(sub);
        saveSyncBase(syncBase);
      }
      // Nothing the server doesn't already have (another tab uploaded it,
      // or a keepalive flush landed) → don't spend a KV write on it.
      if (syncBase.v > 0 && contentMatchesBase()) {
        dirty = false;
        if (saveCache(state, false)) refreshAllMarkers();
        setStatus('已同步 ' + new Date().toLocaleTimeString(), 'ok');
        updateNeedsSyncIndicator();
        return;
      }
      pushInFlight = true;
      setStatus('保存中…', 'busy');
      // Snapshot: if an edit lands while the PUT is in flight, the response
      // must not clear dirty — the queued follow-up push flushes it.
      var genAtPush = stateGen;
      var w = randomWriteId();
      var body = buildBody(w);
      var baseBeforePush = syncBase;
      if (body.length > BODY_WARN_CHARS) {
        setStatus('同步数据接近上限，请删减收藏或书签', 'err');
      }
      fetchAuthed(API, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: body
      })
        .then(function(r) {
          if (r.status === 401) {
            if (pushRetriedAfterSilent) {
              pushRetriedAfterSilent = false;
              setStatus('登录已过期，请重新登录', 'err');
              dropAuth();
              updateNeedsSyncIndicator();
              return;
            }
            // Same recovery as pull(): try silent re-auth before clearing
            // the stored token. dirty stays true so the retried push (or
            // the next user edit) actually flushes our pending changes.
            setStatus('重新连接中…', 'busy');
            saveCache(state, true);
            silentReAuthWatched(function(ok) {
              if (ok) {
                pushRetriedAfterSilent = true;
                setTimeout(push, 100);
                return;
              }
              setStatus('登录已过期，请重新登录', 'err');
              dropAuth();
              updateNeedsSyncIndicator();
            });
            return;
          }
          if (r.status === 409) {
            // Another device wrote since our last pull/push. Merge its blob
            // into local state, rebase, and re-push on top of it via the
            // queue (the .finally below re-invokes immediately). Depth-
            // capped: repeated 409s mean the server is advancing under us;
            // dirty stays true and the 60s poll picks the retry back up.
            if (conflictDepth >= 3) {
              saveCache(state, true);
              setStatus('同步冲突：改动已存本地，稍后自动重试', 'err');
              return;
            }
            return r.json().then(function(theirs) {
              if (!theirs || typeof theirs !== 'object' || Array.isArray(theirs)) theirs = {};
              // A conflicting blob with no version means the server state
              // was wiped or never written (a real mass-delete from another
              // device would carry v). Merging against our old base would
              // read that as "everything deleted" and drop local data —
              // instead void the base so the merge unions and re-uploads.
              var base = (typeof theirs.v !== 'number') ? emptySyncBase(sub) : syncBase;
              mergeRemoteIntoLocal(theirs, base);
              syncBase = snapshotRemote(theirs);
              saveSyncBase(syncBase);
              saveCache(state, true);   // still dirty until the re-push lands
              pushQueued = true;
              pushQueuedDepth = Math.max(pushQueuedDepth, conflictDepth + 1);
            });
          }
          if (r.status === 413) {
            // M-053: over the Worker's 200,000-char cap. Nothing to retry
            // until the user trims; the data stays local and dirty.
            saveCache(state, true);
            setStatus('同步数据超过上限，未能保存到云端', 'err');
            return;
          }
          if (!r.ok) throw new Error('HTTP ' + r.status);
          pushRetriedAfterSilent = false;
          return r.text().then(function(t) {
            // New Worker answers {v: n}; a pre-versioning Worker says 'ok'
            // (then the blob has no v and the next pull rebases us to 0).
            var newV = syncBase.v + 1;
            try {
              var j = JSON.parse(t);
              if (j && typeof j.v === 'number') newV = j.v;
            } catch (_) {}
            var sent = JSON.parse(body);
            prevSyncBase = baseBeforePush;
            syncBase = {
              v: newV,
              w: w,
              sub: sub,
              favorites: sent.favorites || [],
              blacklist: sent.blacklist || [],
              bookmarks: sent.bookmarks || [],
            };
            saveSyncBase(syncBase);
            dirty = (stateGen !== genAtPush);
            if (saveCache(state, dirty)) refreshAllMarkers();
            if (!dirty) {
              setStatus('已同步 ' + new Date().toLocaleTimeString(), 'ok');
            }
            // Still dirty → an edit raced the PUT; its own scheduled push
            // (or the queue below) uploads it within ~2.5 s.
          });
        })
        .catch(function(e) {
          // Keep dirty=true (in memory AND in localStorage) on failure so the
          // next pull — or a page refresh — doesn't silently clobber our
          // unsaved change with the stale remote state. Change recovers when
          // a future push succeeds.
          saveCache(state, true);
          setStatus('保存失败: ' + e.message + '（已存本地，稍后自动重试）', 'err');
        })
        .finally(function() {
          pushInFlight = false;
          updateNeedsSyncIndicator();
          if (pushQueued) {
            pushQueued = false;
            var d = pushQueuedDepth;
            pushQueuedDepth = 0;
            push(d);
          }
        });
    }
    function schedulePush() {
      stateGen++;   // invalidates any pull/push response currently in flight
      dirty = true;
      // M-003: the write merges in whatever another tab saved meanwhile;
      // M-046: it can no longer throw, so the timer below is always set.
      if (saveCache(state, true)) refreshAllMarkers();
      updateNeedsSyncIndicator();
      clearTimeout(pushTimer);
      // M-039: 2.5 s (was 500 ms) so a burst of taps costs one KV write.
      pushTimer = setTimeout(push, 2500);
    }
    // M-045: an edit still inside the debounce window when the tab is
    // backgrounded / closed used to stay local until the next visit. Flush
    // it with a keepalive PUT. Fire-and-forget: the response can't be
    // observed, so dirty and the persisted base are NOT touched — if it
    // landed, the next pull sees v+1 and push() finds nothing new to send;
    // if it didn't, the next push 409s and merges as usual. Not sendBeacon
    // (POST only → 405 on /api/state). keepalive bodies are capped at
    // 64 KB — bigger ones stay on the timer path.
    var lastFlushGen = -1;
    function flushOnHide() {
      if (!dirty || pushInFlight || !configured()) return;
      // pagehide and visibilitychange→hidden both fire on a close; one
      // flush per edit generation is enough.
      if (stateGen === lastFlushGen) return;
      var sub = currentSub();
      if (syncBase.sub && syncBase.sub !== sub) return;   // M-041
      adoptDiskSyncBase();                                // M-001
      if (syncBase.sub !== sub) syncBase = emptySyncBase(sub);
      var body = buildBody(randomWriteId());
      var bytes = body.length * 3;
      try { bytes = new TextEncoder().encode(body).length; } catch (_) {}
      if (bytes > 60000) return;
      lastFlushGen = stateGen;
      clearTimeout(pushTimer); pushTimer = null;
      var headers = {'Content-Type': 'application/json'};
      var a = loadAuth();
      if (a && a.id_token) headers['Authorization'] = 'Bearer ' + a.id_token;
      try {
        fetch(API, {method: 'PUT', keepalive: true, credentials: 'include',
                    headers: headers, body: body}).catch(function() {});
      } catch (_) {}
    }
    // True when startSync's initial pull/push ran with a live session —
    // tryRestoreSession's success path checks it so a signed-in boot does
    // one state GET, not two. When auth was stale at startSync time (exp
    // passed → pull went local-mode without a request), the restore path
    // still owns the real first pull.
    var bootSyncedAuthed = false;
    function startSync() {
      // Paint the dirty indicator on first load so a flag restored from
      // localStorage shows up before the first push/pull lands.
      updateNeedsSyncIndicator();
      bootSyncedAuthed = !!configured();
      // pull() first, always: it merges (never clobbers) and ends by
      // re-pushing anything dirty, so a boot with an unsent edit still
      // uploads it — after learning what the server has. The old push-first
      // order never pulled while that push kept failing (M-053).
      pull(true);
      clearInterval(pollTimer);
      // C-2: the poll stays at 60 s; freshness between polls comes from the
      // storage / pageshow / online events below, not from a faster timer.
      pollTimer = setInterval(function() {
        if (document.visibilityState !== 'visible') return;
        pull();
      }, 60000);
    }
    document.addEventListener('visibilitychange', function() {
      if (document.visibilityState === 'hidden') { flushOnHide(); return; }
      if (document.visibilityState !== 'visible') return;
      pull();
    });
    window.addEventListener('pagehide', flushOnHide);

    // M-047 / M-124 / M-125 / M-138 / M-147: one reconcile() for every "the
    // world may have moved while we weren't looking" signal — a storage
    // event from another tab (300 ms debounce, our four keys only), a
    // bfcache restore, coming back online. Receive path only: it merges
    // disk → memory and repaints, and NEVER calls schedulePush()/push() —
    // two tabs would otherwise mark each other dirty and double every KV
    // write. Whatever it merged in is uploaded by the tab that made the
    // edit, by the next boot (the disk dirty flag), or by this tab's next
    // own push / poll (afterPullSettled → contentMatchesBase).
    var WATCHED_KEYS = [CACHE_KEY, BM_KEY, SYNC_BASE_KEY, AUTH_KEY];
    var lastSeenSub = currentSub();
    var reconcileTimer = null;
    function reconcile() {
      var changed = false;
      // Auth changed in another tab (M-124): a sign-out used to drop this
      // tab into local mode silently. A sign-in / switch is picked up by
      // the next pull (its M-041 branch handles a different account).
      var sub = currentSub();
      if (sub !== lastSeenSub) {
        var was = lastSeenSub;
        lastSeenSub = sub;
        try { refreshAuthUI(); } catch (_) {}
        if (!sub) setStatus('你在另一个窗口退出了登录', 'err');
        else if (was) setStatus('你在另一个窗口切换了登录', 'err');
        else setStatus('你在另一个窗口登录了', '');
      }
      // Favorites / blacklist: same three-way rule as saveCache, no write.
      var disk = loadCache();
      if (disk.fav && disk.black) {
        var base = lastWrittenCache;
        var untouched = !!base && sameStrList(disk.fav, base.fav)
                     && sameStrList(disk.black, base.black)
                     && !!disk.dirty === !!base.dirty;
        if (!untouched) {
          var fav   = mergeSets(base ? base.fav   || [] : [], state.fav,   disk.fav);
          var black = mergeSets(base ? base.black || [] : [], state.black, disk.black);
          if (!setEqualsList(state.fav,   fav))   { state.fav   = fav;   changed = true; }
          if (!setEqualsList(state.black, black)) { state.black = black; changed = true; }
          lastWrittenCache = {fav: disk.fav.slice(), black: disk.black.slice(),
                              dirty: !!disk.dirty};
        }
      }
      if (mergeDiskBookmarksIntoMemory()) changed = true;
      var adopted = adoptDiskSyncBase();   // repaints by itself when it applies
      if (changed && !adopted) refreshAllMarkers();
      updateNeedsSyncIndicator();
    }
    window.addEventListener('storage', function(e) {
      var k = e ? e.key : null;   // null key = localStorage.clear()
      if (k !== null && WATCHED_KEYS.indexOf(k) < 0) return;
      clearTimeout(reconcileTimer);
      reconcileTimer = setTimeout(reconcile, 300);
    });
    // bfcache restore (M-125): JS memory comes back exactly as it was, but
    // another tab may have written localStorage meanwhile.
    window.addEventListener('pageshow', function(e) {
      if (!e || !e.persisted) return;
      reconcile();
      pull();
    });
    // Back online (M-147): one jittered, de-duplicated pull instead of
    // waiting out the rest of the poll interval.
    var onlineTimer = null;
    window.addEventListener('online', function() {
      if (onlineTimer) return;
      onlineTimer = setTimeout(function() {
        onlineTimer = null;
        pull();
      }, 300 + Math.floor(Math.random() * 1200));
    });

    function toggleFav(url) {
      if (state.fav.has(url)) state.fav.delete(url); else state.fav.add(url);
      schedulePush();
    }
    function toggleBlack(url) {
      if (state.black.has(url)) state.black.delete(url); else state.black.add(url);
      schedulePush();
    }

    // Currently-active result from the search box. Held outside makeIcon so
    // the icon HTML can pick up the highlight on (re)creation; recompute()
    // also calls syncHighlight() at the end to flip between the real
    // highlighted marker and the off-cluster ghost.
    var highlightedRow = null;
    var ghostMarker = null;

    // No hard border — price color is a radial-gradient halo behind the
    // emoji. Fav/black state shown via a small corner badge instead.
    function makeIcon(d) {
      var highlighted = (d === highlightedRow);
      var color = highlighted ? '#2563eb' : (BUCKET_COLOR[d.bucket] || '#9ca3af');
      var cat   = d.categories && d.categories[0];
      var emoji = (cat && GENRE_EMOJI[cat]) || '🍽️';
      var size = 36;
      var opacity = 1.0;
      var badge = '';
      if (isBlack(d)) {
        opacity = 0.4;
        badge = '<span style="position:absolute;top:0;right:2px;font-size:11px;' +
                'line-height:1;color:#dc2626;text-shadow:0 0 2px #fff;">✕</span>';
      } else if (isFav(d)) {
        badge = '<span style="position:absolute;top:0;right:2px;font-size:11px;' +
                'line-height:1;text-shadow:0 0 2px #fff;">⭐</span>';
      }
      var pulse = highlighted
        ? '<div class="mk-pulse-ring"></div>'
        : '';
      var html = '<div style="position:relative;width:' + size + 'px;height:' + size + 'px;' +
                 'display:flex;align-items:center;justify-content:center;' +
                 'opacity:' + opacity + ';">' +
                 pulse +
                 // Tighter halo than before (fade-out ends at 75% instead of
                 // filling the whole 36px box) — with hundreds of markers in
                 // a city view the fat fuzzy discs bled into each other and
                 // into the transit layer. No drop-shadow on the emoji: the
                 // halo already provides the contrast backdrop, and the
                 // filter forced per-marker intermediate-surface compositing
                 // during pans.
                 '<div style="position:absolute;inset:0;border-radius:50%;' +
                 'background:radial-gradient(circle closest-side, ' +
                 color + 'E6 0%, ' + color + '99 42%, ' + color + '00 75%);"></div>' +
                 emojiImg(emoji, 'position:relative;width:16px;height:16px;') +
                 badge +
                 '</div>';
      return L.divIcon({className: '', html: html,
                        iconSize: [size, size],
                        iconAnchor: [size / 2, size / 2]});
    }

    // Ghost icon — stands in for a restaurant that's currently filtered out
    // (or off-viewport from the cluster's perspective). Same blue pulse so
    // the user can find it, but a flat gray halo with no emoji, so it reads
    // as "not really on the map right now".
    function makeGhostIcon() {
      var size = 36;
      var html = '<div style="position:relative;width:' + size + 'px;height:' + size + 'px;">' +
                 '<div class="mk-pulse-ring"></div>' +
                 '<div style="position:absolute;inset:6px;border-radius:50%;' +
                 'background:radial-gradient(circle closest-side, ' +
                 '#9ca3afEE 0%, #9ca3afAA 50%, #9ca3af00 100%);"></div>' +
                 '</div>';
      return L.divIcon({className: '', html: html,
                        iconSize: [size, size],
                        iconAnchor: [size / 2, size / 2]});
    }

    // ---- Highlight state-machine ----
    // setHighlight(d): mark d as the active result from the search box.
    //   - If d passes the current filter and has a marker on the cluster:
    //       repaint its icon with the highlighted halo.
    //   - Otherwise: drop a ghost marker at d's coords and show the banner.
    // clearHighlight(): undo everything (used when the sheet closes).
    // syncHighlight(): recompute() calls this so the marker/ghost state
    //   tracks live filter changes — toggling a cuisine off while the sheet
    //   is open swaps the real marker → ghost, and vice versa.
    var bsBanner = null;  // bound on initMap; null-safe everywhere.
    function setHighlight(d) {
      if (highlightedRow === d) { syncHighlight(); return; }
      // Clear previous: repaint old marker (if still cached) to drop halo,
      // tear down any ghost marker that was standing in for it.
      var prev = highlightedRow;
      highlightedRow = null;
      if (prev && prev._m) prev._m.setIcon(makeIcon(prev));
      removeGhostMarker();
      highlightedRow = d;
      syncHighlight();
    }
    function clearHighlight() {
      if (!highlightedRow) return;
      var prev = highlightedRow;
      highlightedRow = null;
      if (prev && prev._m) prev._m.setIcon(makeIcon(prev));
      removeGhostMarker();
      if (bsBanner) bsBanner.hidden = true;
    }
    function removeGhostMarker() {
      if (ghostMarker) { map.removeLayer(ghostMarker); ghostMarker = null; }
    }
    function syncHighlight() {
      if (!highlightedRow) {
        removeGhostMarker();
        if (bsBanner) bsBanner.hidden = true;
        return;
      }
      var d = highlightedRow;
      var visible = passesFilter(d);
      if (visible) {
        // Real marker exists (or will exist if d is in viewport). If
        // already on the map, repaint with the highlighted variant.
        removeGhostMarker();
        if (d._m) d._m.setIcon(makeIcon(d));
        if (bsBanner) bsBanner.hidden = true;
      } else {
        // Filtered out — surface a ghost at the coords so the user can see
        // *where* the place is, plus a banner explaining why it's grayed.
        if (!ghostMarker) {
          ghostMarker = L.marker([d.lat, d.lon], {
            icon: makeGhostIcon(),
            interactive: false,
            keyboard: false,
          }).addTo(map);
        } else {
          ghostMarker.setLatLng([d.lat, d.lon]);
        }
        if (bsBanner) {
          bsBanner.hidden = false;
          bsBanner.textContent = localizeText(
            '🔍 此餐厅当前不在筛选范围内 — 调整左侧筛选条件可让它出现在地图上');
        }
      }
    }

    function syncFavButton(btn, d) {
      var label = btn.querySelector('.ff-fav-label');
      if (!label) return;
      // M-089: the on/off state was carried by background colour alone —
      // invisible to a screen reader and to anyone who can't tell #fef3c7
      // from #f9fafb (WCAG 1.4.1).
      btn.setAttribute('aria-pressed', isFav(d) ? 'true' : 'false');
      if (isFav(d)) {
        label.textContent = '⭐ 已收藏';
        btn.style.background = '#fef3c7';
        btn.style.borderColor = '#fbbf24';
      } else {
        label.textContent = '☆ 收藏';
        btn.style.background = '#f9fafb';
        btn.style.borderColor = '#d1d5db';
      }
    }
    function syncBlackButton(btn, d) {
      var label = btn.querySelector('.ff-black-label');
      if (!label) return;
      btn.setAttribute('aria-pressed', isBlack(d) ? 'true' : 'false');  // M-089
      if (isBlack(d)) {
        label.textContent = '✕ 已弃用';
        btn.style.background = '#fee2e2';
        btn.style.borderColor = '#dc2626';
      } else {
        label.textContent = '🚫 弃用';
        btn.style.background = '#f9fafb';
        btn.style.borderColor = '#d1d5db';
      }
    }

    // ===== Bottom-sheet popup =====
    // Replaces Leaflet's bindPopup. The sheet docks to the bottom of the
    // viewport, never spills off-screen, and scrolls internally. On wide
    // screens it caps at 440px width and stays bottom-centered.
    var bsBackdrop = document.getElementById('bs-backdrop');
    var bsSheet    = document.getElementById('bs-sheet');
    var bsContent  = document.getElementById('bs-content');
    var bsGrip     = document.getElementById('bs-grip');
    var bsActive   = null;

    // Per-field translate buttons in the detail card. Uses the unofficial
    // translate.googleapis.com "gtx" endpoint — CORS-open, no key, returns
    // a nested-array form whose first element is the segment list.
    function txTargetLangCode() {
      if (activeLang === 'zh-TW') return 'zh-TW';
      if (activeLang === 'en')    return 'en';
      return 'zh-CN';
    }
    function googleTranslate(text, sl, tl) {
      var url = 'https://translate.googleapis.com/translate_a/single' +
                '?client=gtx&sl=' + encodeURIComponent(sl || 'auto') +
                '&tl=' + encodeURIComponent(tl) +
                '&dt=t&q=' + encodeURIComponent(text);
      return fetch(url).then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function(j) {
        var segs = (j && j[0]) || [];
        return segs.map(function(s) { return s && s[0] ? s[0] : ''; }).join('');
      });
    }
    function googleTranslateJa(text) {
      return googleTranslate(text, 'ja', txTargetLangCode());
    }
    // ----- Wikidata lookup for bookmark/attraction names -----
    // Try ja → en → zh Wikipedia for the user's name string, resolve to a
    // wikibase_item Q-ID, then pull the multilingual labels from Wikidata.
    // Returns null on miss (no Wikipedia page, disambig page, or coord
    // drift > 5 km from the pin); on hit returns {sc, tc, jp, en} where
    // any field may be ''. Both Wikipedia and Wikidata are CORS-open with
    // origin=* in the query string, so no proxy needed.
    function bmHaversineM(lat1, lon1, lat2, lon2) {
      var R = 6371000;
      var p1 = lat1 * Math.PI / 180, p2 = lat2 * Math.PI / 180;
      var dphi = (lat2 - lat1) * Math.PI / 180;
      var dlam = (lon2 - lon1) * Math.PI / 180;
      var a = Math.sin(dphi / 2) * Math.sin(dphi / 2) +
              Math.cos(p1) * Math.cos(p2) *
              Math.sin(dlam / 2) * Math.sin(dlam / 2);
      return 2 * R * Math.asin(Math.sqrt(a));
    }
    function wikipediaToQid(title, lang) {
      var url = 'https://' + lang + '.wikipedia.org/w/api.php' +
                '?action=query&titles=' + encodeURIComponent(title) +
                '&prop=pageprops%7Ccoordinates' +
                '&redirects=1&formatversion=2&format=json&origin=*';
      return fetch(url).then(function(r) {
        if (!r.ok) throw new Error('wiki HTTP ' + r.status);
        return r.json();
      }).then(function(j) {
        var pages = (j && j.query && j.query.pages) || [];
        if (!pages.length) return null;
        var p = pages[0];
        if (p.missing) return null;
        var pp = p.pageprops || {};
        if ('disambiguation' in pp) return null;
        var qid = pp.wikibase_item;
        if (!qid) return null;
        var c = (p.coordinates && p.coordinates[0]) || null;
        return {qid: qid, lat: c ? c.lat : null, lon: c ? c.lon : null};
      });
    }
    function wikidataLabels(qid) {
      var url = 'https://www.wikidata.org/w/api.php' +
                '?action=wbgetentities&ids=' + encodeURIComponent(qid) +
                '&props=labels&format=json&origin=*';
      return fetch(url).then(function(r) {
        if (!r.ok) throw new Error('wikidata HTTP ' + r.status);
        return r.json();
      }).then(function(j) {
        var ent = (j && j.entities && j.entities[qid]) || {};
        var raw = ent.labels || {};
        function lab(k) { return (raw[k] && raw[k].value) || ''; }
        return {
          sc: lab('zh-hans') || lab('zh-cn') || '',
          tc: lab('zh-hant') || lab('zh-tw') || lab('zh-hk') || '',
          jp: lab('ja'),
          en: lab('en')
        };
      });
    }
    function wikidataLookup(name, lat, lon) {
      // Returns Promise<null | {sc, tc, jp, en}>. Never rejects — all
      // network errors fold into null so commitBookmark can save with
      // empty translation fields (display falls back to name_src).
      if (!name) return Promise.resolve(null);
      var langs = ['ja', 'en', 'zh'];
      var i = 0;
      function tryNext() {
        if (i >= langs.length) return Promise.resolve(null);
        var lang = langs[i++];
        return wikipediaToQid(name, lang)
          .catch(function() { return null; })
          .then(function(r) { return r || tryNext(); });
      }
      return tryNext().then(function(qinfo) {
        if (!qinfo) return null;
        if (qinfo.lat != null && qinfo.lon != null
            && lat != null && lon != null) {
          if (bmHaversineM(lat, lon, qinfo.lat, qinfo.lon) > 5000) {
            return null;
          }
        }
        return wikidataLabels(qinfo.qid).catch(function() { return null; });
      });
    }
    // Helper: button labels go through the runtime localizer so they
    // pick up the active language without us hardcoding 翻譯 / Translate /
    // 翻訳 / 原文 / Original / 原文. localizeText is a no-op when there's
    // no I18N_MAP (i.e. zh-CN), which is exactly what we want there.
    function setTxBtnLabel(btn, label) {
      btn.textContent = (typeof localizeText === 'function')
        ? localizeText(label)
        : label;
    }
    bsContent.addEventListener('click', function(e) {
      var btn = e.target.closest && e.target.closest('.rst-tx-btn');
      if (!btn || btn.disabled) return;
      e.preventDefault();
      e.stopPropagation();
      var row = btn.parentNode;
      var valueEl = row && row.querySelector('.rst-value');
      if (!valueEl) return;
      var jaEl = valueEl.querySelector('[lang="ja"]');
      var target = jaEl || (valueEl.getAttribute('lang') === 'ja' ? valueEl : valueEl);

      // Toggle back to original if this button is already in translated state
      if (btn.dataset.state === 'translated') {
        var orig = btn.dataset.origText;
        if (orig !== undefined) {
          target.textContent = orig;
          if (btn.dataset.origLang) target.setAttribute('lang', btn.dataset.origLang);
        }
        btn.dataset.state = '';
        setTxBtnLabel(btn, '翻译');
        return;
      }

      // First click on this button — fetch translation
      var text = (target.textContent || '').trim();
      if (!text || text === '—') return;
      btn.disabled = true;
      btn.textContent = '…';
      googleTranslateJa(text).then(function(out) {
        if (!out) throw new Error('empty');
        // Cache so the next click restores cleanly.
        btn.dataset.origText = text;
        btn.dataset.origLang = target.getAttribute('lang') || '';
        target.textContent = out;
        if (target.getAttribute('lang') === 'ja') target.removeAttribute('lang');
        btn.disabled = false;
        btn.dataset.state = 'translated';
        setTxBtnLabel(btn, '原文');
      }).catch(function(err) {
        console.warn('[tabelog] translate failed:', err);
        btn.disabled = false;
        // Visible failure: the button used to just flicker "…" and snap
        // back, which read as "nothing happened" (notably for users where
        // the unofficial gtx endpoint is blocked). Show why for a couple
        // of seconds, then restore the affordance.
        setTxBtnLabel(btn, '翻译失败');
        setTimeout(function() {
          if (btn.isConnected && btn.dataset.state !== 'translated') {
            setTxBtnLabel(btn, '翻译');
          }
        }, 2200);
      });
    });

    // Bind the banner element so the highlight helpers (declared earlier
    // in this initMap closure) can toggle it.
    bsBanner = document.getElementById('bs-banner');

    // M-014: every path that opens a card centres the restaurant in the
    // *geometric* middle of the viewport (flyTo/setView), and then the card
    // slides up over the bottom 75-85% of the screen — so the marker the
    // card describes is usually hidden behind the card. Measured on real
    // card heights this happened in 4 of 4 viewports, desktop 1440x900
    // included. Fix: after the sheet has its final height, pan the marker
    // into the middle of the strip of map that is still visible above it.
    //
    // Deliberately measures offsetHeight, not getBoundingClientRect().top:
    // the sheet is bottom-anchored and slides in with a transform, so its
    // laid-out height is already final while the transition is still
    // running — no need to wait for transitionend.
    //
    // Note: panBy fires moveend, which (as before) refreshes the persisted
    // tabelog.mapView and schedules a marker recompute. That is the
    // existing behaviour of every other programmatic pan on this page.
    var bsPanTimer = 0;
    var bsClearTimer = 0;   // M-141: post-close DOM teardown
    function keepSelectionVisible() {
      var d = bsActive;
      if (!d || typeof d.lat !== 'number' || typeof d.lon !== 'number') return;
      var container = map.getContainer();
      if (!container) return;
      var mrect = container.getBoundingClientRect();
      var sheetTop = window.innerHeight - bsSheet.offsetHeight;
      var band = sheetTop - mrect.top;          // visible map above the card
      // Under ~140px there is no room worth panning into — moving the map
      // would only trade one hidden marker for a cramped sliver.
      if (band < 140) return;
      var target = band / 2;
      var dy = map.latLngToContainerPoint([d.lat, d.lon]).y - target;
      if (Math.abs(dy) < 24) return;            // already where we want it
      var reduce = window.matchMedia
                && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      map.panBy([0, dy], {animate: !reduce, duration: 0.3});
    }
    // Coalesces the calls from openSheet + paint() (which land in the same
    // tick on the fast path) into one pan, and lets the async
    // popups.json paint issue a second, corrective one once the card grows
    // from the placeholder to its real height.
    function scheduleKeepSelectionVisible() {
      clearTimeout(bsPanTimer);
      bsPanTimer = setTimeout(keepSelectionVisible, 60);
    }

    function openSheet(d, opts) {
      var peek = !!(opts && opts.peek);
      setHighlight(d);
      // Mutual exclusion with the filter sheet — both dock to the bottom.
      // ffSheet exists once the filter UI has booted; marker clicks can't
      // fire earlier, so the guard is for paranoia, not for races.
      var ffs = document.getElementById('ff-sheet');
      if (ffs && ffs.classList.contains('ff-open')) {
        ffs.classList.remove('ff-open');
        document.getElementById('ff-backdrop').classList.remove('ff-open');
        ffs.setAttribute('aria-hidden', 'true');
        var ffb = document.getElementById('ff-fab');
        if (ffb) ffb.hidden = false;
      }
      bsSheet.classList.toggle('bs-peek', peek);
      bsActive = d;
      // Reflect the selection in the top search box (title mode — the
      // user's typed query survives in ssQuery and comes back when the
      // sheet closes). The × button stays visible (via .has-text)
      // regardless of whether the dropdown is open, so the user has a
      // one-click "deselect" affordance even when the sheet is collapsed
      // to peek and they've panned the map around.
      if (ssInput) {
        ssInput.value = d.name || '';
        ssWrap.classList.add('has-text');
        ssTitleMode = true;
      }
      // Hide the filter FAB so the bottom-left corner stays clean while
      // the restaurant card occupies the bottom slot.
      var ffbtn = document.getElementById('ff-fab');
      if (ffbtn) ffbtn.hidden = true;
      function paint(html) {
        // Guard: user may have closed the sheet or opened another one while
        // the popups.json fetch was in flight.
        if (bsActive !== d) return;
        bsContent.innerHTML = html;
        var favBtn   = bsContent.querySelector('.ff-fav-btn');
        var blackBtn = bsContent.querySelector('.ff-black-btn');
        if (favBtn)   syncFavButton(favBtn, d);
        if (blackBtn) syncBlackButton(blackBtn, d);
        bsContent.scrollTop = 0;
        // M-014: the card's height is what decides how much map is left, so
        // re-check after every repaint (placeholder -> real card grows it).
        scheduleKeepSelectionVisible();
      }
      if (popupsMap) {
        paint(renderPopup(d, popupsMap[d.detail_url]));
      } else {
        // First marker tap on this page load — show a minimal placeholder
        // (name + rating) while popups.json is on the wire. Usually <500ms.
        var name = (d.name || '').replace(/[&<>]/g, function(c) {
          return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
        });
        paint('<div class="rst-card"><div class="rst-header">' +
              '<div class="rst-title"><span lang="ja">' + name + '</span>' +
              '<span class="rst-rating">★' + (d.rating == null ? '–' : d.rating) +
              '</span></div></div>' +
              '<div style="margin-top:10px;color:#6b7280;font-size:13px;">加载中…</div>' +
              '</div>');
        loadPopups().then(function(map) { paint(renderPopup(d, map[d.detail_url])); })
                    .catch(function() {
                      paint('<div class="rst-card"><div class="rst-title"><span lang="ja">' + name +
                            '</span></div><div style="margin-top:10px;color:#dc2626;">加载失败，请检查网络</div></div>');
                    });
      }
      // Force reflow so the transition runs even on rapid reopen.
      void bsSheet.getBoundingClientRect();
      bsSheet.classList.add('bs-open');
      bsBackdrop.classList.add('bs-open');
      bsSheet.setAttribute('aria-hidden', 'false');
      scheduleKeepSelectionVisible();   // M-014
    }
    function closeSheet() {
      bsSheet.classList.remove('bs-open');
      // Leave .bs-peek in place during the slide-out so the photos/info
      // section doesn't flash into view mid-animation. The next openSheet
      // call resets the class explicitly via toggle(.., peek).
      bsBackdrop.classList.remove('bs-open');
      bsSheet.setAttribute('aria-hidden', 'true');
      bsActive = null;
      // Release the search-nav pin so the next pan can reap the marker.
      pinnedRow = null;
      clearHighlight();
      // Swap the selected-restaurant title out of the search box. If the
      // user was mid-search when they opened the card, their query comes
      // back — browsing several candidates used to mean retyping it after
      // every card. When the input holds a live query the user typed after
      // opening the card (not title mode), leave it entirely alone. × still
      // wipes everything via ssExitSearch.
      if (ssInput && ssTitleMode) {
        ssTitleMode = false;
        if (ssQuery) {
          ssInput.value = ssQuery;
          ssWrap.classList.add('has-text');
        } else {
          ssInput.value = '';
          ssWrap.classList.remove('has-text');
        }
      }
      var ffbtn = document.getElementById('ff-fab');
      if (ffbtn) ffbtn.hidden = false;
      // M-014: nothing to pan into view any more.
      clearTimeout(bsPanTimer);
      // M-141: the sheet only slides out of view — its DOM stayed, so a
      // photo whose request never resolved kept its shimmer running (and
      // the whole card kept holding memory) after the card was dismissed.
      // Drop the content once the slide-out has finished; the bsActive
      // guard makes a re-open inside those 300ms a no-op.
      clearTimeout(bsClearTimer);
      bsClearTimer = setTimeout(function() {
        if (!bsActive) bsContent.innerHTML = '';
      }, 300);
    }
    // Close the bottom sheet when the card's × is tapped — mirrors the
    // search box's clear control and the bs-grip swipe-to-dismiss. Delegated
    // because the card HTML is re-rendered on every marker click.
    document.addEventListener('click', function(e) {
      if (e.target.closest('.rst-close')) closeSheet();
    });
    function expandSheet() {
      if (bsSheet.classList.contains('bs-peek')) {
        bsSheet.classList.remove('bs-peek');
        // M-014: peek -> full is the single biggest height jump the card
        // makes, so this is where the marker most often disappears behind
        // it. Removing .bs-peek re-shows the photo grid; there is no height
        // transition to wait for, but rAF lets layout settle first.
        requestAnimationFrame(scheduleKeepSelectionVisible);
      }
    }
    // Backdrop is inert (no pointer events) — this listener is dead in
    // practice, kept only because tearing out the wiring is more risk than
    // benefit. The actual "tap outside the sheet" path runs through
    // map.on('click') below.
    bsBackdrop.addEventListener('click', closeSheet);
    document.addEventListener('keydown', function(e){
      // Same staged dismiss as the swipe-down gesture: Full → Peek → Closed.
      if (e.key !== 'Escape' || !bsActive) return;
      if (bsSheet.classList.contains('bs-peek')) closeSheet();
      else bsSheet.classList.add('bs-peek');
    });

    // Grip drag handler. Downward swipe always dismisses the sheet (80px or
    // fast flick). When the sheet is in peek mode, upward swipe expands it
    // (smaller threshold so a small tug already promotes), and a near-zero
    // movement is treated as a tap-to-expand.
    var bsDrag = null;
    function bsDragStart(e) {
      var p = e.touches ? e.touches[0] : e;
      bsDrag = { y0: p.clientY, t0: Date.now() };
      bsSheet.style.transition = 'none';
    }
    function bsDragMove(e) {
      if (!bsDrag) return;
      var p = e.touches ? e.touches[0] : e;
      var raw = p.clientY - bsDrag.y0;
      // In peek mode the sheet can pull upward (negative dy) with a rubber-
      // band feel; otherwise only downward motion is visualized.
      var peek = bsSheet.classList.contains('bs-peek');
      var dy;
      if (raw >= 0) dy = raw;
      else if (peek) dy = Math.max(raw / 2, -40);
      else dy = 0;
      var prefix = window.innerWidth >= 700 ? 'translate(-50%, ' + dy + 'px)'
                                            : 'translateY(' + dy + 'px)';
      bsSheet.style.transform = prefix;
    }
    function bsDragEnd(e) {
      if (!bsDrag) return;
      var p = (e.changedTouches && e.changedTouches[0]) || e;
      var dy = p.clientY - bsDrag.y0;
      var dt = Date.now() - bsDrag.t0;
      bsSheet.style.transition = '';
      bsSheet.style.transform = '';
      var peek = bsSheet.classList.contains('bs-peek');
      var downward = (dy > 80 || (dy > 30 && dt < 200));
      var upward   = (dy < -20 || (dy < -5 && dt < 250));
      if (downward) {
        // Two-stage dismiss, Google-Maps-style. First swipe collapses to
        // peek and keeps the restaurant selected (marker stays highlighted,
        // map stays pannable); second swipe deselects + closes.
        if (peek) closeSheet();
        else bsSheet.classList.add('bs-peek');
      } else if (peek && upward) {
        expandSheet();
      } else if (peek && Math.abs(dy) < 5 && dt < 250) {
        // Tap on the grip with no real drag — expand.
        expandSheet();
      }
      bsDrag = null;
    }
    bsGrip.addEventListener('touchstart', bsDragStart, { passive: true });
    bsGrip.addEventListener('touchmove',  bsDragMove,  { passive: true });
    bsGrip.addEventListener('touchend',   bsDragEnd);
    // Mouse listeners live only for the duration of a drag — a permanent
    // document-level mousemove runs on every pointer move for the page's
    // whole life, for a gesture that is rare on desktop.
    function bsMouseUp(e) {
      document.removeEventListener('mousemove', bsDragMove);
      document.removeEventListener('mouseup',   bsMouseUp);
      bsDragEnd(e);
    }
    bsGrip.addEventListener('mousedown', function(e) {
      bsDragStart(e);
      document.addEventListener('mousemove', bsDragMove);
      document.addEventListener('mouseup',   bsMouseUp);
    });

    // Whole-peek-card tap to expand. Clicks on the grip have already been
    // handled by bsDragEnd (which removes .bs-peek before this fires), so
    // we'd skip via the `peek` guard anyway. Buttons / links inside the
    // card get the bubbled event too — we let them do their own work and
    // only expand for "neutral" clicks (title text, ribbons, blank area).
    bsSheet.addEventListener('click', function(e) {
      if (!bsSheet.classList.contains('bs-peek')) return;
      if (e.target.closest('button, a, input, #bs-grip')) return;
      expandSheet();
    });

    // Tap on the map area:
    //   - in Full state → demote to Peek (restaurant stays selected, marker
    //     stays highlighted, just like Google Maps);
    //   - in Peek state → no-op (user explicitly swipes down on the grip
    //     to actually deselect).
    map.on('click', function(){
      if (!bsActive) return;
      if (bsSheet.classList.contains('bs-peek')) return;
      bsSheet.classList.add('bs-peek');
    });

    // ===== Viewport-driven marker construction =====
    // We no longer build all 8000+ L.marker objects up front (that allocated
    // ~30-50MB of heap on boot and pinned an inline divIcon DOM string per
    // row). Instead, restaurants live as plain JS row objects; markers are
    // materialized on first appearance and cached on the row as `d._m`.
    // Pan/zoom triggers a viewport-clipped recompute; the filter UI calls
    // the same recompute path. The cluster only ever sees the subset that
    // (a) intersects the visible bbox and (b) passes the current filter.
    //
    // The 0.1° grid (~11 km cells) is fine enough that even a citywide view
    // walks a handful of cells, not the full row list.
    var GRID = 0.1;
    var gridIndex = new Map();
    var rowByUrl = {};
    for (var di = 0; di < data.length; di++) {
      var d = data[di];
      if (typeof d.lat !== 'number' || typeof d.lon !== 'number') continue;
      if (d.detail_url) rowByUrl[d.detail_url] = d;
      var gx = Math.floor(d.lon / GRID), gy = Math.floor(d.lat / GRID);
      var k = gx + ',' + gy;
      var cell = gridIndex.get(k);
      if (!cell) { cell = []; gridIndex.set(k, cell); }
      cell.push(d);
    }
    var onMap = new Set();  // rows currently added to the cluster
    // Row whose marker is being held in the cluster across an in-flight
    // cluster.zoomToShowLayer animation. Without this, recompute() (which
    // fires on every moveend the zoom-to-show animation steps through) can
    // yank the marker before the cluster has a chance to spiderfy it, and
    // the search "fly to result" path silently fails.
    var pinnedRow = null;

    function visibleRows() {
      var b = map.getBounds().pad(0.25);
      var W = b.getWest(), E = b.getEast(), S = b.getSouth(), N = b.getNorth();
      var gx0 = Math.floor(W / GRID), gx1 = Math.floor(E / GRID);
      var gy0 = Math.floor(S / GRID), gy1 = Math.floor(N / GRID);
      var out = [];
      for (var gx = gx0; gx <= gx1; gx++) {
        for (var gy = gy0; gy <= gy1; gy++) {
          var cell = gridIndex.get(gx + ',' + gy);
          if (!cell) continue;
          for (var i = 0; i < cell.length; i++) {
            var d = cell[i];
            if (d.lon < W || d.lon > E || d.lat < S || d.lat > N) continue;
            out.push(d);
          }
        }
      }
      return out;
    }

    function ensureMarker(d) {
      if (d._m) return d._m;
      var m = L.marker([d.lat, d.lon], {icon: makeIcon(d)});
      m._d = d;
      // Marker tap opens the full detail card directly — a deliberate tap
      // on a marker means "I want to read about this place". The peek
      // entry point is reserved for the search-result flow, where the
      // user is still browsing across results.
      m.on('click', function() { openSheet(d); });
      d._m = m;
      return m;
    }

    function setCountText(cls, n) {
      var nodes = document.querySelectorAll('.' + cls);
      for (var i = 0; i < nodes.length; i++) nodes[i].textContent = n;
    }
    setCountText('ff-total', data.length);

    function updateFavCount() {
      var n = 0;
      for (var i = 0; i < data.length; i++) if (isFav(data[i])) n++;
      document.getElementById('ff-fav-count').textContent = n;
    }
    function updateBlackCount() {
      var n = 0;
      for (var i = 0; i < data.length; i++) if (isBlack(data[i])) n++;
      document.getElementById('ff-black-count').textContent = n;
    }

    // Keep ⭐/🚫 buttons in sync if the sheet is open while the user toggles
    // them from elsewhere (rare; kept for parity with the old popupopen path).
    map.on('popupopen', function(e) {
      var node = e.popup.getElement && e.popup.getElement();
      if (!node) return;
      var favBtn = node.querySelector('.ff-fav-btn');
      var blackBtn = node.querySelector('.ff-black-btn');
      var url = (favBtn || blackBtn) && (favBtn || blackBtn).getAttribute('data-url');
      var d = url && rowByUrl[url];
      if (!d) return;
      if (favBtn)   syncFavButton(favBtn, d);
      if (blackBtn) syncBlackButton(blackBtn, d);
    });

    // One delegated handler for both ⭐ and 🚫 clicks anywhere in the DOM.
    document.addEventListener('click', function(e) {
      var favBtn   = e.target.closest && e.target.closest('.ff-fav-btn');
      var blackBtn = e.target.closest && e.target.closest('.ff-black-btn');
      var btn = favBtn || blackBtn;
      if (!btn) return;
      var d = rowByUrl[btn.getAttribute('data-url')];
      if (!d) return;
      if (favBtn) {
        toggleFav(d.detail_url);
        syncFavButton(favBtn, d);
        updateFavCount();
      } else {
        toggleBlack(d.detail_url);
        syncBlackButton(blackBtn, d);
        updateBlackCount();
      }
      if (d._m) d._m.setIcon(makeIcon(d));
      apply();
    });

    var ratingSlider = document.getElementById('ff-rating');
    var ratingLabel = document.getElementById('ff-rating-val');

    var onlyFavEl = document.getElementById('ff-only-fav');
    var hideBlackEl = document.getElementById('ff-hide-black');
    var hideForeignEl = document.getElementById('ff-hide-foreign');
    var FOREIGN_GENRES = new Set(__DEFAULT_OFF_GENRES__);

    var genreSummaryEl = document.getElementById('ff-genre-summary');
    var genreBoxes = document.querySelectorAll('input[name=ff-genre]');
    function updateGenreSummary() {
      var total = genreBoxes.length;
      var n = 0;
      genreBoxes.forEach(function(c){ if (c.checked) n++; });
      if (!genreSummaryEl) return;
      if (n === total) genreSummaryEl.textContent = '全部';
      else if (n === 0) genreSummaryEl.textContent = '无';
      else genreSummaryEl.textContent = '已选 ' + n + ' / ' + total;
    }

    // Filter inputs are read once into this struct so `recompute()` (called
    // on every pan/zoom moveend) doesn't have to re-touch the DOM. `apply()`
    // is the user-callable side: it refreshes the cache, then recomputes.
    var filterState = {
      minRating: 3.4, pSet: {}, gSet: {}, gAny: false,
      aSet: {}, aAny: false,
      bookableOnly: false, onlyFav: false, hideBlack: true, hideForeign: true,
      gcalOnly: false  // TEMP: Google-calibration filter, remove after full calibration
    };
    function readFilterInputs() {
      filterState.minRating = parseFloat(ratingSlider.value);
      ratingLabel.textContent = filterState.minRating.toFixed(2);
      filterState.pSet = {};
      document.querySelectorAll('input[name=ff-price]:checked').forEach(function(c){ filterState.pSet[c.value]=1; });
      filterState.gSet = {};
      filterState.gAny = false;
      document.querySelectorAll('input[name=ff-genre]:checked').forEach(function(c){ filterState.gSet[c.value]=1; filterState.gAny=true; });
      filterState.aSet = {};
      filterState.aAny = false;
      document.querySelectorAll('input[name=ff-award]:checked').forEach(function(c){ filterState.aSet[c.value]=1; filterState.aAny=true; });
      var bEl = document.getElementById('ff-bookable-only');
      filterState.bookableOnly = bEl ? bEl.checked : false;
      filterState.onlyFav = onlyFavEl.checked;
      filterState.hideBlack = hideBlackEl.checked;
      filterState.hideForeign = hideForeignEl.checked;
      // TEMP: Google-calibration filter.
      var gcEl = document.getElementById('ff-gcal-only');
      filterState.gcalOnly = gcEl ? gcEl.checked : false;
    }
    function passesFilter(d) {
      var fs = filterState;
      // Blacklist short-circuits when "隐藏" is on, regardless of other
      // filters. Hide-blacklist defeats only-fav so a starred-then-blacklisted
      // restaurant still hides — easier mental model.
      if (fs.hideBlack && isBlack(d)) return false;
      // Slider min 3.4 covers the whole dataset, so we only filter when
      // the user actually moves it above the minimum.
      if (fs.minRating > 3.4) {
        if (d.rating == null || d.rating < fs.minRating) return false;
      }
      if (!fs.pSet[d.bucket]) return false;
      // Foreign cuisines (中/韩/西/南亚) bypass the regular genre filter —
      // they're gated entirely by hideForeignEl. When shown, they appear
      // regardless of which Japanese-cuisine boxes are checked.
      var cats = d.categories || [];
      var isForeign = cats.length > 0 && FOREIGN_GENRES.has(cats[0]);
      if (isForeign) {
        if (fs.hideForeign) return false;
      } else {
        // Genre filter: OR across selected categories. If none checked, hide all.
        if (!fs.gAny) return false;
        var ok = false;
        for (var i = 0; i < cats.length; i++) {
          if (fs.gSet[cats[i]]) { ok = true; break; }
        }
        if (!ok) return false;
      }
      if (fs.bookableOnly && !d.bookable) return false;
      if (fs.gcalOnly && !d.gcal) return false;  // TEMP: Google-calibration filter
      if (fs.onlyFav && !isFav(d)) return false;
      // Award filter: OR across checked tags. Inactive when nothing checked —
      // that's the "any award status" state, not "show nothing".
      if (fs.aAny) {
        var awards = d.awards;
        if (!awards || !awards.length) return false;
        var hit = false;
        for (var ai = 0; ai < awards.length; ai++) {
          if (fs.aSet[awards[ai]]) { hit = true; break; }
        }
        if (!hit) return false;
      }
      return true;
    }

    function recompute() {
      var candidates = visibleRows();
      var desired = new Set();
      for (var i = 0; i < candidates.length; i++) {
        var d = candidates[i];
        if (passesFilter(d)) desired.add(d);
      }
      // Diff against the current cluster contents — only add/remove the
      // delta. MarkerCluster's bulk addLayers / removeLayers are much
      // cheaper than clearLayers + rebuild on every pan.
      var removeLayers = [];
      onMap.forEach(function(d) {
        if (d === pinnedRow) return;
        if (!desired.has(d) && d._m) removeLayers.push(d._m);
      });
      if (removeLayers.length) {
        cluster.removeLayers(removeLayers);
        // Drop the cached L.marker references so GC can reclaim the DOM
        // + icon + click-closure for each one (~10KB apiece). Without this
        // step, panning across all of Japan accumulates ~100 MB of ghost
        // markers that aren't on the map anymore. ensureMarker() recreates
        // on re-entry; the cluster rebuilds on add either way.
        for (var ri = 0; ri < removeLayers.length; ri++) {
          if (removeLayers[ri]._d) removeLayers[ri]._d._m = null;
        }
      }
      onMap.forEach(function(d) {
        if (d === pinnedRow) return;
        if (!desired.has(d)) onMap.delete(d);
      });

      var addLayers = [];
      desired.forEach(function(d) {
        if (!onMap.has(d)) {
          addLayers.push(ensureMarker(d));
          onMap.add(d);
        }
      });
      if (addLayers.length) cluster.addLayers(addLayers);

      setCountText('ff-count', desired.size);
      updateEmptyState(desired.size);   // M-028
      // Filter/viewport changed — re-evaluate whether the active highlight
      // should be the cluster marker (visible) or the gray ghost (hidden).
      syncHighlight();
    }

    // rAF-coalesce moveend so a long pan with many fired events still maps
    // to one recompute per frame at most. Same pattern as the transit layer.
    var recomputeRaf = 0;
    function scheduleRecompute() {
      if (recomputeRaf) return;
      recomputeRaf = requestAnimationFrame(function() {
        recomputeRaf = 0;
        recompute();
      });
    }
    map.on('moveend', scheduleRecompute);

    function apply() {
      readFilterInputs();
      updateGenreSummary();
      recompute();
      saveFilterState();
    }

    // ===== Persisted filter state =====
    // saveFilterState runs at the end of every apply(); restoreFilterState
    // runs once before the first apply() to repopulate inputs from the last
    // session. Reset button clears the inputs then calls apply(), which
    // overwrites stored state with defaults — a true reset.
    var STATE_KEY_FILTER = 'tabelog.filterState';
    function saveFilterState() {
      try {
        var prices = [];
        document.querySelectorAll('input[name=ff-price]:checked').forEach(function(c){ prices.push(c.value); });
        var genres = [];
        document.querySelectorAll('input[name=ff-genre]:checked').forEach(function(c){ genres.push(c.value); });
        var awards = [];
        document.querySelectorAll('input[name=ff-award]:checked').forEach(function(c){ awards.push(c.value); });
        // M-016: price/cuisine buckets default to *checked*, so persisting
        // the checked list makes the saved state a closed world — the day
        // map_data.py grows a bucket, every returning user has it silently
        // unchecked and those restaurants vanish from their map for good
        // (apply() writes the truncated list straight back). Persist the
        // complement instead: "everything except what I turned off" stays
        // correct however the bucket list grows. The legacy checked lists
        // are still written so an older build — or an older tab still open
        // on another screen — reads exactly what it always did.
        // Awards are NOT in this list on purpose: they default to
        // *unchecked* ("no award constraint"), so a checked list is already
        // the forward-compatible shape for them — a new award slug should
        // arrive unchecked, and it does.
        var uncheckedPrices = [];
        document.querySelectorAll('input[name=ff-price]:not(:checked)').forEach(function(c){ uncheckedPrices.push(c.value); });
        var uncheckedGenres = [];
        document.querySelectorAll('input[name=ff-genre]:not(:checked)').forEach(function(c){ uncheckedGenres.push(c.value); });
        var bEl = document.getElementById('ff-bookable-only');
        var gcEl = document.getElementById('ff-gcal-only');
        localStorage.setItem(STATE_KEY_FILTER, JSON.stringify({
          rating: parseFloat(ratingSlider.value),
          prices: prices,
          genres: genres,
          uncheckedPrices: uncheckedPrices,
          uncheckedGenres: uncheckedGenres,
          awards: awards,
          bookableOnly: bEl ? bEl.checked : false,
          onlyFav: onlyFavEl.checked,
          hideBlack: hideBlackEl.checked,
          hideForeign: hideForeignEl.checked,
          gcalOnly: gcEl ? gcEl.checked : false
        }));
      } catch (e) {}
    }
    function restoreFilterState() {
      var s;
      try { s = JSON.parse(localStorage.getItem(STATE_KEY_FILTER) || 'null'); }
      catch (e) { return; }
      if (!s) return;
      if (typeof s.rating === 'number') ratingSlider.value = String(s.rating);
      // M-016: prefer the "unchecked" complement written by this build —
      // start from all-checked (the default) and only untick what the user
      // actually turned off, so a bucket that did not exist when the state
      // was saved arrives checked instead of silently hiding its
      // restaurants. Fall back to the legacy checked lists for state
      // written before this build. Unknown values in either list are
      // ignored, and a missing/!Array field just leaves the defaults —
      // nothing here throws on a shape it doesn't recognise.
      if (Array.isArray(s.uncheckedPrices)) {
        var upSet = {};
        s.uncheckedPrices.forEach(function(v){ upSet[v] = 1; });
        document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = !upSet[c.value]; });
      } else if (Array.isArray(s.prices)) {
        var pSet = {};
        s.prices.forEach(function(v){ pSet[v] = 1; });
        document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = !!pSet[c.value]; });
      }
      if (Array.isArray(s.uncheckedGenres)) {
        var ugSet = {};
        s.uncheckedGenres.forEach(function(v){ ugSet[v] = 1; });
        document.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = !ugSet[c.value]; });
      } else if (Array.isArray(s.genres)) {
        var gSet = {};
        s.genres.forEach(function(v){ gSet[v] = 1; });
        document.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = !!gSet[c.value]; });
      }
      if (Array.isArray(s.awards)) {
        var aSet = {};
        s.awards.forEach(function(v){ aSet[v] = 1; });
        document.querySelectorAll('input[name=ff-award]').forEach(function(c){ c.checked = !!aSet[c.value]; });
      }
      var bEl = document.getElementById('ff-bookable-only');
      if (bEl) {
        // Forward-compat with the older 3-radio shape that stored 'yes'/'no'/'all'.
        if (typeof s.bookableOnly === 'boolean') bEl.checked = s.bookableOnly;
        else if (typeof s.bookable === 'string') bEl.checked = (s.bookable === 'yes');
      }
      if (typeof s.onlyFav === 'boolean') onlyFavEl.checked = s.onlyFav;
      if (typeof s.hideBlack === 'boolean') hideBlackEl.checked = s.hideBlack;
      if (typeof s.hideForeign === 'boolean') hideForeignEl.checked = s.hideForeign;
      // TEMP: Google-calibration filter. Absent in older saved state => the
      // checkbox stays unchecked (show everything), so no one loses results.
      var gcEl = document.getElementById('ff-gcal-only');
      if (gcEl && typeof s.gcalOnly === 'boolean') gcEl.checked = s.gcalOnly;
    }

    // Live update on drag — routed through the rAF-coalesced scheduler. A
    // drag emits dozens of input events per second, and running the full
    // apply() (checkbox DOM scan + recompute + localStorage write) on
    // every one was textbook slider jank on dense viewports. Per event we
    // only refresh the in-memory filter state + label (cheap); recompute
    // is capped at one per frame; persistence waits for release.
    ratingSlider.addEventListener('input', function() {
      readFilterInputs();
      scheduleRecompute();
    });
    ratingSlider.addEventListener('change', apply);
    document.querySelectorAll('#ff-sheet input[type=checkbox], #ff-sheet input[type=radio]').forEach(function(el) {
      el.addEventListener('change', apply);
    });
    document.getElementById('ff-price-all').addEventListener('click', function(e) {
      e.preventDefault();
      document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = true; });
      apply();
    });
    document.getElementById('ff-price-none').addEventListener('click', function(e) {
      e.preventDefault();
      document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = false; });
      apply();
    });
    document.getElementById('ff-genre-all').addEventListener('click', function(e) {
      e.preventDefault();
      document.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = true; });
      apply();
    });
    document.getElementById('ff-genre-none').addEventListener('click', function(e) {
      e.preventDefault();
      document.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = false; });
      apply();
    });
    document.getElementById('ff-award-none').addEventListener('click', function(e) {
      e.preventDefault();
      document.querySelectorAll('input[name=ff-award]').forEach(function(c){ c.checked = false; });
      apply();
    });
    document.querySelectorAll('[data-genre-group]').forEach(function(box) {
      box.querySelector('.ff-group-all').addEventListener('click', function(e) {
        e.preventDefault();
        box.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = true; });
        apply();
      });
      box.querySelector('.ff-group-none').addEventListener('click', function(e) {
        e.preventDefault();
        box.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = false; });
        apply();
      });
    });
    // ===== Filter bottom sheet =====
    // Same visual treatment as the restaurant detail sheet (#bs-sheet);
    // mutual exclusion ensures both never occupy the bottom slot at once.
    // The bottom-left #ff-fab is the entry point; the restaurant detail
    // sheet auto-closes the filter sheet when a marker is tapped.
    var ffSheet    = document.getElementById('ff-sheet');
    var ffBackdrop = document.getElementById('ff-backdrop');
    var ffGrip     = document.getElementById('ff-grip');
    var ffFab      = document.getElementById('ff-fab');

    function openFilterSheet() {
      if (bsActive) closeSheet();        // restaurant detail yields to filter
      ffSheet.classList.add('ff-open');
      ffBackdrop.classList.add('ff-open');
      ffSheet.setAttribute('aria-hidden', 'false');
      ffFab.hidden = true;
    }
    function closeFilterSheet() {
      ffSheet.classList.remove('ff-open');
      ffBackdrop.classList.remove('ff-open');
      ffSheet.setAttribute('aria-hidden', 'true');
      ffFab.hidden = false;
    }
    function ffIsOpen() { return ffSheet.classList.contains('ff-open'); }

    ffFab.addEventListener('click', openFilterSheet);
    ffBackdrop.addEventListener('click', closeFilterSheet);
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape' && ffIsOpen()) closeFilterSheet();
    });

    // Reuse the bottom-sheet swipe-down dismiss from the restaurant card.
    function makeSheetDrag(sheet, onClose) {
      var drag = null;
      function start(e) {
        var p = e.touches ? e.touches[0] : e;
        drag = { y0: p.clientY, t0: Date.now() };
        sheet.style.transition = 'none';
      }
      function move(e) {
        if (!drag) return;
        var p = e.touches ? e.touches[0] : e;
        var dy = Math.max(0, p.clientY - drag.y0);
        var prefix = window.innerWidth >= 700 ? 'translate(-50%, ' + dy + 'px)'
                                              : 'translateY(' + dy + 'px)';
        sheet.style.transform = prefix;
      }
      function end(e) {
        if (!drag) return;
        var p = (e.changedTouches && e.changedTouches[0]) || e;
        var dy = p.clientY - drag.y0;
        var dt = Date.now() - drag.t0;
        sheet.style.transition = '';
        sheet.style.transform = '';
        if (dy > 80 || (dy > 30 && dt < 200)) onClose();
        drag = null;
      }
      return { start: start, move: move, end: end };
    }
    var ffDrag = makeSheetDrag(ffSheet, closeFilterSheet);
    ffGrip.addEventListener('touchstart', ffDrag.start, { passive: true });
    ffGrip.addEventListener('touchmove',  ffDrag.move,  { passive: true });
    ffGrip.addEventListener('touchend',   ffDrag.end);
    // Same drag-scoped mouse listeners as the restaurant sheet above.
    function ffMouseUp(e) {
      document.removeEventListener('mousemove', ffDrag.move);
      document.removeEventListener('mouseup',   ffMouseUp);
      ffDrag.end(e);
    }
    ffGrip.addEventListener('mousedown', function(e) {
      ffDrag.start(e);
      document.addEventListener('mousemove', ffDrag.move);
      document.addEventListener('mouseup',   ffMouseUp);
    });

    // Filter reset. Extracted from an inline #ff-reset handler so the avatar
    // dropdown's reset row can call it too — see the menu wiring below.
    function resetFilters() {
      ratingSlider.value = '3.4';
      document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = true; });
      document.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = true; });
      document.querySelectorAll('input[name=ff-award]').forEach(function(c){ c.checked = false; });
      var resetBookable = document.getElementById('ff-bookable-only');
      if (resetBookable) resetBookable.checked = false;
      var resetGcal = document.getElementById('ff-gcal-only');  // TEMP
      if (resetGcal) resetGcal.checked = false;
      onlyFavEl.checked = false;
      hideBlackEl.checked = true;
      hideForeignEl.checked = true;
      apply();
    }

    // ----- Cloud sync settings modal: Google sign-in / sign-out.
    // GIS now renders its sign-in button inline in the avatar dropdown
    // (ssm-signin-btn), with its status feedback in ssm-cfg-msg right below.
    var signinBtnContainer = document.getElementById('ssm-signin-btn');
    var cfgMsg  = document.getElementById('ssm-cfg-msg');

    // GIS callback for the visible sign-in button. Exchanges the Google
    // credential for a Worker session cookie, mirrors the profile into
    // localStorage, then reloads so the rest of the page boots signed in.
    function onGoogleCredential(resp) {
      if (!resp || !resp.credential) {
        cfgMsg.style.color = '#dc2626';
        cfgMsg.textContent = localizeText('登录被取消');  // M-104
        return;
      }
      cfgMsg.style.color = '';
      cfgMsg.textContent = localizeText('登录中') + '…';  // M-104
      exchangeForSession(resp.credential, function(ok, profile) {
        if (!ok || !profile) {
          cfgMsg.style.color = '#dc2626';
          cfgMsg.textContent = localizeText('登录处理失败');  // M-104
          return;
        }
        saveSessionProfile(profile);
        cfgMsg.style.color = '#16a34a';
        cfgMsg.textContent = '✓ ' + localizeText('登录成功') + l10nComma()
                           + localizeText('重新加载') + '…';  // M-104
        setTimeout(function() { location.reload(); }, 600);
      });
    }

    // Silent re-auth via Google One Tap with auto_select. Now used only as a
    // last resort — the cookie session lasts 90 days so most loads skip GIS
    // entirely (see tryRestoreSession's /api/me probe). When this DOES run,
    // a successful re-auth is immediately exchanged for a fresh cookie so
    // the 90-day clock resets.
    // With auto_select + an active Google session in the browser, this
    // completes with no UI at all. Multi-account users see a single One Tap
    // chooser — one click to refresh. FedCM is opted in so the flow survives
    // Chrome's third-party cookie phaseout.
    // Queue of callbacks waiting on the current in-flight attempt. null
    // means no attempt is running. Coalescing matters because pull and
    // push can 401 concurrently — without it the second caller would see
    // silentInFlight, bail with cb(false), and clear the AUTH_KEY that
    // the first caller is about to refresh.
    var silentWaiters = null;
    function silentReAuth(cb) {
      cb = cb || function(){};
      if (!window.google || !google.accounts || !google.accounts.id) {
        cb(false); return;
      }
      if (silentWaiters) { silentWaiters.push(cb); return; }
      silentWaiters = [cb];
      var done = false;
      function finish(ok) {
        if (done) return;
        done = true;
        var waiters = silentWaiters;
        silentWaiters = null;
        waiters.forEach(function(c) { c(ok); });
        // initialize() is global GIS state: our one-shot silent callback is
        // now dead, and if the visible sign-in button was already rendered
        // its clicks would deliver the credential here — the user completes
        // the Google popup and the page visibly does nothing. Hand the
        // callback back to the button.
        if (gisRendered && window.google && google.accounts && google.accounts.id) {
          try {
            google.accounts.id.initialize({
              client_id: GOOGLE_CLIENT_ID,
              callback: onGoogleCredential,
              ux_mode: 'popup',
              auto_select: false
            });
          } catch (_) {}
        }
      }
      google.accounts.id.initialize({
        client_id: GOOGLE_CLIENT_ID,
        callback: function(resp) {
          if (!resp || !resp.credential) { finish(false); return; }
          exchangeForSession(resp.credential, function(ok, profile) {
            if (ok && profile) {
              saveSessionProfile(profile);
              finish(true);
            } else {
              finish(false);
            }
          });
        },
        auto_select: true,
        use_fedcm_for_prompt: true,
        cancel_on_tap_outside: false
      });
      try {
        google.accounts.id.prompt(function(notification) {
          // momentHandler — if the prompt was suppressed (no eligible
          // session, user dismissed, FedCM gate failed), the main callback
          // will never fire. Report failure so the caller can fall back.
          if (notification && (notification.isNotDisplayed
                ? (notification.isNotDisplayed() || notification.isSkippedMoment())
                : false)) {
            finish(false);
          }
        });
      } catch (e) {
        finish(false);
      }
      // Hard timeout so a hung prompt doesn't block forever.
      setTimeout(function() { finish(false); }, 4000);
    }

    // Wait for the GIS script to materialize before invoking it. Used by
    // the rare branches that actually need Google (boot-time fallback +
    // 401 recovery in pull/push). The common boot path doesn't need GIS
    // at all because /api/me works with just the cookie.
    function whenGIS(fn) {
      var n = 0;
      (function tick() {
        if (window.google && window.google.accounts && window.google.accounts.id) {
          fn(); return;
        }
        if (n++ > 30) return;   // ~3s give-up
        setTimeout(tick, 100);
      })();
    }
    function fallbackSilentGIS(cb) {
      cb = cb || function(){};
      whenGIS(function() {
        silentReAuth(function(ok) {
          if (ok) {
            refreshAuthUI();
            // Skip when startSync already pulled with live auth — its own
            // 401 recovery covers the revoked-cookie case, so a second
            // boot GET here would be pure duplication.
            if (!bootSyncedAuthed) { if (dirty) push(); else pull(); }
          }
          cb(ok);
        });
      });
    }

    // Called on page load and after a 401 from sync. Tiered restore:
    //   1) GET /api/me — cheapest path, validates the existing Worker
    //      session cookie without touching Google.
    //   2) If localStorage still has a legacy id_token (page loaded before
    //      cookie sessions existed), POST it to /api/session to upgrade
    //      directly. No One Tap UI either.
    //   3) Last resort: GIS silent One Tap, then exchange the resulting
    //      credential for a fresh cookie.
    function tryRestoreSession(cb) {
      cb = cb || function(){};
      var raw = loadAuth();
      if (!raw.sub && !raw.id_token) { cb(false); return; }

      tryMe(function(ok) {
        if (ok) {
          refreshAuthUI();
          // Only sync from here when startSync couldn't (stale local exp
          // made its boot pull bail to local mode); with live auth at
          // boot, startSync's pull already ran — see bootSyncedAuthed.
          if (!bootSyncedAuthed) { if (dirty) push(); else pull(); }
          cb(true);
          return;
        }
        if (raw.id_token && raw.exp && Date.now() < raw.exp - 60000) {
          exchangeForSession(raw.id_token, function(ok2, p) {
            if (ok2 && p) {
              saveSessionProfile(p);
              refreshAuthUI();
              if (!bootSyncedAuthed) { if (dirty) push(); else pull(); }
              cb(true);
            } else {
              fallbackSilentGIS(cb);
            }
          });
          return;
        }
        fallbackSilentGIS(cb);
      });
    }

    // Boot-time restore — fires immediately, no GIS wait, because the
    // common path is just an /api/me probe. Only the fall-through to
    // fallbackSilentGIS needs Google, and that's wrapped in whenGIS.
    function bootSilentReAuth() {
      tryRestoreSession();
    }
    // Only attempt if we previously had a session; first-time visitors
    // shouldn't see an unsolicited One Tap toast or an /api/me round-trip
    // on page load.
    if (loadAuth().sub || loadAuth().id_token) {
      setTimeout(bootSilentReAuth, 0);
    }

    // Lazy-render Google's official sign-in button into ssm-signin-btn inside
    // the avatar dropdown. Called from refreshAuthUI whenever the signed-out
    // pane is shown — we tolerate the GIS script still loading on first call
    // by retrying up to ~3s before giving up.
    var gisRendered = false;
    var gisRetryTimer = null;
    function renderSignInButton(attempt) {
      attempt = attempt || 0;
      if (gisRendered) return;
      if (!window.google || !google.accounts || !google.accounts.id) {
        if (attempt > 30) {
          cfgMsg.style.color = '#dc2626';
          cfgMsg.textContent = 'Google ' + localizeText('登录脚本未加载')
                             + l10nParen(localizeText('检查网络'));  // M-104
          return;
        }
        clearTimeout(gisRetryTimer);
        gisRetryTimer = setTimeout(function() { renderSignInButton(attempt + 1); }, 100);
        return;
      }
      google.accounts.id.initialize({
        client_id: GOOGLE_CLIENT_ID,
        callback: onGoogleCredential,
        // ux_mode: 'popup' is the default; explicit for clarity. iOS Safari
        // sometimes downgrades to a redirect under stricter privacy settings,
        // which is fine — the callback still fires once we're back.
        ux_mode: 'popup',
        // Allow the GIS lib to remember our consent grant across sessions so
        // we don't have to re-prompt every time the token expires.
        auto_select: false
      });
      // Map our activeLang to Google's locale codes.
      var loc = (activeLang === 'zh-TW') ? 'zh_TW'
              : (activeLang === 'en')    ? 'en'
              : (activeLang === 'ja')    ? 'ja'
              : 'zh_CN';
      // Button text variant: "signin_with" = "Sign in with Google" / 等价物.
      google.accounts.id.renderButton(signinBtnContainer, {
        theme: 'outline',
        size: 'large',
        type: 'standard',
        text: 'signin_with',
        shape: 'rectangular',
        logo_alignment: 'left',
        locale: loc
      });
      gisRendered = true;
    }

    // ===== Avatar dropdown ("ss-avatar" → "ss-menu") =====
    // Single Google-style circular control on the search box's right edge.
    // Click toggles a popover with two states — signed-in: account info +
    // sign-out; signed-out: Google's official inline sign-in button + a
    // help paragraph explaining what signing in does — plus always-visible
    // 重置筛选 and a 4-pill language picker. The old "Cloud sync" modal is
    // gone; sign-in is now one click straight to Google.
    var ssAvatar = document.getElementById('ss-avatar');
    var ssAvatarPic = document.getElementById('ss-avatar-pic');
    var ssMenu = document.getElementById('ss-menu');
    var ssmSignedIn = document.getElementById('ssm-signed-in');
    var ssmSignedOut = document.getElementById('ssm-signed-out');
    var ssmAcctPic = document.getElementById('ssm-acct-pic');
    var ssmAcctName = document.getElementById('ssm-acct-name');
    var ssmAcctEmail = document.getElementById('ssm-acct-email');
    var ssmSigninHelp = document.getElementById('ssm-signin-help');

    // Hand-tune the help paragraph per language so the runtime CJK localizer
    // doesn't fragment "登录后…会跨设备同步" into Saved/Discard/Sights-shaped
    // word salad. zh-CN keeps the HTML source; zh-TW would auto-convert via
    // OpenCC but the hand version reads cleaner.
    (function tuneHelp() {
      if (!ssmSigninHelp) return;
      if (activeLang === 'en') {
        ssmSigninHelp.textContent = 'Once signed in, your Saved / Discard / Sights sync across devices. Otherwise they stay in this browser only.';
      } else if (activeLang === 'ja') {
        ssmSigninHelp.textContent = 'サインインすると、お気に入り / 非表示リスト / 観光スポット が端末間で同期されます。それ以外はこのブラウザ内のみに保存されます。';
      } else if (activeLang === 'zh-TW') {
        ssmSigninHelp.textContent = '登入後，收藏 / 棄用 / 觀光景點 會跨裝置同步。未登入則僅存於目前的瀏覽器。';
      }
    })();

    // Single auth-state refresh — keeps the avatar + the menu pane in sync,
    // and lazily renders Google's sign-in button into ssm-signin-btn the
    // first time the signed-out pane shows (renderSignInButton is internally
    // guarded against double-renders + retries while the GIS script loads).
    var DEFAULT_AVATAR = 'img/default-avatar-v2.png';
    function refreshAuthUI() {
      var a = configured();
      if (a) {
        // .signed-in is informational now (the placeholder is just a PNG);
        // we only flip it when we actually have a profile picture.
        if (a.picture) {
          ssAvatar.classList.add('signed-in');
          ssAvatarPic.src = a.picture;
          ssmAcctPic.src = a.picture;
        } else {
          ssAvatar.classList.remove('signed-in');
          ssAvatarPic.src = DEFAULT_AVATAR;
          ssmAcctPic.src = DEFAULT_AVATAR;
        }
        ssmAcctName.textContent = a.name || '';
        ssmAcctEmail.textContent = a.email || '';
        ssmSignedIn.hidden = false;
        ssmSignedOut.hidden = true;
      } else {
        ssAvatar.classList.remove('signed-in');
        ssAvatarPic.src = DEFAULT_AVATAR;
        ssmSignedIn.hidden = true;
        ssmSignedOut.hidden = false;
        renderSignInButton();
      }
    }
    refreshAuthUI();

    function openAvatarMenu() {
      refreshAuthUI();
      ssMenu.hidden = false;
      ssMenu.classList.add('open');
    }
    function closeAvatarMenu() {
      ssMenu.classList.remove('open');
      ssMenu.hidden = true;
    }
    ssAvatar.addEventListener('click', function(e) {
      e.stopPropagation();
      if (ssMenu.classList.contains('open')) closeAvatarMenu();
      else openAvatarMenu();
    });
    // Outside-click + Escape to dismiss. Clicks inside ssMenu (including on
    // the GIS button iframe) stay open so the sign-in flow isn't interrupted.
    document.addEventListener('click', function(e) {
      if (!ssMenu.classList.contains('open')) return;
      if (ssMenu.contains(e.target) || ssAvatar.contains(e.target)) return;
      closeAvatarMenu();
    });
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape' && ssMenu.classList.contains('open')) closeAvatarMenu();
    });
    document.getElementById('ssm-signout').addEventListener('click', function() {
      if (!confirm(localizeText('退出登录？本设备上的本地缓存会保留，但不再同步到云端。'))) return;
      signOut();
    });
    document.getElementById('ssm-reset').addEventListener('click', function() {
      resetFilters();
      closeAvatarMenu();
    });

    // ===== Export / import (favorites.json) =====
    // Export dumps the exact sync blob — favorites + blacklist + bookmarks —
    // so a backup round-trips losslessly back through import. Import lets the
    // user pick which of the three corpora to take, shows each one's size,
    // and MERGES (set-union for fav/blacklist, id-dedup for bookmarks) so an
    // import can only ever add, never clobber existing state. After a merge
    // we schedulePush() — same path a normal star/blacklist toggle takes —
    // which saves locally and (when signed in) flushes to the Worker/KV.
    function backupBlob() {
      return {
        schema: 1,
        app: 'jpfoodmap',
        favorites: Array.from(state.fav),
        blacklist: Array.from(state.black),
        bookmarks: bookmarks
      };
    }
    function downloadBackup() {
      var text = JSON.stringify(backupBlob(), null, 2);
      var blob = new Blob([text], {type: 'application/json'});
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = 'favorites.json';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
    }
    document.getElementById('ssm-export').addEventListener('click', function() {
      downloadBackup();
      closeAvatarMenu();
    });

    // Accept either a string URL or an object carrying one — older/manual
    // exports might store favorites as {url|detail_url, name}.
    function pickUrl(x) {
      if (typeof x === 'string') return x;
      if (x && typeof x === 'object') return x.url || x.detail_url || '';
      return '';
    }
    // Normalize an arbitrary parsed file into {favorites:[url], blacklist:[url],
    // bookmarks:[obj]}. Tolerates a bare array (treated as favorites) and the
    // legacy {fav, black} cache shape. Returns null if nothing usable.
    function normalizeImport(parsed) {
      if (Array.isArray(parsed)) {
        return {favorites: parsed.map(pickUrl).filter(Boolean), blacklist: [], bookmarks: []};
      }
      if (!parsed || typeof parsed !== 'object') return null;
      var favSrc   = parsed.favorites || parsed.fav   || [];
      var blackSrc = parsed.blacklist || parsed.black || [];
      var bmSrc    = parsed.bookmarks || [];
      if (!Array.isArray(favSrc) && !Array.isArray(blackSrc) && !Array.isArray(bmSrc)) return null;
      return {
        favorites: (Array.isArray(favSrc)   ? favSrc   : []).map(pickUrl).filter(Boolean),
        blacklist: (Array.isArray(blackSrc) ? blackSrc : []).map(pickUrl).filter(Boolean),
        bookmarks: (Array.isArray(bmSrc)    ? bmSrc    : []).filter(function(b) {
          return b && typeof b === 'object';
        })
      };
    }

    var impBackdrop = document.getElementById('imp-backdrop');
    var impModal    = document.getElementById('imp-modal');
    var impFile     = document.getElementById('ssm-import-file');
    var impFavCb    = document.getElementById('imp-fav');
    var impBlackCb  = document.getElementById('imp-black');
    var impBmCb     = document.getElementById('imp-bm');
    var impError    = document.getElementById('imp-error');
    var pendingImport = null;

    function setImpRow(cb, countEl, text, available) {
      countEl.textContent = localizeText(text);
      cb.disabled = !available;
      cb.checked  = available;
    }
    function openImportModal(norm) {
      // Bookmark array carries real pins + metadata-only "hidden" tombstones;
      // only count the real pins, split into 景点 (attraction) vs 书签.
      var realBm = norm.bookmarks.filter(function(b) { return b.category !== 'hidden'; });
      var attrN  = realBm.filter(function(b) { return b.category === 'attraction'; }).length;
      var pinN   = realBm.length - attrN;
      setImpRow(impFavCb,   document.getElementById('imp-fav-n'),
                norm.favorites.length + ' 家餐厅', norm.favorites.length > 0);
      setImpRow(impBlackCb, document.getElementById('imp-black-n'),
                norm.blacklist.length + ' 家餐厅', norm.blacklist.length > 0);
      setImpRow(impBmCb,    document.getElementById('imp-bm-n'),
                attrN + ' 个景点 · ' + pinN + ' 个书签', norm.bookmarks.length > 0);
      impError.textContent = '';
      pendingImport = norm;
      impBackdrop.classList.add('imp-open');
      impModal.classList.add('imp-open');
      impModal.setAttribute('aria-hidden', 'false');
    }
    function closeImportModal() {
      impBackdrop.classList.remove('imp-open');
      impModal.classList.remove('imp-open');
      impModal.setAttribute('aria-hidden', 'true');
      pendingImport = null;
    }

    document.getElementById('ssm-import').addEventListener('click', function() {
      impFile.click();
    });
    impFile.addEventListener('change', function() {
      var file = impFile.files && impFile.files[0];
      impFile.value = '';   // let the same file be re-selected later
      if (!file) return;
      closeAvatarMenu();
      var reader = new FileReader();
      reader.onload = function() {
        var parsed;
        try { parsed = JSON.parse(reader.result); }
        catch (_) { alert(localizeText('无法解析该文件，请确认它是导出的 favorites.json')); return; }
        var norm = normalizeImport(parsed);
        if (!norm) { alert(localizeText('文件格式无法识别')); return; }
        openImportModal(norm);
      };
      reader.onerror = function() { alert(localizeText('读取文件失败')); };
      reader.readAsText(file);
    });

    // M-042 / M-133: the import path walks arbitrary user-supplied JSON. An
    // exception halfway through used to escape into the click handler,
    // leaving the modal open, the layers half-rebuilt and nothing on screen
    // to explain it. Nothing here is atomic, but the damage now stops at a
    // message the user can act on.
    function doImport() {
      try {
        doImportInner();
      } catch (e) {
        console.error('[tabelog] import failed:', e);
        try {
          impError.textContent = localizeText('导入失败，请检查文件内容');
        } catch (_) {}
      }
    }
    function doImportInner() {
      if (!pendingImport) return;
      if (!impFavCb.checked && !impBlackCb.checked && !impBmCb.checked) {
        impError.textContent = localizeText('请至少选择一项');
        return;
      }
      var report = [];
      var changed = false;

      if (impFavCb.checked) {
        var n0 = state.fav.size;
        pendingImport.favorites.forEach(function(u) { state.fav.add(u); });
        var df = state.fav.size - n0;
        report.push('收藏 +' + df);
        if (df) changed = true;
      }
      if (impBlackCb.checked) {
        var b0 = state.black.size;
        pendingImport.blacklist.forEach(function(u) { state.black.add(u); });
        var db = state.black.size - b0;
        report.push('弃用 +' + db);
        if (db) changed = true;
      }
      if (impBmCb.checked) {
        // M-133: a plain {} inherits Object.prototype, so an entry whose id
        // was 'constructor' / 'toString' / '__proto__' read back truthy and
        // was silently skipped as a duplicate. A Set has no such keys.
        var existing = new Set();
        bookmarks.forEach(function(bm) { if (bm && bm.id) existing.add(bm.id); });
        var added = 0, rejected = 0;
        pendingImport.bookmarks.forEach(function(bm) {
          if (!bm.id || existing.has(bm.id)) return;   // dedup; skip id-less junk
          // M-133: coordinates from a hand-edited or foreign export were
          // never checked, so junk pins rode straight up to the cloud.
          // "hidden" tombstones legitimately carry no coordinates at all.
          if (bm.category !== 'hidden') {
            if (!(Number.isFinite(bm.lat) && Math.abs(bm.lat) <= 90 &&
                  Number.isFinite(bm.lon) && Math.abs(bm.lon) <= 180)) {
              rejected++;
              return;
            }
          }
          existing.add(bm.id);
          sanitizeBookmarkEmoji(bm);
          bookmarks.push(bm);
          added++;
        });
        report.push('书签 +' + added);
        if (rejected) report.push('已跳过 ' + rejected);
        if (added) {
          changed = true;
          // Full rebuild of both bookmark layers from the merged array —
          // mirrors the pull() path so freshly-merged "hidden" tombstones
          // re-hide their builtins correctly (a piecemeal render wouldn't).
          bookmarksLayer.clearLayers();
          userAttractionsLayer.clearLayers();
          bmMarkerById = {};
          rebuildHiddenIds();
          bookmarks.forEach(function(bm) { renderBookmark(bm); });
          renderFavoritesBuiltin();
          saveBookmarks();
        }
      }

      refreshAllMarkers();
      if (changed) schedulePush();
      closeImportModal();
      alert(localizeText(
        changed ? ('导入完成：' + report.join('，')) : '没有新增内容（全部已存在）'));
    }

    impModal.querySelector('.imp-confirm').addEventListener('click', doImport);
    impModal.querySelector('.imp-cancel').addEventListener('click', closeImportModal);
    impModal.querySelector('.imp-close').addEventListener('click', closeImportModal);
    impBackdrop.addEventListener('click', closeImportModal);
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape' && impModal.classList.contains('imp-open')) closeImportModal();
    });

    document.querySelectorAll('#ss-menu [data-lang]').forEach(function(b) {
      if (b.dataset.lang === activeLang) b.classList.add('on');
      b.addEventListener('click', function() { setLanguage(b.dataset.lang); });
    });

    // ===== Help popovers ("?" badges next to filter subtitles) =====
    // Triggers carry data-help-for="<key>"; sections inside #ff-help-pop
    // carry the matching data-help-for. One popover element handles every
    // trigger — on each open we unhide the right section and reposition
    // anchored below the clicked badge.
    (function wireHelpTriggers() {
      var pop = document.getElementById('ff-help-pop');
      if (!pop) return;
      var sections = pop.querySelectorAll('.ff-help-section');
      // Per-language full-prose override. The default HTML carries the
      // zh-CN authored text, which the global CJK localizer would
      // otherwise translate run-by-run — fine for short subtitles, but
      // for full sentences it produced "wagashi、cafés、bakeries..." in
      // EN and similar punctuation-flavoured prose elsewhere. We swap
      // the entire textContent up-front for the active language; the
      // localizer's earlier walk-and-substitute output (if any) is
      // overwritten here. zh-CN is the canonical form already in the
      // HTML, so the override is a no-op there.
      var HELP_COPY = __HELP_COPY__;
      sections.forEach(function(section) {
        var key = section.getAttribute('data-help-for');
        var byLang = HELP_COPY && HELP_COPY[key];
        if (!byLang) return;
        var text = byLang[activeLang] || byLang['zh-CN'];
        if (text) section.textContent = text;
      });
      var openFor = null;
      function place(trigger) {
        var r = trigger.getBoundingClientRect();
        // Keep at least 8px off the left edge; cap so the right edge
        // doesn't overflow the viewport on narrow phones.
        var max_w = 260;
        var left = Math.max(8,
                            Math.min(window.innerWidth - max_w - 8, r.left - 4));
        pop.style.left = left + 'px';
        pop.style.top  = (r.bottom + 8) + 'px';
      }
      function hide() {
        pop.classList.remove('ff-help-show');
        pop.hidden = true;
        openFor = null;
      }
      function show(key, trigger) {
        sections.forEach(function(s) {
          s.hidden = s.getAttribute('data-help-for') !== key;
        });
        place(trigger);
        pop.hidden = false;
        // Read offsetWidth to flush the [hidden] removal before the
        // transition class lands — otherwise the fade-in skips.
        void pop.offsetWidth;
        pop.classList.add('ff-help-show');
        openFor = trigger;
      }
      document.querySelectorAll('.ff-help-trigger').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
          e.preventDefault();
          e.stopPropagation();
          var key = btn.getAttribute('data-help-for');
          if (openFor === btn) { hide(); return; }
          show(key, btn);
        });
      });
      // Tap anywhere else dismisses the popover. We listen on the
      // capture phase so the trigger's own stopPropagation still works
      // for its toggle handler above.
      document.addEventListener('click', function(e) {
        if (!openFor) return;
        if (pop.contains(e.target)) return;
        if (e.target === openFor) return;
        hide();
      });
      document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && openFor) hide();
      });
      // The filter sheet scrolls internally; if the user scrolls or the
      // viewport resizes, the absolute pixel anchor we computed is stale.
      // Easiest is to hide on either signal.
      var sheetContent = document.getElementById('ff-sheet-content');
      if (sheetContent) sheetContent.addEventListener('scroll', hide);
      window.addEventListener('resize', hide);
    })();

    // Language picker. activeLang was resolved on boot from ?lang= and
    // localStorage; the avatar dropdown's language pills (wired below) call
    // setLanguage on click. We persist, update the URL (?lang=tw is sticky,
    // default zh-CN drops the param) so deep links carry the language, then
    // reload — caching every text node's pre-conversion value just to support
    // a rare toggle isn't worth the memory.
    function setLanguage(v) {
      try { localStorage.setItem(LANG_KEY, v); } catch (_) {}
      // Carry the open card / typed query across the reload so trying a
      // language doesn't dump the user's place. sessionStorage: per-tab,
      // consumed once on the other side.
      try {
        sessionStorage.setItem('tabelog.langSwitch', JSON.stringify({
          u: (bsActive && bsActive.detail_url) || '',
          q: (ssInput && ssInput.value) || ''
        }));
      } catch (_) {}
      var url = new URL(window.location.href);
      if (v === 'zh-TW') url.searchParams.set('lang', 'tw');
      else if (v === 'en') url.searchParams.set('lang', 'en');
      else if (v === 'ja') url.searchParams.set('lang', 'ja');
      else url.searchParams.delete('lang');
      window.location.assign(url.toString());
    }

    restoreFilterState();
    updateFavCount();
    updateBlackCount();
    apply();
    // Kick off the first pull (or stay in local mode if not configured).
    startSync();
    // Warm the popup payload once boot is idle — nearly every session taps
    // a marker eventually, and the 6MB download used to start only at the
    // first tap (seconds of 加载中… on cold 4G). Idle-scheduled so it
    // never competes with the boot critical path; skipped for users who
    // asked to save data.
    // M-063: 1.3MB transferred (6.4MB decompressed) is a fine trade on a
    // fast link and a bad one on 3g in a Japanese basement, so the warmup
    // now also skips when the connection reports anything below '4g'.
    // Browsers without the Network Information API (Safari/iOS) report
    // nothing at all — treated as fast, since "unknown" there is usually
    // Wi-Fi and the on-tap fallback still works either way.
    var netInfo = navigator.connection || null;
    var netFast = !netInfo || !netInfo.effectiveType || netInfo.effectiveType === '4g';
    if (!(netInfo && netInfo.saveData) && netFast) {
      var warmPopups = function() { loadPopups(); };
      if ('requestIdleCallback' in window) {
        requestIdleCallback(warmPopups, {timeout: 8000});
      } else {
        setTimeout(warmPopups, 4000);
      }
    }
    // Pre-normalize every restaurant name for the search index during
    // idle — rowNameNorm is lazy, so the full ~9800-row NFKC pass used to
    // land entirely on the first keystroke (noticeable jank on phones).
    // Chunked against the idle deadline; pure CPU, so Save-Data doesn't
    // apply.
    var warmNameIdx = 0;
    function warmNames(deadline) {
      var more = function() {
        return !deadline || typeof deadline.timeRemaining !== 'function'
            || deadline.timeRemaining() > 4;
      };
      while (warmNameIdx < data.length && more()) {
        rowNameNorm(data[warmNameIdx++]);
      }
      if (warmNameIdx < data.length) {
        if ('requestIdleCallback' in window) {
          requestIdleCallback(warmNames, {timeout: 5000});
        } else {
          setTimeout(warmNames, 250);
        }
      }
    }
    if ('requestIdleCallback' in window) {
      requestIdleCallback(warmNames, {timeout: 5000});
    } else {
      setTimeout(warmNames, 2500);
    }
    // Restore what a language-switch reload carried over: reopen the card
    // that was on screen, or put the typed query back. openSheet itself
    // writes the restaurant name into the search box, so the query only
    // needs restoring when no card was open.
    try {
      var lsw = JSON.parse(sessionStorage.getItem('tabelog.langSwitch') || 'null');
      sessionStorage.removeItem('tabelog.langSwitch');
      if (lsw && lsw.u) {
        for (var lswI = 0; lswI < data.length; lswI++) {
          if (data[lswI].detail_url === lsw.u) { openSheet(data[lswI]); break; }
        }
      } else if (lsw && lsw.q && ssInput) {
        ssInput.value = lsw.q;
        ssWrap.classList.add('has-text');
      }
    } catch (_) {}
  }
  // M-075: a hung request used to sit on 加载中… forever, because fetch has
  // no timeout of its own. 9s is past the p99 for this 3MB payload on 4G
  // and well short of the browser's own ~2min give-up.
  var BOOT_TIMEOUT_MS = 9000;
  var bootPending = false;
  function boot() {
    function setTotals(text) {
      var nodes = document.querySelectorAll('.ff-total');
      for (var i = 0; i < nodes.length; i++) nodes[i].textContent = text;
    }
    if (bootPending) return;          // double-tap on 重试
    bootPending = true;
    hideBootFailure();
    setTotals(localizeText('加载中…'));
    var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timedOut = false;
    var timer = setTimeout(function() {
      timedOut = true;
      if (ctrl) ctrl.abort();
    }, BOOT_TIMEOUT_MS);
    fetch('data/restaurants.json',
          {cache: 'force-cache', signal: ctrl && ctrl.signal})
      .then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function(data) {
        clearTimeout(timer);
        bootPending = false;
        // M-054: an exception out of initMap is an initialisation bug, not
        // a network failure — saying "check your network" about it (and
        // offering a retry that re-downloads 3MB to hit the same bug) sent
        // people chasing the wrong problem.
        try {
          initMap(data);
        } catch (e) {
          console.error('[tabelog] map initialisation failed:', e);
          setTotals(localizeText('初始化失败'));
          showBootFailure('deps', function() { location.reload(); });
        }
      })
      .catch(function(e) {
        clearTimeout(timer);
        bootPending = false;
        console.error('[tabelog] restaurants.json load failed' +
                      (timedOut ? ' (timeout)' : '') + ':', e);
        setTotals(localizeText('加载失败'));
        // M-075 / M-168: visible, announced, and retryable in place — the
        // old handler wrote "加载失败" into a counter inside a drawer that
        // is collapsed by default.
        showBootFailure('data', boot);
      });
  }
  if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
</script>
"""


def parse_bool(v) -> bool:
    """CSV stores tabelog_bookable as the string 'True'/'False'/''.
    bool('False') would be True, so handle string forms explicitly."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y")


def popup_data(row: dict) -> list:
    # Per-restaurant fields needed to render the popup card client-side.
    # Positional layout, matched in JS renderPopup():
    #   [genre, dinner_upper, lunch_upper, seat, station, address, policy,
    #    photos, ribbons_html]
    # name / rating / bucket / bookable / detail_url are NOT included — they
    # already live in restaurants.json, so the JS reader pulls them from there.
    # Dropping the duplicates + the static HTML scaffold takes popups.json
    # from ~28 MB to ~5 MB.
    def _empty_to_none(v):
        return v if v not in (None, "", "None") else None

    photos = [row.get(f"photo{i}_url") for i in (1, 2, 3)]
    photos = [p for p in photos if p]
    return [
        row.get("genre") or "",
        _empty_to_none(row.get("dinner_upper")),
        _empty_to_none(row.get("lunch_upper")),
        row.get("seat_count") or "",
        row.get("station") or "",
        row.get("address") or "",
        row.get("reservation_policy_chinese") or "",
        photos,
        _award_ribbons_html(row.get("awards") or ""),
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--fillall",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="re-geocode every row (default: only geocode rows "
        "whose lat/lon are missing in CSV)",
    )
    # M-094: --fillall alone still short-circuits on the geocode cache, so a
    # cached negative could never be retried. This flag bypasses the cache
    # read (writes still happen); positive entries are kept if the retry
    # misses, so it can only ever add coordinates, never remove them.
    ap.add_argument(
        "--ignore-cache",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="re-query GSI even for addresses already in "
        "data/cache/geocode_cache.json (use with --fillall to retry old "
        "failures; cached hits are preserved if the retry finds nothing)",
    )
    return ap.parse_args(argv)


def _parse_latlon(row: dict) -> tuple[float, float] | None:
    try:
        lat, lon = float(row.get("lat") or ""), float(row.get("lon") or "")
    except (TypeError, ValueError):
        return None
    return lat, lon


def fan_out_coincident(
    rows: list[dict],
    *,
    zoom: int = 19,
    marker_px: int = 36,
    margin_px: float = 6.0,
    precision: int = 6,
) -> int:
    """Spread restaurants that share a single lat/lon into a small ring.

    Some addresses (e.g. "東京駅" with no further detail) geocode to the same
    point for many restaurants. At max zoom the 36-px icons stack into one
    blob. Group by coords rounded to ~11 cm and, for each group of ≥2, lay
    the icons out on a ring sized so they just don't overlap at `zoom`.

    Returns the number of rows nudged (for the build log)."""
    if not rows:
        return 0
    groups: dict[tuple[float, float], list[int]] = {}
    for i, r in enumerate(rows):
        groups.setdefault(
            (round(r["lat"], precision), round(r["lon"], precision)), []
        ).append(i)
    nudged = 0
    for (lat0, lon0), idxs in groups.items():
        n = len(idxs)
        if n < 2:
            continue
        # Pack n equal circles of diameter marker_px on a ring; chord between
        # neighbors = 2R·sin(π/n) ≥ marker_px, so R ≥ marker_px / (2 sin π/n).
        # The 18-px floor covers n=2, where the formula collapses to R=18.
        r_px = max(18.0, marker_px / (2 * math.sin(math.pi / n))) + margin_px
        mpp = 40075016.686 * math.cos(math.radians(lat0)) / (256 * 2**zoom)
        r_m = r_px * mpp
        dlat = r_m / 111320.0
        dlon = r_m / (111320.0 * max(math.cos(math.radians(lat0)), 1e-6))
        # Deterministic start angle keyed off the shared coord so rebuilds
        # produce the same arrangement and same-coord groups in different
        # cities don't all align identically.
        start = ((lat0 * 1000.0 + lon0 * 1000.0) % 1.0) * 2 * math.pi
        for k, idx in enumerate(idxs):
            angle = start + 2 * math.pi * k / n
            rows[idx]["lat"] = lat0 + dlat * math.sin(angle)
            rows[idx]["lon"] = lon0 + dlon * math.cos(angle)
        nudged += n
    return nudged


# M-020: geocode-cache checkpoint interval, in rows.
SAVE_CACHE_EVERY = 200
# M-094: how many rows may fall out of the published payload before the build
# is considered broken. Floor + percentage so a small corpus isn't tripped by
# a single failure and a big one isn't allowed to quietly lose hundreds.
MAX_DROPPED_ROWS_FLOOR = 10
MAX_DROPPED_ROWS_PCT = 0.2


def print_corpus_age(rows: list[dict]) -> None:
    """M-096: how old is the scraped corpus? The master CSV's mtime says
    'today' because map.py writes lat/lon back into it every build, which is
    exactly the misreading this quantile print exists to prevent."""
    stamps: list[float] = []
    now = time.time()
    for r in rows:
        raw = (r.get("scraped_at") or "").strip()
        if not raw:
            continue
        try:
            ts = calendar.timegm(time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        stamps.append(max(0.0, (now - ts) / 86400.0))
    n_missing = len(rows) - len(stamps)
    if not stamps:
        print(
            f"  corpus age: no scraped_at timestamps yet "
            f"({n_missing} rows predate the column; they will fill in on the "
            f"next scrape of their region)"
        )
        return
    stamps.sort()

    def q(p: float) -> float:
        return stamps[min(len(stamps) - 1, int(p * len(stamps)))]

    print(
        f"  corpus age (days since scrape): p50 {q(0.5):.0f}  p90 {q(0.9):.0f}  "
        f"max {stamps[-1]:.0f}  ({len(stamps)} stamped, {n_missing} unstamped)"
    )


def write_dropped_report(
    failed: list[dict], no_address: list[dict], total_rows: int
) -> None:
    """M-094 / M-019: write docs/data/dropped.json and exit non-zero when too
    many rows were omitted from the published payload."""
    dropped = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_csv_rows": total_rows,
        "geocode_failed": [
            {
                "name": r.get("name") or "",
                "detail_url": r.get("detail_url") or "",
                "address": r.get("address") or "",
                "region": r.get("region") or "",
            }
            for r in failed
        ],
        "no_address": [
            {
                "name": r.get("name") or "",
                "detail_url": r.get("detail_url") or "",
                "region": r.get("region") or "",
            }
            for r in no_address
        ],
    }
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(DROPPED_JSON, dropped, separators=(",", ":"))
    n_dropped = len(failed) + len(no_address)
    limit = max(MAX_DROPPED_ROWS_FLOOR, round(total_rows * MAX_DROPPED_ROWS_PCT / 100))
    print(
        f"  dropped.json:     {n_dropped} rows omitted from the payload "
        f"({len(failed)} un-geocodable, {len(no_address)} without an address; "
        f"limit {limit})"
    )
    if n_dropped > limit:
        raise SystemExit(
            f"\nREFUSING to publish: {n_dropped} of {total_rows} rows would be "
            f"missing from restaurants.json, over the limit of {limit}. See "
            f"{DROPPED_JSON} for the list. Either GSI is failing (retry with "
            f"--ignore-cache) or a scrape wrote address-less rows. docs/ was "
            f"NOT modified."
        )


def write_csv_with_coords(rows: list[dict], fieldnames: list[str]) -> None:
    # M-020: the master CSV is gitignored — an interrupted truncating write
    # here had no undo path. tmp + fsync + rename, and keep one .prev.
    atomic_write_csv(
        CSV_PATH, rows, fieldnames, extrasaction="ignore", keep_prev=True
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    carto_api_key = os.environ.get("CARTO_BASEMAP_API_KEY", "").strip()
    if not carto_api_key:
        raise SystemExit(
            "CARTO_BASEMAP_API_KEY is not set. Add it to the project-root .env file."
        )
    carto_tile_url = (
        "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/"
        f"{{z}}/{{x}}/{{y}}.png?key={quote(carto_api_key, safe='')}"
    )

    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        all_rows = list(reader)
    for k in ("lat", "lon"):
        if k not in fieldnames:
            fieldnames.append(k)
        for r in all_rows:
            r.setdefault(k, "")

    addr_rows = [r for r in all_rows if r.get("address")]
    print(
        f"{len(addr_rows)} rows with non-empty address "
        f"(of {len(all_rows)} total){'  [fillall mode]' if args.fillall else ''}"
    )
    print_corpus_age(all_rows)  # M-096

    cache = load_cache()
    geocoded: list[tuple[dict, dict]] = []
    failed: list[dict] = []
    n_skipped = 0
    client = httpx.Client(headers={"User-Agent": "omakase-tabelog-mapper/0.1"})
    for i, row in enumerate(addr_rows, 1):
        addr = row["address"]
        if not args.fillall:
            existing = _parse_latlon(row)
            if existing is not None:
                lat, lon = existing
                geocoded.append(
                    (
                        row,
                        {"lat": lat, "lon": lon, "matched_query": addr, "display": ""},
                    )
                )
                n_skipped += 1
                continue
        loc = geocode(addr, client, cache, ignore_cache=args.ignore_cache)
        if loc:
            row["lat"] = loc["lat"]
            row["lon"] = loc["lon"]
            geocoded.append((row, loc))
            print(
                f"  [{i}/{len(addr_rows)}] {row.get('name')!r} -> "
                f"({loc['lat']:.4f}, {loc['lon']:.4f})"
            )
        else:
            failed.append(row)
            print(f"  [{i}/{len(addr_rows)}] {row.get('name')!r}: NO MATCH ({addr!r})")
        # M-020: was every 5 rows — ~1,960 full rewrites of a 2.4 MB file per
        # --fillall run (≈4.7 GB of redundant IO). The write is atomic now, so
        # a wider checkpoint interval costs at most 200 re-queries on a crash.
        if i % SAVE_CACHE_EVERY == 0:
            save_cache(cache)
    save_cache(cache)

    write_csv_with_coords(all_rows, fieldnames)
    print(f"\nWrote lat/lon back to {CSV_PATH.name}")
    if not args.fillall:
        print(f"Skipped {n_skipped} rows that already had coords")
    print(f"Geocoded {len(geocoded)} / {len(addr_rows)}; failed {len(failed)}")
    if failed:
        print("Failed:")
        for f in failed:
            print(f"  - {f.get('name')!r}: {f.get('address')!r}")
    # M-094 / M-019: publish the omission list next to restaurants.json and
    # refuse to build when it grows past the threshold. Before this, rows that
    # fell out of the map were three lines in a 10,000-line build log.
    no_addr_rows = [r for r in all_rows if not r.get("address")]
    write_dropped_report(failed, no_addr_rows, len(all_rows))

    # CartoDB Voyager as the single base — clean Google-Maps-style. The
    # transit option lives in custom JS as a togglable OpenRailwayMap
    # overlay (rail lines + station markers drawn on top), wired to the
    # floating "🚇 公共交通" pill button (see FAB_HTML / initMap).
    m = folium.Map(location=JAPAN_CENTER, zoom_start=6, tiles=None, zoom_control=False)
    folium.TileLayer(
        tiles=carto_tile_url,
        attr=(
            '&copy; <a href="https://www.openstreetmap.org/copyright">'
            "OpenStreetMap</a> contributors &copy; "
            '<a href="https://carto.com/attributions">CARTO</a>'
        ),
        name="公路 (CartoDB Voyager)",
        max_zoom=19,
        subdomains="abcd",
        # Defer tile requests until a zoom gesture ends — during pinch /
        # double-tap zoom, Leaflet shows scaled existing tiles instead of
        # firing intermediate requests. Matches Google/Apple Maps default
        # behaviour and cuts baseband radio time on mobile.
        update_when_zooming=False,
        # Keep 4 rings of off-screen tiles in the DOM (default 2) so a
        # short pan back over already-seen ground doesn't re-fetch.
        keep_buffer=4,
    ).add_to(m)

    # Empty MarkerCluster only to pull in plugin JS/CSS; markers are built
    # client-side in JS so the filter panel can re-cluster on the fly.
    MarkerCluster(name="_assets", control=False).add_to(m)

    # Landmark layer is rendered client-side from EMBEDDED_FAVORITES_BUILTIN
    # (baked from data/favorites_builtin.json) plus any user pins from
    # localStorage / cloud sync — see the bookmarks block in FILTER_JS_TEMPLATE.
    # No folium-side FeatureGroup any more, so the FAB has only the JS
    # layers to toggle.

    # No LayerControl — replaced by the floating FAB stack (FAB_HTML).

    # Build the per-restaurant JSON payload the filter JS consumes. We split
    # into two files: a small "core" payload (everything the marker and the
    # filter UI need, fetched on boot) and a fat popups map (rendered HTML for
    # the bottom sheet, fetched lazily on first marker click). The core
    # payload used to be inlined into index.html — splitting it out drops the
    # HTML from ~25 MB to ~50 KB and lets the browser parse / paint before
    # the popups have downloaded.
    fav_set = load_favorites()
    black_set = load_blacklist()
    # Google-calibrated coords + bilingual address, keyed by detail_url
    # (accepted matches only). Overrides the GSI coord / Tabelog address in the
    # rendered payload; the source CSVs are left untouched.
    gcal_map = load_google_places()
    core_rows: list[dict] = []
    popups_map: dict[str, str] = {}
    cat_counts: dict[str, int] = {cat: 0 for cat in GENRE_CATEGORIES}
    award_counts: dict[str, int] = {slug: 0 for slug, _, _ in AWARD_TAGS}
    unmapped_tokens: set[str] = set()
    for row, loc in geocoded:
        bkey, _blabel, _bcolor = price_bucket(row)
        try:
            rating_num = (
                float(row.get("rating"))
                if row.get("rating") not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            rating_num = None
        url = row.get("detail_url") or ""
        cats = categorize_genre(row.get("genre") or "")
        for cat in cats:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        # Track tokens that fell through to "其他" so we notice when Tabelog
        # adds a new genre label that deserves its own bucket.
        for tok in (
            t.strip()
            for t in _GENRE_SPLIT_RE.split(row.get("genre") or "")
            if t.strip()
        ):
            if tok not in _GENRE_TO_CAT:
                unmapped_tokens.add(tok)
        awards = parse_awards(row.get("awards") or "")
        for tag in awards:
            award_counts[tag] = award_counts.get(tag, 0) + 1
        # color, emoji and tooltip are derived in JS now: color from bucket
        # via BUCKET_COLOR lookup, emoji from categories[0] via GENRE_EMOJI,
        # and the tooltip was actually dead weight (never read by the JS).
        entry = {
            "lat": loc["lat"],
            "lon": loc["lon"],
            "name": row.get("name") or "",
            "rating": rating_num,
            "bucket": bkey,
            "categories": cats,
            "bookable": parse_bool(row.get("tabelog_bookable")),
            "detail_url": url,
            "favorited": url in fav_set,
            "blacklisted": url in black_set,
        }
        # Keep payload lean: only attach `awards` when there is at least one
        # tag. JS treats absent / undefined the same as an empty array.
        if awards:
            entry["awards"] = awards
        # Google calibration: swap in the more-precise POI coords and flag the
        # row so the card shows the "verified" note and the filter can match.
        gcal = gcal_map.get(url)
        if gcal:
            entry["lat"] = gcal["lat"]
            entry["lon"] = gcal["lon"]
            entry["gcal"] = 1
            # place_id is the one Google field the ToS lets us store; the card's
            # Google Maps button uses it to deep-link straight to the place page.
            if gcal["place_id"]:
                entry["gpid"] = gcal["place_id"]
        # Pre-canonicalized location string (prefecture + city + ward) for
        # the "name location" search syntax. Skipped when the address has no
        # extractable admin prefix — JS treats absent as "won't match any
        # location query," which is the correct behavior.
        addr_raw = row.get("address") or ""
        loc_norm = canon_str(parse_admin_prefix(addr_raw))
        if loc_norm:
            entry["loc_norm"] = loc_norm
        city = extract_city(addr_raw)
        if city:
            entry["city"] = city
        core_rows.append(entry)
        if url:
            parr = popup_data(row)
            # SC / TW / JA popups all show the Japanese address; for calibrated
            # rows use Google's normalized 日本語 form (slot 5). The EN popup
            # gets Google's English address via en_popup_array below.
            if gcal and gcal["addr_ja"]:
                parr[5] = gcal["addr_ja"]
            popups_map[url] = parr
    if unmapped_tokens:
        print(f"  unmapped genre tokens (fell into 其他): {sorted(unmapped_tokens)}")
    n_fav = sum(1 for p in core_rows if p["favorited"])
    n_black = sum(1 for p in core_rows if p["blacklisted"])
    print(
        f"  favorites: {n_fav} from favorites.json, blacklist: {n_black} from blacklist.json"
    )

    nudged = fan_out_coincident(core_rows)
    if nudged:
        print(f"  fanned out {nudged} markers sharing identical coords")

    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    restaurants_bytes = json.dumps(
        core_rows, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    popups_bytes = json.dumps(
        popups_map, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    # M-020: publish artefacts are replaced one atomic rename at a time, so
    # a crash mid-build leaves the previous complete file in place instead of
    # a truncated JSON the deployed page would fail to parse. (Full staging of
    # the whole docs/ tree is a bigger refactor; not in 2.0.0.)
    atomic_write_bytes(RESTAURANTS_JSON, restaurants_bytes)
    atomic_write_bytes(POPUPS_JSON, popups_bytes)
    popups_tw_map = {url: trad_popup_array(arr) for url, arr in popups_map.items()}
    popups_tw_bytes = json.dumps(
        popups_tw_map, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    atomic_write_bytes(POPUPS_TW_JSON, popups_tw_bytes)  # M-020
    policy_en = load_policy_en()
    popups_en_map = {
        url: en_popup_array(
            arr, policy_en.get(url), gcal_map.get(url, {}).get("addr_en")
        )
        for url, arr in popups_map.items()
    }
    popups_en_bytes = json.dumps(
        popups_en_map, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    atomic_write_bytes(POPUPS_EN_JSON, popups_en_bytes)  # M-020
    translated_count = sum(1 for url in popups_map if policy_en.get(url, "").strip())
    policy_ja = load_policy_ja()
    popups_ja_map = {
        url: ja_popup_array(arr, policy_ja.get(url)) for url, arr in popups_map.items()
    }
    popups_ja_bytes = json.dumps(
        popups_ja_map, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    atomic_write_bytes(POPUPS_JA_JSON, popups_ja_bytes)  # M-020
    ja_policy_count = sum(1 for url in popups_map if policy_ja.get(url, "").strip())
    print(
        f"  restaurants.json: {len(restaurants_bytes):,} bytes ({len(core_rows)} rows)"
    )
    print(
        f"  popups.json:      {len(popups_bytes):,} bytes ({len(popups_map)} entries)"
    )
    print(f"  popups-tw.json:   {len(popups_tw_bytes):,} bytes")
    print(
        f"  popups-en.json:   {len(popups_en_bytes):,} bytes "
        f"({translated_count}/{len(popups_map)} policies translated)"
    )
    print(
        f"  popups-ja.json:   {len(popups_ja_bytes):,} bytes "
        f"({ja_policy_count}/{len(popups_map)} original JA policies)"
    )

    gcal_count = sum(1 for e in core_rows if e.get("gcal"))
    panel_html = build_filter_panel_html(cat_counts, award_counts, gcal_count)
    default_off_json = json.dumps(sorted(DEFAULT_OFF_GENRES), ensure_ascii=False)
    bookmarks_json = json.dumps(load_bookmarks(), ensure_ascii=False)
    favorites_builtin_json = json.dumps(load_favorites_builtin(), ensure_ascii=False)
    bucket_colors_json = json.dumps(
        {key: color for key, _label, color, _lo, _hi in PRICE_BUCKETS}
    )
    genre_emoji_json = json.dumps(GENRE_EMOJI, ensure_ascii=False)
    # Apple-style PNG cache built by build_emoji_cache.py. Inlined as the
    # EMOJI_MAP lookup the JS uses to decide local-path vs. emojicdn fallback.
    # Missing manifest is non-fatal: every emoji falls back to the runtime CDN,
    # which is the pre-cache behaviour.
    emoji_manifest_path = DOCS_DIR / "emoji" / "_manifest.json"
    if emoji_manifest_path.exists():
        emoji_manifest_json = emoji_manifest_path.read_text(encoding="utf-8")
    else:
        emoji_manifest_json = "{}"
        print("  ⚠ docs/emoji/_manifest.json missing — "
              "run build_emoji_cache.py to pre-cache emoji PNGs")
    # Variant → canonical (simplified Chinese) per-char table for the search
    # box. Only includes variants of chars that actually appear in some
    # restaurant name (canonical form), so the JSON stays compact.
    canon_set: set[str] = set()
    for row in core_rows:
        for c in canon_str(row.get("name", "")):
            canon_set.add(c)
    han_variants = build_han_variants(canon_set)
    han_variants_json = json.dumps(han_variants, ensure_ascii=False)
    print(
        f"  han_variants:     {len(han_variants)} char mappings, "
        f"{len(han_variants_json.encode('utf-8')):,} bytes"
    )
    # Known location whitelist for the search box. The trailing query token
    # is only treated as a location filter when it canonicalizes into this
    # set — otherwise the whole query stays a single restaurant-name match.
    known_locs: set[str] = set()
    for row, _loc in geocoded:
        for tok in admin_tokens_with_suffix(row.get("address") or ""):
            cn = canon_str(tok)
            if cn:
                known_locs.add(cn)
    known_locs_json = json.dumps(sorted(known_locs), ensure_ascii=False)
    print(
        f"  known_locs:       {len(known_locs)} tokens, "
        f"{len(known_locs_json.encode('utf-8')):,} bytes"
    )
    filter_js = (
        FILTER_JS_TEMPLATE.replace("__DEFAULT_OFF_GENRES__", default_off_json)
        .replace("__BOOKMARKS__", bookmarks_json)
        .replace("__FAVORITES_BUILTIN__", favorites_builtin_json)
        .replace("__BUCKET_COLORS__", bucket_colors_json)
        .replace("__GENRE_EMOJI__", genre_emoji_json)
        .replace("__EMOJI_MANIFEST__", emoji_manifest_json)
        .replace("__HAN_VARIANTS__", han_variants_json)
        .replace("__KNOWN_LOCS__", known_locs_json)
        .replace("__GOOGLE_CLIENT_ID__", GOOGLE_CLIENT_ID)
        .replace("__HELP_COPY__", build_help_copy_json())
    )
    # Content hashes for the runtime-fetched payloads. The page requests
    # them as  <path>?v=<hash>  and the SW keeps a persistent cache keyed by
    # those URLs — an unchanged file survives every deploy with zero
    # re-download, a changed one misses the cache exactly once.
    def _content_v(b: bytes) -> str:
        return hashlib.md5(b).hexdigest()[:10]

    transit_layer_bytes = (DOCS_DIR / "transit-layer.js").read_bytes()
    data_vers = {
        "data/restaurants.json": _content_v(restaurants_bytes),
        "data/popups.json": _content_v(popups_bytes),
        "data/popups-tw.json": _content_v(popups_tw_bytes),
        "data/popups-en.json": _content_v(popups_en_bytes),
        "data/popups-ja.json": _content_v(popups_ja_bytes),
        "transit-layer.js": _content_v(transit_layer_bytes),
    }
    all_versioned_urls = [f"{path}?v={v}" for path, v in data_vers.items()]
    # M-062: popups is one ~6.4MB file per UI language, and the language is
    # only known at runtime — precaching a build-time guess (zh-CN) made
    # every en/ja/tw visitor download a variant they will never open. The
    # page now postMessages the variant it actually wants once activeLang is
    # resolved. all_versioned_urls (activate's GC allowlist) must keep ALL
    # popups variants, or a user who has switched languages loses the copy
    # they are still using.
    precache_urls = [
        f"data/restaurants.json?v={data_vers['data/restaurants.json']}",
        f"transit-layer.js?v={data_vers['transit-layer.js']}",
    ]

    # Service worker — the shell cache is stamped per build; the data and
    # CDN caches persist and are managed by content hash. Unix seconds is
    # plenty granular for a personal site rebuilt by hand.
    build_version = str(int(time.time()))
    atomic_write_text(  # M-020
        SW_JS,
        SW_JS_TEMPLATE.replace("__BUILD_VERSION__", build_version)
        .replace("__MANIFEST_V__", MANIFEST_VERSION)  # M-150
        .replace("__PRECACHE_URLS__", json.dumps(precache_urls))
        .replace("__ALL_VERSIONED_URLS__", json.dumps(all_versioned_urls)),
    )
    print(f"  sw.js:            build {build_version}")
    m.get_root().header.add_child(folium.Element(HEAD_BRANDING))
    m.get_root().header.add_child(folium.Element(LOCATE_ASSETS))
    m.get_root().header.add_child(folium.Element(MOBILE_UX_ASSETS))
    m.get_root().html.add_child(folium.Element(BOTTOM_SHEET_HTML))
    m.get_root().html.add_child(folium.Element(MAP_FAB_HTML))
    m.get_root().html.add_child(folium.Element(SEARCH_BOX_HTML))
    m.get_root().html.add_child(folium.Element(BOOKMARKS_MODAL_HTML))
    m.get_root().html.add_child(folium.Element(HELP_POPOVER_HTML))
    # M-033/M-034/M-028/M-089: sync banners, toasts, empty-state cards.
    # Added after the search box so its CSS (avatar badge, #ff-count.is-zero)
    # wins the tie against the earlier blocks it decorates.
    m.get_root().html.add_child(folium.Element(SYNC_UI_HTML))
    m.get_root().html.add_child(folium.Element(panel_html))
    m.get_root().html.add_child(folium.Element(filter_js))

    # M-020: folium writes with a plain open('w'), and the old code then
    # rewrote the same path a second time after the post-processing passes —
    # two truncation windows over the file Cloudflare Pages actually serves.
    # Render into a scratch file outside the repo (docs/ and data/ are both
    # inside the Dropbox tree, whose watcher can hold a fresh file open long
    # enough to make the cleanup unlink fail on WSL/DrvFs) and replace
    # docs/index.html exactly once, atomically, at the end.
    scratch_dir = Path(tempfile.mkdtemp(prefix="tabelog-build-"))
    html_scratch = scratch_dir / "index.html"
    try:
        m.save(str(html_scratch))
        saved_html = html_scratch.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)

    # Folium's base template unconditionally injects jQuery, Bootstrap,
    # FontAwesome, and Leaflet.awesome-markers into <head>. This page uses
    # none of them — markers are hand-built divIcons, the UI is plain CSS,
    # there's no Bootstrap or jQuery code anywhere. One of the URLs
    # (netdna.bootstrapcdn.com) is on a CDN that stopped serving, so every
    # visit was paying for a hung request that eventually 404s/times out.
    # Strip the lot by URL substring after folium has rendered. Scope the
    # match to actual <script>/<link> tags so a stray mention of the URL
    # inside a CSS comment, docstring, or data attribute isn't eaten.
    DEAD_DEP_MARKERS = (
        "code.jquery.com",
        "bootstrap@5.2.2",
        "Leaflet.awesome-markers",
        "bootstrap-glyphicons",
        "@fortawesome/fontawesome-free",
        "leaflet.awesome.rotate",
    )
    stripped_lines = []
    stripped_count = 0
    for line in saved_html.splitlines(keepends=True):
        stripped_line = line.lstrip()
        is_tag = stripped_line.startswith("<script") or stripped_line.startswith("<link")
        if is_tag and any(marker in line for marker in DEAD_DEP_MARKERS):
            stripped_count += 1
            continue
        stripped_lines.append(line)
    saved_html = "".join(stripped_lines)
    print(f"  stripped {stripped_count} folium-injected dead-dep lines")

    # Let installed iOS/Android web apps use the full screen while exposing
    # safe-area insets to the fixed controls and bottom sheets.
    #
    # M-018: folium's default viewport string also carries
    # `maximum-scale=1.0, user-scalable=no`, and this replacement used to
    # keep both. Blocking page zoom is an accessibility violation on its own,
    # and it bites hardest here because the site is used as an installed PWA
    # — no address bar, no ⋮ menu, so a user who needs bigger text has no way
    # back. Both are dropped; the map's own pinch/ctrl+wheel handling now
    # lives on .leaflet-container (MOBILE_UX_ASSETS), and the 16px input
    # rules were freed from the 480px breakpoint first (M-081) so iOS focus
    # zoom can't kick in as a side effect.
    VIEWPORT_FROM = "initial-scale=1.0, maximum-scale=1.0, user-scalable=no"
    VIEWPORT_TO = "initial-scale=1.0, viewport-fit=cover"
    viewport_hits = saved_html.count(VIEWPORT_FROM)
    if viewport_hits == 1:
        saved_html = saved_html.replace(VIEWPORT_FROM, VIEWPORT_TO)
        print("  viewport: dropped user-scalable=no, added viewport-fit=cover")
    else:
        print(f"  WARNING: expected one viewport meta tag, found {viewport_hits}")

    # M-083: folium emits a bare <html>, so the default (zh-CN) page — which
    # is what nearly every visitor gets — had no lang at all: screen readers
    # picked whatever their default voice was, and whole-page translation had
    # nothing to key off. The runtime `documentElement.lang = activeLang`
    # keeps en/ja/zh-TW in sync; this stamps the static default so it is
    # right at first parse, before any JS runs.
    # Anchored to the doctype prologue rather than the bare tag: the string
    # "<html>" also shows up inside comments in the injected JS.
    LANG_FROM = "<!DOCTYPE html>\n<html>"
    LANG_TO = '<!DOCTYPE html>\n<html lang="zh-CN">'
    lang_hits = saved_html.count(LANG_FROM)
    if lang_hits == 1:
        saved_html = saved_html.replace(LANG_FROM, LANG_TO)
        print('  root element stamped with lang="zh-CN"')
    else:
        print(f"  WARNING: expected one document prologue, found {lang_hits}")

    # Folium injects `.leaflet-container { font-size: 1rem; }` into its
    # auto-generated <style> block — which sits AFTER any CSS we add via
    # m.get_root().header, so it wins the cascade and inflates Leaflet's
    # attribution and tooltips to 16px. Leaflet's own stylesheet wants
    # 0.75rem (12px). Patch the value at the source instead of fighting
    # the cascade with !important.
    LEAFLET_FONT_PATCH_FROM = ".leaflet-container { font-size: 1rem; }"
    LEAFLET_FONT_PATCH_TO   = ".leaflet-container { font-size: 0.75rem; }"
    if LEAFLET_FONT_PATCH_FROM in saved_html:
        saved_html = saved_html.replace(LEAFLET_FONT_PATCH_FROM, LEAFLET_FONT_PATCH_TO)
        print("  patched folium's .leaflet-container font-size 1rem -> 0.75rem")
    else:
        print("  WARNING: folium .leaflet-container font-size rule not found "
              "to patch — check if folium changed the rule format")

    # Stamp every payload reference in the page with its content hash so
    # the SW's persistent cache is addressed by content: the preload link,
    # boot's restaurants fetch, loadPopups' four candidates, and the
    # transit-layer script tag all carry ?v=. Plain string replace — the
    # URLs appear only as literal references. Count-checked so a template
    # rename can't silently ship an unversioned (never-updating) URL.
    for _path, _v in data_vers.items():
        _hits = saved_html.count(_path)
        if _hits == 0:
            print(f"  WARNING: {_path} not referenced in page — ?v not applied")
            continue
        saved_html = saved_html.replace(_path, f"{_path}?v={_v}")
    print(f"  data URLs stamped with content hashes ({len(data_vers)} files)")

    # Restore the saved viewport BEFORE folium's init script creates the
    # map. The restore inside initMap runs far later (it polls for plugin
    # deps) — by then Leaflet has already requested a full whole-Japan z6
    # tile set that the immediate setView throws away: double tile
    # downloads and a visible jump on every boot. A one-shot L.map wrapper
    # injected right after leaflet.js feeds the saved center/zoom straight
    # into folium's constructor call; the initMap restore stays as the
    # fallback when this injection is missing.
    VIEW_RESTORE_SNIPPET = (
        "<script>(function(){try{"
        "var v=JSON.parse(localStorage.getItem('tabelog.mapView')||'null');"
        "if(!v||typeof v.lat!=='number'||typeof v.lon!=='number'"
        "||typeof v.zoom!=='number')return;"
        "var o=L.map;"
        "L.map=function(id,opts){L.map=o;"
        "if(opts){opts.center=[v.lat,v.lon];opts.zoom=v.zoom;}"
        "return o.call(L,id,opts);};"
        "}catch(e){}})();</script>"
    )
    leaflet_tag_re = re.compile(r'<script src="[^"]*/leaflet(?:\.min)?\.js"></script>')
    tag_match = leaflet_tag_re.search(saved_html)
    if tag_match:
        saved_html = (
            saved_html[: tag_match.end()]
            + VIEW_RESTORE_SNIPPET
            + saved_html[tag_match.end() :]
        )
        print("  injected saved-view restore after leaflet.js")
    else:
        print("  WARNING: leaflet.js script tag not found — saved-view "
              "restore not injected (boot will show the z6 default first)")

    # Second pass over the saved file: scan every CJK run that ended up
    # on the page (static UI, bucket names, attraction labels, AND the
    # Chinese string literals inside the inlined <script> blocks — those
    # produce text nodes too once the JS that builds them runs), feed
    # each to full OpenCC s2t, and inject the {simp: trad} map. The JS
    # side reads this at runtime to do precise per-segment conversion;
    # we ship the precomputed answers instead of a converter so the
    # browser doesn't need to load OpenCC's dictionaries.
    text_trad_map = build_text_trad_map(saved_html)
    text_trad_map_json = json.dumps(
        text_trad_map, ensure_ascii=False, separators=(",", ":")
    )
    text_en_map, missing_en = build_text_en_map(saved_html)
    text_en_map_json = json.dumps(
        text_en_map, ensure_ascii=False, separators=(",", ":")
    )
    text_ja_map, missing_ja = build_text_ja_map(saved_html)
    text_ja_map_json = json.dumps(
        text_ja_map, ensure_ascii=False, separators=(",", ":")
    )
    saved_html = (
        saved_html.replace("__TEXT_TRAD_MAP__", text_trad_map_json)
        .replace("__TEXT_EN_MAP__", text_en_map_json)
        .replace("__TEXT_JA_MAP__", text_ja_map_json)
    )
    atomic_write_text(OUT_HTML, saved_html)  # M-020: the only write to docs/index.html
    print(f"\nMap written to {OUT_HTML}")
    print(f"  {len(core_rows)} restaurants in payload (fetched at runtime)")
    print(
        f"  text_trad_map:    {len(text_trad_map)} CJK runs, "
        f"{len(text_trad_map_json.encode('utf-8')):,} bytes"
    )
    print(
        f"  text_en_map:      {len(text_en_map)} CJK runs, "
        f"{len(text_en_map_json.encode('utf-8')):,} bytes"
    )
    if missing_en:
        print(
            f"  missing EN translations: {len(missing_en)} runs "
            f"(stay in Chinese at runtime)"
        )
        # Cap the printed list so a fresh i18n dir doesn't drown the log.
        preview = missing_en[:30]
        for run in preview:
            print(f"    - {run!r}")
        if len(missing_en) > len(preview):
            print(f"    ... and {len(missing_en) - len(preview)} more")
    print(
        f"  text_ja_map:      {len(text_ja_map)} CJK runs, "
        f"{len(text_ja_map_json.encode('utf-8')):,} bytes"
    )
    if missing_ja:
        print(
            f"  missing JA translations: {len(missing_ja)} runs "
            f"(stay in Chinese at runtime)"
        )
        preview = missing_ja[:30]
        for run in preview:
            print(f"    - {run!r}")
        if len(missing_ja) > len(preview):
            print(f"    ... and {len(missing_ja) - len(preview)} more")

    # M-056: machine-readable build summary for scripts/verify_build.py. The
    # missing-translation lists are only ever printed truncated, so record the
    # full sets here — that is what makes "did this change add an untranslated
    # string?" answerable without re-running the build.
    atomic_write_json(
        BUILD_REPORT_JSON,
        {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "build_version": build_version,
            "restaurants_rows": len(core_rows),
            "popup_entries": len(popups_map),
            "csv_rows": len(all_rows),
            "geocode_failed": len(failed),
            "missing_en_count": len(missing_en),
            "missing_ja_count": len(missing_ja),
            "missing_en": missing_en,
            "missing_ja": missing_ja,
        },
        indent=2,
    )
    print(f"  build report:     {BUILD_REPORT_JSON}")


if __name__ == "__main__":
    main()
