"""Restricted CNKI search: journal articles only, from named journals, in given years.

Every page here is offline.  ``cnki_journal_source_search_2026.html`` and the
builders below follow the kns8s result pages saved in the Output Root
(2026-08): the resource tabs, the result table's ``td.name``/``td.source``/
``td.date``/``td.data`` cells, and the hidden ``briefRequest`` in which CNKI
states the search it executed.  Titles, authors and tokens are synthetic.
A live CNKI run is acceptance, never a diagnostic (AGENTS.md Rule 71).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import html as html_lib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, quote_plus, urlsplit

from hunnu_harness.agent_entrypoint import AgentRequestRouter
from hunnu_harness.browser.commands import (
    BrowserObservation,
    BrowserTargetObservation,
    ClickCommand,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from hunnu_harness.cli_output import CliReport
from hunnu_harness.diagnostics import build_capabilities
from hunnu_harness.exit_codes import EXIT_RUN_FAILED
from hunnu_harness.literature.adapters.base import (
    LiteratureSourceAdapter,
    SourceActionRequired,
    SourceLayoutChanged,
)
from hunnu_harness.literature.adapters.cnki import (
    CNKIAdapter,
    CNKIRestrictedQueryRefused,
    CNKIRestrictionUnconfirmed,
)
from hunnu_harness.literature.cli import _run_live_into, build_parser
from hunnu_harness.literature.models import (
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
    UNKNOWN,
)
from hunnu_harness.literature.planning import LiteratureSearchPlanner
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow
from hunnu_harness.paths import TEMP_DIR


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
SOURCE_PAGE = "cnki_journal_source_search_2026.html"
ORIGINAL = "2019-2026年《示例学刊》《样本评论》中的示例议题研究（写法样本）"
SETTLE = "hunnu_harness.literature.adapters.cnki._SEARCH_SETTLE_DELAY_SECONDS"
ACCESS_SETTLE = "hunnu_harness.literature.adapters.cnki._ACCESS_SETTLE_DELAY_SECONDS"
CLICK_SETTLE = "hunnu_harness.literature.adapters.cnki._CNKI_CLICK_RETRY_DELAY_SECONDS"

# The articles of the saved-structure fixture page, by their synthetic token.
# The online-first article's own page states neither journal nor year, as the
# CAPJ pages saved on 2026-08-20 do not; the other two state both.
FIXTURE_ARTICLES = {
    "synthetic-slxk20260910001": ("示例技术的规范问题：一个分析框架", None, None),
    "synthetic-slxk202604003": ("示例系统中的规则设计与责任分配", "示例学刊", "2026"),
    "synthetic-slxk202506007": ("示例议题治理的比较进路", "示例学刊", "2025"),
    "synthetic-slyx202608012": ("示例应用的审查研究", "示例医学杂志", "2026"),
    "synthetic-slxk201803002": ("示例传统与现代社会", "示例学刊", "2018"),
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def token_for(title: str) -> str:
    return "synthetic-" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]


def detail_url(token: str) -> str:
    return f"https://kns.cnki.net/kcms2/article/abstract?v={token}&uniplatform=NZKPT&language=CHS"


def brief_request(
    field: str,
    value: str,
    *,
    cross_ids: str = "YSTT4HG0",
    products: str = "CJFQ,CAPJ,ZHYX,CJTL",
    classid: str = "WD0FTY92",
    resource: str = "CROSSDB",
    extra_conditions: tuple[tuple[str, str], ...] = (),
) -> str:
    """The hidden ``briefRequest`` value, shaped like the saved pages' own."""

    items = [
        {"logic": "AND", "operator": "FUZZY", "field": name, "value": text, "value2": None, "title": ""}
        for name, text in ((field, value), *extra_conditions)
    ]
    inner = {
        "resource": resource,
        "classid": classid,
        "kuaKuCode": cross_ids,
        "qnode": {"qgroup": [{"key": "Subject", "logic": "AND", "items": items, "childItems": []}]},
        "Products": products,
    }
    outer = {
        "queryJson": json.dumps(inner, ensure_ascii=False),
        "classid": classid,
        "resource": resource,
        "kuakuCode": cross_ids,
        "sortField": "PT",
        "sortType": "DESC",
        "searchFrom": "资源范围：总库",
    }
    return html_lib.escape(json.dumps(outer, ensure_ascii=False), quote=True)


def row(
    title: str,
    source: str,
    date: str,
    *,
    label: str | None = "期刊",
    dbname: str | None = "CJFQ",
    resource: str | None = "JOURNAL",
    online_first: bool = False,
) -> str:
    mark = '<b class="marktip">网络首发</b>' if online_first else ""
    data_cell = f'<td class="data"><span>{label}</span></td>' if label is not None else ""
    attributes = "".join(
        f' {name}="{value}"'
        for name, value in (("data-dbname", dbname), ("data-resource", resource))
        if value is not None
    )
    return (
        f'<tr><td class="seq">1</td><td class="name"><a class="fz14 inline" target="_blank" '
        f'href="{html_lib.escape(detail_url(token_for(title)))}">{title}</a>{mark}</td>'
        f'<td class="author"><a class="KnowledgeNetLink">作者甲</a></td>'
        f'<td class="source"><p><a href="https://navi.cnki.net/knavi/detail?p=x">{source}</a></p></td>'
        f'<td class="date">{date}</td>{data_cell}'
        f'<td class="operat"><a class="icon-collect" title="收藏"{attributes}></a></td></tr>'
    )


def kns_page(
    rows: list[str],
    *,
    field: str = "LY",
    value: str = "示例学刊",
    with_brief: bool = True,
    **scope,
) -> str:
    brief = (
        f'<input id="briefRequest" type="hidden" value="{brief_request(field, value, **scope)}">'
        if with_brief
        else ""
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>检索-中国知网</title>'
        '</head><body><div id="countPageDiv" class="result-con-r"><span class="pagerTitleCell">'
        f"<span>共找到</span><em>{len(rows)}</em><span>条结果</span></span></div>"
        '<table class="result-table-list"><thead><tr><th></th><th>题名</th><th>作者</th><th>来源</th>'
        f'<th>发表时间</th><th>数据库</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
        f"{brief}</body></html>"
    )


