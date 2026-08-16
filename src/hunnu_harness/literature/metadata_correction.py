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

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .library import GlobalPaperLibrary, LibraryCatalogError
from .models import UNKNOWN
from .normalization import (
    normalize_doi,
    normalize_person,
    normalize_title,
    sha256_file,
    stable_paper_id,
)


# Fields a correction is allowed to modify (besides derived/audit fields).
CORRECTABLE_FIELDS: tuple[str, ...] = ("title", "authors", "year", "journal")

# The correction API can only write the fields above.  ``first_author`` is a
# derived projection of ``authors`` and ``metadata_corrections`` is the
# append-only audit trail, so both are handled separately from the immutable
# record snapshot below.
_MUTABLE_RECORD_FIELDS: frozenset[str] = frozenset(
    {*CORRECTABLE_FIELDS, "first_author", "metadata_corrections"}
)

IDENTITY_ESCALATION_REQUIRED = "IDENTITY_ESCALATION_REQUIRED"
IMMUTABLE_INVARIANT_VIOLATION = "IMMUTABLE_INVARIANT_VIOLATION"

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
        "version_role",
        "schema_version",
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
        record_before = deepcopy(record)
        immutable_before = self._immutable_snapshot(record_before)
        corrections_before = deepcopy(record_before.get("metadata_corrections", []))

        managed_path = self._managed_pdf_path(record)
        sha_before = sha256_file(managed_path) if managed_path is not None else UNKNOWN

        previous_values = {
            "title": deepcopy(record_before.get("title", UNKNOWN)),
            "authors": deepcopy(record_before.get("authors", [])),
            "first_author": deepcopy(record_before.get("first_author", UNKNOWN)),
            "year": deepcopy(record_before.get("year", UNKNOWN)),
            "journal": deepcopy(record_before.get("journal", UNKNOWN)),
        }

        candidate = deepcopy(record_before)
        fields_changed = self._apply_correction(candidate, correction)
        self._assert_identity_consistency(
            existing_record=record_before,
            corrected_record=candidate,
            correction=correction,
        )

        # This is the second-layer guard.  It compares deep snapshots rather
        # than checking only types or the handful of fields exposed publicly.
        self._assert_immutable_unchanged(candidate, immutable_before)

        new_values = {
            "title": deepcopy(candidate.get("title", UNKNOWN)),
            "authors": deepcopy(candidate.get("authors", [])),
            "first_author": deepcopy(candidate.get("first_author", UNKNOWN)),
            "year": deepcopy(candidate.get("year", UNKNOWN)),
            "journal": deepcopy(candidate.get("journal", UNKNOWN)),
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
        corrections_history = deepcopy(candidate.get("metadata_corrections", []))
        if not isinstance(corrections_history, list):
            raise LibraryCatalogError(
                f"{IMMUTABLE_INVARIANT_VIOLATION}: metadata_corrections is not a list"
            )
        corrections_history.append(event)
        candidate["metadata_corrections"] = corrections_history
        self._assert_correction_history_append_only(
            corrections_before, candidate["metadata_corrections"]
        )

        # Keep the catalog schema version as an existing immutable contract;
        # this patch does not perform a catalog-schema migration.
        record_index = next(
            index for index, item in enumerate(catalog) if item is record
        )
        catalog[record_index] = candidate

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
    def _apply_correction(
        record: dict[str, Any], correction: BibliographicCorrection
    ) -> list[str]:
        """Apply only public bibliographic fields and return changed names.

        Keeping this mutation in one private seam makes the invariant guard
        independently testable: a test can simulate a future internal bug by
        tampering with an immutable field after this method returns.
        """

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
        return fields_changed

    def _assert_identity_consistency(
        self,
        *,
        existing_record: dict[str, Any],
        corrected_record: dict[str, Any],
        correction: BibliographicCorrection,
    ) -> None:
        """Fail closed if correction would invalidate the existing PaperID."""

        existing_paper_id = str(existing_record.get("paper_id", UNKNOWN))
        doi = normalize_doi(str(existing_record.get("doi", UNKNOWN)))

        # A DOI-anchored PaperID must always agree with the formal stable ID,
        # even for a journal-only correction.  The DOI itself is immutable and
        # is checked again by the deep guard below.
        expected_doi_id = stable_paper_id(doi=doi)
        if doi != UNKNOWN:
            if existing_paper_id != expected_doi_id:
                raise LibraryCatalogError(
                    f"{IDENTITY_ESCALATION_REQUIRED}: existing DOI-based PaperID "
                    f"{existing_paper_id} != {expected_doi_id}"
                )
            return

        # Journal-only corrections do not participate in fallback identity and
        # therefore remain allowed without re-identifying an old record.
        identity_fields_changed = any(
            getattr(correction, field_name) is not None
            for field_name in ("title", "authors", "year")
        )
        if not identity_fields_changed:
            return

        authors = self._authors_for_identity(corrected_record)
        title = str(corrected_record.get("title", UNKNOWN)).strip() or UNKNOWN
        year = str(corrected_record.get("year", UNKNOWN)).strip() or UNKNOWN
        first_author = authors[0] if authors else UNKNOWN
        if (
            normalize_title(title) == UNKNOWN
            or self._unknown(year)
            or normalize_person(first_author) == UNKNOWN
        ):
            raise LibraryCatalogError(
                f"{IDENTITY_ESCALATION_REQUIRED}: corrected no-DOI fallback "
                "identity is incomplete"
            )

        expected_fallback_id = stable_paper_id(
            doi=UNKNOWN,
            title=title,
            year=year,
            authors=authors,
        )
        if expected_fallback_id != existing_paper_id:
            raise LibraryCatalogError(
                f"{IDENTITY_ESCALATION_REQUIRED}: corrected no-DOI fallback "
                f"PaperID would change from {existing_paper_id} to {expected_fallback_id}"
            )

    @staticmethod
    def _authors_for_identity(record: dict[str, Any]) -> tuple[str, ...]:
        raw_authors = record.get("authors", [])
        if isinstance(raw_authors, str):
            values = (raw_authors,)
        else:
            values = tuple(raw_authors or ())
        authors = tuple(str(value).strip() for value in values if str(value).strip())
        if authors:
            return authors
        first_author = str(record.get("first_author", UNKNOWN)).strip()
        return (first_author,) if not BibliographicMetadataCorrector._unknown(first_author) else ()

    @staticmethod
    def _unknown(value: Any) -> bool:
        return value is None or str(value).strip() in {"", UNKNOWN, "Unknown"}

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
    def _immutable_snapshot(record: dict[str, Any]) -> dict[str, Any]:
        """Deep-copy every catalog field not explicitly owned by correction.

        Snapshotting the complete remainder (rather than a few scalar fields)
        covers nested versions/provenance and future acquisition/asset fields.
        """

        return {
            key: deepcopy(value)
            for key, value in record.items()
            if key not in _MUTABLE_RECORD_FIELDS
        }

    @staticmethod
    def _assert_immutable_unchanged(
        record: dict[str, Any], previous_immutable: dict[str, Any]
    ) -> None:
        current_immutable = BibliographicMetadataCorrector._immutable_snapshot(record)
        if current_immutable != previous_immutable:
            changed = sorted(
                set(previous_immutable) | set(current_immutable)
            )
            changed = [
                key
                for key in changed
                if previous_immutable.get(key) != current_immutable.get(key)
            ]
            raise LibraryCatalogError(
                f"{IMMUTABLE_INVARIANT_VIOLATION}: changed fields={','.join(changed)}"
            )

    @staticmethod
    def _assert_correction_history_append_only(
        previous: list[Any], current: Any
    ) -> None:
        if not isinstance(current, list) or len(current) != len(previous) + 1:
            raise LibraryCatalogError(
                f"{IMMUTABLE_INVARIANT_VIOLATION}: metadata_corrections must append one event"
            )
        if current[: len(previous)] != previous:
            raise LibraryCatalogError(
                f"{IMMUTABLE_INVARIANT_VIOLATION}: metadata_corrections history was modified"
            )


__all__ = [
    "BibliographicCorrection",
    "BibliographicCorrectionResult",
    "BibliographicMetadataCorrector",
    "CORRECTABLE_FIELDS",
    "IDENTITY_ESCALATION_REQUIRED",
    "IMMUTABLE_INVARIANT_VIOLATION",
    "IMMUTABLE_FIELDS",
]
