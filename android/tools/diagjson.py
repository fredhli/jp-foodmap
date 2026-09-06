#!/usr/bin/env python3
"""Read values out of a diagnostics JSON, and assert over them.

    diagjson.py get   diag.json innerWidth        # value on stdout, exit 3 when absent
    diagjson.py check diag.json innerWidth=475 dpr=2.625 imeMode=WEBVIEW

WHY IT IS KEY-SHAPED AND NOT PATH-SHAPED. Two different producers write these files: the
app's own diagnostics line (docs/PLAN.md §5.8, owned by T5, whose nesting is its business —
today `insets.top`, tomorrow `shell.insets.top`) and tools/diag.sh's DevTools probe, which
writes one flat object. A verify script that hard-coded either layout would go red the day
the other one moved, so a key here matches a whole dotted path, a suffix of one, or a leaf
name — shallowest match wins. `innerWidth` finds it wherever it lives; `env.t` still works
when two branches both carry a `t`.

Exit codes: get -> 0 found / 3 absent; check -> 0 no failure / 1 at least one FAIL. A key
that is absent is SKIP, never FAIL: an older APK legitimately does not report imeMode, and
the acceptance has to be able to say so instead of going red.
"""

import json
import sys


def walk(obj, prefix=""):
    """Every scalar and container in the tree, as (dotted path, value), shallowest first."""
    out = []
    queue = [(prefix, obj)]
    while queue:
        path, node = queue.pop(0)
        if isinstance(node, dict):
            for k, v in node.items():
                p = "%s.%s" % (path, k) if path else str(k)
                out.append((p, v))
                if isinstance(v, (dict, list)):
                    queue.append((p, v))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                p = "%s.%d" % (path, i)
                out.append((p, v))
                if isinstance(v, (dict, list)):
                    queue.append((p, v))
    return out


def lookup(doc, key):
    """-> (path, value) or (None, None). Shallowest match wins, exact path beats a suffix."""
    entries = walk(doc)
    exact = [e for e in entries if e[0] == key]
    if exact:
        return exact[0]
    suffix = [e for e in entries if e[0].endswith("." + key)]
    if suffix:
        suffix.sort(key=lambda e: e[0].count("."))
        return suffix[0]
    leaf = [e for e in entries if e[0].rsplit(".", 1)[-1] == key]
    if leaf:
        leaf.sort(key=lambda e: e[0].count("."))
        return leaf[0]
    return None, None


def as_text(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float, str)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def equal(got, want):
    """Loose on purpose: 475 == '475', 2.625 == '2.6250', true == 'TRUE', WEBVIEW == webview."""
    if isinstance(got, bool) or want.lower() in ("true", "false"):
        return as_text(got).lower() == want.strip().lower()
    try:
        return abs(float(got) - float(want)) <= 0.01
    except (TypeError, ValueError):
        pass
    return as_text(got).strip().lower() == want.strip().lower()


def main(argv):
    if len(argv) < 3:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    mode, path = argv[1], argv[2]
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print("diagjson: cannot read %s: %s" % (path, exc), file=sys.stderr)
        return 2

    if mode == "get":
        if len(argv) < 4:
            return 2
        found, value = lookup(doc, argv[3])
        if found is None:
            print("diagjson: no key %r in %s" % (argv[3], path), file=sys.stderr)
            return 3
        print(as_text(value))
        return 0

    if mode == "check":
        bad = 0
        for spec in argv[3:]:
            if "=" not in spec:
                print("SKIP  %s (not key=value)" % spec)
                continue
            key, want = spec.split("=", 1)
            found, value = lookup(doc, key)
            if found is None:
                print("SKIP  %-14s absent from this build's diagnostics" % key)
                continue
            if equal(value, want):
                print("PASS  %-14s %s" % (key, as_text(value)))
            else:
                print("FAIL  %-14s expected %s, got %s   (at %s)"
                      % (key, want, as_text(value), found))
                bad += 1
        return 1 if bad else 0

    print("diagjson: unknown mode %r" % mode, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
