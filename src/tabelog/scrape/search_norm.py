"""Per-character canonicalization for the search box.

Restaurant names are Japanese (shinjitai kanji + kana). Users typing in the
search box use simplified Chinese. To make "烧肉" match "焼肉ジャンボ" we
canonicalize both sides to one form (simplified Chinese), via the chain
JP shinjitai → Traditional → Simplified.

The Traditional → Simplified step uses OpenCC's t2s config. The JP →
Traditional step uses a copy of OpenCC's JPVariants.txt that lives
alongside this module (the opencc-python-reimplemented package ships t2s
but not jp2t, so we load the file ourselves).

This module is build-only: at render time we (1) compute the canonical
form of each name char to know which canonicals matter, then (2) build a
small variant→canonical table that we ship inline in the JS so the
browser can canonicalize the user's query the same way."""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path

from opencc import OpenCC

JP_VARIANTS_TXT = Path(__file__).with_name("jp_variants.txt")


@lru_cache(maxsize=1)
def _jp_to_trad() -> dict[str, str]:
    """JP shinjitai → Traditional. First trad listed in JPVariants.txt wins
    when a jp char has multiple trad candidates."""
    out: dict[str, str] = {}
    for line in JP_VARIANTS_TXT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        trad = parts[0].strip()
        for jp in parts[1].split():
            out.setdefault(jp, trad)
    return out


@lru_cache(maxsize=1)
def _t2s() -> OpenCC:
    return OpenCC("t2s")


@lru_cache(maxsize=1)
def _t2s_single_char_dict() -> dict[str, str]:
    """Single-char slice of t2s's compiled chain. Values can be 'a b'
    (space-separated alternatives) — caller takes the first."""
    chain = _t2s()._dict_chain_data[0]
    for max_len, _min_len, d in chain:
        if max_len == 1 and isinstance(d, dict):
            return d
    return {}


def canon_char(c: str) -> str:
    """jp/trad/simp → simp. Identity for chars with no known variant."""
    after_jp = _jp_to_trad().get(c, c)
    out = _t2s().convert(after_jp)
    return out[:1] if out else c


def canon_str(s: str) -> str:
    """NFKC + casefold + per-char canonicalization. Matches the JS-side
    `normalizeForSearch` exactly so build-time and runtime stay in sync."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return "".join(canon_char(c) for c in s)


def build_han_variants(canonical_chars: set[str]) -> dict[str, str]:
    """Variant → canonical, restricted to variants of chars that actually
    appear (in canonical form) in any restaurant name. Keeps the JSON we
    inline in the page small (~order of hundreds, not thousands)."""
    out: dict[str, str] = {}
    jp2t = _jp_to_trad()
    t2s_chars = _t2s_single_char_dict()
    # JP shinjitai + traditional siblings
    for jp_c, trad_c in jp2t.items():
        target = canon_char(jp_c)
        if target not in canonical_chars:
            continue
        if jp_c != target:
            out[jp_c] = target
        if trad_c != target:
            out.setdefault(trad_c, target)
    # All other trad → simp entries
    for trad_c, simp_field in t2s_chars.items():
        target = simp_field.split(" ", 1)[0]
        if target in canonical_chars and trad_c != target:
            out.setdefault(trad_c, target)
    return out
