"""A third-party title hosted under ``/org/`` never said whether it had full text.

A live run on 2026-09-18 found 10.1108/jfra-03-2025-0162 -- Journal of Financial
Reporting and Accounting, an Emerald title ScienceDirect hosts at
``/org/science/article/abs/pii/S1985251726000401``.  Search, metadata and the
search/detail identity lock all succeeded.  The access check then spent its whole
twenty-second window and ended ``ARTICLE_READINESS_TIMEOUT``: SOURCE_LAYOUT_CHANGED,
access Unknown.  The run kept no page content, so the page was read once more on
2026-09-19, from the tab that run had left open -- no navigation, no reload.
Two things were on it, and either alone would have kept it undecided:

  * It has no PDF control of its own and no ScienceDirect access box.  Where
    "View PDF" would sit there is a link out to the publisher, "View at
    publisher", and its own "View full text" link is disabled.  None of the
    no-full-text phrases the adapter knew occurs anywhere on the page, inline
    state included.
  * The only PDF controls on it are the "View PDF" links of the "Recommended
    articles" panel, each bound to another article's PII.  The adapter refused
    them, correctly -- and stopped reading there, before it ever looked for a
    notice.  So a new phrase alone would have changed nothing.

What is pinned here.  The hand-off beside this article's own disabled full-text
link is a refusal, read once, and it lands on the existing fail-closed
METADATA_ONLY decision.  The hand-off alone is not: it is a way out, and a hosted
title the session is entitled to could carry it while its own PDF control is
still rendering.  Someone else's PDF still never authorises this article: not as
a readiness verdict, and not by the refusal being handed to a decision that takes
the first PDF-like control it meets.  A page with neither this article's control
nor a notice is still undecided and still times out.  Both signals are rendered
links, not words that happen to be on the page.  The fixture is a reduction of
the observed page; see its header.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.browser.commands import (
    BrowserObservation,
    NavigateCommand,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from hunnu_harness.literature.adapters import sciencedirect as module
from hunnu_harness.literature.adapters.sciencedirect import (
    ArticleReadiness,
    ScienceDirectAdapter,
)
from hunnu_harness.literature.models import (
    UNKNOWN,
    AccessType,
    LiteratureSearchRequest,
    RunStatus,
)
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow
from hunnu_harness.paths import TEMP_DIR

FIXTURES = Path(__file__).parent / "fixtures" / "literature"
ORG_PAGE = (FIXTURES / "sciencedirect_org_hosted_abstract.html").read_text(encoding="utf-8")

ORG_PII = "S1985251726000401"
RECOMMENDED_PII = "S0313592622001746"
ORG_URL = f"https://www.sciencedirect.com/org/science/article/abs/pii/{ORG_PII}"
ORG_TITLE = (
    "Board nationality diversity and earnings management: the moderating effect of "
    "audit committee independence. Evidence from French listed companies"
)

HANDOFF = (
    '<a class="link-button link-button-primary medium" '
    'href="https://doi.org/10.1108/JFRA-03-2025-0162" target="_blank">'
    '<span class="link-button-text-container"><span class="link-button-text">'
    "View at publisher</span></span></a>"
)


def recommended_card(pii: str = RECOMMENDED_PII) -> str:
    """One card of the "Recommended articles" panel, as observed."""

    return (
        '<li class="related-article-card"><div class="related-article-card-body">'
        '<div class="related-article-card-labels text-xs">Research article '
        '<span class="content-meta-access-label">Full text access</span></div>'
        f'<a class="anchor anchor-secondary" href="/science/article/pii/{pii}">'
        "A recommended article</a></div>"
        '<div><a class="anchor anchor-primary anchor-icon-left anchor-with-icon" '
        f'href="/science/article/pii/{pii}/pdfft?md5=0&amp;pid=1-s2.0-{pii}-main.pdf" '
        'target="_blank" rel="nofollow"><span class="anchor-text-container">'
        '<span class="anchor-text">View PDF</span></span></a></div></li>'
    )


def own_control(href: str) -> str:
    return (
        f'<a class="link-button link-button-primary" aria-label="View PDF" href="{href}">'
        '<span class="link-button-text">View PDF</span></a>'
    )


def fulltext_link(pii: str = ORG_PII, *, disabled: bool = True) -> str:
    """The page's link to an article's full text -- observed disabled, for itself."""

    state = ' aria-disabled="true" tabindex="-1"' if disabled else ""
    return (
        '<a class="anchor u-margin-s-ver anchor-primary" '
        f'href="/org/science/article/pii/{pii}"{state} lang="en">'
        '<span class="anchor-text-container"><span class="anchor-text">'
        "View full text</span></span></a>"
    )


def hosted_page(
    actions: str,
    *,
    recommended: bool = True,
    extra: str = "",
    fulltext: str | None = None,
) -> str:
    """The observed page's shape with its action area swapped out."""

    own_fulltext = fulltext_link() if fulltext is None else fulltext
    panel = (
        '<div id="recommended-articles"><h2>Recommended articles</h2>'
        f"<ul>{recommended_card()}</ul></div>"
        if recommended
        else ""
    )
    return (
        '<!doctype html><html lang="en-US"><head>'
        f"<title>{ORG_TITLE} - ScienceDirect</title>"
        f'<meta name="citation_title" content="{ORG_TITLE}">'
        '<meta name="citation_doi" content="10.1108/JFRA-03-2025-0162">'
        '<meta name="citation_journal_title" content="Journal of Financial Reporting and Accounting">'
        f"</head><body>{extra}"
        f'<div class="content-details-actions"><div class="content-actions">{actions}</div></div>'
        '<div class="body-area"><p>A paraphrased abstract.</p></div>'
        f"{own_fulltext}{panel}</body></html>"
    )


