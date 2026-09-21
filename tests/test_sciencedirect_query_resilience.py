"""Consecutive ScienceDirect queries, and the ways one used to go wrong.

A live run on 2026-09-18 sent four queries.  The first returned candidates; the
second died with ``Search failed: ObservationUnavailable``; the third and fourth
were logged as clean empty searches, twenty seconds each.  None of the last
three was what it said.  The publisher had begun challenging the session, and:

  * a read that coincided with the challenge page reloading itself was fatal and
    opaque -- ``page.content()`` refuses while a navigation is in flight, and the
    workflow could only log the exception's class name;
  * a bot-check interstitial carrying no CAPTCHA words fell through the result
    parser as an empty, apparently successful search;
  * so did a page that never rendered anything decisive;
  * and because none of those stopped the run, the next query was sent into an
    active challenge.

The same run failed the search/detail identity lock for all four candidates of
its first query: ``extract_metadata`` read each article page the instant
navigation returned, with no idea what kind of page it was holding.

What is pinned here.  Only a page that says "no results" is an empty search.  A
gate stops the run for a person and is never touched -- these tests fail if the
adapter issues anything but a navigation or an observation.  A page between
documents is re-read inside the same bounded window rather than abandoned, and
re-reading never navigates again.  One query's page never answers for another's.
"""

from __future__ import annotations

