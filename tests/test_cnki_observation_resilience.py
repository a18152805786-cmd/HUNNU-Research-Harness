"""CNKI observation failures stay inside the adapter's bounded settle windows."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.browser.commands import (
    BrowserObservation,
    BrowserTargetObservation,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from hunnu_harness.literature.adapters import cnki as module
from hunnu_harness.literature.adapters.base import (
    SourceActionRequired,
    SourceLayoutChanged,
    SourceUnavailable,
)
from hunnu_harness.literature.adapters.cnki import CNKIAdapter
from hunnu_harness.literature.models import AccessType, LiteratureSearchRequest, RunStatus


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
ARTICLE_URL = (
    "https://kns.cnki.net/kcms2/article/abstract?dbcode=CJFD&filename=KJYJ202601001"
)
SEARCH_URL = "https://kns.cnki.net/kns8s/defaultresult/index?korder=SU&kw=test"
SEARCH_HTML = """
<html><head><title>检索-中国知网</title></head><body>
  <main><div>共找到 1 条结果</div>
    <a class="fz14 inline" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=TEST202601001">Test paper</a>
  </main>
</body></html>
"""
ACCESS_HTML = (FIXTURES / "cnki_abstract_2026.html").read_text(encoding="utf-8")


@dataclass(frozen=True)
class UnreadableRound:
    """The local executor could not read HTML while the page was changing documents."""

    fallback_raises: bool = False

    @property
    def primary_error(self) -> ObservationUnavailable:
        return ObservationUnavailable(
            "Local page HTML observation failed: page is navigating and changing documents"
        )


class ScriptedPort:
    """Serve scripted observations and reject every non-observation settle action."""

    navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

    def __init__(self, states: list[object], *, initial_url: str = SEARCH_URL) -> None:
        self.states = states
        self.current_url = initial_url
        self.allow_initial_navigation = initial_url == SEARCH_URL
        self.session = SessionHandle("cnki-observation-resilience")
        self.page_handle = PageHandle("main", session=self.session)
        self.commands: list[object] = []
        self.primary_reads = 0
        self._pending_fallback: UnreadableRound | None = None

    def _observation(
        self,
        *,
        html: str | None = None,
        structured_content: object | None = None,
        url: str | None = None,
        title: str = "检索-中国知网",
        target_observations: tuple[BrowserTargetObservation, ...] = (),
    ) -> BrowserObservation:
        return BrowserObservation(
            session=self.session,
            page=self.page_handle,
            generation=len(self.commands),
            url=url or self.current_url,
            title=title,
            html=html,
            structured_content=structured_content,
            target_observations=target_observations,
        )

    async def execute(self, command):
        self.commands.append(command)
        if isinstance(command, NavigateCommand):
            if not self.allow_initial_navigation or any(
                isinstance(previous, NavigateCommand) for previous in self.commands[:-1]
            ):
                raise AssertionError("CNKI settle re-reading must not navigate")
            self.current_url = command.url
            return self._observation()
        if not isinstance(command, ObserveCommand):
            raise AssertionError(
                f"CNKI settle re-reading may only observe, got {type(command).__name__}"
            )
        if command.include_html:
            if self._pending_fallback is not None:
                raise AssertionError("a fallback observation was not consumed")
            if self.primary_reads >= len(self.states):
                raise AssertionError("the adapter exceeded the scripted observation window")
            state = self.states[self.primary_reads]
            self.primary_reads += 1
            if isinstance(state, UnreadableRound):
                self._pending_fallback = state
                raise state.primary_error
            if isinstance(state, BaseException):
                raise state
            if isinstance(state, BrowserObservation):
                return state
            if isinstance(state, str):
                return self._observation(html=state)
            raise AssertionError(f"unsupported scripted state: {state!r}")

        if command.include_visible_text:
            raise AssertionError("the fallback must not request visible text")
        pending = self._pending_fallback
        if pending is None:
            raise AssertionError("fallback observation was issued without an HTML failure")
        self._pending_fallback = None
        if pending.fallback_raises:
            raise ObservationUnavailable("fallback structured snapshot observation failed")
        return self._observation()


def search_request() -> LiteratureSearchRequest:
    return LiteratureSearchRequest(
        original_research_request="CNKI observation resilience",
        keywords_en=("test",),
        max_search_results=1,
        max_results_per_source=1,
        max_downloads=0,
        max_downloads_per_run=0,
    )


def visible_challenge_observation(port: ScriptedPort) -> BrowserObservation:
    return port._observation(
        html=SEARCH_HTML,
        target_observations=(
            BrowserTargetObservation(
                marker="验证码",
                playwright_visible=True,
                bounding_box={"x": 10, "y": 10, "width": 100, "height": 40},
                client_rect={"x": 10, "y": 10, "width": 100, "height": 40},
                client_width=100,
                client_height=40,
                viewport_width=1280,
                viewport_height=720,
                frame_viewport_visible=True,
                inspection_complete=True,
            ),
        ),
    )


class CNKIObservationResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_between_document_search_read_is_reobserved_and_succeeds(self) -> None:
        """This failing means a transient local HTML read aborts the CNKI search."""
        port = ScriptedPort([UnreadableRound(), SEARCH_HTML])
        with patch.object(module, "_SEARCH_SETTLE_DELAY_SECONDS", 0):
            records = await CNKIAdapter(port).search("test", search_request())

        self.assertEqual([record.title for record in records], ["Test paper"])
        self.assertEqual(port.primary_reads, 2)
        self.assertTrue(all(isinstance(command, ObserveCommand) for command in port.commands[1:]))

    async def test_a_between_document_access_read_is_reobserved_and_succeeds(self) -> None:
        """This failing means a transient local HTML read aborts the CNKI access check."""
        port = ScriptedPort([UnreadableRound(), ACCESS_HTML], initial_url=ARTICLE_URL)
        with patch.object(module, "_ACCESS_SETTLE_DELAY_SECONDS", 0):
            decision = await CNKIAdapter(port).check_fulltext_access()

        self.assertTrue(decision.full_text_accessible)
        self.assertTrue(decision.authorized_access)
        self.assertIs(decision.status, RunStatus.SUCCESS)
        self.assertEqual(port.primary_reads, 2)
        self.assertTrue(all(isinstance(command, ObserveCommand) for command in port.commands))

    async def test_an_unreadable_search_page_is_bounded_and_preserves_its_original_cause(self) -> None:
        """This failing means unreadable rounds escape early or lose the local observation cause."""
        for fallback_raises in (False, True):
            with self.subTest(fallback_raises=fallback_raises):
                port = ScriptedPort(
                    [
                        UnreadableRound(fallback_raises=fallback_raises)
                        for _ in range(module._SEARCH_SETTLE_MAX_OBSERVATIONS)
                    ]
                )
                with patch.object(module, "_SEARCH_SETTLE_DELAY_SECONDS", 0):
                    with self.assertRaises(SourceUnavailable) as caught:
                        await CNKIAdapter(port).search("test", search_request())

                self.assertEqual(port.primary_reads, module._SEARCH_SETTLE_MAX_OBSERVATIONS)
                self.assertIsInstance(caught.exception.__cause__, ObservationUnavailable)
                self.assertIn("HTML observation", str(caught.exception))
                self.assertIn("structured browser snapshot", str(caught.exception))

    async def test_an_unreadable_access_page_is_bounded_and_preserves_its_original_cause(self) -> None:
        """This failing means access observation failures escape their bounded settle window."""
        port = ScriptedPort(
            [UnreadableRound() for _ in range(module._ACCESS_SETTLE_MAX_OBSERVATIONS)],
            initial_url=ARTICLE_URL,
        )

        with patch.object(module, "_ACCESS_SETTLE_DELAY_SECONDS", 0):
            with self.assertRaises(SourceUnavailable) as caught:
                await CNKIAdapter(port).check_fulltext_access()

        self.assertEqual(port.primary_reads, module._ACCESS_SETTLE_MAX_OBSERVATIONS)
        self.assertIsInstance(caught.exception.__cause__, ObservationUnavailable)
        self.assertEqual(len(port.commands), module._ACCESS_SETTLE_MAX_OBSERVATIONS * 2)
        self.assertTrue(all(isinstance(command, ObserveCommand) for command in port.commands))

    async def test_a_readable_undetermined_access_followed_by_unreadable_rounds_never_becomes_authorized(self) -> None:
        """This failing means an earlier unknown decision can be mistaken for authorization after unreadable rereads."""
        undetermined_html = (FIXTURES / "cnki_article_live_structure.html").read_text(encoding="utf-8")
        port = ScriptedPort(
            [
                undetermined_html,
                *[UnreadableRound() for _ in range(module._ACCESS_SETTLE_MAX_OBSERVATIONS - 1)],
            ],
            initial_url=ARTICLE_URL,
        )

        with patch.object(module, "_ACCESS_SETTLE_DELAY_SECONDS", 0):
            decision = await CNKIAdapter(port).check_fulltext_access()

        self.assertFalse(decision.full_text_accessible)
        self.assertFalse(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.UNKNOWN)
        self.assertEqual(decision.status, RunStatus.SOURCE_LAYOUT_CHANGED)
        self.assertEqual(port.primary_reads, module._ACCESS_SETTLE_MAX_OBSERVATIONS)
        self.assertTrue(all(isinstance(command, ObserveCommand) for command in port.commands))

    async def test_a_challenge_on_a_reread_stops_before_any_later_command(self) -> None:
        """This failing means the unreadable-round catch swallows a later CNKI challenge."""
        port = ScriptedPort([UnreadableRound()])
        port.states.append(visible_challenge_observation(port))

        with patch.object(module, "_SEARCH_SETTLE_DELAY_SECONDS", 0):
            with self.assertRaises(SourceActionRequired):
                await CNKIAdapter(port).search("test", search_request())

        self.assertEqual(port.primary_reads, 2)
        self.assertTrue(all(isinstance(command, ObserveCommand) for command in port.commands[1:]))
        self.assertEqual(len(port.commands), 4)

    async def test_target_page_identity_uncertainty_on_a_reread_propagates(self) -> None:
        """This failing means a target-page guard is incorrectly treated as unreadable."""
        uncertain = BrowserObservation(
            session=SessionHandle("uncertain-session"),
            page=PageHandle("uncertain-page", session=SessionHandle("uncertain-session")),
            generation=1,
            url="about:blank",
            title="",
            html=SEARCH_HTML,
        )
        port = ScriptedPort([UnreadableRound(), uncertain])

        with patch.object(module, "_SEARCH_SETTLE_DELAY_SECONDS", 0):
            with self.assertRaises(SourceLayoutChanged) as caught:
                await CNKIAdapter(port).search("test", search_request())

        self.assertIn("TargetPageIdentity=uncertain", str(caught.exception))
        self.assertEqual(port.primary_reads, 2)
        self.assertEqual(len(port.commands), 4)

    async def test_an_unrelated_browser_port_error_is_not_retried(self) -> None:
        """This failing means the settle loop catches more than its private unreadable error."""
        port = ScriptedPort([SourceUnavailable("Browser command port is unavailable")])

        with self.assertRaisesRegex(SourceUnavailable, "Browser command port is unavailable"):
            await CNKIAdapter(port).search("test", search_request())

        self.assertEqual(port.primary_reads, 1)
        self.assertEqual(len(port.commands), 2)
        self.assertIsInstance(port.commands[-1], ObserveCommand)


if __name__ == "__main__":
    unittest.main()
