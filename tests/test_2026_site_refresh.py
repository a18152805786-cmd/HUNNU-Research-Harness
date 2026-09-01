from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from urllib.parse import quote_plus
from unittest.mock import patch

from hunnu_harness.browser.commands import (
    BrowserActionResult,
    BrowserObservation,
    ClickCommand,
    NavigateCommand,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from hunnu_harness.literature.adapters.base import SourceLayoutChanged
from hunnu_harness.literature.adapters.cnki import CNKIAdapter
from hunnu_harness.literature.adapters.oxfordacademic import OxfordAcademicAdapter
from hunnu_harness.literature.cnki_challenge import ChallengeState, classify_static_challenge
from hunnu_harness.literature.institutional import (
    HUNNU_OXFORD_ROUTE_URL_ENV,
    HUNNUInstitutionalAccessResolver,
    InstitutionalResolutionTrigger,
    InstitutionalRouteResult,
)
from hunnu_harness.literature.models import (
    AccessType,
    FullTextFormat,
    LiteratureRecord,
    LiteratureSearchRequest,
)


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
CNKI_SEARCH_URL = "https://kns.cnki.net/kns8s/defaultresult/index"
CNKI_ARTICLE_URL = (
    "https://kns.cnki.net/kcms2/article/abstract?"
    "dbcode=CJFQ&filename=FIXTURE2021001&language=CHS&uniplatform=NZKPT"
)
HUNNU_PORTAL = "https://www.hunnu.edu.cn/"
HUNNU_LIBRARY = "https://lib.hunnu.edu.cn/"
CHAOXING_DATABASE = "https://wisdom.chaoxing.com/newwisdom/database"
OXFORD_DETAIL = (
    "https://wisdom.chaoxing.com/newwisdom/doordatabase/"
    "databasedetail.html?databaseId=fixture"
)
OXFORD_GATEWAY = (
    "https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE-OXFORD-ROUTE/"
    "?opaque=REDACTED_TEST_VALUE"
)
OXFORD_DIRECT_ROUTE = "https://academic.oup.com/journals"
OXFORD_SEARCH_QUERY = "Double/debiased machine learning for treatment and structural parameters"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class CNKI2026FixtureTests(unittest.TestCase):
    def test_spa_result_table_exposes_the_target_article_row(self) -> None:
        records = CNKIAdapter.parse_search_results_html(
            fixture("cnki_search_2026.html"),
            query="数字化转型与企业分工",
            source_url=CNKI_SEARCH_URL,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0].title,
            "数字化转型与企业分工:专业化还是纵向一体化",
        )
        self.assertEqual(records[0].stable_identifier, "cjfq:FIXTURE2026001")
        self.assertIn("/kcms2/article/abstract", records[0].navigation_url)

    def test_2026_article_semantics_supply_identity_lock_metadata(self) -> None:
        detail = CNKIAdapter.parse_article_html(
            fixture("cnki_abstract_2026.html"),
            source_url=CNKI_ARTICLE_URL,
        )
        self.assertEqual(
            detail.title,
            "企业数字化转型与资本市场表现——来自股票流动性的经验证据",
        )
        self.assertEqual(detail.authors, ("作者甲 1", "作者乙 2"))
        self.assertEqual(detail.journal, "示例期刊")
        self.assertEqual(detail.year, "2021")
        self.assertEqual(detail.doi, "10.1234/cnki.fixture.2021.001")
        self.assertEqual(detail.stable_identifier, "cjfq:FIXTURE2021001")

        expected = LiteratureRecord(
            paper_id="expected",
            title=detail.title,
            authors=("作者甲", "作者乙"),
            year="2021",
            journal="示例期刊",
            stable_identifier="cjfq:FIXTURE2021001",
        )
        self.assertTrue(CNKIAdapter.identity_matches(expected, detail)[0])

    def test_2026_semantics_outrank_stale_legacy_meta_then_old_fixtures_remain_fallbacks(self) -> None:
        html = fixture("cnki_abstract_2026.html").replace(
            "<title>",
            '<meta name="citation_title" content="过期题名"><title>',
            1,
        )
        detail = CNKIAdapter.parse_article_html(html, source_url=CNKI_ARTICLE_URL)
        self.assertNotEqual(detail.title, "过期题名")
        legacy = CNKIAdapter.parse_article_html(
            fixture("cnki_article_pdf.html"),
            source_url=(
                "https://kns.cnki.net/kcms2/article/abstract?"
                "dbcode=CJFD&filename=KJYJ202601001"
            ),
        )
        self.assertEqual(legacy.title, "人工智能漂洗、审计监督与盈余管理")

    def test_2026_download_controls_are_authorized_and_pdf_precedes_caj(self) -> None:
        decision = CNKIAdapter.check_fulltext_access_html(
            fixture("cnki_abstract_2026.html"),
            source_url=CNKI_ARTICLE_URL,
        )
        self.assertTrue(decision.full_text_accessible)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)
        self.assertEqual(decision.full_text_format, FullTextFormat.PDF)
        self.assertEqual(decision.download_locator, "PDF下载")
        self.assertEqual(decision.download_url, "https://bar.cnki.net/bar/download/order?id=fixture-pdf")

    def test_captured_parked_challenge_shape_remains_dormant(self) -> None:
        evidence = CNKIAdapter.static_challenge_evidence(
            fixture("cnki_abstract_2026.html"),
            url=CNKI_ARTICLE_URL,
        )
        self.assertTrue(evidence.text_present)
        self.assertFalse(evidence.on_screen_text_present)
        self.assertEqual(classify_static_challenge(evidence), ChallengeState.DORMANT)

    def test_new_fixtures_contain_no_long_session_parameter_values(self) -> None:
        pattern = re.compile(r"(?:[?&](?:v|sign|id)=)[A-Za-z0-9._~%+/=-]{16,}", re.IGNORECASE)
        for name in (
            "cnki_search_2026.html",
            "cnki_abstract_2026.html",
            "hunnu_library_2026.html",
            "hunnu_chaoxing_database_shell_2026.html",
        ):
            self.assertIsNone(pattern.search(fixture(name)), name)