import asyncio
import csv
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from hunnu_harness.browser.commands import (
    BrowserCommandError,
    BrowserObservation,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from hunnu_harness.literature.adapters import sciencedirect as module
from hunnu_harness.literature.adapters.base import (
    HumanActionReason,
    LiteratureSourceError,
    SourceActionRequired,
    SourceLayoutChanged,
    SourceUnavailable,
)
from hunnu_harness.literature.adapters.sciencedirect import (
    ArticlePageState,
    ScienceDirectAdapter,
    SearchPageType,
)
from hunnu_harness.literature.models import UNKNOWN, LiteratureSearchRequest, RunStatus
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow
from hunnu_harness.paths import TEMP_DIR

FIXTURES = Path(__file__).parent / "fixtures" / "literature"
CAPTCHA_FIXTURE = (FIXTURES / "sciencedirect_captcha.html").read_text(encoding="utf-8")
LOGIN_FIXTURE = (FIXTURES / "sciencedirect_login.html").read_text(encoding="utf-8")


# -- pages ---------------------------------------------------------------------


def card(pii: str, title: str) -> str:
    return (
        f'<li class="ResultItem col-xs-24 push-m" data-doi="10.1016/j.test.{pii}">'
        f'<h2><span><a id="title-{pii}" class="anchor result-list-title-link" '
        f'href="/science/article/pii/{pii}">{title}</a></span></h2>'
        f'<a class="anchor" href="/science/article/pii/{pii}/pdfft?pid=1-s2.0-{pii}-main.pdf">'
        "<span>View PDF</span></a></li>"
    )


def search_shell(body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><title>Search | ScienceDirect.com</title>'
        f"</head><body>{body}</body></html>"
    )


def results_page(*cards: str) -> str:
    return search_shell(
        '<ol class="search-result-wrapper" id="srp-results-list">' + "".join(cards) + "</ol>"
    )


def article_page(pii: str, title: str, *, abstract: str = "An abstract.") -> str:
    return (
        '<!doctype html><html lang="en"><head>'
        f"<title>{title} - ScienceDirect</title>"
        f'<meta name="citation_title" content="{title}">'
        '<meta name="citation_journal_title" content="Finance Research Letters">'
        f'<meta name="citation_doi" content="10.1016/j.test.{pii.casefold()}">'
        '<meta name="citation_publication_date" content="2026/01/01">'
        f"</head><body><h1>{title}</h1><p>{abstract}</p></body></html>"
    )


NO_RESULTS = search_shell("<main><h2>No results found</h2></main>")
# Neither a results list nor a no-results notice: the page has not decided.
UNDECIDED = search_shell('<div id="srp-results-list-container"></div>')
ARTICLE_SHELL = (
    '<!doctype html><html lang="en"><head><title>ScienceDirect</title></head>'
    '<body><div id="root"></div></body></html>'
)
# The publisher's bot-check as observed: a localized "Just a moment" title and
# no CAPTCHA wording anywhere, which is exactly why it slipped past the detector.
INTERSTITIAL_ZH = (
    '<!doctype html><html lang="zh-CN"><head><title>请稍候…</title></head>'
    '<body><div class="main-wrapper"><noscript>Enable JavaScript and cookies to continue'
    "</noscript></div></body></html>"
)
INTERSTITIAL_EN = (
    '<!doctype html><html lang="en"><head><title>Just a moment...</title></head>'
    '<body><div class="main-wrapper"><noscript>Enable JavaScript and cookies to continue'
    "</noscript></div></body></html>"
)

A1 = ("S1544612326002151", "AI washing under media pressure: evidence from China")
A2 = ("S0165410109000469", "How does financial reporting quality relate to investment")
B1 = ("S0304405X24000011", "Audit effort and earnings management")


def navigating() -> ObservationUnavailable:
    """What the local executor raises when ``page.content()`` lands mid-navigation."""

    try:
        try:
            raise RuntimeError(
                "Page.content: Unable to retrieve content because the page is "
                "navigating and changing the content."
            )
        except RuntimeError as cause:
            raise ObservationUnavailable(
                "Local page HTML observation failed: Error"
            ) from cause
    except ObservationUnavailable as error:
        return error


# -- the browser stand-in -------------------------------------------------------


class ScriptedPort:
    """A BrowserCommandPort that serves scripted page states per navigation.

    A state is either HTML or an exception to raise for that read.  Anything but
    a navigation or an observation fails the test: a gate is never clicked,
    typed into, or otherwise answered.
    """

    def __init__(self, script, downloads_dir: Path | None = None) -> None:
        self.script = script
        self.session = SessionHandle(value="test")
        self.page_handle = PageHandle(value="page-1", session=self.session)
        self.downloads_dir = downloads_dir or Path(tempfile.gettempdir())
        self.url = "about:blank"
        self.states: list = [""]
        self.reads = 0
        self.commands: list[tuple[str, str]] = []

    @property
    def navigations(self) -> list[str]:
        return [target for kind, target in self.commands if kind == "Navigate"]

    @property
    def search_navigations(self) -> list[str]:
        return [url for url in self.navigations if "/search?" in url]

    @property
    def article_navigations(self) -> list[str]:
        return [url for url in self.navigations if "/science/article/" in url]

    def _observation(self, html: str | None) -> BrowserObservation:
        return BrowserObservation(
            session=self.session,
            page=self.page_handle,
            generation=len(self.commands),
            url=self.url,
            title="scripted",
            html=html,
        )

    async def execute(self, command):
        if isinstance(command, NavigateCommand):
            self.commands.append(("Navigate", command.url))
            self.url = command.url
            self.states = list(self.script(command.url))
            self.reads = 0
            return self._observation(None)
        if isinstance(command, ObserveCommand):
            self.commands.append(("Observe", self.url))
            state = self.states[min(self.reads, len(self.states) - 1)]
            self.reads += 1
            if isinstance(state, BaseException):
                raise state
            return self._observation(state)
        raise AssertionError(
            f"ScienceDirect search may only navigate and observe, got {type(command).__name__}"
        )


def by_query(pages: dict[str, list]):
    """Route a search URL to its scripted states by the decoded ``qs`` value."""

    def script(url: str) -> list:
        parsed = urlsplit(url)
        if parsed.path == "/search":
            query = parse_qs(parsed.query)["qs"][0]
            return pages[query]
        return pages[parsed.path.rsplit("/", 1)[-1]]

    return script


def request(*keywords: str, max_results: int = 5) -> LiteratureSearchRequest:
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": "query resilience",
            "KeywordsEN": list(keywords) or ["AI washing"],
            "MaxSearchResults": max_results,
            "MaxResultsPerSource": max_results,
            "MaxDownloads": 0,
            "MaxDownloadsPerRun": 0,
        }
    )


class _FastWindows(unittest.TestCase):
    """The readiness windows are wall-clock; shrink them so the suite is not."""

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


def search(port: ScriptedPort, query: str, req: LiteratureSearchRequest | None = None):
    adapter = ScienceDirectAdapter(port)
    return adapter, asyncio.run(adapter.search(query, req or request()))


# -- Case 1 and Case 5 ----------------------------------------------------------


