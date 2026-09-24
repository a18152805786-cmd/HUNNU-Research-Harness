import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from hunnu_harness.literature.downloads import (
    InvalidPDFDownload,
    LiteratureDownloadManager,
    UnauthorizedFullTextError,
)
from hunnu_harness.literature.models import (
    AccessDecision,
    AccessType,
    FullTextFormat,
    LiteratureRecord,
    RunStatus,
)
from hunnu_harness.literature.normalization import sha256_file
from hunnu_harness.literature.pdf import PDFValidator
from hunnu_harness.paths import _logical_path

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
    # Length, in UTF-16 units, of the archive directory the long-path tests
    # write into, whatever the temp root.  Each canonical name they archive
    # (61 to 84 units) then overruns the 240-unit CANONICAL_PATH_SAFE_BUDGET
    # by a fixed margin, and the 39 units left under the budget still hold a
    # shortened name (a readable prefix, "__", a 12-hex digest and the
    # suffix).  A literal rather than derived from the budget: with the
    # budget disabled, the assertions must fail, not the fixture chase an
    # unbounded path.
    LONG_ARCHIVE_DIR_UNITS = 200

    def _long_download_root(self, root: Path) -> Path:
        """Return a download root whose archive directory is LONG_ARCHIVE_DIR_UNITS long.

        The depth follows the resolved temp root, which is what the manager
        measures: a fixed depth left nothing to shorten under Linux's 16-unit
        /tmp and no room for a shortened name under a long TMPDIR.
        """

        base = _logical_path(root)
        units = LiteratureDownloadManager._windows_path_units
        shortfall = self.LONG_ARCHIVE_DIR_UNITS - units(base / "downloads" / "archive")
        if shortfall < 2:
            self.fail(
                f"temp root {base} ({units(base)} UTF-16 units) is too long to place an "
                f"archive directory {self.LONG_ARCHIVE_DIR_UNITS} units deep; "
                "point TMPDIR at a shorter directory"
            )
        # Whole "managed-segment-NN" directories (19 units with a separator)
        # while more than 20 units remain, then one of 1-19 characters that
        # lands the archive directory exactly.
        current = base
        index = 0
        while shortfall > 20:
            current /= f"managed-segment-{index:02d}"
            shortfall -= 19
            index += 1
        current /= f"managed-segment-{index:02d}-"[: shortfall - 1]
        return current / "downloads"

    @staticmethod
    def _long_record(*, title_tail: str = "A") -> LiteratureRecord:
        return LiteratureRecord(
            paper_id=f"P-LONG-{title_tail}",
            title=("A very long canonical literature title prefix " * 5) + title_tail,
            authors=("Gary C. Biddle",),
            year="2009",
            doi=f"10.1000/long-{title_tail}",
            source_database="ScienceDirect",
            stable_identifier=f"S-LONG-{title_tail}",
            target_identity_confirmed=True,
        )

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

    def test_short_canonical_filename_is_not_changed_unnecessarily(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "short.pdf")
            record = LiteratureRecord(
                paper_id="P-SHORT",
                title="Short title",
                authors=("A. Smith",),
                year="2026",
            )
            manager = LiteratureDownloadManager(
                root / "downloads", allow_outside_project_for_tests=True, make_archive_read_only=False
            )
            entry = manager.archive_authorized_pdf(source, record, authorized_access())
            self.assertEqual(Path(entry.local_path).name, "2026_Smith_Short_title.pdf")
            self.assertNotIn("__", Path(entry.local_path).stem)

    def test_li11_style_long_path_is_shortened_and_archived_within_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "li11.pdf")
            manager = LiteratureDownloadManager(
                self._long_download_root(root),
                allow_outside_project_for_tests=True,
                make_archive_read_only=False,
            )
            record = LiteratureRecord(
                paper_id="P48F6ACEC90DC",
                title="How does financial reporting quality relate to investment efficiency?",
                authors=("Gary C. Biddle", "Gilles Hilary", "Rodrigo S. Verdi"),
                year="2009",
                doi="10.1016/j.jacceco.2009.09.001",
                source_database="ScienceDirect",
                stable_identifier="S0165410109000469",
                target_identity_confirmed=True,
            )
            unbounded = manager.archive_dir / "2009_Biddle_How_does_financial_reporting_quality_relate_to_investment_efficiency.pdf"
            self.assertGreater(manager._windows_path_units(unbounded), manager.CANONICAL_PATH_SAFE_BUDGET)
            entry = manager.archive_authorized_pdf(source, record, authorized_access())
            archived = Path(entry.local_path)
            self.assertTrue(archived.is_file())
            self.assertLessEqual(
                manager._windows_path_units(archived), manager.CANONICAL_PATH_SAFE_BUDGET
            )
            self.assertIn(f"__{sha256_file(source)[:12]}", archived.stem)
            self.assertEqual(archived.suffix, ".pdf")

    def test_long_path_filename_is_deterministic_for_repeat_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "repeat.pdf")
            manager = LiteratureDownloadManager(
                self._long_download_root(root),
                allow_outside_project_for_tests=True,
                make_archive_read_only=False,
            )
            first = manager.archive_authorized_pdf(source, self._long_record(), authorized_access())
            second = manager.archive_authorized_pdf(source, self._long_record(), authorized_access())
            self.assertEqual(first.local_path, second.local_path)
            self.assertEqual(first.sha256, second.sha256)

    def test_long_prefix_different_artifacts_have_distinct_canonical_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_source = write_minimal_pdf(root / "one.pdf")
            second_source = write_minimal_pdf(root / "two.pdf")
            second_source.write_bytes(second_source.read_bytes() + b"\n% distinct artifact\n")
            manager = LiteratureDownloadManager(
                self._long_download_root(root),
                allow_outside_project_for_tests=True,
                make_archive_read_only=False,
            )
            first = manager.archive_authorized_pdf(first_source, self._long_record(title_tail="A"), authorized_access())
            second = manager.archive_authorized_pdf(second_source, self._long_record(title_tail="B"), authorized_access())
            self.assertNotEqual(Path(first.local_path).name, Path(second.local_path).name)
            self.assertTrue(Path(first.local_path).is_file())
            self.assertTrue(Path(second.local_path).is_file())

    def test_windows_invalid_title_characters_remain_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "invalid.pdf")
            record = LiteratureRecord(
                paper_id="P-INVALID",
                title='Question?: star* quote" less< greater> pipe| slash/ back\\',
                authors=("A: Author",),
                year="2026",
            )
            manager = LiteratureDownloadManager(
                root / "downloads", allow_outside_project_for_tests=True, make_archive_read_only=False
            )
            entry = manager.archive_authorized_pdf(source, record, authorized_access())
            self.assertFalse(any(character in Path(entry.local_path).name for character in '<>:"/\\|?*'))

    def test_shortening_preserves_pdf_and_caj_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = LiteratureDownloadManager(
                self._long_download_root(root),
                allow_outside_project_for_tests=True,
                make_archive_read_only=False,
            )
            pdf = write_minimal_pdf(root / "long.pdf")
            caj = root / "long.caj"
            caj.write_bytes(b"KDH 2.00 Copyright(C) 2000 CAJCD\ncontent\n")
            pdf_entry = manager.archive_authorized_pdf(pdf, self._long_record(title_tail="PDF"), authorized_access())
            caj_access = replace(authorized_access(), full_text_format=FullTextFormat.CAJ)
            caj_entry = manager.archive_authorized_fulltext(
                caj,
                self._long_record(title_tail="CAJ"),
                caj_access,
                full_text_format=FullTextFormat.CAJ,
            )
            self.assertEqual(Path(pdf_entry.local_path).suffix, ".pdf")
            self.assertEqual(Path(caj_entry.local_path).suffix, ".caj")

    def test_long_chinese_unicode_title_is_deterministic_and_within_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "unicode.pdf")
            record = LiteratureRecord(
                paper_id="P-UNICODE",
                title="人工智能赋能企业全球价值链韧性提升与供应链风险治理机制研究" * 5,
                authors=("王小明",),
                year="2026",
                doi="10.1000/unicode",
                target_identity_confirmed=True,
            )
            manager = LiteratureDownloadManager(
                self._long_download_root(root),
                allow_outside_project_for_tests=True,
                make_archive_read_only=False,
            )
            first = manager.archive_authorized_pdf(source, record, authorized_access())
            second = manager.archive_authorized_pdf(source, record, authorized_access())
            self.assertEqual(first.local_path, second.local_path)
            self.assertLessEqual(
                manager._windows_path_units(first.local_path), manager.CANONICAL_PATH_SAFE_BUDGET
            )

    def test_parent_depth_changes_dynamic_filename_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "depth.pdf")
            record = self._long_record(title_tail="DEPTH")
            shallow = LiteratureDownloadManager(
                root / "shallow", allow_outside_project_for_tests=True, make_archive_read_only=False
            )
            deep = LiteratureDownloadManager(
                self._long_download_root(root),
                allow_outside_project_for_tests=True,
                make_archive_read_only=False,
            )
            shallow_entry = shallow.archive_authorized_pdf(source, record, authorized_access())
            deep_entry = deep.archive_authorized_pdf(source, record, authorized_access())
            self.assertGreater(len(Path(shallow_entry.local_path).name), len(Path(deep_entry.local_path).name))
            self.assertLessEqual(
                deep._windows_path_units(deep_entry.local_path), deep.CANONICAL_PATH_SAFE_BUDGET
            )

    def test_same_sha_reuses_existing_destination_and_different_sha_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_source = write_minimal_pdf(root / "one.pdf")
            second_source = write_minimal_pdf(root / "two.pdf")
            second_source.write_bytes(second_source.read_bytes() + b"\n% different\n")
            record = LiteratureRecord(
                paper_id="P-COLLISION",
                title="Same canonical title",
                authors=("A. Smith",),
                year="2026",
            )
            manager = LiteratureDownloadManager(
                root / "downloads", allow_outside_project_for_tests=True, make_archive_read_only=False
            )
            first = manager.archive_authorized_pdf(first_source, record, authorized_access())
            repeated = manager.archive_authorized_pdf(first_source, record, authorized_access())
            second = manager.archive_authorized_pdf(second_source, record, authorized_access())
            self.assertEqual(first.local_path, repeated.local_path)
            self.assertNotEqual(first.local_path, second.local_path)
            self.assertEqual(sha256_file(Path(first.local_path)), sha256_file(first_source))
            self.assertEqual(sha256_file(Path(second.local_path)), sha256_file(second_source))

    def test_bounded_destination_remains_inside_managed_archive_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_minimal_pdf(root / "containment.pdf")
            manager = LiteratureDownloadManager(
                self._long_download_root(root),
                allow_outside_project_for_tests=True,
                make_archive_read_only=False,
            )
            entry = manager.archive_authorized_pdf(source, self._long_record(title_tail="../escape"), authorized_access())
            self.assertEqual(Path(entry.local_path).resolve().parent, manager.archive_dir.resolve())


if __name__ == "__main__":
    unittest.main()