def article_page(title: str, *, journal: str | None, year: str | None) -> str:
    """A 2026-layout article page; without a journal it states neither.

    Its navigation names 学位论文 and 报纸 as real pages do, which is what the
    article parser's body-text classification guesses from when it has no
    journal to go on.
    """

    top_tip = (
        f'<div class="doc-top"><div class="top-tip">{journal} . {year} ,42 (04) : 10-20</div></div>'
        if journal
        else ""
    )
    return (
        f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{title} - 中国知网</title>'
        "</head><body><nav><a>学术期刊</a><a>学位论文</a><a>报纸</a></nav>"
        '<div class="ecp_header_login_area"><div class="ecp_header_login_status ecp_header_login_status1">'
        '<div class="ecp_header_unitName" title="示例大学图书馆">示例大学图书馆</div></div></div>'
        f'{top_tip}<article class="wx-tit"><h1>{title}</h1>'
        '<h3 id="authorpart"><a href="/kcms2/author/detail">作者甲</a></h3>'
        '<div id="ChDivSummary">摘要：本文以合成样本说明文章页结构。</div>'
        '<p class="keywords">示例议题；规范问题；</p></article>'
        '<ul class="operate-btn"><li><a id="pdfDown" name="pdfDown" target="_blank" '
        'href="https://bar.cnki.net/bar/download/order?id=synthetic-pdf">PDF下载</a></li></ul>'
        "</body></html>"
    )


def relock_page(title: str, token: str) -> str:
    return (
        '<html><head><title>检索-中国知网</title></head><body><div>共找到 1 条结果</div>'
        '<table class="result-table-list"><tbody><tr><td class="name"><a class="fz14 inline" '
        f'target="_blank" href="{html_lib.escape(detail_url(token))}">{title}</a></td></tr>'
        "</tbody></table></body></html>"
    )


NO_HITS = (
    '<html><head><title>检索-中国知网</title></head><body><div>共找到 0 条结果</div></body></html>'
)


def rendered_challenge(marker: str = "拖动下方拼图完成验证") -> BrowserTargetObservation:
    """Runtime evidence of a verification component rendered in the viewport."""

    box = {"x": 420.0, "y": 260.0, "width": 320.0, "height": 180.0}
    return BrowserTargetObservation(
        marker=marker,
        frame_index=0,
        frame_url="https://kns.cnki.net/",
        playwright_visible=True,
        bounding_box=box,
        client_rect=box,
        display="block",
        visibility="visible",
        opacity="1",
        pointer_events="auto",
        client_width=320.0,
        client_height=180.0,
        viewport_width=1280.0,
        viewport_height=800.0,
        frame_viewport_visible=True,
        inspection_complete=True,
        blocking_overlay=False,
    )


def request(**overrides) -> LiteratureSearchRequest:
    values = {
        "original_research_request": ORIGINAL,
        "source_journals": ("示例学刊",),
        "year_start": 2019,
        "year_end": 2026,
        "max_search_results": 10,
        "max_results_per_source": 10,
        "max_downloads": 0,
        "max_downloads_per_run": 0,
    }
    values.update(overrides)
    return LiteratureSearchRequest(**values)


class _CNKISite:
    """A fake Research Chrome tab: kns8s searches by URL, articles by token.

    Journal-scoped searches (``crossids=``) are served from ``scoped`` by
    ``(korder, kw)``; an exact-title relock search answers with one row that
    links the known article of that title; an article URL serves its page.
    """

    navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

    def __init__(
        self,
        scoped: dict[tuple[str, str], list[str]],
        *,
        articles: dict[str, tuple[str, str | None, str | None]] | None = None,
        relock_hits: bool = True,
        snapshot_only: bool = False,
        probes: tuple[BrowserTargetObservation, ...] = (),
    ) -> None:
        self.scoped = {key: list(pages) for key, pages in scoped.items()}
        self.articles = dict(articles or {})
        self.relock_hits = relock_hits
        self.snapshot_only = snapshot_only
        self.probes = probes
        # Pages successive observations see, for a result list still rendering.
        self.progression: list[str] = []
        self.commands: list = []
        self.observations = 0
        self.current_url = "about:blank"
        self.current_html = "<html></html>"
        self.session = SessionHandle("cnki-restricted-search-test")
        self.page_handle = PageHandle("main", session=self.session)

    @property
    def navigations(self) -> list[str]:
        return [command.url for command in self.commands if isinstance(command, NavigateCommand)]

    @property
    def scoped_navigations(self) -> list[str]:
        return [url for url in self.navigations if "crossids=" in url]

    def _observation(self) -> BrowserObservation:
        return BrowserObservation(
            session=self.session,
            page=self.page_handle,
            generation=len(self.commands),
            url=self.current_url,
            title="检索-中国知网",
            html=self.current_html,
            target_observations=self.probes,
        )

    def _serve(self, url: str) -> str:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.path.startswith("/kns8s/"):
            if "crossids" in query:
                pages = self.scoped.get((query["korder"][0], query["kw"][0]))
                if not pages:
                    return NO_HITS
                return pages.pop(0) if len(pages) > 1 else pages[0]
            wanted = query.get("kw", [""])[0]
            for token, (title, _journal, _year) in self.articles.items():
                if self.relock_hits and CNKIAdapter.identity_matches(
                    LiteratureRecord(paper_id="wanted", title=wanted),
                    LiteratureRecord(paper_id="known", title=title),
                )[0]:
                    return relock_page(title, token)
            return NO_HITS
        if parts.path.startswith("/kcms2/article/abstract"):
            token = query.get("v", [""])[0]
            if token in self.articles:
                title, journal, year = self.articles[token]
                return article_page(title, journal=journal, year=year)
        return "<html><head><title>404</title></head><body>页面不存在</body></html>"

    async def execute(self, command):
        self.commands.append(command)
        if isinstance(command, NavigateCommand):
            self.current_url = command.url
            self.current_html = self._serve(command.url)
            return self._observation()
        if isinstance(command, ObserveCommand):
            self.observations += 1
            if self.progression:
                self.current_html = self.progression.pop(0)
            if self.snapshot_only:
                if command.include_html:
                    raise ObservationUnavailable("structured snapshot only")
                return BrowserObservation(
                    session=self.session,
                    page=self.page_handle,
                    generation=len(self.commands),
                    url=self.current_url,
                    title="检索-中国知网",
                    structured_content="- generic [ref=e1]: 共找到 3 条结果",
                )
            return self._observation()
        if isinstance(command, ClickCommand):
            return self._observation()
        raise AssertionError(type(command).__name__)