class ConsecutiveQueryTests(_FastWindows):
    def test_a_normal_results_page_yields_candidates(self) -> None:
        port = ScriptedPort(by_query({"first": [results_page(card(*A1), card(*A2))]}))
        adapter, records = search(port, "first")
        self.assertEqual([r.stable_identifier for r in records], [A1[0], A2[0]])
        self.assertEqual(records[0].title, A1[1])
        self.assertIs(adapter.last_search_observation.page_type, SearchPageType.RESULTS_PRESENT)

    def test_each_query_is_answered_by_its_own_page(self) -> None:
        port = ScriptedPort(
            by_query(
                {
                    '"AI washing"': [results_page(card(*A1), card(*A2))],
                    '"earnings management" AND "audit"': [results_page(card(*B1))],
                }
            )
        )
        adapter = ScienceDirectAdapter(port)
        first = asyncio.run(adapter.search('"AI washing"', request()))
        second = asyncio.run(adapter.search('"earnings management" AND "audit"', request()))

        self.assertEqual([r.stable_identifier for r in first], [A1[0], A2[0]])
        self.assertEqual([r.stable_identifier for r in second], [B1[0]])
        self.assertEqual(second[0].search_query, '"earnings management" AND "audit"')
        # Two independent navigations, each carrying its own query and nothing else.
        self.assertEqual(
            [parse_qs(urlsplit(url).query)["qs"][0] for url in port.search_navigations],
            ['"AI washing"', '"earnings management" AND "audit"'],
        )

    def test_an_empty_query_after_a_full_one_is_empty_not_stale(self) -> None:
        port = ScriptedPort(
            by_query({"full": [results_page(card(*A1), card(*A2))], "empty": [NO_RESULTS]})
        )
        adapter = ScienceDirectAdapter(port)
        self.assertEqual(len(asyncio.run(adapter.search("full", request()))), 2)
        self.assertEqual(asyncio.run(adapter.search("empty", request())), [])
        self.assertIs(
            adapter.last_search_observation.page_type, SearchPageType.GENUINE_ZERO_RESULTS
        )

    def test_a_failed_query_does_not_poison_the_next_one(self) -> None:
        port = ScriptedPort(
            by_query({"stuck": [UNDECIDED], "fine": [results_page(card(*B1))]})
        )
        adapter = ScienceDirectAdapter(port)
        with self.assertRaises(SourceLayoutChanged):
            asyncio.run(adapter.search("stuck", request()))
        records = asyncio.run(adapter.search("fine", request()))
        self.assertEqual([r.stable_identifier for r in records], [B1[0]])


# -- the ObservationUnavailable of 2026-09-18 -----------------------------------


class PageBetweenDocumentsTests(_FastWindows):
    def test_a_read_that_lands_mid_navigation_is_not_fatal(self) -> None:
        """The exact shape of the second query of the live run."""

        port = ScriptedPort(by_query({"second": [navigating(), results_page(card(*B1))]}))
        _adapter, records = search(port, "second")
        self.assertEqual([r.stable_identifier for r in records], [B1[0]])
        self.assertEqual([kind for kind, _ in port.commands], ["Navigate", "Observe", "Observe"])

    def test_re_reading_never_navigates_again(self) -> None:
        """Waiting for the page is not reloading it: one query, one navigation."""

        port = ScriptedPort(
            by_query({"second": [navigating(), navigating(), navigating(), results_page(card(*B1))]})
        )
        search(port, "second")
        self.assertEqual(len(port.navigations), 1)

    def test_an_undecided_page_followed_by_an_unreadable_one_still_settles(self) -> None:
        port = ScriptedPort(
            by_query({"q": [UNDECIDED, navigating(), UNDECIDED, results_page(card(*A1))]})
        )
        _adapter, records = search(port, "q")
        self.assertEqual([r.stable_identifier for r in records], [A1[0]])

    def test_a_page_that_is_never_readable_is_a_graded_source_error(self) -> None:
        """Bounded, and reported as what it is -- not as a bare browser error.

        The workflow logs a ``LiteratureSourceError`` with its message and a
        graded status; anything else it can only name by class, which is all
        ``Search failed: ObservationUnavailable`` ever said.
        """

        port = ScriptedPort(by_query({"q": [navigating()]}))
        with self.assertRaises(SourceUnavailable) as caught:
            search(port, "q")
        error = caught.exception
        self.assertIsInstance(error, LiteratureSourceError)
        self.assertNotIsInstance(error, BrowserCommandError)
        self.assertIs(error.status, RunStatus.SOURCE_UNAVAILABLE)
        self.assertIn("PAGE_OBSERVATION_TIMEOUT", str(error))
        self.assertIn("page is navigating", str(error))
        self.assertEqual(len(port.navigations), 1)

    def test_the_reported_cause_is_sanitized(self) -> None:
        try:
            try:
                raise RuntimeError(
                    "navigating to https://www.sciencedirect.com/search?qs=x&token=SECRETVALUE"
                )
            except RuntimeError as cause:
                raise ObservationUnavailable("Local page HTML observation failed: Error") from cause
        except ObservationUnavailable as prepared:
            unreadable = prepared

        port = ScriptedPort(by_query({"q": [unreadable]}))
        with self.assertRaises(SourceUnavailable) as caught:
            search(port, "q")
        self.assertNotIn("SECRETVALUE", str(caught.exception))
        self.assertNotIn("token=", str(caught.exception))


