"""Waiting out a bot-check interstitial, and reporting what the browser did.

Three defects are pinned here, all observed on a real ScienceDirect run:

  * ``goto`` returned on ``domcontentloaded``, which is precisely while the
    interstitial is still deciding.  The page was read as "Just a moment…",
    acquisition saw no results, and Chrome was torn down 0.2s later -- before
    the check would have cleared on its own.
  * Nothing reported that a browser had launched at all, so an Agent inferred
    "no browser opened" for a run in which Chrome launched, reached the
    article host, and closed five seconds later.
  * An unreadable page sample was treated as a clear page, so a reload or a
    closed page could end a wait before the gate had actually gone away.

Waiting is not solving.  These tests also pin that the wait never clicks,
types, or otherwise touches the page.
"""

from __future__ import annotations

import asyncio
import unittest

from hunnu_harness.browser.playwright_backend import (
    DEFAULT_INTERSTITIAL_WAIT_SECONDS,
    PlaywrightBrowser,
)


class _FakePage:
    """A page whose title/body follow a script, recording every call made."""

    def __init__(self, frames):
        self._frames = list(frames)
        self._index = 0
        self.url = "https://www.sciencedirect.com/search"
        self.interactions: list[str] = []
        self.title_reads = 0

    def _frame(self):
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame

    async def title(self):
        self.title_reads += 1
        return self._frame()[0]

    def locator(self, _selector):
        page = self

        class _Loc:
            async def inner_text(self, timeout=None):
                return page._frames[min(page._index - 1, len(page._frames) - 1)][1]

            async def click(self, *a, **k):
                page.interactions.append("click")

            async def fill(self, *a, **k):
                page.interactions.append("fill")

        return _Loc()

    # Anything below would be an interaction; none of it may be called.
    async def click(self, *a, **k):
        self.interactions.append("click")

    async def fill(self, *a, **k):
        self.interactions.append("fill")

    async def check(self, *a, **k):
        self.interactions.append("check")

    def get_by_label(self, *a, **k):
        self.interactions.append("get_by_label")
        raise AssertionError("the interstitial wait must not touch form controls")


class _TitleScriptPage(_FakePage):
    """A fake page whose title can fail on selected scripted frames."""

    async def title(self):
        self.title_reads += 1
        frame = self._frame()
        if isinstance(frame, BaseException):
            raise frame
        return frame[0]


class _UnreadablePage:
    """A page whose title cannot be read, optionally because it is closed."""

    url = "about:blank"

    def __init__(self, *, closed: bool):
        self.closed = closed
        self.closed_checks = 0
        self.interactions: list[str] = []
        self.title_reads = 0

    def is_closed(self):
        self.closed_checks += 1
        return self.closed

    async def title(self):
        self.title_reads += 1
        raise RuntimeError("page cannot be read")


def _browser(page, **kwargs):
    browser = PlaywrightBrowser(
        profile_dir="unused",
        downloads_dir="unused",
        interstitial_poll_seconds=0.001,
        **kwargs,
    )
    browser.page = page
    return browser


class InterstitialWaitTests(unittest.TestCase):
    def test_a_clearing_interstitial_is_waited_out(self) -> None:
        page = _FakePage(
            [
                ("Just a moment...", "Checking your browser"),
                ("Just a moment...", "Checking your browser"),
                ("ScienceDirect", "AI washing: Strategic disclosure and backlash"),
            ]
        )
        settled = asyncio.run(_browser(page).settle_automated_interstitial())
        self.assertTrue(settled)
        self.assertGreaterEqual(page.title_reads, 2)

    def test_the_chinese_interstitial_title_is_recognised(self) -> None:
        page = _FakePage([("请稍候…", ""), ("ScienceDirect", "article")])
        self.assertTrue(asyncio.run(_browser(page).settle_automated_interstitial()))

    def test_a_normal_page_is_not_waited_on(self) -> None:
        page = _FakePage([("ScienceDirect", "AI washing")])
        settled = asyncio.run(_browser(page).settle_automated_interstitial())
        self.assertTrue(settled)
        self.assertEqual(page.title_reads, 1)

    def test_a_persistent_gate_gives_up_and_leaves_the_page_alone(self) -> None:
        page = _FakePage([("Just a moment...", "Checking your browser")])
        settled = asyncio.run(
            _browser(page, interstitial_wait_seconds=0.05).settle_automated_interstitial()
        )
        self.assertFalse(settled)
        self.assertEqual(page.interactions, [])

    def test_waiting_never_interacts_with_the_page(self) -> None:
        page = _FakePage(
            [
                ("Just a moment...", "Verify you are human"),
                ("Just a moment...", "Verify you are human"),
                ("ScienceDirect", "article"),
            ]
        )
        asyncio.run(_browser(page).settle_automated_interstitial())
        self.assertEqual(
            page.interactions,
            [],
            "the interstitial wait must never click, fill, or check anything",
        )

    def test_body_marker_alone_is_enough_to_keep_waiting(self) -> None:
        page = _FakePage(
            [
                ("", "Verifying you are human. This may take a few seconds."),
                ("ScienceDirect", "article"),
            ]
        )
        self.assertTrue(asyncio.run(_browser(page).settle_automated_interstitial()))

    def test_a_closed_page_is_not_waited_on(self) -> None:
        """This failing means a closed page is still entering the read loop."""

        page = _UnreadablePage(closed=True)
        settled = asyncio.run(
            _browser(page, interstitial_wait_seconds=60.0).settle_automated_interstitial()
        )
        self.assertTrue(settled)
        self.assertEqual(page.title_reads, 0)
        self.assertEqual(page.interactions, [])

    def test_an_open_unreadable_page_is_not_known_to_be_clear(self) -> None:
        """This failing means an unreadable sample is being treated as clear."""

        page = _UnreadablePage(closed=False)
        settled = asyncio.run(
            _browser(page, interstitial_wait_seconds=0.005).settle_automated_interstitial()
        )
        self.assertFalse(settled)
        self.assertGreater(page.title_reads, 0)
        self.assertEqual(page.interactions, [])

    def test_a_reload_read_error_does_not_end_the_wait(self) -> None:
        """This failing means a read error can end a wait during navigation."""

        page = _TitleScriptPage(
            [
                RuntimeError("navigation is in flight"),
                ("Just a moment...", "Checking your browser"),
                ("ScienceDirect", "AI washing"),
            ]
        )
        # The default budget on purpose: the third read ends this wait whatever
        # the clock says, so a tight budget buys nothing and fails the test on a
        # loaded machine (4.8% of runs with the CPUs saturated, at 0.05 s).
        settled = asyncio.run(_browser(page).settle_automated_interstitial())
        self.assertTrue(settled)
        self.assertGreaterEqual(page.title_reads, 3)
        self.assertEqual(page.interactions, [])

    def test_default_budget_is_bounded(self) -> None:
        self.assertGreater(DEFAULT_INTERSTITIAL_WAIT_SECONDS, 0)
        self.assertLessEqual(DEFAULT_INTERSTITIAL_WAIT_SECONDS, 60)


