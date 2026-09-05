"""Crash-safe, copy-only Global Paper Library for validated full text.

The run archive remains acquisition evidence.  This module reconciles a
validated copy into a long-lived corpus keyed by the existing PaperID while
retaining SHA-256 as the identity of each concrete file version.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..paths import (
    LIBRARY_CATALOG_CSV,
    LIBRARY_CATALOG_JSONL,
    LIBRARY_IMPORT_STAGING_DIR,
    LIBRARY_ROOT,
    _WINDOWS_EXTENDED_PATH_LIMIT,
    _WINDOWS_MAX_COMPONENT_LENGTH,
    _TRANSACTION_TOKEN_LENGTH,
    _logical_path,
    _transaction_token,
    _windows_io_path,
    _windows_path_units,
    require_output_path,
)
from .fulltext import (
    AuthorizedFullTextValidator,
    ExternalIdentityDecision,
    verify_external_paper_identity,
)
from .models import FullTextFormat, LiteratureRecord, UNKNOWN
from .normalization import normalize_doi, normalize_person, normalize_title, sha256_file, stable_paper_id
from .security import sanitize_url


CATALOG_SCHEMA_VERSION = "0.2.11"
# The 0.2.10 -> 0.2.11 marker bump accompanied the additive bibliographic
# metadata correction (commit 46402be); the catalog record shapes are isomorphic.
# Add a version here only when it shares this lineage and the current reader has
# demonstrated long-term compatibility with it. Unknown versions must still be
# rejected.
COMPATIBLE_CATALOG_SCHEMA_VERSIONS: frozenset[str] = frozenset(
    {"0.2.10", "0.2.11"}
)
_PAPER_ID_RE = re.compile(r"^P[0-9A-F]{12}$")


class LibraryDisposition(str, Enum):
    NEW_PAPER = "NEW_PAPER"
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    SAME_WORK_DIFFERENT_VERSION = "SAME_WORK_DIFFERENT_VERSION"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    INVALID_PDF = "INVALID_PDF"
    INVALID_FULLTEXT = "INVALID_FULLTEXT"
    INSUFFICIENT_IDENTITY = "INSUFFICIENT_IDENTITY"
    EXTERNAL_IDENTITY_UNVERIFIED = "EXTERNAL_IDENTITY_UNVERIFIED"


# The dispositions after which a catalog record exists for the WORK, and so the
# only ones topic filing can act on.
MANAGED_DISPOSITIONS: frozenset[LibraryDisposition] = frozenset(
    {
        LibraryDisposition.NEW_PAPER,
        LibraryDisposition.EXACT_DUPLICATE,
        LibraryDisposition.SAME_WORK_DIFFERENT_VERSION,
    }
)

# Topic filing on the import path reports itself in the download manifest's
# vocabulary (``ClassificationStatus`` and friends).  ``NOT_ATTEMPTED`` is the
# one value that vocabulary lacks: it is what an importer with no classifier
# attached says, so that "nothing was filed" is a statement on the result
# rather than an absence from it.  An import that stays silent about topics
# produced a MANAGED WORK with no topic and nothing that said so.
TOPIC_FILING_NOT_ATTEMPTED = "NOT_ATTEMPTED"
TOPIC_FILING_NO_CLASSIFIER = "NO_TOPIC_CLASSIFIER_ATTACHED"


class LibraryCatalogError(RuntimeError):
    """Raised when the JSONL source of truth cannot be trusted or committed."""


class UnsafeImportSource(ValueError):
    """Raised when an external import bypasses the controlled staging root."""


@dataclass(frozen=True)
class LibraryIngestResult:
    disposition: LibraryDisposition
    paper_id: str
    sha256: str = UNKNOWN
    managed_path: Path | None = None
    notes_path: Path | None = None
    catalog_jsonl_path: Path | None = None
    catalog_csv_path: Path | None = None
    status: str = "REJECTED"
    reason: str = UNKNOWN
    source_unchanged: bool = True
    # Topic filing, in the same field set the download manifest reports
    # (``DownloadManifestEntry``), so an Agent reads one vocabulary whichever
    # way a paper entered the Library.  Defaults describe a rejected import:
    # no WORK, nothing to file.
    classification_status: str = UNKNOWN
    assigned_topics: tuple[str, ...] = ()
    assigned_primary_topic: str = UNKNOWN
    assigned_secondary_topics: tuple[str, ...] = ()
    proposed_topics: tuple[str, ...] = ()
    topic_review_required: bool = False
    topic_metadata_updated: bool = False
    topic_view_updated: bool = False
    classification_reason: str = UNKNOWN
    navigator_metadata_ready: bool = False
    navigator_topic_ready: bool = False
    navigator_fulltext_index_status: str = UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "Disposition": self.disposition.value,
            "PaperID": self.paper_id,
            "SHA256": self.sha256,
            "ManagedPath": str(self.managed_path) if self.managed_path is not None else UNKNOWN,
            "NotesPath": str(self.notes_path) if self.notes_path is not None else UNKNOWN,
            "CatalogJSONLPath": (
                str(self.catalog_jsonl_path) if self.catalog_jsonl_path is not None else UNKNOWN
            ),
            "CatalogCSVPath": str(self.catalog_csv_path) if self.catalog_csv_path is not None else UNKNOWN,
            "Status": self.status,
            "Reason": self.reason,
            "SourceUnchanged": self.source_unchanged,
            "ClassificationStatus": self.classification_status,
            "AssignedTopics": list(self.assigned_topics),
            "AssignedPrimaryTopic": self.assigned_primary_topic,
            "AssignedSecondaryTopics": list(self.assigned_secondary_topics),
            "ProposedTopics": list(self.proposed_topics),
            "TopicReviewRequired": self.topic_review_required,
            "TopicMetadataUpdated": self.topic_metadata_updated,
            "TopicViewUpdated": self.topic_view_updated,
            "ClassificationReason": self.classification_reason,
            "NavigatorMetadataReady": self.navigator_metadata_ready,
            "NavigatorTopicReady": self.navigator_topic_ready,
            "NavigatorFulltextIndexStatus": self.navigator_fulltext_index_status,
        }


@dataclass(frozen=True)
class StagedPaperCandidate:
    source_path: Path
    staged_path: Path
    sha256: str
    sidecar_path: Path
    source_unchanged: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "SourcePath": str(self.source_path),
            "StagedPath": str(self.staged_path),
            "SHA256": self.sha256,
            "SidecarPath": str(self.sidecar_path),
            "SourceUnchanged": self.source_unchanged,
        }


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unknown(value: Any) -> bool:
    return value is None or str(value).strip() in {"", UNKNOWN, "Unknown"}


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _library_sha256_file(path: Path) -> str:
    return sha256_file(_windows_io_path(path))


def _atomic_text(path: Path, content: str) -> None:
    _windows_io_path(path.parent).mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{_transaction_token()}.tmp")
    temporary_io = _windows_io_path(temporary)
    try:
        temporary_io.write_text(content, encoding="utf-8", newline="\n")
        temporary_io.replace(_windows_io_path(path))
    finally:
        temporary_io.unlink(missing_ok=True)


class GlobalPaperLibrary:
    """Filesystem + JSONL paper corpus with fail-closed identity handling."""

    def __init__(
        self,
        library_root: Path = LIBRARY_ROOT,
        *,
        allow_outside_output_for_tests: bool = False,
        make_managed_read_only: bool = True,
    ) -> None:
        try:
            self.library_root = _logical_path(library_root)
        except OSError as exc:
            raise ValueError(
                "Global Paper Library cannot resolve its configured Output Root path. "
                "Set HUNNU_HARNESS_OUTPUT_ROOT to a shorter, shallower directory "
                "and retry."
            ) from exc
        if not allow_outside_output_for_tests:
            try:
                self.library_root = require_output_path(self.library_root, label="Global Paper Library")
            except OSError as exc:
                raise ValueError(
                    "Global Paper Library cannot initialize because its configured Output Root "
                    "path is too deep for Windows path resolution. Set "
                    "HUNNU_HARNESS_OUTPUT_ROOT to a shorter, shallower directory and retry."
                ) from exc
        self.path_base = self.library_root.parent
        self.papers_dir = self.library_root / "papers"
        self.notes_dir = self.library_root / "notes"
        self.catalog_dir = self.library_root / "catalog"
        self.catalog_jsonl_path = self.catalog_dir / LIBRARY_CATALOG_JSONL.name
        self.catalog_csv_path = self.catalog_dir / LIBRARY_CATALOG_CSV.name
        self.import_staging_dir = self.library_root / LIBRARY_IMPORT_STAGING_DIR.name
        self.review_dir = self.import_staging_dir / "review"
        self.transaction_dir = self.library_root / ".transactions"
        self.make_managed_read_only = make_managed_read_only
        self._check_windows_path_lengths()
        self.ensure_contract()

    def ensure_contract(self) -> None:
        for directory in (
            self.papers_dir,
            self.notes_dir,
            self.catalog_dir,
            self.import_staging_dir,
            self.review_dir,
            self.transaction_dir,
        ):
            _windows_io_path(directory).mkdir(parents=True, exist_ok=True)
        if _windows_io_path(self.catalog_csv_path).exists() and not _windows_io_path(
            self.catalog_jsonl_path
        ).exists():
            raise LibraryCatalogError(
                "CSV projection exists without the JSONL source of truth; refusing to guess"
            )
        if not _windows_io_path(self.catalog_jsonl_path).exists():
            self._commit_catalog_only([])
        elif not _windows_io_path(self.catalog_csv_path).exists():
            self._commit_catalog_only(self._load_catalog())

    def _check_windows_path_lengths(self) -> None:
        """Reject only paths that Windows extended-length I/O cannot support."""

        if os.name != "nt":
            return

        alternate_name = f"P{'0' * 12}__{'0' * 64}.pdf"
        staging_name = f"{'0' * 64}.pdf"
        derived_paths = (
            ("library root", self.library_root),
            ("managed papers directory", self.papers_dir),
            ("notes directory", self.notes_dir),
            ("catalog directory", self.catalog_dir),
            ("catalog JSONL", self.catalog_jsonl_path),
            ("catalog CSV", self.catalog_csv_path),
            ("import staging directory", self.import_staging_dir),
            ("review directory", self.review_dir),
            ("transaction directory", self.transaction_dir),
            ("primary managed paper", self.papers_dir / "P000000000000.pdf"),
            ("alternate managed paper", self.papers_dir / alternate_name),
            (
                "catalog transaction JSONL",
                self.transaction_dir / f"papers.{'0' * _TRANSACTION_TOKEN_LENGTH}.jsonl.tmp",
            ),
            (
                "catalog transaction CSV",
                self.transaction_dir / f"papers.{'0' * _TRANSACTION_TOKEN_LENGTH}.csv.tmp",
            ),
            (
                "managed-file transaction",
                self.transaction_dir / f".{alternate_name}.{'0' * _TRANSACTION_TOKEN_LENGTH}.tmp",
            ),
            ("staging candidate", self.import_staging_dir / "candidates" / staging_name),
            (
                "staging sidecar",
                self.import_staging_dir / "candidates" / f"{staging_name[:-4]}.staging.json",
            ),
        )
        for label, path in derived_paths:
            io_path = _windows_io_path(path)
            units = _windows_path_units(io_path)
            if units >= _WINDOWS_EXTENDED_PATH_LIMIT:
                raise ValueError(
                    "Global Paper Library cannot initialize because a derived Windows path "
                    f"is too long ({label}: {units} UTF-16 code units): {path}. "
                    f"The configured Output Root ({self.path_base}) is too deep or too long; set "
                    "HUNNU_HARNESS_OUTPUT_ROOT to a shorter, shallower directory "
                    "(for example C:\\HUNNU-Output) and retry."
                )
            for component in path.parts:
                if component == path.anchor:
                    continue
                component_units = _windows_path_units(component)
                if component_units > _WINDOWS_MAX_COMPONENT_LENGTH:
                    raise ValueError(
                        "Global Paper Library cannot initialize because a derived Windows path "
                        f"component is too long ({label}: {component_units} UTF-16 code units). "
                        f"The configured Output Root ({self.path_base}) is too deep or too long; set "
                        "HUNNU_HARNESS_OUTPUT_ROOT to a shorter, shallower directory "
                        "and retry."
                    )

    def notes_path(self, paper_id: str, *, create: bool = False) -> Path:
        self._require_paper_id(paper_id)
        path = self.notes_dir / paper_id
        if create:
            _windows_io_path(path).mkdir(parents=True, exist_ok=True)
        return path

    def ingest_acquired_fulltext(
        self,
        source: Path,
        record: LiteratureRecord,
        *,
        full_text_format: FullTextFormat,
        source_locator: str = UNKNOWN,
        original_paths: Iterable[Path | str] = (),
    ) -> LibraryIngestResult:
        """Reconcile one already-authorized run artifact after identity lock."""

        return self._ingest(
            source,
            record,
            full_text_format=full_text_format,
            source_type="HARNESS_DOWNLOAD",
            source_locator=source_locator,
            original_paths=original_paths,
            version_role=record.publication_status,
            require_target_identity=True,
        )

    def ingest_external_pdf(
        self,
        source: Path,
        record: LiteratureRecord,
        *,
        source_locator: str = "EXTERNAL_IMPORT_STAGING",
        original_paths: Iterable[Path | str] = (),
        version_role: str = UNKNOWN,
    ) -> LibraryIngestResult:
        """Reconcile one explicit external PDF without mutating its source."""

        return self._ingest(
            source,
            record,
            full_text_format=FullTextFormat.PDF,
            source_type="EXTERNAL_IMPORT",
            source_locator=source_locator,
            original_paths=original_paths,
            version_role=version_role,
            require_target_identity=False,
        )

    def _ingest(
        self,
        source: Path,
        record: LiteratureRecord,
        *,
        full_text_format: FullTextFormat,
        source_type: str,
        source_locator: str,
        original_paths: Iterable[Path | str],
        version_role: str,
        require_target_identity: bool,
    ) -> LibraryIngestResult:
        source = _logical_path(source)
        source_io = _windows_io_path(source)
        source_digest_before = (
            _library_sha256_file(source) if source_io.exists() and source_io.is_file() else UNKNOWN
        )
        validation = AuthorizedFullTextValidator.validate(source_io, full_text_format, record=record)
        if not validation.passed:
            disposition = (
                LibraryDisposition.INVALID_PDF
                if full_text_format == FullTextFormat.PDF
                else LibraryDisposition.INVALID_FULLTEXT
            )
            return self._reject(
                disposition,
                record,
                source=source,
                digest=source_digest_before,
                reason=validation.error,
            )

        identity_problem = self._identity_problem(record, require_target_identity=require_target_identity)
        if identity_problem is not None:
            disposition, reason = identity_problem
            return self._reject(
                disposition,
                record,
                source=source,
                digest=source_digest_before,
                reason=reason,
            )

        external_identity_reason = UNKNOWN
        if source_type == "EXTERNAL_IMPORT":
            verification = verify_external_paper_identity(
                source_io,
                record,
                validation=validation,
            )
            record.content_title_verification = verification.title_verification
            if not verification.verified:
                disposition = (
                    LibraryDisposition.IDENTITY_CONFLICT
                    if verification.decision == ExternalIdentityDecision.CONFLICT
                    else LibraryDisposition.EXTERNAL_IDENTITY_UNVERIFIED
                )
                return self._reject(
                    disposition,
                    record,
                    source=source,
                    digest=source_digest_before,
                    reason=verification.reason,
                )
            external_identity_reason = verification.reason

        paper_id = record.paper_id
        digest = source_digest_before
        extension = source.suffix.casefold()
        entered_at = _timestamp()
        source_paths = _unique_strings([source, *original_paths])
        provenance = self._provenance_event(
            source_type=source_type,
            source_locator=source_locator,
            entered_at=entered_at,
            original_paths=source_paths,
        )

        catalog = self._load_catalog()
        by_paper_id = {item["paper_id"]: item for item in catalog}
        hash_owner = self._hash_owner(catalog, digest)
        if hash_owner is not None and hash_owner[0] != paper_id:
            return self._reject(
                LibraryDisposition.IDENTITY_CONFLICT,
                record,
                source=source,
                digest=digest,
                reason=f"Identical SHA256 is already assigned to {hash_owner[0]}",
            )

        existing = by_paper_id.get(paper_id)
        if existing is None:
            conflicting_paper = self._same_work_other_id(catalog, record)
            if conflicting_paper is not None:
                return self._reject(
                    LibraryDisposition.IDENTITY_CONFLICT,
                    record,
                    source=source,
                    digest=digest,
                    reason=f"Same-work evidence is already cataloged under {conflicting_paper}",
                )
            destination = self.papers_dir / f"{paper_id}{extension}"
            if _windows_io_path(destination).exists():
                return self._reject(
                    LibraryDisposition.IDENTITY_CONFLICT,
                    record,
                    source=source,
                    digest=digest,
                    reason="Primary managed destination exists without a trustworthy catalog entry",
                )
            notes_path = self.notes_path(paper_id)
            version = self._version_record(
                digest=digest,
                destination=destination,
                full_text_format=full_text_format,
                source_type=source_type,
                source_locator=source_locator,
                entered_at=entered_at,
                original_paths=source_paths,
                version_role=version_role,
                provenance=provenance,
            )
            updated = [*catalog, self._paper_record(record, version, notes_path)]
            disposition = LibraryDisposition.NEW_PAPER
        else:
            identity_conflict = self._existing_identity_conflict(existing, record)
            if identity_conflict is not None:
                return self._reject(
                    LibraryDisposition.IDENTITY_CONFLICT,
                    record,
                    source=source,
                    digest=digest,
                    reason=identity_conflict,
                )
            version = next((item for item in existing.get("versions", []) if item.get("sha256") == digest), None)
            if version is not None:
                managed = self._absolute_managed_path(version.get("managed_path", UNKNOWN))
                if (
                    managed is None
                    or not _windows_io_path(managed).exists()
                    or _library_sha256_file(managed) != digest
                ):
                    return self._reject(
                        LibraryDisposition.IDENTITY_CONFLICT,
                        record,
                        source=source,
                        digest=digest,
                        reason="Catalog/file integrity mismatch for an exact duplicate",
                    )
                version["original_paths"] = _unique_strings(
                    [*version.get("original_paths", []), *source_paths]
                )
                events = list(version.get("provenance", []))
                if provenance not in events:
                    events.append(provenance)
                version["provenance"] = events
                existing["original_paths"] = _unique_strings(
                    [*existing.get("original_paths", []), *source_paths]
                )
                updated = catalog
                destination = managed
                notes_path = self.notes_path(paper_id, create=True)
                self._commit_catalog_only(updated)
                return LibraryIngestResult(
                    disposition=LibraryDisposition.EXACT_DUPLICATE,
                    paper_id=paper_id,
                    sha256=digest,
                    managed_path=destination,
                    notes_path=notes_path,
                    catalog_jsonl_path=self.catalog_jsonl_path,
                    catalog_csv_path=self.catalog_csv_path,
                    status="ALREADY_MANAGED",
                    reason=external_identity_reason,
                    source_unchanged=self._source_unchanged(source, source_digest_before),
                )

            destination = self.papers_dir / f"{paper_id}__{digest}{extension}"
            if _windows_io_path(destination).exists():
                return self._reject(
                    LibraryDisposition.IDENTITY_CONFLICT,
                    record,
                    source=source,
                    digest=digest,
                    reason="Alternate-version destination already exists and will not be overwritten",
                )
            notes_path = self.notes_path(paper_id)
            version = self._version_record(
                digest=digest,
                destination=destination,
                full_text_format=full_text_format,
                source_type=source_type,
                source_locator=source_locator,
                entered_at=entered_at,
                original_paths=source_paths,
                version_role=version_role,
                provenance=provenance,
            )
            existing.setdefault("versions", []).append(version)
            existing["same_work_different_version"] = True
            existing["other_version_sha256s"] = [
                item["sha256"] for item in existing["versions"][1:]
            ]
            existing["original_paths"] = _unique_strings(
                [*existing.get("original_paths", []), *source_paths]
            )
            updated = catalog
            disposition = LibraryDisposition.SAME_WORK_DIFFERENT_VERSION

        self._commit_new_file_and_catalog(source, destination, digest, updated, notes_path)
        return LibraryIngestResult(
            disposition=disposition,
            paper_id=paper_id,
            sha256=digest,
            managed_path=destination,
            notes_path=notes_path,
            catalog_jsonl_path=self.catalog_jsonl_path,
            catalog_csv_path=self.catalog_csv_path,
            status="MANAGED",
            reason=external_identity_reason,
            source_unchanged=self._source_unchanged(source, source_digest_before),
        )

    def _identity_problem(
        self,
        record: LiteratureRecord,
        *,
        require_target_identity: bool,
    ) -> tuple[LibraryDisposition, str] | None:
        if require_target_identity and not record.target_identity_confirmed:
            return (
                LibraryDisposition.INSUFFICIENT_IDENTITY,
                "Harness acquisition lacks Target Identity Lock confirmation",
            )
        if not _PAPER_ID_RE.fullmatch(record.paper_id):
            return LibraryDisposition.IDENTITY_CONFLICT, "PaperID does not match the existing stable format"
        if require_target_identity:
            # Existing acquisition finalizers may deliberately retain the
            # identity-locked search PaperID after richer detail metadata is
            # extracted.  That is an existing Harness contract, so Library
            # must reuse it rather than recompute or redesign it.
            return None
        metadata_problem = self._external_metadata_problem(record)
        if metadata_problem is not None:
            return metadata_problem
        expected = stable_paper_id(
            doi=normalize_doi(record.doi),
            title=record.title,
            year=str(record.year),
            authors=tuple(record.authors),
        )
        if record.paper_id != expected:
            return (
                LibraryDisposition.IDENTITY_CONFLICT,
                "PaperID is inconsistent with stable_paper_id() for the supplied identity",
            )
        return None

    @staticmethod
    def _external_metadata_problem(
        record: LiteratureRecord,
    ) -> tuple[LibraryDisposition, str] | None:
        doi = normalize_doi(record.doi)
        if doi == UNKNOWN and (
            normalize_title(record.title) == UNKNOWN
            or _unknown(record.year)
            or normalize_person(record.first_author) == UNKNOWN
        ):
            return (
                LibraryDisposition.INSUFFICIENT_IDENTITY,
                "Fallback PaperID requires title, year, and first author",
            )
        return None

    def _paper_record(
        self,
        record: LiteratureRecord,
        version: dict[str, Any],
        notes_path: Path,
    ) -> dict[str, Any]:
        managed_path = version["managed_path"]
        is_pdf = version["full_text_format"] == FullTextFormat.PDF.value
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "paper_id": record.paper_id,
            "doi": normalize_doi(record.doi),
            "title": record.title,
            "authors": list(record.authors),
            "first_author": record.first_author,
            "year": str(record.year),
            "journal": record.journal,
            "managed_pdf_path": managed_path if is_pdf else UNKNOWN,
            "managed_fulltext_path": managed_path,
            "sha256": version["sha256"],
            "source_type": version["source_type"],
            "source_locator": version["source_locator"],
            "acquired_at": version["acquired_at"],
            "imported_at": version["imported_at"],
            "original_paths": list(version["original_paths"]),
            "version_role": version["version_role"],
            "notes_path": self._relative(notes_path),
            "status": "MANAGED",
            "same_work_different_version": False,
            "other_version_sha256s": [],
            "versions": [version],
        }

    def _version_record(
        self,
        *,
        digest: str,
        destination: Path,
        full_text_format: FullTextFormat,
        source_type: str,
        source_locator: str,
        entered_at: str,
        original_paths: list[str],
        version_role: str,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "sha256": digest,
            "managed_path": self._relative(destination),
            "full_text_format": full_text_format.value,
            "source_type": source_type,
            "source_locator": self._safe_locator(source_locator),
            "acquired_at": entered_at if source_type == "HARNESS_DOWNLOAD" else UNKNOWN,
            "imported_at": entered_at if source_type == "EXTERNAL_IMPORT" else UNKNOWN,
            "original_paths": list(original_paths),
            "version_role": version_role if not _unknown(version_role) else UNKNOWN,
            "status": "MANAGED",
            "provenance": [provenance],
        }

    def _provenance_event(
        self,
        *,
        source_type: str,
        source_locator: str,
        entered_at: str,
        original_paths: list[str],
    ) -> dict[str, Any]:
        return {
            "source_type": source_type,
            "source_locator": self._safe_locator(source_locator),
            "entered_at": entered_at,
            "original_paths": list(original_paths),
        }

    @staticmethod
    def _safe_locator(value: str) -> str:
        text = str(value).strip()
        if text.casefold().startswith(("http://", "https://")):
            return sanitize_url(text)
        return text or UNKNOWN

    def _existing_identity_conflict(
        self,
        existing: Mapping[str, Any],
        record: LiteratureRecord,
    ) -> str | None:
        existing_doi = normalize_doi(str(existing.get("doi", UNKNOWN)))
        current_doi = normalize_doi(record.doi)
        if existing_doi != UNKNOWN or current_doi != UNKNOWN:
            if existing_doi != current_doi:
                return "Same PaperID has conflicting DOI identity"
            return None
        same_fallback = (
            normalize_title(str(existing.get("title", UNKNOWN))) == normalize_title(record.title)
            and str(existing.get("year", UNKNOWN)) == str(record.year)
            and normalize_person(str(existing.get("first_author", UNKNOWN)))
            == normalize_person(record.first_author)
        )
        return None if same_fallback else "Same PaperID has conflicting fallback identity"

    def _same_work_other_id(
        self,
        catalog: Iterable[Mapping[str, Any]],
        record: LiteratureRecord,
    ) -> str | None:
        current_doi = normalize_doi(record.doi)
        current_title = normalize_title(record.title)
        current_author = normalize_person(record.first_author)
        for item in catalog:
            if item.get("paper_id") == record.paper_id:
                continue
            item_doi = normalize_doi(str(item.get("doi", UNKNOWN)))
            if current_doi != UNKNOWN and item_doi != UNKNOWN:
                if item_doi == current_doi:
                    return str(item.get("paper_id"))
                # Two distinct normalized DOIs are stronger evidence than a
                # reused/generic title; do not collapse them by fallback keys.
                continue
            item_title = normalize_title(str(item.get("title", UNKNOWN)))
            if current_title == UNKNOWN or item_title != current_title:
                continue
            same_year = not _unknown(record.year) and str(item.get("year", UNKNOWN)) == str(record.year)
            same_author = (
                current_author != UNKNOWN
                and normalize_person(str(item.get("first_author", UNKNOWN))) == current_author
            )
            if same_year or same_author:
                return str(item.get("paper_id"))
        return None

    @staticmethod
    def _hash_owner(
        catalog: Iterable[Mapping[str, Any]],
        digest: str,
    ) -> tuple[str, Mapping[str, Any]] | None:
        for item in catalog:
            for version in item.get("versions", []):
                if version.get("sha256") == digest:
                    return str(item.get("paper_id")), version
        return None

    def _load_catalog(self) -> list[dict[str, Any]]:
        catalog_path = _windows_io_path(self.catalog_jsonl_path)
        if not catalog_path.exists():
            return []
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            lines = catalog_path.read_text(encoding="utf-8-sig").splitlines()
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict) or not _PAPER_ID_RE.fullmatch(str(item.get("paper_id", ""))):
                    raise LibraryCatalogError(f"Invalid catalog record at line {line_number}")
                paper_id = str(item["paper_id"])
                if paper_id in seen:
                    raise LibraryCatalogError(f"Duplicate PaperID in catalog: {paper_id}")
                seen.add(paper_id)
                declared_schema = item.get("schema_version")
                if declared_schema not in COMPATIBLE_CATALOG_SCHEMA_VERSIONS:
                    found = "absent" if declared_schema is None else repr(declared_schema)
                    raise LibraryCatalogError(
                        f"Catalog record at line {line_number} (PaperID {paper_id}) declares "
                        f"schema_version {found}, but this Harness build accepts only the compatible "
                        "catalog schema versions "
                        f"{sorted(COMPATIBLE_CATALOG_SCHEMA_VERSIONS)!r} and refuses to silently "
                        "reinterpret the record under different assumptions. Load the catalog "
                        "with the Harness version that wrote it, or migrate the catalog deliberately "
                        "before retrying; never hand-edit catalog records."
                    )
                records.append(item)
        except (OSError, json.JSONDecodeError) as exc:
            raise LibraryCatalogError("Global catalog JSONL is unreadable; refusing to guess") from exc
        return records

    def _commit_new_file_and_catalog(
        self,
        source: Path,
        destination: Path,
        digest: str,
        catalog: list[dict[str, Any]],
        notes_path: Path,
    ) -> None:
        json_temp, csv_temp = self._prepare_catalog_files(catalog)
        managed_created = False
        json_committed = False
        try:
            self._copy_no_overwrite_verified(
                source,
                destination,
                digest,
                temporary_dir=self.transaction_dir,
            )
            managed_created = True
            _windows_io_path(notes_path).mkdir(parents=True, exist_ok=True)
            if self.make_managed_read_only:
                destination_io = _windows_io_path(destination)
                destination_io.chmod(destination_io.stat().st_mode & ~stat.S_IWRITE)
            _windows_io_path(json_temp).replace(_windows_io_path(self.catalog_jsonl_path))
            json_committed = True
            _windows_io_path(csv_temp).replace(_windows_io_path(self.catalog_csv_path))
        except Exception:
            if managed_created and not json_committed:
                GlobalPaperLibrary._unlink_read_only_path(destination)
            raise
        finally:
            GlobalPaperLibrary._unlink_read_only_path(json_temp)
            GlobalPaperLibrary._unlink_read_only_path(csv_temp)

    def _commit_catalog_only(self, catalog: list[dict[str, Any]]) -> None:
        json_temp, csv_temp = self._prepare_catalog_files(catalog)
        try:
            _windows_io_path(json_temp).replace(_windows_io_path(self.catalog_jsonl_path))
            _windows_io_path(csv_temp).replace(_windows_io_path(self.catalog_csv_path))
        finally:
            GlobalPaperLibrary._unlink_read_only_path(json_temp)
            GlobalPaperLibrary._unlink_read_only_path(csv_temp)

    def _prepare_catalog_files(self, catalog: list[dict[str, Any]]) -> tuple[Path, Path]:
        ordered = sorted(catalog, key=lambda item: str(item["paper_id"]).casefold())
        transaction = _transaction_token()
        json_temp = self.transaction_dir / f"papers.{transaction}.jsonl.tmp"
        csv_temp = self.transaction_dir / f"papers.{transaction}.csv.tmp"
        json_content = "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in ordered
        )
        _windows_io_path(json_temp).write_text(json_content, encoding="utf-8", newline="\n")
        with _windows_io_path(csv_temp).open("w", encoding="utf-8-sig", newline="") as handle:
            fields = (
                "paper_id",
                "doi",
                "title",
                "authors",
                "first_author",
                "year",
                "journal",
                "managed_pdf_path",
                "managed_fulltext_path",
                "sha256",
                "source_type",
                "source_locator",
                "acquired_at",
                "imported_at",
                "original_paths",
                "version_role",
                "notes_path",
                "status",
                "same_work_different_version",
                "other_version_sha256s",
                "version_count",
            )
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for item in ordered:
                row = {field: item.get(field, UNKNOWN) for field in fields}
                row["authors"] = "; ".join(item.get("authors", []))
                row["original_paths"] = " | ".join(item.get("original_paths", []))
                row["other_version_sha256s"] = " | ".join(
                    item.get("other_version_sha256s", [])
                )
                row["version_count"] = len(item.get("versions", []))
                writer.writerow(row)
        return json_temp, csv_temp

    @staticmethod
    def _copy_no_overwrite_verified(
        source: Path,
        destination: Path,
        digest: str,
        *,
        temporary_dir: Path | None = None,
    ) -> None:
        _windows_io_path(destination.parent).mkdir(parents=True, exist_ok=True)
        if _windows_io_path(destination).exists():
            raise FileExistsError(f"Managed destination exists: {destination}")
        temporary_root = Path(temporary_dir) if temporary_dir is not None else destination.parent
        _windows_io_path(temporary_root).mkdir(parents=True, exist_ok=True)
        temporary = temporary_root / f".{destination.name}.{_transaction_token()}.tmp"
        destination_created = False
        try:
            shutil.copy2(_windows_io_path(source), _windows_io_path(temporary))
            if _library_sha256_file(temporary) != digest:
                raise OSError("COPY verification failed: source and temporary SHA256 differ")
            # The hard-link commit is atomic and fails if the destination appears
            # concurrently.  It never replaces an existing managed paper.
            os.link(_windows_io_path(temporary), _windows_io_path(destination))
            destination_created = True
            if _library_sha256_file(destination) != digest:
                raise OSError("COPY verification failed: destination SHA256 differs")
        except Exception:
            if destination_created:
                GlobalPaperLibrary._unlink_read_only_path(destination)
            raise
        finally:
            GlobalPaperLibrary._unlink_read_only_path(temporary)

    @staticmethod
    def _unlink_read_only_path(path: Path) -> None:
        """Remove a controlled transaction path even when copy2 preserved ReadOnly."""

        path_io = _windows_io_path(path)
        try:
            path_io.chmod(path_io.stat().st_mode | stat.S_IWRITE)
        except FileNotFoundError:
            return
        path_io.unlink(missing_ok=True)

    def _reject(
        self,
        disposition: LibraryDisposition,
        record: LiteratureRecord,
        *,
        source: Path,
        digest: str,
        reason: str,
    ) -> LibraryIngestResult:
        payload = {
            "SchemaVersion": CATALOG_SCHEMA_VERSION,
            "Disposition": disposition.value,
            "PaperID": record.paper_id,
            "SHA256": digest,
            "SourcePath": str(source),
            "Reason": reason,
            "ReviewRequired": disposition
            in {
                LibraryDisposition.IDENTITY_CONFLICT,
                LibraryDisposition.INSUFFICIENT_IDENTITY,
                LibraryDisposition.EXTERNAL_IDENTITY_UNVERIFIED,
            },
            "SourceMutation": False,
            "RecordedAt": _timestamp(),
        }
        review_path = self.review_dir / (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_"
            f"{disposition.value}_{digest[:12] if digest != UNKNOWN else uuid.uuid4().hex[:12]}.json"
        )
        _atomic_text(review_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return LibraryIngestResult(
            disposition=disposition,
            paper_id=record.paper_id,
            sha256=digest,
            catalog_jsonl_path=self.catalog_jsonl_path,
            catalog_csv_path=self.catalog_csv_path,
            status="REVIEW_REQUIRED"
            if payload["ReviewRequired"]
            else "REJECTED",
            reason=reason,
            source_unchanged=self._source_unchanged(source, digest),
        )

    def _relative(self, path: Path) -> str:
        try:
            return _logical_path(path).relative_to(_logical_path(self.path_base)).as_posix()
        except ValueError:
            raise ValueError("Managed library paths must share the configured Output Root")

    def _absolute_managed_path(self, value: Any) -> Path | None:
        text = str(value).strip()
        if not text or text == UNKNOWN:
            return None
        candidate = _logical_path(self.path_base / Path(text))
        papers_dir = _logical_path(self.papers_dir)
        if candidate != papers_dir and not candidate.is_relative_to(papers_dir):
            return None
        return candidate

    @staticmethod
    def _require_paper_id(paper_id: str) -> None:
        if not _PAPER_ID_RE.fullmatch(paper_id):
            raise ValueError("PaperID must use the existing PXXXXXXXXXXXX format")

    @staticmethod
    def _source_unchanged(source: Path, digest_before: str) -> bool:
        source_io = _windows_io_path(source)
        return (
            digest_before != UNKNOWN
            and source_io.exists()
            and source_io.is_file()
            and _library_sha256_file(source) == digest_before
        )


_ATTACH_DEFAULT_CLASSIFIER = object()


class ExternalPaperImporter:
    """Controlled single-file staging/import API; intentionally no scanner.

    Topic filing is part of the import, as it is part of an acquisition
    (AGENTS.md 69): once a staged PDF has become a managed WORK, the same
    post-ingest classification the download path runs decides whether the
    WORK is filed or lands in ``REVIEW_REQUIRED`` with its proposals, and the
    result reports which.  An import that ran no classification produced a
    MANAGED WORK carrying no topic, with nothing on its result saying so.

    The classifier is attached by default only when this importer targets the
    real Library.  An isolated library -- a test tree, a rehearsal -- gets
    none, and then says so on the result rather than staying silent.
    """

    def __init__(
        self,
        library: GlobalPaperLibrary | None = None,
        *,
        topic_classifier: Any = _ATTACH_DEFAULT_CLASSIFIER,
    ) -> None:
        self.library = library or GlobalPaperLibrary()
        self.candidates_dir = self.library.import_staging_dir / "candidates"
        _windows_io_path(self.candidates_dir).mkdir(parents=True, exist_ok=True)
        self._topic_classifier = topic_classifier

    @staticmethod
    def classifies_by_default(library_root: Path) -> bool:
        """Whether an importer for this library attaches the real classifier.

        Only the real Library does.  Any other root is an isolated copy and
        must never reach the frozen taxonomy or the shared topic store through
        a default, exactly as the acquisition path keeps an isolated test run
        away from them.
        """

        return _logical_path(library_root) == _logical_path(LIBRARY_ROOT)

    @property
    def topic_classifier(self) -> Any:
        """The post-ingest classifier, resolved once and only when needed.

        Resolved lazily so that ``library-stage`` -- a COPY into staging --
        never constructs one, and so that an explicit ``None`` stays ``None``.
        """

        if self._topic_classifier is _ATTACH_DEFAULT_CLASSIFIER:
            if self.classifies_by_default(self.library.library_root):
                from .auto_classification import PostAcquisitionClassifier

                self._topic_classifier = PostAcquisitionClassifier()
            else:
                self._topic_classifier = None
        return self._topic_classifier

    def stage_pdf(self, source: Path) -> StagedPaperCandidate:
        source = _logical_path(source)
        source_io = _windows_io_path(source)
        if not source_io.exists() or not source_io.is_file():
            raise FileNotFoundError(f"External paper candidate does not exist: {source}")
        digest_before = _library_sha256_file(source)
        staged = self.candidates_dir / f"{digest_before}.pdf"
        if _windows_io_path(staged).exists():
            if _library_sha256_file(staged) != digest_before:
                raise FileExistsError("Staging destination exists with conflicting content")
        else:
            self.library._copy_no_overwrite_verified(source, staged, digest_before)
        if _library_sha256_file(source) != digest_before:
            raise OSError("External source changed during COPY staging")

        sidecar = staged.with_suffix(".staging.json")
        original_paths: list[str] = []
        if _windows_io_path(sidecar).exists():
            try:
                payload = json.loads(_windows_io_path(sidecar).read_text(encoding="utf-8"))
                original_paths.extend(payload.get("OriginalSourcePaths", []))
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                raise LibraryCatalogError("Staging sidecar is unreadable; refusing to overwrite it") from exc
        original_paths = _unique_strings([*original_paths, source])
        payload = {
            "SchemaVersion": CATALOG_SCHEMA_VERSION,
            "SHA256": digest_before,
            "StagedPath": str(staged),
            "OriginalSourcePaths": original_paths,
            "SourceMutation": False,
            "StagedAt": _timestamp(),
        }
        _atomic_text(sidecar, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return StagedPaperCandidate(
            source_path=source,
            staged_path=staged,
            sha256=digest_before,
            sidecar_path=sidecar,
            source_unchanged=True,
        )

    def import_staged_pdf(
        self,
        staged_pdf: Path,
        metadata: Mapping[str, Any],
    ) -> LibraryIngestResult:
        staged_pdf = _logical_path(staged_pdf)
        candidates_dir = _logical_path(self.candidates_dir)
        if not (
            staged_pdf == candidates_dir
            or staged_pdf.is_relative_to(candidates_dir)
        ):
            raise UnsafeImportSource("External imports must come from library/import_staging/candidates")
        sidecar = staged_pdf.with_suffix(".staging.json")
        if not _windows_io_path(sidecar).exists():
            raise UnsafeImportSource("Staged candidate is missing its COPY provenance sidecar")
        payload = json.loads(_windows_io_path(sidecar).read_text(encoding="utf-8"))
        if payload.get("SHA256") != _library_sha256_file(staged_pdf):
            raise UnsafeImportSource("Staged candidate no longer matches its recorded SHA256")

        staged_digest = str(payload["SHA256"])

        title = str(self._metadata_value(metadata, "title", "Title", default=UNKNOWN)).strip() or UNKNOWN
        year = str(self._metadata_value(metadata, "year", "Year", default=UNKNOWN)).strip() or UNKNOWN
        doi = str(self._metadata_value(metadata, "doi", "DOI", default=UNKNOWN)).strip() or UNKNOWN
        journal = str(self._metadata_value(metadata, "journal", "Journal", default=UNKNOWN)).strip() or UNKNOWN
        raw_authors = self._metadata_value(metadata, "authors", "Authors", default=())
        if isinstance(raw_authors, str):
            authors = tuple(item.strip() for item in raw_authors.split(";") if item.strip())
        elif isinstance(raw_authors, (list, tuple)):
            authors = tuple(str(item).strip() for item in raw_authors if str(item).strip())
        else:
            authors = ()
        claimed_record = LiteratureRecord(
            paper_id=UNKNOWN,
            title=title,
            authors=authors,
            year=year,
            journal=journal,
            doi=normalize_doi(doi),
            source_database="ExternalImport",
            stable_identifier=str(
                self._metadata_value(metadata, "source_locator", "SourceLocator", default=UNKNOWN)
            ),
        )
        validation = AuthorizedFullTextValidator.validate(
            _windows_io_path(staged_pdf),
            FullTextFormat.PDF,
            record=claimed_record,
        )
        if not validation.passed:
            return self.library._reject(
                LibraryDisposition.INVALID_PDF,
                claimed_record,
                source=staged_pdf,
                digest=staged_digest,
                reason=validation.error,
            )
        metadata_problem = self.library._external_metadata_problem(claimed_record)
        if metadata_problem is not None:
            disposition, reason = metadata_problem
            return self.library._reject(
                disposition,
                claimed_record,
                source=staged_pdf,
                digest=staged_digest,
                reason=reason,
            )
        verification = verify_external_paper_identity(
            _windows_io_path(staged_pdf),
            claimed_record,
            validation=validation,
        )
        claimed_record.content_title_verification = verification.title_verification
        if not verification.verified:
            disposition = (
                LibraryDisposition.IDENTITY_CONFLICT
                if verification.decision == ExternalIdentityDecision.CONFLICT
                else LibraryDisposition.EXTERNAL_IDENTITY_UNVERIFIED
            )
            return self.library._reject(
                disposition,
                claimed_record,
                source=staged_pdf,
                digest=staged_digest,
                reason=verification.reason,
            )

        paper_id = stable_paper_id(doi=doi, title=title, year=year, authors=authors)
        claimed_record.paper_id = paper_id
        claimed_record.canonical_paper_id = paper_id
        supplied_original_paths = self._metadata_value(
            metadata,
            "original_paths",
            "OriginalPaths",
            default=(),
        )
        if isinstance(supplied_original_paths, str):
            supplied_original_paths = (supplied_original_paths,)
        elif not isinstance(supplied_original_paths, (list, tuple)):
            supplied_original_paths = ()
        original_paths = _unique_strings(
            [*payload.get("OriginalSourcePaths", []), staged_pdf, *supplied_original_paths]
        )
        ingested = self.library.ingest_external_pdf(
            staged_pdf,
            claimed_record,
            source_locator=claimed_record.stable_identifier,
            original_paths=original_paths,
            version_role=str(
                self._metadata_value(metadata, "version_role", "VersionRole", default=UNKNOWN)
            ),
        )
        return self._file_topics(ingested)

    def _file_topics(self, result: LibraryIngestResult) -> LibraryIngestResult:
        """Run post-ingest topic filing for a managed WORK and report it.

        Mirrors ``LiteratureDownloadManager._classify_archived_work``: a
        classification failure is a metadata outcome, never an import outcome.
        The managed file and the catalog record are already committed and stay
        exactly as they are; what changes is that the result now states
        whether the WORK was filed, needs a person, or was not classified at
        all.  ``TopicReviewRequired`` is true exactly when the WORK still
        carries no topic after this step, so a caller never has to infer that
        from the absence of a field.
        """

        if result.disposition not in MANAGED_DISPOSITIONS:
            return result
        classifier = self.topic_classifier
        if classifier is None:
            return replace(
                result,
                classification_status=TOPIC_FILING_NOT_ATTEMPTED,
                topic_review_required=True,
                classification_reason=TOPIC_FILING_NO_CLASSIFIER,
            )

        from .classification import ClassificationStatus

        try:
            outcome, applied = classifier.classify_after_ingest(
                result.paper_id,
                disposition=result.disposition.value,
            )
        except Exception as exc:
            return replace(
                result,
                classification_status=ClassificationStatus.FAILED_SAFE.value,
                topic_review_required=True,
                classification_reason=f"Topic classification failed: {type(exc).__name__}",
            )

        if outcome.status is ClassificationStatus.SKIPPED_EXISTING:
            assigned = tuple(outcome.existing_topics)
        else:
            assigned = tuple(outcome.assigned_labels)
        readiness = None
        try:
            readiness = classifier.navigator_readiness(result.paper_id)
        except Exception:
            readiness = None
        return replace(
            result,
            classification_status=outcome.status.value,
            assigned_topics=assigned,
            assigned_primary_topic=assigned[0] if assigned else UNKNOWN,
            assigned_secondary_topics=assigned[1:],
            proposed_topics=tuple(outcome.proposed_labels),
            topic_review_required=bool(outcome.review_required) or not assigned,
            topic_metadata_updated=bool(applied.topic_metadata_updated),
            topic_view_updated=bool(applied.topic_view_updated),
            classification_reason=str(applied.reason),
            navigator_metadata_ready=bool(readiness and readiness.metadata_ready),
            navigator_topic_ready=bool(readiness and readiness.topic_ready),
            navigator_fulltext_index_status=(
                readiness.fulltext_index_status if readiness is not None else UNKNOWN
            ),
        )

    @staticmethod
    def _metadata_value(
        metadata: Mapping[str, Any],
        snake: str,
        title: str,
        *,
        default: Any,
    ) -> Any:
        if snake in metadata:
            return metadata[snake]
        if title in metadata:
            return metadata[title]
        return default


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "COMPATIBLE_CATALOG_SCHEMA_VERSIONS",
    "MANAGED_DISPOSITIONS",
    "TOPIC_FILING_NOT_ATTEMPTED",
    "TOPIC_FILING_NO_CLASSIFIER",
    "ExternalPaperImporter",
    "GlobalPaperLibrary",
    "LibraryCatalogError",
    "LibraryDisposition",
    "LibraryIngestResult",
    "StagedPaperCandidate",
    "UnsafeImportSource",
]