# -- Case 3 and Case 4 ----------------------------------------------------------


class GateIsNotAnEmptySearchTests(_FastWindows):
    def test_an_interstitial_without_captcha_words_stops_for_a_person(self) -> None:
        """The third and fourth queries of the live run, logged as ``0,0,unknown``."""

        for label, html in (("zh", INTERSTITIAL_ZH), ("en", INTERSTITIAL_EN)):
            with self.subTest(locale=label):
                port = ScriptedPort(by_query({"q": [html]}))
                with self.assertRaises(SourceActionRequired) as caught:
                    search(port, "q")
                error = caught.exception
                self.assertIs(error.status, RunStatus.ACTION_REQUIRED_USER_LOGIN)
                self.assertIn("ACTION_REQUIRED_USER_LOGIN=true", str(error))
                self.assertIn("interstitial", str(error))
                # Gate text read from a live page, visibility never probed: the
                # verdict may not claim a challenge the user can see.
                self.assertIs(error.reason, HumanActionReason.CHALLENGE_TEXT_UNVERIFIED)
                self.assertTrue(error.challenge_observed)
                self.assertFalse(error.challenge_visible)
                self.assertFalse(error.browser_ready_for_manual_action)

    def test_a_gate_is_only_ever_navigated_to_and_observed(self) -> None:
        """No click, no typing, no reload -- and not a second navigation either."""

        port = ScriptedPort(by_query({"q": [INTERSTITIAL_ZH]}))
        with self.assertRaises(SourceActionRequired):
            search(port, "q")
        self.assertEqual({kind for kind, _ in port.commands}, {"Navigate", "Observe"})
        self.assertEqual(len(port.navigations), 1)

    def test_a_gate_is_decided_at_once_not_polled(self) -> None:
        port = ScriptedPort(by_query({"q": [INTERSTITIAL_ZH]}))
        with self.assertRaises(SourceActionRequired):
            search(port, "q")
        self.assertEqual(port.reads, 1)

    def test_the_captcha_fixture_still_stops_through_the_live_path(self) -> None:
        port = ScriptedPort(by_query({"q": [CAPTCHA_FIXTURE]}))
        with self.assertRaises(SourceActionRequired) as caught:
            search(port, "q")
        self.assertIn("Challenge text present", str(caught.exception))
        self.assertIs(caught.exception.reason, HumanActionReason.CHALLENGE_TEXT_UNVERIFIED)

    def test_the_login_fixture_still_stops_as_a_login(self) -> None:
        port = ScriptedPort(by_query({"q": [LOGIN_FIXTURE]}))
        with self.assertRaises(SourceActionRequired) as caught:
            search(port, "q")
        self.assertIs(caught.exception.reason, HumanActionReason.LOGIN_REQUIRED)

    def test_a_page_that_never_decides_is_not_an_empty_search(self) -> None:
        port = ScriptedPort(by_query({"q": [UNDECIDED]}))
        with self.assertRaises(SourceLayoutChanged) as caught:
            search(port, "q")
        self.assertIn("SEARCH_READINESS_TIMEOUT", str(caught.exception))
        self.assertIs(caught.exception.status, RunStatus.SOURCE_LAYOUT_CHANGED)

    def test_a_genuinely_empty_search_is_still_empty(self) -> None:
        port = ScriptedPort(by_query({"q": [NO_RESULTS]}))
        adapter, records = search(port, "q")
        self.assertEqual(records, [])
        self.assertIs(
            adapter.last_search_observation.page_type, SearchPageType.GENUINE_ZERO_RESULTS
        )

    def test_an_empty_search_that_echoes_a_gate_word_is_still_empty(self) -> None:
        """A search page repeats the query; the query is not the page's verdict."""

        for phrase in ("one moment in time", "attention required", "安全检查"):
            with self.subTest(phrase=phrase):
                html = search_shell(
                    f'<main><h2>No results found</h2><p>Your search for "{phrase}" '
                    "did not match any articles.</p></main>"
                )
                observation = ScienceDirectAdapter.observe_search_page(
                    html, source_url="https://www.sciencedirect.com/search?qs=x"
                )
                self.assertIs(observation.page_type, SearchPageType.GENUINE_ZERO_RESULTS)
                port = ScriptedPort(by_query({"q": [html]}))
                _adapter, records = search(port, "q")
                self.assertEqual(records, [])