def source_site(**kwargs) -> _CNKISite:
    return _CNKISite({("LY", "示例学刊"): [fixture(SOURCE_PAGE)]}, articles=FIXTURE_ARTICLES, **kwargs)


# -- the request ----------------------------------------------------------------


class RestrictedRequestTests(unittest.TestCase):
    def test_a_request_can_ask_for_journal_articles_from_named_journals_in_given_years(self) -> None:
        parsed = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": ORIGINAL,
                "ResourceType": "journal_article",
                "SourceJournals": ["《示例学刊》", "样本评论", "示例学刊"],
                "YearStart": 2019,
                "YearEnd": "2026",
            }
        )
        self.assertTrue(parsed.restricted_search)
        self.assertEqual(parsed.resource_type, "JournalArticle")
        # Book-title marks and a repeated name are not two journals.
        self.assertEqual(parsed.source_journals, ("示例学刊", "样本评论"))
        self.assertEqual((parsed.year_start, parsed.year_end), (2019, 2026))

    def test_year_from_and_year_to_are_the_same_years_under_another_name(self) -> None:
        parsed = LiteratureSearchRequest.from_mapping(
            {"OriginalResearchRequest": ORIGINAL, "YearFrom": 2019, "YearTo": 2026, "YearStart": "2019"}
        )
        self.assertEqual((parsed.year_start, parsed.year_end), (2019, 2026))

    def test_two_spellings_of_a_year_that_disagree_fail_closed(self) -> None:
        # If this fails, one of two contradictory years was silently picked.
        with self.assertRaisesRegex(ValueError, "YearStart is given more than once"):
            LiteratureSearchRequest.from_mapping(
                {"OriginalResearchRequest": ORIGINAL, "YearStart": 2019, "YearFrom": 2020}
            )

    def test_an_unknown_resource_type_fails_closed_instead_of_searching_everything(self) -> None:
        for value in ("Dissertation", "报纸", "journals please"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "ResourceType"):
                LiteratureSearchRequest.from_mapping({"OriginalResearchRequest": ORIGINAL, "ResourceType": value})

    def test_naming_journals_restricts_to_journal_articles_without_saying_so(self) -> None:
        parsed = LiteratureSearchRequest.from_mapping(
            {"OriginalResearchRequest": ORIGINAL, "SourceJournals": "示例学刊，样本评论"}
        )
        self.assertEqual(parsed.resource_type, "JournalArticle")
        self.assertEqual(parsed.source_journals, ("示例学刊", "样本评论"))

    def test_an_english_journal_name_keeps_its_comma(self) -> None:
        parsed = LiteratureSearchRequest.from_mapping(
            {"OriginalResearchRequest": ORIGINAL, "SourceJournals": ["Journal of Law, Economics, and Organization"]}
        )
        self.assertEqual(parsed.source_journals, ("Journal of Law, Economics, and Organization",))

    def test_journal_names_that_cannot_be_searched_as_one_source_fail_closed(self) -> None:
        for journals, message in (
            (('示例"学刊',), "quote"),
            (("《》",), "empty"),
            (tuple(f"期刊{index}" for index in range(9)), "at most 8"),
            (("很长的刊名" * 20,), "longer than"),
        ):
            with self.subTest(journals=journals), self.assertRaisesRegex(ValueError, message):
                LiteratureSearchRequest(original_research_request=ORIGINAL, source_journals=journals)

    def test_a_restriction_cannot_ride_on_an_exact_title_doi_or_author_lookup(self) -> None:
        # If this fails, a restricted request can slip into the exact-title path,
        # which searches the whole library and was never meant to be restricted.
        for lookup in ({"exact_titles": ("某篇论文的精确题名",)}, {"dois": ("10.1000/x",)}, {"authors": ("作者甲",)}):
            with self.subTest(lookup=lookup), self.assertRaisesRegex(ValueError, "cannot be restricted"):
                LiteratureSearchRequest(original_research_request=ORIGINAL, resource_type="JournalArticle", **lookup)

    def test_an_unrestricted_request_stays_unrestricted_through_its_record(self) -> None:
        plain = LiteratureSearchRequest(original_research_request=ORIGINAL, exact_titles=("某篇论文的精确题名",))
        self.assertFalse(plain.restricted_search)
        self.assertEqual(plain.as_dict()["ResourceType"], UNKNOWN)
        again = LiteratureSearchRequest.from_mapping(plain.as_dict())
        self.assertFalse(again.restricted_search)
        restricted = request(source_journals=("示例学刊", "样本评论"))
        again = LiteratureSearchRequest.from_mapping(restricted.as_dict())
        self.assertEqual(again.source_journals, restricted.source_journals)
        self.assertEqual(again.resource_type, "JournalArticle")


# -- the plan -------------------------------------------------------------------


class RestrictedPlanningTests(unittest.TestCase):
    def test_each_named_journal_is_searched_once_by_source_and_topics_are_left_to_screening(self) -> None:
        plans = LiteratureSearchPlanner().plan(
            request(source_journals=("示例学刊", "样本评论"), keywords_cn=("示例议题", "示例概念"))
        )
        self.assertEqual([plan.query for plan in plans], ['source:"示例学刊"', 'source:"样本评论"'])
        self.assertEqual(plans[0].filters["SourceJournals"], ["示例学刊", "样本评论"])
        self.assertEqual(plans[0].filters["ResourceType"], "JournalArticle")
        self.assertIn("screened, not searched", plans[0].rationale)

    def test_a_journal_articles_only_request_keeps_its_topic_queries(self) -> None:
        restricted = LiteratureSearchPlanner().plan(
            request(source_journals=(), resource_type="JournalArticle", keywords_cn=("示例议题", "规范问题"))
        )
        self.assertEqual([plan.query for plan in restricted], ["示例议题 规范问题"])
        self.assertEqual(restricted[0].filters["ResourceType"], "JournalArticle")

    def test_unrestricted_plan_filters_are_unchanged(self) -> None:
        plans = LiteratureSearchPlanner().plan(
            LiteratureSearchRequest(original_research_request=ORIGINAL, keywords_cn=("示例议题",))
        )
        self.assertEqual(
            set(plans[0].filters),
            {"YearStart", "YearEnd", "PreferredLanguages", "PreferredPublicationTypes", "MaxResultsPerSource"},
        )


