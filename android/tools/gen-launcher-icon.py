#!/usr/bin/env python3
"""Regenerate the launcher-icon foreground bitmaps from the SITE's own icon.

    uv run python android/tools/gen-launcher-icon.py

(`uv run` because Pillow is a dependency of the site's pipeline, not of the system python.)

Source: docs/icons/icon-japan-emoji-v2-maskable-512.png — the maskable PWA icon, 🗾 on
#fdf6e3. Its artwork occupies the middle 320 of 512 px (62.5 %). Re-run this if the site's
icon changes; it is the only step that copies pixels from the site into the APK.

Output, at the five densities of a 108dp canvas:

  mipmap-*/ic_launcher_foreground.png   the artwork inset to FG_SCALE (see below)
  mipmap-*/ic_splash_foreground.png     the artwork at full canvas size

PNG rather than a vector because the source is a photograph of an emoji, not geometry —
vectorising it is neither possible nor wanted.

Why the launcher icon is inset (2.1.0, bug A-2). The 66.7 % an adaptive icon guarantees is
visible under any mask is a FLOOR — "smaller than this and nothing can clip it" — not a
size to draw at. Material's typography baseline for an adaptive icon is the old 48dp icon's
38dp square scaled by 1.5 = 57dp of a 108dp canvas (52.8 %). Drawing the source at full
canvas size put the artwork at 62.5 % of 108dp = 67.5dp, and both launchers measured showed
it filling ~82 % of the visible circle: on Pixel's round mask the white border of the map glyph
is sliced off, which reads as a photo cropped into a circle rather than an icon. At
FG_SCALE the artwork lands on 62.5 % × 0.76 = 47.5 % of the canvas (≈51dp), which is what
Chrome's WebAPK generator produces from the same source (measured 47.4 %) — so the sideloaded
icon and an installed PWA of the site look the same size side by side, which is the
acceptance criterion Fred gave.

Why the splash keeps its own copy at full size. res/drawable/ic_splash.xml used to point at
the launcher foreground, which is what kept the two from drifting; the platform draws the
splash icon on a 288dp canvas expecting the artwork inside a 192dp circle, and the full-size
bitmap puts it at 180dp — right. Inset to 0.76 it would be 137dp, visibly small. So the two
sizes are decoupled but still come out of THIS script from THAT source in one run, which is
all the "cannot drift" was ever buying.
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

# How much of the 108dp foreground canvas the source image is drawn into, for the LAUNCHER
# icon. The source's own artwork is 62.5 % of it, so the artwork ends up at 47.5 % of the
# canvas — see the module docstring for where that number comes from. The splash uses 1.0.
FG_SCALE = 0.76


def _render(im: Image.Image, px: int, scale: float) -> Image.Image:
    """The source, resized to `px * scale`, centred on a `px` square of its own background.

    The pad colour is taken from the source's own corner pixel rather than from
    @color/ic_launcher_background: they are the same beige today, and if the site ever
    changes its icon this keeps the foreground seamless against itself. The background layer
    still shows through nothing — the foreground is opaque edge to edge, which is deliberate
    (the glyph has a soft shadow over the beige and any alpha extraction leaves a halo).
    """
    if scale >= 1.0:
        # LANCZOS on a downscale of a soft-edged emoji; the upscale case (mdpi source,
        # which cannot happen with a 512 px original) would be the same call.
        return im.resize((px, px), Image.LANCZOS)
    inner = max(1, round(px * scale))
    canvas = Image.new("RGBA", (px, px), im.getpixel((0, 0)))
    canvas.paste(im.resize((inner, inner), Image.LANCZOS), ((px - inner) // 2,) * 2)
    return canvas


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
        for name, scale in (("ic_launcher_foreground", FG_SCALE), ("ic_splash_foreground", 1.0)):
            dst = d / f"{name}.png"
            _render(im, px, scale).save(dst, optimize=True)
            print(f"wrote {dst.relative_to(HERE.parent)}  ({px}x{px}, scale {scale})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
