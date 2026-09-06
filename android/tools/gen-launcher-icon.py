#!/usr/bin/env python3
"""Regenerate the launcher-icon foreground bitmaps from the SITE's own icon.

    uv run python android/tools/gen-launcher-icon.py

(`uv run` because Pillow is a dependency of the site's pipeline, not of the system python.)

Source: docs/icons/icon-japan-emoji-v2-maskable-512.png — the maskable PWA icon, 🗾 on
#fdf6e3. Its artwork occupies the middle 320 of 512 px (62.5 %), which already sits inside
the 66.7 % that an adaptive icon guarantees is visible under any launcher mask, so the
image is written at full canvas size with no further inset. Re-run this if the site's icon
changes; it is the only step that copies pixels from the site into the APK.

Output: app/src/main/res/mipmap-{mdpi..xxxhdpi}/ic_launcher_foreground.png at the five
densities of a 108dp canvas. PNG rather than a vector because the source is a photograph of
an emoji, not geometry — vectorising it is neither possible nor wanted.

Why one bitmap serves both the icon and the splash: res/drawable/ic_splash.xml is a
<bitmap> pointing at this same resource, so the app icon and the splash icon cannot drift.
"""
import pathlib
import sys

from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent            # android/tools
REPO = HERE.parents[1]                                    # repo root
SRC = REPO / "docs/icons/icon-japan-emoji-v2-maskable-512.png"
OUT = HERE.parent / "app/src/main/res"

# 108dp foreground canvas at each density bucket: mdpi is 1x = 108 px.
DENSITIES = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}


def main() -> int:
    src = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else SRC
    if not src.is_file():
        raise SystemExit(f"no source icon at {src}")
    im = Image.open(src).convert("RGBA")
    if im.width != im.height:
        raise SystemExit(f"{src} is {im.width}x{im.height}; a launcher icon must be square")
    for bucket, px in DENSITIES.items():
        d = OUT / f"mipmap-{bucket}"
        d.mkdir(parents=True, exist_ok=True)
        dst = d / "ic_launcher_foreground.png"
        # LANCZOS on a downscale of a soft-edged emoji; the upscale case (mdpi source,
        # which cannot happen with a 512 px original) would be the same call.
        im.resize((px, px), Image.LANCZOS).save(dst, optimize=True)
        print(f"wrote {dst.relative_to(HERE.parent)}  ({px}x{px})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