# -- the URL CNKI receives ------------------------------------------------------


class RestrictedSearchUrlTests(unittest.TestCase):
    def test_a_journal_source_search_scopes_academic_journals_and_searches_the_source_field(self) -> None:
        url = CNKIAdapter.build_search_url("示例学刊", mode="journal_source")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(urlsplit(url).path, "/kns8s/defaultresult/index")
        self.assertEqual(query["crossids"], ["YSTT4HG0"])
        self.assertEqual(query["korder"], ["LY"])
        self.assertEqual(query["kw"], ["示例学刊"])

    def test_spaces_reach_cnki_as_spaces_not_as_literal_plus_signs(self) -> None:
        # CNKI keeps "+" in kw literally: the saved pages executed "A+B" for a
        # title sent as quote_plus("A B").  If this fails, a restricted topic
        # search asks CNKI for a term with plus signs in it.
        url = CNKIAdapter.build_search_url("示例议题 规范问题", mode="journal_topic")
        self.assertIn("korder=SU", url)
        self.assertIn("%20", url)
        self.assertNotIn("+", url)

    def test_a_journal_name_with_cnki_operator_characters_is_searched_as_one_quoted_term(self) -> None:
        spec = CNKIAdapter._restricted_search_spec(
            'source:"北京大学学报(哲学社会科学版)"',
            request(source_journals=("北京大学学报(哲学社会科学版)", "示例学刊")),
        )
        self.assertEqual(spec.term, '"北京大学学报(哲学社会科学版)"')
        plain = CNKIAdapter._restricted_search_spec('source:"示例学刊"', request())
        self.assertEqual(plain.term, "示例学刊")

    def test_unrestricted_search_urls_are_unchanged(self) -> None:
        # If this fails, the exact-title acquisition path changed its request.
        origin = "https://kns.cnki.net/kns8s/defaultresult/index"
        self.assertEqual(
            CNKIAdapter.build_search_url("人工智能漂洗、审计监督与盈余管理", mode="exact_title"),
            f"{origin}?korder=TI&kw={quote_plus('人工智能漂洗、审计监督与盈余管理')}",
        )
        self.assertEqual(
            CNKIAdapter.build_search_url("盈余管理 审计", mode="keyword"),
            f"{origin}?korder=SU&kw={quote_plus('盈余管理 审计')}",
        )
        self.assertEqual(
            CNKIAdapter.build_search_url("张三", mode="author"),
            f"{origin}?korder=AU&kw={quote_plus('张三')}",
        )


# -- what the page says ---------------------------------------------------------


class RestrictedPageReadingTests(unittest.TestCase):
    def test_result_rows_carry_journal_year_database_and_online_first(self) -> None:
        page = CNKIAdapter.parse_restricted_search_html(fixture(SOURCE_PAGE))
        self.assertTrue(page.terminal)
        self.assertEqual(page.reported_total, 5)
        self.assertEqual(
            [(item.source, item.year, item.database_label) for item in page.rows],
            [
                ("示例学刊", "2026", "期刊"),
                ("示例学刊", "2026", "期刊"),
                ("示例学刊", "2025", "期刊"),
                ("示例医学杂志", "2026", "期刊"),
                ("示例学刊", "2018", "期刊"),
            ],
        )
        self.assertEqual([item.online_first for item in page.rows], [True, False, False, False, False])
        # The citation-count links in the 被引 cell point at the same articles
        # but are not results.
        self.assertNotIn("2", [item.title for item in page.rows])

    def test_the_page_states_the_scope_and_the_query_it_executed(self) -> None:
        scope = CNKIAdapter.parse_restricted_search_html(fixture(SOURCE_PAGE)).scope
        self.assertIsNotNone(scope)
        self.assertTrue(scope.journal_only)
        self.assertEqual(scope.conditions, (("LY", "示例学刊"),))
        self.assertEqual(scope.sort, "PT DESC")

    def test_a_whole_library_page_is_not_a_journal_only_page(self) -> None:
        default_cross_ids = "YSTT4HG0,LSTPFY1C,EMRPGLPA,JUP3MUPD,MPMFIG1A,WQ0UVIAA,BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R"
        page = CNKIAdapter.parse_restricted_search_html(
            kns_page([row("某文", "示例学刊", "2026-01-01")], cross_ids=default_cross_ids, products="CJFQ,CCND")
        )
        self.assertFalse(page.scope.journal_only)

    def test_a_challenge_page_stops_the_restricted_reading_for_a_person(self) -> None:
        with self.assertRaises(SourceActionRequired):
            CNKIAdapter.parse_restricted_search_html(fixture("cnki_security_challenge.html"))


# -- the adapter's restricted search --------------------------------------------


class RestrictedSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_confirmed_journal_source_search_returns_only_that_journal_within_the_years(self) -> None:
        site = source_site()
        adapter = CNKIAdapter(site)
        records = await adapter.search('source:"示例学刊"', request())
        self.assertEqual(
            [(record.title, record.journal, record.year) for record in records],
            [
                ("示例技术的规范问题:一个分析框架", "示例学刊", "2026"),
                ("示例系统中的规则设计与责任分配", "示例学刊", "2026"),
                ("示例议题治理的比较进路", "示例学刊", "2025"),
            ],
        )
        self.assertTrue(all(record.publication_type == "JournalArticle" for record in records))
        self.assertEqual(records[0].publication_status, PublicationStatus.ONLINE_FIRST.value)
        self.assertEqual(len(site.navigations), 1)
        report = adapter.search_restriction_report('source:"示例学刊"')
        self.assertTrue(report["ScopeConfirmed"])
        self.assertEqual(report["RowsOnPage"], 5)
        self.assertEqual(report["RowsKept"], 3)
        self.assertEqual(report["RowsDroppedOtherSource"], 1)
        self.assertEqual(report["RowsDroppedOutsideYears"], 1)
        self.assertEqual(report["PageYears"], "2018-2026")
        self.assertIn("CrossIds=YSTT4HG0", report["ScopeEvidence"])

    async def test_the_candidate_cap_is_shared_across_named_journals(self) -> None:
        # If this fails, the first named journal can take every candidate slot.
        records = await CNKIAdapter(source_site()).search(
            'source:"示例学刊"',
            request(source_journals=("示例学刊", "样本评论"), max_search_results=4, max_results_per_source=4),
        )
        self.assertEqual(len(records), 2)

    async def test_a_page_that_searched_the_whole_library_is_reported_and_not_returned(self) -> None:
        # If this fails, CNKI ignoring the journal scope comes back as a
        # restricted result -- the 2026-09-23 newspaper list, relabelled.
        whole_library = kns_page(
            [row("代表热议示例议题", "人民日报", "2026-03-05", label="报纸", dbname="CCND", resource="NEWSPAPER")],
            cross_ids="YSTT4HG0,LSTPFY1C,MPMFIG1A",
            products="CJFQ,CDFD,CCND",
        )
        adapter = CNKIAdapter(_CNKISite({("LY", "示例学刊"): [whole_library]}))
        with self.assertRaisesRegex(CNKIRestrictionUnconfirmed, "CNKI_RESTRICTION_NOT_APPLIED"):
            await adapter.search('source:"示例学刊"', request())
        report = adapter.search_restriction_report('source:"示例学刊"')
        self.assertFalse(report["ScopeConfirmed"])
        self.assertIn("CrossIds=LSTPFY1C,MPMFIG1A,YSTT4HG0", report["Note"])

    async def test_a_non_journal_row_under_a_journal_scope_fails_closed(self) -> None:
        page = kns_page(
            [
                row("示例议题治理的比较进路", "示例学刊", "2025-11-20"),
                row("示例学刊会年会召开", "示例学刊", "2026-01-01", label="报纸", dbname="CCND", resource="NEWSPAPER"),
            ]
        )
        with self.assertRaisesRegex(CNKIRestrictionUnconfirmed, "row 2 is not an academic journal article"):
            await CNKIAdapter(_CNKISite({("LY", "示例学刊"): [page]})).search('source:"示例学刊"', request())

    async def test_a_row_that_states_no_resource_type_is_not_vouched_for(self) -> None:
        page = kns_page([row("示例议题治理的比较进路", "示例学刊", "2025-11-20", label=None, dbname=None, resource=None)])
        with self.assertRaisesRegex(CNKIRestrictionUnconfirmed, "states no resource type"):
            await CNKIAdapter(_CNKISite({("LY", "示例学刊"): [page]})).search('source:"示例学刊"', request())

    async def test_a_page_that_never_states_its_scope_is_unconfirmed_after_bounded_observations(self) -> None:
        silent = kns_page([row("示例议题治理的比较进路", "示例学刊", "2025-11-20")], with_brief=False)
        site = _CNKISite({("LY", "示例学刊"): [silent]})
        with patch(SETTLE, 0), self.assertRaisesRegex(CNKIRestrictionUnconfirmed, "did not state the scope"):
            await CNKIAdapter(site).search('source:"示例学刊"', request())
        self.assertEqual(site.observations, 3)
        self.assertEqual(len(site.navigations), 1)

    async def test_a_stale_page_showing_another_search_is_not_taken_for_this_one(self) -> None:
        stale = kns_page([row("示例议题治理的比较进路", "示例学刊", "2025-11-20")], field="TI", value="上一篇论文")
        with patch(SETTLE, 0), self.assertRaisesRegex(CNKIRestrictionUnconfirmed, "different search"):
            await CNKIAdapter(_CNKISite({("LY", "示例学刊"): [stale]})).search('source:"示例学刊"', request())

    async def test_the_previous_journal_still_on_the_page_is_not_taken_for_this_one(self) -> None:
        # If this fails, the page left over from the last named journal passes
        # for this journal's results: same field, same scope, other journal.
        previous = kns_page([row("示例空间的规范责任", "样本评论", "2026-05-01")], value="样本评论")
        with patch(SETTLE, 0), self.assertRaisesRegex(CNKIRestrictionUnconfirmed, "LY=样本评论"):
            await CNKIAdapter(_CNKISite({("LY", "示例学刊"): [previous]})).search(
                'source:"示例学刊"', request()
            )

    async def test_a_result_list_still_rendering_is_observed_again_before_it_is_judged(self) -> None:
        loading = '<html><head><title>检索-中国知网</title></head><body>检索结果正在加载</body></html>'
        site = source_site()
        site.progression = [loading, fixture(SOURCE_PAGE)]
        with patch(SETTLE, 0):
            records = await CNKIAdapter(site).search('source:"示例学刊"', request())
        self.assertEqual(len(records), 3)
        self.assertEqual(site.observations, 2)
        self.assertEqual(len(site.navigations), 1)

    async def test_a_snapshot_only_observation_cannot_confirm_a_restriction(self) -> None:
        with self.assertRaisesRegex(CNKIRestrictionUnconfirmed, "accessibility snapshot"):
            await CNKIAdapter(source_site(snapshot_only=True)).search('source:"示例学刊"', request())

    async def test_a_visible_challenge_stops_a_restricted_search_for_a_person(self) -> None:
        # If this fails, the restricted path reads past a CAPTCHA the user is
        # looking at (Rules 2 and 24).
        site = source_site(probes=(rendered_challenge(),))
        adapter = CNKIAdapter(site)
        with self.assertRaisesRegex(SourceActionRequired, "ACTION_REQUIRED_USER_LOGIN=true"):
            await adapter.search('source:"示例学刊"', request())
        self.assertEqual(
            {type(command).__name__ for command in site.commands}, {"NavigateCommand", "ObserveCommand"}
        )
        # A gate is a gate, not a verdict about the restriction.
        self.assertIsNone(adapter.search_restriction_report('source:"示例学刊"'))

    async def test_the_parked_verification_preload_on_a_normal_result_page_does_not_stop_it(self) -> None:
        # The saved-structure page carries CNKI's off-screen puzzle preload, as
        # real result pages do; it is dormant, not a gate (Rule 23).
        records = await CNKIAdapter(source_site()).search('source:"示例学刊"', request())
        self.assertEqual(len(records), 3)

    async def test_a_query_longer_than_the_cnki_search_box_is_refused_before_any_request(self) -> None:
        long_question = "关于" + "示例议题与示例治理" * 12
        site = _CNKISite({})
        with self.assertRaisesRegex(CNKIRestrictedQueryRefused, "at most 100 characters"):
            await CNKIAdapter(site).search(
                long_question,
                request(source_journals=(), resource_type="JournalArticle"),
            )
        self.assertEqual(site.navigations, [])

    async def test_no_row_in_the_requested_years_is_explained_not_hidden(self) -> None:
        # If this fails, "no results" is reported as if the journal had no
        # articles in those years, when CNKI's newest-first first page simply
        # never reached them.
        adapter = CNKIAdapter(source_site())
        records = await adapter.search('source:"示例学刊"', request(year_start=2010, year_end=2015))
        self.assertEqual(records, [])
        report = adapter.search_restriction_report('source:"示例学刊"')
        self.assertTrue(report["ScopeConfirmed"])
        self.assertEqual(report["RowsDroppedOutsideYears"], 4)
        self.assertIn("later pages", report["Note"])

    async def test_a_topic_search_is_scoped_to_journals_and_keeps_every_confirmed_row(self) -> None:
        topic_page = kns_page(
            [row("示例议题治理的比较进路", "示例学刊", "2025-11-20"), row("示例技术规范", "示例学报", "2024-03-01")],
            field="SU",
            value="示例议题 规范问题",
        )
        site = _CNKISite({("SU", "示例议题 规范问题"): [topic_page]})
        records = await CNKIAdapter(site).search(
            "示例议题 规范问题",
            request(source_journals=(), resource_type="JournalArticle", keywords_cn=("示例议题", "规范问题")),
        )
        self.assertEqual([record.journal for record in records], ["示例学刊", "示例学报"])
        self.assertIn("korder=SU", site.navigations[0])

    async def test_a_record_the_exact_title_search_cannot_reach_relocks_through_the_same_scoped_search(self) -> None:
        site = source_site(relock_hits=False)
        adapter = CNKIAdapter(site)
        records = await adapter.search('source:"示例学刊"', request())
        await adapter.open_result(records[1])
        self.assertEqual(len(site.scoped_navigations), 2)
        self.assertEqual(site.scoped_navigations[0], site.scoped_navigations[1])
        self.assertIn("/kcms2/article/abstract", site.navigations[-1])