# -- Case 2 ---------------------------------------------------------------------


class DefensiveCardParsingTests(_FastWindows):
    BROKEN_CARDS = (
        # a card whose title anchor is empty
        '<li class="ResultItem"><h2><a class="result-list-title-link" '
        'href="/science/article/pii/S0000000000000001"></a></h2></li>'
        # a card with no anchor at all
        '<li class="ResultItem"><h2><span>Title with no link</span></h2></li>'
        # a card whose link is not an article path
        '<li class="ResultItem"><h2><a class="result-list-title-link" '
        'href="/science/journal/15446123">A journal, not a paper</a></h2></li>'
        # a card with no href attribute
        '<li class="ResultItem"><h2><a class="result-list-title-link">No href</a></h2></li>'
    )

    def test_a_card_missing_its_fields_does_not_cost_the_other_cards(self) -> None:
        html = results_page(card(*A1), self.BROKEN_CARDS, card(*B1))
        records = ScienceDirectAdapter.parse_search_results_html(
            html, query="q", source_url="https://www.sciencedirect.com/search?qs=q"
        )
        self.assertEqual([r.stable_identifier for r in records], [A1[0], B1[0]])

    def test_the_same_page_through_the_live_path_is_not_a_layout_failure(self) -> None:
        port = ScriptedPort(
            by_query({"q": [results_page(card(*A1), self.BROKEN_CARDS, card(*B1))]})
        )
        _adapter, records = search(port, "q")
        self.assertEqual([r.stable_identifier for r in records], [A1[0], B1[0]])

    def test_a_page_of_only_unreadable_cards_is_an_error_not_an_empty_search(self) -> None:
        port = ScriptedPort(by_query({"q": [results_page(self.BROKEN_CARDS)]}))
        with self.assertRaises(SourceLayoutChanged) as caught:
            search(port, "q")
        self.assertIn("ParserFailureDetected=true", str(caught.exception))


# -- the article page -----------------------------------------------------------


ARTICLE_URL = f"https://www.sciencedirect.com/science/article/pii/{A1[0]}"


def observe_article(html: str) -> ArticlePageState:
    return ScienceDirectAdapter.observe_article_page(html, source_url=ARTICLE_URL)


class ArticlePageStateTests(_FastWindows):
    def test_the_states_are_told_apart(self) -> None:
        self.assertIs(observe_article(article_page(*A1)), ArticlePageState.METADATA_PRESENT)
        self.assertIs(observe_article(INTERSTITIAL_ZH), ArticlePageState.PAGE_GATE)
        self.assertIs(observe_article(INTERSTITIAL_EN), ArticlePageState.PAGE_GATE)
        self.assertIs(observe_article(CAPTCHA_FIXTURE), ArticlePageState.INTERRUPTED)
        self.assertIs(observe_article(ARTICLE_SHELL), ArticlePageState.PENDING)
        self.assertFalse(observe_article(ARTICLE_SHELL).decided)

    def test_a_title_outranks_gate_words_in_the_article_itself(self) -> None:
        html = article_page(*A1, abstract="For one moment, attention required of auditors rose.")
        self.assertIs(observe_article(html), ArticlePageState.METADATA_PRESENT)

    def _adapter_on(self, states: list) -> tuple[ScienceDirectAdapter, ScriptedPort]:
        port = ScriptedPort(lambda _url: states)
        adapter = ScienceDirectAdapter(port)
        asyncio.run(port.execute(NavigateCommand(ARTICLE_URL)))
        return adapter, port

    def test_a_gate_stops_as_a_gate_not_as_a_failed_identity_lock(self) -> None:
        adapter, port = self._adapter_on([INTERSTITIAL_ZH])
        with self.assertRaises(SourceActionRequired) as caught:
            asyncio.run(adapter.extract_metadata(search_query="q"))
        self.assertIn("article page", str(caught.exception))
        self.assertEqual({kind for kind, _ in port.commands}, {"Navigate", "Observe"})

    def test_an_already_rendered_article_is_read_once(self) -> None:
        adapter, port = self._adapter_on([article_page(*A1)])
        record = asyncio.run(adapter.extract_metadata(search_query="q"))
        self.assertEqual(record.title, A1[1])
        self.assertEqual(port.reads, 1)

    def test_metadata_that_arrives_late_is_waited_for(self) -> None:
        adapter, port = self._adapter_on(
            [ARTICLE_SHELL, navigating(), ARTICLE_SHELL, article_page(*A1)]
        )
        record = asyncio.run(adapter.extract_metadata(search_query="q"))
        self.assertEqual(record.title, A1[1])
        self.assertEqual(record.doi, f"10.1016/j.test.{A1[0].casefold()}")
        self.assertEqual(port.reads, 4)

    def test_a_page_that_never_presents_a_title_still_fails_closed_downstream(self) -> None:
        """The wait changes when the page is read, never what the lock accepts."""

        adapter, _port = self._adapter_on([ARTICLE_SHELL])
        record = asyncio.run(adapter.extract_metadata(search_query="q"))
        self.assertEqual(record.title, UNKNOWN)
        self.assertEqual(record.doi, UNKNOWN)


