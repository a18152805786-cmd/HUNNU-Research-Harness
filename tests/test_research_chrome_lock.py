"""One Harness process at a time drives the Research Chrome.

Two acquisitions side by side share the browser's first tab, its browser-wide
download directory, and pacing ledgers whose read-wait-append is not atomic:
they navigate each other's page, receive each other's PDFs, and slip through
the same pacing gap together.  These tests pin the lock that prevents it.  A
failure here means parallel acquisition is possible again.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from hunnu_harness.browser import research_chrome_lock
from hunnu_harness.browser.persistent_browser import PersistentBrowserStatus
from hunnu_harness.browser.playwright_backend import PlaywrightBrowser, ResearchChromeNotRunning
from hunnu_harness.exit_codes import EXIT_ENV_NOT_READY
from hunnu_harness.literature import cli as literature_cli
from hunnu_harness.paths import TEMP_DIR

RUNNING = PersistentBrowserStatus(running=True, endpoint="http://127.0.0.1:9", port=9)
STOPPED = PersistentBrowserStatus(running=False, endpoint="http://127.0.0.1:9", port=9)

_HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from hunnu_harness.browser import research_chrome_lock as lock
    try:
        lock.acquire("a test holder", path=Path(sys.argv[1]))
    except lock.ResearchChromeBusy as exc:
        print("busy " + str(exc), flush=True)
        raise SystemExit(0)
    print("locked", flush=True)
    if len(sys.argv) > 2:
        sys.stdin.read()
    """
)