# -- the identity lock ----------------------------------------------------------


class ListingMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def _open_and_extract(self, token: str, *, detail_year: str | None = "keep"):
        articles = dict(FIXTURE_ARTICLES)
        if detail_year != "keep":
            title, journal, _year = articles[token]
            articles[token] = (title, journal or "示例学刊", detail_year)
        site = _CNKISite({("LY", "示例学刊"): [fixture(SOURCE_PAGE)]}, articles=articles)
        adapter = CNKIAdapter(site)
        records = await adapter.search('source:"示例学刊"', request())
        listed = next(record for record in records if record.navigation_url.endswith(f"v={token}&uniplatform=NZKPT&language=CHS"))
        await adapter.open_result(listed)
        return listed, await adapter.extract_metadata(search_query='source:"示例学刊"')

    async def test_an_online_first_article_page_gets_journal_and_year_from_its_listing_row(self) -> None:
        # If this fails, online-first journal articles come back with journal and
        # year "unknown" again, as they did on 2026-09-23.
        listed, detail = await self._open_and_extract("synthetic-slxk20260910001")
        self.assertTrue(detail.target_identity_confirmed)
        self.assertEqual((detail.journal, detail.year), ("示例学刊", "2026"))
        # The page's own guess, made from navigation text, is not kept.
        self.assertEqual(detail.publication_type, "JournalArticle")
        self.assertEqual(detail.publication_status, PublicationStatus.ONLINE_FIRST.value)
        self.assertEqual(detail.paper_id, listed.paper_id)

    async def test_an_article_page_that_states_its_own_journal_and_year_keeps_them(self) -> None:
        title, journal, year = FIXTURE_ARTICLES["synthetic-slxk202604003"]
        own = CNKIAdapter.parse_article_html(
            article_page(title, journal=journal, year=year),
            source_url=detail_url("synthetic-slxk202604003"),
        )
        # The page states both itself, so the identity lock compared them.
        self.assertEqual((own.journal, own.year), ("示例学刊", "2026"))
        _listed, detail = await self._open_and_extract("synthetic-slxk202604003")
        self.assertEqual((detail.journal, detail.year), ("示例学刊", "2026"))
        self.assertEqual(detail.publication_type, "JournalArticle")

    async def test_an_article_page_from_another_year_still_fails_the_identity_lock(self) -> None:
        # If this fails, the listing row's year stopped counting as identity.
        with self.assertRaisesRegex(SourceLayoutChanged, "Year mismatch"):
            await self._open_and_extract("synthetic-slxk202604003", detail_year="2024")

    async def test_exact_title_records_carry_nothing_into_the_article_record(self) -> None:
        # If this fails, the exact-title acquisition path changed.
        title = "示例技术的规范问题：一个分析框架"
        token = "synthetic-slxk20260910001"
        site = _CNKISite({}, articles={token: (title, None, None)})
        adapter = CNKIAdapter(site)
        exact = LiteratureSearchRequest(
            original_research_request=ORIGINAL,
            exact_titles=(title,),
            max_search_results=1,
            max_results_per_source=1,
            max_downloads=0,
            max_downloads_per_run=0,
        )
        records = await adapter.search(f'"{title}"', exact)
        self.assertEqual((records[0].journal, records[0].year), (UNKNOWN, UNKNOWN))
        self.assertNotIn("crossids=", site.navigations[0])
        await adapter.open_result(records[0])
        detail = await adapter.extract_metadata(search_query=f'"{title}"')
        self.assertEqual((detail.journal, detail.year), (UNKNOWN, UNKNOWN))
        self.assertIsNone(adapter.search_restriction_report(f'"{title}"'))


# -- the whole run --------------------------------------------------------------


