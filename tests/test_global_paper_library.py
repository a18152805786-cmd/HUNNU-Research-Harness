import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.cli import build_parser
from hunnu_harness.literature.library import (
    ExternalPaperImporter,
    GlobalPaperLibrary,
    LibraryDisposition,
    UnsafeImportSource,
)
from hunnu_harness.literature.models import FullTextFormat, LiteratureRecord
from hunnu_harness.literature.normalization import sha256_file, stable_paper_id
from hunnu_harness.paths import (
    CORE_ROOT,
    LIBRARY_CATALOG_CSV,
    LIBRARY_CATALOG_JSONL,
    LIBRARY_CATALOG_DIR,
    LIBRARY_IMPORT_STAGING_DIR,
    LIBRARY_NOTES_DIR,
    LIBRARY_PAPERS_DIR,
    LIBRARY_ROOT,
    OUTPUT_ROOT,
    is_within,
)

from literature_test_support import (
    write_minimal_pdf,
    write_minimal_pdf_with_text,
)


def paper_record(
    *,
    doi: str = "10.1000/library-test",
    title: str = "Global library test paper",
    authors: tuple[str, ...] = ("A. Author",),
    year: str = "2026",
    journal: str = "Journal of Library Tests",
) -> LiteratureRecord:
    paper_id = stable_paper_id(doi=doi, title=title, year=year, authors=authors)
    return LiteratureRecord(
        paper_id=paper_id,
        doi=doi,
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        canonical_paper_id=paper_id,
    )


def write_paper_pdf(
    path: Path,
    record: LiteratureRecord,
    *,
    variant: str = "",
) -> Path:
    identity_text = f"{record.title} DOI: {record.doi} {variant}".strip()
    return write_minimal_pdf_with_text(path, identity_text)


class GlobalLibraryPathContractTests(unittest.TestCase):
    def test_every_library_path_is_output_root_only(self) -> None:
        paths = (
            LIBRARY_ROOT,
            LIBRARY_PAPERS_DIR,
            LIBRARY_NOTES_DIR,
            LIBRARY_CATALOG_DIR,
            LIBRARY_CATALOG_JSONL,
            LIBRARY_CATALOG_CSV,
            LIBRARY_IMPORT_STAGING_DIR,
        )
        self.assertTrue(all(is_within(path, OUTPUT_ROOT) for path in paths))
        self.assertTrue(all(not is_within(path, CORE_ROOT) for path in paths))

    def test_notes_contract_is_physically_separate_from_papers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library = GlobalPaperLibrary(
                Path(tmp) / "library",
                allow_outside_output_for_tests=True,
                make_managed_read_only=False,
            )
            record = paper_record()
            source = write_paper_pdf(Path(tmp) / "paper.pdf", record)
            result = library.ingest_external_pdf(source, record)
            self.assertEqual(result.notes_path, library.notes_dir / record.paper_id)
            self.assertTrue(result.notes_path.is_dir())
            self.assertTrue(result.managed_path.is_relative_to(library.papers_dir))
            self.assertFalse(result.notes_path.is_relative_to(library.papers_dir))


class GlobalLibraryReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = GlobalPaperLibrary(
            self.root / "library",
            allow_outside_output_for_tests=True,
            make_managed_read_only=False,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _catalog(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.library.catalog_jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_existing_stable_paper_id_is_used_as_managed_filename(self) -> None:
        record = paper_record(doi="https://doi.org/10.1000/LIBRARY-TEST")
        expected = stable_paper_id(doi="10.1000/library-test")
        source = write_paper_pdf(self.root / "source.pdf", record)
        result = self.library.ingest_external_pdf(source, record)
        self.assertEqual(record.paper_id, expected)
        self.assertEqual(result.managed_path.name, f"{expected}.pdf")

    def test_identity_locked_acquisition_reuses_existing_search_paper_id(self) -> None:
        search_paper_id = stable_paper_id(title="Identity locked title")
        record = LiteratureRecord(
            paper_id=search_paper_id,
            title="Identity locked title",
            authors=("A. Author",),
            year="2026",
            doi="10.1000/detail-doi",
            target_identity_confirmed=True,
            canonical_paper_id=search_paper_id,
        )
        source = write_minimal_pdf(self.root / "locked.pdf")
        result = self.library.ingest_acquired_fulltext(
            source,
            record,
            full_text_format=FullTextFormat.PDF,
        )
        self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
        self.assertEqual(result.managed_path.name, f"{search_paper_id}.pdf")

    def test_identical_sha_is_not_landed_twice_and_provenance_is_merged(self) -> None:
        record = paper_record()
        first = write_paper_pdf(self.root / "first.pdf", record)
        second = write_paper_pdf(self.root / "second.pdf", record)
        initial = self.library.ingest_external_pdf(first, record, original_paths=(first,))
        duplicate = self.library.ingest_external_pdf(second, record, original_paths=(second,))
        self.assertEqual(initial.disposition, LibraryDisposition.NEW_PAPER)
        self.assertEqual(duplicate.disposition, LibraryDisposition.EXACT_DUPLICATE)
        self.assertEqual(len(list(self.library.papers_dir.glob("*.pdf"))), 1)
        catalog = self._catalog()[0]
        self.assertIn(str(first.resolve()), catalog["original_paths"])
        self.assertIn(str(second.resolve()), catalog["original_paths"])

    def test_same_doi_with_different_sha_is_retained_as_an_alternate_version(self) -> None:
        record = paper_record()
        first = write_paper_pdf(self.root / "publisher.pdf", record, variant="publisher")
        second = write_paper_pdf(self.root / "accepted.pdf", record, variant="accepted manuscript")
        initial = self.library.ingest_external_pdf(first, record, version_role="PublisherPDF")
        alternate = self.library.ingest_external_pdf(second, record, version_role="AcceptedManuscript")
        self.assertEqual(alternate.disposition, LibraryDisposition.SAME_WORK_DIFFERENT_VERSION)
        self.assertEqual(initial.managed_path.name, f"{record.paper_id}.pdf")
        self.assertEqual(
            alternate.managed_path.name,
            f"{record.paper_id}__{sha256_file(second)}.pdf",
        )
        self.assertEqual(len(list(self.library.papers_dir.glob("*.pdf"))), 2)
        catalog = self._catalog()[0]
        self.assertTrue(catalog["same_work_different_version"])
        self.assertEqual(len(catalog["versions"]), 2)

    def test_different_paper_ids_never_overwrite_each_other(self) -> None:
        first_record = paper_record(doi="10.1000/one")
        second_record = paper_record(doi="10.1000/two")
        first = write_paper_pdf(self.root / "one.pdf", first_record)
        second = write_paper_pdf(self.root / "two.pdf", second_record)
        first_result = self.library.ingest_external_pdf(first, first_record)
        second_result = self.library.ingest_external_pdf(second, second_record)
        self.assertEqual(second_result.disposition, LibraryDisposition.NEW_PAPER)
        self.assertNotEqual(first_result.managed_path, second_result.managed_path)
        self.assertTrue(first_result.managed_path.exists())
        self.assertTrue(second_result.managed_path.exists())

    def test_same_file_claimed_by_different_paper_id_is_identity_conflict(self) -> None:
        first_record = paper_record(doi="10.1000/one")
        second_record = paper_record(doi="10.1000/two", title="A different identity")
        source = write_paper_pdf(self.root / "same.pdf", first_record)
        self.library.ingest_external_pdf(source, first_record)
        conflict = self.library.ingest_external_pdf(source, second_record)
        self.assertEqual(conflict.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertEqual(len(self._catalog()), 1)

    def test_occupied_primary_destination_fails_closed_without_overwrite(self) -> None:
        record = paper_record()
        occupied = self.library.papers_dir / f"{record.paper_id}.pdf"
        occupied.write_bytes(b"do not overwrite")
        source = write_paper_pdf(self.root / "source.pdf", record)
        result = self.library.ingest_external_pdf(source, record)
        self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
        self.assertEqual(occupied.read_bytes(), b"do not overwrite")
        self.assertEqual(self.library.catalog_jsonl_path.read_text(encoding="utf-8"), "")

    def test_invalid_pdf_never_enters_formal_library_or_catalog(self) -> None:
        source = self.root / "invalid.pdf"
        source.write_text("<html>access denied</html>", encoding="utf-8")
        result = self.library.ingest_external_pdf(source, paper_record())
        self.assertEqual(result.disposition, LibraryDisposition.INVALID_PDF)
        self.assertEqual(list(self.library.papers_dir.iterdir()), [])
        self.assertEqual(self.library.catalog_jsonl_path.read_text(encoding="utf-8"), "")
        self.assertTrue(any(self.library.review_dir.glob("*INVALID_PDF*.json")))

    def test_insufficient_fallback_identity_fails_closed(self) -> None:
        source = write_minimal_pdf(self.root / "unknown.pdf")
        record = LiteratureRecord(
            paper_id=stable_paper_id(title="Title only"),
            title="Title only",
        )
        result = self.library.ingest_external_pdf(source, record)
        self.assertEqual(result.disposition, LibraryDisposition.INSUFFICIENT_IDENTITY)
        self.assertEqual(list(self.library.papers_dir.iterdir()), [])

    def test_catalog_records_managed_and_original_paths_and_csv_projection(self) -> None:
        record = paper_record()
        source = write_paper_pdf(self.root / "original-location.pdf", record)
        result = self.library.ingest_external_pdf(source, record)
        catalog = self._catalog()[0]
        self.assertEqual(catalog["managed_pdf_path"], f"library/papers/{result.paper_id}.pdf")
        self.assertIn(str(source.resolve()), catalog["original_paths"])
        self.assertEqual(catalog["notes_path"], f"library/notes/{result.paper_id}")
        self.assertTrue(self.library.catalog_csv_path.exists())

    def test_catalog_prepare_failure_leaves_no_managed_file_or_catalog(self) -> None:
        record = paper_record()
        source = write_paper_pdf(self.root / "source.pdf", record)
        with patch.object(self.library, "_prepare_catalog_files", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                self.library.ingest_external_pdf(source, record)
        self.assertEqual(list(self.library.papers_dir.iterdir()), [])
        self.assertEqual(self.library.catalog_jsonl_path.read_text(encoding="utf-8"), "")


class ExternalImporterSafetyTests(unittest.TestCase):
    def test_root_cli_exposes_explicit_single_file_stage_and_import_commands(self) -> None:
        parser = build_parser()
        staged = parser.parse_args(["library-stage", "--source", "paper.pdf"])
        imported = parser.parse_args(
            [
                "library-import",
                "--source",
                "staged.pdf",
                "--metadata-json",
                "metadata.json",
            ]
        )
        self.assertEqual(staged.command, "library-stage")
        self.assertEqual(imported.command, "library-import")

    def test_staging_and_import_preserve_source_and_record_original_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = GlobalPaperLibrary(
                root / "library",
                allow_outside_output_for_tests=True,
                make_managed_read_only=False,
            )
            importer = ExternalPaperImporter(library)
            source = write_minimal_pdf_with_text(
                root / "historical-paper.pdf",
                "Historical paper DOI: 10.1000/historical",
            )
            before = source.read_bytes()
            before_mtime = source.stat().st_mtime_ns
            staged = importer.stage_pdf(source)
            result = importer.import_staged_pdf(
                staged.staged_path,
                {
                    "Title": "Historical paper",
                    "Authors": ["H. Author"],
                    "Year": "2020",
                    "DOI": "10.1000/historical",
                    "Journal": "History Journal",
                    "VersionRole": "PublisherPDF",
                    "SourceLocator": "WorkBuddyInventory",
                },
            )
            self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(source.stat().st_mtime_ns, before_mtime)
            catalog = json.loads(library.catalog_jsonl_path.read_text(encoding="utf-8"))
            self.assertIn(str(source.resolve()), catalog["original_paths"])
            self.assertIn(str(staged.staged_path.resolve()), catalog["original_paths"])

    def test_importer_rejects_direct_non_staged_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = GlobalPaperLibrary(
                root / "library",
                allow_outside_output_for_tests=True,
                make_managed_read_only=False,
            )
            source = write_minimal_pdf(root / "outside-staging.pdf")
            with self.assertRaises(UnsafeImportSource):
                ExternalPaperImporter(library).import_staged_pdf(
                    source,
                    {"Title": "x", "Authors": ["A"], "Year": "2026"},
                )


if __name__ == "__main__":
    unittest.main()
