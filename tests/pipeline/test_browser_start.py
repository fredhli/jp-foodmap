"""Offline checks for detached Chrome startup diagnostics."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tabelog import browser


class BrowserStartupTests(unittest.TestCase):
    def test_spawn_keeps_dedicated_profile_and_captures_chrome_errors(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            executable = root / 'chrome.exe'
            executable.touch()
            profile = root / 'profile'
            output = root / 'output'
            process = object()
            with patch.object(browser, 'CHROME_PATH', str(executable)), \
                 patch.object(browser, 'PROFILE_DIR', profile), \
                 patch.object(browser, 'OUTPUT_DIR', output), \
                 patch.object(browser.subprocess, 'Popen', return_value=process) as spawn:
                launched, log = browser._spawn_detached_chrome()
            self.assertIs(launched, process)
            self.assertTrue(log.exists())
            args = spawn.call_args.args[0]
            self.assertIn('--remote-debugging-port=9223', args)
            self.assertIn(f'--user-data-dir={profile}', args)
            self.assertIn('--enable-logging=stderr', args)
            self.assertEqual(spawn.call_args.kwargs['stderr'].name, str(log))
            self.assertTrue(spawn.call_args.kwargs['stderr'].closed)

    def test_startup_failure_reports_exit_and_browser_stderr(self):
        class Process:
            def poll(self):
                return 7

        class Chromium:
            connect_over_cdp = AsyncMock(side_effect=RuntimeError('connection refused'))

        class Playwright:
            chromium = Chromium()

        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / 'chrome.log'
            log.write_text('profile is already in use')
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(browser._wait_cdp_reachable(
                    Playwright(), 'http://127.0.0.1:9223', 0.01,
                    process=Process(), startup_log=log,
                ))
        message = str(caught.exception)
        self.assertIn('launcher exited with code 7', message)
        self.assertIn('connection refused', message)
        self.assertIn('profile is already in use', message)

    def test_existing_cdp_does_not_launch_second_chrome(self):
        connected = object()

        class Chromium:
            connect_over_cdp = AsyncMock(return_value=connected)

        class Playwright:
            chromium = Chromium()

        with patch.object(browser, '_spawn_detached_chrome', side_effect=AssertionError('spawned')):
            self.assertIs(asyncio.run(browser.get_or_spawn_chrome(Playwright())), connected)


if __name__ == '__main__':
    unittest.main()
