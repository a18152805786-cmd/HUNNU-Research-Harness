"""Waiting for an article page to say whether it has full text.

The same failure as the search page, one step later.  ``page.goto`` returns at
``domcontentloaded``; ScienceDirect assembles the PDF control after that.  Read
at that moment, an open-access article the institution is entitled to looks
exactly like one behind a paywall, and was reported as one -- measured on the
live page as 172 KB with no control against 1.67 MB with it.

What is pinned here is that the wait changed only *when* the page is read.  The
decision is the same function on the same HTML, so a page that genuinely has no
full text still has none; a page still rendering is PENDING rather than a
refusal; and the control has to belong to the article being read, because
another paper's PDF is not this paper's full text.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from hunnu_harness.literature.adapters.sciencedirect import (
    ArticleReadiness,
    ScienceDirectAdapter,
)
from hunnu_harness.literature.models import AccessType, RunStatus

PII = "S1544612326002151"
OTHER_PII = "S0165410109000469"
ARTICLE_URL = f"https://www.sciencedirect.com/science/article/pii/{PII}"
FIXTURES = Path(__file__).parent / "fixtures" / "literature"


def page(body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head>'
        "<title>AI washing: Strategic disclosure and backlash - ScienceDirect</title>"
        f"</head><body>{body}</body></html>"
    )


def pdf_control(pii: str = PII) -> str:
    return (
        f'<a class="anchor" aria-label="Download PDF" '
        f'href="/science/article/pii/{pii}/pdfft?md5=abc&pid=1-s2.0-{pii}-main.pdf">'
        "<span>View PDF</span></a>"
    )


# The early page: the strings are already there, the control is not.  This is
# what made the moment so hard to tell apart from a real refusal.
STILL_RENDERING = page(
    '<h1>AI washing: Strategic disclosure and backlash</h1>'
    "<p>Finance Research Letters</p>"
    '<div id="pdf-download-placeholder"></div>'
)
AUTHORIZED = page(
    "<h1>AI washing: Strategic disclosure and backlash</h1>"
    "<p>Open access</p>" + pdf_control()
)
FOREIGN_CONTROL = page(
    "<h1>AI washing: Strategic disclosure and backlash</h1>" + pdf_control(OTHER_PII)
)
PAYWALLED = page(
    "<h1>Some other paper</h1>"
    "<p>Get access through your institution to read the full text.</p>"
)
PURCHASE = page("<h1>Some other paper</h1><p>Purchase PDF</p>")


def observe(html: str, url: str = ARTICLE_URL):
    return ScienceDirectAdapter.observe_article_readiness(html, source_url=url)


class ReadinessVerdictTests(unittest.TestCase):
    def test_a_page_still_rendering_is_pending_not_a_refusal(self) -> None:
        observation = observe(STILL_RENDERING)
        self.assertIs(observation.readiness, ArticleReadiness.PENDING_RENDER)
        self.assertFalse(observation.decided)
        self.assertIsNot(observation.readiness, ArticleReadiness.FULLTEXT_NOT_AUTHORIZED)

    def test_a_rendered_control_is_authorized_and_bound_to_this_article(self) -> None:
        observation = observe(AUTHORIZED)
        self.assertIs(observation.readiness, ArticleReadiness.FULLTEXT_AUTHORIZED)
        self.assertEqual(observation.pdf_control_pii, PII)
        self.assertEqual(observation.page_pii, PII)
        self.assertTrue(observation.decided)

    def test_a_control_for_another_paper_never_authorizes_this_one(self) -> None:
        """Fail closed: someone else's PDF leaves this page undecided."""

        observation = observe(FOREIGN_CONTROL)
        self.assertIsNot(observation.readiness, ArticleReadiness.FULLTEXT_AUTHORIZED)
        self.assertIs(observation.readiness, ArticleReadiness.PENDING_RENDER)
        self.assertEqual(observation.pdf_control_pii, OTHER_PII)
        self.assertEqual(observation.page_pii, PII)

    def test_an_explicit_no_fulltext_notice_is_decisive(self) -> None:
        for html in (PAYWALLED, PURCHASE):
            with self.subTest(html=html[:60]):
                observation = observe(html)
                self.assertIs(
                    observation.readiness, ArticleReadiness.FULLTEXT_NOT_AUTHORIZED
                )
                self.assertTrue(observation.decided)

    def test_a_login_page_is_an_interruption_not_a_refusal(self) -> None:
        login = (FIXTURES / "sciencedirect_login.html").read_text(encoding="utf-8")
        observation = observe(login)
        self.assertIs(observation.readiness, ArticleReadiness.INTERRUPTED)
        self.assertIsNot(observation.readiness, ArticleReadiness.FULLTEXT_NOT_AUTHORIZED)

    def test_a_challenge_page_is_an_interruption_not_a_refusal(self) -> None:
        challenge = (FIXTURES / "sciencedirect_captcha.html").read_text(encoding="utf-8")
        observation = observe(challenge)
        self.assertIs(observation.readiness, ArticleReadiness.INTERRUPTED)


