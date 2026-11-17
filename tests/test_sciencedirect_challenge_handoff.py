"""Challenge words on a live ScienceDirect page are probed, then handed over or let pass.

The case behind this (2026-11-17): a DOI run stopped on the article page with
"Challenge text present in page content but not confirmed visible", the browser
marked not ready, and no step named.  The user clicked View PDF once in the
Research Chrome, the publisher's check passed (``crasolve=1``), and the same
acquire-batch command, rerun unchanged, downloaded both papers.

What is pinned here (AGENTS.md Rules 23, 24, 31, 72):

* text is never the verdict: on a live page the words are probed for
  visibility with the typed ``ObserveCommand`` text probe, and nothing else is
  issued -- no click, no typing, no second navigation;
* a rendered challenge on a page that is not showing its content stops as
  VISIBLE_CHALLENGE with the browser handed over and the step named;
* anything the probe cannot settle -- no matches, an incomplete inspection, a
  failed probe, or a rendered occurrence on a page already showing its own
  content -- stops exactly as before, CHALLENGE_TEXT_UNVERIFIED, now with the
  step named too;
* words proven not rendered (a hidden widget) are page text: the run goes on;
* a login page is a login, unprobed.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from hunnu_harness.browser.commands import (
    BrowserObservation,
    BrowserTargetObservation,
    NavigateCommand,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from hunnu_harness.literature.adapters import sciencedirect as module
from hunnu_harness.literature.adapters.base import HumanActionReason, SourceActionRequired
from hunnu_harness.literature.adapters.sciencedirect import ScienceDirectAdapter
from hunnu_harness.literature.models import LiteratureSearchRequest, RunStatus

FIXTURES = Path(__file__).parent / "fixtures" / "literature"
CAPTCHA_PAGE = (FIXTURES / "sciencedirect_captcha.html").read_text(encoding="utf-8")
LOGIN_PAGE = (FIXTURES / "sciencedirect_login.html").read_text(encoding="utf-8")

PII = "S0304405X13002675"
TITLE = "Betting against beta"
ARTICLE_URL = f"https://www.sciencedirect.com/science/article/pii/{PII}"
SEARCH_URL = "https://www.sciencedirect.com/search?qs=q"
HIDDEN_WIDGET = '<div style="display:none">This site is protected by reCAPTCHA.</div>'


def article(*, extra: str = "", abstract: str = "Leverage constraints.", pdf: bool = True) -> str:
    control = (
        f'<a class="anchor" aria-label="View PDF" href="/science/article/pii/{PII}/pdfft?'
        f'md5=abc&pid=1-s2.0-{PII}-main.pdf"><span>View PDF</span></a>'
        if pdf
        else ""
    )
    return (
        '<!doctype html><html lang="en"><head>'
        f"<title>{TITLE} - ScienceDirect</title>"
        f'<meta name="citation_title" content="{TITLE}">'
        '<meta name="citation_journal_title" content="Journal of Financial Economics">'
        '<meta name="citation_doi" content="10.1016/j.jfineco.2013.10.005">'
        '<meta name="citation_publication_date" content="2014/01/01">'
        f"</head><body><h1>{TITLE}</h1><p>{abstract}</p>{control}{extra}</body></html>"
    )


def results(*titles: str, extra: str = "") -> str:
    cards = "".join(
        f'<li class="ResultItem"><h2><a class="result-list-title-link" '
        f'href="/science/article/pii/S00000000000{i}">{t}</a></h2></li>'
        for i, t in enumerate(titles)
    )
    return (
        '<!doctype html><html lang="en"><head><title>Search | ScienceDirect.com</title></head>'
        f'<body><ol class="search-result-wrapper" id="srp-results-list">{cards}</ol>{extra}</body></html>'
    )


def node(*, visible: bool | None, complete: bool = True, blocking: bool = False) -> BrowserTargetObservation:
    return BrowserTargetObservation(
        marker="captcha",
        playwright_visible=visible,
        bounding_box={"x": 10.0, "y": 10.0, "width": 200.0, "height": 40.0},
        inspection_complete=complete,
        blocking_overlay=blocking,
        viewport_width=1280,
        viewport_height=720,
    )


class ProbePort:
    """Serves page states; answers a text-probe observation with scripted evidence.

    Anything but a navigation or an observation fails the test.
    """

    def __init__(self, url: str, states: list[str], probe=(), *, probe_complete=True, probe_error=None) -> None:
        self.session = SessionHandle(value="test")
        self.page_handle = PageHandle(value="page-1", session=self.session)
        self.url = url
        self.states = states
        self.reads = 0
        self.probe = tuple(probe)
        self.probe_complete = probe_complete
        self.probe_error = probe_error
        self.commands: list[str] = []

    async def execute(self, command):
        if isinstance(command, NavigateCommand):
            self.commands.append("Navigate")
            self.url = command.url
            return BrowserObservation(session=self.session, page=self.page_handle, generation=0, url=self.url)
        if isinstance(command, ObserveCommand) and command.text_probes:
            self.commands.append("Probe")
            if self.probe_error is not None:
                raise self.probe_error
            return BrowserObservation(
                session=self.session,
                page=self.page_handle,
                generation=len(self.commands),
                url=self.url,
                target_observations=self.probe,
                inspection_complete=self.probe_complete,
            )
        if isinstance(command, ObserveCommand):
            self.commands.append("Observe")
            html = self.states[min(self.reads, len(self.states) - 1)]
            self.reads += 1
            return BrowserObservation(
                session=self.session, page=self.page_handle, generation=len(self.commands), url=self.url, html=html
            )
        raise AssertionError(f"Only navigate and observe are allowed, got {type(command).__name__}")


def request() -> LiteratureSearchRequest:
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": "challenge handoff",
            "KeywordsEN": ["beta"],
            "MaxSearchResults": 5,
            "MaxResultsPerSource": 5,
            "MaxDownloads": 0,
        }
    )


class _Fast(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (
            module.SEARCH_RENDER_TIMEOUT_SECONDS,
            module.SEARCH_RENDER_POLL_SECONDS,
            module.ARTICLE_RENDER_TIMEOUT_SECONDS,
            module.ARTICLE_RENDER_POLL_SECONDS,
        )
        module.SEARCH_RENDER_TIMEOUT_SECONDS = 0.3
        module.SEARCH_RENDER_POLL_SECONDS = 0.01
        module.ARTICLE_RENDER_TIMEOUT_SECONDS = 0.3
        module.ARTICLE_RENDER_POLL_SECONDS = 0.01

    def tearDown(self) -> None:
        (
            module.SEARCH_RENDER_TIMEOUT_SECONDS,
            module.SEARCH_RENDER_POLL_SECONDS,
            module.ARTICLE_RENDER_TIMEOUT_SECONDS,
            module.ARTICLE_RENDER_POLL_SECONDS,
        ) = self._saved

    def metadata(self, port: ProbePort):
        return asyncio.run(ScienceDirectAdapter(port).extract_metadata(search_query="q"))

    def access(self, port: ProbePort):
        return asyncio.run(ScienceDirectAdapter(port).check_fulltext_access())

    def search(self, port: ProbePort):
        return asyncio.run(ScienceDirectAdapter(port).search("q", request()))

    def assertOnlyLooked(self, port: ProbePort) -> None:
        self.assertLessEqual(set(port.commands), {"Navigate", "Observe", "Probe"})


class VisibleChallengeIsHandedOver(_Fast):
    def test_a_rendered_challenge_on_the_article_page_names_the_step(self) -> None:
        port = ProbePort(ARTICLE_URL, [CAPTCHA_PAGE], probe=[node(visible=True)])
        with self.assertRaises(SourceActionRequired) as caught:
            self.metadata(port)
        error = caught.exception
        self.assertIs(error.status, RunStatus.ACTION_REQUIRED_USER_LOGIN)
        self.assertIs(error.reason, HumanActionReason.VISIBLE_CHALLENGE)
        self.assertTrue(error.challenge_visible)
        self.assertTrue(error.browser_ready_for_manual_action)
        message = str(error)
        self.assertIn("BrowserReadyForManualAction=true", message)
        self.assertIn("ManualStep=", message)
        self.assertIn("click View PDF once", message)
        self.assertIn("run the same command again", message)
        self.assertIn("VisibilityProbe=matches:1,visible:1", message)
        self.assertEqual(port.commands.count("Probe"), 1)
        self.assertOnlyLooked(port)

    def test_a_blocking_overlay_counts_as_visible(self) -> None:
        port = ProbePort(ARTICLE_URL, [CAPTCHA_PAGE], probe=[node(visible=False, blocking=True)])
        with self.assertRaises(SourceActionRequired) as caught:
            self.access(port)
        self.assertIs(caught.exception.reason, HumanActionReason.VISIBLE_CHALLENGE)

    def test_a_rendered_challenge_on_the_search_page_names_the_search_step(self) -> None:
        port = ProbePort(SEARCH_URL, [CAPTCHA_PAGE], probe=[node(visible=True)])
        with self.assertRaises(SourceActionRequired) as caught:
            self.search(port)
        self.assertIs(caught.exception.reason, HumanActionReason.VISIBLE_CHALLENGE)
        self.assertIn("complete any verification the publisher shows", str(caught.exception))
        # Already on this query's page: resumed in place, never navigated away.
        self.assertEqual(port.commands.count("Navigate"), 0)
        self.assertOnlyLooked(port)


class UnsettledEvidenceStopsAsBefore(_Fast):
    def assertUnverified(self, error: SourceActionRequired) -> None:
        self.assertIs(error.reason, HumanActionReason.CHALLENGE_TEXT_UNVERIFIED)
        self.assertFalse(error.challenge_visible)
        self.assertFalse(error.browser_ready_for_manual_action)
        self.assertIn("Challenge text present", str(error))
        self.assertIn("ManualStep=", str(error))

    def test_no_probe_matches_is_not_proof_of_hidden(self) -> None:
        port = ProbePort(ARTICLE_URL, [CAPTCHA_PAGE], probe=[])
        with self.assertRaises(SourceActionRequired) as caught:
            self.metadata(port)
        self.assertUnverified(caught.exception)

    def test_an_incomplete_inspection_is_not_proof_of_hidden(self) -> None:
        port = ProbePort(ARTICLE_URL, [CAPTCHA_PAGE], probe=[node(visible=False, complete=False)])
        with self.assertRaises(SourceActionRequired) as caught:
            self.metadata(port)
        self.assertUnverified(caught.exception)

    def test_a_probe_that_reports_incomplete_overall_is_not_proof(self) -> None:
        port = ProbePort(ARTICLE_URL, [CAPTCHA_PAGE], probe=[node(visible=False)], probe_complete=False)
        with self.assertRaises(SourceActionRequired) as caught:
            self.metadata(port)
        self.assertUnverified(caught.exception)

    def test_a_failed_probe_stops_rather_than_passing(self) -> None:
        port = ProbePort(ARTICLE_URL, [CAPTCHA_PAGE], probe_error=RuntimeError("probe broke"))
        with self.assertRaises(SourceActionRequired) as caught:
            self.access(port)
        self.assertUnverified(caught.exception)
        self.assertIn("VisibilityProbe=failed:RuntimeError", str(caught.exception))

    def test_visible_words_on_an_article_showing_its_content_are_not_a_seen_challenge(self) -> None:
        """An abstract about CAPTCHAs is rendered too; visibility cannot tell them apart."""

        page = article(abstract="We study CAPTCHA design and user friction.")
        port = ProbePort(ARTICLE_URL, [page], probe=[node(visible=True)])
        with self.assertRaises(SourceActionRequired) as caught:
            self.metadata(port)
        self.assertUnverified(caught.exception)
        self.assertIn("content_present:true", str(caught.exception))

    def test_visible_words_in_a_result_title_are_not_a_seen_challenge(self) -> None:
        port = ProbePort(SEARCH_URL, [results("CAPTCHA and bots")], probe=[node(visible=True)])
        with self.assertRaises(SourceActionRequired) as caught:
            self.search(port)
        self.assertUnverified(caught.exception)


class HiddenWordsArePageText(_Fast):
    def test_a_hidden_widget_does_not_stop_metadata(self) -> None:
        port = ProbePort(ARTICLE_URL, [article(extra=HIDDEN_WIDGET)], probe=[node(visible=False)])
        record = self.metadata(port)
        self.assertEqual(record.title, TITLE)
        self.assertEqual(record.doi, "10.1016/j.jfineco.2013.10.005")
        self.assertOnlyLooked(port)

    def test_a_hidden_widget_does_not_stop_the_access_decision(self) -> None:
        port = ProbePort(ARTICLE_URL, [article(extra=HIDDEN_WIDGET)], probe=[node(visible=False)])
        decision = self.access(port)
        self.assertTrue(decision.authorized_access)
        self.assertIn(PII, decision.download_url)

    def test_a_hidden_widget_does_not_stop_the_search(self) -> None:
        port = ProbePort(
            SEARCH_URL, [results("Betting against beta", extra=HIDDEN_WIDGET)], probe=[node(visible=False)]
        )
        records = self.search(port)
        self.assertEqual([r.title for r in records], ["Betting against beta"])

    def test_one_rendered_occurrence_among_hidden_ones_still_stops(self) -> None:
        port = ProbePort(ARTICLE_URL, [CAPTCHA_PAGE], probe=[node(visible=False), node(visible=True)])
        with self.assertRaises(SourceActionRequired) as caught:
            self.metadata(port)
        self.assertIs(caught.exception.reason, HumanActionReason.VISIBLE_CHALLENGE)


class LoginIsStillALogin(_Fast):
    def test_a_login_page_is_reported_as_a_login_without_a_probe(self) -> None:
        port = ProbePort(ARTICLE_URL, [LOGIN_PAGE], probe=[node(visible=True)])
        with self.assertRaises(SourceActionRequired) as caught:
            self.metadata(port)
        self.assertIs(caught.exception.reason, HumanActionReason.LOGIN_REQUIRED)
        self.assertNotIn("Probe", port.commands)


class StaticAnalysisIsUnchanged(unittest.TestCase):
    def test_static_html_still_never_claims_a_visible_challenge(self) -> None:
        with self.assertRaises(SourceActionRequired) as caught:
            ScienceDirectAdapter.parse_article_html(CAPTCHA_PAGE, source_url=ARTICLE_URL)
        self.assertIs(caught.exception.reason, HumanActionReason.CHALLENGE_TEXT_UNVERIFIED)
        self.assertFalse(caught.exception.challenge_observed)

    def test_interstitial_stop_names_the_step(self) -> None:
        stop = ScienceDirectAdapter._interstitial_stop("article")
        self.assertIn("ManualStep=", str(stop))
        self.assertIn("click View PDF once", str(stop))


if __name__ == "__main__":
    unittest.main()
