"""Durable Oxford native-download handoffs; every resume operation is local.

This module has no browser argument or network client. The persisted proof is
created from the live adapter's real pre-click snapshot, never reconstructed
from a file that the user already downloaded. The receipt hash must be carried
from the stopped run into the explicit resume command.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..browser.manual_download_handoff import ManualDownloadHandoff, ManualDownloadHandoffState
from ..paths import _logical_path, _windows_io_path, require_output_path
from .adapters.oxfordacademic import OxfordAcademicAdapter, _ARTICLE_PATH, _canonical_article_url
from .models import AccessDecision, AccessType, FullTextFormat, LiteratureRecord, LiteratureRunResult, LiteratureSearchRequest, RunStatus, UNKNOWN
from .normalization import normalize_doi, normalize_title, sha256_file
from .security import sanitize_text

_SCHEMA = "oxford-manual-resume-v1"
_DIRECTORY = "manual-download-handoff"
_STATE = "STATE.json"
_RECORD_FIELDS = (
    "paper_id", "title", "authors", "year", "journal", "volume", "issue",
    "pages_or_article_number", "doi", "issn", "language", "publication_type",
    "publication_status", "abstract", "keywords", "source_database", "source_page",
    "stable_identifier", "search_query", "source_host", "source_route",
    "institutional_route_used", "institution", "source_access_status",
)


class ManualResumeError(ValueError):
    """A local handoff is absent, changed, ambiguous or not safe to finalize."""


@dataclass(frozen=True)
class ManualHandoffReceipt:
    state_path: Path
    state_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"StatePath": str(self.state_path), "StateSHA256": self.state_sha256,
                "ManualDownloadHandoffPersisted": True, "ResumeNetworkAccess": False}


def _checked_path(path: Path, *, allow_outside_output_for_tests: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ManualResumeError("Manual resume paths must be absolute")
    # Check the spelling before realpath: a symlink inside Output must not
    # silently select a different run, watch directory or local original.
    for ancestor in (candidate, *candidate.parents):
        io = _windows_io_path(ancestor)
        if io.is_symlink() or (getattr(io, "is_junction", lambda: False)()):
            raise ManualResumeError("Manual resume paths cannot contain links or junctions")
    resolved = _logical_path(candidate)
    if not allow_outside_output_for_tests:
        require_output_path(resolved, label="Manual resume evidence")
    return resolved


def _write_new(path: Path, data: bytes) -> None:
    with _windows_io_path(path).open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _public_text(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _public_text(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_public_text(item) for item in value]
    return sanitize_text(value) if isinstance(value, str) else value


def _public_record(record: LiteratureRecord) -> dict[str, Any]:
    values = {name: getattr(record, name) for name in _RECORD_FIELDS}
    # Canonical source provenance, not the live gateway or signed PDF URL.
    values["source_page"] = _canonical_article_url(record.source_page)
    values["search_query"] = sanitize_text(record.search_query)
    return _public_text(values)


def _validate_payload(payload: Any, state_path: Path, *, allow_outside_output_for_tests: bool) -> tuple[LiteratureSearchRequest, LiteratureRecord, AccessDecision, ManualDownloadHandoffState]:
    if not isinstance(payload, dict) or set(payload) != {"Schema", "OriginalRunRoot", "SavedAtNS", "Request", "Record", "Authorization", "Handoff"} or payload["Schema"] != _SCHEMA:
        raise ManualResumeError("Unsupported manual handoff schema")
    root = _checked_path(Path(payload["OriginalRunRoot"]), allow_outside_output_for_tests=allow_outside_output_for_tests)
    if state_path != root / _DIRECTORY / _STATE:
        raise ManualResumeError("State path is not bound to its original run")
    saved_at = payload["SavedAtNS"]
    if type(saved_at) is not int or not 0 < saved_at <= time.time_ns():
        raise ManualResumeError("Invalid handoff persistence time")
    values = payload["Record"]
    if not isinstance(values, dict) or set(values) != set(_RECORD_FIELDS):
        raise ManualResumeError("Invalid public Oxford record schema")
    if any(not isinstance(values[name], str) for name in _RECORD_FIELDS if name not in {"authors", "keywords", "institutional_route_used"}):
        raise ManualResumeError("Oxford identity metadata must be strings")
    if any(not isinstance(values[name], (tuple, list)) or any(not isinstance(item, str) for item in values[name]) for name in ("authors", "keywords")):
        raise ManualResumeError("Invalid Oxford author or keyword metadata")
    if type(values["institutional_route_used"]) is not bool:
        raise ManualResumeError("Invalid institutional-route evidence")
    record = LiteratureRecord(**{**values, "authors": tuple(values["authors"]), "keywords": tuple(values["keywords"])})
    article = urlsplit(record.source_page)
    match = _ARTICLE_PATH.fullmatch(article.path)
    if (record.source_database != "OxfordAcademic" or not match or record.source_page != _canonical_article_url(record.source_page)
        or any(part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in article.path.split("/")[1:])
        or ".pdf" in article.path.casefold() or record.stable_identifier != match.group("identifier")
        or normalize_doi(record.doi) == UNKNOWN or normalize_doi(record.doi) != record.doi.casefold()
        or not re.fullmatch(r"[A-Za-z0-9_-]+", record.paper_id) or record.title in {"", UNKNOWN}
        or record.source_host != "academic.oup.com"
        or record.source_route != ("HUNNU_GATEWAY_TO_OXFORD" if record.institutional_route_used else "OXFORD_DIRECT")):
        raise ManualResumeError("Oxford public identity or source evidence is invalid")
    proof = payload["Authorization"]
    if not isinstance(proof, dict) or set(proof) != {"FullTextAccessible", "AuthorizedAccess", "TargetIdentityConfirmed", "OfficialPdfActionConfirmed", "PdfViewerOpened", "AccessType", "Status", "FullTextFormat"}:
        raise ManualResumeError("Invalid persisted authorization schema")
    if any(proof[key] is not True for key in ("FullTextAccessible", "AuthorizedAccess", "TargetIdentityConfirmed", "OfficialPdfActionConfirmed", "PdfViewerOpened")) or proof["Status"] != RunStatus.SUCCESS.value or proof["FullTextFormat"] != FullTextFormat.PDF.value:
        raise ManualResumeError("Persisted manual-download authorization was not confirmed")
    access_type = AccessType(proof["AccessType"])
    if access_type not in {AccessType.OPEN_ACCESS, AccessType.INSTITUTIONAL_AUTHENTICATED, AccessType.PUBLIC_FULL_TEXT}:
        raise ManualResumeError("Persisted access type cannot authorize a PDF")
    request = LiteratureSearchRequest.from_mapping(payload["Request"])
    if request.max_downloads < 1 or request.max_downloads_per_run < 1:
        raise ManualResumeError("Original request did not authorize a download")
    if request.dois and record.doi.casefold() not in {normalize_doi(value) for value in request.dois}:
        raise ManualResumeError("Persisted record differs from the requested DOI")
    if request.exact_titles and normalize_title(record.title) not in {normalize_title(value) for value in request.exact_titles}:
        raise ManualResumeError("Persisted record differs from the requested exact title")
    if not isinstance(payload["Handoff"], dict):
        raise ManualResumeError("Missing real handoff snapshot")
    state = ManualDownloadHandoffState.from_mapping(payload["Handoff"])
    if _json_bytes(payload["Handoff"]) != _json_bytes(state.as_dict()):
        raise ManualResumeError("Unexpected or inconsistent handoff snapshot fields")
    watch = _checked_path(state.watch_directory, allow_outside_output_for_tests=True)
    staging = _checked_path(state.staging_directory, allow_outside_output_for_tests=allow_outside_output_for_tests)
    if not staging.is_relative_to(root) or staging == watch or watch.is_relative_to(staging):
        raise ManualResumeError("Manual staging must belong to the original run and preserve the external original")
    if (type(state.before.captured_at_ns) is not int or type(state.armed_at_ns) is not int
        or state.before.directory != state.watch_directory or not 0 < state.before.captured_at_ns <= state.armed_at_ns <= saved_at):
        raise ManualResumeError("Handoff snapshot time or directory does not match")
    seen: set[str] = set()
    for entry in state.before.files:
        name = entry.filename
        if not name or Path(name).name != name or "/" in name or "\\" in name or name in {".", ".."} or name.casefold() in seen or entry.size < 0 or entry.mtime_ns < 0:
            raise ManualResumeError("Invalid before-snapshot entry")
        if name.casefold().endswith(".pdf") and not re.fullmatch(r"[a-f0-9]{64}", entry.sha256):
            raise ManualResumeError("The real before snapshot must hash existing PDFs")
        seen.add(name.casefold())
    record.target_identity_confirmed = True
    record.official_pdf_action_confirmed = True
    record.full_text_accessible = True
    record.acquisition_method = "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF"
    record.manual_download_required = record.manual_download_handoff_armed = record.manual_download_handoff_used = True
    record.user_native_viewer_click_required = True
    access = AccessDecision(True, access_type, True, RunStatus.SUCCESS, "Persisted source authorization for local manual-download finalization")
    return request, record, access, state


def save_oxford_manual_handoff(*, request: LiteratureSearchRequest, record: LiteratureRecord, access: AccessDecision, state: ManualDownloadHandoffState, run_root: Path, allow_outside_output_for_tests: bool = False) -> ManualHandoffReceipt:
    if not (record.target_identity_confirmed and record.official_pdf_action_confirmed and record.manual_download_handoff_armed and record.manual_download_handoff_used and record.acquisition_method == "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF"):
        raise ManualResumeError("The live Oxford adapter did not establish a manual handoff")
    root = _checked_path(run_root, allow_outside_output_for_tests=allow_outside_output_for_tests)
    directory = root / _DIRECTORY
    state_path = directory / _STATE
    payload = {"Schema": _SCHEMA, "OriginalRunRoot": str(root), "SavedAtNS": time.time_ns(),
               "Request": _public_text(request.as_dict()), "Record": _public_record(record), "Handoff": state.as_dict(),
               "Authorization": {"FullTextAccessible": access.full_text_accessible, "AuthorizedAccess": access.authorized_access,
                   "TargetIdentityConfirmed": record.target_identity_confirmed, "OfficialPdfActionConfirmed": record.official_pdf_action_confirmed,
                   "PdfViewerOpened": True, "AccessType": access.access_type.value, "Status": access.status.value, "FullTextFormat": access.full_text_format.value}}
    _validate_payload(payload, state_path, allow_outside_output_for_tests=allow_outside_output_for_tests)
    data = _json_bytes(payload)
    if len(data) > 4_000_000:
        raise ManualResumeError("Manual state is too large to persist safely")
    digest = hashlib.sha256(data).hexdigest()
    _checked_path(directory, allow_outside_output_for_tests=allow_outside_output_for_tests)
    _windows_io_path(directory).mkdir(parents=True, exist_ok=False)
    _write_new(state_path, data)
    _write_new(directory / "STATE.sha256", (digest + "\n").encode("ascii"))
    return ManualHandoffReceipt(state_path, digest)


def get_handoff_receipt(run_root: Path, *, allow_outside_output_for_tests: bool = False) -> ManualHandoffReceipt | None:
    root = _checked_path(run_root, allow_outside_output_for_tests=allow_outside_output_for_tests)
    path = _checked_path(root / _DIRECTORY / _STATE, allow_outside_output_for_tests=allow_outside_output_for_tests)
    if not _windows_io_path(path).exists():
        return None
    sidecar = _checked_path(path.with_name("STATE.sha256"), allow_outside_output_for_tests=allow_outside_output_for_tests)
    expected = _windows_io_path(sidecar).read_text(encoding="ascii").strip()
    _load(path, expected, allow_outside_output_for_tests=allow_outside_output_for_tests)
    return ManualHandoffReceipt(path, expected)


def _load(path: Path, expected: str, *, allow_outside_output_for_tests: bool) -> tuple[LiteratureSearchRequest, LiteratureRecord, AccessDecision, ManualDownloadHandoffState]:
    if not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
        raise ManualResumeError("An explicit original StateSHA256 is required")
    if _windows_io_path(path).stat().st_size > 4_000_000:
        raise ManualResumeError("Manual state is too large")
    data = _windows_io_path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected.casefold():
        raise ManualResumeError("Manual state SHA256 mismatch")
    try:
        return _validate_payload(json.loads(data), path, allow_outside_output_for_tests=allow_outside_output_for_tests)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ManualResumeError(f"Manual state validation failed: {sanitize_text(str(exc))}") from exc


async def resume_oxford_manual_handoff(*, state_path: Path, expected_sha256: str, timeout_seconds: float = 120.0, poll_interval_seconds: float = 0.5, allow_outside_output_for_tests: bool = False) -> LiteratureRunResult:
    path = _checked_path(state_path, allow_outside_output_for_tests=allow_outside_output_for_tests)
    request, record, access, state = _load(path, expected_sha256, allow_outside_output_for_tests=allow_outside_output_for_tests)
    directory = path.parent
    result_path = _checked_path(directory / "RESULT.json", allow_outside_output_for_tests=allow_outside_output_for_tests)
    resume_root = _checked_path(directory.parent / "manual-resume", allow_outside_output_for_tests=allow_outside_output_for_tests)
    lock = _checked_path(directory / "RESUME.lock", allow_outside_output_for_tests=allow_outside_output_for_tests)
    if _windows_io_path(result_path).exists() or _windows_io_path(resume_root).exists():
        raise ManualResumeError("Manual handoff already finalized or has retained partial output; inspect it without repeating")
    try:
        _write_new(lock, expected_sha256.encode("ascii"))
    except FileExistsError as exc:
        raise ManualResumeError("This handoff already has an active or interrupted local resume") from exc
    try:
        if _windows_io_path(result_path).exists() or _windows_io_path(resume_root).exists():
            raise ManualResumeError("Manual handoff already has retained finalization evidence")
        handoff = ManualDownloadHandoff(state.watch_directory, state.staging_directory, allow_outside_output_for_tests=allow_outside_output_for_tests)
        detection = await handoff.wait_for_completed_download(state, timeout_seconds=timeout_seconds, poll_interval_seconds=poll_interval_seconds)
        if detection.new_download_candidates != 1:
            raise ManualResumeError("Exactly one changed PDF candidate is required")
        _checked_path(detection.source_path, allow_outside_output_for_tests=True)
        result = handoff.stage(detection, controlled_filename=f"{record.paper_id}.pdf")
        identity = OxfordAcademicAdapter.validate_pdf_identity(result.staged_path, record)
        if not identity.confirmed or not identity.title_matched or not identity.doi_matched:
            _write_new(result_path, _json_bytes({"Status": "IDENTITY_REJECTED", "StateSHA256": expected_sha256, "StagedSHA256": sha256_file(result.staged_path), "OriginalManualDownloadPreserved": result.original_manual_download_preserved}))
            raise ManualResumeError("Manual Oxford PDF failed local DOI and title identity; staged bytes retained")
        record.target_title_matched, record.target_doi_matched = True, True
        record.manual_download_detected = record.human_download_action = True
        record.original_manual_download_preserved = result.original_manual_download_preserved
        record.download_initiation_mode = result.download_initiation_mode
        record.file_finalization_mode = result.file_finalization_mode
        record.oxford_unattended_download_ready = False
        # Import lazily to keep the production workflow's persistence hook free
        # of a module cycle. No browser is constructed or accepted here.
        from .workflow import finalize_manual_download_handoff_acceptance
        finalized = finalize_manual_download_handoff_acceptance(request=request, record=record, access=access, staged_pdf=result.staged_path, run_root=resume_root, allow_outside_project_for_tests=allow_outside_output_for_tests)
        proof = {**result.as_dict(), "Status": finalized.status.value, "StateSHA256": expected_sha256,
                 "SourceSHA256": sha256_file(result.source_path), "StagedSHA256": sha256_file(result.staged_path),
                 "ResumeRunRoot": str(resume_root), "DownloadCount": len(finalized.downloads), "OxfordUnattendedDownloadReady": False}
        _write_new(result_path, _json_bytes(proof))
        return finalized
    finally:
        try:
            _windows_io_path(lock).unlink(missing_ok=True)
        except OSError:
            # The retained lock is conservative; it must not mask a committed
            # result or the original validation failure.
            pass


__all__ = ["ManualResumeError", "ManualHandoffReceipt", "save_oxford_manual_handoff", "get_handoff_receipt", "resume_oxford_manual_handoff"]