# -- the whole run --------------------------------------------------------------


def run_workflow(script, *keywords: str, max_results: int = 5, search_pacer=None):
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sdresil-", dir=TEMP_DIR) as raw:
        tmp = Path(raw)
        port = ScriptedPort(script, downloads_dir=tmp)
        if search_pacer is not None:
            search_pacer.port = port
        workflow = LiteratureAcquisitionWorkflow(
            ScienceDirectAdapter(port),
            run_root=tmp / "run",
            human_like_delay_seconds=0.0,
            allow_outside_project_for_tests=True,
            search_pacer=search_pacer,
        )
        result = asyncio.run(workflow.run(request(*keywords, max_results=max_results)))
        with workflow.writer.query_log_path.open(encoding="utf-8-sig", newline="") as handle:
            query_log = list(csv.DictReader(handle))
        return port, result, query_log


class RunStopsAtTheGateTests(_FastWindows):
    """The 2026-09-18 run, replayed: three planned queries, a gate on the second."""

    KEYWORDS = ("AI washing", "earnings management", "discretionary accruals", "earnings quality")
    Q1 = '"AI washing" AND "earnings management"'
    Q2 = '"AI washing" AND "discretionary accruals"'
    Q3 = '"AI washing" AND "earnings quality"'

    def test_a_gate_on_the_second_query_stops_the_run_for_a_person(self) -> None:
        port, result, query_log = run_workflow(
            by_query(
                {
                    self.Q1: [results_page(card(*A1))],
                    A1[0]: [article_page(*A1)],
                    self.Q2: [navigating(), INTERSTITIAL_ZH],
                    self.Q3: [results_page(card(*B1))],
                }
            ),
            *self.KEYWORDS,
        )
        self.assertIs(result.status, RunStatus.ACTION_REQUIRED_USER_LOGIN)
        self.assertIn("interstitial", result.action_required_reason)
        # The first query's candidate survives, with its metadata read.
        self.assertEqual([r.title for r in result.records], [A1[1]])
        self.assertEqual(result.records[0].year, "2026")
        # The third query is never sent into the active challenge.
        self.assertEqual(len(port.search_navigations), 2)
        self.assertEqual([row["GeneratedQuery"] for row in query_log], [self.Q1, self.Q2])
        self.assertEqual(query_log[0]["Errors"], UNKNOWN)
        self.assertIn("ACTION_REQUIRED_USER_LOGIN=true", query_log[1]["Errors"])
        self.assertNotIn("ObservationUnavailable", query_log[1]["Errors"])

    def test_without_a_gate_every_query_completes(self) -> None:
        port, result, query_log = run_workflow(
            by_query(
                {
                    self.Q1: [results_page(card(*A1))],
                    A1[0]: [article_page(*A1)],
                    self.Q2: [navigating(), results_page(card(*B1))],
                    B1[0]: [article_page(*B1)],
                    self.Q3: [NO_RESULTS],
                }
            ),
            *self.KEYWORDS,
        )
        self.assertIs(result.status, RunStatus.SUCCESS)
        self.assertEqual([r.title for r in result.records], [A1[1], B1[1]])
        self.assertEqual([row["ResultsReturned"] for row in query_log], ["1", "1", "0"])
        self.assertEqual([row["Errors"] for row in query_log], [UNKNOWN, UNKNOWN, UNKNOWN])
        self.assertEqual(len(port.search_navigations), 3)

    def test_a_gate_on_an_article_page_stops_further_article_navigations(self) -> None:
        """All four lock failures of the live run, and the three pages it should not have opened."""

        port, result, _query_log = run_workflow(
            by_query(
                {
                    self.Q1: [results_page(card(*A1), card(*A2), card(*B1))],
                    A1[0]: [INTERSTITIAL_ZH],
                    A2[0]: [article_page(*A2)],
                    B1[0]: [article_page(*B1)],
                }
            ),
            "AI washing",
            "earnings management",
        )
        self.assertIs(result.status, RunStatus.ACTION_REQUIRED_USER_LOGIN)
        self.assertEqual(len(port.article_navigations), 1)
        self.assertFalse(
            any("identity lock failed" in error for error in result.errors), result.errors
        )



