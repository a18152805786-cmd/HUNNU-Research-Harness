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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..paths import (
    LIBRARY_CATALOG_CSV,
    LIBRARY_CATALOG_JSONL,
    LIBRARY_IMPORT_STAGING_DIR,
    LIBRARY_ROOT,
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


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class GlobalPaperLibrary:
    """Filesystem + JSONL paper corpus with fail-closed identity handling."""

    def __init__(
        self,
        library_root: Path = LIBRARY_ROOT,
        *,
        allow_outside_output_for_tests: bool = False,
        make_managed_read_only: bool = True,
    ) -> None:
        self.library_root = Path(library_root).resolve()
        if not allow_outside_output_for_tests:
            self.library_root = require_output_path(self.library_root, label="Global Paper Library")
        self.path_base = self.library_root.parent
        self.papers_dir = self.library_root / "papers"
        self.notes_dir = self.library_root / "notes"
        self.catalog_dir = self.library_root / "catalog"
        self.catalog_jsonl_path = self.catalog_dir / LIBRARY_CATALOG_JSONL.name
        self.catalog_csv_path = self.catalog_dir / LIBRARY_CATALOG_CSV.name
        self.import_staging_dir = self.library_root / LIBRARY_IMPORT_STAGING_DIR.name
        self.review_dir = self.import_staging_dir / "review"
        self.transaction_dir = self.import_staging_dir / ".transactions"
        self.make_managed_read_only = make_managed_read_only
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
            directory.mkdir(parents=True, exist_ok=True)
        if self.catalog_csv_path.exists() and not self.catalog_jsonl_path.exists():
            raise LibraryCatalogError(
                "CSV projection exists without the JSONL source of truth; refusing to guess"
            )
        if not self.catalog_jsonl_path.exists():
            self._commit_catalog_only([])
        elif not self.catalog_csv_path.exists():
            self._commit_catalog_only(self._load_catalog())

    def notes_path(self, paper_id: str, *, create: bool = False) -> Path:
        self._require_paper_id(paper_id)
        path = self.notes_dir / paper_id
        if create:
            path.mkdir(parents=True, exist_ok=True)
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
        source = Path(source).resolve()
        source_digest_before = sha256_file(source) if source.exists() and source.is_file() else UNKNOWN
        validation = AuthorizedFullTextValidator.validate(source, full_text_format, record=record)
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
                source,
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
            if destination.exists():
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
                if managed is None or not managed.exists() or sha256_file(managed) != digest:
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
            if destination.exists():
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
        if not self.catalog_jsonl_path.exists():
            return []
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            lines = self.catalog_jsonl_path.read_text(encoding="utf-8-sig").splitlines()
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
            notes_path.mkdir(parents=True, exist_ok=True)
            if self.make_managed_read_only:
                destination.chmod(destination.stat().st_mode & ~stat.S_IWRITE)
            json_temp.replace(self.catalog_jsonl_path)
            json_committed = True
            csv_temp.replace(self.catalog_csv_path)
        except Exception:
            if managed_created and not json_committed:
                destination.chmod(destination.stat().st_mode | stat.S_IWRITE)
                destination.unlink(missing_ok=True)
            raise
        finally:
            json_temp.unlink(missing_ok=True)
            csv_temp.unlink(missing_ok=True)

    def _commit_catalog_only(self, catalog: list[dict[str, Any]]) -> None:
        json_temp, csv_temp = self._prepare_catalog_files(catalog)
        try:
            json_temp.replace(self.catalog_jsonl_path)
            csv_temp.replace(self.catalog_csv_path)
        finally:
            json_temp.unlink(missing_ok=True)
            csv_temp.unlink(missing_ok=True)

    def _prepare_catalog_files(self, catalog: list[dict[str, Any]]) -> tuple[Path, Path]:
        ordered = sorted(catalog, key=lambda item: str(item["paper_id"]).casefold())
        transaction = f"{os.getpid()}.{uuid.uuid4().hex}"
        json_temp = self.transaction_dir / f"papers.{transaction}.jsonl.tmp"
        csv_temp = self.transaction_dir / f"papers.{transaction}.csv.tmp"
        json_content = "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in ordered
        )
        json_temp.write_text(json_content, encoding="utf-8", newline="\n")
        with csv_temp.open("w", encoding="utf-8-sig", newline="") as handle:
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
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"Managed destination exists: {destination}")
        temporary_root = Path(temporary_dir) if temporary_dir is not None else destination.parent
        temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = temporary_root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        destination_created = False
        try:
            shutil.copy2(source, temporary)
            if sha256_file(temporary) != digest:
                raise OSError("COPY verification failed: source and temporary SHA256 differ")
            # The hard-link commit is atomic and fails if the destination appears
            # concurrently.  It never replaces an existing managed paper.
            os.link(temporary, destination)
            destination_created = True
            if sha256_file(destination) != digest:
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

        try:
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
        except FileNotFoundError:
            return
        path.unlink(missing_ok=True)

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
            return path.resolve().relative_to(self.path_base.resolve()).as_posix()
        except ValueError:
            raise ValueError("Managed library paths must share the configured Output Root")

    def _absolute_managed_path(self, value: Any) -> Path | None:
        text = str(value).strip()
        if not text or text == UNKNOWN:
            return None
        candidate = (self.path_base / Path(text)).resolve()
        if candidate != self.papers_dir and not candidate.is_relative_to(self.papers_dir):
            return None
        return candidate

    @staticmethod
    def _require_paper_id(paper_id: str) -> None:
        if not _PAPER_ID_RE.fullmatch(paper_id):
            raise ValueError("PaperID must use the existing PXXXXXXXXXXXX format")

    @staticmethod
    def _source_unchanged(source: Path, digest_before: str) -> bool:
        return (
            digest_before != UNKNOWN
            and source.exists()
            and source.is_file()
            and sha256_file(source) == digest_before
        )


