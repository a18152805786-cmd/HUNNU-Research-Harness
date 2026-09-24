from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote_plus, unquote, urlsplit

from hunnu_harness.browser.commands import (
    BrowserCommandError,
    BrowserObservation,
    BrowserTargetObservation,
    ClickCommand,
    DownloadArtifact,
    DownloadCommand,
    DownloadFailure,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from hunnu_harness.literature.adapters.base import SourceActionRequired, SourceUnavailable
from hunnu_harness.literature.adapters.cnki import (
    CNKIAdapter,
    _ACCESS_SETTLE_MAX_OBSERVATIONS,
    _canonicalize_cnki_title_identity,
    _decode_cnki_form_query_value,
)
from hunnu_harness.literature.cnki_challenge import ChallengeState
from hunnu_harness.literature.adapters.sciencedirect import ScienceDirectAdapter
from hunnu_harness.literature.adapters.springerlink import SpringerLinkAdapter
from hunnu_harness.literature.artifacts import LiteratureArtifactWriter
from hunnu_harness.literature.dedupe import LiteratureDeduplicator
from hunnu_harness.literature.downloads import InvalidFullTextDownload, LiteratureDownloadManager
from hunnu_harness.literature.fulltext import AuthorizedFullTextValidator
from hunnu_harness.literature.models import (
    AccessDecision,
    AccessType,
    FullTextFormat,
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
    UNKNOWN,
)
from hunnu_harness.literature.normalization import normalize_title, sha256_file
from hunnu_harness.literature.planning import LiteratureSearchPlanner
from hunnu_harness.literature.workflow import finalize_captured_cnki_acceptance

from literature_test_support import isolated_fetch_ledger, write_minimal_pdf


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
ARTICLE_URL = (
    "https://kns.cnki.net/kcms2/article/abstract?dbcode=CJFD&filename=KJYJ202601001"
    "&uniplatform=NZKPT&language=CHS"
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def kw_as_cnki_reads_it(url: str) -> str:
    """Decode ``kw`` the way CNKI does: percent-decoding only, ``+`` stays ``+``.

    ``parse_qs`` would form-decode a ``+`` into a space and hide exactly what
    these tests guard: CNKI's own record of a query sent as ``A+B`` is ``A+B``.
    """

    raw = urlsplit(url).query.split("kw=", 1)[1].split("&", 1)[0]
    return unquote(raw)


def authorized(full_text_format: FullTextFormat) -> AccessDecision:
    return AccessDecision(
        full_text_accessible=True,
        access_type=AccessType.INSTITUTIONAL_AUTHENTICATED,
        authorized_access=True,
        status=RunStatus.SUCCESS,
        reason="Official CNKI single-paper control",
        full_text_format=full_text_format,
    )


class CNKIParserTests(unittest.TestCase):
    @staticmethod
    def authenticated_header(*, extra_body: str = "") -> str:
        return f'''
        <header class="ecp_header_login_area">
          <div class="ecp_header_login_status ecp_header_login_status1">
            <div class="ecp_header_unitName" title="湖南师范大学" style="display: inline-block;">
              湖南师范大学
            </div>
            <div class="ecp_header_personal_loginbg">个人登录</div>
          </div>
        </header>
        {extra_body}
        '''

    def test_exact_title_result_parsing_is_bounded_and_deduplicated(self) -> None:
        records = CNKIAdapter.parse_search_results_html(
            fixture("cnki_search.html"),
            query="人工智能漂洗、审计监督与盈余管理",
            max_results=1,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "人工智能漂洗、审计监督与盈余管理")
        self.assertEqual(records[0].stable_identifier, "cjfd:KJYJ202601001")
        self.assertNotIn("temporary-removed", records[0].source_page)
        self.assertEqual(records[0].source_database, "CNKI")

    def test_highlighted_exact_title_collapses_only_cnki_markup_spacing(self) -> None:
        title = "供应链冲击、多元化战略与企业发展韧性——来自中国重大自然灾害的证据"
        html = f"""
        <html><body><main><div>共找到 1 条结果</div>
          <a class="fz14 inline" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=GGYY202409007">
            <font color="red">供应链冲击</font>、<font color="red">多元化战略与企业发展韧性</font>
            ——<font color="red">来自中国重大自然灾害的证据</font>
          </a>
        </main></body></html>
        """
        records = CNKIAdapter.parse_search_results_html(html, query=title, max_results=1)
        self.assertEqual([record.title for record in records], [title])
        self.assertEqual(
            _canonicalize_cnki_title_identity(records[0].title),
            _canonicalize_cnki_title_identity(title),
        )

    def test_highlighted_title_collapses_question_and_quote_markup_spacing(self) -> None:
        title = "社会信用环境改善降低了企业违规吗？——来自“中国社会信用体系建设”的证据"
        html = """
        <html><body><main><div>共找到 1 条结果</div>
          <a class="fz14 inline" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=JRYJ202301001">
            <font color="red">社会信用环境改善降低了企业违规吗</font>?——
            <font color="red">来自</font>“<font color="red">中国社会信用体系建设</font>”
            <font color="red">的证据</font>
          </a>
        </main></body></html>
        """
        records = CNKIAdapter.parse_search_results_html(html, query=title, max_results=1)
        self.assertEqual(
            _canonicalize_cnki_title_identity(records[0].title),
            _canonicalize_cnki_title_identity(title),
        )
        self.assertEqual(records[0].title, title.replace("？", "?"))

    def test_search_modes_are_explicit_and_do_not_embed_credentials(self) -> None:
        self.assertIn("korder=TI", CNKIAdapter.build_search_url("人工智能漂洗", mode="exact_title"))
        self.assertIn("korder=AU", CNKIAdapter.build_search_url("张三", mode="author"))
        self.assertIn("korder=SU", CNKIAdapter.build_search_url("盈余管理", mode="keyword"))

    def test_exact_title_relock_decodes_form_plus_before_identity_comparison(self) -> None:
        requested = "全球价值链升级——基于中国上市公司的实证研究"
        serialized = "全球价值链升级+——+基于中国上市公司的实证研究"
        observed = "全球价值链升级——基于中国上市公司的实证研究"
        self.assertEqual(_decode_cnki_form_query_value(serialized), "全球价值链升级 —— 基于中国上市公司的实证研究")
        self.assertEqual(_canonicalize_cnki_title_identity(observed), _canonicalize_cnki_title_identity(requested))
        search = LiteratureRecord(paper_id="search", title=requested)
        detail = LiteratureRecord(paper_id="detail", title=observed)
        self.assertTrue(CNKIAdapter.identity_matches(search, detail)[0])

    def test_exact_title_query_removes_cnki_markup_spacing_but_keeps_english_spaces(self) -> None:
        spaced = "A —— B"
        compact = "A——B"
        self.assertEqual(
            _canonicalize_cnki_title_identity(spaced),
            _canonicalize_cnki_title_identity(compact),
        )
        url = CNKIAdapter.build_search_url(spaced, mode="exact_title")
        self.assertNotIn("%20%E2%80%94", url)
        self.assertEqual(_canonicalize_cnki_title_identity("AI and firms"), "ai and firms")
        self.assertIn("%3F%20Evidence", CNKIAdapter.build_search_url("Does AI matter? Evidence", mode="exact_title"))

    def test_form_plus_between_words_is_decoded_at_the_query_boundary(self) -> None:
        self.assertEqual(_decode_cnki_form_query_value("A+B"), "A B")
        self.assertEqual(
            _canonicalize_cnki_title_identity(_decode_cnki_form_query_value("A+B")),
            _canonicalize_cnki_title_identity("A B"),
        )

    def test_literal_plus_in_bibliographic_title_is_preserved(self) -> None:
        literal = "C++相关研究"
        without_plus = "C  相关研究"
        self.assertEqual(_canonicalize_cnki_title_identity(literal), "c++相关研究")
        self.assertNotEqual(
            _canonicalize_cnki_title_identity(literal),
            _canonicalize_cnki_title_identity(without_plus),
        )
        self.assertIn("%2B%2B", CNKIAdapter.build_search_url(literal, mode="exact_title").upper())

    def test_a_space_in_a_keyword_search_reaches_cnki_as_a_space_not_a_plus(self) -> None:
        # If this fails, CNKI again runs "城市轨道交通+客流+预测": it keeps a "+" sent for a
        # space and reads it as OR, and a three-term search sent that way on 2026-09-23
        # came back as ten papers, none of them on its topic.
        url = CNKIAdapter.build_search_url("城市轨道交通 客流 预测", mode="keyword")
        self.assertIn("korder=SU", url)
        self.assertEqual(kw_as_cnki_reads_it(url), "城市轨道交通 客流 预测")

    def test_a_plus_typed_in_a_keyword_still_reaches_cnki_as_a_plus(self) -> None:
        # If this fails, encoding spaces as %20 also swallowed a "+" that belongs to the term.
        url = CNKIAdapter.build_search_url("互联网+ 企业创新", mode="keyword")
        self.assertEqual(kw_as_cnki_reads_it(url), "互联网+ 企业创新")

    def test_a_space_left_in_an_exact_title_reaches_cnki_as_a_space(self) -> None:
        # If this fails, CNKI again receives "Cladosporium+sp.+SCSIO+41415": the relock search
        # for this title on 2026-08-20 ran as OR, matched 29,918 records and lost its target.
        title = "北部湾海绵共附生真菌Cladosporium sp. SCSIO 41415次级代谢产物研究"
        url = CNKIAdapter.build_search_url(title, mode="exact_title")
        self.assertIn("korder=TI", url)
        self.assertEqual(kw_as_cnki_reads_it(url), title)

    def test_a_space_in_an_author_name_reaches_cnki_as_a_space(self) -> None:
        # If this fails, an author name reaches CNKI as "John+A.+Smith", which it runs as
        # any one of the words.
        url = CNKIAdapter.build_search_url("John A. Smith", mode="author")
        self.assertIn("korder=AU", url)
        self.assertEqual(kw_as_cnki_reads_it(url), "John A. Smith")

    def test_a_search_without_a_space_or_a_title_hyphen_sends_the_same_url_as_before(self) -> None:
        # If this fails, the change reached past spaces and title hyphens, and searches that
        # already worked -- exact titles, author names and keywords alike -- now send CNKI a
        # different request.  (A hyphen in a title search is the other deliberate change; the
        # HIF-1α title that used to sit here never found its paper, see the hyphen tests below.)
        cases = (
            ("keyword", "SU", "城市轨道交通"),
            ("keyword", "SU", "A/B?C&D=E#F%G：H"),
            ("author", "AU", "张三"),
            ("exact_title", "TI", "城市轨道交通客流预测——基于深度学习的方法"),
            ("exact_title", "TI", "高速铁路:网络演化与区域可达性"),
            ("exact_title", "TI", "“互联网+”为什么加出了业绩"),
            ("exact_title", "TI", "云南省德宏州2014—2025年间日疟复发趋势分析"),
        )
        for mode, order, query in cases:
            with self.subTest(mode=mode, query=query):
                self.assertEqual(
                    CNKIAdapter.build_search_url(query, mode=mode),
                    f"https://kns.cnki.net/kns8s/defaultresult/index?korder={order}&kw={quote_plus(query)}",
                )

    def test_a_hyphen_in_an_exact_title_reaches_cnki_as_a_space_not_as_not(self) -> None:
        # If this fails, CNKI's search box again reads the hyphen as NOT.  Each of these four
        # titles was searched that way on 2026-08-20 and none returned its paper -- which it
        # never can, because the paper's own title holds the excluded words.  "山-水" came back
        # as 1,960,998 records that all contain 山 and none 池 or 湖, as "山 NOT 池 NOT 湖" would.
        cases = (
            (
                "“山-水”“山-池-宅”“山-湖-城”——空性、气韵之于绘画、园林与城市",
                "“山 水”“山 池 宅”“山 湖 城”——空性、气韵之于绘画、园林与城市",
            ),
            (
                "HIF-1α对缺血性结肠炎小鼠巨噬细胞极化的影响机制",
                "HIF 1α对缺血性结肠炎小鼠巨噬细胞极化的影响机制",
            ),
            (
                "基于全二维气相色谱-飞行时间质谱的不同质量等级浓酱兼香型白酒挥发性风味物质差异分析",
                "基于全二维气相色谱 飞行时间质谱的不同质量等级浓酱兼香型白酒挥发性风味物质差异分析",
            ),
            (
                "基于脊髓背角TSP-4/α2δ-1表达变化探讨电针缓解神经病理性疼痛的机制",
                "基于脊髓背角TSP 4/α2δ 1表达变化探讨电针缓解神经病理性疼痛的机制",
            ),
        )
        for title, sent in cases:
            with self.subTest(title=title):
                url = CNKIAdapter.build_search_url(title, mode="exact_title")
                self.assertIn("korder=TI", url)
                self.assertEqual(kw_as_cnki_reads_it(url), sent)

    def test_a_fullwidth_hyphen_in_an_exact_title_does_not_reach_cnki_as_not_either(self) -> None:
        # If this fails, a title written with a full-width "－" still reaches CNKI as NOT: the
        # exact-title canonicalization (NFKC) turns it into "-" before the query is sent.
        url = CNKIAdapter.build_search_url("山东典型丘陵区土壤－玉米系统重金属迁移富集规律", mode="exact_title")
        self.assertEqual(kw_as_cnki_reads_it(url), "山东典型丘陵区土壤 玉米系统重金属迁移富集规律")

    def test_a_spaced_hyphen_in_an_exact_title_leaves_a_single_space(self) -> None:
        # If this fails, "A - B" reaches CNKI as a run of spaces, a form no saved search shows.
        url = CNKIAdapter.build_search_url("Urban rail transit - Evidence from Chinese cities", mode="exact_title")
        self.assertEqual(kw_as_cnki_reads_it(url), "Urban rail transit Evidence from Chinese cities")

    def test_author_and_keyword_searches_still_send_a_hyphen_as_typed(self) -> None:
        # If this fails, sending a hyphen as a space has spread past title searches.  On
        # 2026-09-24 the user chose to change title searches only; widening it is their call.
        for mode, query in (("author", "Jean-Pierre Dupont"), ("keyword", "COVID-19 城市轨道交通客流")):
            with self.subTest(mode=mode):
                self.assertEqual(kw_as_cnki_reads_it(CNKIAdapter.build_search_url(query, mode=mode)), query)

    def test_the_identity_lock_still_compares_the_real_hyphenated_title(self) -> None:
        # If this fails, the search form leaked into the identity lock: a record whose title has
        # a space where the requested title has a hyphen would pass as the same paper.
        title = "HIF-1α对缺血性结肠炎小鼠巨噬细胞极化的影响机制"
        requested = LiteratureRecord(paper_id="requested", title=title)
        same = LiteratureRecord(paper_id="same", title=title)
        spaced = LiteratureRecord(paper_id="spaced", title=title.replace("-", " "))
        self.assertTrue(CNKIAdapter.identity_matches(requested, same)[0])
        self.assertEqual(CNKIAdapter.identity_matches(requested, spaced), (False, "Target title mismatch"))

    def test_exact_title_relock_rejects_subtitle_mismatch(self) -> None:
        left = LiteratureRecord(paper_id="left", title="A——基于中国上市公司的研究")
        right = LiteratureRecord(paper_id="right", title="A——基于中国制造业企业的研究")
        self.assertFalse(CNKIAdapter.identity_matches(left, right)[0])

    def test_exact_title_relock_rejects_author_mismatch(self) -> None:
        left = LiteratureRecord(paper_id="left", title="A", authors=("张三",), year="2025")
        right = LiteratureRecord(paper_id="right", title="A", authors=("李四",), year="2025")
        matched, reason = CNKIAdapter.identity_matches(left, right)
        self.assertFalse(matched)
        self.assertEqual(reason, "Author mismatch")

    def test_exact_title_relock_rejects_year_mismatch(self) -> None:
        left = LiteratureRecord(paper_id="left", title="A", authors=("张三",), year="2025")
        right = LiteratureRecord(paper_id="right", title="A", authors=("张三",), year="2024")
        matched, reason = CNKIAdapter.identity_matches(left, right)
        self.assertFalse(matched)
        self.assertEqual(reason, "Year mismatch")

    def test_exact_title_relock_ignores_cnki_affiliation_markers(self) -> None:
        left = LiteratureRecord(
            paper_id="left",
            title="企业如何出口骗税",
            authors=("李红 1; 包群 2; 樊军锋 2",),
        )
        right = LiteratureRecord(
            paper_id="right",
            title="企业如何出口骗税",
            authors=("李红", "包群", "樊军锋"),
        )
        self.assertTrue(CNKIAdapter.identity_matches(left, right)[0])

    def test_exact_title_relock_accepts_cnki_subtitle_separator_variant(self) -> None:
        left = LiteratureRecord(
            paper_id="left",
            title="最低工资与异质性人力资本需求——基于招聘网站数据的研究",
        )
        right = LiteratureRecord(
            paper_id="right",
            title="最低工资与异质性人力资本需求:基于招聘网站数据的研究",
        )
        self.assertTrue(CNKIAdapter.identity_matches(left, right)[0])

    def test_existing_cnki_exact_title_fixture_identity_remains_locked(self) -> None:
        search = CNKIAdapter.parse_search_results_html(
            fixture("cnki_search.html"), query="人工智能漂洗", max_results=1
        )[0]
        detail = CNKIAdapter.parse_article_html(fixture("cnki_article_pdf.html"), source_url=ARTICLE_URL)
        self.assertTrue(CNKIAdapter.identity_matches(search, detail)[0])

    def test_structured_snapshot_search_is_bounded_and_deduplicated(self) -> None:
        records = CNKIAdapter.parse_search_results_snapshot(
            fixture("cnki_search_snapshot.yml"),
            query='"投贷联动、媒体监督与科技企业AI漂洗"',
            max_results=1,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "投贷联动、媒体监督与科技企业AI漂洗")
        self.assertEqual(records[0].stable_identifier, "cjfq:CJKX202602009")

    def test_structured_snapshot_extracts_article_metadata(self) -> None:
        record = CNKIAdapter.parse_article_snapshot(
            fixture("cnki_article_snapshot.yml"),
            source_url=(
                "https://kns.cnki.net/kcms2/article/abstract?"
                "dbcode=CJFQ&filename=CJKX202602009"
            ),
        )
        self.assertEqual(record.title, "投贷联动、媒体监督与科技企业AI漂洗")
        self.assertEqual(record.authors, ("李媛媛", "崔梦萦"))
        self.assertEqual(record.journal, "财经科学")
        self.assertEqual(record.year, "2026")
        self.assertEqual(record.issue, "02")
        self.assertEqual(record.pages_or_article_number, "113-126")
        self.assertEqual(record.doi, "10.27041/j.cnki.cjkx.2026.02.008")
        self.assertEqual(record.keywords, ("人工智能", "AI漂洗"))
        self.assertIn("媒体监督", record.abstract)

    def test_structured_snapshot_accepts_volume_comma_spacing(self) -> None:
        snapshot = fixture("cnki_article_snapshot.yml").replace(
            "财经科学 . 2026 (02) : 113-126",
            "财经科学 . 2026 ,46 (02) : 113-126",
        )
        record = CNKIAdapter.parse_article_snapshot(
            snapshot,
            source_url=(
                "https://kns.cnki.net/kcms2/article/abstract?"
                "dbcode=CJFQ&filename=CJKX202602009"
            ),
        )
        self.assertEqual(record.year, "2026")
        self.assertEqual(record.issue, "02")
        self.assertEqual(record.pages_or_article_number, "113-126")

    def test_structured_snapshot_access_prefers_authorized_pdf(self) -> None:
        decision = CNKIAdapter.check_fulltext_access_snapshot(
            fixture("cnki_article_snapshot.yml"),
            source_url=ARTICLE_URL,
        )
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)
        self.assertEqual(decision.full_text_format, FullTextFormat.PDF)
        self.assertEqual(decision.download_locator, "PDF下载")

    def test_metadata_and_doi_are_normalized(self) -> None:
        record = CNKIAdapter.parse_article_html(
            fixture("cnki_article_pdf.html"), source_url=ARTICLE_URL, search_query="人工智能漂洗"
        )
        self.assertEqual(record.title, "人工智能漂洗、审计监督与盈余管理")
        self.assertEqual(record.authors, ("张三", "李四"))
        self.assertEqual(record.year, "2026")
        self.assertEqual(record.journal, "会计研究")
        self.assertEqual(record.volume, "42")
        self.assertEqual(record.issue, "3")
        self.assertEqual(record.pages_or_article_number, "18-31")
        self.assertEqual(record.doi, "10.1234/cnki.test.2026.001")
        self.assertEqual(record.issn, "1003-2886")
        self.assertEqual(record.language, "zh")
        self.assertEqual(record.stable_identifier, "cjfd:KJYJ202601001")

    def test_abstract_and_keywords_preserve_chinese_utf8(self) -> None:
        record = CNKIAdapter.parse_article_html(fixture("cnki_article_pdf.html"), source_url=ARTICLE_URL)
        self.assertIn("审计监督", record.abstract)
        self.assertEqual(record.keywords, ("人工智能漂洗", "审计监督", "盈余管理"))

    def test_missing_doi_remains_unknown(self) -> None:
        record = CNKIAdapter.parse_article_html(fixture("cnki_article_caj.html"), source_url=ARTICLE_URL)
        self.assertEqual(record.doi, UNKNOWN)

    def test_live_cnki_semantic_structure_extracts_metadata_without_citation_meta(self) -> None:
        record = CNKIAdapter.parse_article_html(
            fixture("cnki_article_live_structure.html"),
            source_url="https://kns.cnki.net/kcms2/article/abstract?v=redacted",
        )
        self.assertEqual(record.title, "投贷联动、媒体监督与科技企业AI漂洗")
        self.assertEqual(record.authors, ("李媛媛", "崔梦萦"))
        self.assertEqual(record.journal, "财经科学")
        self.assertEqual(record.year, "2026")
        self.assertEqual(record.issue, "02")
        self.assertEqual(record.pages_or_article_number, "113-126")
        self.assertEqual(record.doi, "10.27041/j.cnki.cjkx.2026.02.008")
        self.assertEqual(record.stable_identifier, "cjfq:CJKX202602009")
        self.assertIn("媒体监督", record.abstract)
        self.assertIn("AI漂洗", record.keywords)

    def test_journal_evidence_outranks_dissertation_navigation_text(self) -> None:
        html = fixture("cnki_article_live_structure.html").replace(
            "<body>",
            "<body><nav>学术期刊 学位论文 会议论文 报纸</nav>",
        )

        record = CNKIAdapter.parse_article_html(
            html,
            source_url="https://kns.cnki.net/kcms2/article/abstract?v=redacted",
        )

        self.assertEqual(record.journal, "财经科学")
        self.assertEqual(record.publication_type, "JournalArticle")
        self.assertEqual(record.publication_status, PublicationStatus.UNKNOWN.value)

    def test_hidden_cnki_title_ui_marker_is_not_bibliographic_text(self) -> None:
        html = fixture("cnki_article_live_structure.html").replace(
            "<h1>投贷联动、媒体监督与科技企业AI漂洗</h1>",
            '<h1>投贷联动、媒体监督与科技企业AI漂洗'
            '<span id="corr-video" style="display: none">附视频</span></h1>',
        )
        record = CNKIAdapter.parse_article_html(
            html,
            source_url="https://kns.cnki.net/kcms2/article/abstract?v=redacted",
        )
        self.assertEqual(record.title, "投贷联动、媒体监督与科技企业AI漂洗")

    def test_pdf_access_requires_enabled_single_paper_control(self) -> None:
        decision = CNKIAdapter.check_fulltext_access_html(fixture("cnki_article_pdf.html"), source_url=ARTICLE_URL)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)
        self.assertEqual(decision.full_text_format, FullTextFormat.PDF)
        self.assertEqual(decision.download_locator, "PDF下载")

    def test_html_access_prefers_pdf_when_caj_precedes_it_in_dom(self) -> None:
        html = """
        <html><body><div>当前机构已获得全文访问权限</div>
          <a href="/download/article/target.caj">CAJ下载</a>
          <a href="/download/article/target.pdf">PDF下载</a>
        </body></html>
        """
        decision = CNKIAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.full_text_format, FullTextFormat.PDF)
        self.assertEqual(decision.download_locator, "PDF下载")

    def test_html_access_uses_caj_when_pdf_control_is_disabled(self) -> None:
        html = """
        <html><body><div>当前机构已获得全文访问权限</div>
          <a href="/download/article/target.pdf" aria-disabled="true">PDF下载</a>
          <a href="/download/article/target.caj">CAJ下载</a>
        </body></html>
        """
        decision = CNKIAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.full_text_format, FullTextFormat.CAJ)
        self.assertEqual(decision.download_locator, "CAJ下载")

    def test_html_access_uses_cnki_caj_when_pdf_control_is_external(self) -> None:
        html = """
        <html><body><div>当前机构已获得全文访问权限</div>
          <a href="https://example.invalid/target.pdf">PDF下载</a>
          <a href="/download/article/target.caj">CAJ下载</a>
        </body></html>
        """
        decision = CNKIAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.full_text_format, FullTextFormat.CAJ)
        self.assertEqual(decision.download_locator, "CAJ下载")

    def test_snapshot_access_prefers_pdf_when_caj_precedes_it(self) -> None:
        snapshot = """
        ### Page
        - Page URL: https://kns.cnki.net/kcms2/article/abstract?dbcode=CJFQ&filename=TEST
        ### Snapshot
        - banner: 湖南师范大学
        - link "CAJ下载" [ref=a1]:
          - /url: https://bar.cnki.net/bar/download/order?id=caj
        - link "PDF下载" [ref=a2]:
          - /url: https://bar.cnki.net/bar/download/order?id=pdf
        """
        decision = CNKIAdapter.check_fulltext_access_snapshot(snapshot, source_url=ARTICLE_URL)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.full_text_format, FullTextFormat.PDF)
        self.assertEqual(decision.download_locator, "PDF下载")

    def test_caj_access_is_supported_without_conversion(self) -> None:
        decision = CNKIAdapter.check_fulltext_access_html(fixture("cnki_article_caj.html"), source_url=ARTICLE_URL)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.full_text_format, FullTextFormat.CAJ)

    def test_abstract_visibility_is_not_fulltext_access(self) -> None:
        decision = CNKIAdapter.check_fulltext_access_html(
            fixture("cnki_article_metadata_only.html"), source_url=ARTICLE_URL
        )
        self.assertFalse(decision.full_text_accessible)
        self.assertFalse(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.METADATA_ONLY)

    def test_recommended_and_batch_controls_are_rejected(self) -> None:
        html = "<html><body><a href='/batch'>批量下载</a><a href='/recommend'>相关推荐</a></body></html>"
        decision = CNKIAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertFalse(decision.authorized_access)

    def test_order_control_without_verified_access_is_metadata_only(self) -> None:
        decision = CNKIAdapter.check_fulltext_access_html(
            fixture("cnki_article_live_structure.html"),
            source_url="https://kns.cnki.net/kcms2/article/abstract?v=redacted",
        )
        self.assertFalse(decision.full_text_accessible)
        self.assertFalse(decision.authorized_access)
        self.assertEqual(decision.full_text_format, FullTextFormat.PDF)
        self.assertEqual(decision.access_type, AccessType.UNKNOWN)
        self.assertIn("FULLTEXT_ACCESS_UNKNOWN", decision.reason)

    def test_authenticated_order_action_overrides_generic_personal_purchase_text(self) -> None:
        html = fixture("cnki_article_live_structure.html").replace(
            "<body>",
            "<body>"
            + self.authenticated_header(extra_body='<div class="site-nav">个人登录 购买 充值</div>')
            + '<div style="display:none">单篇购买 请登录后购买</div>',
            1,
        )
        decision = CNKIAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertTrue(decision.full_text_accessible)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)
        self.assertEqual(decision.full_text_format, FullTextFormat.PDF)

    def test_current_article_explicit_not_subscribed_state_is_not_authorized(self) -> None:
        html = f'''
        <html><head><title>CNKI</title></head><body>
          {self.authenticated_header()}
          <article><h1>人工智能时代下企业智能化转型与全球价值链升级</h1>
          <p>机构未订购，单篇购买</p></article>
        </body></html>
        '''
        decision = CNKIAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertFalse(decision.full_text_accessible)
        self.assertFalse(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.METADATA_ONLY)

    def test_ambiguous_fulltext_action_is_unknown_even_with_institution_header(self) -> None:
        html = fixture("cnki_article_live_structure.html").replace(
            "<body>",
            "<body>"
            + self.authenticated_header(extra_body="<div>购买</div>"),
            1,
        ).replace(
            "https://bar.cnki.net/bar/download/order?id=redacted-for-fixture",
            "/fulltext/action",
        )
        decision = CNKIAdapter.check_fulltext_access_html(
            html,
            source_url="https://kns.cnki.net/kcms2/article/abstract?v=redacted",
        )
        self.assertFalse(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.UNKNOWN)
        self.assertIn("FULLTEXT_ACCESS_UNKNOWN", decision.reason)

    def test_login_requirement_without_authentication_remains_an_authentication_stop(self) -> None:
        html = fixture("cnki_article_live_structure.html").replace(
            "<body>",
            "<body><div>请登录后下载全文</div>",
            1,
        )
        with self.assertRaisesRegex(SourceActionRequired, "ACTION_REQUIRED_USER_LOGIN=true"):
            CNKIAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)

    def test_hidden_purchase_template_does_not_block_authorized_action(self) -> None:
        html = fixture("cnki_article_live_structure.html").replace(
            "<body>",
            "<body>"
            + self.authenticated_header(
                extra_body='<div class="purchase-modal" style="display:none">单篇购买 请登录后购买 权限不足</div>'
            ),
            1,
        )
        decision = CNKIAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)

    def test_visible_single_article_purchase_without_action_is_not_authorized(self) -> None:
        html = f'''
        <html><head><title>CNKI</title></head><body>
          {self.authenticated_header()}
          <article><h1>人工智能时代下企业智能化转型与全球价值链升级</h1>
          <p>单篇购买</p></article>
        </body></html>
        '''
        decision = CNKIAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertFalse(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.METADATA_ONLY)

    def test_institution_name_alone_does_not_authorize_fulltext(self) -> None:
        html = f'''
        <html><head><title>CNKI</title></head><body>
          {self.authenticated_header()}
          <article><h1>人工智能时代下企业智能化转型与全球价值链升级</h1></article>
        </body></html>
        '''
        decision = CNKIAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertFalse(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.METADATA_ONLY)

    def test_identity_lock_accepts_stable_identifier_and_rejects_wrong_doi(self) -> None:
        search = CNKIAdapter.parse_search_results_html(
            fixture("cnki_search.html"), query="人工智能漂洗", max_results=1
        )[0]
        detail = CNKIAdapter.parse_article_html(fixture("cnki_article_pdf.html"), source_url=ARTICLE_URL)
        self.assertTrue(CNKIAdapter.identity_matches(search, detail)[0])
        search.doi = "10.1234/wrong"
        self.assertFalse(CNKIAdapter.identity_matches(search, detail)[0])

    def test_security_challenge_stops_for_manual_action(self) -> None:
        with self.assertRaisesRegex(SourceActionRequired, "ACTION_REQUIRED_USER_LOGIN=true"):
            CNKIAdapter.parse_search_results_html(fixture("cnki_security_challenge.html"), query="测试")

    def test_title_normalization_handles_fullwidth_punctuation_without_breaking_english(self) -> None:
        self.assertEqual(normalize_title("人工智能漂洗（审计监督）"), normalize_title("人工智能漂洗 (审计监督)"))
        self.assertEqual(normalize_title("AI Washing: Audit"), normalize_title("AI washing — audit"))

    def test_pdf_title_identity_tolerates_extractor_inserted_spaces(self) -> None:
        self.assertTrue(
            AuthorizedFullTextValidator._title_matches_extracted_text(
                "投贷联动、媒体监督与科技企业AI漂洗",
                "《财经科学》2026年第2期\n投贷联动、媒体监督与科技企业 AI 漂洗\n李媛媛 崔梦萦",
            )
        )


