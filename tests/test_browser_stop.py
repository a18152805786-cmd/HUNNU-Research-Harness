"""browser-stop must stop the browser it reports stopped.

The command connected over CDP through Playwright and called the driver's
``Browser.close()``, which for a browser Playwright merely connected to only
disconnects.  It then reported ``PersistentBrowserStopped=true`` while Chrome
-- the main process on the debugging port and a dozen children -- carried on,
so the documented next step, ``browser-configure-session-restore``, refused
with "Research Chrome profile is in use" and the operator closed Chrome by
hand.

These pin the honest contract: the protocol's own graceful ``Browser.close``
is what is requested; "stopped" is claimed only after the port has gone quiet
and no Chrome process holds the profile; anything short of that is reported
as it is, with a non-zero exit; and ``browser-status`` answers from the same
facts, so the two commands cannot contradict each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from hunnu_harness.browser.persistent_browser import (
    DEFAULT_RESEARCH_DEBUG_PORT,
    PersistentBrowserStatus,
    _close_over_cdp,
    endpoint_for,
    stop_persistent_browser,
)
from hunnu_harness.cli import build_parser, main as harness_main
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
MODULE = "hunnu_harness.browser.persistent_browser"
PROFILE = TEMP_DIR / "stop-profile"


def _sequence(values):
    """Each call yields the next value; the last one repeats forever."""

    remaining = list(values)

    def next_value(*_args, **_kwargs):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return next_value


def _stop(*, probes, held, closes=None, timeout: float = 0.6):
    closes = [] if closes is None else closes
    held_values = held if isinstance(held, list) else [held]
    with mock.patch(f"{MODULE}.probe", side_effect=_sequence(probes)):
        return stop_persistent_browser(
            profile_dir=PROFILE,
            exit_timeout_seconds=timeout,
            request_close=closes.append,
            profile_in_use_check=_sequence(held_values),
        )


class StopTests(unittest.TestCase):
    def test_a_running_browser_is_asked_to_leave_and_confirmed_gone(self) -> None:
        closes: list[str] = []
        result = _stop(probes=[RUNNING, STOPPED], held=False, closes=closes)

        self.assertEqual(closes, [RUNNING.endpoint])
        self.assertTrue(result.was_running)
        self.assertTrue(result.close_requested)
        self.assertFalse(result.running_after)
        self.assertIs(result.profile_in_use, False)
        self.assertTrue(result.stopped)
        self.assertTrue(result.ok)
        payload = result.as_dict()
        self.assertIs(payload["PersistentBrowserStopped"], True)
        self.assertIs(payload["PersistentBrowserRunning"], False)
        self.assertNotIn("Reason", payload)

    def test_a_browser_that_ignores_the_request_is_not_reported_stopped(self) -> None:
        """This failing means "stopped" is again inferred from having asked."""

        result = _stop(probes=[RUNNING], held=False, timeout=0.5)

        self.assertTrue(result.close_requested)
        self.assertTrue(result.running_after)
        self.assertFalse(result.stopped)
        self.assertFalse(result.ok)
        self.assertIn("still listening", result.reason)
        payload = result.as_dict()
        self.assertIs(payload["PersistentBrowserStopped"], False)
        self.assertIs(payload["PersistentBrowserRunning"], True)
        # The profile was never worth asking about while the port answered.
        self.assertEqual(payload["ProfileInUse"], "unknown")

    def test_a_quiet_port_with_processes_still_holding_the_profile_is_not_stopped(self) -> None:
        """The port closing first is exactly what today's stuck stop looked like."""

        result = _stop(probes=[RUNNING, STOPPED], held=True, timeout=0.5)

        self.assertFalse(result.running_after)
        self.assertIs(result.profile_in_use, True)
        self.assertFalse(result.stopped)
        self.assertFalse(result.ok)
        self.assertIn("still held the profile", result.reason)

    def test_children_that_let_go_within_the_wait_count_as_stopped(self) -> None:
        result = _stop(probes=[RUNNING, STOPPED], held=[True, True, False])

        self.assertTrue(result.stopped)
        self.assertIs(result.profile_in_use, False)

    def test_an_unavailable_process_check_is_reported_unknown_and_does_not_veto(self) -> None:
        result = _stop(probes=[RUNNING, STOPPED], held=None)

        self.assertTrue(result.stopped)
        self.assertEqual(result.as_dict()["ProfileInUse"], "unknown")

    def test_nothing_listening_requests_nothing_and_says_so(self) -> None:
        closes: list[str] = []
        result = _stop(probes=[STOPPED], held=False, closes=closes)

        self.assertEqual(closes, [])
        self.assertFalse(result.was_running)
        self.assertFalse(result.close_requested)
        self.assertFalse(result.stopped, "nothing was stopped by this command")
        self.assertTrue(result.ok, "but the end state the operator wants holds")
        self.assertEqual(result.reason, "No persistent Research Chrome is listening")

    def test_nothing_listening_but_a_portless_chrome_holding_the_profile_is_not_ok(self) -> None:
        result = _stop(probes=[STOPPED], held=True)

        self.assertFalse(result.close_requested)
        self.assertFalse(result.ok)
        self.assertIn("holds the profile", result.reason)

    def test_a_request_that_cannot_be_made_is_reported_not_raised(self) -> None:
        """No driver, connection refused: a different fact from "would not leave"."""

        def refuse(_endpoint: str) -> None:
            raise ConnectionRefusedError("no CDP endpoint")

        with mock.patch(f"{MODULE}.probe", return_value=RUNNING):
            result = stop_persistent_browser(
                profile_dir=PROFILE,
                exit_timeout_seconds=0.3,
                request_close=refuse,
                profile_in_use_check=lambda _profile: False,
            )

        self.assertTrue(result.was_running)
        self.assertFalse(result.close_requested)
        self.assertTrue(result.running_after)
        self.assertFalse(result.ok)
        self.assertIn("Could not ask Research Chrome to close", result.reason)
        self.assertIn("ConnectionRefusedError", result.reason)


