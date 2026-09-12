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

try:  # M-060 — optional; see _resample()
    from PIL import Image
except Exception:  # pragma: no cover - environment without Pillow
    Image = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tabelog.paths import DATA, DOCS_DIR, FAVORITES_BUILTIN_JSON, UI_DIR  # noqa: E402
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
    # 2B00-2BFF: Misc Symbols and Arrows — ⭐ (2B50) lives here and was
    # slipping through to the emojicdn runtime fallback despite being the
    # favorites badge on every starred marker.
    r"\U00002B00-\U00002BFF"
    r"\U0001F000-\U0001F02F\U0001F0A0-\U0001F0FF]️?)"
)


def emoji_stem(emoji: str) -> str:
    """All codepoints lowercase-hex, dash-joined. Same emoji char becomes
    the same filename in Python and in the JS lookup."""
    return "-".join(f"{ord(c):x}" for c in emoji)


def emoji_filename(emoji: str) -> str:
    """The as-downloaded 160px file. Still written (it is the local source
    cache for the resample below) and NEVER deleted — an old index.html
    sitting in some visitor's service-worker cache still points at it."""
    return emoji_stem(emoji) + ".png"


# M-060: emojicdn serves 160x160. The page draws these at 11-24 CSS px, so
# even a DPR-3 phone never needs more than ~72 device px — 130 files x 160px
# was 2.82 MB on disk and ~2.16 MB of first-visit traffic (about 45% of it).
# 64px is the smallest power-of-two above that ceiling.
TARGET_PX = 64
SMALL_SUFFIX = f"-{TARGET_PX}"


def small_filename(emoji: str) -> str:
    return f"{emoji_stem(emoji)}{SMALL_SUFFIX}.png"


def _resample(src: Path, dst: Path) -> bool:
    """160px -> TARGET_PX, LANCZOS, optimized PNG. Returns False when the
    resample is unavailable or fails; the caller then keeps pointing the
    manifest at the original file, which is a size regression but never a
    broken image."""
    if Image is None:
        return False
    try:
        with Image.open(src) as im:
            im = im.convert("RGBA")
            if im.width <= TARGET_PX and im.height <= TARGET_PX:
                dst.write_bytes(src.read_bytes())
                return True
            im.resize((TARGET_PX, TARGET_PX), Image.LANCZOS).save(
                dst, format="PNG", optimize=True
            )
        return True
    except Exception as exc:  # noqa: BLE001 - any decode/encode failure
        print(f"  ! resample failed for {src.name}: {exc!r}")
        if dst.exists():
            dst.unlink(missing_ok=True)
        return False


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
    # 4.0.0: the front end moved out of map.py into src/tabelog/ui/. Every
    # emoji a module writes lives in one of these files now, so without them
    # this script would keep pre-caching only the 3.2.x set and every new
    # glyph would fall through to the emojicdn runtime fetch on first render.
    if UI_DIR.exists():
        text_targets.append(UI_DIR / "shell.html")
        for sub, pattern in (("css", "*.css"), ("js", "*.js"), ("i18n", "*.json")):
            text_targets.extend(sorted((UI_DIR / sub).glob(pattern)))

    for path in text_targets:
        if not path.exists():
            continue
        out.update(EMOJI_RE.findall(path.read_text(encoding="utf-8")))

    out.discard("")
    return out


def download_missing(emojis: set[str]) -> tuple[dict[str, str], int, int, list[str]]:
    """Returns (char -> manifest stem, downloaded_count, resampled_count,
    failed_descriptions). The returned mapping is exactly what the manifest
    should contain — chars whose PNG is actually on disk. Failures (e.g.
    ★ ✓ which aren't real emoji and have no Apple glyph) are dropped from the
    manifest so emojiImg() skips the local path and lets the system font
    render them.

    M-060: each char now resolves to its 64px derivative when one could be
    produced, and falls back to the original 160px stem otherwise, so a build
    machine without Pillow still ships a complete, working manifest."""
    EMOJI_DIR.mkdir(parents=True, exist_ok=True)
    if Image is None:
        print(
            "  ! WARNING: Pillow not importable — emoji PNGs stay at the "
            "downloaded 160px size. Run `uv sync` to install it."
        )
    cached: dict[str, str] = {}
    dl = 0
    resampled = 0
    failed: list[str] = []
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        for e in sorted(emojis):
            target = EMOJI_DIR / emoji_filename(e)
            if not (target.exists() and target.stat().st_size > 0):
                try:
                    r = client.get(EMOJICDN.format(e))
                    r.raise_for_status()
                    target.write_bytes(r.content)
                    dl += 1
                    time.sleep(0.05)
                except Exception as exc:
                    failed.append(f"{emoji_filename(e)} ({exc!r})")
                    continue
            small = EMOJI_DIR / small_filename(e)
            if small.exists() and small.stat().st_size > 0:
                cached[e] = small.stem
            elif _resample(target, small):
                resampled += 1
                cached[e] = small.stem
            else:
                cached[e] = target.stem
    return cached, dl, resampled, failed


def carry_forward(stems: dict[str, str]) -> dict[str, str]:
    """Union the freshly collected stems with the entries already in the
    manifest whose PNG is still on disk.

    A char drops out of collect_emojis() the moment the last text that
    mentioned it changes — a re-scraped popups*.json, a reworded module
    string. Without this the manifest would lose the entry while the PNG
    stayed (files here are never deleted, M-060), and the page would start
    paying an emojicdn round-trip for a glyph it already ships. Entries are
    only carried, never invented: a stem whose file is gone is dropped."""
    out = dict(stems)
    if not MANIFEST.exists():
        return out
    try:
        prior = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return out
    for char, stem in prior.items():
        if char in out or not isinstance(stem, str):
            continue
        if (EMOJI_DIR / f"{stem}.png").exists():
            out[char] = stem
    return out


def write_manifest(stems: dict[str, str]) -> None:
    """Map emoji char -> hex stem (no .png suffix). map.py inlines this
    so emojiImg() can decide local vs. CDN fallback per char. Changing a
    stem is safe on the wire: docs/_headers marks /emoji/*.png immutable,
    and the old file is still there for pages served from an old cache."""
    m = {e: stems[e] for e in sorted(stems)}
    MANIFEST.write_text(
        json.dumps(m, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    emojis = collect_emojis()
    print(f"Collected {len(emojis)} unique emoji.")
    cached, dl, resampled, failed = download_missing(emojis)
    print(
        f"Cached {len(cached)} ({dl} new this run, {resampled} resampled to "
        f"{TARGET_PX}px), failed {len(failed)}."
    )
    for f in failed:
        print(f"  ! {f}")
    small_bytes = sum(
        (EMOJI_DIR / f"{s}.png").stat().st_size
        for s in cached.values()
        if (EMOJI_DIR / f"{s}.png").exists()
    )
    print(f"Manifest payload: {small_bytes:,} B across {len(cached)} files")
    final = carry_forward(cached)
    carried = len(final) - len(cached)
    if carried:
        print(f"Carried forward {carried} manifest entry/entries no longer collected")
    write_manifest(final)
    rel = MANIFEST.relative_to(DOCS_DIR.parent)
    print(f"Wrote manifest -> {rel} ({len(final)} entries)")


if __name__ == "__main__":
    main()