class CNKIAuthenticationClassificationTests(unittest.TestCase):
    @staticmethod
    def authenticated_header(*, extra_body: str = "") -> str:
        return f"""
        <html><head><title>检索-中国知网</title></head><body>
          <header class="ecp_header_login_area">
            <div class="ecp_header_login_status ecp_header_login_status1">
              <div class="ecp_header_unitName" title="湖南师范大学" style="display: inline-block;">
                湖南师范大学
              </div>
              <div class="ecp_header_personal_loginbg">个人登录</div>
            </div>
          </header>
          <main><section aria-label="检索结果">学术期刊 检索结果</section></main>
          {extra_body}
        </body></html>
        """

    def test_institution_authenticated_with_personal_login_entry_is_allowed(self) -> None:
        html = self.authenticated_header(
            extra_body='<div style="display:none"><p>请登录个人账号使用。</p></div>'
        )
        CNKIAdapter.detect_interruption(
            html,
            url="https://kns.cnki.net/kns8s/defaultresult/index?korder=TI&kw=test",
        )

    def test_explicit_login_requirement_without_institution_authentication_stops(self) -> None:
        html = "<html><head><title>CNKI 登录</title></head><body><main>请登录后下载全文</main></body></html>"
        with self.assertRaisesRegex(SourceActionRequired, "manual authentication required"):
            CNKIAdapter.detect_interruption(html, url="https://kns.cnki.net/login")

    def test_authenticated_page_with_dormant_personal_login_ui_is_allowed(self) -> None:
        html = self.authenticated_header(
            extra_body="""
              <div class="ecp_personalLoginBox" style="display:none">个人登录 请登录个人账号使用</div>
              <div class="verification-component" style="position:absolute;top:-1000000px">安全验证 验证码</div>
            """
        )
        CNKIAdapter.detect_interruption(
            html,
            url="https://kns.cnki.net/kns8s/defaultresult/index?korder=TI&kw=test",
        )

    def test_active_security_challenge_still_precedes_institution_authentication(self) -> None:
        html = self.authenticated_header(extra_body="<h1>安全验证</h1><p>请完成滑块验证码后继续。</p>")
        html = html.replace("学术期刊 检索结果", "")
        with self.assertRaisesRegex(SourceActionRequired, "CAPTCHA or security verification"):
            CNKIAdapter.detect_interruption(html, url="https://kns.cnki.net/security-check")

    def test_unrelated_institution_name_does_not_authenticate(self) -> None:
        html = """
        <html><head><title>CNKI</title></head><body>
          <article>历史合作机构包括湖南师范大学。</article>
          <main>请登录后继续</main>
        </body></html>
        """
        with self.assertRaisesRegex(SourceActionRequired, "manual authentication required"):
            CNKIAdapter.detect_interruption(html, url="https://kns.cnki.net/account")

    def test_existing_cnki_challenge_fixtures_keep_their_behavior(self) -> None:
        CNKIAdapter.detect_interruption(
            fixture("cnki_dormant_challenge.html"),
            url="https://kns.cnki.net/kns8s/search?kw=test",
        )
        with self.assertRaisesRegex(SourceActionRequired, "CAPTCHA or security verification"):
            CNKIAdapter.detect_interruption(
                fixture("cnki_security_challenge.html"),
                url="https://kns.cnki.net/security-check",
            )


