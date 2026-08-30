from __future__ import annotations

import shutil
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import require_output_path
from .fulltext import AuthorizedFullTextValidator, infer_full_text_format
from .auto_classification import PostAcquisitionClassifier
from .classification import ClassificationStatus
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

    # Keep generated managed paths below the legacy Windows MAX_PATH boundary
    # with room for runtime/internal suffixes.  The budget is applied to the
    # complete destination path in UTF-16 code units, not merely the filename.
    CANONICAL_PATH_SAFE_BUDGET = 240
    _DIGEST_SUFFIX_LENGTHS = (12, 24, 64)

    def __init__(
        self,
        downloads_root: Path,
        *,
        allow_outside_project_for_tests: bool = False,
        make_archive_read_only: bool = True,
        global_library: GlobalPaperLibrary | None = None,
        topic_classifier: PostAcquisitionClassifier | None = None,
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
        self.topic_classifier = topic_classifier

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
        classification_status = UNKNOWN
        assigned_topics: tuple[str, ...] = ()
        assigned_primary_topic = UNKNOWN
        assigned_secondary_topics: tuple[str, ...] = ()
        proposed_topics: tuple[str, ...] = ()
        topic_review_required = False
        topic_metadata_updated = False
        topic_view_updated = False
        classification_reason = UNKNOWN
        navigator_metadata_ready = False
        navigator_topic_ready = False
        navigator_fulltext_index_status = UNKNOWN
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
                if self.topic_classifier is not None and library_result.status != "REJECTED":
                    classified = self._classify_archived_work(
                        library_result.paper_id,
                        library_disposition,
                    )
                    classification_status = classified["status"]
                    assigned_topics = classified["assigned"]
                    assigned_primary_topic = classified["primary"]
                    assigned_secondary_topics = classified["secondary"]
                    proposed_topics = classified["proposed"]
                    topic_review_required = classified["review_required"]
                    topic_metadata_updated = classified["metadata_updated"]
                    topic_view_updated = classified["view_updated"]
                    classification_reason = classified["reason"]
                    navigator_metadata_ready = classified["nav_metadata_ready"]
                    navigator_topic_ready = classified["nav_topic_ready"]
                    navigator_fulltext_index_status = classified["nav_fulltext_status"]
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
            classification_status=classification_status,
            assigned_topics=assigned_topics,
            assigned_primary_topic=assigned_primary_topic,
            assigned_secondary_topics=assigned_secondary_topics,
            proposed_topics=proposed_topics,
            topic_review_required=topic_review_required,
            navigator_metadata_ready=navigator_metadata_ready,
            navigator_topic_ready=navigator_topic_ready,
            navigator_fulltext_index_status=navigator_fulltext_index_status,
            topic_metadata_updated=topic_metadata_updated,
            topic_view_updated=topic_view_updated,
            classification_reason=classification_reason,
        )

    def _classify_archived_work(
        self,
        paper_id: str,
        disposition: str,
    ) -> dict[str, Any]:
        """Classify one just-archived WORK after its identity lock and ingest.

        A classification failure is a metadata outcome, never an acquisition
        one: the authorized download and the archived file are already valid
        evidence and stay untouched whatever happens here.

        Navigator readiness is reported capability by capability.  A newly
        archived work is immediately visible to catalog and topic lookups but is
        not in the derived full-text index until that index is rebuilt, so the
        index keeps its own STALE/FRESH verdict rather than being folded into a
        single "ready" flag.
        """

        failed = {
            "status": "FAILED_SAFE",
            "assigned": (),
            "primary": UNKNOWN,
            "secondary": (),
            "proposed": (),
            "review_required": True,
            "metadata_updated": False,
            "view_updated": False,
            "reason": UNKNOWN,
            "nav_metadata_ready": False,
            "nav_topic_ready": False,
            "nav_fulltext_status": UNKNOWN,
        }
        try:
            result, outcome = self.topic_classifier.classify_after_ingest(
                paper_id,
                disposition=disposition,
            )
        except Exception as exc:
            return {**failed, "reason": f"Topic classification failed: {type(exc).__name__}"}

        if result.status is ClassificationStatus.SKIPPED_EXISTING:
            assigned = tuple(result.existing_topics)
            primary = assigned[0] if assigned else UNKNOWN
            secondary = assigned[1:]
        else:
            assigned = result.assigned_labels
            primary = result.primary_topic
            secondary = result.secondary_labels

        readiness = None
        try:
            readiness = self.topic_classifier.navigator_readiness(paper_id)
        except Exception:
            readiness = None
        return {
            "status": result.status.value,
            "assigned": assigned,
            "primary": primary,
            "secondary": secondary,
            "proposed": result.proposed_labels,
            "review_required": result.review_required,
            "metadata_updated": outcome.topic_metadata_updated,
            "view_updated": outcome.topic_view_updated,
            "reason": outcome.reason,
            "nav_metadata_ready": bool(readiness and readiness.metadata_ready),
            "nav_topic_ready": bool(readiness and readiness.topic_ready),
            "nav_fulltext_status": readiness.fulltext_index_status if readiness else UNKNOWN,
        }

    @staticmethod
    def _windows_path_units(value: Path | str) -> int:
        return len(str(value).encode("utf-16-le")) // 2

    @classmethod
    def _truncate_to_windows_units(cls, value: str, maximum: int) -> str:
        if maximum <= 0:
            return ""
        result: list[str] = []
        used = 0
        for character in value:
            width = cls._windows_path_units(character)
            if used + width > maximum:
                break
            result.append(character)
            used += width
        return "".join(result)

    @classmethod
    def _bounded_destination(
        cls,
        parent: Path,
        filename: str,
        digest: str,
        *,
        force_digest_suffix: bool = False,
        digest_length: int = 12,
    ) -> Path:
        parent = Path(parent).resolve()
        safe_name = Path(filename).name
        suffix = Path(safe_name).suffix
        stem = Path(safe_name).stem.rstrip(" ._") or "artifact"
        direct = parent / safe_name
        if (
            not force_digest_suffix
            and cls._windows_path_units(direct) <= cls.CANONICAL_PATH_SAFE_BUDGET
        ):
            return direct

        token = f"__{digest[:digest_length]}"
        fixed_units = (
            cls._windows_path_units(parent)
            + 1
            + cls._windows_path_units(token)
            + cls._windows_path_units(suffix)
        )
        readable_budget = cls.CANONICAL_PATH_SAFE_BUDGET - fixed_units
        if readable_budget < 1:
            raise InvalidFullTextDownload(
                "Managed archive parent leaves no safe deterministic filename budget"
            )
        readable = cls._truncate_to_windows_units(stem, readable_budget).rstrip(" ._")
        if not readable:
            readable = "a"
        candidate = parent / f"{readable}{token}{suffix}"
        if candidate.parent.resolve() != parent:
            raise InvalidFullTextDownload("Canonical archive destination escaped its managed parent")
        if cls._windows_path_units(candidate) > cls.CANONICAL_PATH_SAFE_BUDGET:
            raise InvalidFullTextDownload("Canonical archive destination exceeds the safe path budget")
        return candidate

    @classmethod
    def _copy_unique(cls, source: Path, destination: Path, digest: str) -> Path:
        source = Path(source).resolve()
        parent = destination.parent.resolve()
        parent.mkdir(parents=True, exist_ok=True)
        candidates = [cls._bounded_destination(parent, destination.name, digest)]
        for length in cls._DIGEST_SUFFIX_LENGTHS:
            try:
                candidate = cls._bounded_destination(
                    parent,
                    destination.name,
                    digest,
                    force_digest_suffix=True,
                    digest_length=length,
                )
            except InvalidFullTextDownload:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            if candidate.exists():
                if sha256_file(candidate) == digest:
                    return candidate
                continue
            try:
                with source.open("rb") as source_handle, candidate.open("xb") as destination_handle:
                    shutil.copyfileobj(source_handle, destination_handle)
                shutil.copystat(source, candidate)
                return candidate
            except FileExistsError:
                if candidate.exists() and sha256_file(candidate) == digest:
                    return candidate
                continue
            except Exception:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        raise InvalidFullTextDownload(
            "Canonical archive filename collision could not be resolved deterministically"
        )