# -- Case 6: an explicit refusal by the publisher -------------------------------


def block_page(reference: str = "a3d722eaede511e1", code: str = "CPE00001") -> str:
    """Elsevier's refusal page, as read from the live page on 2026-09-19.

    The run artifacts keep no page content, so this wording was captured out
    of band during that run.  Note what it does NOT contain: no CAPTCHA or
    login words, no result cards, and no no-results notice -- which is exactly
    why it used to be read as PENDING for the whole render budget and then
    reported as a layout change.
    """

    return (
        '<!doctype html><html lang="en"><head><title>ScienceDirect</title></head><body>'
        "<h1>There was a problem providing the content you requested</h1>"
        "<p>Please contact our support team for more information and provide the "
        "details below.</p>"
        f"<p>Reference number: {reference}</p>"
        "<p>IP Address: 203.0.113.10</p>"
        "<p>Timestamp: 2026-09-19 08:14:22 UTC</p>"
        f"<p>{code}</p>"
        "</body></html>"
    )


class PublisherBlockIsNotALayoutChangeTests(_FastWindows):
    """2026-09-19: ScienceDirect refused the session and said so.

    The page carried a support reference number.  It was classified PENDING,
    waited out the 20s render budget, and surfaced as SOURCE_LAYOUT_CHANGED --
    so the run reported a broken adapter, went on to the next query against an
    active block, and the session reading the output concluded the campus IP
    had been banned, which was wrong.
    """

    def test_the_refusal_is_decided_on_the_first_read(self) -> None:
        """This failing means the run waits out the render budget again."""

        observation = ScienceDirectAdapter.observe_search_page(
            block_page(), source_url="https://www.sciencedirect.com/search?qs=x"
        )
        self.assertIs(observation.page_type, SearchPageType.PUBLISHER_BLOCKED)
        self.assertTrue(observation.decided)

    def test_it_is_neither_an_empty_search_nor_a_layout_change(self) -> None:
        observation = ScienceDirectAdapter.observe_search_page(
            block_page(), source_url="https://www.sciencedirect.com/search?qs=x"
        )
        self.assertIsNot(observation.page_type, SearchPageType.GENUINE_ZERO_RESULTS)
        self.assertIsNot(observation.page_type, SearchPageType.PENDING)

    def test_search_stops_and_quotes_the_publisher_reference(self) -> None:
        port = ScriptedPort(by_query({'"x"': [block_page()]}))
        adapter = ScienceDirectAdapter(port)
        with self.assertRaises(SourceActionRequired) as caught:
            asyncio.run(adapter.search('"x"', request("x")))
        message = str(caught.exception)
        self.assertIn("a3d722eaede511e1", message)
        self.assertIn("CPE00001", message)
        self.assertNotIn("SEARCH_READINESS_TIMEOUT", message)

    def test_the_wording_alone_is_not_a_refusal(self) -> None:
        """A search page echoes the query back; an abstract can say anything.

        ee971e0 already had to reorder these checks once, because a query
        containing "one moment" would otherwise have become a gate.  Both the
        wording and a support reference number are required.
        """

        no_reference = (
            '<!doctype html><html><head><title>ScienceDirect</title></head><body>'
            "<p>There was a problem providing the content you requested</p>"
            "</body></html>"
        )
        observation = ScienceDirectAdapter.observe_search_page(
            no_reference, source_url="https://www.sciencedirect.com/search?qs=x"
        )
        self.assertIsNot(observation.page_type, SearchPageType.PUBLISHER_BLOCKED)

    def test_a_reference_number_alone_is_not_a_refusal(self) -> None:
        only_reference = search_shell("<p>Reference number: a3d722eaede511e1</p>")
        observation = ScienceDirectAdapter.observe_search_page(
            only_reference, source_url="https://www.sciencedirect.com/search?qs=x"
        )
        self.assertIsNot(observation.page_type, SearchPageType.PUBLISHER_BLOCKED)

    def test_a_real_results_page_quoting_the_wording_is_still_results(self) -> None:
        """A page with results on it is never a refusal, whatever it quotes."""

        page = results_page(card(*A1)).replace(
            "<ol", "<p>There was a problem providing the content you requested. "
            "Reference number: a3d722eaede511e1</p><ol"
        )
        observation = ScienceDirectAdapter.observe_search_page(
            page, source_url="https://www.sciencedirect.com/search?qs=x"
        )
        self.assertIs(observation.page_type, SearchPageType.RESULTS_PRESENT)

    def test_an_article_page_refusal_does_not_become_a_failed_identity_lock(self) -> None:
        self.assertIs(observe_article(block_page()), ArticlePageState.PUBLISHER_BLOCKED)

    def test_the_run_stops_instead_of_sending_the_next_query(self) -> None:
        """This failing means the loop queries a publisher that already refused."""

        keywords = ("AI washing", "earnings management", "discretionary accruals")
        first = '"AI washing" AND "earnings management"'
        port, result, query_log = run_workflow(
            by_query({first: [block_page()]}), *keywords, max_results=5
        )
        searched = [
            url
            for url in port.navigations
            if "/search" in url
        ]
        self.assertEqual(len(searched), 1)
        self.assertEqual(result.status, RunStatus.ACTION_REQUIRED_USER_LOGIN)

    def test_the_refusal_is_not_treated_as_a_page_that_clears_itself(self) -> None:
        """The browser layer must not wait its whole budget on this page.

        _INTERSTITIAL_*_MARKERS mean "a gate that clears by itself"; a refusal
        with a support reference never does.
        """

        from hunnu_harness.browser import playwright_backend

        markers = " ".join(
            (
                *playwright_backend._INTERSTITIAL_TITLE_MARKERS,
                *playwright_backend._INTERSTITIAL_BODY_MARKERS,
            )
        ).casefold()
        self.assertNotIn("there was a problem providing", markers)
        self.assertNotIn("reference number", markers)


