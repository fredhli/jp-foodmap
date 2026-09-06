#!/usr/bin/env python3
"""Coarse pixel statistics for one screenshot — "is there a map under there?".

    png-stats.py shot.png                       # JSON on stdout
    png-stats.py shot.png --crop 0,0.12,1,0.88  # only that fraction of the frame
    png-stats.py shot.png --assert-map          # exit 1 when the region looks unpainted

WHY. docs/STANDARDS.md §3.3 asks for "no grey block after a geometry change", and the only
evidence a headless box has is the frame itself. A repainted Leaflet map is thousands of
colours; an unpainted one is one flat colour over a big area — Leaflet's own #ddd, the
shell's #fdf6e3 window background, or white. So the test is not "which grey" but "how flat":
the share of the single most common colour inside the sampled region, plus how many distinct
colours there are at all. Both are reported; --assert-map turns them into an exit code.

No Pillow on this box, so the PNG is decoded here: zlib + the five row filters, 8-bit,
colour type 0/2/4/6, non-interlaced — which is everything `adb screencap -p` and the
emulator console's own screenshot produce. Sampling is by stride AFTER unfiltering (the
filters are cumulative row to row, so no row can be skipped) — the cost is ~2 s for a
1248x1972 frame, which is why the verify scripts call it on the frames they assert on
rather than on all of them.
"""

import argparse
import json
import struct
import sys
import zlib


def decode_png(path):
    """-> (width, height, channels, bytearray of raw samples). 8-bit only."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG (adb screencap can emit its abort message instead)")
    pos, idat, w, h, ch, depth = 8, [], 0, 0, 0, 0
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln  # length + type + data + crc
        if typ == b"IHDR":
            w, h, depth, colour, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8:
                raise ValueError("bit depth %d is not supported" % depth)
            if interlace:
                raise ValueError("interlaced PNG is not supported")
            ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
            if ch is None or colour == 3:
                raise ValueError("colour type %d is not supported" % colour)
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    raw = zlib.decompress(b"".join(idat))
    stride = w * ch
    out = bytearray(stride * h)
    prev = bytearray(stride)
    src = 0
    for y in range(h):
        ft = raw[src]
        src += 1
        line = bytearray(raw[src:src + stride])
        src += stride
        if ft == 1:  # Sub
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif ft == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:  # Average
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:  # Paeth
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        elif ft != 0:
            raise ValueError("bad row filter %d on row %d" % (ft, y))
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, ch, out


def stats(path, crop, stride):
    w, h, ch, px = decode_png(path)
    x0 = max(0, min(w - 1, int(crop[0] * w)))
    y0 = max(0, min(h - 1, int(crop[1] * h)))
    x1 = max(x0 + 1, min(w, int(crop[2] * w)))
    y1 = max(y0 + 1, min(h, int(crop[3] * h)))
    rowb = w * ch
    counts = {}
    n = 0
    for y in range(y0, y1, stride):
        base = y * rowb
        for x in range(x0, x1, stride):
            i = base + x * ch
            if ch >= 3:
                key = (px[i] << 16) | (px[i + 1] << 8) | px[i + 2]
            else:
                v = px[i]
                key = (v << 16) | (v << 8) | v
            counts[key] = counts.get(key, 0) + 1
            n += 1
    if not n:
        raise ValueError("empty crop")
    dom_key, dom_n = max(counts.items(), key=lambda kv: kv[1])
    neutral = grey = 0
    for key, c in counts.items():
        r, g, b = (key >> 16) & 255, (key >> 8) & 255, key & 255
        if max(r, g, b) - min(r, g, b) <= 8 and r >= 190:
            neutral += c
        # Leaflet paints #ddd where a tile has not arrived. That exact neutral is what a
        # "grey block" IS, and it is not a colour any Positron tile or any of this page's
        # chrome uses — so its share is a much sharper signal than overall flatness (a
        # legitimate frame of open sea is 60 % one blue).
        if abs(r - 221) <= 6 and abs(g - 221) <= 6 and abs(b - 221) <= 6:
            grey += c
    # A 3x3 read of the same region, as evidence rather than as an assertion: a grey block
    # in one corner barely moves the whole-region numbers but empties one cell.
    cells = []
    for gy in range(3):
        for gx in range(3):
            cx0 = x0 + (x1 - x0) * gx // 3
            cx1 = x0 + (x1 - x0) * (gx + 1) // 3
            cy0 = y0 + (y1 - y0) * gy // 3
            cy1 = y0 + (y1 - y0) * (gy + 1) // 3
            cc = {}
            for y in range(cy0, cy1, stride):
                base = y * rowb
                for x in range(cx0, cx1, stride):
                    i = base + x * ch
                    k = ((px[i] << 16) | (px[i + 1] << 8) | px[i + 2]) if ch >= 3 else px[i]
                    cc[k] = cc.get(k, 0) + 1
            cells.append(len(cc))
    return {
        "file": path,
        "size": [w, h],
        "crop_px": [x0, y0, x1, y1],
        "samples": n,
        "distinct": len(counts),
        "dominant": "#%06x" % dom_key,
        "dominant_share": round(dom_n / n, 4),
        "light_neutral_share": round(neutral / n, 4),
        "leaflet_grey_share": round(grey / n, 4),
        "cell_distinct": cells,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("png")
    ap.add_argument("--crop", default="0,0,1,1",
                    help="x0,y0,x1,y1 as fractions of the frame (default the whole frame)")
    ap.add_argument("--stride", type=int, default=4, help="sample every Nth pixel (default 4)")
    ap.add_argument("--assert-map", action="store_true",
                    help="exit 1 when the region looks flat/unpainted")
    ap.add_argument("--max-flat", type=float, default=0.90,
                    help="highest share one colour may hold under --assert-map")
    ap.add_argument("--min-distinct", type=int, default=200,
                    help="fewest distinct colours the region may hold under --assert-map")
    ap.add_argument("--max-grey", type=float, default=0.15,
                    help="highest share of Leaflet's #ddd no-tile grey under --assert-map")
    a = ap.parse_args()
    try:
        crop = tuple(float(v) for v in a.crop.split(","))
        if len(crop) != 4:
            raise ValueError
    except ValueError:
        print("png-stats: --crop wants x0,y0,x1,y1", file=sys.stderr)
        return 2
    try:
        s = stats(a.png, crop, max(1, a.stride))
    except Exception as exc:  # noqa: BLE001
        print("png-stats: %s" % exc, file=sys.stderr)
        return 2
    verdict = "ok"
    if a.assert_map:
        if (s["dominant_share"] > a.max_flat
                or s["distinct"] < a.min_distinct
                or s["leaflet_grey_share"] > a.max_grey):
            verdict = "flat"
    s["verdict"] = verdict
    print(json.dumps(s, ensure_ascii=False))
    return 1 if verdict == "flat" else 0


if __name__ == "__main__":
    sys.exit(main())
