#!/usr/bin/env python3
"""Evaluate one JS expression inside the app's WebView, over the DevTools protocol.

    wv-eval.py [--serial emulator-5554] [--pkg com.fredhli.jpfoodmap.debug]
               [--port 9333] [--timeout 12] 'document.title'
    wv-eval.py ... --file probe.js          # expression from a file (multi-line is fine)

Prints the evaluated value as JSON on stdout. Exit codes, chosen so a verify script can
tell "the app cannot answer" from "the answer was wrong":

    0  evaluated, value on stdout
    3  no DevTools endpoint — this is a RELEASE APK (WebView.setWebContentsDebuggingEnabled
       is BuildConfig.DEBUG) or the app is not running. Callers report SKIP, never FAIL.
    1  a real failure: the socket was there and the evaluation threw, or the protocol broke.

WHY THIS EXISTS. Three acceptances in docs/STANDARDS.md are statements about the DOM, not
about pixels: "the open restaurant card is still open after the fold proxy" (§3.2), "the
filter panel is open" (§2.2) and "the offline bar is showing" (§9.1). The shell's own
diagnostics JSON (§5.8) reports geometry, not page state, and adb can see neither. A debug
build's WebView publishes a DevTools endpoint on an abstract unix socket
(@webview_devtools_remote_<pid>); forwarding that to a local port and speaking the protocol
is the only way to ask the page a question from this box. The alternative — reading the
accessibility tree with `uiautomator dump` — reports nothing for WebView content unless an
accessibility service is running, so it is not a substitute.

Deliberately no dependencies: a ~70-line WebSocket client (RFC 6455, one masked text frame
out, frames in) rather than `websockets`, because these tools have to run on a box where
only the Android SDK is guaranteed to be installed.
"""

import argparse
import base64
import json
import os
import re
import socket
import subprocess
import sys
import time


def adb(serial, *args, timeout=25):
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def find_socket(serial, pkg):
    """The devtools socket name for pkg, or None.

    pidof is the cheap path. /proc/net/unix is the honest one: the socket is named after the
    process that opened it, and a WebView in a :sandboxed process would not match pidof —
    so read the actual listening names and keep any that belong to one of the package's pids.
    """
    rc, out, _ = adb(serial, "shell", "pidof", pkg)
    pids = out.split() if rc == 0 else []
    rc, unix, _ = adb(serial, "shell", "cat", "/proc/net/unix")
    names = set()
    if rc == 0:
        for line in unix.splitlines():
            m = re.search(r"@(webview_devtools_remote_\d+)", line)
            if m:
                names.add(m.group(1))
    for pid in pids:
        want = "webview_devtools_remote_%s" % pid
        if want in names:
            return want
    # No /proc/net/unix (SELinux on some images) — fall back to the first pid.
    if pids and not names:
        return "webview_devtools_remote_%s" % pids[0]
    return None


def http_get(port, path, timeout=6):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(
            ("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n" % path).encode()
        )
        # Content-Length, not "read until the peer closes": the DevTools HTTP server keeps
        # the connection open whatever Connection: close says, so reading to EOF is a
        # guaranteed six-second stall on every single call.
        buf = b""
        need = None
        while True:
            if need is not None:
                head_len = buf.index(b"\r\n\r\n") + 4
                if len(buf) - head_len >= need:
                    break
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if need is None and b"\r\n\r\n" in buf:
                head = buf.split(b"\r\n\r\n", 1)[0]
                m = re.search(rb"(?i)content-length:\s*(\d+)", head)
                need = int(m.group(1)) if m else 0
    finally:
        s.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n")[0]:
        raise RuntimeError("devtools HTTP %s" % head.split(b"\r\n")[0].decode(errors="replace"))
    return body