class _FakeSession:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def send(self, method, params=None):  # noqa: ANN001
        self.sent.append(method)
        if self.fail:
            raise RuntimeError("Target closed")
        return {}


class _FakeBrowser:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.disconnected = False

    async def new_browser_cdp_session(self) -> _FakeSession:
        return self.session

    async def close(self) -> None:
        self.disconnected = True


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.browser = browser
        self.chromium = self
        self.endpoint = ""

    async def connect_over_cdp(self, endpoint):  # noqa: ANN001
        self.endpoint = endpoint
        return self.browser


class _AsyncPlaywrightContext:
    """Stands in for ``async_playwright()``, used as an async context manager."""

    def __init__(self, playwright: _FakePlaywright) -> None:
        self._playwright = playwright

    async def __aenter__(self) -> _FakePlaywright:
        return self._playwright

    async def __aexit__(self, *_exc) -> None:
        return None


class GracefulCloseTests(unittest.TestCase):
    """What is sent is the protocol's Browser.close, not the driver's disconnect."""

    def test_the_protocol_close_is_sent_over_a_browser_level_session(self) -> None:
        session = _FakeSession()
        browser = _FakeBrowser(session)
        playwright = _FakePlaywright(browser)
        with mock.patch(
            "playwright.async_api.async_playwright",
            return_value=_AsyncPlaywrightContext(playwright),
        ):
            asyncio.run(_close_over_cdp(RUNNING.endpoint))

        self.assertEqual(playwright.endpoint, RUNNING.endpoint)
        self.assertEqual(session.sent, ["Browser.close"])
        self.assertTrue(browser.disconnected, "the driver lets go afterwards")

    def test_the_connection_dropping_under_the_request_is_not_an_error(self) -> None:
        """Chrome leaving closes the socket; the probe afterwards is the judge."""

        session = _FakeSession(fail=True)
        browser = _FakeBrowser(session)
        with mock.patch(
            "playwright.async_api.async_playwright",
            return_value=_AsyncPlaywrightContext(_FakePlaywright(browser)),
        ):
            asyncio.run(_close_over_cdp(RUNNING.endpoint))

        self.assertEqual(session.sent, ["Browser.close"])


class CliTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = harness_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_browser_stop_accepts_the_profile_it_checks(self) -> None:
        args = build_parser().parse_args(["browser-stop", "--profile", "somewhere"])
        self.assertEqual(args.profile, Path("somewhere"))

    def test_browser_stop_reports_stopped_only_after_the_browser_left(self) -> None:
        closes: list[str] = []
        with mock.patch(f"{MODULE}.probe", side_effect=_sequence([RUNNING, STOPPED])), mock.patch(
            f"{MODULE}._request_graceful_close", side_effect=closes.append
        ), mock.patch(f"{MODULE}.profile_in_use", return_value=False):
            code, out, _err = self._run(["browser-stop", "--json", "--profile", str(PROFILE)])

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(closes, [RUNNING.endpoint])
        self.assertIs(payload["BrowserCloseRequested"], True)
        self.assertIs(payload["PersistentBrowserStopped"], True)
        self.assertIs(payload["PersistentBrowserRunning"], False)
        self.assertIs(payload["ProfileInUse"], False)
        # The old report's "SessionEnded" claimed something no command can
        # know without inspecting cookies; it is gone rather than guessed.
        self.assertNotIn("SessionEnded", payload)

    def test_browser_stop_does_not_claim_a_stop_that_did_not_happen(self) -> None:
        """This failing means the report again says stopped while Chrome runs."""

        with mock.patch(f"{MODULE}.probe", return_value=RUNNING), mock.patch(
            f"{MODULE}._request_graceful_close"
        ), mock.patch(f"{MODULE}.profile_in_use", return_value=False), mock.patch(
            f"{MODULE}.DEFAULT_EXIT_TIMEOUT_SECONDS", 0.4
        ):
            code, out, _err = self._run(["browser-stop", "--json", "--profile", str(PROFILE)])

        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertIs(payload["PersistentBrowserStopped"], False)
        self.assertIs(payload["PersistentBrowserRunning"], True)
        self.assertIn("still listening", payload["Reason"])

    def test_browser_stop_plain_output_keeps_key_value_lines(self) -> None:
        with mock.patch(f"{MODULE}.probe", side_effect=_sequence([RUNNING, STOPPED])), mock.patch(
            f"{MODULE}._request_graceful_close"
        ), mock.patch(f"{MODULE}.profile_in_use", return_value=False):
            code, out, _err = self._run(["browser-stop", "--profile", str(PROFILE)])

        self.assertEqual(code, 0)
        self.assertIn("PersistentBrowserStopped=true\n", out)
        self.assertIn("ProfileInUse=false\n", out)

    def test_browser_stop_with_nothing_listening_exits_clean_and_says_so(self) -> None:
        with mock.patch(f"{MODULE}.probe", return_value=STOPPED), mock.patch(
            f"{MODULE}._request_graceful_close"
        ) as close, mock.patch(f"{MODULE}.profile_in_use", return_value=False):
            code, out, _err = self._run(["browser-stop", "--json", "--profile", str(PROFILE)])

        self.assertEqual(code, 0)
        close.assert_not_called()
        payload = json.loads(out)
        self.assertIs(payload["PersistentBrowserRunning"], False)
        self.assertIs(payload["BrowserCloseRequested"], False)
        self.assertEqual(payload["Reason"], "No persistent Research Chrome is listening")

    def test_browser_status_reports_whether_the_profile_is_held(self) -> None:
        for held, expected in ((True, True), (False, False), (None, "unknown")):
            with self.subTest(held=held):
                with mock.patch(f"{MODULE}.probe", return_value=STOPPED), mock.patch(
                    f"{MODULE}.profile_in_use", return_value=held
                ):
                    code, out, _err = self._run(["browser-status", "--json", "--profile", str(PROFILE)])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["ProfileInUse"], expected)

    def test_browser_status_and_browser_stop_answer_from_the_same_facts(self) -> None:
        """A stop that did not happen and a status afterwards must agree."""

        with mock.patch(f"{MODULE}.probe", return_value=RUNNING), mock.patch(
            f"{MODULE}._request_graceful_close"
        ), mock.patch(f"{MODULE}.profile_in_use", return_value=True), mock.patch(
            f"{MODULE}.DEFAULT_EXIT_TIMEOUT_SECONDS", 0.4
        ):
            stop_code, stop_out, _ = self._run(["browser-stop", "--json", "--profile", str(PROFILE)])
            status_code, status_out, _ = self._run(["browser-status", "--json", "--profile", str(PROFILE)])

        self.assertEqual((stop_code, status_code), (2, 0))
        stop_payload, status_payload = json.loads(stop_out), json.loads(status_out)
        self.assertIs(stop_payload["PersistentBrowserStopped"], False)
        self.assertIs(stop_payload["PersistentBrowserRunning"], True)
        self.assertIs(status_payload["PersistentBrowserRunning"], True)
        self.assertIs(status_payload["ProfileInUse"], True)


if __name__ == "__main__":
    unittest.main()