class CNKIAccessSettlingTests(unittest.IsolatedAsyncioTestCase):
    class _HTMLBrowser:
        navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

        def __init__(self, observations: list[str]) -> None:
            self.observations = observations
            self.observe_count = 0
            self.commands = []
            self.session = SessionHandle("html-access-settling-test")
            self.page_handle = PageHandle("main", session=self.session)

        async def execute(self, command):
            self.commands.append(command)
            if not isinstance(command, ObserveCommand):
                raise AssertionError(type(command).__name__)
            index = min(self.observe_count, len(self.observations) - 1)
            self.observe_count += 1
            return BrowserObservation(
                session=self.session,
                page=self.page_handle,
                generation=self.observe_count,
                url=ARTICLE_URL,
                title="CNKI article",
                html=self.observations[index],
            )

    @staticmethod
    def _settling_html() -> str:
        return """
        <html><body>
          <article><h1>企业数字化转型与资本市场表现——来自股票流动性的经验证据</h1></article>
          <a id="pdfDown" href="javascript:void(0)">PDF下载</a>
        </body></html>
        """

    async def test_access_reobserves_ambiguous_control_until_authenticated_order_action_appears(self) -> None:
        settling_html = self._settling_html()
        initial = CNKIAdapter.check_fulltext_access_html(
            settling_html,
            source_url=ARTICLE_URL,
        )
        self.assertEqual(initial.access_type, AccessType.UNKNOWN)
        self.assertEqual(initial.download_url, UNKNOWN)
        self.assertEqual(initial.download_locator, "PDF下载")
        self.assertIn("FULLTEXT_ACCESS_UNKNOWN", initial.reason)

        browser = self._HTMLBrowser(
            [settling_html, fixture("cnki_abstract_2026.html")]
        )
        with patch(
            "hunnu_harness.literature.adapters.cnki._ACCESS_SETTLE_DELAY_SECONDS",
            0,
        ):
            decision = await CNKIAdapter(browser).check_fulltext_access()

        self.assertTrue(decision.full_text_accessible)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)
        self.assertEqual(decision.status, RunStatus.SUCCESS)
        self.assertEqual(browser.observe_count, 2)

    async def test_access_does_not_retry_explicit_authorization_block(self) -> None:
        explicit_block = """
        <html><body>
          <article><h1>企业数字化转型与资本市场表现——来自股票流动性的经验证据</h1>
            <p>当前机构未获得全文访问权限</p>
          </article>
          <a id="pdfDown" href="https://bar.cnki.net/bar/download/order?id=denied">PDF下载</a>
        </body></html>
        """
        browser = self._HTMLBrowser(
            [explicit_block, fixture("cnki_abstract_2026.html")]
        )
        with patch(
            "hunnu_harness.literature.adapters.cnki._ACCESS_SETTLE_DELAY_SECONDS",
            0,
        ):
            decision = await CNKIAdapter(browser).check_fulltext_access()

        self.assertFalse(decision.full_text_accessible)
        self.assertFalse(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.METADATA_ONLY)
        self.assertEqual(decision.status, RunStatus.FULLTEXT_NOT_AUTHORIZED)
        self.assertNotEqual(decision.download_url, UNKNOWN)
        self.assertIn("explicit full-text authorization block", decision.reason)
        self.assertEqual(browser.observe_count, 1)

    async def test_access_returns_ambiguous_decision_after_bounded_observations(self) -> None:
        browser = self._HTMLBrowser([self._settling_html()])
        with patch(
            "hunnu_harness.literature.adapters.cnki._ACCESS_SETTLE_DELAY_SECONDS",
            0,
        ):
            decision = await CNKIAdapter(browser).check_fulltext_access()

        self.assertFalse(decision.full_text_accessible)
        self.assertEqual(decision.access_type, AccessType.UNKNOWN)
        self.assertIn("FULLTEXT_ACCESS_UNKNOWN", decision.reason)
        self.assertEqual(browser.observe_count, _ACCESS_SETTLE_MAX_OBSERVATIONS)