class _CNKI2026Browser:
    navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

    def __init__(self, *, detail_navigation_sticks: bool = False) -> None:
        self.search_html = fixture("cnki_search_2026.html")
        self.article_html = fixture("cnki_abstract_2026.html")
        self.current_url = "about:blank"
        self.current_html = "<html><title>blank</title></html>"
        self.commands = []
        self.detail_navigation_sticks = detail_navigation_sticks
        self.session = SessionHandle("cnki-2026-refresh")
        self.page_handle = PageHandle("main", session=self.session)

    def observation(self) -> BrowserObservation:
        return BrowserObservation(
            session=self.session,
            page=self.page_handle,
            generation=len(self.commands),
            url=self.current_url,
            title="中国知网",
            html=self.current_html,
        )

    async def execute(self, command):
        self.commands.append(command)
        if isinstance(command, NavigateCommand):
            if "/kns8s/" in command.url:
                self.current_url = command.url
                self.current_html = self.search_html
            elif "/kcms2/article/abstract" in command.url and not self.detail_navigation_sticks:
                self.current_url = command.url
                self.current_html = self.article_html
            return self.observation()
        if isinstance(command, ObserveCommand):
            return self.observation()
        if isinstance(command, ClickCommand):
            return BrowserActionResult(
                session=self.session,
                page=self.page_handle,
                generation=len(self.commands),
                action="click",
                url=self.current_url,
            )
        raise AssertionError(type(command).__name__)


class CNKI2026OpenResultTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def target_record() -> LiteratureRecord:
        return CNKIAdapter.parse_search_results_html(
            fixture("cnki_search_2026.html"),
            query="数字化转型与企业分工",
            source_url=CNKI_SEARCH_URL,
        )[0]

    async def test_new_tab_layout_navigates_fresh_href_on_same_page_without_click(self) -> None:
        browser = _CNKI2026Browser()
        await CNKIAdapter(browser).open_result(self.target_record())
        navigations = [
            command.url for command in browser.commands if isinstance(command, NavigateCommand)
        ]
        self.assertEqual(len(navigations), 2)
        self.assertIn("/kns8s/", navigations[0])
        self.assertIn("/kcms2/article/abstract", navigations[1])
        self.assertFalse(any(isinstance(command, ClickCommand) for command in browser.commands))
        self.assertIn("/kcms2/article/abstract", browser.current_url)

    async def test_same_page_navigation_that_remains_on_result_page_fails_closed(self) -> None:
        browser = _CNKI2026Browser(detail_navigation_sticks=True)
        with self.assertRaisesRegex(SourceLayoutChanged, "after same-page navigation"):
            await CNKIAdapter(browser).open_result(self.target_record())
        self.assertFalse(any(isinstance(command, ClickCommand) for command in browser.commands))
        self.assertIn("/kns8s/", browser.current_url)