class ExternalPaperImporter:
    """Controlled single-file staging/import API; intentionally no scanner."""

    def __init__(self, library: GlobalPaperLibrary | None = None) -> None:
        self.library = library or GlobalPaperLibrary()
        self.candidates_dir = self.library.import_staging_dir / "candidates"
        self.candidates_dir.mkdir(parents=True, exist_ok=True)

    def stage_pdf(self, source: Path) -> StagedPaperCandidate:
        source = Path(source).resolve()
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"External paper candidate does not exist: {source}")
        digest_before = sha256_file(source)
        staged = self.candidates_dir / f"{digest_before}.pdf"
        if staged.exists():
            if sha256_file(staged) != digest_before:
                raise FileExistsError("Staging destination exists with conflicting content")
        else:
            self.library._copy_no_overwrite_verified(source, staged, digest_before)
        if sha256_file(source) != digest_before:
            raise OSError("External source changed during COPY staging")

        sidecar = staged.with_suffix(".staging.json")
        original_paths: list[str] = []
        if sidecar.exists():
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
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
        staged_pdf = Path(staged_pdf).resolve()
        if not (
            staged_pdf == self.candidates_dir.resolve()
            or staged_pdf.is_relative_to(self.candidates_dir.resolve())
        ):
            raise UnsafeImportSource("External imports must come from library/import_staging/candidates")
        sidecar = staged_pdf.with_suffix(".staging.json")
        if not sidecar.exists():
            raise UnsafeImportSource("Staged candidate is missing its COPY provenance sidecar")
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        if payload.get("SHA256") != sha256_file(staged_pdf):
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
            staged_pdf,
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
            staged_pdf,
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
        return self.library.ingest_external_pdf(
            staged_pdf,
            claimed_record,
            source_locator=claimed_record.stable_identifier,
            original_paths=original_paths,
            version_role=str(
                self._metadata_value(metadata, "version_role", "VersionRole", default=UNKNOWN)
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
    "ExternalPaperImporter",
    "GlobalPaperLibrary",
    "LibraryCatalogError",
    "LibraryDisposition",
    "LibraryIngestResult",
    "StagedPaperCandidate",
    "UnsafeImportSource",
]
