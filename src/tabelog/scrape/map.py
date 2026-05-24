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
import csv
import html as _html
import json
import math
import re
import sys
import time
from pathlib import Path

import folium
import httpx
from folium.plugins import MarkerCluster

# Force UTF-8 stdout — Windows console defaults to cp1252 and chokes on
# the Japanese restaurant names printed during the geocode pass.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

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
    TABELOG_CSV,
    SW_JS,
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


def en_popup_array(arr: list, policy_en: str | None) -> list:
    """Same shape as the simp popup row, with the policy field swapped
    for the English translation when one is available. Ribbons (index 8)
    are left in Chinese — the runtime localizer rewrites their few CJK
    runs (百名店, 受賞店, etc.) via TEXT_EN_MAP at insertion time, so we
    don't need to pre-process them here."""
    a = list(arr)
    if policy_en and len(a) > 6:
        a[6] = policy_en
    return a


def ja_popup_array(arr: list, policy_ja: str | None) -> list:
    """JA variant — overlay the policy field with the original Japanese
    text from data/tabelog/tabelog.csv `reservation_policy`. Same fallback
    convention as en_popup_array."""
    a = list(arr)
    if policy_ja and len(a) > 6:
        a[6] = policy_ja
    return a


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
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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


def gsi_geocode(query: str, client: httpx.Client) -> dict | None:
    """Hit GSI AddressSearch. Returns the top hit's coords + title, or None."""
    try:
        r = client.get(GSI_URL, params={"q": query}, timeout=30.0)
        r.raise_for_status()
        hits = r.json()
    except Exception as e:
        print(f"  GSI error on {query!r}: {e}")
        return None
    if not hits:
        return None
    top = hits[0]
    geom = (top.get("geometry") or {}).get("coordinates") or []
    if len(geom) != 2:
        return None
    lon, lat = geom  # GSI returns [lon, lat]
    return {
        "lat": float(lat),
        "lon": float(lon),
        "matched_query": query,
        "display": (top.get("properties") or {}).get("title", ""),
    }