class DecisionUnchangedTests(unittest.TestCase):
    """The wait moved; the authorization criteria did not."""

    def test_the_archived_authorized_fixture_still_reads_authorized(self) -> None:
        html = (FIXTURES / "sciencedirect_article_authorized.html").read_text(
            encoding="utf-8"
        )
        decision = ScienceDirectAdapter.check_fulltext_access_html(
            html, source_url=ARTICLE_URL
        )
        self.assertTrue(decision.full_text_accessible)
        self.assertTrue(decision.authorized_access)

    def test_the_archived_metadata_only_fixture_still_reads_unauthorized(self) -> None:
        html = (FIXTURES / "sciencedirect_article_metadata_only.html").read_text(
            encoding="utf-8"
        )
        decision = ScienceDirectAdapter.check_fulltext_access_html(
            html, source_url=ARTICLE_URL
        )
        self.assertFalse(decision.full_text_accessible)
        self.assertFalse(decision.authorized_access)
        self.assertIs(decision.access_type, AccessType.METADATA_ONLY)

    def test_readiness_and_the_decision_agree_on_a_rendered_page(self) -> None:
        """Readiness ends exactly where the decision says yes, by construction."""

        observation = observe(AUTHORIZED)
        decision = ScienceDirectAdapter.check_fulltext_access_html(
            AUTHORIZED, source_url=ARTICLE_URL
        )
        self.assertIs(observation.readiness, ArticleReadiness.FULLTEXT_AUTHORIZED)
        self.assertTrue(decision.authorized_access)
        self.assertIn(PII, decision.download_url)


class _ScriptedBrowser:
    """Serves a scripted sequence of page states, counting observations."""

    def __init__(self, states: list[str]) -> None:
        self.states = states
        self.observations = 0

    async def execute(self, command):  # noqa: ANN001
        index = min(self.observations, len(self.states) - 1)
        self.observations += 1
        html = self.states[index]
        return type(
            "Observation",
            (),
            {"url": ARTICLE_URL, "require_html": lambda self, body=html: body},
        )()


def run_access(states: list[str]) -> tuple:
    adapter = ScienceDirectAdapter(_ScriptedBrowser(states))
    decision = asyncio.run(adapter.check_fulltext_access())
    return decision, adapter


class BoundedWaitTests(unittest.TestCase):
    """No fixed sleep: a ready page costs one read, a slow one costs a few."""

    def test_an_already_rendered_page_is_read_once(self) -> None:
        from hunnu_harness.literature.adapters import sciencedirect as module

        decision, adapter = run_access([AUTHORIZED])
        self.assertTrue(decision.authorized_access)
        self.assertEqual(adapter.browser.observations, 1)
        # Never slept.  Exact zero is not assertable: where time.monotonic() ticks
        # every 15.6 ms (GetTickCount64 on Windows) a tick landing inside this
        # sub-millisecond read reports 14-16, in about 0.6% of runs.  The loop's
        # only sleep is one poll interval, so less than that is "did not sleep".
        self.assertLess(
            adapter.last_article_readiness_wait_ms,
            module.ARTICLE_RENDER_POLL_SECONDS * 1000,
        )

    def test_a_late_control_is_waited_for_then_authorized(self) -> None:
        decision, adapter = run_access(
            [STILL_RENDERING, STILL_RENDERING, AUTHORIZED]
        )
        self.assertTrue(decision.authorized_access)
        self.assertIs(decision.access_type, AccessType.OPEN_ACCESS)
        self.assertEqual(adapter.browser.observations, 3)
        self.assertIs(
            adapter.last_article_readiness.readiness,
            ArticleReadiness.FULLTEXT_AUTHORIZED,
        )

    def test_a_late_refusal_is_waited_for_then_denied(self) -> None:
        decision, adapter = run_access([STILL_RENDERING, PAYWALLED])
        self.assertFalse(decision.authorized_access)
        self.assertIs(decision.access_type, AccessType.METADATA_ONLY)
        self.assertGreaterEqual(adapter.browser.observations, 2)

    def test_a_page_that_never_decides_times_out_rather_than_refusing(self) -> None:
        """The distinction the whole repair exists for."""

        from hunnu_harness.literature.adapters import sciencedirect as module

        original = module.ARTICLE_RENDER_TIMEOUT_SECONDS
        module.ARTICLE_RENDER_TIMEOUT_SECONDS = 1.0
        try:
            decision, adapter = run_access([STILL_RENDERING])
        finally:
            module.ARTICLE_RENDER_TIMEOUT_SECONDS = original

        self.assertFalse(decision.authorized_access)
        self.assertIs(decision.access_type, AccessType.UNKNOWN)
        self.assertIsNot(decision.access_type, AccessType.METADATA_ONLY)
        self.assertIs(decision.status, RunStatus.SOURCE_LAYOUT_CHANGED)
        self.assertIn("ARTICLE_READINESS_TIMEOUT", decision.reason)

    def test_a_foreign_control_times_out_rather_than_authorizing(self) -> None:
        from hunnu_harness.literature.adapters import sciencedirect as module

        original = module.ARTICLE_RENDER_TIMEOUT_SECONDS
        module.ARTICLE_RENDER_TIMEOUT_SECONDS = 1.0
        try:
            decision, _adapter = run_access([FOREIGN_CONTROL])
        finally:
            module.ARTICLE_RENDER_TIMEOUT_SECONDS = original

        self.assertFalse(decision.authorized_access)
        self.assertIn("ARTICLE_READINESS_TIMEOUT", decision.reason)


if __name__ == "__main__":
    unittest.main()