# The panel has rendered; the action area has not.  No control here is this
# article's, and a disabled full-text link with no hand-off is not a notice.
PANEL_ONLY = hosted_page("")
# An ordinary paywalled page whose recommendations carry PDF links too.
PAYWALLED_WITH_PANEL = hosted_page("<p>Get access through your institution</p>")


def observe(html: str, url: str = ORG_URL):
    return ScienceDirectAdapter.observe_article_readiness(html, source_url=url)


class _ScriptedBrowser:
    """Serves a scripted sequence of page states, counting observations."""

    def __init__(self, states: list[str], url: str = ORG_URL) -> None:
        self.states = states
        self.url = url
        self.observations = 0

    async def execute(self, command):  # noqa: ANN001
        index = min(self.observations, len(self.states) - 1)
        self.observations += 1
        html = self.states[index]
        return type(
            "Observation",
            (),
            {"url": self.url, "require_html": lambda self, body=html: body},
        )()


class _FastWindow(unittest.TestCase):
    """The readiness window is wall-clock; shrink it so a red test is not 20s."""

    def setUp(self) -> None:
        self._saved = (
            module.ARTICLE_RENDER_TIMEOUT_SECONDS,
            module.ARTICLE_RENDER_POLL_SECONDS,
        )
        module.ARTICLE_RENDER_TIMEOUT_SECONDS = 0.3
        module.ARTICLE_RENDER_POLL_SECONDS = 0.01

    def tearDown(self) -> None:
        (
            module.ARTICLE_RENDER_TIMEOUT_SECONDS,
            module.ARTICLE_RENDER_POLL_SECONDS,
        ) = self._saved


def run_access(states: list[str]) -> tuple:
    adapter = ScienceDirectAdapter(_ScriptedBrowser(states))
    decision = asyncio.run(adapter.check_fulltext_access())
    return decision, adapter