def ws_eval(port, path, expression, timeout):
    """One Runtime.evaluate over a hand-rolled WebSocket. Returns the CDP result object."""
    key = base64.b64encode(os.urandom(16)).decode()
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.settimeout(timeout)
    req = (
        "GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, key)
    )
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("devtools closed during the websocket handshake")
        buf += chunk
    if b"101" not in buf.split(b"\r\n")[0]:
        raise RuntimeError("websocket upgrade refused: %s" % buf.split(b"\r\n")[0].decode())
    rest = buf.split(b"\r\n\r\n", 1)[1]

    msg = json.dumps(
        {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": int(timeout * 1000),
            },
        }
    ).encode()
    # One masked text frame. Payloads here are short, but the 126/127 length forms cost two
    # lines and a truncated frame is a confusing failure.
    mask = os.urandom(4)
    n = len(msg)
    if n < 126:
        header = bytes([0x81, 0x80 | n])
    elif n < (1 << 16):
        header = bytes([0x81, 0x80 | 126, (n >> 8) & 0xFF, n & 0xFF])
    else:
        header = bytes([0x81, 0x80 | 127]) + n.to_bytes(8, "big")
    s.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(msg)))

    deadline = time.time() + timeout

    def recv(k):
        nonlocal rest
        while len(rest) < k:
            if time.time() > deadline:
                raise RuntimeError("timed out reading the devtools reply")
            chunk = s.recv(65536)
            if not chunk:
                raise RuntimeError("devtools closed while reading the reply")
            rest += chunk
        out, rest = rest[:k], rest[k:]
        return out

    while True:
        b0, b1 = recv(2)
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        if ln == 126:
            ln = int.from_bytes(recv(2), "big")
        elif ln == 127:
            ln = int.from_bytes(recv(8), "big")
        mk = recv(4) if masked else b""
        payload = recv(ln)
        if masked:
            payload = bytes(b ^ mk[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            raise RuntimeError("devtools closed the websocket")
        if opcode in (0x9, 0xA):  # ping/pong — ignore, nothing here holds a long connection
            continue
        try:
            m = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            continue
        if m.get("id") == 1:
            s.close()
            return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("expression", nargs="?", default=None)
    ap.add_argument("--file", help="read the expression from this file instead")
    ap.add_argument("--serial", default=os.environ.get("JPFM_SERIAL", ""))
    ap.add_argument("--pkg", default=os.environ.get("JPFM_PKG", "com.fredhli.jpfoodmap.debug"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("JPFM_CDP_PORT", "9333")))
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--url-match", default="jpfoodmap.com")
    args = ap.parse_args()

    expr = args.expression
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            expr = fh.read()
    if not expr:
        print("wv-eval: nothing to evaluate", file=sys.stderr)
        return 1

    sock = find_socket(args.serial, args.pkg)
    if not sock:
        print("wv-eval: no webview_devtools_remote socket for %s (release build, or not "
              "running)" % args.pkg, file=sys.stderr)
        return 3

    adb(args.serial, "forward", "--remove", "tcp:%d" % args.port)
    rc, _, err = adb(args.serial, "forward", "tcp:%d" % args.port, "localabstract:%s" % sock)
    if rc != 0:
        print("wv-eval: adb forward failed: %s" % err, file=sys.stderr)
        return 3
    try:
        targets = json.loads(http_get(args.port, "/json/list").decode("utf-8", "replace"))
        pages = [t for t in targets if t.get("webSocketDebuggerUrl")]
        if not pages:
            print("wv-eval: devtools has no debuggable page (targets: %d)" % len(targets),
                  file=sys.stderr)
            return 3
        pages.sort(key=lambda t: (args.url_match not in (t.get("url") or ""),))
        ws_url = pages[0]["webSocketDebuggerUrl"]
        path = "/" + ws_url.split("/", 3)[3] if ws_url.count("/") >= 3 else "/"
        reply = ws_eval(args.port, path, expr, args.timeout)
    except Exception as exc:  # noqa: BLE001 — the caller only needs the reason as text
        print("wv-eval: %s" % exc, file=sys.stderr)
        return 1
    finally:
        adb(args.serial, "forward", "--remove", "tcp:%d" % args.port)

    res = reply.get("result", {})
    if "exceptionDetails" in res:
        print("wv-eval: page threw: %s" % json.dumps(res["exceptionDetails"])[:400],
              file=sys.stderr)
        return 1
    value = res.get("result", {}).get("value")
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
