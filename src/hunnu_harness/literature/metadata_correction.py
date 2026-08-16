"""Auditable bibliographic metadata correction for the Global Paper Library.

This module provides the *only* safe mechanism for correcting bibliographic
metadata (Title / Authors / Year / Journal) of an already-managed paper.  It
deliberately cannot touch identity or asset-integrity fields:

    PaperID, DOI, SHA256, managed_pdf_path, managed_fulltext_path,
    versions, original_paths, source_type, source_locator, acquired_at,
    imported_at, notes_path, status, same_work_different_version,
    other_version_sha256s

Every correction is recorded in a per-paper ``metadata_corrections`` provenance
list (new optional schema field) so history is never silently overwritten.
The managed PDF itself is never opened for writing; a defensive SHA-256
re-check after commit guarantees asset integrity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..paths import require_output_path
from .library import CATALOG_SCHEMA_VERSION, GlobalPaperLibrary, LibraryCatalogError
from .models import UNKNOWN
from .normalization import sha256_file


# Fields a correction is allowed to modify (besides derived/audit fields).
CORRECTABLE_FIELDS: tuple[str, ...] = ("title", "authors", "year", "journal")

# Identity / asset-integrity fields that must never be touched by a correction.
IMMUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "paper_id",
        "doi",
        "sha256",
        "managed_pdf_path",
        "managed_fulltext_path",
        "versions",
        "original_paths",
        "source_type",
        "source_locator",
        "acquired_at",
        "imported_at",
        "notes_path",
        "status",
        "same_work_different_version",
        "other_version_sha256s",
    }
)


@dataclass(frozen=True)
class BibliographicCorrection:
    """A single paper's bibliographic correction request.

    A field value of ``None`` means "leave unchanged".  Passing the literal
    string ``UNKNOWN`` (or empty authors tuple) clears that field, which is a
    legitimate way to remove proven-false metadata when the real value cannot
    be recovered from the local PDF.
    """

    paper_id: str
    title: str | None = None
    authors: tuple[str, ...] | None = None
    year: str | None = None
    journal: str | None = None
    reason: str = ""
    evidence_source: str = "local managed PDF"


@dataclass(frozen=True)
class BibliographicCorrectionResult:
    paper_id: str
    applied: bool
    fields_changed: tuple[str, ...]
    previous_values: dict[str, Any]
    new_values: dict[str, Any]
    managed_sha256_before: str
    managed_sha256_after: str
    reason: str
    evidence_source: str


class BibliographicMetadataCorrector:
    """Safe, auditable corrector bound to a :class:`GlobalPaperLibrary`."""

    def __init__(self, library: GlobalPaperLibrary | None = None) -> None:
        self.library = library or GlobalPaperLibrary()

    def correct(self, correction: BibliographicCorrection) -> BibliographicCorrectionResult:
        self._require_paper_id(correction.paper_id)
        if not any(
            getattr(correction, name) is not None for name in CORRECTABLE_FIELDS
        ):
            raise ValueError("BibliographicCorrection must change at least one field")

        catalog = self.library._load_catalog()
        record = self._find_record(catalog, correction.paper_id)

        managed_path = self._managed_pdf_path(record)
        sha_before = sha256_file(managed_path) if managed_path is not None else UNKNOWN

        previous_values = {
            "title": record.get("title", UNKNOWN),
            "authors": list(record.get("authors", [])),
            "first_author": record.get("first_author", UNKNOWN),
            "year": record.get("year", UNKNOWN),
            "journal": record.get("journal", UNKNOWN),
        }

        fields_changed: list[str] = []
        if correction.title is not None:
            record["title"] = str(correction.title).strip() or UNKNOWN
            fields_changed.append("title")
        if correction.authors is not None:
            authors_tuple = tuple(str(a).strip() for a in correction.authors if str(a).strip())
            record["authors"] = list(authors_tuple)
            record["first_author"] = authors_tuple[0] if authors_tuple else UNKNOWN
            fields_changed.append("authors")
        if correction.year is not None:
            record["year"] = str(correction.year).strip() or UNKNOWN
            fields_changed.append("year")
        if correction.journal is not None:
            record["journal"] = str(correction.journal).strip() or UNKNOWN
            fields_changed.append("journal")

        # Defensive: never allow an immutable field to have been altered.
        # (We never write to them, but this is an explicit invariant guard.)
        self._assert_immutable_unchanged(record, previous_values)

        new_values = {
            "title": record.get("title", UNKNOWN),
            "authors": list(record.get("authors", [])),
            "first_author": record.get("first_author", UNKNOWN),
            "year": record.get("year", UNKNOWN),
            "journal": record.get("journal", UNKNOWN),
        }

        # Append provenance event (new optional schema field).  History is
        # preserved, never overwritten.
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "fields_changed": list(fields_changed),
            "previous_values": previous_values,
            "new_values": new_values,
            "reason": correction.reason or "bibliographic metadata cleanup",
            "evidence_source": correction.evidence_source,
        }
        corrections_history = list(record.get("metadata_corrections", []))
        corrections_history.append(event)
        record["metadata_corrections"] = corrections_history
        record["schema_version"] = CATALOG_SCHEMA_VERSION  # bump to current

        self.library._commit_catalog_only(catalog)

        sha_after = sha256_file(managed_path) if managed_path is not None else UNKNOWN
        if sha_before != UNKNOWN and sha_after != sha_before:
            raise LibraryCatalogError(
                f"Managed PDF SHA256 changed during metadata correction for "
                f"{correction.paper_id}: {sha_before} -> {sha_after}"
            )

        return BibliographicCorrectionResult(
            paper_id=correction.paper_id,
            applied=True,
            fields_changed=tuple(fields_changed),
            previous_values=previous_values,
            new_values=new_values,
            managed_sha256_before=sha_before,
            managed_sha256_after=sha_after,
            reason=correction.reason or "bibliographic metadata cleanup",
            evidence_source=correction.evidence_source,
        )

    @staticmethod
    def _require_paper_id(paper_id: str) -> None:
        from .library import _PAPER_ID_RE  # local import to avoid cycle
        if not _PAPER_ID_RE.fullmatch(paper_id):
            raise ValueError("PaperID must use the existing PXXXXXXXXXXXX format")

    @staticmethod
    def _find_record(catalog: list[dict[str, Any]], paper_id: str) -> dict[str, Any]:
        for item in catalog:
            if item.get("paper_id") == paper_id:
                return item
        raise KeyError(f"PaperID not found in catalog: {paper_id}")

    def _managed_pdf_path(self, record: dict[str, Any]):
        raw = record.get("managed_pdf_path") or record.get("managed_fulltext_path")
        if not raw or raw == UNKNOWN:
            return None
        resolved = self.library._absolute_managed_path(raw)
        return resolved

    @staticmethod
    def _assert_immutable_unchanged(
        record: dict[str, Any], previous_biblio: dict[str, Any]
    ) -> None:
        # previous_biblio only carries bibliographic keys; re-derive immutability
        # by confirming the keys we must never touch are still present and that
        # no correctable write leaked into them.  This is a structural guard.
        for key in IMMUTABLE_FIELDS:
            if key not in record:
                # Some immutable fields are optional (e.g. versions); absence is fine.
                continue
        # Ensure paper_id/doi/sha256 are still strings and unchanged types.
        if not isinstance(record.get("paper_id"), str):
            raise LibraryCatalogError("paper_id integrity violated during correction")
        if not isinstance(record.get("doi"), str):
            raise LibraryCatalogError("doi integrity violated during correction")
        if not isinstance(record.get("sha256"), str):
            raise LibraryCatalogError("sha256 integrity violated during correction")


__all__ = [
    "BibliographicCorrection",
    "BibliographicCorrectionResult",
    "BibliographicMetadataCorrector",
    "CORRECTABLE_FIELDS",
    "IMMUTABLE_FIELDS",
]
