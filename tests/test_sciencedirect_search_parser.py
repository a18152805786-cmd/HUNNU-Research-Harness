"""Reading a ScienceDirect search page, and knowing when you cannot.

A live run reported ``NO_RESULTS`` for a query whose page carried the target
paper.  The cause was timing, not selectors: ``page.goto`` returns at
``domcontentloaded``, and ScienceDirect renders its result list afterwards, so
the adapter read a page that had no results, no no-results notice, and nothing
to say which it was -- then reported the paper was not there.

Two things are pinned here.  The parser now only reads the results list, so a
recommendation can never arrive as a search result; and a page that has results
the parser cannot read is an error rather than an empty answer, because "not on
ScienceDirect" and "the Harness can no longer read ScienceDirect" are opposite
conclusions for whoever is asking.

The markup mirrors the live page as observed: ``ol.search-result-wrapper``
holding ``li.ResultItem`` cards, each with a title anchor and a second anchor to
the same PII for the PDF, and a ``li.LoginMessageResultItem`` notice sitting
among the cards.
"""

from __future__ import annotations

import unittest

from hunnu_harness.literature.adapters.base import SourceActionRequired
from hunnu_harness.literature.adapters.sciencedirect import (
    ScienceDirectAdapter,
    SearchPageType,
)

SEARCH_URL = "https://www.sciencedirect.com/search?qs=test"


def card(pii: str, title: str, *, with_pdf_link: bool = True) -> str:
    pdf = (
        f'<a class="anchor" href="/science/article/pii/{pii}/pdfft?pid=1-s2.0-{pii}-main.pdf">'
        "<span>View PDF</span></a>"
        if with_pdf_link
        else ""
    )
    return (
        f'<li class="ResultItem col-xs-24 push-m" data-doi="10.1016/j.test.{pii}">'
        '<div class="result-item-container"><div class="result-item-content">'
        f'<h2><span><a id="title-{pii}" class="anchor result-list-title-link" '
        f'href="/science/article/pii/{pii}">{title}</a></span></h2>'
        f"{pdf}"
        "</div></div></li>"
    )


def page(body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head>"
        "<title>Search | ScienceDirect.com</title></head><body>"
        f"{body}</body></html>"
    )


def results_page(*cards: str, extra: str = "") -> str:
    return page(
        '<ol class="search-result-wrapper" id="srp-results-list">'
        + "".join(cards)
        + "</ol>"
        + extra
    )


ONE = card("S1544612326002151", "AI washing: Strategic disclosure and backlash")
TWO = card("S0165410109000469", "How does financial reporting quality relate to investment")
LOGIN_NOTICE = (
    '<li class="LoginMessageResultItem col-sm-24">'
    "<span>Sign in to see more results</span></li>"
)
RECOMMENDATION_OUTSIDE = (
    '<aside><h3>Recommended articles</h3>'
    '<a href="/science/article/pii/S9999999999999999">A recommended paper</a></aside>'
)


def parse(html: str):
    return ScienceDirectAdapter.parse_search_results_html(
        html, query="test", source_url=SEARCH_URL, max_results=30
    )


def observe(html: str):
    return ScienceDirectAdapter.observe_search_page(html, source_url=SEARCH_URL)


class ResultExtractionTests(unittest.TestCase):
    def test_the_current_page_shape_yields_a_result(self) -> None:
        records = parse(results_page(ONE))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].stable_identifier, "S1544612326002151")
        self.assertEqual(
            records[0].title, "AI washing: Strategic disclosure and backlash"
        )
        self.assertEqual(
            records[0].source_page,
            "https://www.sciencedirect.com/science/article/pii/S1544612326002151",
        )

    def test_two_cards_give_two_candidates(self) -> None:
        records = parse(results_page(ONE, TWO))
        self.assertEqual(
            [r.stable_identifier for r in records],
            ["S1544612326002151", "S0165410109000469"],
        )

    def test_the_pdf_anchor_does_not_become_a_second_candidate(self) -> None:
        """A card carries the title link and a PDF link to the same paper."""

        html = results_page(ONE)
        self.assertGreater(html.count("S1544612326002151"), 1)
        records = parse(html)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].stable_identifier, "S1544612326002151")

    def test_a_recommendation_outside_the_results_list_is_ignored(self) -> None:
        records = parse(results_page(ONE, extra=RECOMMENDATION_OUTSIDE))
        self.assertEqual([r.stable_identifier for r in records], ["S1544612326002151"])

    def test_every_article_anchor_outside_the_list_is_ignored(self) -> None:
        """With no results list at all, nothing on the page is a result."""

        self.assertEqual(parse(page(RECOMMENDATION_OUTSIDE)), [])

    def test_a_sign_in_notice_among_the_cards_is_not_a_paper(self) -> None:
        """Its class contains ResultItem; only a token match keeps it out."""

        records = parse(results_page(ONE, LOGIN_NOTICE))
        self.assertEqual([r.stable_identifier for r in records], ["S1544612326002151"])

    def test_the_abs_url_shape_is_the_same_paper(self) -> None:
        html = results_page(
            '<li class="ResultItem"><a class="result-list-title-link" '
            'href="/science/article/abs/pii/S1544612326007385">Some paper</a></li>'
        )
        records = parse(html)
        self.assertEqual(records[0].stable_identifier, "S1544612326007385")

    def test_a_result_url_on_another_host_is_refused(self) -> None:
        """The article path alone does not make a link a ScienceDirect paper."""

        html = results_page(
            '<li class="ResultItem"><a class="result-list-title-link" '
            'href="https://example.invalid/science/article/pii/S1544612326002151">'
            "Looks right</a></li>"
        )
        self.assertEqual(parse(html), [])

    def test_a_malformed_article_path_yields_nothing(self) -> None:
        for href in (
            "/science/article/pii/",
            "/science/article/pii/!!!!",
            "/science/journal/15446123",
            "/topics/economics/ai-washing",
        ):
            with self.subTest(href=href):
                html = results_page(
                    f'<li class="ResultItem"><a class="result-list-title-link" '
                    f'href="{href}">Title</a></li>'
                )
                self.assertEqual(parse(html), [])

    def test_the_result_limit_is_honoured(self) -> None:
        cards = [card(f"S000000000000000{i}", f"Paper {i}") for i in range(5)]
        records = ScienceDirectAdapter.parse_search_results_html(
            results_page(*cards), query="test", source_url=SEARCH_URL, max_results=2
        )
        self.assertEqual(len(records), 2)


