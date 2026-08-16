import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.literature.library import (
    CATALOG_SCHEMA_VERSION,
    GlobalPaperLibrary,
    LibraryCatalogError,
)
from hunnu_harness.literature.metadata_correction import (
    BibliographicCorrection,
    BibliographicMetadataCorrector,
    CORRECTABLE_FIELDS,
    IMMUTABLE_FIELDS,
)
from hunnu_harness.literature.models import LiteratureRecord
from hunnu_harness.literature.normalization import sha256_file, stable_paper_id

from literature_test_support import write_minimal_pdf_with_text


def paper_record(
    *,
    doi: str = "10.1000/correction-test",
    title: str = "Correction test paper",
    authors: tuple[str, ...] = ("Alpha Author", "Beta Author"),
    year: str = "2026",
    journal: str = "Journal of Corrections",
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


def _ingest(tmp: Path) -> tuple[GlobalPaperLibrary, LiteratureRecord, str]:
    library = GlobalPaperLibrary(
        tmp / "library",
        allow_outside_output_for_tests=True,
        make_managed_read_only=False,
    )
    record = paper_record()
    source = write_minimal_pdf_with_text(
        tmp / "source.pdf", f"{record.title} DOI: {record.doi}"
    )
    library.ingest_external_pdf(source, record)
    sha = sha256_file(library.papers_dir / f"{record.paper_id}.pdf")
    return library, record, sha


def _load_jsonl(library: GlobalPaperLibrary) -> dict:
    lines = library.catalog_jsonl_path.read_text(encoding="utf-8-sig").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    assert len(records) == 1
    return records[0]


def _load_csv_row(library: GlobalPaperLibrary) -> dict:
    with library.catalog_csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    return rows[0]


class BibliographicMetadataCorrectionTests(unittest.TestCase):
    # A. Allows Title / Authors / Year / Journal correction.
    def test_correct_all_four_bibliographic_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, record, sha_before = _ingest(Path(tmp))
            corrector = BibliographicMetadataCorrector(library)
            result = corrector.correct(
                BibliographicCorrection(
                    paper_id=record.paper_id,
                    title="Corrected Title",
                    authors=("New First", "New Second", "New Third"),
                    year="2024",
                    journal="Corrected Journal",
                    reason="pdf-driven cleanup",
                )
            )
            self.assertTrue(result.applied)
            self.assertEqual(
                set(result.fields_changed), {"title", "authors", "year", "journal"}
            )
            rec = _load_jsonl(library)
            self.assertEqual(rec["title"], "Corrected Title")
            self.assertEqual(rec["authors"], ["New First", "New Second", "New Third"])
            self.assertEqual(rec["first_author"], "New First")
            self.assertEqual(rec["year"], "2024")
            self.assertEqual(rec["journal"], "Corrected Journal")

    # B. Forbids changing identity / asset-integrity fields.
    def test_correction_cannot_change_immutable_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, record, sha_before = _ingest(Path(tmp))
            corrector = BibliographicMetadataCorrector(library)
            # The API surface simply does not expose immutable fields.
            self.assertFalse(set(CORRECTABLE_FIELDS) & IMMUTABLE_FIELDS)
            corrector.correct(
                BibliographicCorrection(
                    paper_id=record.paper_id,
                    title="New Title",
                )
            )
            rec = _load_jsonl(library)
            self.assertEqual(rec["paper_id"], record.paper_id)
            self.assertEqual(rec["doi"], record.doi)
            self.assertEqual(rec["sha256"], sha_before)
            self.assertTrue(rec["managed_pdf_path"].endswith(f"{record.paper_id}.pdf"))
            self.assertEqual(rec["source_type"], "EXTERNAL_IMPORT")
            self.assertEqual(len(rec["versions"]), 1)
            self.assertEqual(rec["versions"][0]["sha256"], sha_before)

    def test_correction_rejects_invalid_paper_id_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, _, _ = _ingest(Path(tmp))
            corrector = BibliographicMetadataCorrector(library)
            with self.assertRaises(ValueError):
                corrector.correct(
                    BibliographicCorrection(paper_id="not-a-paper-id", title="x")
                )

    def test_correction_rejects_empty_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, record, _ = _ingest(Path(tmp))
            corrector = BibliographicMetadataCorrector(library)
            with self.assertRaises(ValueError):
                corrector.correct(BibliographicCorrection(paper_id=record.paper_id))

    def test_correction_rejects_unknown_paper_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, _, _ = _ingest(Path(tmp))
            corrector = BibliographicMetadataCorrector(library)
            with self.assertRaises(KeyError):
                corrector.correct(
                    BibliographicCorrection(
                        paper_id="P000000000000", title="x"
                    )
                )

    # C. Managed PDF SHA unchanged after correction.
    def test_managed_pdf_sha_unchanged_after_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, record, sha_before = _ingest(Path(tmp))
            managed = library.papers_dir / f"{record.paper_id}.pdf"
            content_before = managed.read_bytes()
            corrector = BibliographicMetadataCorrector(library)
            result = corrector.correct(
                BibliographicCorrection(
                    paper_id=record.paper_id,
                    title="Different Title",
                    journal="Different Journal",
                )
            )
            self.assertEqual(result.managed_sha256_before, sha_before)
            self.assertEqual(result.managed_sha256_after, sha_before)
            self.assertEqual(managed.read_bytes(), content_before)
            self.assertEqual(sha256_file(managed), sha_before)

    # D. JSONL updated correctly (source of truth).
    def test_jsonl_updated_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, record, _ = _ingest(Path(tmp))
            corrector = BibliographicMetadataCorrector(library)
            corrector.correct(
                BibliographicCorrection(
                    paper_id=record.paper_id,
                    title="JSONL Title",
                    year="2023",
                )
            )
            rec = _load_jsonl(library)
            self.assertEqual(rec["title"], "JSONL Title")
            self.assertEqual(rec["year"], "2023")
            self.assertEqual(rec["paper_id"], record.paper_id)
            self.assertEqual(rec["doi"], record.doi)

    # E. CSV projection updated correctly.
    def test_csv_projection_updated_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, record, _ = _ingest(Path(tmp))
            corrector = BibliographicMetadataCorrector(library)
            corrector.correct(
                BibliographicCorrection(
                    paper_id=record.paper_id,
                    title="CSV Title",
                    authors=("Csv First", "Csv Second"),
                    journal="CSV Journal",
                )
            )
            row = _load_csv_row(library)
            self.assertEqual(row["title"], "CSV Title")
            self.assertEqual(row["authors"], "Csv First; Csv Second")
            self.assertEqual(row["first_author"], "Csv First")
            self.assertEqual(row["journal"], "CSV Journal")
            self.assertEqual(row["paper_id"], record.paper_id)
            self.assertEqual(row["doi"], record.doi)

    # F. Historical correction provenance preserved.
    def test_correction_provenance_preserved_across_multiple_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, record, _ = _ingest(Path(tmp))
            corrector = BibliographicMetadataCorrector(library)
            corrector.correct(
                BibliographicCorrection(
                    paper_id=record.paper_id, title="First Title", reason="first"
                )
            )
            corrector.correct(
                BibliographicCorrection(
                    paper_id=record.paper_id,
                    title="Second Title",
                    journal="Second Journal",
                    reason="second",
                )
            )
            rec = _load_jsonl(library)
            history = rec.get("metadata_corrections", [])
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["new_values"]["title"], "First Title")
            self.assertEqual(history[0]["previous_values"]["title"], record.title)
            self.assertEqual(history[1]["previous_values"]["title"], "First Title")
            self.assertEqual(history[1]["new_values"]["title"], "Second Title")
            self.assertEqual(history[1]["new_values"]["journal"], "Second Journal")
            self.assertEqual(
                set(history[0]["fields_changed"]), {"title"}
            )
            self.assertEqual(
                set(history[1]["fields_changed"]), {"title", "journal"}
            )
            self.assertEqual(rec["schema_version"], CATALOG_SCHEMA_VERSION)

    def test_correction_provenance_records_evidence_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, record, _ = _ingest(Path(tmp))
            corrector = BibliographicMetadataCorrector(library)
            corrector.correct(
                BibliographicCorrection(
                    paper_id=record.paper_id,
                    title="Ev Title",
                    evidence_source="local managed PDF p1",
                )
            )
            rec = _load_jsonl(library)
            self.assertEqual(
                rec["metadata_corrections"][0]["evidence_source"],
                "local managed PDF p1",
            )

    # G. Failed update is atomic (on-disk catalog untouched).
    def test_failed_update_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library, record, _ = _ingest(Path(tmp))
            jsonl_before = library.catalog_jsonl_path.read_text(encoding="utf-8-sig")
            csv_before = library.catalog_csv_path.read_text(encoding="utf-8-sig")
            corrector = BibliographicMetadataCorrector(library)
            with patch.object(
                GlobalPaperLibrary,
                "_commit_catalog_only",
                side_effect=OSError("simulated commit failure"),
            ):
                with self.assertRaises(OSError):
                    corrector.correct(
                        BibliographicCorrection(
                            paper_id=record.paper_id, title="Should Not Persist"
                        )
                    )
            self.assertEqual(
                library.catalog_jsonl_path.read_text(encoding="utf-8-sig"),
                jsonl_before,
            )
            self.assertEqual(
                library.catalog_csv_path.read_text(encoding="utf-8-sig"),
                csv_before,
            )
            rec = _load_jsonl(library)
            self.assertEqual(rec["title"], record.title)

    # Bonus: clearing a proven-false field to UNKNOWN is supported.
    def test_correction_can_clear_false_title_to_unknown(self) -> None:
        from hunnu_harness.literature.models import UNKNOWN

        with tempfile.TemporaryDirectory() as tmp:
            library, record, _ = _ingest(Path(tmp))
            corrector = BibliographicMetadataCorrector(library)
            corrector.correct(
                BibliographicCorrection(
                    paper_id=record.paper_id, title=UNKNOWN, reason="unverifiable"
                )
            )
            rec = _load_jsonl(library)
            self.assertEqual(rec["title"], UNKNOWN)


if __name__ == "__main__":
    unittest.main()