class _OxfordSearchBrowser:
    def __init__(self) -> None:
        self.current_url = "about:blank"
        self.current_html = "<html><title>blank</title></html>"
        self.commands = []
        self.session = SessionHandle("oxford-2026-search")
        self.page_handle = PageHandle("main", session=self.session)

    def observation(self) -> BrowserObservation:
        return BrowserObservation(
            session=self.session,
            page=self.page_handle,
            generation=len(self.commands),
            url=self.current_url,
            title="Search Results | Oxford Academic",
            html=self.current_html,
        )

    async def execute(self, command):
        self.commands.append(command)
        if isinstance(command, NavigateCommand):
            self.current_url = command.url
            self.current_html = fixture("oxfordacademic_search.html")
            return self.observation()
        if isinstance(command, ObserveCommand):
            return self.observation()
        raise AssertionError(type(command).__name__)


def oxford_route(publisher_navigation_url: str) -> InstitutionalRouteResult:
    return InstitutionalRouteResult(
        requested_source="OxfordAcademic",
        resolution_trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
        institutional_route_resolved=True,
        institutional_target_database_match=True,
        publisher_navigation_url=publisher_navigation_url,
    )


class Oxford2026SearchRoutingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request() -> LiteratureSearchRequest:
        return LiteratureSearchRequest(
            original_research_request="Oxford 2026 route search regression",
            max_downloads=0,
            max_downloads_per_run=0,
        )

    async def test_resolved_official_route_uses_direct_oxford_search(self) -> None:
        browser = _OxfordSearchBrowser()
        adapter = OxfordAcademicAdapter(browser)
        adapter.bind_institutional_route(oxford_route(OXFORD_DIRECT_ROUTE))

        records = await adapter.search(OXFORD_SEARCH_QUERY, self.request())

        expected = (
            "https://academic.oup.com/search-results?q="
            f"{quote_plus(OXFORD_SEARCH_QUERY)}"
        )
        navigations = [
            command.url for command in browser.commands if isinstance(command, NavigateCommand)
        ]
        self.assertEqual(navigations, [expected])
        self.assertEqual(len(records), 1)

    async def test_resolved_yclib_route_retains_gateway_search_rewrite(self) -> None:
        browser = _OxfordSearchBrowser()
        adapter = OxfordAcademicAdapter(browser)
        adapter.bind_institutional_route(oxford_route(OXFORD_GATEWAY))

        records = await adapter.search(OXFORD_SEARCH_QUERY, self.request())

        expected = OXFORD_GATEWAY.replace("/?opaque=", "/search-results?opaque=", 1)
        expected += f"&q={quote_plus(OXFORD_SEARCH_QUERY)}"
        navigations = [
            command.url for command in browser.commands if isinstance(command, NavigateCommand)
        ]
        self.assertEqual(navigations, [expected])
        self.assertEqual(len(records), 1)


class _RoutePage:
    def __init__(self, browser: "_RouteBrowser") -> None:
        self.browser = browser

    @property
    def url(self) -> str:
        return self.browser.current_url

    async def content(self) -> str:
        return self.browser.current_html

    async def title(self) -> str:
        return HUNNUInstitutionalAccessResolver._parse(self.browser.current_html).title


class _RouteBrowser:
    def __init__(self, pages: dict[str, str | tuple[str, str]]) -> None:
        self.pages = pages
        self.current_url = "about:blank"
        self.current_html = "<html><title>blank</title></html>"
        self.history: list[str] = []
        self.page = _RoutePage(self)

    async def goto(self, url: str) -> None:
        self.history.append(url)
        if url not in self.pages:
            raise AssertionError(f"Unexpected fixture navigation: {url}")
        value = self.pages[url]
        if isinstance(value, tuple):
            self.current_html, self.current_url = value
        else:
            self.current_html, self.current_url = value, url


def base_route_pages() -> dict[str, str | tuple[str, str]]:
    return {
        HUNNU_PORTAL: fixture("hunnu_portal.html"),
        HUNNU_LIBRARY: fixture("hunnu_library_2026.html"),
    }


