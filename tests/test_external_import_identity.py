import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.literature.fulltext import AuthorizedFullTextValidator
from hunnu_harness.literature.library import (
    ExternalPaperImporter,
    GlobalPaperLibrary,
    LibraryDisposition,
)
from hunnu_harness.literature.models import LiteratureRecord
from hunnu_harness.literature.normalization import sha256_file, stable_paper_id

from literature_test_support import write_minimal_pdf, write_minimal_pdf_with_text


TITLE_A = "Artificial Intelligence and Firm Value"
TITLE_B = "Corporate Tax Avoidance and Audit Quality"
DOI_A = "10.1234/paper-a"
DOI_B = "10.1234/paper-b"


class ExternalImportIdentityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = GlobalPaperLibrary(
            self.root / "library",
            allow_outside_output_for_tests=True,
            make_managed_read_only=False,
        )
        self.importer = ExternalPaperImporter(self.library)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _metadata(
        self,
        *,
        title: str = TITLE_A,
        doi: str | None = None,
        authors: tuple[str, ...] = ("Ada Author",),
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "Title": title,
            "Authors": list(authors),
            "Year": "2026",
            "Journal": "Journal of Identity Safety",
            "SourceLocator": "SyntheticExternalInventory",
        }
        if doi is not None:
            metadata["DOI"] = doi
        return metadata

    def _stage_text(self, text: str, name: str = "candidate.pdf"):
        source = write_minimal_pdf_with_text(self.root / name, text)
        return source, self.importer.stage_pdf(source)

    def _catalog_bytes(self) -> tuple[bytes, bytes]:
        return (
            self.library.catalog_jsonl_path.read_bytes(),
            self.library.catalog_csv_path.read_bytes(),
        )

    def test_correct_title_without_doi_is_verified_and_imported(self) -> None:
        _, staged = self._stage_text(TITLE_A)
        result = self.importer.import_staged_pdf(staged.staged_path, self._metadata())

        self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
        self.assertIn("TitleVerification=matched", result.reason)
        self.assertTrue(result.managed_path.exists())

    def test_clearly_wrong_title_fails_closed_before_managed_or_catalog_write(self) -> None:
        _, staged = self._stage_text(TITLE_A)
        metadata = self._metadata(title=TITLE_B)
        claimed_id = stable_paper_id(
            title=TITLE_B,
            year="2026",
            authors=("Ada Author",),
        )
        before = self._catalog_bytes()

        result = self.importer.import_staged_pdf(staged.staged_path, metadata)

        self.assertRegex(claimed_id, r"^P[0-9A-F]{12}$")
        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertEqual(list(self.library.papers_dir.iterdir()), [])
        self.assertEqual(self._catalog_bytes(), before)

    def test_normalized_doi_match_is_verified(self) -> None:
        _, staged = self._stage_text(f"{TITLE_A} DOI: {DOI_A.upper()}.")
        result = self.importer.import_staged_pdf(
            staged.staged_path,
            self._metadata(doi=f"https://doi.org/{DOI_A}"),
        )

        self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
        self.assertIn("DOIVerification=DOI_MATCH", result.reason)

    def test_doi_match_can_verify_when_title_evidence_is_unavailable(self) -> None:
        _, staged = self._stage_text(f"DOI: {DOI_A}")

        result = self.importer.import_staged_pdf(
            staged.staged_path,
            self._metadata(doi=DOI_A),
        )

        self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
        self.assertIn("DOIVerification=DOI_MATCH", result.reason)
        self.assertIn("TitleVerification=not_available", result.reason)

    def test_supplied_doi_missing_from_pdf_is_not_rescued_by_title_match(self) -> None:
        _, staged = self._stage_text(TITLE_A)

        result = self.importer.import_staged_pdf(
            staged.staged_path,
            self._metadata(doi=DOI_A),
        )

        self.assertEqual(
            result.disposition,
            LibraryDisposition.EXTERNAL_IDENTITY_UNVERIFIED,
        )
        self.assertIn("DOIVerification=DOI_NOT_AVAILABLE", result.reason)
        self.assertIn("TitleVerification=matched", result.reason)

    def test_doi_conflict_fails_closed(self) -> None:
        _, staged = self._stage_text(f"{TITLE_A} DOI: {DOI_A}")
        result = self.importer.import_staged_pdf(
            staged.staged_path,
            self._metadata(doi=DOI_B),
        )

        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertIn("DOIVerification=DOI_CONFLICT", result.reason)
        self.assertEqual(list(self.library.papers_dir.iterdir()), [])

    def test_additional_different_doi_fails_closed_even_when_claimed_doi_is_present(self) -> None:
        _, staged = self._stage_text(f"{TITLE_A} DOI: {DOI_A} related DOI: {DOI_B}")

        result = self.importer.import_staged_pdf(
            staged.staged_path,
            self._metadata(doi=DOI_A),
        )

        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertIn("DOIVerification=DOI_CONFLICT", result.reason)

    def test_complete_metadata_for_another_paper_cannot_bind_pdf_to_wrong_paper_id(self) -> None:
        _, staged = self._stage_text(f"{TITLE_A} DOI: {DOI_A}")
        metadata = self._metadata(title=TITLE_B, doi=DOI_B)
        claimed_id = stable_paper_id(doi=DOI_B, title=TITLE_B)

        result = self.importer.import_staged_pdf(staged.staged_path, metadata)

        self.assertRegex(claimed_id, r"^P[0-9A-F]{12}$")
        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertFalse((self.library.papers_dir / f"{claimed_id}.pdf").exists())

    def test_structurally_valid_pdf_without_extractable_identity_is_unverified(self) -> None:
        source = write_minimal_pdf(self.root / "no-text.pdf")
        staged = self.importer.stage_pdf(source)

        result = self.importer.import_staged_pdf(
            staged.staged_path,
            self._metadata(doi=DOI_A),
        )

        self.assertEqual(
            result.disposition,
            LibraryDisposition.EXTERNAL_IDENTITY_UNVERIFIED,
        )
        self.assertIn("DOIVerification=DOI_NOT_AVAILABLE", result.reason)
        self.assertIn("TitleVerification=not_available", result.reason)
        self.assertEqual(list(self.library.papers_dir.iterdir()), [])

    def test_identity_failure_preserves_external_source_hash(self) -> None:
        source, staged = self._stage_text(f"{TITLE_A} DOI: {DOI_A}")
        before = sha256_file(source)

        result = self.importer.import_staged_pdf(
            staged.staged_path,
            self._metadata(title=TITLE_B, doi=DOI_B),
        )

        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertTrue(source.exists())
        self.assertEqual(sha256_file(source), before)

    def test_conflict_and_unverified_candidates_remain_in_staging(self) -> None:
        _, conflict = self._stage_text(f"{TITLE_A} DOI: {DOI_A}", "conflict.pdf")
        no_text_source = write_minimal_pdf(self.root / "unverified.pdf")
        unverified = self.importer.stage_pdf(no_text_source)

        conflict_result = self.importer.import_staged_pdf(
            conflict.staged_path,
            self._metadata(doi=DOI_B),
        )
        unverified_result = self.importer.import_staged_pdf(
            unverified.staged_path,
            self._metadata(doi=DOI_A),
        )

        self.assertEqual(conflict_result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertEqual(
            unverified_result.disposition,
            LibraryDisposition.EXTERNAL_IDENTITY_UNVERIFIED,
        )
        self.assertTrue(conflict.staged_path.exists())
        self.assertTrue(unverified.staged_path.exists())

    def test_identity_failure_leaves_both_catalog_projections_untouched(self) -> None:
        _, staged = self._stage_text(f"{TITLE_A} DOI: {DOI_A}")
        before = self._catalog_bytes()

        result = self.importer.import_staged_pdf(
            staged.staged_path,
            self._metadata(doi=DOI_B),
        )

        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertEqual(self._catalog_bytes(), before)

    def test_verified_exact_duplicate_still_uses_existing_dedupe_path(self) -> None:
        first_source, first_staged = self._stage_text(
            f"{TITLE_A} DOI: {DOI_A}",
            "first.pdf",
        )
        second_source = self.root / "second.pdf"
        second_source.write_bytes(first_source.read_bytes())
        second_staged = self.importer.stage_pdf(second_source)

        initial = self.importer.import_staged_pdf(
            first_staged.staged_path,
            self._metadata(doi=DOI_A),
        )
        duplicate = self.importer.import_staged_pdf(
            second_staged.staged_path,
            self._metadata(doi=DOI_A),
        )

        self.assertEqual(initial.disposition, LibraryDisposition.NEW_PAPER)
        self.assertEqual(duplicate.disposition, LibraryDisposition.EXACT_DUPLICATE)
        self.assertEqual(len(list(self.library.papers_dir.glob("*.pdf"))), 1)

    def test_verified_same_work_different_version_still_retains_both_files(self) -> None:
        _, first = self._stage_text(f"{TITLE_A} DOI: {DOI_A} publisher", "publisher.pdf")
        _, second = self._stage_text(f"{TITLE_A} DOI: {DOI_A} accepted", "accepted.pdf")

        initial = self.importer.import_staged_pdf(
            first.staged_path,
            self._metadata(doi=DOI_A),
        )
        alternate = self.importer.import_staged_pdf(
            second.staged_path,
            self._metadata(doi=DOI_A),
        )

        self.assertEqual(initial.disposition, LibraryDisposition.NEW_PAPER)
        self.assertEqual(
            alternate.disposition,
            LibraryDisposition.SAME_WORK_DIFFERENT_VERSION,
        )
        self.assertEqual(len(list(self.library.papers_dir.glob("*.pdf"))), 2)

    def test_matching_doi_does_not_override_explicit_title_contradiction(self) -> None:
        _, staged = self._stage_text(f"{TITLE_A} DOI: {DOI_A}")

        result = self.importer.import_staged_pdf(
            staged.staged_path,
            self._metadata(title=TITLE_B, doi=DOI_A),
        )

        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertIn("TitleVerification=not_matched", result.reason)
        self.assertEqual(list(self.library.papers_dir.iterdir()), [])

    def test_trailing_known_author_byline_in_claimed_title_is_accepted(self) -> None:
        title = "新质生产力水平测算与中国经济增长新动能"
        first_page = (
            f"题目： {title}\n"
            "作者： 韩文龙，张瑞生，赵峰\n"
            f"DOI： {DOI_A}"
        )
        _, staged = self._stage_text(f"DOI: {DOI_A}")

        with patch.object(
            AuthorizedFullTextValidator,
            "extract_pdf_first_page_text",
            return_value=first_page,
        ):
            result = self.importer.import_staged_pdf(
                staged.staged_path,
                self._metadata(
                    title=f"{title}-韩文龙",
                    doi=DOI_A,
                    authors=("韩文龙", "张瑞生", "赵峰"),
                ),
            )

        self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
        self.assertIn("DOIVerification=DOI_MATCH", result.reason)
        self.assertIn("TitleVerification=matched", result.reason)

    def test_arbitrary_extra_title_content_is_not_treated_as_byline(self) -> None:
        title = "企业数字化转型与创新"
        first_page = (
            f"题目： {title}\n"
            "作者： 韩文龙，张瑞生\n"
            f"DOI： {DOI_A}"
        )
        _, staged = self._stage_text(f"DOI: {DOI_A}")

        with patch.object(
            AuthorizedFullTextValidator,
            "extract_pdf_first_page_text",
            return_value=first_page,
        ):
            result = self.importer.import_staged_pdf(
                staged.staged_path,
                self._metadata(
                    title=f"{title}-机制研究及其他完全不同内容",
                    doi=DOI_A,
                    authors=("韩文龙", "张瑞生"),
                ),
            )

        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertIn("TitleVerification=not_matched", result.reason)

    def test_f281_body_fragment_pattern_remains_identity_conflict(self) -> None:
        first_page = (
            "企业技术创新、管理者风险偏好与组织韧性\n"
            "作者： 王婉，黄庆华\n"
            f"DOI： {DOI_A}"
        )
        _, staged = self._stage_text(f"DOI: {DOI_A}")

        with patch.object(
            AuthorizedFullTextValidator,
            "extract_pdf_first_page_text",
            return_value=first_page,
        ):
            result = self.importer.import_staged_pdf(
                staged.staged_path,
                self._metadata(
                    title="（2015）Batsiolas（2019）发现技术创新作为一种能力可以提高组织韧性",
                    doi=DOI_A,
                    authors=("冯挺和", "蒋峦等"),
                ),
            )

        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertIn("TitleVerification=not_matched", result.reason)

    def test_doi_match_still_requires_title_noncontradiction_after_byline_rule(self) -> None:
        first_page = (
            f"题目： {TITLE_A}\n"
            "Authors: Ada Author\n"
            f"DOI: {DOI_A}"
        )
        _, staged = self._stage_text(f"DOI: {DOI_A}")

        with patch.object(
            AuthorizedFullTextValidator,
            "extract_pdf_first_page_text",
            return_value=first_page,
        ):
            result = self.importer.import_staged_pdf(
                staged.staged_path,
                self._metadata(title=TITLE_B, doi=DOI_A),
            )

        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertIn("DOIVerification=DOI_MATCH", result.reason)
        self.assertIn("TitleVerification=not_matched", result.reason)

    def test_existing_exact_title_remains_matched_after_byline_rule(self) -> None:
        first_page = (
            f"Title: {TITLE_A}\n"
            "Authors: Ada Author\n"
            f"DOI: {DOI_A}"
        )
        _, staged = self._stage_text(f"DOI: {DOI_A}")

        with patch.object(
            AuthorizedFullTextValidator,
            "extract_pdf_first_page_text",
            return_value=first_page,
        ):
            result = self.importer.import_staged_pdf(
                staged.staged_path,
                self._metadata(doi=DOI_A),
            )

        self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
        self.assertIn("TitleVerification=matched", result.reason)

    def test_direct_external_library_api_cannot_bypass_identity_gate(self) -> None:
        source = write_minimal_pdf(self.root / "direct-no-text.pdf")
        record = LiteratureRecord(
            paper_id=stable_paper_id(doi=DOI_A),
            title=TITLE_A,
            authors=("Ada Author",),
            year="2026",
            doi=DOI_A,
        )

        result = self.library.ingest_external_pdf(source, record)

        self.assertEqual(
            result.disposition,
            LibraryDisposition.EXTERNAL_IDENTITY_UNVERIFIED,
        )
        self.assertEqual(list(self.library.papers_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