class SearchPageVerdictTests(unittest.TestCase):
    """Empty, still rendering, and unreadable are three different answers."""

    def test_a_page_with_results_says_so(self) -> None:
        observation = observe(results_page(ONE, TWO))
        self.assertIs(observation.page_type, SearchPageType.RESULTS_PRESENT)
        self.assertEqual(observation.result_cards, 2)
        self.assertEqual(observation.article_links, 4)
        self.assertTrue(observation.decided)

    def test_a_page_still_rendering_is_pending_not_empty(self) -> None:
        """This is the exact page the adapter used to call NO_RESULTS."""

        observation = observe(
            page('<div id="srp-results-list-container"></div><footer>Elsevier</footer>')
        )
        self.assertIs(observation.page_type, SearchPageType.PENDING)
        self.assertFalse(observation.decided)
        self.assertEqual(observation.article_links, 0)

    def test_a_genuinely_empty_search_says_so(self) -> None:
        observation = observe(page("<main><h2>No results found</h2></main>"))
        self.assertIs(observation.page_type, SearchPageType.GENUINE_ZERO_RESULTS)
        self.assertTrue(observation.decided)

    def test_a_challenge_page_is_not_an_empty_search(self) -> None:
        from pathlib import Path

        challenge = (
            Path(__file__).parent
            / "fixtures"
            / "literature"
            / "sciencedirect_captcha.html"
        ).read_text(encoding="utf-8")
        observation = observe(challenge)
        self.assertIs(observation.page_type, SearchPageType.NON_SEARCH_PAGE)
        self.assertNotEqual(observation.page_type, SearchPageType.GENUINE_ZERO_RESULTS)

    def test_a_login_page_is_not_an_empty_search(self) -> None:
        from pathlib import Path

        login = (
            Path(__file__).parent
            / "fixtures"
            / "literature"
            / "sciencedirect_login.html"
        ).read_text(encoding="utf-8")
        self.assertIs(observe(login).page_type, SearchPageType.NON_SEARCH_PAGE)

    def test_a_bot_check_interstitial_is_not_an_empty_search(self) -> None:
        """Still on the gate the browser layer waits out, not a finished search."""

        observation = observe(
            page("<h1>Just a moment...</h1><p>Checking your browser</p>")
        )
        self.assertIs(observation.page_type, SearchPageType.NON_SEARCH_PAGE)

    def test_observing_never_raises_where_parsing_would(self) -> None:
        """Polling must not turn a mid-flight page into a run-ending error.

        The real challenge fixture is used rather than a hand-written page:
        detection keys off the markers the publisher actually emits, and a
        stand-in that merely says "Are you a robot" proves nothing about it.
        """

        from pathlib import Path

        challenge = (
            Path(__file__).parent
            / "fixtures"
            / "literature"
            / "sciencedirect_captcha.html"
        ).read_text(encoding="utf-8")

        self.assertIs(observe(challenge).page_type, SearchPageType.NON_SEARCH_PAGE)
        with self.assertRaises(SourceActionRequired):
            ScienceDirectAdapter.parse_search_results_html(
                challenge, query="test", source_url=SEARCH_URL
            )


class ArchivedFixtureTests(unittest.TestCase):
    """The shape captured before this change must still read."""

    def test_the_stored_search_fixture_still_parses(self) -> None:
        from pathlib import Path

        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "literature"
            / "sciencedirect_search.html"
        ).read_text(encoding="utf-8")
        records = parse(fixture)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [r.stable_identifier for r in records],
            ["S1544612326004149", "S1544612326007385"],
        )
        self.assertIs(observe(fixture).page_type, SearchPageType.RESULTS_PRESENT)


if __name__ == "__main__":
    unittest.main()
