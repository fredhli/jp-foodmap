#!/usr/bin/env python3
"""Download the front-end dependencies into docs/vendor/ (M-006 / M-011 / BUG-01).

The page used to load Leaflet + emoji-picker-element from cdn.jsdelivr.net and
MarkerCluster + the locate plugin from cdnjs.cloudflare.com. That is three
things at once:

  * an availability dependency — the 02 audit reproduced a DNS-level cdnjs
    outage and got `L.markerClusterGroup is not a function`, zero markers and
    a stuck "loading" counter;
  * a supply-chain dependency — no SRI anywhere, so whatever those hosts
    served executed with full same-origin rights (it can read
    localStorage['tabelog.auth'] and talk to /api/state with the session
    cookie);
  * a privacy dependency — every visitor's IP was handed to two extra CDNs.

Self-hosting removes all three and lets a strict-ish CSP name 'self' instead
of two wildcard script hosts.

Versions are pinned to EXACTLY what production was already loading. This
script does not upgrade anything: MarkerCluster in particular is load-bearing
for the duck-typed FAB layer detection described in CLAUDE.md, so a version
bump needs its own regression pass.

Layout: the version lives in the directory name
(``docs/vendor/leaflet-1.9.3/…``) so ``/vendor/*`` can be served
``immutable`` honestly — an upgrade changes the URL. leaflet.css references
``images/*.png`` relatively, so those files are kept in the same tree.

Usage:
    uv run python scripts/fetch_vendor.py            # fetch + verify
    uv run python scripts/fetch_vendor.py --print    # print hashes, no write
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tabelog.paths import DOCS_DIR  # noqa: E402

VENDOR_DIR = DOCS_DIR / "vendor"

# (local path under docs/vendor, source URL, sha256 of the upstream bytes).
# The hash is verified on every run — a CDN that starts serving different
# bytes for a pinned, immutable path is exactly the event this guards against.
# The pins were cross-checked against unpkg/cdnjs before being written here —
# three independent origins agreed on every JS bundle.
# `None` means "not pinned yet"; run with --print and paste the value in.
ASSETS: list[tuple[str, str, str | None]] = [
    # ---- leaflet 1.9.3 (was cdn.jsdelivr.net) -----------------------------
    (
        "leaflet-1.9.3/leaflet.js",
        "https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js",
        "5819285cec137b229c94e1ee5ad73e8b6b84345a4367d60f75fe477fe0fb7b03",
    ),
    (
        "leaflet-1.9.3/leaflet.js.map",
        "https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js.map",
        "a7a6c324c430b0a049d0a86862b18400583daa4dc10fbe55835cb492baa9fdc1",
    ),
    (
        "leaflet-1.9.3/leaflet.css",
        "https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.css",
        "90b693d86392a4779c861b28cf307e7e59c3fb35328c4d8b95f58f814d38c722",
    ),
    (
        "leaflet-1.9.3/images/layers.png",
        "https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/images/layers.png",
        "1dbbe9d028e292f36fcba8f8b3a28d5e8932754fc2215b9ac69e4cdecf5107c6",
    ),
    (
        "leaflet-1.9.3/images/layers-2x.png",
        "https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/images/layers-2x.png",
        "066daca850d8ffbef007af00b06eac0015728dee279c51f3cb6c716df7c42edf",
    ),
    (
        "leaflet-1.9.3/images/marker-icon.png",
        "https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/images/marker-icon.png",
        "574c3a5cca85f4114085b6841596d62f00d7c892c7b03f28cbfa301deb1dc437",
    ),
    (
        "leaflet-1.9.3/images/marker-icon-2x.png",
        "https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/images/marker-icon-2x.png",
        "00179c4c1ee830d3a108412ae0d294f55776cfeb085c60129a39aa6fc4ae2528",
    ),
    (
        "leaflet-1.9.3/images/marker-shadow.png",
        "https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/images/marker-shadow.png",
        "264f5c640339f042dd729062cfc04c17f8ea0f29882b538e3848ed8f10edb4da",
    ),
    # ---- leaflet.markercluster 1.1.0 (was cdnjs.cloudflare.com) -----------
    (
        "leaflet.markercluster-1.1.0/leaflet.markercluster.js",
        "https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.1.0/leaflet.markercluster.js",
        "7e1a592d5bf4b1698700edecc0ac091166d41d3c31373611627b4e7b9faef067",
    ),
    (
        "leaflet.markercluster-1.1.0/MarkerCluster.css",
        "https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.1.0/MarkerCluster.css",
        "f9b756b96397305917d2ff42bebdce58294f89879f0d0cfd18664fffbc59c5d7",
    ),
    (
        "leaflet.markercluster-1.1.0/MarkerCluster.Default.css",
        "https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.1.0/MarkerCluster.Default.css",
        "2d687359a406651b1616bac9c60fba667f134fce24d3fb6bb621c173aa9c1a96",
    ),
    # ---- leaflet.locatecontrol 0.79.0 (was cdn.jsdelivr.net) --------------
    (
        "leaflet.locatecontrol-0.79.0/L.Control.Locate.min.js",
        "https://cdn.jsdelivr.net/npm/leaflet.locatecontrol@0.79.0/dist/L.Control.Locate.min.js",
        "8d574d1e38ce389328ca4c4b39d1b1394cc6243966afc30ceac145fbe6f5fec2",
    ),
    (
        "leaflet.locatecontrol-0.79.0/L.Control.Locate.min.css",
        "https://cdn.jsdelivr.net/npm/leaflet.locatecontrol@0.79.0/dist/L.Control.Locate.min.css",
        "6f5154b217ed50383363f56d14c6a63c3b34a37184479d92f5393c20afb03a81",
    ),
    (
        "leaflet.locatecontrol-0.79.0/L.Control.Locate.min.js.map",
        "https://cdn.jsdelivr.net/npm/leaflet.locatecontrol@0.79.0/dist/L.Control.Locate.min.js.map",
        "258f58a86fa35d63111c5e2925cbdd8c1e61c8babb57c329b1aaa19d7ee9e6d1",
    ),
    # ---- emoji-picker-element 1.27.0 (was cdn.jsdelivr.net, lazy import) --
    # Stays a dynamic import(); only the URL moves to same-origin. index.js is
    # a 98-byte re-export shim: the browser follows its two relative imports,
    # so picker.js + database.js have to sit next to it (no bare specifiers in
    # the graph, verified by spidering it — nothing else to pull).
    (
        "emoji-picker-element-1.27.0/index.js",
        "https://cdn.jsdelivr.net/npm/emoji-picker-element@1.27.0/index.js",
        "7138d5c683bba03d3987d242b11b6eb53356b25581bb4f2f5e139e1d92e91bc1",
    ),
    (
        "emoji-picker-element-1.27.0/picker.js",
        "https://cdn.jsdelivr.net/npm/emoji-picker-element@1.27.0/picker.js",
        "1b5094a9ae36b6921a67f0630ea4b39de54e136f5a79329b7875a4df4990e9b6",
    ),
    (
        "emoji-picker-element-1.27.0/database.js",
        "https://cdn.jsdelivr.net/npm/emoji-picker-element@1.27.0/database.js",
        "3cc04177b65b6cdfd8bd6fe6fb4d76226f7e07e05963c49cc02aa6ec20dbe433",
    ),
    # ---- emoji-picker-element-data 1.8.0 ---------------------------------
    # The picker ships no emoji data: on first open it fetched this file
    # from a FLOATING jsDelivr range (`@^1`), which browser devtools only
    # reveal once the panel is actually opened — the reason it was missed in
    # the first pass here. 1.8.0 is what `^1` resolved to on 2026-09-05.
    # The <emoji-picker> tag's data-source attribute points at this copy.
    (
        "emoji-picker-element-data-1.8.0/en/emojibase/data.json",
        "https://cdn.jsdelivr.net/npm/emoji-picker-element-data@1.8.0/en/emojibase/data.json",
        "e5408f6c06fda053316e98ee811aae2774876809d409bb5f2d9186e60b48e0c3",
    ),
]

UA = "jpfoodmap-vendor-fetch/1.0 (+https://jpfoodmap.com)"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{url} -> HTTP {resp.status}")
        return resp.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="download and print sha256 for pinning, write nothing",
    )
    args = ap.parse_args()

    failures = 0
    for rel, url, want in ASSETS:
        try:
            body = fetch(url)
        except Exception as exc:  # noqa: BLE001 — report and keep going
            print(f"  FAIL  {rel}: {exc}")
            failures += 1
            continue
        got = hashlib.sha256(body).hexdigest()
        if want and want != got:
            print(f"  HASH MISMATCH  {rel}\n        want {want}\n        got  {got}")
            failures += 1
            continue
        if args.print_only:
            print(f'    ("{rel}",\n     "{url}",\n     "{got}"),')
            continue
        dest = VENDOR_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, dest)
        print(f"  ok    {rel:58s} {len(body):>8,d} B  {got[:16]}")

    if failures:
        print(f"\n{failures} asset(s) failed — vendor tree may be incomplete")
        return 1
    if not args.print_only:
        print(f"\nvendor tree written to {VENDOR_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