class ObservedPageTests(_FastWindow):
    def test_the_fixture_is_the_page_that_was_observed(self) -> None:
        """Every PDF control on it is someone else's, and no known phrase occurs.

        This failing means the fixture stopped being the observed page, and the
        tests below stopped being about it.
        """

        parser = ScienceDirectAdapter._parser(ORG_PAGE)
        controls = [anchor.href for anchor in parser.anchors if "/pdfft" in anchor.href]
        self.assertTrue(controls)
        for href in controls:
            self.assertNotIn(ORG_PII, href)
        haystack = parser.body_text.casefold()
        for marker in module._NO_FULLTEXT_MARKERS:
            self.assertNotIn(marker, haystack)

    def test_the_org_prefix_does_not_change_which_article_this_is(self) -> None:
        self.assertEqual(observe(ORG_PAGE).page_pii, ORG_PII)

    def test_a_publisher_handoff_is_a_refusal_not_an_undecided_page(self) -> None:
        observation = observe(ORG_PAGE)
        self.assertIs(observation.readiness, ArticleReadiness.FULLTEXT_NOT_AUTHORIZED)
        self.assertTrue(observation.decided)

    def test_the_refusal_is_the_existing_metadata_only_decision(self) -> None:
        decision, adapter = run_access([ORG_PAGE])
        self.assertFalse(decision.full_text_accessible)
        self.assertFalse(decision.authorized_access)
        self.assertIs(decision.access_type, AccessType.METADATA_ONLY)
        self.assertIs(decision.status, RunStatus.FULLTEXT_NOT_AUTHORIZED)
        self.assertNotIn("ARTICLE_READINESS_TIMEOUT", decision.reason)
        self.assertIs(
            adapter.last_article_readiness.readiness,
            ArticleReadiness.FULLTEXT_NOT_AUTHORIZED,
        )

    def test_the_refusal_costs_one_read_not_the_whole_window(self) -> None:
        _decision, adapter = run_access([ORG_PAGE])
        self.assertEqual(adapter.browser.observations, 1)

    def test_a_recommended_articles_pdf_never_becomes_this_articles_full_text(self) -> None:
        """This failing means a refusal was re-decided by the first PDF on the page.

        ``check_fulltext_access_html`` takes the first PDF-like control it meets.
        On this page that is a recommended article's, so a refusal handed to it
        comes back as an authorisation of the wrong paper.
        """

        decision, adapter = run_access([ORG_PAGE])
        self.assertFalse(decision.authorized_access)
        self.assertFalse(decision.full_text_accessible)
        self.assertEqual(decision.download_url, UNKNOWN)
        self.assertNotIn(RECOMMENDED_PII, decision.download_url)
        self.assertIsNot(
            adapter.last_article_readiness.readiness, ArticleReadiness.FULLTEXT_AUTHORIZED
        )

    def test_a_handoff_that_renders_late_is_waited_for_then_refused(self) -> None:
        decision, adapter = run_access([PANEL_ONLY, PANEL_ONLY, ORG_PAGE])
        self.assertIs(decision.access_type, AccessType.METADATA_ONLY)
        self.assertEqual(adapter.browser.observations, 3)


class ForeignControlTests(_FastWindow):
    """Someone else's PDF decides nothing -- in either direction."""

    def test_a_foreign_control_no_longer_hides_an_explicit_notice(self) -> None:
        """The general form: the panel's PDF links used to end the reading."""

        observation = observe(PAYWALLED_WITH_PANEL)
        self.assertIs(observation.readiness, ArticleReadiness.FULLTEXT_NOT_AUTHORIZED)
        decision, _adapter = run_access([PAYWALLED_WITH_PANEL])
        self.assertFalse(decision.authorized_access)
        self.assertIs(decision.access_type, AccessType.METADATA_ONLY)
        self.assertEqual(decision.download_url, UNKNOWN)

    def test_a_foreign_control_with_no_notice_is_still_undecided(self) -> None:
        """This failing means the absence of this article's control began deciding."""

        observation = observe(PANEL_ONLY)
        self.assertIs(observation.readiness, ArticleReadiness.PENDING_RENDER)
        self.assertFalse(observation.decided)
        self.assertEqual(observation.pdf_control_pii, RECOMMENDED_PII)
        self.assertEqual(observation.page_pii, ORG_PII)

    def test_an_undecided_hosted_page_still_times_out_rather_than_deciding(self) -> None:
        decision, _adapter = run_access([PANEL_ONLY])
        self.assertFalse(decision.authorized_access)
        self.assertFalse(decision.full_text_accessible)
        self.assertIs(decision.access_type, AccessType.UNKNOWN)
        self.assertIs(decision.status, RunStatus.SOURCE_LAYOUT_CHANGED)
        self.assertIn("ARTICLE_READINESS_TIMEOUT", decision.reason)

    def test_the_refusal_still_says_whose_control_was_turned_away(self) -> None:
        observation = observe(ORG_PAGE)
        self.assertEqual(observation.pdf_control_pii, RECOMMENDED_PII)
        self.assertEqual(observation.page_pii, ORG_PII)


