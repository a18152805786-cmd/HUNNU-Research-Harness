from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.adapters.base import SourceActionRequired
from hunnu_harness.literature.adapters.cnki import CNKIAdapter
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

    def test_pdf_access_requires_enabled_single_paper_control(self) -> None:
        decision = CNKIAdapter.check_fulltext_access_html(fixture("cnki_article_pdf.html"), source_url=ARTICLE_URL)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)
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
        self.assertIn("no verified institutional", decision.reason)

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