# -- Case 7: the search throttle is asked before each search goes out -----------


class _RecordingPacer:
    """Notes how many searches had already gone out each time it was asked to wait."""

    port: ScriptedPort | None = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def pace(self, *, source: str, query: str) -> float:
        assert self.port is not None
        self.calls.append((query, len(self.port.search_navigations)))
        return 0.0


class SearchIsPacedBeforeItGoesOutTests(_FastWindows):
    """The pace ledger only protects the session if the workflow asks it first.

    Pacing after the request would record the search but not delay it, and the
    block page it exists to prevent is served to the request itself.
    """

    def test_every_query_waits_before_its_search_is_sent(self) -> None:
        pacer = _RecordingPacer()
        gate = RunStopsAtTheGateTests
        port, result, _query_log = run_workflow(
            by_query({gate.Q1: [NO_RESULTS], gate.Q2: [NO_RESULTS], gate.Q3: [NO_RESULTS]}),
            *gate.KEYWORDS,
            search_pacer=pacer,
        )
        self.assertEqual(len(port.search_navigations), 3)
        self.assertEqual(pacer.calls, [(gate.Q1, 0), (gate.Q2, 1), (gate.Q3, 2)])

    def test_an_isolated_run_neither_waits_nor_shares_the_real_ledger(self) -> None:
        """This failing means every workflow test sleeps 20s per query."""

        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sdresil-", dir=TEMP_DIR) as raw:
            tmp = Path(raw)
            workflow = LiteratureAcquisitionWorkflow(
                ScienceDirectAdapter(ScriptedPort(by_query({}), downloads_dir=tmp)),
                run_root=tmp / "run",
                human_like_delay_seconds=0.0,
                allow_outside_project_for_tests=True,
            )
            pacer = workflow.search_pacer
            self.assertEqual(pacer.min_interval_seconds, 0.0)
            self.assertTrue(pacer.path.resolve().is_relative_to(tmp.resolve()))


if __name__ == "__main__":
    unittest.main()