class OwnControlTests(_FastWindow):
    """The hand-off never outranks a control bound to this article."""

    def test_the_articles_own_control_outranks_the_handoff(self) -> None:
        # Neither shape was observed on the hosted page, which has no control of
        # its own.  The first is the one ScienceDirect is known to use; the
        # second pins that the /org/ prefix would not by itself unbind a control.
        for href in (
            f"/science/article/pii/{ORG_PII}/pdfft?md5=0&amp;pid=1-s2.0-{ORG_PII}-main.pdf",
            f"/org/science/article/pii/{ORG_PII}/pdfft?md5=0&amp;pid=1-s2.0-{ORG_PII}-main.pdf",
        ):
            with self.subTest(href=href):
                html = hosted_page(own_control(href) + HANDOFF)
                observation = observe(html)
                self.assertIs(observation.readiness, ArticleReadiness.FULLTEXT_AUTHORIZED)
                self.assertEqual(observation.pdf_control_pii, ORG_PII)
                decision, _adapter = run_access([html])
                self.assertTrue(decision.authorized_access)
                self.assertIn(ORG_PII, decision.download_url)
                self.assertNotIn(RECOMMENDED_PII, decision.download_url)


class HandoffAloneIsNotARefusalTests(_FastWindow):
    """A way out is not a refusal; the disabled full-text link is what refuses."""

    def test_a_handoff_alone_is_not_a_refusal(self) -> None:
        """This failing means an entitled hosted title can read as refused.

        An open-access hosted title could carry the same link out while its own
        PDF control is still rendering.  Refusing on the link alone would report
        it as out of reach on the first read -- the mistake the readiness wait
        exists to prevent.
        """

        observation = observe(hosted_page(HANDOFF, fulltext=""))
        self.assertIs(observation.readiness, ArticleReadiness.PENDING_RENDER)

    def test_a_disabled_full_text_link_alone_is_not_a_refusal(self) -> None:
        observation = observe(hosted_page("", recommended=False))
        self.assertIs(observation.readiness, ArticleReadiness.PENDING_RENDER)

    def test_an_enabled_full_text_link_is_not_a_refusal(self) -> None:
        html = hosted_page(HANDOFF, fulltext=fulltext_link(disabled=False))
        self.assertIs(observe(html).readiness, ArticleReadiness.PENDING_RENDER)

    def test_another_articles_disabled_link_is_not_this_articles_refusal(self) -> None:
        html = hosted_page(HANDOFF, fulltext=fulltext_link(RECOMMENDED_PII))
        self.assertIs(observe(html).readiness, ArticleReadiness.PENDING_RENDER)

    def test_an_entitled_hosted_title_is_waited_for_not_refused(self) -> None:
        own = own_control(
            f"/science/article/pii/{ORG_PII}/pdfft?md5=0&amp;pid=1-s2.0-{ORG_PII}-main.pdf"
        )
        still_rendering = hosted_page(HANDOFF, fulltext="")
        rendered = hosted_page(own + HANDOFF, fulltext="")
        decision, adapter = run_access([still_rendering, still_rendering, rendered])
        self.assertTrue(decision.authorized_access)
        self.assertIn(ORG_PII, decision.download_url)
        self.assertEqual(adapter.browser.observations, 3)


class HandoffIsARenderedLinkTests(_FastWindow):
    def test_handoff_words_outside_a_link_are_not_a_notice(self) -> None:
        """The adapter's page text includes inline script; three words are not a link.

        This failing means the hand-off became a substring of the page, and any
        page that merely mentions it -- in its inline state, in an abstract --
        reads as a refusal before its own PDF control has rendered.
        """

        html = hosted_page(
            "",
            recommended=False,
            extra=(
                '<script type="application/json">{"labels":{"publisher":"View at publisher"}}'
                "</script><p>Readers may view at publisher sites.</p><p>View at publisher</p>"
            ),
        )
        observation = observe(html)
        self.assertIs(observation.readiness, ArticleReadiness.PENDING_RENDER)

    def test_full_text_words_outside_a_link_are_not_the_disabled_link(self) -> None:
        html = hosted_page(
            HANDOFF,
            recommended=False,
            fulltext='<p aria-disabled="true">View full text</p>',
        )
        self.assertIs(observe(html).readiness, ArticleReadiness.PENDING_RENDER)

    def test_the_label_is_read_whatever_its_case_or_wrapping(self) -> None:
        for label in (
            "View at publisher",
            "VIEW AT PUBLISHER",
            "<span>View at</span>\n      <span>publisher</span>",
            "View   at\n publisher",
        ):
            with self.subTest(label=label):
                html = hosted_page(
                    f'<a href="https://doi.org/10.1108/x">{label}</a>', recommended=False
                )
                self.assertIs(
                    observe(html).readiness, ArticleReadiness.FULLTEXT_NOT_AUTHORIZED
                )


