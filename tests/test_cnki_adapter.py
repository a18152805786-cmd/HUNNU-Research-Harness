from __future__ import annotations

import csv
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

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
from hunnu_harness.literature.adapters.base import (
    SourceActionRequired,
    SourceLayoutChanged,
    SourceUnavailable,
)
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
    ScreeningDecision,
    UNKNOWN,
)
from hunnu_harness.literature.normalization import normalize_title, sha256_file
from hunnu_harness.literature.workflow import (
    LiteratureAcquisitionWorkflow,
    _is_fulltext_acquisition_candidate,
    _search_detail_title_key,
    finalize_captured_cnki_acceptance,
)

from literature_test_support import isolated_fetch_ledger, write_minimal_pdf


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
ARTICLE_URL = (
    "https://kns.cnki.net/kcms2/article/abstract?dbcode=CJFD&filename=KJYJ202601001"
    "&uniplatform=NZKPT&language=CHS"
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


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
        self.assertNotIn("+%E2%80%94", url)
        self.assertEqual(_canonicalize_cnki_title_identity("AI and firms"), "ai and firms")
        self.assertIn("%3F+Evidence", CNKIAdapter.build_search_url("Does AI matter? Evidence", mode="exact_title"))

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


ONLINE_FIRST_TITLE = "城市轨道交通网络规划的示例研究"
NEWSPAPER_TITLE = "示例城市推进社区体育设施建设"


class CNKIOnlineFirstAndNewspaperPageTests(unittest.IsolatedAsyncioTestCase):
    """Online-first (CAPJ) and newspaper (CCND) pages carry no issue citation.

    Their source is the first link in ``.top-tip`` and their date sits under a
    label of its own (网络首发时间 / 报纸日期).  On the saved 2026-08 CNKI pages,
    every online-first and newspaper page paired by title with its own result
    row agreed with that row's 来源 and 发表时间.
    """

    DETAIL_URL = "https://kns.cnki.net/kcms2/article/abstract?v=fixture&uniplatform=NZKPT&language=CHS"
    NAME_LINK = (
        '<a target="_blank" href="https://navi.cnki.net/knavi/detail?p=fixture-journal&amp;uniplatform=NZKPT">'
        "示例管理评论 . </a>"
    )
    FIRST_PUBLISHED = "<span>（录用定稿）网络首发时间：2026-04-11 15:03:27</span>"

    @classmethod
    def parse(cls, html: str) -> LiteratureRecord:
        return CNKIAdapter.parse_article_html(html, source_url=cls.DETAIL_URL)

    @staticmethod
    def result_page(title: str, *, source: str, date: str, database: str) -> str:
        """One kns8s result row laid out as on the saved 2026 result pages."""

        return f"""
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 1 条结果</div>
            <table class="result-table-list"><tbody><tr>
              <td class="name"><a class="fz14 inline" target="_blank"
                href="/kcms2/article/abstract?v=fixture&amp;uniplatform=NZKPT&amp;language=CHS">{title}</a></td>
              <td class="source"><p><a target="_blank" href="https://navi.cnki.net/knavi/detail?p=fixture">{source}</a></p></td>
              <td class="date">{date}</td>
              <td class="data"><span>{database}</span></td>
            </tr></tbody></table>
          </main>
        </body></html>
        """

    def test_online_first_page_reads_journal_from_its_top_tip_link_and_year_from_its_first_published_time(self) -> None:
        # If this fails, online-first pages lose journal and year again: .top-tip
        # holds only "刊名 . " there, and the date is in .head-time.
        for stage in ("录用定稿", "排版定稿"):
            with self.subTest(stage=stage):
                record = self.parse(fixture("cnki_article_online_first_2026.html").replace("录用定稿", stage))
                self.assertEqual(record.title, ONLINE_FIRST_TITLE)
                self.assertEqual(record.journal, "示例管理评论")
                self.assertEqual(record.year, "2026")
                self.assertEqual(record.issue, UNKNOWN)
                self.assertEqual(record.pages_or_article_number, UNKNOWN)
                self.assertEqual(record.stable_identifier, "capj:FIXTURE20260409001")

    def test_an_online_first_article_stays_a_journal_article_whatever_the_navigation_menu_names(self) -> None:
        # If this fails, the body-text guess decides again: the menu on these
        # pages names 学位论文 and 报纸, and online-first articles were filed as
        # Dissertation.
        html = fixture("cnki_article_online_first_2026.html")
        self.assertIn("<li>学位论文</li>", html)
        record = self.parse(html)
        self.assertEqual(record.publication_type, "JournalArticle")
        self.assertEqual(record.publication_status, PublicationStatus.ONLINE_FIRST.value)

    def test_newspaper_page_reads_the_newspaper_and_the_year_of_its_newspaper_date(self) -> None:
        # If this fails, newspaper pages lose their source and date again, or the
        # level beside the name (地方级) leaks into it.
        record = self.parse(fixture("cnki_article_newspaper_2026.html"))
        self.assertEqual(record.title, NEWSPAPER_TITLE)
        self.assertEqual(record.journal, "示例日报")
        self.assertEqual(record.year, "2025")
        self.assertEqual(record.publication_type, "Newspaper")
        self.assertEqual(record.publication_status, PublicationStatus.OTHER.value)

    def test_the_platform_online_time_is_never_read_as_a_publication_year(self) -> None:
        # If this fails, 在线公开时间 -- which the page itself says "不代表文献的发表时间"
        # -- became a year: it dated the newspaper's 2025-12-31 issue by its
        # 2026-01-02 upload, or gave an undated page a date it never stated.
        self.assertEqual(self.parse(fixture("cnki_article_newspaper_2026.html")).year, "2025")
        undated = fixture("cnki_article_online_first_2026.html").replace(self.FIRST_PUBLISHED, "")
        self.assertIn("在线公开时间", undated)
        record = self.parse(undated)
        self.assertEqual(record.year, UNKNOWN)

    def test_the_top_tip_link_is_read_as_a_source_only_on_a_page_that_says_what_it_is(self) -> None:
        # If this fails, any page without an issue citation has its first .top-tip
        # link taken for a journal -- a dissertation's university or a
        # conference would then be filed as a JournalArticle's journal.
        undated = fixture("cnki_article_online_first_2026.html").replace(self.FIRST_PUBLISHED, "")
        self.assertIn(self.NAME_LINK, undated)
        self.assertEqual(self.parse(undated).journal, UNKNOWN)

    def test_the_source_is_the_first_top_tip_link_to_cnki_navigation_or_nothing(self) -> None:
        # If this fails, some other .top-tip link was read as the journal: with the
        # name link gone, the next one reads 查看该刊数据库收录来源.
        html = fixture("cnki_article_online_first_2026.html")
        without_name = html.replace(self.NAME_LINK, "")
        record = self.parse(without_name)
        self.assertEqual(record.journal, UNKNOWN)
        self.assertEqual(record.year, "2026")
        self.assertEqual(record.publication_status, PublicationStatus.ONLINE_FIRST.value)
        off_site = html.replace(
            "https://navi.cnki.net/knavi/detail?p=fixture-journal&amp;uniplatform=NZKPT\">示例管理评论",
            "https://example.org/journal\">示例管理评论",
        )
        self.assertEqual(self.parse(off_site).journal, UNKNOWN)
        preceded = html.replace(self.NAME_LINK, '<a href="https://example.org/notice">通知</a>' + self.NAME_LINK)
        self.assertEqual(self.parse(preceded).journal, UNKNOWN)

    def test_a_page_stating_both_an_online_first_time_and_a_newspaper_date_is_left_unknown(self) -> None:
        # If this fails, a page contradicting itself was resolved by a guess.
        html = fixture("cnki_article_online_first_2026.html").replace(
            '<div class="operate" id="DownLoadParts">',
            '<div class="row"><span class="rowtit">报纸日期：</span><p>2026-04-10</p></div>'
            '<div class="operate" id="DownLoadParts">',
        )
        record = self.parse(html)
        self.assertEqual((record.journal, record.year), (UNKNOWN, UNKNOWN))

    def test_online_first_times_in_two_different_years_leave_the_year_unknown(self) -> None:
        # If this fails, one of two years the page gives was picked for it.
        html = fixture("cnki_article_online_first_2026.html").replace(
            self.FIRST_PUBLISHED,
            self.FIRST_PUBLISHED + "<span>（排版定稿）网络首发时间：2025-12-30 09:00:00</span>",
        )
        record = self.parse(html)
        self.assertEqual((record.journal, record.year), (UNKNOWN, UNKNOWN))

    def test_a_page_with_an_issue_citation_is_read_exactly_as_before(self) -> None:
        # If this fails, the new reading reached pages that already worked: an
        # issue citation still decides journal, year, issue and pages, even beside
        # an online-first time from another year.
        url = "https://kns.cnki.net/kcms2/article/abstract?v=redacted"
        plain = CNKIAdapter.parse_article_html(fixture("cnki_article_live_structure.html"), source_url=url)
        stated = CNKIAdapter.parse_article_html(
            fixture("cnki_article_live_structure.html").replace(
                '<div class="doc-top">',
                '<div class="head-time"><span>（排版定稿）网络首发时间：2025-12-30 09:00:00</span></div>'
                '<div class="doc-top">',
            ),
            source_url=url,
        )
        fields = ("journal", "year", "issue", "pages_or_article_number", "publication_type", "publication_status")
        self.assertEqual(
            [getattr(stated, field) for field in fields],
            [getattr(plain, field) for field in fields],
        )
        self.assertEqual((plain.journal, plain.year), ("财经科学", "2026"))

    def test_an_unclosed_top_tip_link_does_not_cost_a_page_its_issue_citation(self) -> None:
        # If this fails, reading the .top-tip links interferes with reading the
        # .top-tip itself: markup that leaves the name link open lost the issue
        # citation of a page that parsed before.
        html = fixture("cnki_article_live_structure.html")
        self.assertIn("<a>财经科学 .</a>", html)
        record = CNKIAdapter.parse_article_html(
            html.replace("<a>财经科学 .</a>", "<a>财经科学 ."),
            source_url="https://kns.cnki.net/kcms2/article/abstract?v=redacted",
        )
        self.assertEqual((record.journal, record.year, record.issue), ("财经科学", "2026", "02"))

    def test_exact_title_search_records_still_lock_to_online_first_and_newspaper_pages(self) -> None:
        # If this fails, `acquire` and `acquire-batch` can no longer take an
        # online-first or newspaper article: their search records carry no
        # journal or year, so the lock still settles on the title.
        cases = (
            (ONLINE_FIRST_TITLE, "cnki_article_online_first_2026.html", "示例管理评论", "2026-04-11 15:03", "期刊"),
            (NEWSPAPER_TITLE, "cnki_article_newspaper_2026.html", "示例日报", "2025-12-31", "报纸"),
        )
        for title, article, source, date, database in cases:
            with self.subTest(title=title):
                search = CNKIAdapter.parse_search_results_html(
                    self.result_page(title, source=source, date=date, database=database),
                    query=title,
                )[0]
                self.assertTrue(CNKIAdapter.identity_matches(search, self.parse(fixture(article)))[0])

    def test_a_result_row_carrying_its_source_and_date_locks_to_its_own_page(self) -> None:
        # If this fails, the journal or year now read from the page disagrees with
        # CNKI's own result row for the same article; on every saved online-first
        # and newspaper pair the two agreed.
        cases = (
            ("cnki_article_online_first_2026.html", ONLINE_FIRST_TITLE, "示例管理评论", "2026"),
            ("cnki_article_newspaper_2026.html", NEWSPAPER_TITLE, "示例日报", "2025"),
        )
        for article, title, source, year in cases:
            with self.subTest(source=source):
                row = LiteratureRecord(paper_id="row", title=title, journal=source, year=year)
                self.assertTrue(CNKIAdapter.identity_matches(row, self.parse(fixture(article)))[0])

    def test_a_same_titled_article_from_another_newspaper_no_longer_locks(self) -> None:
        # If this fails, the lock is back to title-only on newspaper pages.  The
        # saved 2026-08 pages hold exactly this: one headline printed by two
        # newspapers, three years apart.
        detail = self.parse(fixture("cnki_article_newspaper_2026.html"))
        other_year = LiteratureRecord(paper_id="row", title=NEWSPAPER_TITLE, journal="另一晚报", year="2023")
        self.assertEqual(CNKIAdapter.identity_matches(other_year, detail), (False, "Year mismatch"))
        same_year = LiteratureRecord(paper_id="row", title=NEWSPAPER_TITLE, journal="另一晚报", year="2025")
        self.assertEqual(CNKIAdapter.identity_matches(same_year, detail), (False, "Journal mismatch"))

    class _ArticleBrowser:
        """Serve one kns8s result page and the kcms2 article page it links to."""

        navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

        def __init__(self, result_html: str, article_html: str, article_url: str) -> None:
            self.result_html = result_html
            self.article_html = article_html
            self.article_url = article_url
            self.commands = []
            self.current_url = "about:blank"
            self.session = SessionHandle("uncited-source-page-test")
            self.page_handle = PageHandle("main", session=self.session)

        async def execute(self, command):
            self.commands.append(command)
            if isinstance(command, NavigateCommand):
                self.current_url = command.url
            elif isinstance(command, ClickCommand):
                self.current_url = self.article_url
            elif not isinstance(command, ObserveCommand):
                raise AssertionError(type(command).__name__)
            on_article = "/kcms2/article/abstract" in self.current_url
            return BrowserObservation(
                session=self.session,
                page=self.page_handle,
                generation=len(self.commands),
                url=self.current_url,
                title="中国知网" if on_article else "检索-中国知网",
                html=self.article_html if on_article else self.result_html,
            )

    async def _exact_title_run(self, title: str, result_page: str, article: str):
        browser = self._ArticleBrowser(result_page, fixture(article), self.DETAIL_URL)
        request = LiteratureSearchRequest(
            original_research_request=f"Find exact literature item: {title}",
            exact_titles=(title,),
            max_search_results=1,
            max_results_per_source=1,
            max_downloads=0,
            max_downloads_per_run=0,
            require_full_text=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            workflow = LiteratureAcquisitionWorkflow(
                CNKIAdapter(browser),
                run_root=Path(directory) / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            )
            result = await workflow.run(request)
            with (Path(directory) / "run" / "SEARCH_RESULTS.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        return result, rows

    async def test_an_exact_title_run_over_an_online_first_article_records_its_journal_and_year(self) -> None:
        # If this fails, an exact-title run over an online-first article either
        # fails its identity lock or writes journal and year as "unknown" again.
        page = self.result_page(ONLINE_FIRST_TITLE, source="示例管理评论", date="2026-04-11 15:03", database="期刊")
        result, rows = await self._exact_title_run(ONLINE_FIRST_TITLE, page, "cnki_article_online_first_2026.html")
        self.assertEqual(result.status, RunStatus.SUCCESS)
        [record] = result.records
        self.assertTrue(record.target_identity_confirmed)
        # The year now feeds the page's own fallback PaperID; the run must still
        # keep the identity-locked search PaperID (AGENTS.md Rule 52).
        search = CNKIAdapter.parse_search_results_html(page, query=ONLINE_FIRST_TITLE)[0]
        self.assertEqual(record.paper_id, search.paper_id)
        self.assertEqual(
            [(row["Title"], row["Journal"], row["Year"], row["PublicationStatus"]) for row in rows],
            [(ONLINE_FIRST_TITLE, "示例管理评论", "2026", PublicationStatus.ONLINE_FIRST.value)],
        )

    async def test_an_exact_title_run_over_a_newspaper_article_records_its_newspaper_and_year(self) -> None:
        # If this fails, an exact-title run over a newspaper article either fails
        # its identity lock or writes source and year as "unknown" again.
        page = self.result_page(NEWSPAPER_TITLE, source="示例日报", date="2025-12-31", database="报纸")
        result, rows = await self._exact_title_run(NEWSPAPER_TITLE, page, "cnki_article_newspaper_2026.html")
        self.assertEqual(result.status, RunStatus.SUCCESS)
        [record] = result.records
        self.assertTrue(record.target_identity_confirmed)
        self.assertEqual(record.publication_type, "Newspaper")
        search = CNKIAdapter.parse_search_results_html(page, query=NEWSPAPER_TITLE)[0]
        self.assertEqual(record.paper_id, search.paper_id)
        self.assertEqual(
            [(row["Title"], row["Journal"], row["Year"]) for row in rows],
            [(NEWSPAPER_TITLE, "示例日报", "2025")],
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


class CNKISpacedNewspaperHeadlineTests(unittest.IsolatedAsyncioTestCase):
    """Newspaper headlines that CNKI prints with spaces between their phrases.

    The result-page parser drops whitespace between two CJK ideographs, while
    the article parser keeps the h1's spacing, so one record reaches the
    workflow's search/detail lock in two spellings.  Both fixtures are
    synthetic; they mirror the structure of saved kns8s result rows and kcms2
    CCND article pages.
    """

    HEADLINE = "凝聚发展共识  汇聚各方智慧  共谋开放新局"

    class _SearchThenArticleBrowser:
        """Serve the result page to every search and the article page to a detail URL."""

        navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

        def __init__(self, search_html: str, article_html: str) -> None:
            self.search_html = search_html
            self.article_html = article_html
            self.commands: list[object] = []
            self.current_url = "about:blank"
            self.session = SessionHandle("cnki-spaced-headline-test")
            self.page_handle = PageHandle("main", session=self.session)

        async def execute(self, command):
            self.commands.append(command)
            if isinstance(command, NavigateCommand):
                self.current_url = command.url
            elif not isinstance(command, ObserveCommand):
                raise AssertionError(type(command).__name__)
            on_article = "/kcms2/article/abstract" in self.current_url
            return BrowserObservation(
                session=self.session,
                page=self.page_handle,
                generation=len(self.commands),
                url=self.current_url,
                title="中国知网" if on_article else "检索-中国知网",
                html=self.article_html if on_article else self.search_html,
            )

    @staticmethod
    def _request(**identity) -> LiteratureSearchRequest:
        return LiteratureSearchRequest(
            original_research_request="CNKI newspaper headline printed with spaces",
            max_search_results=1,
            max_results_per_source=1,
            max_downloads=0,
            max_downloads_per_run=0,
            **identity,
        )

    async def _run(
        self,
        request: LiteratureSearchRequest,
        *,
        search_title: str | None = None,
        article_title: str | None = None,
    ):
        search_html = fixture("cnki_search_newspaper_spaced_headline.html")
        article_html = fixture("cnki_article_newspaper_spaced_headline.html")
        if search_title is not None:
            search_html = search_html.replace(self.HEADLINE, search_title)
        if article_title is not None:
            article_html = article_html.replace(self.HEADLINE, article_title)
        browser = self._SearchThenArticleBrowser(search_html, article_html)
        with tempfile.TemporaryDirectory() as directory:
            return await LiteratureAcquisitionWorkflow(
                CNKIAdapter(browser),
                run_root=Path(directory) / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(request)

    async def test_a_headline_printed_with_spaces_between_its_phrases_passes_the_search_detail_lock(self) -> None:
        # If this fails, every CNKI headline printed with spaces between its phrases
        # stops at "Search/detail target identity lock failed before acquisition" --
        # found by an author or keyword search as much as by its own exact title.
        result = await self._run(self._request(authors=("作者甲",)))
        self.assertEqual(result.errors, [])
        self.assertEqual(result.status, RunStatus.SUCCESS)
        [record] = result.records
        self.assertEqual(record.error_status, UNKNOWN)
        self.assertTrue(record.target_identity_confirmed)

    async def test_an_exact_title_request_for_a_spaced_headline_as_cnki_prints_it_reaches_the_download_step(self) -> None:
        # If this fails, `acquire --title` (and every acquire-batch item) cannot fetch a
        # newspaper headline copied as CNKI prints it: the run ends before the access check.
        result = await self._run(self._request(exact_titles=(self.HEADLINE,)))
        self.assertEqual(result.errors, [])
        [record] = result.records
        self.assertTrue(record.target_identity_confirmed)
        self.assertEqual(record.screening_decision, ScreeningDecision.KEEP.value)
        self.assertTrue(record.full_text_accessible)
        self.assertTrue(_is_fulltext_acquisition_candidate(record))

    async def test_the_workflow_lock_by_itself_still_refuses_a_different_title(self) -> None:
        # If this fails, the workflow's own search/detail lock accepts a different paper
        # once the adapter's identity check is out of the way -- it no longer backs that
        # check up, and a word, a subtitle, or Latin word spacing slips through it.
        def adapter_check_always_agrees(_search_record, _detail_record):
            return True, "patched: only the workflow lock is left"

        cases = {
            "one character": (None, "凝聚发展共识  汇聚各方智慧  共谋开放大局"),
            "an added subtitle": (None, "凝聚发展共识  汇聚各方智慧  共谋开放新局——访示例专家"),
            "a missing phrase": (None, "凝聚发展共识  汇聚各方智慧"),
            "Latin word spacing": ("Open markets and on line trade", "Open markets and online trade"),
        }
        for label, (search_title, article_title) in cases.items():
            with self.subTest(difference=label):
                with patch.object(
                    CNKIAdapter,
                    "identity_matches",
                    staticmethod(adapter_check_always_agrees),
                ):
                    result = await self._run(
                        self._request(authors=("作者甲",)),
                        search_title=search_title,
                        article_title=article_title,
                    )
                [record] = result.records
                self.assertFalse(record.target_identity_confirmed)
                self.assertEqual(record.error_status, RunStatus.SOURCE_LAYOUT_CHANGED.value)
                self.assertIn("Search/detail target identity lock failed", record.error_reason)

    def test_only_the_registered_cnki_adapter_compares_titles_through_the_cnki_spacing_rule(self) -> None:
        # If this fails, a substitute adapter -- a CNKIAdapter subclass included -- has its
        # search/detail lock relaxed by CNKI's spacing rule instead of plain normalize_title.
        class _Substitute(CNKIAdapter):
            pass

        cnki_key = _search_detail_title_key(CNKIAdapter(None))
        self.assertEqual(cnki_key(self.HEADLINE), cnki_key(self.HEADLINE.replace(" ", "")))
        for adapter in (_Substitute(None), ScienceDirectAdapter(None)):
            with self.subTest(adapter=type(adapter).__name__):
                self.assertIs(_search_detail_title_key(adapter), normalize_title)


class CNKISharedHeadlineRowTests(unittest.IsolatedAsyncioTestCase):
    """Different newspaper articles that CNKI lists under one headline.

    A 2026 kns8s result link is signed and names no dbcode/filename, so the
    result parser keyed each row by its title and merged same-headline rows
    into the first.  Each row names its own record on its collect control
    (``a.icon-collect[data-dbname][data-filename]``).  The fixtures are
    synthetic; they mirror the structure of saved kns8s result rows and kcms2
    CCND article pages.
    """

    HEADLINE = "春耕生产正当时"
    JOURNAL_TITLE = "春季田间管理技术要点"
    DAILY_URL = (
        "https://kns.cnki.net/kcms2/article/abstract?v=fixture-shared-headline-daily"
        "&uniplatform=NZKPT&language=CHS"
    )
    EVENING_URL = (
        "https://kns.cnki.net/kcms2/article/abstract?v=fixture-shared-headline-evening"
        "&uniplatform=NZKPT&language=CHS"
    )
    JOURNAL_URL = (
        "https://kns.cnki.net/kcms2/article/abstract?v=fixture-journal-row"
        "&uniplatform=NZKPT&language=CHS"
    )
    DAILY_COLLECT = (
        '<a class="icon-collect" title="收藏" data-dbname="CCND" data-resource="NEWSPAPER" '
        'data-filename="SLRB202603180001"><i></i></a>'
    )
    EVENING_COLLECT = (
        '<a class="icon-collect" title="收藏" data-dbname="CCND" data-resource="NEWSPAPER" '
        'data-filename="SLWB202603180003"><i></i></a>'
    )

    class _CNKIBrowser:
        """Serve result pages by search field (korder) and article pages by their link.

        Each field's pages are served in order, the last one repeating.  Like
        CNKI, every render signs its result links afresh, so a link read from
        one search never reappears in the next.
        """

        navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

        def __init__(self, searches: dict[str, list[str]], *, downloads_dir: Path | None = None) -> None:
            self.searches = {order: list(pages) for order, pages in searches.items()}
            self.articles = {
                "daily": fixture("cnki_article_newspaper_shared_headline_daily.html"),
                "evening": fixture("cnki_article_newspaper_shared_headline_evening.html"),
            }
            self.downloads_dir = downloads_dir
            self.renders = 0
            self.commands: list[object] = []
            self.current_url = "about:blank"
            self.current_html = ""
            self.session = SessionHandle("cnki-shared-headline-test")
            self.page_handle = PageHandle("main", session=self.session)

        @staticmethod
        def article_name(url: str) -> str:
            """``daily`` or ``evening`` for a shared-headline article link, whatever its signature."""

            return parse_qs(urlsplit(url).query)["v"][0].split(".")[0].removeprefix("fixture-shared-headline-")

        async def execute(self, command):
            self.commands.append(command)
            if isinstance(command, NavigateCommand):
                self.current_url = command.url
                query = parse_qs(urlsplit(command.url).query)
                if "/kcms2/article/abstract" in command.url:
                    self.current_html = self.articles[self.article_name(command.url)]
                else:
                    pages = self.searches[query["korder"][0]]
                    self.renders += 1
                    self.current_html = re.sub(
                        r"(v=fixture-[a-z-]+)",
                        rf"\g<1>.{self.renders}",
                        pages.pop(0) if len(pages) > 1 else pages[0],
                    )
            elif isinstance(command, DownloadCommand):
                downloaded = write_minimal_pdf(self.downloads_dir / f"download-{len(self.commands)}.pdf")
                return DownloadArtifact.from_path(
                    downloaded,
                    suggested_filename=command.suggested_filename,
                    page=self.page_handle,
                )
            elif not isinstance(command, ObserveCommand):
                raise AssertionError(type(command).__name__)
            return BrowserObservation(
                session=self.session,
                page=self.page_handle,
                generation=len(self.commands),
                url=self.current_url,
                title="检索-中国知网",
                html=self.current_html,
            )

        def opened_articles(self) -> list[str]:
            return [
                self.article_name(command.url)
                for command in self.commands
                if isinstance(command, NavigateCommand) and "/kcms2/article/abstract" in command.url
            ]

        def search_fields(self) -> list[str]:
            return [
                parse_qs(urlsplit(command.url).query)["korder"][0]
                for command in self.commands
                if isinstance(command, NavigateCommand) and "korder=" in command.url
            ]

    @staticmethod
    def _page() -> str:
        return fixture("cnki_search_newspaper_shared_headline.html")

    @classmethod
    def _page_with_rows(cls, *names: str) -> str:
        """The result page keeping only the rows that mention one of ``names``."""

        return re.sub(
            r"\s*<tr>.*?</tr>",
            lambda row: row.group(0) if any(name in row.group(0) for name in names) else "",
            cls._page(),
            flags=re.S,
        )

    @staticmethod
    def _request(**identity) -> LiteratureSearchRequest:
        return LiteratureSearchRequest(
            original_research_request="CNKI newspaper articles printed under one headline",
            max_search_results=3,
            max_results_per_source=3,
            max_downloads=0,
            max_downloads_per_run=0,
            **identity,
        )

    def _titles(self, html: str, *, max_results: int = 30) -> list[str]:
        return [
            record.title
            for record in CNKIAdapter.parse_search_results_html(html, query=self.HEADLINE, max_results=max_results)
        ]

    def test_rows_that_share_a_headline_but_name_different_records_stay_separate_results(self) -> None:
        # If this fails, the second of two same-headline newspaper articles vanishes from
        # CNKI's results again: its signed link names no filename, so the row is keyed by
        # its title and merged into the first one.
        expected = [
            (self.HEADLINE, self.DAILY_URL),
            (self.HEADLINE, self.EVENING_URL),
            (self.JOURNAL_TITLE, self.JOURNAL_URL),
        ]
        for max_results in (1, 2, 3, 30):
            with self.subTest(max_results=max_results):
                records = CNKIAdapter.parse_search_results_html(
                    self._page(), query=self.HEADLINE, max_results=max_results
                )
                self.assertEqual(
                    [(record.title, record.navigation_url) for record in records],
                    expected[:max_results],
                )
                # The row key stays inside the adapter: identity_matches, the fetch
                # ledger and the manifest see the same identifier as before.
                self.assertEqual({record.stable_identifier for record in records}, {UNKNOWN})

    def test_one_record_listed_twice_under_its_own_key_is_still_one_result(self) -> None:
        # If this fails, a row key split one CNKI record into two results.
        page = self._page().replace(self.EVENING_COLLECT, self.DAILY_COLLECT)
        self.assertEqual(self._titles(page), [self.HEADLINE, self.JOURNAL_TITLE])

    def test_rows_without_a_key_of_their_own_still_merge_by_title(self) -> None:
        # If this fails, rows are told apart by something other than their own collect
        # control naming one record -- position, half a key, one of two keys, or a key
        # that sits in another row -- and a merge the title rule made quietly changes.
        page = self._page()
        variants = {
            "the first row names no record": page.replace(self.DAILY_COLLECT, ""),
            "the second row names no record": page.replace(self.EVENING_COLLECT, ""),
            "a collect control without data-dbname": page.replace(
                self.EVENING_COLLECT, self.EVENING_COLLECT.replace(' data-dbname="CCND"', "")
            ),
            "one row naming two records": page.replace(
                self.EVENING_COLLECT,
                self.EVENING_COLLECT + self.EVENING_COLLECT.replace("SLWB202603180003", "SLWB202603180009"),
            ),
            "the key sitting in a row of its own": re.sub(
                r'<tr>\s*<td class="seq">3</td>',
                lambda row: f'<tr><td class="operat">{self.EVENING_COLLECT}</td></tr>{row.group(0)}',
                page.replace(self.EVENING_COLLECT, ""),
            ),
        }
        for label, variant in variants.items():
            with self.subTest(variant=label):
                self.assertEqual(self._titles(variant), [self.HEADLINE, self.JOURNAL_TITLE])

    def test_a_row_key_never_splits_the_other_links_in_its_row(self) -> None:
        # If this fails, a row's key reaches links that are not its title -- a citation
        # count, say -- and every page listing two rows with the same count gains a result.
        page = self._page().replace(
            'fixture-shared-headline-evening&amp;uniplatform=NZKPT&amp;language=CHS">春耕生产正当时',
            'fixture-shared-headline-evening&amp;uniplatform=NZKPT&amp;language=CHS">春耕备耕两不误',
        )
        rows = iter(range(1, 4))
        cited = page.replace('<td class="operat">', "{citation}<td class=\"operat\">")
        while "{citation}" in cited:
            cited = cited.replace(
                "{citation}",
                '<td class="quote"><a target="_blank" href="https://kns.cnki.net/kcms2/article/abstract?'
                f'v=fixture-citing-{next(rows)}&amp;uniplatform=NZKPT&amp;language=CHS">3</a></td>',
                1,
            )
        keyless = re.sub(r'<a class="icon-collect"[^>]*><i></i></a>', "", cited)
        self.assertEqual(self._titles(cited), self._titles(keyless))

    async def test_each_same_headline_row_relocks_onto_its_own_article(self) -> None:
        # If this fails, opening the second row lands on the first row's article: the
        # exact-title refresh lists both, and identity_matches sees nothing but a title.
        browser = self._CNKIBrowser({"TI": [self._page()]})
        adapter = CNKIAdapter(browser)
        daily, evening = await adapter.search(
            f'"{self.HEADLINE}"', self._request(exact_titles=(self.HEADLINE,))
        )
        await adapter.open_result(evening)
        await adapter.open_result(daily)
        self.assertEqual(browser.opened_articles(), ["evening", "daily"])

    async def test_a_row_found_by_an_author_search_relocks_onto_its_own_article(self) -> None:
        # If this fails, an author search finds the evening paper's article and the
        # exact-title refresh then swaps in the daily paper's article under the same headline.
        browser = self._CNKIBrowser({"AU": [self._page_with_rows("作者乙")], "TI": [self._page()]})
        adapter = CNKIAdapter(browser)
        [evening] = await adapter.search('author:"作者乙"', self._request(authors=("作者乙",)))
        await adapter.open_result(evening)
        self.assertEqual(browser.search_fields(), ["AU", "TI"])
        self.assertEqual(browser.opened_articles(), ["evening"])

    async def test_a_row_missing_from_the_refresh_is_never_relocked_onto_another_row_with_its_headline(self) -> None:
        # If this fails, the relock substitutes a different CNKI record for the one that was
        # screened, only because the two share a headline.
        browser = self._CNKIBrowser({"TI": [self._page(), self._page_with_rows("作者甲")]})
        adapter = CNKIAdapter(browser)
        _daily, evening = await adapter.search(
            f'"{self.HEADLINE}"', self._request(exact_titles=(self.HEADLINE,))
        )
        with self.assertRaisesRegex(SourceLayoutChanged, "could not relock"):
            await adapter.open_result(evening)
        self.assertEqual(browser.opened_articles(), [])

    async def test_a_refresh_page_whose_rows_carry_no_key_relocks_by_identity_as_before(self) -> None:
        # If this fails, a result page without collect controls -- an older layout, or one
        # read as a structured snapshot -- no longer relocks at all.  A row without a key is
        # judged by identity_matches alone, exactly as before.
        keyless = self._page().replace(self.DAILY_COLLECT, "").replace(self.EVENING_COLLECT, "")
        browser = self._CNKIBrowser({"TI": [self._page(), keyless]})
        adapter = CNKIAdapter(browser)
        daily, _evening = await adapter.search(
            f'"{self.HEADLINE}"', self._request(exact_titles=(self.HEADLINE,))
        )
        await adapter.open_result(daily)
        self.assertEqual(browser.opened_articles(), ["daily"])

    async def test_an_exact_title_and_author_request_reaches_and_downloads_the_second_same_headline_article(self) -> None:
        # If this fails, the second of two different newspaper articles printed under one
        # headline is unreachable again: its row is merged into the first, or a relock --
        # while screening, or again before the download -- lands on the first paper's
        # article, where the requested-author check or the identity lock refuses it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = self._CNKIBrowser(
                {"TI": [self._page()], "AU": [self._page_with_rows("作者乙")]},
                downloads_dir=root,
            )
            adapter = CNKIAdapter(browser)
            with isolated_fetch_ledger() as ledger:
                adapter.fetch_ledger = ledger
                result = await LiteratureAcquisitionWorkflow(
                    adapter,
                    run_root=root / "run",
                    human_like_delay_seconds=0,
                    allow_outside_project_for_tests=True,
                ).run(
                    LiteratureSearchRequest(
                        original_research_request="The evening paper's article under a shared headline",
                        exact_titles=(self.HEADLINE,),
                        authors=("作者乙",),
                        max_search_results=2,
                        max_results_per_source=2,
                        max_downloads=1,
                        max_downloads_per_run=1,
                    )
                )
        self.assertEqual(result.errors, [])
        self.assertEqual(result.status, RunStatus.SUCCESS)
        self.assertEqual(
            [(record.authors, record.target_identity_confirmed) for record in result.records],
            [(("作者甲",), False), (("作者乙",), True)],
        )
        [download] = result.downloads
        self.assertEqual(download.doi, "10.99999/n.cnki.fixture.2026.000012")
        # Screening opened each row's own article once; the download reopened the evening one.
        self.assertEqual(browser.opened_articles(), ["daily", "evening", "evening"])


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


class CNKICitationCountLinkTests(unittest.IsolatedAsyncioTestCase):
    """A result row's citation count links to its article; it is not a result.

    kns8s prints each row's citation count in ``td.quote`` as
    ``<a class="quoteCnt" href="/kcms2/article/abstract?v=...&anchor=citnet">7</a>``.
    That is a kcms2 detail URL, so it passes the result-link filter, and a 2026
    ``v=`` URL carries no dbcode/filename to de-duplicate it against the row's
    title.  The rows are synthetic; they mirror the cells of saved kns8s result
    pages, as cnki_search_citation_count_snapshot.yml mirrors a saved
    accessibility snapshot.
    """

    ARTICLE_TITLE = "企业数字化转型与资本市场表现——来自股票流动性的经验证据"
    OTHER_TITLE = "城市轨道交通网络韧性评价"

    class _ResultsThenArticleBrowser:
        """Serve the result page to every search and the article page to a detail URL."""

        navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

        def __init__(self, results_html: str, article_html: str) -> None:
            self.results_html = results_html
            self.article_html = article_html
            self.commands: list[object] = []
            self.current_url = "about:blank"
            self.session = SessionHandle("cnki-citation-count-test")
            self.page_handle = PageHandle("main", session=self.session)

        async def execute(self, command):
            self.commands.append(command)
            if isinstance(command, NavigateCommand):
                self.current_url = command.url
            elif not isinstance(command, (ObserveCommand, ClickCommand)):
                raise AssertionError(type(command).__name__)
            on_article = "/kcms2/article/abstract" in self.current_url
            return BrowserObservation(
                session=self.session,
                page=self.page_handle,
                generation=len(self.commands),
                url=self.current_url,
                title="中国知网" if on_article else "检索-中国知网",
                html=self.article_html if on_article else self.results_html,
            )

    @staticmethod
    def _detail_url(token: str, *, anchor: str = "") -> str:
        url = f"https://kns.cnki.net/kcms2/article/abstract?v={token}&amp;uniplatform=NZKPT&amp;language=CHS"
        return f"{url}&amp;anchor={anchor}" if anchor else url

    @classmethod
    def _citation_link(cls, token: str, count: str, *, anchor: str = "citnet") -> str:
        return (
            f'<span><a class="quoteCnt" target="_blank" '
            f'href="{cls._detail_url(token, anchor=anchor)}">{count}</a></span>'
        )

    @classmethod
    def _results_page(cls, *rows: tuple[str, str, str], plain_title_rows: tuple[str, ...] = ()) -> str:
        """One kns8s result table; each row is ``(title, v token, td.quote content)``.

        CNKI prints a title with subscripts as a plain ``a.fz14`` without
        ``inline``; the rows whose tokens are in ``plain_title_rows`` get one.
        """

        body = "".join(
            f"""
              <tr>
                <td class="seq">{number}</td>
                <td class="name"><a class="{'fz14' if token in plain_title_rows else 'fz14 inline'}" target="_blank" href="{cls._detail_url(token)}">{title}</a></td>
                <td class="author"><a class="KnowledgeNetLink" target="knet" href="https://kns.cnki.net/kcms2/author/detail?v=fixture-author">作者甲</a></td>
                <td class="source"><p><a target="_blank" href="https://navi.cnki.net/knavi/detail?p=fixture-journal">示例期刊</a></p></td>
                <td class="date">2026-07-30 13:08</td>
                <td class="data"><span>期刊</span></td>
                <td class="quote">{quote}</td>
                <td class="download"><div><a class="downloadCnt" href="javascript:void(0);">77</a></div></td>
              </tr>"""
            for number, (title, token, quote) in enumerate(rows, start=1)
        )
        return f"""<!doctype html>
        <html lang="zh-CN"><head><meta charset="utf-8"><title>检索-中国知网</title></head>
        <body><main>
          <div class="pagerTitleCell">共找到 {len(rows)} 条结果</div>
          <table class="result-table-list">
            <thead><tr><th></th><th>题名</th><th>作者</th><th>来源</th><th>发表时间</th><th>数据库</th><th>被引</th><th>下载</th></tr></thead>
            <tbody>{body}</tbody>
          </table>
        </main></body></html>
        """

    @staticmethod
    def _titles(html: str) -> list[str]:
        records = CNKIAdapter.parse_search_results_html(html, query='author:"作者甲"', max_results=30)
        return [record.title for record in records]

    def test_a_citation_count_in_a_result_row_is_not_a_result_of_its_own(self) -> None:
        # If this fails, every CNKI result row with a citation count adds a record titled
        # with that count ("1", "7", "77"), and a run then spends one CNKI exact-title
        # search on each number -- the saved 2026-08-20 runs searched kw=1, kw=14, kw=77.
        html = self._results_page(
            (self.ARTICLE_TITLE, "row-a", self._citation_link("row-a", "7")),
            (self.OTHER_TITLE, "row-b", self._citation_link("row-b", "77")),
            ("高速铁路站点布局研究", "row-c", ""),
        )
        records = CNKIAdapter.parse_search_results_html(html, query='author:"作者甲"', max_results=30)
        self.assertEqual(
            [record.title for record in records],
            [self.ARTICLE_TITLE, self.OTHER_TITLE, "高速铁路站点布局研究"],
        )
        self.assertFalse([record for record in records if "anchor=citnet" in record.navigation_url])

    def test_a_title_that_begins_with_digits_is_still_a_result(self) -> None:
        # If this fails, the citation-count filter drops real results whose titles merely
        # begin with a number -- a count of samples, a year range, a chemical locant.
        titles = [
            "26份谷子种质资源的农艺性状与遗传多样性分析",
            "2015—2025年城市轨道交通客流特征分析",
            "1 , 4 -丁二醇的合成研究",
            "12 个高速铁路项目集中开工",
            "5G与高速铁路通信研究",
        ]
        html = self._results_page(*((title, f"row-{index}", "") for index, title in enumerate(titles)))
        self.assertEqual(self._titles(html), titles)

    def test_a_citnet_link_is_rejected_even_when_its_text_is_not_a_bare_number(self) -> None:
        # If this fails, a citation link is recognised only by its text being a number, so
        # a count CNKI prints any other way ("1,234", "被引 12") becomes a result again.
        for count in ("1,234", "被引 12"):
            with self.subTest(count=count):
                html = self._results_page((self.OTHER_TITLE, "row-a", self._citation_link("row-a", count)))
                self.assertEqual(self._titles(html), [self.OTHER_TITLE])

    def test_a_bare_number_link_is_rejected_even_without_the_citnet_anchor(self) -> None:
        # If this fails, a citation link is recognised only by its ``anchor=citnet``
        # parameter, so one whose URL loses that parameter becomes a result titled "12".
        html = self._results_page(
            (self.OTHER_TITLE, "row-a", self._citation_link("row-a", "12", anchor="")),
        )
        self.assertEqual(self._titles(html), [self.OTHER_TITLE])

    def test_citation_counts_do_not_crowd_a_real_title_out_of_the_result_window(self) -> None:
        # If this fails, row 1's citation count again takes a result slot ahead of row 2's
        # title and a real hit falls outside max_results: CNKI prints a title with
        # subscripts as a plain ``a.fz14``, which ranks with the count links, not before them.
        from urllib.parse import parse_qs, urlsplit

        html = self._results_page(
            (self.ARTICLE_TITLE, "row-a", self._citation_link("row-a", "1572")),
            ("Fe<sub>3</sub>O<sub>4</sub>纳米颗粒的制备与表征", "row-b", self._citation_link("row-b", "8")),
            plain_title_rows=("row-b",),
        )
        records = CNKIAdapter.parse_search_results_html(html, query='author:"作者甲"', max_results=2)
        self.assertEqual(
            [parse_qs(urlsplit(record.navigation_url).query)["v"][0] for record in records],
            ["row-a", "row-b"],
        )
        self.assertFalse([record for record in records if record.title.isdigit()])

    def test_the_snapshot_parser_leaves_the_same_citation_count_link_out(self) -> None:
        # If this fails, the accessibility-snapshot fallback -- what a CNKI run parses when
        # the HTML observation is unavailable -- turns a row's citation count back into a
        # result.  cnki_search_snapshot.yml cannot show it: its count link carries
        # dbcode/filename, so de-duplication drops the count even without the filter.
        records = CNKIAdapter.parse_search_results_snapshot(
            fixture("cnki_search_citation_count_snapshot.yml"),
            query="示例关键词",
            max_results=30,
        )
        self.assertEqual([record.title for record in records], [self.ARTICLE_TITLE])

    async def test_an_author_search_sends_no_exact_title_search_for_a_citation_count(self) -> None:
        # If this fails, an author or keyword search again treats a citation count as a paper:
        # an exact-title search for "7", a click on whatever reads "7", and a visit to the
        # row's citation page -- all before the identity lock throws the record out.
        from urllib.parse import parse_qs, urlsplit

        browser = self._ResultsThenArticleBrowser(
            self._results_page((self.ARTICLE_TITLE, "row-a", self._citation_link("row-a", "7"))),
            fixture("cnki_abstract_2026.html"),
        )
        request = LiteratureSearchRequest(
            original_research_request="CNKI author search over a row with a citation count",
            authors=("作者甲",),
            max_search_results=5,
            max_results_per_source=5,
            max_downloads=0,
            max_downloads_per_run=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = await LiteratureAcquisitionWorkflow(
                CNKIAdapter(browser),
                run_root=Path(directory) / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(request)

        navigations = [command.url for command in browser.commands if isinstance(command, NavigateCommand)]
        exact_title_terms = [
            parse_qs(urlsplit(url).query)["kw"][0]
            for url in navigations
            if parse_qs(urlsplit(url).query).get("korder") == ["TI"]
        ]
        self.assertEqual(exact_title_terms, [self.ARTICLE_TITLE])
        self.assertFalse([url for url in navigations if "anchor=citnet" in url])
        self.assertEqual([record.title for record in result.records], [self.ARTICLE_TITLE])
        self.assertEqual(result.errors, [])


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