class _RecordingPacer:
    """Notes how many scoped searches had gone out each time it was asked to wait."""

    def __init__(self, site: _CNKISite) -> None:
        self.site = site
        self.calls: list[tuple[str, int]] = []

    def pace(self, *, source: str, query: str) -> float:
        self.calls.append((query, len(self.site.scoped_navigations)))
        return 0.0


class _PlainAdapter(LiteratureSourceAdapter):
    """A source that never declared restricted search."""

    name = "PlainSource"
    human_like_delay_seconds = 0

    def __init__(self) -> None:
        super().__init__(browser=None)
        self.search_calls = 0

    async def search(self, query, request):
        self.search_calls += 1
        raise AssertionError("a restricted request must not reach this search")

    async def open_result(self, record):
        raise AssertionError("unreachable")

    async def extract_metadata(self, *, search_query):
        raise AssertionError("unreachable")

    async def extract_abstract(self):
        raise AssertionError("unreachable")

    async def check_fulltext_access(self):
        raise AssertionError("unreachable")

    async def download_fulltext(self, record, access):
        raise AssertionError("unreachable")

    async def get_citation(self):
        return {}


class RestrictedRunTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, adapter, run_request, *, pacer=None):
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        raw = tempfile.mkdtemp(prefix="cnki-restricted-", dir=TEMP_DIR)
        self.addCleanup(shutil.rmtree, raw, ignore_errors=True)
        workflow = LiteratureAcquisitionWorkflow(
            adapter,
            run_root=Path(raw) / "run",
            human_like_delay_seconds=0.0,
            allow_outside_project_for_tests=True,
            search_pacer=pacer,
        )
        with patch(SETTLE, 0), patch(ACCESS_SETTLE, 0), patch(CLICK_SETTLE, 0):
            result = await workflow.run(run_request)

        def rows_of(path: Path) -> list[dict[str, str]]:
            if not path.exists():
                return []
            with path.open(encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))

        events = [
            json.loads(line)
            for line in workflow.event_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return (
            result,
            rows_of(workflow.writer.query_log_path),
            rows_of(workflow.writer.search_results_path),
            events,
        )

    @staticmethod
    def two_journal_site(first_page: str | None = None) -> _CNKISite:
        second = kns_page([row("示例空间的规范责任", "样本评论", "2026-05-01")], value="样本评论")
        return _CNKISite(
            {
                ("LY", "示例学刊"): [first_page or fixture(SOURCE_PAGE)],
                ("LY", "样本评论"): [second],
            },
            articles={
                **FIXTURE_ARTICLES,
                token_for("示例空间的规范责任"): ("示例空间的规范责任", "样本评论", "2026"),
            },
        )

    async def test_each_restricted_search_is_paced_before_it_goes_out_and_is_one_request(self) -> None:
        # If this fails, a restricted search reaches CNKI without waiting out
        # the search pace ledger, or sends more than the one request it paced.
        site = self.two_journal_site()
        pacer = _RecordingPacer(site)
        await self._run(CNKIAdapter(site), request(source_journals=("示例学刊", "样本评论")), pacer=pacer)
        self.assertEqual(pacer.calls, [('source:"示例学刊"', 0), ('source:"样本评论"', 1)])
        self.assertEqual(len(site.scoped_navigations), 2)

    async def test_a_run_stops_searching_once_cnki_did_not_apply_the_restriction(self) -> None:
        # If this fails, a run keeps sending searches CNKI has already shown it
        # will not restrict -- volume is what draws a refusal (Rule 74).
        ignored = kns_page(
            [row("代表热议示例议题", "人民日报", "2026-03-05", label="报纸", dbname="CCND", resource="NEWSPAPER")],
            cross_ids="YSTT4HG0,MPMFIG1A",
        )
        site = self.two_journal_site(ignored)
        result, query_log, _results, _events = await self._run(
            CNKIAdapter(site), request(source_journals=("示例学刊", "样本评论"))
        )
        self.assertIs(result.status, RunStatus.SOURCE_LAYOUT_CHANGED)
        self.assertEqual(result.records, [])
        self.assertEqual(len(site.scoped_navigations), 1)
        self.assertEqual(len(query_log), 1)
        self.assertIn("CNKI_RESTRICTION_NOT_APPLIED", query_log[0]["Errors"])
        self.assertIn("CNKI_RESTRICTION_NOT_APPLIED", " ".join(result.errors))

    async def test_a_source_without_restricted_search_refuses_before_any_search(self) -> None:
        adapter = _PlainAdapter()
        result, query_log, _results, _events = await self._run(
            adapter, request(source_journals=(), resource_type="JournalArticle", keywords_cn=("示例议题",))
        )
        self.assertEqual(adapter.search_calls, 0)
        self.assertIs(result.status, RunStatus.SOURCE_UNAVAILABLE)
        self.assertIn("RESTRICTED_SEARCH_UNSUPPORTED", " ".join(result.errors))
        self.assertEqual(query_log, [])

    async def test_restricted_results_reach_the_search_results_with_journal_and_year(self) -> None:
        # If this fails, a restricted run's results lose the journal and year
        # the page confirmed -- the "unknown" columns of 2026-09-23.
        result, query_log, results, events = await self._run(CNKIAdapter(source_site()), request())
        self.assertEqual(
            sorted((item["Title"], item["Journal"], item["Year"]) for item in results),
            sorted(
                [
                    ("示例技术的规范问题：一个分析框架", "示例学刊", "2026"),
                    ("示例系统中的规则设计与责任分配", "示例学刊", "2026"),
                    ("示例议题治理的比较进路", "示例学刊", "2025"),
                ]
            ),
        )
        online_first = next(record for record in result.records if record.title.startswith("示例技术"))
        self.assertEqual(online_first.publication_status, PublicationStatus.ONLINE_FIRST.value)
        self.assertTrue(all(record.target_identity_confirmed for record in result.records))
        outcome = json.loads(query_log[0]["Filters"])["RestrictionOutcome"]
        self.assertTrue(outcome["ScopeConfirmed"])
        self.assertEqual(outcome["RowsDroppedOtherSource"], 1)
        checked = [event for event in events if event.get("action") == "literature_search_restriction_checked"]
        self.assertEqual(len(checked), 1)
        self.assertEqual(checked[0]["RowsKept"], 3)


# -- the entry points -----------------------------------------------------------


class RestrictedRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = AgentRequestRouter()

    def payload(self, **fields):
        return {"TaskType": "literature_search", "Query": ORIGINAL, "MaxCandidates": 12, **fields}

    def test_the_router_carries_a_cnki_restriction_into_the_plan(self) -> None:
        decision = self.router.route(
            self.payload(PreferredSources=["CNKI"], SourceJournals=["示例学刊", "样本评论"], YearFrom=2019, YearTo=2026)
        )
        self.assertTrue(decision.is_routable, decision.as_dict())
        planned = decision.literature_plans[0].request
        self.assertEqual(planned.source_journals, ("示例学刊", "样本评论"))
        self.assertEqual(planned.resource_type, "JournalArticle")
        self.assertEqual((planned.year_start, planned.year_end), (2019, 2026))
        self.assertEqual(
            decision.as_dict()["LiteraturePlans"][0]["Request"]["SourceJournals"], ["示例学刊", "样本评论"]
        )

    def test_the_router_refuses_a_restriction_a_selected_source_cannot_honour(self) -> None:
        for sources in (["CNKI", "SpringerLink"], "auto"):
            with self.subTest(sources=sources):
                decision = self.router.route(
                    self.payload(PreferredSources=sources, Languages=["zh", "en"], ResourceType="JournalArticle")
                )
                self.assertEqual(decision.status, "UNSUPPORTED_CAPABILITY")
                self.assertFalse(decision.harness_capability_available)
                self.assertIn("SpringerLink", decision.missing_capability)
                self.assertIn("CNKI", decision.missing_capability)

    def test_the_router_rejects_an_unknown_resource_type_as_an_invalid_request(self) -> None:
        decision = self.router.route(self.payload(PreferredSources=["CNKI"], ResourceType="Dissertation"))
        self.assertEqual(decision.status, "INVALID_REQUEST")
        self.assertIn("ResourceType", decision.missing_request_details)

    def test_the_router_refuses_year_spellings_that_disagree(self) -> None:
        decision = self.router.route(self.payload(PreferredSources=["CNKI"], YearStart=2019, YearFrom=2021))
        self.assertEqual(decision.status, "INVALID_REQUEST")
        self.assertIn("more than once", decision.missing_request_details)


class _ResearchChrome(_CNKISite):
    """Stands in for PlaywrightBrowser: the fixture site behind the live CLI path."""

    downloads_dir = None

    def __init__(self, **_options) -> None:
        super().__init__({("LY", "示例学刊"): [fixture(SOURCE_PAGE)]}, articles=FIXTURE_ARTICLES)

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def lifecycle(self) -> dict:
        return {}


class RestrictedCommandLineTests(unittest.TestCase):
    def _run_live(self, command: str, payload: dict, *, browser=None) -> tuple[int, object, dict]:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cnki-restricted-cli-", dir=TEMP_DIR) as raw:
            request_path = Path(raw) / "request.json"
            request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            args = build_parser().parse_args(
                [command, f"--request-json={request_path}", f"--run-root={Path(raw) / 'run'}"]
            )
            report = CliReport(True)
            chrome = (
                patch("hunnu_harness.literature.cli.PlaywrightBrowser", browser)
                if browser is not None
                else patch(
                    "hunnu_harness.literature.cli.PlaywrightBrowser",
                    side_effect=AssertionError("the Research Chrome must not be touched"),
                )
            )
            with chrome, patch(SETTLE, 0), patch(ACCESS_SETTLE, 0), patch(CLICK_SETTLE, 0), patch.object(
                LiteratureAcquisitionWorkflow, "_rate_limit", new=AsyncMock()
            ):
                exit_code, result = asyncio.run(_run_live_into(args, report))
        return exit_code, result, report.payload()

    def test_acquire_reports_what_cnki_confirmed_for_a_restricted_search(self) -> None:
        # If this fails, the acquire report no longer says per query whether
        # CNKI demonstrably applied the restriction and what it discarded.
        exit_code, result, report = self._run_live(
            "live-cnki",
            {
                "OriginalResearchRequest": ORIGINAL,
                "SourceJournals": ["示例学刊"],
                "YearStart": 2019,
                "YearEnd": 2026,
                "MaxSearchResults": 10,
                "MaxResultsPerSource": 10,
                "MaxDownloads": 0,
                "MaxDownloadsPerRun": 0,
            },
            browser=_ResearchChrome,
        )
        self.assertEqual(exit_code, 0, report)
        self.assertEqual(report["Results"], 3)
        self.assertEqual(report["SearchRestriction"]["SourceJournals"], ["示例学刊"])
        (outcome,) = report["SearchRestrictionOutcome"]
        self.assertTrue(outcome["ScopeConfirmed"])
        self.assertEqual(outcome["RowsKept"], 3)
        self.assertEqual(outcome["RowsDroppedOutsideYears"], 1)

    def test_acquire_refuses_a_restricted_request_for_a_source_without_it_before_chrome_starts(self) -> None:
        exit_code, result, report = self._run_live(
            "live-springerlink",
            {"OriginalResearchRequest": ORIGINAL, "ResourceType": "JournalArticle", "KeywordsEN": ["AI ethics"]},
        )
        self.assertEqual(exit_code, EXIT_RUN_FAILED)
        self.assertIsNone(result)
        self.assertEqual(report["Status"], "UNSUPPORTED_CAPABILITY")
        self.assertIn("CNKI only", report["MissingCapability"])

    def test_acquire_reports_an_invalid_restriction_instead_of_a_traceback(self) -> None:
        exit_code, result, report = self._run_live(
            "live-cnki", {"OriginalResearchRequest": ORIGINAL, "ResourceType": "Dissertation"}
        )
        self.assertEqual(exit_code, EXIT_RUN_FAILED)
        self.assertIsNone(result)
        self.assertEqual(report["Status"], "INVALID_REQUEST")
        self.assertIn("ResourceType", report["Reason"])

    def test_capabilities_name_cnki_as_the_only_restricted_search_source(self) -> None:
        sources = {item["cli_source"]: item["supports_restricted_search"] for item in build_capabilities()["Sources"]}
        self.assertEqual(
            sources,
            {"sciencedirect": False, "springerlink": False, "cnki": True, "oxfordacademic": False},
        )


if __name__ == "__main__":
    unittest.main()