class LifecycleReportTests(unittest.TestCase):
    def test_a_started_browser_reports_where_it_ended_up(self) -> None:
        page = _FakePage([("ScienceDirect", "article")])
        page.url = "https://www.sciencedirect.com/science/article/pii/S1544612326002151"
        report = asyncio.run(_browser(page).lifecycle())
        self.assertTrue(report["BrowserLaunched"])
        self.assertEqual(report["FinalURL"], page.url)
        self.assertEqual(report["FinalPageTitle"], "ScienceDirect")

    def test_a_browser_that_never_started_says_so(self) -> None:
        browser = PlaywrightBrowser(profile_dir="unused", downloads_dir="unused")
        report = asyncio.run(browser.lifecycle())
        self.assertFalse(report["BrowserLaunched"])
        self.assertEqual(report["FinalURL"], "")

    def test_lifecycle_survives_a_broken_page(self) -> None:
        class _Dead:
            url = "https://example.com"

            async def title(self):
                raise RuntimeError("gone")

        report = asyncio.run(_browser(_Dead()).lifecycle())
        self.assertTrue(report["BrowserLaunched"])

    def test_report_names_the_fields_an_agent_needs(self) -> None:
        page = _FakePage([("ScienceDirect", "article")])
        report = asyncio.run(_browser(page).lifecycle())
        for field in ("BrowserLaunched", "BrowserHeadless", "FinalURL", "FinalPageTitle"):
            self.assertIn(field, report)