def geocode(addr: str, client: httpx.Client, cache: dict) -> dict | None:
    if addr in cache:
        return cache[addr]
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
    for q in candidates:
        if q in seen:
            continue
        seen.add(q)
        loc = gsi_geocode(q, client)
        if loc is not None:
            cache[addr] = loc
            return loc
    cache[addr] = None
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
        f'<span style="color:#9ca3af;font-size:11px;">({award_counts.get(slug, 0)})</span></label>'
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
            f'{cat} <span style="color:#9ca3af;">({cat_counts.get(cat, 0)})</span></label>'
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
    font-size: 13px; line-height: 1.5; color: #111827;
  }}
  /* Bottom-left FAB that opens the sheet. Matches the right-side .map-fab
     style but stands alone — labelled with the live filter count so the
     "how many results match" feedback survives the collapse to a sheet. */
  #ff-fab {{
    position: fixed; bottom: 18px; left: 14px;
    z-index: 9995;
    background: #fff; color: #374151;
    border: 1px solid #d1d5db;
    border-radius: 999px;
    padding: 9px 14px;
    font-size: 13px; font-weight: 600;
    cursor: pointer;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    display: inline-flex; align-items: center; gap: 8px;
    user-select: none;
    transition: background 0.15s ease-out, box-shadow 0.15s ease-out;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    line-height: 1;
  }}
  #ff-fab:hover {{ background: #f9fafb;
                   box-shadow: 0 4px 10px rgba(0,0,0,0.18); }}
  #ff-fab[hidden] {{ display: none; }}
  #ff-fab .ff-fab-ic {{ font-size: 15px; }}
  #ff-fab .ff-fab-count {{ font-variant-numeric: tabular-nums; }}
  #ff-fab .ff-fab-count b {{ color: #2563eb; }}
  /* Unsynced-changes alerts. Two flavours so the colour matches the
     user's actual situation:
       .needs-sync         — red. Not signed in, so edits live only in
                              this browser. Urgent.
       .needs-sync-pending — blue. Signed in; the push just hasn't landed
                              yet (or last attempt failed, but the status
                              text covers that channel). Informational. */
  @keyframes ff-fab-pulse {{
    0%   {{ background: #fee2e2; box-shadow: 0 2px 6px rgba(220,38,38,0.35); }}
    50%  {{ background: #dc2626; box-shadow: 0 4px 14px rgba(220,38,38,0.55); }}
    100% {{ background: #fee2e2; box-shadow: 0 2px 6px rgba(220,38,38,0.35); }}
  }}
  #ff-fab.needs-sync {{
    color: #fff; border-color: #dc2626;
    animation: ff-fab-pulse 1.4s ease-in-out infinite;
  }}
  #ff-fab.needs-sync:hover {{ animation-play-state: paused;
                              background: #dc2626; }}
  #ff-fab.needs-sync .ff-fab-count b {{ color: #fff; }}
  #ff-fab.needs-sync .ff-fab-count {{ color: #fff; }}
  @keyframes ff-fab-pulse-pending {{
    0%   {{ background: #dbeafe; box-shadow: 0 2px 6px rgba(37,99,235,0.35); }}
    50%  {{ background: #2563eb; box-shadow: 0 4px 14px rgba(37,99,235,0.55); }}
    100% {{ background: #dbeafe; box-shadow: 0 2px 6px rgba(37,99,235,0.35); }}
  }}
  #ff-fab.needs-sync-pending {{
    color: #fff; border-color: #2563eb;
    animation: ff-fab-pulse-pending 1.4s ease-in-out infinite;
  }}
  #ff-fab.needs-sync-pending:hover {{ animation-play-state: paused;
                                      background: #2563eb; }}
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
<button id="ff-fab" type="button" aria-label="打开筛选" title="筛选">
  <span class="ff-fab-ic">🎛</span>
  <span class="ff-fab-count"><b class="ff-count">–</b> / <span class="ff-total">–</span></span>
</button>
<div id="ff-backdrop"></div>
<div id="ff-sheet" role="dialog" aria-modal="true" aria-hidden="true"
     aria-labelledby="ff-sheet-title">
  <div id="ff-grip"></div>
  <div id="ff-sheet-content">
  <div style="display:flex;justify-content:space-between;align-items:center;
              border-bottom:1px solid #e5e7eb;padding:2px 0 8px;margin-bottom:10px;">
    <span id="ff-sheet-title" style="font-weight:700;font-size:15px;">筛选</span>
    <span style="font-size:12px;color:#6b7280;">
      显示 <b class="ff-count">–</b> / <span class="ff-total">–</span>
    </span>
  </div>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">
    <span style="font-weight:600;">评分</span>
    <span style="font-size:11px;color:#374151;">≥ <b id="ff-rating-val">3.4</b></span>
  </div>
  <input type="range" id="ff-rating" min="3.4" max="4.5" step="0.05" value="3.4"
         style="width:100%;margin-bottom:6px;">

  <div style="display:flex;justify-content:space-between;align-items:baseline;">
    <span style="font-weight:600;">晚餐价格</span>
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

  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin-bottom:4px;">
    <button id="ff-reset" style="padding:4px 0;border:1px solid #d1d5db;
            background:#f9fafb;border-radius:4px;cursor:pointer;font-size:11px;
            color:#374151;">重置筛选</button>
    <button id="ff-settings" style="padding:4px 0;border:1px solid #d1d5db;
            background:#eff6ff;border-radius:4px;cursor:pointer;font-size:11px;
            color:#1d4ed8;">⚙️ 同步设置</button>
    <!-- Language placeholder: persists the user's choice to localStorage
         under `tabelog.lang`; the actual translation pass will read it later. -->
    <select id="ff-lang" aria-label="语言"
            style="padding:4px 4px;border:1px solid #d1d5db;
                   background:#f9fafb;border-radius:4px;cursor:pointer;font-size:11px;
                   color:#374151;font-family:inherit;line-height:1.3;
                   text-align:center;text-align-last:center;
                   appearance:none;-webkit-appearance:none;-moz-appearance:none;">
      <option value="zh-CN">🌐 简体</option>
      <option value="zh-TW">🌐 繁體</option>
      <option value="en">🌐 EN</option>
      <option value="ja">🌐 日本語</option>
    </select>
  </div>
  <div id="ff-sync-status" style="font-size:10px;color:#6b7280;text-align:center;
       margin-top:2px;min-height:13px;">本地模式</div>
  </div>
</div>

<!-- Cloud sync settings modal. One Google sign-in button is the entire UX —
     no Gist ID, no PAT, no help docs. Two states: signed-out (just the
     button) and signed-in (email + sign-out). z-index beats ff-sheet
     (10002) and bm-modal (10011) since this opens from the filter sheet. -->
<div id="ff-modal-bg" style="display:none;position:fixed;inset:0;z-index:10020;
     background:rgba(0,0,0,0.4);align-items:center;justify-content:center;">
  <div style="background:#fff;border-radius:10px;padding:18px 20px;width:340px;
       font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       font-size:13px;color:#111827;box-shadow:0 10px 30px rgba(0,0,0,0.25);">
    <div style="font-weight:700;font-size:15px;margin-bottom:10px;">云同步</div>

    <!-- Signed-out state: Google's officially-rendered sign-in button.
         We mount it into #ff-signin-btn via google.accounts.id.renderButton,
         which gives the proper "popup window → choose account" flow that
         works on iOS / Android browsers (the older One Tap / id.prompt()
         path was unreliable on mobile and on desktop just auto-picked the
         already-logged-in account in a top-right toast). -->
    <div id="ff-auth-out" style="display:none;text-align:center;padding:6px 0;">
      <div id="ff-signin-btn" style="display:inline-block;min-height:40px;"></div>
      <div style="font-size:11px;color:#6b7280;margin-top:10px;line-height:1.5;">
        登录后，收藏 / 弃用 / 景点 会跨设备同步。<br>
        未登录则只存在当前浏览器。
      </div>
    </div>

    <!-- Signed-in state: avatar + email + sign-out. -->
    <div id="ff-auth-in" style="display:none;">
      <div style="display:flex;align-items:center;gap:10px;
                  padding:8px 10px;background:#f9fafb;border-radius:6px;
                  margin-bottom:8px;">
        <img id="ff-auth-pic" src="" alt=""
             style="width:32px;height:32px;border-radius:50%;
                    background:#e5e7eb;flex-shrink:0;">
        <div style="flex:1;min-width:0;">
          <div id="ff-auth-name" style="font-weight:600;font-size:12px;
                  color:#111827;overflow:hidden;text-overflow:ellipsis;
                  white-space:nowrap;"></div>
          <div id="ff-auth-email" style="font-size:11px;color:#6b7280;
                  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></div>
        </div>
      </div>
      <button id="ff-signout"
              style="width:100%;padding:6px;border:1px solid #d1d5db;
                     background:#f9fafb;color:#374151;border-radius:4px;
                     cursor:pointer;font-size:12px;">退出登录</button>
    </div>

    <div id="ff-cfg-msg" style="font-size:11px;min-height:14px;margin:10px 0 4px;"></div>

    <div style="display:flex;gap:6px;">
      <button id="ff-cfg-cancel" style="flex:1;padding:6px;border:1px solid #d1d5db;
              background:#f9fafb;color:#374151;border-radius:4px;cursor:pointer;
              font-size:12px;">关闭</button>
    </div>
  </div>
</div>

"""


# Page title + favicon. SVG-emoji favicon is a one-liner that avoids
# shipping a binary asset and renders consistently on every modern browser
# (Chrome / Safari / Firefox all accept utf-8 SVG data URLs). 🗾 = Japan
# silhouette — most thematic for the project. Tab icons always render with
# the system emoji font; the emojicdn Apple-PNG swap covers page content
# only, not tab/bookmark/window-title icons (browser security limit).
HEAD_BRANDING = """
<title>Japan Foodmap</title>
<link rel="icon" type="image/svg+xml" href='data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🗾</text></svg>'>
<!-- Pre-warm TCP/TLS to the two cross-origin hosts the page hits early:
     jsDelivr serves Leaflet locatecontrol + emoji-picker-element synchronously;
     emojicdn is the fallback for any emoji not in our local /emoji/ cache. -->
<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
<link rel="preconnect" href="https://emojicdn.elk.sh" crossorigin>
<link rel="preconnect" href="https://accounts.google.com" crossorigin>
<link rel="preconnect" href="https://api.jpfoodmap.com" crossorigin>
<script src="https://accounts.google.com/gsi/client" async defer></script>
"""

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
     contextmenu handler fires cleanly on touch. */
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
    position: fixed; bottom: 18px; right: 14px;
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
    position: fixed; top: 12px; left: 50%;
    transform: translateX(-50%);
    z-index: 9996;
    width: min(calc(100vw - 32px), 380px);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
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
  #ss-input-wrap.busy #ss-clear   { display: none; }
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
    color: #9ca3af; background: #fafafa;
    border-bottom: 1px solid #f3f4f6;
  }
  #ss-list .ss-rating {
    flex-shrink: 0; font-size: 11px; font-weight: 600;
    color: #b45309; padding: 0 6px;
  }
  @media (max-width: 480px) {
    #ss-input { font-size: 16px; }       /* iOS no-zoom */
    #ss-box { top: 8px; width: calc(100vw - 16px); }
  }
</style>
<div id="ss-box">
  <div id="ss-input-wrap">
    <span id="ss-icon">🔍</span>
    <input id="ss-input" type="text" autocomplete="off"
           placeholder="搜索餐厅 / 景点 / 地址 ...">
    <div id="ss-spinner"></div>
    <button id="ss-clear" type="button" aria-label="清空">×</button>
  </div>
  <div id="ss-list" role="listbox"></div>
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
</div>
"""


BOOKMARKS_MODAL_HTML = """
<script type="module"
        src="https://cdn.jsdelivr.net/npm/emoji-picker-element@^1/index.js"></script>
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
    <div class="bm-coord" id="bm-coord"></div>
    <div class="bm-row">
      <label>类型</label>
      <div class="bm-kind-seg" role="radiogroup" aria-label="类型">
        <button type="button" data-kind="bookmark" class="active"
                role="radio" aria-checked="true">⭐ 收藏</button>
        <button type="button" data-kind="attraction"
                role="radio" aria-checked="false">🗾 景点</button>
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
"""


MOBILE_UX_ASSETS = """
<style>
  html, body { overscroll-behavior: none; }
</style>
<script>
(function() {
  // iOS Safari page-level pinch.
  document.addEventListener('gesturestart',  function(e){ e.preventDefault(); });
  document.addEventListener('gesturechange', function(e){ e.preventDefault(); });
  document.addEventListener('gestureend',    function(e){ e.preventDefault(); });

  // iOS Safari double-tap zoom. Scope to outside the map so Leaflet's
  // own double-tap-to-zoom-in keeps working.
  var lastTouchEnd = 0;
  document.addEventListener('touchend', function(e){
    if (e.target && e.target.closest && e.target.closest('.leaflet-container')) return;
    var now = Date.now();
    if (now - lastTouchEnd <= 350) e.preventDefault();
    lastTouchEnd = now;
  }, { passive: false });

  // Desktop ctrl/cmd + wheel. Leaflet's wheel zoom doesn't use ctrlKey.
  document.addEventListener('wheel', function(e){
    if (e.ctrlKey || e.metaKey) e.preventDefault();
  }, { passive: false });

  // Desktop cmd/ctrl + 0/-/=/+ keyboard zoom.
  document.addEventListener('keydown', function(e){
    if (!(e.ctrlKey || e.metaKey)) return;
    if (e.key === '=' || e.key === '-' || e.key === '+' || e.key === '0') {
      e.preventDefault();
    }
  });
})();
</script>
<style>
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
    font-size: 10px; color: #9ca3af;
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
  .rst-ribbon-hyaku-2023 { background: #b8a988; color: #3a2f15; }
  .rst-ribbon-hyaku-2022 { background: #d4c8aa; color: #3a2f15; }
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
  .rst-actions { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }
  .rst-photos { display: grid; grid-template-columns: repeat(3, 1fr);
                gap: 6px; margin-bottom: 10px; }
  .rst-photos a { display: block; min-width: 0; }
  .rst-photos img { width: 100%; aspect-ratio: 1 / 1; object-fit: cover;
                    border-radius: 6px; display: block; background: #f3f4f6; }
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
    animation: mk-pulse 1.6s ease-out infinite;
    pointer-events: none;
  }
</style>"""


# Bottom-sheet DOM. Injected into <body>; populated by openSheet() in the
# filter JS. Backdrop is a sibling so taps fall through to it.
BOTTOM_SHEET_HTML = """
<div id="bs-backdrop"></div>
<div id="bs-sheet" role="dialog" aria-modal="true" aria-hidden="true">
  <div id="bs-grip"></div>
  <div id="bs-banner" hidden></div>
  <div id="bs-content"></div>
</div>
"""


# Service worker source. Written verbatim to docs/sw.js at build time after
# __BUILD_VERSION__ is substituted. Strategy by request type:
#   same-origin HTML / nav    → network-first   (so redeploys land quickly)
#   same-origin JSON / GeoJSON / JS → cache-first   (busted by version bump)
#   third-party tiles / CDN   → stale-while-revalidate
#   anything else (Nominatim, jpfoodmap API) → not intercepted, browser default
# Each build bumps the cache name via VERSION, so activate() purges the old
# cache and the next fetch repopulates with fresh data.
SW_JS_TEMPLATE = r"""// Auto-generated by src/tabelog/scrape/map.py — do not edit by hand.
// Build version: __BUILD_VERSION__
const VERSION = '__BUILD_VERSION__';
const CACHE = 'tabelog-' + VERSION;

self.addEventListener('install', () => { self.skipWaiting(); });

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    // sw.js itself must always come from the network so updates land.
    if (url.pathname.endsWith('/sw.js')) return;
    if (req.mode === 'navigate' || url.pathname.endsWith('.html')) {
      event.respondWith(networkFirst(req));
    } else {
      event.respondWith(cacheFirst(req));
    }
    return;
  }

  // Tiles + CDN assets we serve from cache on revisit but refresh in the
  // background. Anything outside this list (Nominatim search, jpfoodmap
  // sync API) passes straight through to the browser default.
  if (/(\.tile\.openstreetmap\.org$)|(^[a-c]\.tile\.)/i.test(url.hostname) ||
      url.hostname === 'emojicdn.elk.sh' ||
      url.hostname === 'cdn.jsdelivr.net' ||
      url.hostname === 'unpkg.com') {
    event.respondWith(staleWhileRevalidate(req));
  }
});

// Opaque (cross-origin no-cors) responses report ok=false / status=0 but
// we still want them cached for revisits.
function cacheable(resp) {
  return !!resp && (resp.ok || resp.type === 'opaque');
}

async function cacheFirst(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req);
  if (cached) return cached;
  const fresh = await fetch(req);
  if (cacheable(fresh)) cache.put(req, fresh.clone());
  return fresh;
}

async function networkFirst(req) {
  const cache = await caches.open(CACHE);
  try {
    const fresh = await fetch(req);
    if (cacheable(fresh)) cache.put(req, fresh.clone());
    return fresh;
  } catch (e) {
    const cached = await cache.match(req);
    if (cached) return cached;
    throw e;
  }
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req);
  const fresh = fetch(req).then(resp => {
    if (cacheable(resp)) cache.put(req, resp.clone());
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
  function loadPopups() {
    if (popupsMap) return Promise.resolve(popupsMap);
    if (popupsPromise) return popupsPromise;
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
    popupsPromise = fetch(popupsUrl, {cache: 'force-cache'})
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
  function emojiImg(m, extraStyle) {
    var key = EMOJI_MAP[m];
    var src = key
      ? 'emoji/' + key + '.png'
      : 'https://emojicdn.elk.sh/' + encodeURIComponent(m) + '?style=apple';
    return '<img src="' + src + '" alt="' + m + '" draggable="false" ' +
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
  // Attach a MutationObserver to one container so any future emoji-bearing
  // content under it gets swapped to Apple PNGs. Exposed so initMap() can
  // hook the Leaflet popup pane once Leaflet has created it.
  function observeForEmoji(root) {
    if (!root) return;
    new MutationObserver(function(muts) {
      for (var i = 0; i < muts.length; i++) {
        var added = muts[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var nd = added[j];
          if (nd.nodeType === 1) emojify(nd);
          else if (nd.nodeType === 3 && nd.parentNode) emojify(nd.parentNode);
        }
      }
    }).observe(root, {childList: true, subtree: true});
  }
  function startEmojiObserver() {
    // One-shot pass over the static page (filter panel, FAB labels, modal
    // titles…) — these never change after load.
    emojify(document.body);
    // Narrow ongoing observers only on the containers that mutate with
    // emoji-bearing HTML at runtime. The previous wider `document.body`
    // observer caught every marker insertion too, wasting one TreeWalker
    // pass per marker on each pan — marker divIcons already pre-swap their
    // emoji at construction (see makeIcon → emojiImg), so they don't need
    // the observer. Leaflet popups (search result, right-click "加入收藏")
    // live under .leaflet-popup-pane, which initMap() hooks once Leaflet
    // has built its panes.
    observeForEmoji(document.getElementById('bs-content'));
    observeForEmoji(document.getElementById('bm-modal'));
    observeForEmoji(document.getElementById('ss-list'));
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startEmojiObserver);
  } else {
    startEmojiObserver();
  }

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
  function localizeTree(root) {
    if (!root || !I18N_MAP) return;
    if (root.nodeType !== 1) return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function(n) {
        var p = n.parentNode;
        while (p && p.nodeType === 1) {
          var t = p.tagName;
          if (t === 'SCRIPT' || t === 'STYLE' || t === 'TEXTAREA' || t === 'INPUT') {
            return NodeFilter.FILTER_REJECT;
          }
          if (p.getAttribute && p.getAttribute('lang') === 'ja') {
            return NodeFilter.FILTER_REJECT;
          }
          p = p.parentNode;
        }
        return NodeFilter.FILTER_ACCEPT;
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
  function observeForI18n(root) {
    if (!root || !I18N_MAP) return;
    new MutationObserver(function(muts) {
      for (var i = 0; i < muts.length; i++) {
        var added = muts[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var nd = added[j];
          if (nd.nodeType === 1) localizeTree(nd);
          else if (nd.nodeType === 3 && nd.parentNode) localizeTree(nd.parentNode);
        }
      }
    }).observe(root, {childList: true, subtree: true});
  }
  function startI18nObserver() {
    if (!I18N_MAP) return;
    localizeTree(document.body);
    observeForI18n(document.getElementById('bs-content'));
    observeForI18n(document.getElementById('bm-modal'));
    observeForI18n(document.getElementById('ss-list'));
    // Filter sheet has dynamic textContent rewrites (cuisine summary
    // "全部"/"无"/"已选 N / M", live count chips). Without an observer
    // here those flip back to Chinese after every filter change.
    observeForI18n(document.getElementById('ff-sheet-content'));
    // Sync-settings modal (#ff-modal-bg) lives outside #ff-sheet and
    // mutates its own status line (cfgMsg) — "测试中…", "已清除", etc.
    observeForI18n(document.getElementById('ff-modal-bg'));
    // Reflect onto <html lang> — browsers use it for hyphenation and
    // accessibility (screen readers, especially).
    try { document.documentElement.lang = activeLang; } catch (_) {}
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startI18nObserver);
  } else {
    startI18nObserver();
  }
  // Exposed so initMap() can hook the Leaflet popup pane the same way
  // emojify does, once Leaflet has built its panes.
  window.__observeForI18n = observeForI18n;
  window.__localizeTree = localizeTree;
  window.__activeLang = activeLang;

  // ===== Cloud sync layer (Google OAuth + Cloudflare Worker + KV) =====
  //
  // Auth (Google ID token + cached profile) is per-device, stored in
  // localStorage. The state (favorites/blacklist sets + bookmarks) is also
  // cached locally so the page works offline / before the first pull.
  // When signed in, the page pulls on load + tab-focus + every 60s, and
  // pushes (debounced 500ms) on every edit. Last-writer-wins: no merge.
  var AUTH_KEY = 'tabelog.auth';
  var CACHE_KEY = 'omakase_state_cache_v2';
  var API = 'https://api.jpfoodmap.com/api/state';

  function loadAuth() {
    try { return JSON.parse(localStorage.getItem(AUTH_KEY) || '{}'); }
    catch (_) { return {}; }
  }
  function saveAuth(a) { localStorage.setItem(AUTH_KEY, JSON.stringify(a)); }
  function signOut() {
    localStorage.removeItem(AUTH_KEY);
    location.reload();
  }

  function loadCache() {
    try {
      var d = JSON.parse(localStorage.getItem(CACHE_KEY) || '{}');
      return {fav: d.fav || null, black: d.black || null, dirty: !!d.dirty};
    } catch (_) { return {fav: null, black: null, dirty: false}; }
  }
  // Persists state + the dirty flag. Storing dirty across reloads is the
  // whole point — without it, an unpushed change would be silently
  // overwritten by the next pull after a refresh.
  function saveCache(state, dirtyFlag) {
    localStorage.setItem(CACHE_KEY, JSON.stringify({
      fav: Array.from(state.fav),
      black: Array.from(state.black),
      dirty: !!dirtyFlag,
    }));
  }

  // Single source of truth for "are we currently signed in with a token
  // that hasn't expired yet". exp is stored in ms on saveAuth(). Returns
  // the full auth object (including id_token, email, name, picture) when
  // valid, null otherwise — callers use truthiness.
  function configured() {
    var a = loadAuth();
    if (!a.id_token) return null;
    if (a.exp && Date.now() >= a.exp) return null;   // expired
    return a;
  }
  function authHeaders() {
    var a = configured();
    return a ? {'Authorization': 'Bearer ' + a.id_token} : null;
  }

  function initMap(data) {
    var mapEl = document.querySelector('.folium-map');
    if (!mapEl) { setTimeout(function(){ initMap(data); }, 50); return; }
    // Find the leaflet map object that folium attached to this element.
    var mapId = mapEl.id;
    var map = window[mapId];
    if (!map) { setTimeout(function(){ initMap(data); }, 50); return; }
    if (typeof L === 'undefined' || !L.markerClusterGroup) { setTimeout(function(){ initMap(data); }, 50); return; }
    if (!L.control.locate) { setTimeout(function(){ initMap(data); }, 50); return; }

    // Late-bound emoji observer for Leaflet popups: search-result temp
    // marker and the right-click "加入收藏" popup both inject HTML into
    // .leaflet-popup-pane, which only exists after the map initializes.
    var popupPane = map.getPane && map.getPane('popupPane');
    if (popupPane) {
      observeForEmoji(popupPane);
      if (window.__observeForI18n) {
        window.__observeForI18n(popupPane);
      }
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
    map.on('moveend', function() {
      try {
        var c = map.getCenter();
        localStorage.setItem(STATE_KEY_VIEW, JSON.stringify({
          lat: c.lat, lon: c.lng, zoom: map.getZoom()
        }));
      } catch (e) {}
    });
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
      locateOptions: {enableHighAccuracy: true, maximumAge: 5000, watch: false},
      strings: {
        title: '显示我的位置',
        popup: '你在约 {distance} {unit} 范围内',
        outsideMapBoundsMsg: '当前位置在地图范围之外'
      }
    }).addTo(map);
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
            low:  'https://assets.jpfoodmap.com/japan-low.geojson',
            mid:  'https://assets.jpfoodmap.com/japan-mid.geojson',
            high: 'https://assets.jpfoodmap.com/japan.geojson'
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
    function sanitizeBookmarkEmoji(bm) {
      if (bm && bm.emoji && !isPureEmoji(bm.emoji)) {
        bm.emoji = '📍';
      }
      return bm;
    }
    function sanitizeBookmarkArray(arr) {
      if (Array.isArray(arr)) arr.forEach(sanitizeBookmarkEmoji);
      return arr;
    }
    var bookmarks = (function() {
      try {
        var raw = localStorage.getItem(BM_KEY);
        if (raw === null) {
          // First visit on this device — seed from the embedded baseline
          // and persist it so subsequent edits anchor against that copy.
          var seed = EMBEDDED_BOOKMARKS.slice();
          localStorage.setItem(BM_KEY, JSON.stringify(seed));
          return sanitizeBookmarkArray(seed);
        }
        var arr = JSON.parse(raw);
        return sanitizeBookmarkArray(Array.isArray(arr) ? arr : []);
      } catch (_) { return sanitizeBookmarkArray(EMBEDDED_BOOKMARKS.slice()); }
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

    function saveBookmarks() {
      try { localStorage.setItem(BM_KEY, JSON.stringify(bookmarks)); } catch (_) {}
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
        photoHtml = '<div class="rst-photos">' + photos.map(function(big) {
          var thumb = String(big).replace('640x640_rect_', '150x150_square_');
          return '<a href="' + escapeHtml(big) + '" target="_blank" rel="noopener">'
               + '<img src="' + escapeHtml(thumb) + '" loading="lazy" alt="" '
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
      // show the button for `seat` — station and address are mostly
      // kanji Chinese readers can read directly, so the affordance is
      // visual noise there. English shows all three. The label "翻译"
      // rides the CJK localizer: zh-TW gets 翻譯 via OpenCC, en gets
      // "Translate" via TEXT_EN_MAP.
      function txBtn(field, val) {
        if (!val || val === '—') return '';
        if (_lang === 'ja') return '';
        if ((_lang === 'zh-CN' || _lang === 'zh-TW') && field !== 'seat') return '';
        return '<button type="button" class="rst-tx-btn" data-tx="' + field + '">翻译</button>';
      }
      // Quick-jump to a Google Maps search for "<name> <address>". The
      // search URL avoids needing a place_id — Google's `?api=1&query=`
      // form is documented + stable, and lands on the search results
      // page so the user can pick the right pin if there are dupes.
      // Title stays English (emoji's universal) so the build-time CJK
      // scan doesn't pick up phantom runs from JS string literals.
      var gmapsQ = encodeURIComponent(
        ((d.name || '') + ' ' + (addr || '')).trim()
      );
      var gmapsUrl = 'https://www.google.com/maps/search/?api=1&query=' + gmapsQ;
      var gmapsBtn = '<a class="rst-gmaps" href="' + gmapsUrl
                   + '" target="_blank" rel="noopener" '
                   + 'aria-label="Open in Google Maps" '
                   + 'title="Open in Google Maps">'
                   + '<img src="img/google-maps.png" alt="Google Maps" '
                   + 'width="18" height="18" loading="lazy"></a>';
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
          + '</div>'
        + '</div>'
        + photoHtml
        + '<div class="rst-genre"><span lang="ja">' + escapeHtml(genre) + '</span> / ' + escapeHtml(bucket) + '</div>'
        + '<div class="rst-info">'
          + '<div class="rst-info-row"><span class="rst-label">晚</span><span class="rst-value">' + escapeHtml(dinnerS) + '</span></div>'
          + '<div class="rst-info-row"><span class="rst-label">车站</span><span class="rst-value">📍 <span lang="ja">' + escapeHtml(station) + '</span></span>' + txBtn('station', station) + '</div>'
          + '<div class="rst-info-row"><span class="rst-label">午</span><span class="rst-value">' + escapeHtml(lunchS) + '</span></div>'
          + '<div class="rst-info-row"><span class="rst-label">座位</span><span class="rst-value">' + (seat ? escapeHtml(seat) : '—') + '</span>' + txBtn('seat', seat) + '</div>'
          + '<div class="rst-info-row"><span class="rst-label">地址</span><span class="rst-value" lang="ja">' + escapeHtml(addr) + '</span>' + txBtn('addr', addr) + '</div>'
        + '</div>'
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
          '<div style="font-family:monospace;font-size:11px;color:#6b7280;' +
                      'margin-bottom:8px;">' + coord + '</div>' +
          actionBtn +
        '</div>';
      L.popup({offset: [0, offY]})
        .setLatLng([bm.lat, bm.lon])
        .setContent(html)
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
          if (i >= 0) bookmarks.splice(i, 1);
          removeBookmarkMarker(bm);
          saveBookmarks();
          schedulePush();
          map.closePopup();
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

    function openBookmarkModal(latlng, prefillName) {
      bmPending = {lat: latlng.lat, lng: latlng.lng};
      bmCoordEl.textContent = latlng.lat.toFixed(6) + ', ' + latlng.lng.toFixed(6);
      bmNameInput.value = prefillName || '';
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
    function setBmModalBusy(busy) {
      var saveBtn   = bmModal.querySelector('.bm-save');
      var cancelBtn = bmModal.querySelector('.bm-cancel');
      var closeBtn  = bmModal.querySelector('.bm-close');
      if (busy) {
        saveBtn.dataset.origLabel = saveBtn.textContent;
        saveBtn.textContent = '…';
      } else if (saveBtn.dataset.origLabel) {
        saveBtn.textContent = saveBtn.dataset.origLabel;
        delete saveBtn.dataset.origLabel;
      }
      saveBtn.disabled = busy;
      cancelBtn.disabled = busy;
      closeBtn.disabled = busy;
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
      // Capture coords + category now so a click on a different pin while
      // we're awaiting the translate fetch can't redirect the save.
      var pendingLat = bmPending.lat;
      var pendingLng = bmPending.lng;
      var pendingCat = bmKind;
      setBmModalBusy(true);
      // Wikidata lookup: hit fills sc/tc/jp/en with community-curated
      // labels, miss leaves them empty — display falls back to name_src
      // via bmDisplayName. We deliberately don't fall back to MT here;
      // honest empty fields beat literal translations of proper nouns
      // ("新世界" → "new world" was the cautionary example).
      wikidataLookup(name, pendingLat, pendingLng).then(function(wd) {
        var bm = {
          id: 'bm-' + Date.now().toString(36) + '-' +
              Math.random().toString(36).slice(2, 7),
          name_src: name,
          name_sc: (wd && wd.sc) || '',
          name_tc: (wd && wd.tc) || '',
          name_jp: (wd && wd.jp) || '',
          name_en: (wd && wd.en) || '',
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
      }).then(function() {
        setBmModalBusy(false);
        closeBookmarkModal();
      });
    }
    bmBackdrop.addEventListener('click', closeBookmarkModal);
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
    bmEmojiMore.addEventListener('click', function() {
      var nowOpen = !bmPicker.classList.contains('bm-show');
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
          '<div style="font-family:monospace;margin-bottom:8px;">' + s + '</div>' +
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
      L.popup().setLatLng(e.latlng).setContent(html).openOn(map);
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
    var SS_RESTAURANT_LIMIT = 200;
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
      ssList.appendChild(h);
    }
    function ssAppendSubSectionHead(label) {
      var h = document.createElement('div');
      h.className = 'ss-subsection-head';
      h.textContent = label;
      ssList.appendChild(h);
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
      ssList.appendChild(row);
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
      ssList.appendChild(row);
    }
    function ssRender(localMatch, apiItems, apiPending) {
      ssList.innerHTML = '';
      var items = (localMatch && localMatch.items) || [];
      var total = (localMatch && localMatch.total) || 0;
      var hasLocal = items.length > 0;
      var hasApi   = apiItems && apiItems.length > 0;
      if (!hasLocal && !hasApi && !apiPending) {
        var empty = document.createElement('div');
        empty.className = 'ss-row ss-empty';
        empty.textContent = '没有匹配的结果';
        ssList.appendChild(empty);
        ssList.classList.add('open');
        return;
      }
      if (hasLocal) {
        ssAppendSectionHead('餐厅库');
        ssAppendRestaurantSection(items, localMatch.inViewportCount);
        if (total > items.length) {
          var more = document.createElement('div');
          more.className = 'ss-row ss-empty';
          more.textContent = '+' + (total - items.length) +
                             ' 个其他匹配 · 输入更多字以缩小范围';
          ssList.appendChild(more);
        }
      }
      if (hasApi || apiPending) {
        ssAppendSectionHead('地图搜索');
        if (hasApi) {
          apiItems.forEach(ssAppendApiRow);
        } else {
          var pending = document.createElement('div');
          pending.className = 'ss-row ss-empty';
          pending.textContent = '搜索中…';
          ssList.appendChild(pending);
        }
      }
      ssList.classList.add('open');
    }
    // Re-renders the dropdown with local section preserved, then appends a
    // single error row in place of the API section. Local hits stay usable
    // even when Nominatim is unreachable.
    function ssShowError(localMatch, msg) {
      ssList.innerHTML = '';
      var items = (localMatch && localMatch.items) || [];
      var total = (localMatch && localMatch.total) || 0;
      var ivc = localMatch ? localMatch.inViewportCount : null;
      if (items.length > 0) {
        ssAppendSectionHead('餐厅库');
        ssAppendRestaurantSection(items, ivc);
        if (total > items.length) {
          var more = document.createElement('div');
          more.className = 'ss-row ss-empty';
          more.textContent = '+' + (total - items.length) +
                             ' 个其他匹配 · 输入更多字以缩小范围';
          ssList.appendChild(more);
        }
      }
      ssAppendSectionHead('地图搜索');
      var r = document.createElement('div');
      r.className = 'ss-row ss-empty ss-error';
      r.textContent = msg;
      ssList.appendChild(r);
      ssList.classList.add('open');
    }
    function ssCloseDropdown() {
      ssList.classList.remove('open');
    }
    // Pan to a restaurant in the library and open its bottom sheet — the
    // same code path a marker click triggers. Keeps a temp marker out of
    // the way; the actual restaurant marker is already on the map.
    function ssGotoRestaurant(d) {
      ssCloseDropdown();
      ssInput.value = d.name || '';
      ssWrap.classList.add('has-text');
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
      function onArrive() {
        map.off('moveend', onArrive);
        reveal();
      }
      map.on('moveend', onArrive);
      map.flyTo(latlng, targetZoom, {duration: 0.8});
    }
    function ssGoto(it) {
      ssCloseDropdown();
      ssInput.value = it.name;
      ssWrap.classList.add('has-text');
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
          '<div style="font-family:monospace;font-size:11px;color:#6b7280;' +
                      'margin-bottom:8px;">' +
            it.lat.toFixed(6) + ', ' + it.lon.toFixed(6) + '</div>' +
          '<button id="ss-add-bm" ' +
                  'style="padding:4px 12px;font-size:12px;cursor:pointer;' +
                  'border:1px solid #2563eb;border-radius:4px;' +
                  'background:#2563eb;color:#fff;font-weight:600;">' +
                  '⭐ 加入收藏</button>' +
        '</div>';
      L.popup({offset: [0, -26]}).setLatLng(latlng).setContent(popHtml).openOn(map);
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
    function ssSearch(q) {
      var seq = ++ssReqSeq;
      ssWrap.classList.add('busy');
      // Japan bbox: lon 122-154, lat 24-46. viewbox order:
      //   x1 (left lon), y1 (top lat), x2 (right lon), y2 (bottom lat)
      // bounded=1 forbids matches outside the box; otherwise OSM happily
      // returns "Osaka, Texas" before the city in Japan.
      var url = 'https://nominatim.openstreetmap.org/search?'
              + 'format=jsonv2&limit=6&addressdetails=0&namedetails=1'
              + '&viewbox=122,46,154,24&bounded=1'
              + '&accept-language=zh-CN,zh,ja,en'
              + '&q=' + encodeURIComponent(q);
      fetch(url, {headers: {'Accept': 'application/json'}})
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(arr) {
          if (seq !== ssReqSeq) return;       // stale response
          ssWrap.classList.remove('busy');
          var items = (Array.isArray(arr) ? arr : [])
            .map(ssParseResult)
            .filter(function(x){ return !isNaN(x.lat) && !isNaN(x.lon); });
          ssRender(ssLocalMatch, items, false);
        })
        .catch(function(err) {
          if (seq !== ssReqSeq) return;
          ssWrap.classList.remove('busy');
          ssShowError(ssLocalMatch, '搜索失败: ' + err.message);
        });
    }
    function ssOnInput() {
      var v = ssInput.value.trim();
      if (v) ssWrap.classList.add('has-text');
      else   ssWrap.classList.remove('has-text');
      clearTimeout(ssDebounce);
      if (!v) {
        ssReqSeq++;
        ssLocalMatch = {items: [], total: 0};
        ssWrap.classList.remove('busy');
        ssCloseDropdown();
        ssRemoveTempMarker();
        return;
      }
      // Restaurant-library match runs synchronously — paint it first so the
      // user sees results in the same frame, no 300ms wait. The Nominatim
      // call still goes through the debounce.
      ssLocalMatch = ssMatchLocal(v);
      ssRender(ssLocalMatch, null, true);
      ssDebounce = setTimeout(function() { ssSearch(v); }, 300);
    }
    // exitSearch: full bail-out. Used by the × button and Escape — clears
    // text, drops the temp marker + popup, closes the dropdown, and blurs
    // the input so the mobile keyboard goes away.
    function ssExitSearch() {
      ssReqSeq++;
      ssLocalMatch = {items: [], total: 0};
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
      if (ssList.children.length > 0) ssList.classList.add('open');
    });
    ssInput.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') {
        if (ssList.classList.contains('open') && ssInput.value) {
          ssCloseDropdown();
        } else {
          ssExitSearch();
        }
      } else if (e.key === 'Enter') {
        // Enter on a non-empty dropdown -> pick the first result.
        var first = ssList.querySelector('.ss-row:not(.ss-empty)');
        if (first) first.click();
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
    function setStatus(text, kind) {
      if (!statusEl) return;
      statusEl.textContent = text;
      statusEl.style.color = kind === 'err' ? '#dc2626'
                           : kind === 'ok'  ? '#16a34a'
                           : kind === 'busy'? '#2563eb' : '#6b7280';
    }
    // dirty is restored from cache so an unpushed change survives a refresh.
    var dirty = cache.dirty || false;
    var pushTimer = null, pollTimer = null;

    // Flash the filter FAB when there are local changes that haven't
    // landed on the server. Colour depends on whether the user is signed in:
    //   not signed in → red pulse (urgent — edits live only in this browser)
    //   signed in     → blue pulse (just informational — push will arrive)
    var fabEl = document.getElementById('ff-fab');
    function updateNeedsSyncIndicator() {
      if (!fabEl) return;
      var signedIn = !!configured();
      var d = !!dirty;
      fabEl.classList.toggle('needs-sync',         d && !signedIn);
      fabEl.classList.toggle('needs-sync-pending', d &&  signedIn);
      fabEl.title = d
        ? (signedIn
            ? '改动待同步到云端…'
            : '收藏 / 弃用 / 景点 仅存于本地浏览器，点击登录以跨设备同步')
        : '筛选';
    }

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
    // True for the single retry that follows a successful silentReAuth.
    // Prevents an infinite loop in the (rare) case the Worker rejects a
    // freshly-minted token too.
    var pullRetriedAfterSilent = false;
    function pull() {
      var headers = authHeaders();
      if (!headers) {
        setStatus(dirty ? '本地模式（改动仅存浏览器）' : '本地模式',
                  dirty ? 'err' : '');
        updateNeedsSyncIndicator();
        return;
      }
      // Don't clobber unsent local edits with a stale remote.
      if (dirty) { updateNeedsSyncIndicator(); return; }
      setStatus('同步中…', 'busy');
      fetch(API, {headers: headers})
        .then(function(r) {
          if (r.status === 401) {
            // If we already retried after a silent refresh and STILL got
            // 401, the Worker is rejecting fresh tokens — don't loop;
            // bail to the manual sign-in flow.
            if (pullRetriedAfterSilent) {
              pullRetriedAfterSilent = false;
              setStatus('登录已过期，请重新登录', 'err');
              localStorage.removeItem(AUTH_KEY);
              updateNeedsSyncIndicator();
              return null;
            }
            // Token rejected by Worker — expired between our cached exp
            // check and the request. Try silent re-auth; on success the
            // next pull picks up the fresh token. Only clear stored auth
            // (forcing the user to re-sign-in) if silent re-auth also
            // fails — that's the genuine "Google session is gone" case.
            setStatus('重新连接中…', 'busy');
            silentReAuth(function(ok) {
              if (ok) {
                pullRetriedAfterSilent = true;
                setTimeout(pull, 100);
                return;
              }
              setStatus('登录已过期，请重新登录', 'err');
              localStorage.removeItem(AUTH_KEY);
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
          // Empty server (new account): server returns {}; nothing to apply.
          // Server with data: {favorites, blacklist, bookmarks}.
          if (Array.isArray(remote.favorites)) state.fav   = new Set(remote.favorites);
          if (Array.isArray(remote.blacklist)) state.black = new Set(remote.blacklist);
          if (Array.isArray(remote.bookmarks)) {
            // Wipe + re-render in place — closures hold the same array
            // reference, so we mutate rather than reassign. Both layers
            // (bookmarks + user-added attractions) get cleared so the
            // pull rebuild starts from a clean slate. The builtin
            // landmarks share the same layers, so the wipe takes them
            // out too — renderFavoritesBuiltin puts them back.
            bookmarks.length = 0;
            bookmarksLayer.clearLayers();
            userAttractionsLayer.clearLayers();
            bmMarkerById = {};
            remote.bookmarks.forEach(function(bm) {
              sanitizeBookmarkEmoji(bm);
              bookmarks.push(bm);
              renderBookmark(bm);   // early-returns on hidden / no-coord
            });
            // Recompute the hidden-builtin set from the freshly-pulled
            // bookmarks before re-rendering builtins, so any hide flag
            // the user set on another device takes effect now.
            rebuildHiddenIds();
            renderFavoritesBuiltin();
            saveBookmarks();
          }
          saveCache(state, false);
          refreshAllMarkers();
          setStatus('已同步 ' + new Date().toLocaleTimeString(), 'ok');
        })
        .catch(function(e) { setStatus('同步失败: ' + e.message, 'err'); })
        .finally(function() { updateNeedsSyncIndicator(); });
    }
    var pushRetriedAfterSilent = false;
    function push() {
      var headers = authHeaders();
      // Local mode (not signed in): nothing to push, but keep dirty=true so
      // the FAB keeps flashing — the whole point is for the user to notice
      // they haven't enabled sync. Indicator clears once they sign in and
      // a real push succeeds.
      if (!headers) {
        saveCache(state, dirty);
        setStatus(dirty ? '本地模式（改动仅存浏览器）' : '本地模式',
                  dirty ? 'err' : '');
        updateNeedsSyncIndicator();
        return;
      }
      setStatus('保存中…', 'busy');
      headers['Content-Type'] = 'application/json';
      var body = JSON.stringify({
        favorites: Array.from(state.fav),
        blacklist: Array.from(state.black),
        bookmarks: bookmarks,
      });
      fetch(API, {method: 'PUT', headers: headers, body: body})
        .then(function(r) {
          if (r.status === 401) {
            if (pushRetriedAfterSilent) {
              pushRetriedAfterSilent = false;
              setStatus('登录已过期，请重新登录', 'err');
              localStorage.removeItem(AUTH_KEY);
              updateNeedsSyncIndicator();
              return;
            }
            // Same recovery as pull(): try silent re-auth before clearing
            // the stored token. dirty stays true so the retried push (or
            // the next user edit) actually flushes our pending changes.
            setStatus('重新连接中…', 'busy');
            saveCache(state, true);
            silentReAuth(function(ok) {
              if (ok) {
                pushRetriedAfterSilent = true;
                setTimeout(push, 100);
                return;
              }
              setStatus('登录已过期，请重新登录', 'err');
              localStorage.removeItem(AUTH_KEY);
              updateNeedsSyncIndicator();
            });
            return;
          }
          if (!r.ok) throw new Error('HTTP ' + r.status);
          pushRetriedAfterSilent = false;
          dirty = false;
          saveCache(state, false);
          setStatus('已同步 ' + new Date().toLocaleTimeString(), 'ok');
        })
        .catch(function(e) {
          // Keep dirty=true (in memory AND in localStorage) on failure so the
          // next pull — or a page refresh — doesn't silently clobber our
          // unsaved change with the stale remote state. Change recovers when
          // a future push succeeds.
          saveCache(state, true);
          setStatus('保存失败: ' + e.message + '（已存本地，下次推送时重试）', 'err');
        })
        .finally(function() { updateNeedsSyncIndicator(); });
    }
    function schedulePush() {
      dirty = true;
      saveCache(state, true);
      updateNeedsSyncIndicator();
      clearTimeout(pushTimer);
      pushTimer = setTimeout(push, 500);
    }
    function startSync() {
      // Paint the dirty indicator on first load so a flag restored from
      // localStorage shows up before the first push/pull lands.
      updateNeedsSyncIndicator();
      // If a previous session left an unpushed change, retry pushing it
      // *before* pulling; otherwise pull would just confirm the remote
      // state (which still lacks the change) and the user would see "已同步"
      // while their edit is actually still local-only.
      if (dirty) push(); else pull();
      clearInterval(pollTimer);
      pollTimer = setInterval(function() {
        if (document.visibilityState === 'visible') pull();
      }, 60000);
    }
    document.addEventListener('visibilitychange', function() {
      if (document.visibilityState === 'visible') pull();
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
                 '<div style="position:absolute;inset:0;border-radius:50%;' +
                 'background:radial-gradient(circle closest-side, ' +
                 color + 'EE 0%, ' + color + 'AA 50%, ' + color + '00 100%);"></div>' +
                 emojiImg(emoji, 'position:relative;width:16px;height:16px;' +
                                 'filter:drop-shadow(0 1px 2px rgba(0,0,0,0.35));') +
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
          bsBanner.textContent =
            '🔍 此餐厅当前不在筛选范围内 — 调整左侧筛选条件可让它出现在地图上';
        }
      }
    }

    function syncFavButton(btn, d) {
      var label = btn.querySelector('.ff-fav-label');
      if (!label) return;
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
        setTxBtnLabel(btn, '翻译');
      });
    });

    // Bind the banner element so the highlight helpers (declared earlier
    // in this initMap closure) can toggle it.
    bsBanner = document.getElementById('bs-banner');

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
      // Reflect the selection in the top search box. The × button stays
      // visible (via .has-text) regardless of whether the dropdown is
      // open, so the user has a one-click "deselect" affordance even when
      // the sheet is collapsed to peek and they've panned the map around.
      if (ssInput) {
        ssInput.value = d.name || '';
        ssWrap.classList.add('has-text');
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
                            '</span></div><div style="margin-top:10px;color:#dc2626;">加载失败,请检查网络</div></div>');
                    });
      }
      // Force reflow so the transition runs even on rapid reopen.
      void bsSheet.getBoundingClientRect();
      bsSheet.classList.add('bs-open');
      bsBackdrop.classList.add('bs-open');
      bsSheet.setAttribute('aria-hidden', 'false');
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
      // Drop the selected-restaurant name from the top search box. The
      // input may be holding either that name (if the user opened the
      // sheet then never touched the box) or a typed query (if they were
      // mid-search) — both should clear when the selection goes away.
      if (ssInput) {
        ssInput.value = '';
        ssWrap.classList.remove('has-text');
      }
      var ffbtn = document.getElementById('ff-fab');
      if (ffbtn) ffbtn.hidden = false;
    }
    function expandSheet() {
      if (bsSheet.classList.contains('bs-peek')) {
        bsSheet.classList.remove('bs-peek');
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
    bsGrip.addEventListener('mousedown',  bsDragStart);
    document.addEventListener('mousemove', bsDragMove);
    document.addEventListener('mouseup',   bsDragEnd);

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
      bookableOnly: false, onlyFav: false, hideBlack: true, hideForeign: true
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
        var bEl = document.getElementById('ff-bookable-only');
        localStorage.setItem(STATE_KEY_FILTER, JSON.stringify({
          rating: parseFloat(ratingSlider.value),
          prices: prices,
          genres: genres,
          awards: awards,
          bookableOnly: bEl ? bEl.checked : false,
          onlyFav: onlyFavEl.checked,
          hideBlack: hideBlackEl.checked,
          hideForeign: hideForeignEl.checked
        }));
      } catch (e) {}
    }
    function restoreFilterState() {
      var s;
      try { s = JSON.parse(localStorage.getItem(STATE_KEY_FILTER) || 'null'); }
      catch (e) { return; }
      if (!s) return;
      if (typeof s.rating === 'number') ratingSlider.value = String(s.rating);
      if (Array.isArray(s.prices)) {
        var pSet = {};
        s.prices.forEach(function(v){ pSet[v] = 1; });
        document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = !!pSet[c.value]; });
      }
      if (Array.isArray(s.genres)) {
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
    }

    // Live update on drag, not just on release.
    ratingSlider.addEventListener('input', apply);
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
    ffGrip.addEventListener('mousedown',  ffDrag.start);
    document.addEventListener('mousemove', ffDrag.move);
    document.addEventListener('mouseup',   ffDrag.end);

    document.getElementById('ff-reset').addEventListener('click', function() {
      ratingSlider.value = '3.4';
      document.querySelectorAll('input[name=ff-price]').forEach(function(c){ c.checked = true; });
      document.querySelectorAll('input[name=ff-genre]').forEach(function(c){ c.checked = true; });
      document.querySelectorAll('input[name=ff-award]').forEach(function(c){ c.checked = false; });
      var resetBookable = document.getElementById('ff-bookable-only');
      if (resetBookable) resetBookable.checked = false;
      onlyFavEl.checked = false;
      hideBlackEl.checked = true;
      hideForeignEl.checked = true;
      apply();
    });

    // ----- Cloud sync settings modal: Google sign-in / sign-out.
    var modalBg = document.getElementById('ff-modal-bg');
    var authOut = document.getElementById('ff-auth-out');
    var authIn  = document.getElementById('ff-auth-in');
    var authPic = document.getElementById('ff-auth-pic');
    var authName= document.getElementById('ff-auth-name');
    var authEmail = document.getElementById('ff-auth-email');
    var signinBtnContainer = document.getElementById('ff-signin-btn');
    var cfgMsg  = document.getElementById('ff-cfg-msg');

    // Decode a Google ID token (JWT) and persist {id_token, email, name,
    // picture, exp} via saveAuth(). Pure data — no UI side effects, no
    // page reload — so both the interactive sign-in callback and the
    // silent re-auth path can reuse it. Returns true on success.
    function saveAuthFromCredential(credential) {
      try {
        var parts = credential.split('.');
        var pad = '='.repeat((4 - parts[1].length % 4) % 4);
        var b64 = (parts[1] + pad).replace(/-/g, '+').replace(/_/g, '/');
        var payload = JSON.parse(decodeURIComponent(
          atob(b64).split('').map(function(c) {
            return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
          }).join('')));
        saveAuth({
          id_token: credential,
          email: payload.email || '',
          name:  payload.name  || '',
          picture: payload.picture || '',
          exp: (payload.exp || 0) * 1000
        });
        return true;
      } catch (e) {
        return false;
      }
    }

    // GIS callback for the visible sign-in button. Saves the new token and
    // reloads so the rest of the page boots into the signed-in state.
    function onGoogleCredential(resp) {
      if (!resp || !resp.credential) {
        cfgMsg.style.color = '#dc2626';
        cfgMsg.textContent = '登录被取消';
        return;
      }
      if (!saveAuthFromCredential(resp.credential)) {
        cfgMsg.style.color = '#dc2626';
        cfgMsg.textContent = '登录处理失败';
        return;
      }
      cfgMsg.style.color = '#16a34a';
      cfgMsg.textContent = '✓ 登录成功，重新加载…';
      setTimeout(function() { location.reload(); }, 600);
    }

    // Silent re-auth via Google One Tap with auto_select. Google ID tokens
    // have a fixed 1h TTL (Google-side, can't extend); without this the
    // user gets bumped to the sign-in button every hour, which on mobile
    // (where the browser eagerly evicts background tabs) feels like
    // "closing the app logs me out". With auto_select + an active Google
    // session in the browser, this completes with no UI at all. Multi-
    // account users see a single One Tap chooser — one click to refresh.
    // FedCM is opted in (use_fedcm_for_prompt) so the flow survives
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
      }
      google.accounts.id.initialize({
        client_id: GOOGLE_CLIENT_ID,
        callback: function(resp) {
          if (resp && resp.credential && saveAuthFromCredential(resp.credential)) {
            finish(true);
          } else {
            finish(false);
          }
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

    // Called on page load (after GIS loads) and on sync 401. Attempts to
    // refresh the id_token silently; on success the new token is in
    // localStorage and any in-page UI that re-checks configured() will
    // see it as signed-in again.
    function tryRestoreSession(cb) {
      cb = cb || function(){};
      var raw = loadAuth();
      if (!raw.id_token) { cb(false); return; }
      // Token still valid → nothing to do.
      if (raw.exp && Date.now() < raw.exp - 60000) { cb(true); return; }
      silentReAuth(function(ok) {
        if (ok) {
          refreshAuthUI();
          // startSync already ran in local-mode (token had expired by the
          // time it checked). Now that we have a fresh token, kick a
          // sync — push if there's a pending local change, pull otherwise
          // — so the user doesn't sit in "本地模式" until the 60s poll.
          if (dirty) push(); else pull();
        }
        cb(ok);
      });
    }

    // Boot-time silent re-auth. Waits for GIS to load (same retry budget
    // as renderSignInButton — ~3 s), then attempts to refresh. Mostly
    // invisible: succeeds silently on single-account browsers, no-ops if
    // the token is still fresh, and triggers the One Tap chooser only
    // for multi-account users with an expired token.
    var bootSilentAttempt = 0;
    function bootSilentReAuth() {
      if (!window.google || !google.accounts || !google.accounts.id) {
        if (bootSilentAttempt > 30) return;
        bootSilentAttempt++;
        setTimeout(bootSilentReAuth, 100);
        return;
      }
      tryRestoreSession();
    }
    // Only attempt if we previously had a session; first-time visitors
    // shouldn't see an unsolicited One Tap toast on page load.
    if (loadAuth().id_token) {
      setTimeout(bootSilentReAuth, 0);
    }

    // Lazy-render Google's official sign-in button into the modal. Called on
    // every openModal so we tolerate the GIS script still loading on first
    // open — if it isn't ready yet, retry up to ~3s before giving up.
    var gisRendered = false;
    var gisRetryTimer = null;
    function renderSignInButton(attempt) {
      attempt = attempt || 0;
      if (gisRendered) return;
      if (!window.google || !google.accounts || !google.accounts.id) {
        if (attempt > 30) {
          cfgMsg.style.color = '#dc2626';
          cfgMsg.textContent = 'Google 登录脚本未加载（检查网络）';
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

    function refreshAuthUI() {
      var a = configured();
      if (a) {
        authOut.style.display = 'none';
        authIn.style.display  = 'block';
        authPic.src = a.picture || '';
        authName.textContent  = a.name || '';
        authEmail.textContent = a.email || '';
      } else {
        authOut.style.display = 'block';
        authIn.style.display  = 'none';
        renderSignInButton();
      }
    }

    function openModal() {
      cfgMsg.textContent = '';
      refreshAuthUI();
      modalBg.style.display = 'flex';
    }
    function closeModal() { modalBg.style.display = 'none'; }

    document.getElementById('ff-settings').addEventListener('click', openModal);
    document.getElementById('ff-cfg-cancel').addEventListener('click', closeModal);
    modalBg.addEventListener('click', function(e) {
      if (e.target === modalBg) closeModal();
    });

    document.getElementById('ff-signout').addEventListener('click', function() {
      if (!confirm('退出登录？本设备上的本地缓存会保留，但不再同步到云端。')) return;
      signOut();
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
    // localStorage; reflect it into the dropdown so the UI matches state.
    // On change: persist, update the URL (?lang=tw is sticky, default
    // simplified drops the param) so deep links carry the language, then
    // reload so the page reboots in the new language. We reload rather
    // than live-convert because keeping every text node's pre-conversion
    // value cached just to support a rare toggle isn't worth the memory.
    var langEl = document.getElementById('ff-lang');
    if (langEl) {
      langEl.value = activeLang;
      langEl.addEventListener('change', function() {
        var v = langEl.value;
        try { localStorage.setItem(LANG_KEY, v); } catch (_) {}
        var url = new URL(window.location.href);
        if (v === 'zh-TW') url.searchParams.set('lang', 'tw');
        else if (v === 'en') url.searchParams.set('lang', 'en');
        else url.searchParams.delete('lang');
        window.location.assign(url.toString());
      });
    }

    restoreFilterState();
    updateFavCount();
    updateBlackCount();
    apply();
    // Kick off the first pull (or stay in local mode if not configured).
    startSync();
  }
  function boot() {
    function setTotals(text) {
      var nodes = document.querySelectorAll('.ff-total');
      for (var i = 0; i < nodes.length; i++) nodes[i].textContent = text;
    }
    setTotals('加载中…');
    fetch('data/restaurants.json', {cache: 'force-cache'})
      .then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function(data) { initMap(data); })
      .catch(function(e) {
        console.error('[tabelog] restaurants.json load failed:', e);
        setTotals('加载失败');
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


def write_csv_with_coords(rows: list[dict], fieldnames: list[str]) -> None:
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

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
        loc = geocode(addr, client, cache)
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
        if i % 5 == 0:
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

    # CartoDB Voyager as the single base — clean Google-Maps-style. The
    # transit option lives in custom JS as a togglable OpenRailwayMap
    # overlay (rail lines + station markers drawn on top), wired to the
    # floating "🚇 公共交通" pill button (see FAB_HTML / initMap).
    m = folium.Map(location=JAPAN_CENTER, zoom_start=6, tiles=None, zoom_control=False)
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
        attr=(
            '&copy; <a href="https://www.openstreetmap.org/copyright">'
            "OpenStreetMap</a> contributors &copy; "
            '<a href="https://carto.com/attributions">CARTO</a>'
        ),
        name="公路 (CartoDB Voyager)",
        max_zoom=19,
        subdomains="abcd",
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
            popups_map[url] = popup_data(row)
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
    RESTAURANTS_JSON.write_bytes(restaurants_bytes)
    POPUPS_JSON.write_bytes(popups_bytes)
    popups_tw_map = {url: trad_popup_array(arr) for url, arr in popups_map.items()}
    popups_tw_bytes = json.dumps(
        popups_tw_map, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    POPUPS_TW_JSON.write_bytes(popups_tw_bytes)
    policy_en = load_policy_en()
    popups_en_map = {
        url: en_popup_array(arr, policy_en.get(url)) for url, arr in popups_map.items()
    }
    popups_en_bytes = json.dumps(
        popups_en_map, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    POPUPS_EN_JSON.write_bytes(popups_en_bytes)
    translated_count = sum(1 for url in popups_map if policy_en.get(url, "").strip())
    policy_ja = load_policy_ja()
    popups_ja_map = {
        url: ja_popup_array(arr, policy_ja.get(url)) for url, arr in popups_map.items()
    }
    popups_ja_bytes = json.dumps(
        popups_ja_map, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    POPUPS_JA_JSON.write_bytes(popups_ja_bytes)
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

    panel_html = build_filter_panel_html(cat_counts, award_counts)
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
    )
    # Service worker — version-stamp the cache name so each redeploy
    # invalidates the previous one. Unix seconds is plenty granular for a
    # personal site rebuilt by hand.
    build_version = str(int(time.time()))
    SW_JS.write_text(
        SW_JS_TEMPLATE.replace("__BUILD_VERSION__", build_version),
        encoding="utf-8",
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
    m.get_root().html.add_child(folium.Element(panel_html))
    m.get_root().html.add_child(folium.Element(filter_js))

    m.save(str(OUT_HTML))
    # Second pass over the saved file: scan every CJK run that ended up
    # on the page (static UI, bucket names, attraction labels, AND the
    # Chinese string literals inside the inlined <script> blocks — those
    # produce text nodes too once the JS that builds them runs), feed
    # each to full OpenCC s2t, and inject the {simp: trad} map. The JS
    # side reads this at runtime to do precise per-segment conversion;
    # we ship the precomputed answers instead of a converter so the
    # browser doesn't need to load OpenCC's dictionaries.
    saved_html = OUT_HTML.read_text(encoding="utf-8")
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
    OUT_HTML.write_text(saved_html, encoding="utf-8")
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


if __name__ == "__main__":
    main()