def try_from_another_process(path: Path) -> str:
    """Whether a separate process could take the lock right now."""

    completed = subprocess.run(
        [sys.executable, "-c", _HOLDER, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout.strip()


@contextlib.contextmanager
def held_by_another_process(path: Path):
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = process.stdout.readline().strip()
        if line != "locked":
            raise AssertionError(f"the holder process could not take the lock: {line!r}")
        yield process
    finally:
        process.stdin.close()
        process.wait(timeout=60)


class _FakePage:
    url = ""

    async def title(self) -> str:
        return ""


class _FakeContext:
    def __init__(self) -> None:
        self.pages = [_FakePage()]


class _FakeBrowserHandle:
    def __init__(self) -> None:
        self.contexts = [_FakeContext()]

    async def new_browser_cdp_session(self):
        raise RuntimeError("no CDP session in this test")


class _FakeChromium:
    def __init__(self, *, fail_connect: bool = False) -> None:
        self.fail_connect = fail_connect
        self.launched = False

    async def connect_over_cdp(self, endpoint: str):
        if self.fail_connect:
            raise RuntimeError("connection refused")
        return _FakeBrowserHandle()

    async def launch_persistent_context(self, **_kwargs):
        self.launched = True
        return _FakeContext()


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium

    async def stop(self) -> None:
        return None


class _Starter:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self._playwright = playwright

    async def start(self) -> _FakePlaywright:
        return self._playwright


def _browser(**kwargs) -> PlaywrightBrowser:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return PlaywrightBrowser(
        profile_dir=TEMP_DIR / "lock-test-profile",
        downloads_dir=TEMP_DIR / "lock-test-downloads",
        **kwargs,
    )


class CrossProcessLockTests(unittest.TestCase):
    def test_a_second_process_cannot_take_the_research_chrome_while_one_holds_it(self) -> None:
        path = research_chrome_lock.acquire("this test")
        try:
            outcome = try_from_another_process(path)
            self.assertTrue(outcome.startswith("busy"), outcome)
            self.assertIn("acquire-batch", outcome)
        finally:
            research_chrome_lock.release(path)
        self.assertEqual(try_from_another_process(path), "locked")

    def test_holds_nest_in_one_process_and_the_os_lock_goes_with_the_outermost(self) -> None:
        path = research_chrome_lock.acquire("outer: a batch")
        research_chrome_lock.acquire("inner: one item's browser")
        research_chrome_lock.release(path)
        self.assertTrue(try_from_another_process(path).startswith("busy"))
        research_chrome_lock.release(path)
        self.assertFalse(research_chrome_lock.held_here(path))
        self.assertEqual(try_from_another_process(path), "locked")

    def test_the_refusal_names_the_holder_without_waiting(self) -> None:
        with held_by_another_process(research_chrome_lock.LOCK_PATH):
            with self.assertRaises(research_chrome_lock.ResearchChromeBusy) as caught:
                research_chrome_lock.acquire("a second acquisition")
        self.assertIn("a test holder", str(caught.exception))

    def test_the_lock_stays_inside_the_output_root(self) -> None:
        with self.assertRaises(ValueError):
            research_chrome_lock.acquire("misplaced", path=Path(__file__).resolve().parent / "x.lock")


class BrowserHoldsTheLockTests(unittest.TestCase):
    def test_a_run_holds_the_lock_from_start_to_close(self) -> None:
        browser = _browser()
        with mock.patch(
            "hunnu_harness.browser.persistent_browser.probe", return_value=RUNNING
        ), mock.patch(
            "playwright.async_api.async_playwright",
            return_value=_Starter(_FakePlaywright(_FakeChromium())),
        ):
            asyncio.run(browser.start())
            self.assertTrue(research_chrome_lock.held_here())
            asyncio.run(browser.close())
        self.assertFalse(research_chrome_lock.held_here())

    def test_a_failed_start_releases_the_lock(self) -> None:
        browser = _browser()
        with mock.patch(
            "hunnu_harness.browser.persistent_browser.probe", return_value=RUNNING
        ), mock.patch(
            "playwright.async_api.async_playwright",
            return_value=_Starter(_FakePlaywright(_FakeChromium(fail_connect=True))),
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(browser.start())
        self.assertFalse(research_chrome_lock.held_here())
        self.assertEqual(try_from_another_process(research_chrome_lock.LOCK_PATH), "locked")

    def test_an_attach_only_run_refuses_to_launch_a_browser_of_its_own(self) -> None:
        chromium = _FakeChromium()
        browser = _browser(require_attach=True)
        with mock.patch(
            "hunnu_harness.browser.persistent_browser.probe", return_value=STOPPED
        ), mock.patch(
            "playwright.async_api.async_playwright",
            return_value=_Starter(_FakePlaywright(chromium)),
        ):
            with self.assertRaises(ResearchChromeNotRunning):
                asyncio.run(browser.start())
        self.assertFalse(chromium.launched, "an attach-only run must never launch Chrome")
        self.assertFalse(research_chrome_lock.held_here())

    def test_a_single_acquire_refuses_while_another_process_drives_the_research_chrome(self) -> None:
        """This failing means two acquisitions can drive one browser at once again."""

        connected: list[str] = []

        class _Chromium(_FakeChromium):
            async def connect_over_cdp(self, endpoint: str):
                connected.append(endpoint)
                return await super().connect_over_cdp(endpoint)

        out = io.StringIO()
        with held_by_another_process(research_chrome_lock.LOCK_PATH), mock.patch(
            "hunnu_harness.browser.persistent_browser.probe", return_value=RUNNING
        ), mock.patch(
            "playwright.async_api.async_playwright",
            return_value=_Starter(_FakePlaywright(_Chromium())),
        ):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = literature_cli.main(
                    [
                        "live-cnki",
                        "--title",
                        "x",
                        "--run-root",
                        str(TEMP_DIR / "lock-refusal-run"),
                        "--profile",
                        str(TEMP_DIR / "lock-test-profile"),
                        "--json",
                    ]
                )
        payload = json.loads(out.getvalue())
        self.assertEqual(code, EXIT_ENV_NOT_READY)
        self.assertEqual(payload["Status"], "SOURCE_UNAVAILABLE")
        self.assertIn("acquire-batch", payload["Reason"])
        self.assertEqual(connected, [], "the refused run must not have touched the browser")


if __name__ == "__main__":
    unittest.main()