class CNKISearchSettlingTests(unittest.IsolatedAsyncioTestCase):
    class _HTMLBrowser:
        navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

        def __init__(self, observations: list[str]) -> None:
            self.observations = observations
            self.observe_count = 0
            self.commands = []
            self.current_url = "about:blank"
            self.session = SessionHandle("html-search-settling-test")
            self.page_handle = PageHandle("main", session=self.session)

        async def execute(self, command):
            self.commands.append(command)
            if isinstance(command, NavigateCommand):
                self.current_url = command.url
                return self._observation(self.observations[0])
            if isinstance(command, ObserveCommand):
                index = min(self.observe_count, len(self.observations) - 1)
                self.observe_count += 1
                return self._observation(self.observations[index])
            if isinstance(command, ClickCommand):
                return self._observation(self.observations[-1])
            raise AssertionError(type(command).__name__)

        def _observation(self, html: str) -> BrowserObservation:
            return BrowserObservation(
                session=self.session,
                page=self.page_handle,
                generation=self.observe_count,
                url=self.current_url,
                title="检索-中国知网",
                html=html,
            )

    @staticmethod
    def _request(title: str) -> LiteratureSearchRequest:
        return LiteratureSearchRequest(
            original_research_request="CNKI dynamic HTML search result",
            exact_titles=(title,),
            max_search_results=1,
            max_results_per_source=1,
            max_downloads=0,
            max_downloads_per_run=0,
        )

    async def test_html_search_reobserves_once_when_initial_page_has_no_terminal_marker(self) -> None:
        title = "人工智能漂洗、审计监督与盈余管理"
        transient = """
        <html><head><title>检索-中国知网</title></head><body>
          <main><section aria-label="检索结果">检索结果正在加载</section></main>
        </body></html>
        """
        browser = self._HTMLBrowser([transient, fixture("cnki_search.html")])
        with patch(
            "hunnu_harness.literature.adapters.cnki._SEARCH_SETTLE_DELAY_SECONDS",
            0,
        ):
            records = await CNKIAdapter(browser).search(f'"{title}"', self._request(title))
        self.assertEqual([record.title for record in records], [title])
        self.assertEqual(browser.observe_count, 2)

    async def test_html_search_can_settle_on_third_bounded_observation(self) -> None:
        title = "人工智能漂洗、审计监督与盈余管理"
        transient = """
        <html><head><title>检索-中国知网</title></head><body>
          <main><section aria-label="检索结果">检索结果正在加载</section></main>
        </body></html>
        """
        browser = self._HTMLBrowser([transient, transient, fixture("cnki_search.html")])
        with patch(
            "hunnu_harness.literature.adapters.cnki._SEARCH_SETTLE_DELAY_SECONDS",
            0,
        ):
            records = await CNKIAdapter(browser).search(f'"{title}"', self._request(title))
        self.assertEqual([record.title for record in records], [title])
        self.assertEqual(browser.observe_count, 3)

    async def test_html_search_does_not_retry_an_explicit_no_results_state(self) -> None:
        title = "不存在的精确论文标题"
        no_results = """
        <html><head><title>检索-中国知网</title></head><body>
          <main><section aria-label="检索结果">抱歉，未找到相关结果</section></main>
        </body></html>
        """
        browser = self._HTMLBrowser([no_results, fixture("cnki_search.html")])
        records = await CNKIAdapter(browser).search(f'"{title}"', self._request(title))
        self.assertEqual(records, [])
        self.assertEqual(browser.observe_count, 1)

    async def test_html_search_does_not_retry_after_an_explicit_result_count(self) -> None:
        title = "人工智能漂洗、审计监督与盈余管理"
        counted_results = """
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 1 条结果</div>
            <a href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=OTHER2026001">其他论文</a>
          </main>
        </body></html>
        """
        browser = self._HTMLBrowser([counted_results, fixture("cnki_search.html")])
        records = await CNKIAdapter(browser).search(f'"{title}"', self._request(title))
        self.assertEqual(records, [])
        self.assertEqual(browser.observe_count, 1)

    async def test_exact_title_checks_bounded_thirty_results_before_filtering(self) -> None:
        title = "人工智能技术应用如何影响企业创新"
        distractors = "\n".join(
            f'<a class="fz14" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=DISTRACTOR{i:03d}">相似题名{i}</a>'
            for i in range(1, 15)
        )
        counted_results = f"""
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 18 条结果</div>
            {distractors}
            <a class="fz14" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=GGYY202410009">{title}</a>
          </main>
        </body></html>
        """
        browser = self._HTMLBrowser([counted_results])
        records = await CNKIAdapter(browser).search(f'"{title}"', self._request(title))
        self.assertEqual([record.title for record in records], [title])
        self.assertEqual(browser.observe_count, 1)

    async def test_exact_title_accepts_single_cnki_subtitle_punctuation_variant(self) -> None:
        requested = "最低工资与异质性人力资本需求——基于招聘网站数据的研究"
        observed = "最低工资与异质性人力资本需求 : 基于招聘网站数据的研究"
        counted_results = f"""
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 1 条结果</div>
            <a href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=SJJJ202312003">{observed}</a>
          </main>
        </body></html>
        """
        browser = self._HTMLBrowser([counted_results])
        records = await CNKIAdapter(browser).search(f'"{requested}"', self._request(requested))
        self.assertEqual(
            [record.title for record in records],
            ["最低工资与异质性人力资本需求:基于招聘网站数据的研究"],
        )
        self.assertEqual(browser.observe_count, 1)

    async def test_exact_title_refresh_relocks_target_after_first_ten_results(self) -> None:
        title = "人工智能技术应用如何影响企业创新"
        distractors = "\n".join(
            f'<a class="fz14" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=DISTRACTOR{i:03d}">相似题名{i}</a>'
            for i in range(1, 15)
        )
        counted_results = f"""
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 18 条结果</div>
            {distractors}
            <a class="fz14" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=GGYY202410009">{title}</a>
          </main>
        </body></html>
        """
        browser = self._HTMLBrowser([counted_results])
        adapter = CNKIAdapter(browser)
        records = await adapter.search(f'"{title}"', self._request(title))
        await adapter.open_result(records[0])
        click = next(command for command in reversed(browser.commands) if isinstance(command, ClickCommand))
        self.assertEqual(click.target.text, title)

    async def test_exact_title_click_falls_back_to_fresh_detail_url_when_click_stays_on_results(self) -> None:
        title = "最低工资与异质性人力资本需求——基于招聘网站数据的研究"
        html = f"""
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 1 条结果</div>
            <a href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=SJJJ202312003">{title}</a>
          </main>
        </body></html>
        """
        browser = self._HTMLBrowser([html])
        expected = CNKIAdapter.parse_search_results_html(html, query=title, max_results=1)[0]
        await CNKIAdapter(browser).open_result(expected)
        navigations = [command.url for command in browser.commands if isinstance(command, NavigateCommand)]
        self.assertEqual(len(navigations), 2)
        self.assertIn("/kcms2/article/abstract?", navigations[-1])

    async def test_html_search_retry_remains_bounded_when_page_never_settles(self) -> None:
        title = "不存在的精确论文标题"
        transient = """
        <html><head><title>检索-中国知网</title></head><body>
          <main><section aria-label="检索结果">检索结果正在加载</section></main>
        </body></html>
        """
        browser = self._HTMLBrowser([transient, transient, transient, fixture("cnki_search.html")])
        with patch(
            "hunnu_harness.literature.adapters.cnki._SEARCH_SETTLE_DELAY_SECONDS",
            0,
        ):
            with self.assertRaisesRegex(SourceUnavailable, "bounded observation"):
                await CNKIAdapter(browser).search(f'"{title}"', self._request(title))
        self.assertEqual(browser.observe_count, 3)

    async def test_exact_title_refresh_uses_the_same_bounded_settling(self) -> None:
        title = "人工智能漂洗、审计监督与盈余管理"
        transient = """
        <html><head><title>检索-中国知网</title></head><body>
          <main><section aria-label="检索结果">检索结果正在加载</section></main>
        </body></html>
        """
        expected = CNKIAdapter.parse_search_results_html(
            fixture("cnki_search.html"),
            query=title,
            max_results=1,
        )[0]
        browser = self._HTMLBrowser([transient, transient, fixture("cnki_search.html")])
        with patch(
            "hunnu_harness.literature.adapters.cnki._SEARCH_SETTLE_DELAY_SECONDS",
            0,
        ):
            await CNKIAdapter(browser).open_result(expected)
        self.assertEqual(browser.observe_count, 3)
        self.assertTrue(any(isinstance(command, ClickCommand) for command in browser.commands))

    async def test_exact_title_click_retries_transient_missing_find_ref(self) -> None:
        title = "社会信用环境改善降低了企业违规吗?——来自“中国社会信用体系建设”的证据"
        html = f"""
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 1 条结果</div>
            <a href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=JRYJ202301001">{title}</a>
          </main>
        </body></html>
        """

        class _TransientClickBrowser(self._HTMLBrowser):
            def __init__(self, observations: list[str]) -> None:
                super().__init__(observations)
                self.click_attempts = 0

            async def execute(self, command):
                if isinstance(command, ClickCommand):
                    self.click_attempts += 1
                    if self.click_attempts == 1:
                        raise BrowserCommandError(
                            "MCP browser_find returned no executable snapshot ref"
                        )
                return await super().execute(command)

        expected = CNKIAdapter.parse_search_results_html(html, query=title, max_results=1)[0]
        browser = _TransientClickBrowser([html])
        adapter = CNKIAdapter(browser)
        with patch(
            "hunnu_harness.literature.adapters.cnki._CNKI_CLICK_RETRY_DELAY_SECONDS",
            0,
        ):
            await adapter.open_result(expected)
        self.assertEqual(browser.click_attempts, 2)

    class _RoutedBrowser(_HTMLBrowser):
        """Serve CNKI result pages by search field (korder), one page per navigation."""

        def __init__(self, pages_by_order: dict[str, list[str]]) -> None:
            super().__init__([""])
            self.pages_by_order = {order: list(pages) for order, pages in pages_by_order.items()}
            self.current_html = ""

        async def execute(self, command):
            from urllib.parse import parse_qs, urlsplit

            self.commands.append(command)
            if isinstance(command, NavigateCommand):
                self.current_url = command.url
                order = parse_qs(urlsplit(command.url).query).get("korder", [""])[0]
                pages = self.pages_by_order.get(order)
                if pages:
                    self.current_html = pages.pop(0) if len(pages) > 1 else pages[0]
                return self._observation(self.current_html)
            if isinstance(command, ObserveCommand):
                self.observe_count += 1
                return self._observation(self.current_html)
            if isinstance(command, ClickCommand):
                return self._observation(self.current_html)
            raise AssertionError(type(command).__name__)

    _PLUS_TITLE = "“互联网+”为什么加出了业绩"
    _NO_HITS = "<html><head><title>检索-中国知网</title></head><body><main><div>共找到 0 条结果</div></main></body></html>"

    @classmethod
    def _one_hit(cls, title: str) -> str:
        return f"""
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 1 条结果</div>
            <a class="fz14" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=GGYY201805005">{title}</a>
          </main>
        </body></html>
        """

    def _plus_request(self) -> LiteratureSearchRequest:
        return LiteratureSearchRequest(
            original_research_request="CNKI title unreachable by exact-title search",
            exact_titles=(self._PLUS_TITLE,),
            keywords_cn=("为什么加出了业绩",),
            max_search_results=1,
            max_results_per_source=1,
            max_downloads=0,
            max_downloads_per_run=0,
        )

    def _korders(self, browser) -> list[str]:
        from urllib.parse import parse_qs, urlsplit

        return [
            parse_qs(urlsplit(command.url).query).get("korder", [""])[0]
            for command in browser.commands
            if isinstance(command, NavigateCommand) and "korder=" in command.url
        ]

    async def test_refresh_falls_back_to_the_search_that_found_a_title_exact_search_cannot_reach(self) -> None:
        # If this fails, a paper that CNKI's keyword search finds but its exact-title
        # search cannot (nested quotes around "+") is again undownloadable.
        browser = self._RoutedBrowser({"SU": [self._one_hit(self._PLUS_TITLE)], "TI": [self._NO_HITS]})
        adapter = CNKIAdapter(browser)
        records = await adapter.search("为什么加出了业绩", self._plus_request())
        self.assertEqual([record.title for record in records], [self._PLUS_TITLE])
        await adapter.open_result(records[0])
        self.assertEqual(self._korders(browser), ["SU", "TI", "SU"])
        # Followed in-page, never clicked: a click here once left two tabs on the
        # same article and the page-identity gate stopped the download.
        self.assertFalse(any(isinstance(command, ClickCommand) for command in browser.commands))
        last_navigation = [command.url for command in browser.commands if isinstance(command, NavigateCommand)][-1]
        self.assertIn("/kcms2/article/abstract?", last_navigation)
        self.assertIn("GGYY201805005", last_navigation)

    async def test_refresh_fallback_never_applies_to_a_record_found_by_exact_title(self) -> None:
        # If this fails, the fallback widened the exact-title path it was meant to leave alone.
        from hunnu_harness.literature.adapters.base import SourceLayoutChanged

        title = "人工智能技术应用如何影响企业创新"
        browser = self._RoutedBrowser({"TI": [self._one_hit(title), self._NO_HITS]})
        adapter = CNKIAdapter(browser)
        records = await adapter.search(f'"{title}"', self._request(title))
        with self.assertRaisesRegex(SourceLayoutChanged, "could not relock"):
            await adapter.open_result(records[0])
        self.assertEqual(self._korders(browser), ["TI", "TI"])

    async def test_refresh_fallback_still_requires_the_same_identity(self) -> None:
        # If this fails, the fallback relocked onto a different paper than the one screened.
        from hunnu_harness.literature.adapters.base import SourceLayoutChanged

        browser = self._RoutedBrowser({
            "SU": [self._one_hit(self._PLUS_TITLE), self._one_hit("“互联网+”为什么加出了业绩——另一篇")],
            "TI": [self._NO_HITS],
        })
        adapter = CNKIAdapter(browser)
        records = await adapter.search("为什么加出了业绩", self._plus_request())
        with self.assertRaisesRegex(SourceLayoutChanged, "could not relock"):
            await adapter.open_result(records[0])
        self.assertEqual(self._korders(browser), ["SU", "TI", "SU"])

    async def test_a_planned_chinese_keyword_pair_reaches_cnki_as_both_words(self) -> None:
        # If this fails, the planner's "primary secondary" pairing is sent as
        # "primary+secondary" again, which CNKI runs as either word, not both.
        request = LiteratureSearchRequest(
            original_research_request="CNKI keyword pairing",
            keywords_cn=("城市轨道交通", "客流"),
            max_search_results=1,
            max_results_per_source=1,
            max_downloads=0,
            max_downloads_per_run=0,
        )
        planned = LiteratureSearchPlanner().plan(request)
        self.assertEqual([item.query for item in planned], ["城市轨道交通 客流"])
        browser = self._RoutedBrowser({"SU": [self._one_hit("城市轨道交通客流的时空分布研究")]})
        await CNKIAdapter(browser).search(planned[0].query, request)
        navigations = [command.url for command in browser.commands if isinstance(command, NavigateCommand)]
        self.assertEqual(self._korders(browser), ["SU"])
        self.assertEqual(kw_as_cnki_reads_it(navigations[0]), "城市轨道交通 客流")

    async def test_a_relock_refresh_repeats_a_spaced_keyword_search_byte_for_byte(self) -> None:
        # If this fails, the relock fallback no longer sends the search that found the
        # record, and a paper found by a spaced keyword search can no longer be relocked.
        title = "高速铁路、区域可达性与城市扩张"
        request = LiteratureSearchRequest(
            original_research_request="CNKI keyword relock",
            keywords_cn=("高速铁路", "区域可达性"),
            max_search_results=1,
            max_results_per_source=1,
            max_downloads=0,
            max_downloads_per_run=0,
        )
        browser = self._RoutedBrowser({"SU": [self._one_hit(title)], "TI": [self._NO_HITS]})
        adapter = CNKIAdapter(browser)
        records = await adapter.search("高速铁路 区域可达性", request)
        await adapter.open_result(records[0])
        self.assertEqual(self._korders(browser), ["SU", "TI", "SU"])
        searches = [
            command.url
            for command in browser.commands
            if isinstance(command, NavigateCommand) and "korder=SU" in command.url
        ]
        self.assertEqual(searches[0], searches[1])
        self.assertEqual(kw_as_cnki_reads_it(searches[1]), "高速铁路 区域可达性")

    async def test_an_exact_title_relock_sends_a_remaining_space_as_a_space(self) -> None:
        # If this fails, opening a record found by exact title refreshes it with "+" for a
        # space, and CNKI searches the title as OR at the moment its identity is relocked.
        title = "北部湾海绵共附生真菌Cladosporium sp. SCSIO 41415次级代谢产物研究"
        page = f"""
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 1 条结果</div>
            <a class="fz14" target="_blank"
               href="/kcms2/article/abstract?dbcode=CMFD&amp;filename=1025123456.nh">{title}</a>
          </main>
        </body></html>
        """
        browser = self._RoutedBrowser({"TI": [page]})
        adapter = CNKIAdapter(browser)
        records = await adapter.search(f'"{title}"', self._request(title))
        await adapter.open_result(records[0])
        self.assertEqual(self._korders(browser), ["TI", "TI"])
        searches = [
            command.url
            for command in browser.commands
            if isinstance(command, NavigateCommand) and "korder=TI" in command.url
        ]
        self.assertEqual([kw_as_cnki_reads_it(url) for url in searches], [title, title])

    async def test_an_exact_title_with_a_hyphen_is_searched_and_relocked_without_a_not(self) -> None:
        # If this fails, acquire / acquire-batch or the relock before download again send CNKI
        # "基于全二维气相色谱" NOT "飞行时间质谱的...": on 2026-08-20 that search left out this very
        # paper although it was newer than every row CNKI showed.  The result rows are still
        # read, filtered and clicked by the real title, hyphen included.
        title = "基于全二维气相色谱-飞行时间质谱的不同质量等级浓酱兼香型白酒挥发性风味物质差异分析"
        neighbour = "基于全二维气相色谱-飞行时间质谱联用技术的聚乙烯热解油分子结构表征"
        page = f"""
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 2 条结果</div>
            <a class="fz14" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=SYLH202606011">{neighbour}</a>
            <a class="fz14" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=ZGNZ202607012">{title}</a>
          </main>
        </body></html>
        """
        browser = self._RoutedBrowser({"TI": [page]})
        adapter = CNKIAdapter(browser)
        records = await adapter.search(f'"{title}"', self._request(title))
        self.assertEqual([record.title for record in records], [title])
        with patch("hunnu_harness.literature.adapters.cnki._CNKI_CLICK_RETRY_DELAY_SECONDS", 0):
            await adapter.open_result(records[0])
        self.assertEqual(self._korders(browser), ["TI", "TI"])
        searches = [
            command.url
            for command in browser.commands
            if isinstance(command, NavigateCommand) and "korder=TI" in command.url
        ]
        sent = "基于全二维气相色谱 飞行时间质谱的不同质量等级浓酱兼香型白酒挥发性风味物质差异分析"
        self.assertEqual([kw_as_cnki_reads_it(url) for url in searches], [sent, sent])
        click = next(command for command in reversed(browser.commands) if isinstance(command, ClickCommand))
        self.assertEqual(click.target.text, title)


class CNKIStructuredFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_snapshot_fallback_ignores_proven_offscreen_challenge(self) -> None:
        class _StructuredBrowser:
            navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

            def __init__(self) -> None:
                self.current_url = "about:blank"
                self.navigated: list[str] = []
                self.commands = []
                self.downloads_dir = None
                self.session = SessionHandle("snapshot-test")
                self.page_handle = PageHandle("main", session=self.session)

            def observation(self) -> BrowserObservation:
                return BrowserObservation(
                    session=self.session,
                    page=self.page_handle,
                    generation=0,
                    url=self.current_url,
                    title="检索-中国知网",
                    structured_content=fixture("cnki_search_snapshot.yml"),
                    target_observations=(
                        BrowserTargetObservation(
                            marker="拖动下方拼图完成验证",
                            bounding_box={"x": 15, "y": -999985, "width": 188, "height": 18},
                            client_rect={"x": 15, "y": -999985, "width": 188, "height": 18},
                            client_width=188,
                            client_height=18,
                            viewport_width=1_000_000,
                            viewport_height=1_000_000,
                            frame_viewport_visible=True,
                            inspection_complete=True,
                        ),
                    ),
                )

            async def execute(self, command):
                self.commands.append(command)
                if isinstance(command, NavigateCommand):
                    self.current_url = command.url
                    self.navigated.append(command.url)
                    return self.observation()
                if isinstance(command, ObserveCommand) and command.include_html:
                    raise ObservationUnavailable("structured snapshot only")
                if isinstance(command, ObserveCommand):
                    return self.observation()
                if isinstance(command, ClickCommand):
                    return self.observation()
                raise AssertionError(type(command).__name__)

        browser = _StructuredBrowser()
        adapter = CNKIAdapter(browser)
        request = LiteratureSearchRequest(
            original_research_request="CNKI structured snapshot fallback",
            exact_titles=("投贷联动、媒体监督与科技企业AI漂洗",),
            max_search_results=2,
            max_results_per_source=2,
            max_downloads=0,
            max_downloads_per_run=0,
        )
        records = await adapter.search('"投贷联动、媒体监督与科技企业AI漂洗"', request)
        self.assertEqual(len(records), 1)
        self.assertIn("korder=TI", browser.navigated[0])
        self.assertNotIn("%22", browser.navigated[0])
        self.assertIsNotNone(adapter.last_challenge_diagnostic)
        self.assertEqual(adapter.last_challenge_diagnostic.state, ChallengeState.DORMANT)
        await adapter.open_result(records[0])
        click = next(command for command in reversed(browser.commands) if isinstance(command, ClickCommand))
        self.assertTrue(click.follow_new_page)
        self.assertTrue(click.close_origin_when_sole_page)
        self.assertEqual(click.target.text, records[0].title)

    async def test_download_uses_exact_label_without_unsupported_css_text_combo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-cnki-download-target-") as temporary:
            root = Path(temporary)
            downloaded = write_minimal_pdf(root / "authorized.pdf")
            snapshot = fixture("cnki_article_snapshot.yml")

            class _DownloadBrowser:
                navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

                def __init__(self) -> None:
                    self.commands = []
                    self.downloads_dir = root
                    self.session = SessionHandle("download-target-test")
                    self.page_handle = PageHandle("main", session=self.session)

                async def execute(self, command):
                    self.commands.append(command)
                    if isinstance(command, ObserveCommand) and command.include_html:
                        raise ObservationUnavailable("structured snapshot only")
                    if isinstance(command, ObserveCommand):
                        return BrowserObservation(
                            session=self.session,
                            page=self.page_handle,
                            generation=0,
                            url=ARTICLE_URL,
                            title="投贷联动、媒体监督与科技企业AI漂洗 - 中国知网",
                            structured_content=snapshot,
                            target_observations=(
                                BrowserTargetObservation(
                                    marker="拖动下方拼图完成验证",
                                    bounding_box={"x": 15, "y": -999985, "width": 188, "height": 18},
                                    client_rect={"x": 15, "y": -999985, "width": 188, "height": 18},
                                    client_width=188,
                                    client_height=18,
                                    viewport_width=1_000_000,
                                    viewport_height=1_000_000,
                                    frame_viewport_visible=True,
                                    inspection_complete=True,
                                ),
                            ),
                        )
                    if isinstance(command, DownloadCommand):
                        return DownloadArtifact.from_path(
                            downloaded,
                            suggested_filename=command.suggested_filename,
                            page=self.page_handle,
                        )
                    raise AssertionError(type(command).__name__)

            browser = _DownloadBrowser()
            adapter = CNKIAdapter(browser)
            record = CNKIAdapter.parse_article_snapshot(snapshot, source_url=ARTICLE_URL)
            access = CNKIAdapter.check_fulltext_access_snapshot(snapshot, source_url=ARTICLE_URL)
            with isolated_fetch_ledger() as ledger:
                adapter.fetch_ledger = ledger
                path = await adapter.download_fulltext(record, access)
            self.assertEqual(path, downloaded.resolve())
            command = next(item for item in browser.commands if isinstance(item, DownloadCommand))
            self.assertIsNone(command.target.css)
            self.assertEqual(command.target.text, "PDF下载")
            self.assertTrue(command.target.exact_text)
            self.assertEqual(command.identity_labels, (record.title,))

    async def test_failed_pdf_download_does_not_blindly_retry_caj(self) -> None:
        snapshot = fixture("cnki_article_snapshot.yml")

        class _FailingDownloadBrowser:
            navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

            def __init__(self) -> None:
                self.commands = []
                self.downloads_dir = None
                self.session = SessionHandle("download-failure-test")
                self.page_handle = PageHandle("main", session=self.session)

            async def execute(self, command):
                self.commands.append(command)
                if isinstance(command, ObserveCommand) and command.include_html:
                    raise ObservationUnavailable("structured snapshot only")
                if isinstance(command, ObserveCommand):
                    return BrowserObservation(
                        session=self.session,
                        page=self.page_handle,
                        generation=0,
                        url=ARTICLE_URL,
                        title="投贷联动、媒体监督与科技企业AI漂洗 - 中国知网",
                        structured_content=snapshot,
                    )
                if isinstance(command, DownloadCommand):
                    raise DownloadFailure("download result is uncertain")
                raise AssertionError(type(command).__name__)

        browser = _FailingDownloadBrowser()
        adapter = CNKIAdapter(browser)
        record = CNKIAdapter.parse_article_snapshot(snapshot, source_url=ARTICLE_URL)
        access = CNKIAdapter.check_fulltext_access_snapshot(snapshot, source_url=ARTICLE_URL)
        with isolated_fetch_ledger() as ledger:
            adapter.fetch_ledger = ledger
            with self.assertRaises(SourceUnavailable) as caught:
                await adapter.download_fulltext(record, access)

        self.assertIn(
            "DownloadFailure: download result is uncertain",
            str(caught.exception),
        )

        downloads = [item for item in browser.commands if isinstance(item, DownloadCommand)]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0].target.text, "PDF下载")
        self.assertEqual(downloads[0].suggested_filename, f"{record.paper_id}.pdf")


