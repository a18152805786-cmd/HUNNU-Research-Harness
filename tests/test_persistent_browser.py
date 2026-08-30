"""A Research Chrome that survives the run that started it.

The session a publisher issues after institutional sign-in has no expiry, so it
lives in the browser process.  Every acquisition command used to launch its own
Chrome and close it in a ``finally``, which meant the sign-in could not outlive
one command and every later run arrived anonymous.

These pin the parts that make attaching safe: that a running browser is reused
rather than restarted, that attaching never opens a port of its own, and above
all that a run which attached does not close the browser it borrowed.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from hunnu_harness.browser.persistent_browser import (
    DEFAULT_RESEARCH_DEBUG_PORT,
    RESEARCH_DEBUG_PORT_ENV,
    PersistentBrowserError,
    PersistentBrowserStatus,
    endpoint_for,
    research_debug_port,
    start_persistent_browser,
)
from hunnu_harness.browser.playwright_backend import PlaywrightBrowser
from hunnu_harness.paths import TEMP_DIR

RUNNING = PersistentBrowserStatus(
    running=True,
    endpoint=endpoint_for(DEFAULT_RESEARCH_DEBUG_PORT),
    port=DEFAULT_RESEARCH_DEBUG_PORT,
    browser_version="Chrome/151.0.0.0",
)
STOPPED = PersistentBrowserStatus(
    running=False,
    endpoint=endpoint_for(DEFAULT_RESEARCH_DEBUG_PORT),
    port=DEFAULT_RESEARCH_DEBUG_PORT,
)


class PortTests(unittest.TestCase):
    def test_the_default_port_is_not_chromes_conventional_one(self) -> None:
        """9222 is where every other tool looks; sharing it invites a mix-up."""

        self.assertNotEqual(DEFAULT_RESEARCH_DEBUG_PORT, 9222)

    def test_the_endpoint_is_loopback_only(self) -> None:
        self.assertTrue(endpoint_for(DEFAULT_RESEARCH_DEBUG_PORT).startswith("http://127.0.0.1:"))

    def test_the_port_can_be_overridden(self) -> None:
        with mock.patch.dict("os.environ", {RESEARCH_DEBUG_PORT_ENV: "9411"}):
            self.assertEqual(research_debug_port(), 9411)

    def test_a_nonsense_port_fails_closed(self) -> None:
        for value in ("not-a-port", "0", "70000"):
            with mock.patch.dict("os.environ", {RESEARCH_DEBUG_PORT_ENV: value}):
                with self.assertRaises(PersistentBrowserError):
                    research_debug_port()


class StartTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_a_running_browser_is_reused_and_never_restarted(self) -> None:
        """Restarting it would throw away the session it is holding."""

        spawned: list[list[str]] = []
        with mock.patch(
            "hunnu_harness.browser.persistent_browser.probe", return_value=RUNNING
        ):
            status = start_persistent_browser(
                profile_dir=TEMP_DIR / "unused-profile",
                chrome_executable=Path("chrome.exe"),
                spawn=spawned.append,
            )
        self.assertTrue(status.running)
        self.assertEqual(spawned, [])

    def test_starting_passes_the_debugging_port_and_the_dedicated_profile(self) -> None:
        spawned: list[list[str]] = []
        profile = TEMP_DIR / "persistent-profile"
        chrome = TEMP_DIR / "chrome.exe"
        chrome.parent.mkdir(parents=True, exist_ok=True)
        chrome.write_bytes(b"")

        with mock.patch(
            "hunnu_harness.browser.persistent_browser.probe",
            side_effect=[STOPPED, RUNNING],
        ):
            start_persistent_browser(
                profile_dir=profile,
                chrome_executable=chrome,
                spawn=spawned.append,
            )

        self.assertEqual(len(spawned), 1)
        command = spawned[0]
        self.assertIn(f"--remote-debugging-port={DEFAULT_RESEARCH_DEBUG_PORT}", command)
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
        self.assertTrue(any(part.startswith("--user-data-dir=") for part in command))
        self.assertTrue(any(str(profile.resolve()) in part for part in command))

    def test_a_missing_chrome_fails_before_anything_is_spawned(self) -> None:
        spawned: list[list[str]] = []
        with mock.patch(
            "hunnu_harness.browser.persistent_browser.probe", return_value=STOPPED
        ):
            with self.assertRaises(PersistentBrowserError):
                start_persistent_browser(
                    profile_dir=TEMP_DIR / "persistent-profile",
                    chrome_executable=TEMP_DIR / "no-such-chrome.exe",
                    spawn=spawned.append,
                )
        self.assertEqual(spawned, [])

    def test_a_profile_already_held_without_a_port_fails_closed(self) -> None:
        """A second Chrome on the same profile would fail on the lock anyway."""

        profile = TEMP_DIR / "locked-profile"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "SingletonLock").write_text("", encoding="utf-8")
        chrome = TEMP_DIR / "chrome.exe"
        chrome.write_bytes(b"")
        try:
            with mock.patch(
                "hunnu_harness.browser.persistent_browser.probe", return_value=STOPPED
            ):
                with self.assertRaises(PersistentBrowserError):
                    start_persistent_browser(
                        profile_dir=profile,
                        chrome_executable=chrome,
                        spawn=lambda command: None,
                    )
        finally:
            (profile / "SingletonLock").unlink(missing_ok=True)

    def test_it_gives_up_rather_than_waiting_forever(self) -> None:
        chrome = TEMP_DIR / "chrome.exe"
        chrome.write_bytes(b"")
        with mock.patch(
            "hunnu_harness.browser.persistent_browser.probe", return_value=STOPPED
        ):
            with self.assertRaises(PersistentBrowserError):
                start_persistent_browser(
                    profile_dir=TEMP_DIR / "persistent-profile",
                    chrome_executable=chrome,
                    startup_timeout_seconds=0.5,
                    spawn=lambda command: None,
                )


class _FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False
        self.chromium = self

    async def connect_over_cdp(self, endpoint):  # noqa: ANN001
        self.endpoint = endpoint
        return _FakeBrowser()

    async def stop(self) -> None:
        self.stopped = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False
        self.contexts = [_FakeContext()]

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.closed = False
        self.pages = [_FakePage()]

    async def close(self) -> None:
        self.closed = True


class _FakePage:
    url = "about:blank"

    async def title(self) -> str:
        return ""


class AttachTests(unittest.TestCase):
    """The part that matters: a borrowed browser must survive the borrower."""

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self.profile = TEMP_DIR / "attach-profile"
        self.profile.mkdir(parents=True, exist_ok=True)

    def _browser(self) -> PlaywrightBrowser:
        return PlaywrightBrowser(
            profile_dir=self.profile,
            downloads_dir=TEMP_DIR / "attach-downloads",
        )

    def test_a_run_attaches_to_a_persistent_browser_instead_of_launching(self) -> None:
        fake = _FakePlaywright()
        browser = self._browser()
        with mock.patch(
            "hunnu_harness.browser.persistent_browser.probe", return_value=RUNNING
        ), mock.patch("playwright.async_api.async_playwright", return_value=_Starter(fake)):
            asyncio.run(browser.start())

        self.assertTrue(browser.attached)
        self.assertEqual(fake.endpoint, RUNNING.endpoint)

    def test_closing_an_attached_run_leaves_the_browser_running(self) -> None:
        fake = _FakePlaywright()
        browser = self._browser()
        with mock.patch(
            "hunnu_harness.browser.persistent_browser.probe", return_value=RUNNING
        ), mock.patch("playwright.async_api.async_playwright", return_value=_Starter(fake)):
            asyncio.run(browser.start())
            context = browser.context
            underlying = browser._browser
            asyncio.run(browser.close())

        self.assertFalse(context.closed, "the borrowed context must not be closed")
        self.assertFalse(underlying.closed, "the borrowed browser must not be closed")
        self.assertTrue(fake.stopped, "the connection itself should be released")
        self.assertFalse(browser.attached)

    def test_a_run_that_launched_its_own_browser_still_closes_it(self) -> None:
        fake = _FakePlaywright()
        browser = self._browser()
        browser._playwright = fake
        browser.context = _FakeContext()
        browser.page = browser.context.pages[0]
        browser.attached = False

        asyncio.run(browser.close())

        self.assertTrue(fake.stopped)


class _Starter:
    """Stands in for ``async_playwright()``, whose result is awaited via start()."""

    def __init__(self, playwright: _FakePlaywright) -> None:
        self._playwright = playwright

    async def start(self) -> _FakePlaywright:
        return self._playwright


if __name__ == "__main__":
    unittest.main()
