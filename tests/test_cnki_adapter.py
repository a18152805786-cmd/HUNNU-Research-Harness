from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.browser.commands import (
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
from hunnu_harness.literature.workflow import finalize_captured_cnki_acceptance

from literature_test_support import write_minimal_pdf


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

    def test_exact_title_query_removes_only_dash_adjacent_spaces(self) -> None:
        spaced = "A —— B"
        compact = "A——B"
        self.assertEqual(
            _canonicalize_cnki_title_identity(spaced),
            _canonicalize_cnki_title_identity(compact),
        )
        url = CNKIAdapter.build_search_url(spaced, mode="exact_title")
        self.assertNotIn("+%E2%80%94", url)

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

    async def test_html_search_retry_remains_bounded_when_page_never_settles(self) -> None:
        title = "不存在的精确论文标题"
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
        self.assertEqual(records, [])
        self.assertEqual(browser.observe_count, 2)


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
            path = await adapter.download_fulltext(record, access)
            self.assertEqual(path, downloaded.resolve())
            command = next(item for item in browser.commands if isinstance(item, DownloadCommand))
            self.assertIsNone(command.target.css)
            self.assertEqual(command.target.text, "PDF下载")
            self.assertTrue(command.target.exact_text)

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
        with self.assertRaises(SourceUnavailable):
            await adapter.download_fulltext(record, access)

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
