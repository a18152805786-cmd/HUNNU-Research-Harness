from __future__ import annotations

import shutil
import stat
from datetime import datetime
from pathlib import Path

from ..paths import require_output_path
from .fulltext import AuthorizedFullTextValidator, infer_full_text_format
from .library import GlobalPaperLibrary
from .models import AccessDecision, DownloadManifestEntry, FullTextFormat, LiteratureRecord, UNKNOWN
from .normalization import normalized_fulltext_filename, sha256_file
from .security import sanitize_url


class UnauthorizedFullTextError(PermissionError):
    pass


class InvalidFullTextDownload(ValueError):
    pass


class InvalidPDFDownload(InvalidFullTextDownload):
    pass


class LiteratureDownloadManager:
    """Preserve the original download and create a normalized, validated copy."""

    def __init__(
        self,
        downloads_root: Path,
        *,
        allow_outside_project_for_tests: bool = False,
        make_archive_read_only: bool = True,
        global_library: GlobalPaperLibrary | None = None,
    ):
        self.downloads_root = Path(downloads_root).resolve()
        if not allow_outside_project_for_tests:
            self.downloads_root = require_output_path(
                self.downloads_root,
                label="Literature downloads",
            )
        self.raw_dir = self.downloads_root / "raw"
        self.archive_dir = self.downloads_root / "archive"
        self.staging_dir = self.downloads_root / "staging"
        for directory in (self.raw_dir, self.archive_dir, self.staging_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.make_archive_read_only = make_archive_read_only
        self.global_library = global_library

    def archive_authorized_pdf(
        self,
        source: Path,
        record: LiteratureRecord,
        access: AccessDecision,
    ) -> DownloadManifestEntry:
        try:
            return self.archive_authorized_fulltext(
                source,
                record,
                access,
                full_text_format=FullTextFormat.PDF,
            )
        except InvalidFullTextDownload as exc:
            raise InvalidPDFDownload(str(exc)) from exc

    def archive_authorized_fulltext(
        self,
        source: Path,
        record: LiteratureRecord,
        access: AccessDecision,
        *,
        full_text_format: FullTextFormat | None = None,
    ) -> DownloadManifestEntry:
        if not access.full_text_accessible or not access.authorized_access:
            raise UnauthorizedFullTextError("Full text was not confirmed as authorized by the source page")
        source = Path(source).resolve()
        declared_format = full_text_format or access.full_text_format
        resolved_format = infer_full_text_format(source, declared_format)
        validation = AuthorizedFullTextValidator.validate(source, resolved_format, record=record)
        if not validation.passed:
            raise InvalidFullTextDownload(validation.error)

        digest = sha256_file(source)
        original_name = source.name
        raw_path = self._copy_unique(source, self.raw_dir / original_name, digest)
        normalized_name = normalized_fulltext_filename(record, suffix=source.suffix or ".bin")
        normalized_path = self._copy_unique(raw_path, self.archive_dir / normalized_name, digest)
        archived_validation = AuthorizedFullTextValidator.validate(
            normalized_path,
            resolved_format,
            record=record,
        )
        if not archived_validation.passed:
            raise InvalidFullTextDownload(f"Archived copy failed validation: {archived_validation.error}")

        if self.make_archive_read_only:
            for path in (raw_path, normalized_path):
                path.chmod(path.stat().st_mode & ~stat.S_IWRITE)

        timestamp = datetime.now().astimezone().isoformat()
        record.access_type = access.access_type.value
        record.full_text_accessible = True
        record.full_text_downloaded = True
        record.full_text_format = resolved_format.value
        record.original_filename = original_name
        record.normalized_filename = normalized_path.name
        record.local_path = str(normalized_path)
        record.sha256 = digest
        record.file_size_bytes = archived_validation.file_size_bytes
        record.download_timestamp = timestamp
        record.pdf_validation_passed = archived_validation.pdf_validation_passed
        record.file_validation_passed = archived_validation.passed
        record.content_title_verification = archived_validation.content_title_verification

        stable_source = record.stable_identifier
        if stable_source == UNKNOWN and record.source_page != UNKNOWN:
            stable_source = sanitize_url(record.source_page)
        library_disposition = UNKNOWN
        library_status = UNKNOWN
        library_managed_path = UNKNOWN
        library_notes_path = UNKNOWN
        library_catalog_path = UNKNOWN
        library_reason = UNKNOWN
        if self.global_library is not None:
            try:
                library_result = self.global_library.ingest_acquired_fulltext(
                    normalized_path,
                    record,
                    full_text_format=resolved_format,
                    source_locator=stable_source,
                    original_paths=(source, raw_path, normalized_path),
                )
                library_disposition = library_result.disposition.value
                library_status = library_result.status
                if library_result.managed_path is not None:
                    library_managed_path = str(library_result.managed_path)
                if library_result.notes_path is not None:
                    library_notes_path = str(library_result.notes_path)
                if library_result.catalog_jsonl_path is not None:
                    library_catalog_path = str(library_result.catalog_jsonl_path)
                library_reason = library_result.reason
            except Exception as exc:
                # The already-validated run archive remains acquisition evidence.
                # The manifest makes a Library failure explicit rather than
                # silently reclassifying the source download as unsuccessful.
                library_status = "INGEST_FAILED"
                library_reason = f"Global Library ingest failed: {type(exc).__name__}"

        return DownloadManifestEntry(
            paper_id=record.paper_id,
            source=record.source_database,
            title=record.title,
            doi=record.doi,
            access_type=access.access_type.value,
            authorized_access=True,
            original_url_or_stable_identifier=stable_source,
            original_filename=original_name,
            normalized_filename=normalized_path.name,
            download_timestamp=timestamp,
            file_size_bytes=archived_validation.file_size_bytes,
            sha256=digest,
            local_path=str(normalized_path),
            pdf_validation_passed=archived_validation.pdf_validation_passed,
            full_text_format=resolved_format.value,
            file_validation_passed=archived_validation.passed,
            target_identity_confirmed=record.target_identity_confirmed,
            content_title_verification=archived_validation.content_title_verification,
            acquisition_method=record.acquisition_method,
            source_host=record.source_host,
            source_route=record.source_route,
            institutional_route_used=record.institutional_route_used,
            institution=record.institution,
            download_event_emitted=record.download_event_emitted,
            authorized_pdf_response_captured=record.authorized_pdf_response_captured,
            signed_url_persisted=False,
            query_string_persisted=False,
            authorization_header_persisted=False,
            cookie_persisted=False,
            target_title_matched=record.target_title_matched,
            target_doi_matched=record.target_doi_matched,
            official_pdf_action_confirmed=record.official_pdf_action_confirmed,
            source_access_status=record.source_access_status,
            manual_download_required=record.manual_download_required,
            manual_download_handoff_armed=record.manual_download_handoff_armed,
            manual_download_detected=record.manual_download_detected,
            human_download_action=record.human_download_action,
            original_manual_download_preserved=record.original_manual_download_preserved,
            download_initiation_mode=record.download_initiation_mode,
            file_finalization_mode=record.file_finalization_mode,
            unattended_download_attempted=record.unattended_download_attempted,
            automatic_download_initiation=record.automatic_download_initiation,
            automatic_download_detection=record.automatic_download_detection,
            user_native_viewer_click_required=record.user_native_viewer_click_required,
            manual_download_handoff_used=record.manual_download_handoff_used,
            oxford_unattended_download_ready=record.oxford_unattended_download_ready,
            research_chrome_direct_pdf_download_configured=(
                record.research_chrome_direct_pdf_download_configured
            ),
            library_disposition=library_disposition,
            library_status=library_status,
            library_managed_path=library_managed_path,
            library_notes_path=library_notes_path,
            library_catalog_path=library_catalog_path,
            library_reason=library_reason,
        )

    @staticmethod
    def _copy_unique(source: Path, destination: Path, digest: str) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if sha256_file(destination) == digest:
                return destination
            destination = destination.with_name(f"{destination.stem}_{digest[:12]}{destination.suffix}")
        shutil.copy2(source, destination)
        return destination
