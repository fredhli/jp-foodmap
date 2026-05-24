"""Pre-download Apple-style emoji PNGs into docs/emoji/ so the runtime page
doesn't hit emojicdn.elk.sh on every marker render. Visual style is
identical (same PNGs, same CDN — just fetched once at build time instead
of N times per visitor). Emit a manifest map.py inlines as a JS lookup.

Run with:  uv run python src/tabelog/scrape/build_emoji_cache.py
Idempotent — files already on disk are skipped.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tabelog.paths import DATA, DOCS_DIR, FAVORITES_BUILTIN_JSON  # noqa: E402
from tabelog.scrape.map_data import GENRE_EMOJI  # noqa: E402

EMOJI_DIR = DOCS_DIR / "emoji"
MANIFEST = EMOJI_DIR / "_manifest.json"
MAP_PY = Path(__file__).parent / "map.py"

EMOJICDN = "https://emojicdn.elk.sh/{}?style=apple"

# Mirrors the JS EMOJI_RE in map.py: flag pairs, BMP-plane symbols, and the
# 1F000-1FAFF block where most pictographs live. Best-effort — anything we
# miss falls through to the runtime CDN fallback in emojiImg().
EMOJI_RE = re.compile(
    r"(?:[\U0001F1E6-\U0001F1FF][\U0001F1E6-\U0001F1FF])"
    r"|(?:[\U0001F300-\U0001FAFF\U00002600-\U000027BF"
    r"\U0001F000-\U0001F02F\U0001F0A0-\U0001F0FF]️?)"
)


def emoji_filename(emoji: str) -> str:
    """All codepoints lowercase-hex, dash-joined. Same emoji char becomes
    the same filename in Python and in the JS lookup."""
    return "-".join(f"{ord(c):x}" for c in emoji) + ".png"


def collect_emojis() -> set[str]:
    """Union of every emoji that can land on the rendered page. Structured
    sources (GENRE_EMOJI, the `emoji` field on built-in pins) are read
    directly; everything else is harvested by regex from text payloads —
    popup JSON, help markdown, i18n tables — so a new emoji shipped via
    any of those gets pre-cached the next time the script runs."""
    out: set[str] = set()
    out.update(GENRE_EMOJI.values())
    for entry in json.loads(FAVORITES_BUILTIN_JSON.read_text(encoding="utf-8")):
        e = entry.get("emoji")
        if e:
            out.add(e)

    text_targets: list[Path] = [MAP_PY]
    text_targets.extend(sorted((DOCS_DIR / "data").glob("popups*.json")))
    text_targets.extend(sorted((DOCS_DIR / "help").glob("*.md")))
    i18n_dir = DATA / "i18n"
    if i18n_dir.exists():
        text_targets.extend(sorted(i18n_dir.glob("*.json")))

    for path in text_targets:
        if not path.exists():
            continue
        out.update(EMOJI_RE.findall(path.read_text(encoding="utf-8")))

    out.discard("")
    return out


def download_missing(emojis: set[str]) -> tuple[set[str], int, list[str]]:
    """Returns (cached_emojis, downloaded_count, failed_descriptions).
    cached_emojis is what the manifest should contain — chars whose PNG
    is actually on disk. Failures (e.g. ★ ✓ which aren't real emoji and
    have no Apple glyph) are dropped from the manifest so emojiImg()
    skips the local path and lets the system font render them."""
    EMOJI_DIR.mkdir(parents=True, exist_ok=True)
    cached: set[str] = set()
    dl = 0
    failed: list[str] = []
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        for e in sorted(emojis):
            target = EMOJI_DIR / emoji_filename(e)
            if target.exists() and target.stat().st_size > 0:
                cached.add(e)
                continue
            try:
                r = client.get(EMOJICDN.format(e))
                r.raise_for_status()
                target.write_bytes(r.content)
                cached.add(e)
                dl += 1
                time.sleep(0.05)
            except Exception as exc:
                failed.append(f"{emoji_filename(e)} ({exc!r})")
    return cached, dl, failed


def write_manifest(emojis: set[str]) -> None:
    """Map emoji char -> hex stem (no .png suffix). map.py inlines this
    so emojiImg() can decide local vs. CDN fallback per char."""
    m = {e: emoji_filename(e).removesuffix(".png") for e in sorted(emojis)}
    MANIFEST.write_text(
        json.dumps(m, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    emojis = collect_emojis()
    print(f"Collected {len(emojis)} unique emoji.")
    cached, dl, failed = download_missing(emojis)
    print(f"Cached {len(cached)} ({dl} new this run), failed {len(failed)}.")
    for f in failed:
        print(f"  ! {f}")
    write_manifest(cached)
    rel = MANIFEST.relative_to(DOCS_DIR.parent)
    print(f"Wrote manifest -> {rel} ({len(cached)} entries)")


if __name__ == "__main__":
    main()