class HumanClearanceTests(unittest.TestCase):
    """A person may pass a gate; the Harness may only notice that they did."""

    def test_unattended_runs_never_wait_for_a_human(self) -> None:
        page = _FakePage([("Are you a robot?", "complete the captcha challenge")])
        browser = _browser(page, interstitial_wait_seconds=0.01)
        self.assertEqual(browser.human_wait_seconds, 0.0)
        # A rendered challenge is left to the adapter, and no one is waited for.
        self.assertTrue(asyncio.run(browser.settle_page_gates()))
        self.assertEqual(page.interactions, [])

    def test_a_human_passing_the_gate_lets_the_run_continue(self) -> None:
        page = _FakePage(
            [
                ("Are you a robot?", "complete the captcha challenge"),
                ("Are you a robot?", "complete the captcha challenge"),
                ("ScienceDirect", "AI washing: Strategic disclosure and backlash"),
            ]
        )
        browser = _browser(
            page,
            interstitial_wait_seconds=0.01,
            human_wait_seconds=5.0,
            human_wait_poll_seconds=0.001,
        )
        self.assertTrue(asyncio.run(browser.settle_page_gates()))
        self.assertEqual(page.interactions, [])

    def test_waiting_for_a_human_still_never_touches_the_challenge(self) -> None:
        page = _FakePage([("Are you a robot?", "complete the captcha challenge below")])
        browser = _browser(
            page,
            interstitial_wait_seconds=0.01,
            human_wait_seconds=0.05,
            human_wait_poll_seconds=0.001,
        )
        self.assertFalse(asyncio.run(browser.settle_page_gates()))
        self.assertEqual(
            page.interactions,
            [],
            "a rendered challenge must be left entirely to the person",
        )

    def test_a_rendered_challenge_is_a_gate_but_not_an_interstitial(self) -> None:
        page = _FakePage([("Are you a robot?", "complete the captcha challenge")])
        browser = _browser(page)
        self.assertFalse(asyncio.run(browser._looks_like_interstitial()))
        self.assertTrue(asyncio.run(browser._page_is_gated()))

    def test_a_clean_page_is_not_a_gate(self) -> None:
        page = _FakePage([("ScienceDirect", "AI washing")])
        self.assertFalse(asyncio.run(_browser(page)._page_is_gated()))

    def test_an_unreadable_human_wait_sample_resets_the_clear_streak(self) -> None:
        """This failing means an unreadable sample can advance clearance."""

        page = _TitleScriptPage(
            [
                ("Are you a robot?", "captcha challenge"),
                ("Are you a robot?", "captcha challenge"),
                ("Are you a robot?", "captcha challenge"),  # announcement
                ("ScienceDirect", "AI washing"),
                RuntimeError("navigation is in flight"),
                ("ScienceDirect", "AI washing"),
                ("ScienceDirect", "AI washing"),
                RuntimeError("navigation is in flight"),
                RuntimeError("navigation is in flight"),
            ]
        )
        browser = _browser(
            page,
            human_wait_seconds=0.01,
            human_wait_poll_seconds=0.0,
            human_clear_confirmations=2,
        )
        self.assertFalse(asyncio.run(browser.await_human_clearance()))
        self.assertEqual(page.interactions, [])

    def test_a_gate_followed_only_by_unreadable_samples_ends_at_the_deadline(self) -> None:
        """This failing means unreadable samples can satisfy the clear streak."""

        page = _TitleScriptPage(
            [
                ("Are you a robot?", "captcha challenge"),
                ("Are you a robot?", "captcha challenge"),
                ("Are you a robot?", "captcha challenge"),  # announcement
                RuntimeError("navigation is in flight"),
                RuntimeError("navigation is in flight"),
            ]
        )
        browser = _browser(
            page,
            human_wait_seconds=0.005,
            human_wait_poll_seconds=0.0,
            human_clear_confirmations=2,
        )
        self.assertFalse(asyncio.run(browser.await_human_clearance()))
        self.assertEqual(page.interactions, [])

    def test_a_closed_page_ends_a_human_wait_at_once(self) -> None:
        """This failing means a closed page can consume the human budget."""

        page = _UnreadablePage(closed=True)
        browser = _browser(
            page,
            human_wait_seconds=60.0,
            human_wait_poll_seconds=0.0,
        )
        self.assertFalse(asyncio.run(browser.await_human_clearance()))
        self.assertEqual(page.title_reads, 0)
        self.assertEqual(page.interactions, [])

    def test_bool_gate_apis_still_return_false_for_an_unreadable_page(self) -> None:
        """This failing means the compatibility bool API changed its result."""

        page = _UnreadablePage(closed=False)
        browser = _browser(page)
        self.assertFalse(asyncio.run(browser._looks_like_interstitial()))
        self.assertFalse(asyncio.run(browser._page_is_gated()))


class RotatingGateTests(unittest.TestCase):
    """A single clean sample is not proof the gate is gone."""

    def test_one_clean_poll_between_rotations_does_not_end_the_wait(self) -> None:
        # Cloudflare reloads its own challenge page; the gap reads clean.
        page = _FakePage(
            [
                ("Are you a robot?", "captcha challenge"),
                ("ScienceDirect", ""),            # rotation gap
                ("Are you a robot?", "captcha challenge"),
                ("Are you a robot?", "captcha challenge"),
            ]
        )
        browser = _browser(
            page,
            interstitial_wait_seconds=0.001,
            human_wait_seconds=0.05,
            human_wait_poll_seconds=0.001,
        )
        self.assertFalse(asyncio.run(browser.settle_page_gates()))
        self.assertEqual(page.interactions, [])

    def test_a_genuinely_cleared_gate_is_confirmed_and_continues(self) -> None:
        page = _FakePage(
            [
                ("Are you a robot?", "captcha challenge"),
                ("ScienceDirect", "AI washing"),
                ("ScienceDirect", "AI washing"),
                ("ScienceDirect", "AI washing"),
                ("ScienceDirect", "AI washing"),
            ]
        )
        browser = _browser(
            page,
            interstitial_wait_seconds=0.001,
            human_wait_seconds=5.0,
            human_wait_poll_seconds=0.001,
        )
        self.assertTrue(asyncio.run(browser.settle_page_gates()))
        self.assertEqual(page.interactions, [])

    def test_confirmation_count_is_at_least_two(self) -> None:
        from hunnu_harness.browser.playwright_backend import (
            DEFAULT_HUMAN_CLEAR_CONFIRMATIONS,
        )

        self.assertGreaterEqual(DEFAULT_HUMAN_CLEAR_CONFIRMATIONS, 2)


if __name__ == "__main__":
    unittest.main()
