import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.downloads import (
    InvalidPDFDownload,
    LiteratureDownloadManager,
    UnauthorizedFullTextError,
)
from hunnu_harness.literature.models import AccessDecision, AccessType, LiteratureRecord, RunStatus
from hunnu_harness.literature.normalization import sha256_file
from hunnu_harness.literature.pdf import PDFValidator

from literature_test_support import write_minimal_pdf


def authorized_access() -> AccessDecision:
    return AccessDecision(
        full_text_accessible=True,
        access_type=AccessType.INSTITUTIONAL_AUTHENTICATED,
        authorized_access=True,
        status=RunStatus.SUCCESS,
        reason="Official PDF control",
    )


class PDFValidationTests(unittest.TestCase):
    def test_valid_pdf_has_header_and_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = write_minimal_pdf(Path(tmp) / "valid.pdf")
            result = PDFValidator.validate(pdf)
            self.assertTrue(result.exists)
            self.assertTrue(result.non_zero_size)
            self.assertTrue(result.pdf_header_valid)
            self.assertTrue(result.passed)
            self.assertGreaterEqual(result.page_count or 1, 1)

    def test_html_named_pdf_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "error.pdf"
            fake.write_text("<html><body>access denied</body></html>", encoding="utf-8")
            result = PDFValidator.validate(fake)
            self.assertFalse(result.passed)
            self.assertEqual(result.error, "HTML response detected")

    def test_zero_size_pdf_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.pdf"
            empty.touch()
            result = PDFValidator.validate(empty)
            self.assertFalse(result.non_zero_size)
            self.assertFalse(result.passed)

    def test_missing_pdf_is_rejected(self):
        result = PDFValidator.validate(Path("definitely-missing.pdf"))
        self.assertFalse(result.exists)
        self.assertFalse(result.passed)


class LiteratureDownloadManagerTests(unittest.TestCase):
    def test_authorized_pdf_preserves_original_and_creates_normalized_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "publisher-original.pdf")
            record = LiteratureRecord(
                paper_id="P1",
                title="AI washing and audit monitoring",
                authors=("A. Smith",),
                year="2026",
                doi="10.1000/test",
                source_database="ScienceDirect",
                stable_identifier="S123",
            )
            manager = LiteratureDownloadManager(
                root / "run-downloads",
                allow_outside_project_for_tests=True,
                make_archive_read_only=False,
            )
            entry = manager.archive_authorized_pdf(source, record, authorized_access())
            self.assertTrue(source.exists())
            self.assertTrue((manager.raw_dir / source.name).exists())
            self.assertTrue(Path(entry.local_path).exists())
            self.assertNotEqual(Path(entry.local_path).name, source.name)
            self.assertEqual(entry.sha256, sha256_file(source))
            self.assertTrue(entry.authorized_access)
            self.assertTrue(entry.pdf_validation_passed)
            self.assertEqual(record.original_filename, source.name)

    def test_unauthorized_fulltext_never_copies_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "source.pdf")
            manager = LiteratureDownloadManager(
                root / "downloads", allow_outside_project_for_tests=True, make_archive_read_only=False
            )
            decision = AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.METADATA_ONLY,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="No access",
            )
            with self.assertRaises(UnauthorizedFullTextError):
                manager.archive_authorized_pdf(source, LiteratureRecord("P1"), decision)
            self.assertEqual(list(manager.raw_dir.iterdir()), [])
            self.assertEqual(list(manager.archive_dir.iterdir()), [])

    def test_invalid_download_never_enters_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "error.pdf"
            source.write_text("Access denied", encoding="utf-8")
            manager = LiteratureDownloadManager(
                root / "downloads", allow_outside_project_for_tests=True, make_archive_read_only=False
            )
            with self.assertRaises(InvalidPDFDownload):
                manager.archive_authorized_pdf(source, LiteratureRecord("P1"), authorized_access())
            self.assertEqual(list(manager.raw_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