class HUNNU2026OxfordRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_official_outer_proxy_is_prioritized_and_javascript_anchor_is_rejected(self) -> None:
        entries = HUNNUInstitutionalAccessResolver.discover_resource_entries(
            fixture("hunnu_library_2026.html"),
            base_url=HUNNU_LIBRARY,
        )
        self.assertEqual(len(entries), 1)
        self.assertTrue(
            HUNNUInstitutionalAccessResolver.is_hunnu_library_outer_proxy_url(
                entries[0].navigation_url
            )
        )
        self.assertEqual(entries[0].domain, "lib.hunnu.edu.cn")
        self.assertEqual(
            entries[0].match_basis,
            "HUNNU 2026 official database-navigation proxy",
        )

    async def test_dynamic_chaoxing_shell_returns_actionable_graded_failure(self) -> None:
        pages = base_route_pages()
        entry = HUNNUInstitutionalAccessResolver.discover_resource_entries(
            pages[HUNNU_LIBRARY],
            base_url=HUNNU_LIBRARY,
        )[0]
        pages[entry.navigation_url] = (
            fixture("hunnu_chaoxing_database_shell_2026.html"),
            CHAOXING_DATABASE,
        )
        browser = _RouteBrowser(pages)
        with patch.dict(os.environ, {HUNNU_OXFORD_ROUTE_URL_ENV: ""}, clear=False):
            route = await HUNNUInstitutionalAccessResolver(browser).resolve(
                "OxfordAcademic",
                trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
            )
        self.assertFalse(route.institutional_route_resolved)
        self.assertEqual(len(route.route_steps), 3)
        self.assertEqual(route.route_steps[-1].official_domain, "wisdom.chaoxing.com")
        self.assertIn("JavaScript-rendered resource list", route.reason)
        self.assertIn(HUNNU_OXFORD_ROUTE_URL_ENV, route.reason)

    async def test_valid_configured_entry_still_runs_detail_and_publisher_identity_locks(self) -> None:
        pages = base_route_pages()
        pages[OXFORD_DETAIL] = fixture("hunnu_oxford_detail_gateway.html")
        pages[OXFORD_GATEWAY] = fixture("oxford_gateway_home.html")
        browser = _RouteBrowser(pages)
        with patch.dict(os.environ, {HUNNU_OXFORD_ROUTE_URL_ENV: OXFORD_DETAIL}, clear=False):
            route = await HUNNUInstitutionalAccessResolver(browser).resolve(
                "OxfordAcademic",
                trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
            )
        self.assertTrue(route.institutional_route_resolved)
        self.assertTrue(route.institutional_target_database_match)
        self.assertIn(OXFORD_DETAIL, browser.history)
        self.assertEqual(route.route_steps[2].official_domain, "wisdom.chaoxing.com")
        self.assertNotIn("databaseId=fixture", str(route.as_dict()))

    async def test_configured_trusted_url_cannot_bypass_the_oxford_identity_lock(self) -> None:
        pages = base_route_pages()
        pages[OXFORD_DETAIL] = "<html><title>无关数据库</title><body>无关资源</body></html>"
        browser = _RouteBrowser(pages)
        with patch.dict(os.environ, {HUNNU_OXFORD_ROUTE_URL_ENV: OXFORD_DETAIL}, clear=False):
            route = await HUNNUInstitutionalAccessResolver(browser).resolve(
                "OxfordAcademic",
                trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
            )
        self.assertFalse(route.institutional_route_resolved)
        self.assertIn("did not identify the requested source", route.reason)
        self.assertNotIn(OXFORD_GATEWAY, browser.history)

    async def test_external_configured_url_is_rejected_before_browser_navigation(self) -> None:
        browser = _RouteBrowser({})
        with patch.dict(
            os.environ,
            {HUNNU_OXFORD_ROUTE_URL_ENV: "https://example.invalid/oxford"},
            clear=False,
        ):
            route = await HUNNUInstitutionalAccessResolver(browser).resolve(
                "OxfordAcademic",
                trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
            )
        self.assertFalse(route.institutional_route_resolved)
        self.assertEqual(browser.history, [])
        self.assertIn("rejected before navigation", route.reason)


if __name__ == "__main__":
    unittest.main()