class CNKIDownloadAndManifestTests(unittest.TestCase):
    def _manager(self, root: Path) -> LiteratureDownloadManager:
        return LiteratureDownloadManager(
            root / "downloads", allow_outside_project_for_tests=True, make_archive_read_only=False
        )

    def _record(self) -> LiteratureRecord:
        return LiteratureRecord(
            paper_id="LIT-CNKI-LOCKED",
            title="人工智能漂洗、审计监督与盈余管理",
            authors=("张三", "李四"),
            year="2026",
            doi="10.1234/cnki.test.2026.001",
            source_database="CNKI",
            stable_identifier="cjfd:KJYJ202601001",
            canonical_paper_id="LIT-CNKI-LOCKED",
            target_identity_confirmed=True,
        )

    def test_cnki_pdf_download_metadata_uses_existing_pdf_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "原始下载.pdf")
            entry = self._manager(root).archive_authorized_fulltext(
                source, self._record(), authorized(FullTextFormat.PDF)
            )
            self.assertEqual(entry.full_text_format, "PDF")
            self.assertTrue(entry.pdf_validation_passed)
            self.assertTrue(entry.file_validation_passed)
            self.assertTrue(entry.target_identity_confirmed)
            self.assertEqual(entry.sha256, sha256_file(source))

    def test_cnki_caj_is_preserved_validated_hashed_and_not_converted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "原始下载.caj"
            source.write_bytes(b"CAJViewer 7.0\x00authorized fixture content")
            entry = self._manager(root).archive_authorized_fulltext(
                source, self._record(), authorized(FullTextFormat.CAJ)
            )
            self.assertEqual(entry.full_text_format, "CAJ")
            self.assertFalse(entry.pdf_validation_passed)
            self.assertTrue(entry.file_validation_passed)
            self.assertTrue(Path(entry.local_path).name.endswith(".caj"))
            self.assertEqual(entry.sha256, sha256_file(source))

    def test_html_error_page_named_pdf_or_caj_is_rejected(self) -> None:
        for name, full_text_format in (("error.pdf", FullTextFormat.PDF), ("error.caj", FullTextFormat.CAJ)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / name
                source.write_text("<!doctype html><html><body>请登录</body></html>", encoding="utf-8")
                with self.assertRaisesRegex(InvalidFullTextDownload, "DownloadedHTMLInsteadOfFullText"):
                    self._manager(root).archive_authorized_fulltext(
                        source, self._record(), authorized(full_text_format)
                    )

    def test_cnki_manifest_contains_format_identity_validation_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "paper.caj"
            source.write_bytes(b"CAJViewer\x00fixture")
            manager = self._manager(root)
            entry = manager.archive_authorized_fulltext(source, self._record(), authorized(FullTextFormat.CAJ))
            writer = LiteratureArtifactWriter(root / "run", allow_outside_project_for_tests=True)
            manifest_path = writer.write_download_manifest([entry])
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = payload["Downloads"][0]
            self.assertEqual(item["PaperID"], "LIT-CNKI-LOCKED")
            self.assertEqual(item["FullTextFormat"], "CAJ")
            self.assertTrue(item["AuthorizedAccess"])
            self.assertTrue(item["FileValidationPassed"])
            self.assertTrue(item["TargetIdentityConfirmed"])
            self.assertEqual(item["SHA256"], sha256_file(source))

    def test_confirmed_authenticated_browser_capture_is_validated_and_manifested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "投贷联动、媒体监督与科技企业AI漂洗.pdf")
            request = LiteratureSearchRequest(
                original_research_request="在 CNKI 精确检索目标论文并下载一篇有权访问的全文",
                research_question="CNKI adapter acceptance",
                exact_titles=("投贷联动、媒体监督与科技企业AI漂洗",),
                max_search_results=1,
                max_downloads=1,
                max_results_per_source=1,
                max_downloads_per_run=1,
                require_full_text=True,
            )
            result = finalize_captured_cnki_acceptance(
                request=request,
                query="投贷联动、媒体监督与科技企业AI漂洗",
                search_html=fixture("cnki_search_live_structure.html"),
                article_html=fixture("cnki_article_live_structure.html"),
                article_url=(
                    "https://kns.cnki.net/kcms2/article/abstract?"
                    "dbcode=CJFQ&filename=CJKX202602009"
                ),
                downloaded_fulltext=source,
                run_root=root / "run",
                authorized_browser_download_confirmed=True,
                allow_outside_project_for_tests=True,
            )
            self.assertEqual(result.status, RunStatus.SUCCESS)
            self.assertEqual(len(result.downloads), 1)
            self.assertTrue(result.downloads[0].authorized_access)
            self.assertTrue(result.downloads[0].file_validation_passed)
            self.assertTrue(result.downloads[0].target_identity_confirmed)

    def test_unconfirmed_capture_does_not_promote_metadata_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "target.pdf")
            request = LiteratureSearchRequest(
                original_research_request="CNKI metadata-only fixture",
                exact_titles=("投贷联动、媒体监督与科技企业AI漂洗",),
                max_search_results=1,
                max_downloads=1,
                max_results_per_source=1,
                max_downloads_per_run=1,
                require_full_text=True,
            )
            result = finalize_captured_cnki_acceptance(
                request=request,
                query="投贷联动、媒体监督与科技企业AI漂洗",
                search_html=fixture("cnki_search_live_structure.html"),
                article_html=fixture("cnki_article_live_structure.html"),
                article_url=(
                    "https://kns.cnki.net/kcms2/article/abstract?"
                    "dbcode=CJFQ&filename=CJKX202602009"
                ),
                downloaded_fulltext=source,
                run_root=root / "run",
                allow_outside_project_for_tests=True,
            )
            self.assertEqual(result.status, RunStatus.DOWNLOAD_FAILED)
            self.assertFalse(result.downloads)

    def test_chinese_search_results_csv_round_trips_utf8_without_column_shift(self) -> None:
        records = CNKIAdapter.parse_search_results_html(fixture("cnki_search.html"), query="人工智能漂洗")
        with tempfile.TemporaryDirectory() as tmp:
            writer = LiteratureArtifactWriter(Path(tmp) / "run", allow_outside_project_for_tests=True)
            path = writer.write_search_results(records)
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                fields = tuple(reader.fieldnames or ())
            self.assertEqual(rows[0]["Title"], "人工智能漂洗、审计监督与盈余管理")
            self.assertEqual(rows[0]["Source"], "CNKI")
            self.assertEqual(set(rows[0]), set(fields))

    def test_cnki_records_use_existing_deduplication_rules(self) -> None:
        first = self._record()
        second = self._record()
        second.paper_id = "SECOND"
        LiteratureDeduplicator().deduplicate([first, second])
        self.assertFalse(first.duplicate_detected)
        self.assertTrue(second.duplicate_detected)
        self.assertIn("DOI exact match", second.duplicate_reason)

    def test_existing_adapters_remain_pdf_by_default(self) -> None:
        sd = ScienceDirectAdapter.check_fulltext_access_html(
            fixture("sciencedirect_article_authorized.html"),
            source_url="https://www.sciencedirect.com/science/article/pii/S1544612326004149",
        )
        springer = SpringerLinkAdapter.check_fulltext_access_html(
            fixture("springerlink_article_open_access.html"),
            source_url="https://link.springer.com/article/10.1007/s11573-023-01162-8",
        )
        self.assertEqual(sd.full_text_format, FullTextFormat.PDF)
        self.assertEqual(springer.full_text_format, FullTextFormat.PDF)


if __name__ == "__main__":
    unittest.main()
