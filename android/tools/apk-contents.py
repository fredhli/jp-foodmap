#!/usr/bin/env python3
"""What is actually inside an APK, biggest entries first.

There is no `unzip` binary in this distro, and the size question the verify pass asks is
not "how big is the file" but "what made it that big" — a debug Glance widget has no
business carrying a font set or the whole Material icon pack.

    python3 apk-contents.py app/build/outputs/apk/debug/app-debug.apk
"""
import sys
import zipfile

if len(sys.argv) != 2:
    raise SystemExit(__doc__)

z = zipfile.ZipFile(sys.argv[1])
e = sorted(z.infolist(), key=lambda i: -i.file_size)
print("%d entries, %.2f MB uncompressed" % (len(e), sum(i.file_size for i in e) / 1e6))
for i in e[:15]:
    print("  %8.1f KB  %s" % (i.file_size / 1024, i.filename))