# -- the run of 2026-09-18, replayed ---------------------------------------------


class _Port:
    """A BrowserCommandPort that only navigates and observes."""

    def __init__(self, pages: dict[str, str], downloads_dir: Path) -> None:
        self.pages = pages
        self.session = SessionHandle(value="test")
        self.page_handle = PageHandle(value="page-1", session=self.session)
        self.downloads_dir = downloads_dir
        self.url = "about:blank"
        self.commands: list[tuple[str, str]] = []

    async def execute(self, command):
        if isinstance(command, NavigateCommand):
            self.commands.append(("Navigate", command.url))
            self.url = command.url
        elif isinstance(command, ObserveCommand):
            self.commands.append(("Observe", self.url))
        else:
            raise AssertionError(f"unexpected command {type(command).__name__}")
        key = "search" if "/search?" in self.url else "article"
        return BrowserObservation(
            session=self.session,
            page=self.page_handle,
            generation=len(self.commands),
            url=self.url,
            title="scripted",
            html=self.pages[key] if isinstance(command, ObserveCommand) else None,
        )


SEARCH_PAGE = (
    '<!doctype html><html lang="en"><head><title>Search | ScienceDirect.com</title></head>'
    '<body><ol class="search-result-wrapper" id="srp-results-list">'
    '<li class="ResultItem col-xs-24 push-m"><h2><span>'
    f'<a class="anchor result-list-title-link" href="/org/science/article/abs/pii/{ORG_PII}">'
    f"{ORG_TITLE}</a></span></h2></li></ol></body></html>"
)


class LiveRunReplayTests(_FastWindow):
    def test_the_inspection_event_now_says_metadata_only(self) -> None:
        """2026-09-18 logged SOURCE_LAYOUT_CHANGED / Unknown for this page."""

        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sdorg-", dir=TEMP_DIR) as raw:
            tmp = Path(raw)
            port = _Port({"search": SEARCH_PAGE, "article": ORG_PAGE}, tmp)
            workflow = LiteratureAcquisitionWorkflow(
                ScienceDirectAdapter(port),
                run_root=tmp / "run",
                human_like_delay_seconds=0.0,
                allow_outside_project_for_tests=True,
            )
            request = LiteratureSearchRequest.from_mapping(
                {
                    "OriginalResearchRequest": "hosted third-party title",
                    "DOIs": ["10.1108/jfra-03-2025-0162"],
                    "MaxSearchResults": 1,
                    "MaxResultsPerSource": 1,
                    "MaxDownloads": 0,
                    "MaxDownloadsPerRun": 0,
                }
            )
            result = asyncio.run(workflow.run(request))
            events = [
                json.loads(line)
                for line in (tmp / "run" / "audit" / "LITERATURE_EVENTS.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        inspected = [e for e in events if e["action"] == "literature_result_inspected"]
        self.assertEqual(len(inspected), 1)
        self.assertEqual(inspected[0]["stable_identifier"], ORG_PII)
        self.assertEqual(inspected[0]["status"], RunStatus.FULLTEXT_NOT_AUTHORIZED.value)
        self.assertEqual(inspected[0]["access_type"], AccessType.METADATA_ONLY.value)
        self.assertFalse(inspected[0]["full_text_accessible"])
        self.assertTrue(inspected[0]["target_identity_confirmed"])
        self.assertEqual(result.downloads, [])
        self.assertEqual([kind for kind, _ in port.commands].count("Navigate"), 2)


if __name__ == "__main__":
    unittest.main()
