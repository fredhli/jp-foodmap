"""
Detached-Chrome helpers for omakase.in scraping.

Mirrors the CDP pattern from hkff/parse_disclosure/scrape_di.py, but with its
own debug port (9223) and profile dir (omakase/.chrome_profile/). This keeps
omakase's login cookies isolated from hkff — important because hkff/main.py
wipes its own .chrome_profile on exit, which would otherwise wipe our login.

Flow:
  1. get_or_spawn_chrome() — attach to an existing detached Chrome on 9223,
     or spawn one. Chrome survives this Python process.
  2. User logs in manually in that window once. Cookies persist in the
     profile dir across runs.
  3. Subsequent scripts attach via CDP and reuse the logged-in session.
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Browser, Page

from tabelog.paths import PROFILE_DIR, OUTPUT_DIR

DEBUG_PORT = 9223
if sys.platform == "darwin":
    CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
elif os.name == "nt":
    CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
else:
    CHROME_PATH = "google-chrome"
SPAWN_TIMEOUT_S = 45.0


def _startup_log_tail(path: Path, limit: int = 2000) -> str:
    try:
        with path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - limit))
            return log.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


async def _wait_cdp_reachable(p, cdp_url: str, timeout_s: float, *,
                              process=None, startup_log: Path | None = None) -> Browser:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    last_err = None
    while loop.time() < deadline:
        try:
            return await p.chromium.connect_over_cdp(cdp_url, timeout=1000)
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.5)
    name = last_err.__class__.__name__ if last_err else "unknown"
    detail = str(last_err or "").strip()
    exit_code = process.poll() if process is not None else None
    state = (f"Chrome launcher exited with code {exit_code}. " if exit_code is not None
             else "Chrome launcher is still running, but CDP is unavailable. ")
    log_text = _startup_log_tail(startup_log) if startup_log else ""
    log_hint = f"Startup log: {startup_log}." if startup_log else ""
    raise RuntimeError(
        f"Chrome did not expose CDP at {cdp_url} within {timeout_s:.0f}s. "
        f"{state}Last connection error: {name}: {detail}. "
        f"{log_hint} {log_text} "
        "Check the project Chrome window and run scripts/diagnose_chrome_cdp.ps1 "
        "before trying the crawler again."
    )


def _spawn_detached_chrome() -> tuple[subprocess.Popen, Path]:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    if not Path(CHROME_PATH).is_file():
        raise RuntimeError(f"Chrome executable not found: {CHROME_PATH}")
    log_dir = OUTPUT_DIR / "browser_startup"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ("chrome-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".log")
    cmd = [
        CHROME_PATH,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,900",
        "--window-position=0,0",
        "--restore-last-session=false",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore",
        "--enable-logging=stderr",
        "--log-level=2",
    ]
    if os.name == "nt":
        kwargs = {
            "creationflags": (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            ),
        }
    else:
        kwargs = {"start_new_session": True}
    with log_path.open("wb") as stderr_log:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            **kwargs,
        )
    return process, log_path


async def get_or_spawn_chrome(p) -> Browser:
    cdp_url = f"http://127.0.0.1:{DEBUG_PORT}"
    try:
        browser = await p.chromium.connect_over_cdp(cdp_url, timeout=2000)
        print(f"Attached to existing Chrome at {cdp_url}")
        return browser
    except Exception as e:
        print(f"Chrome CDP unavailable at {cdp_url} ({e.__class__.__name__}); starting project Chrome")

    process, startup_log = _spawn_detached_chrome()
    browser = await _wait_cdp_reachable(
        p, cdp_url, timeout_s=SPAWN_TIMEOUT_S, process=process, startup_log=startup_log,
    )
    print(f"Attached to spawned Chrome at {cdp_url}")
    return browser


async def get_or_open_page(browser: Browser) -> Page:
    contexts = browser.contexts
    ctx = contexts[0] if contexts else await browser.new_context()

    for _ in range(20):
        if ctx.pages:
            break
        await asyncio.sleep(0.2)

    pages = list(ctx.pages)
    usable = [
        pg for pg in pages
        if not (pg.url or "").lower().startswith(
            ("chrome://", "devtools://", "chrome-extension://")
        )
    ]

    if not usable:
        page = await ctx.new_page()
        await page.bring_to_front()
        print("Opened new page")
        return page

    page = next(
        (pg for pg in usable if "omakase" in (pg.url or "").lower()),
        usable[0],
    )

    for extra in usable:
        if extra is page:
            continue
        u = (extra.url or "").lower()
        if u in ("", "about:blank"):
            try:
                await extra.close()
            except Exception:
                pass

    try:
        await page.bring_to_front()
    except Exception:
        pass
    print(f"Reusing page: {page.url!r}")
    return page
