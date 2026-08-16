from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..paths import RUNS_ROOT, require_output_path
from .models import DownloadManifestEntry, LiteratureRecord, LiteratureSearchRequest, QueryLogEntry, UNKNOWN
from .security import sanitize_value


UPGRADE_RUN_ROOT = RUNS_ROOT / "Harness_V02_Literature_Acquisition_Upgrade"
CNKI_UPGRADE_RUN_ROOT = RUNS_ROOT / "Harness_V021_CNKI_Adapter"
INSTITUTIONAL_UPGRADE_RUN_ROOT = (
    RUNS_ROOT / "Harness_V022_HUNNU_Institutional_Access_Resolver"
)
OXFORD_UPGRADE_RUN_ROOT = RUNS_ROOT / "Harness_V026_Oxford_Academic_Adapter"

QUERY_LOG_FIELDS = (
    "Timestamp",
    "Source",
    "OriginalResearchRequest",
    "GeneratedQuery",
    "Filters",
    "ResultsReturned",
    "ResultsInspected",
    "Errors",
)
SEARCH_RESULT_FIELDS = (
    "PaperID",
    "Title",
    "Authors",
    "Year",
    "Journal",
    "DOI",
    "Language",
    "PublicationStatus",
    "Source",
    "SearchQuery",
    "AbstractAvailable",
    "FullTextAccessible",
    "FullTextDownloaded",
    "FullTextFormat",
    "RelevanceScore",
    "ScreeningDecision",
    "ScreeningReason",
    "LocalPath",
    "SHA256",
    "FileValidationPassed",
    "TargetIdentityConfirmed",
    "ContentTitleVerification",
)
SCREENING_FIELDS = (
    "PaperID",
    "Title",
    "RelevanceScore",
    "ScreeningDecision",
    "ScreeningReason",
    "AI_ASSISTED",
    "DuplicateDetected",
    "DuplicateReason",
    "CanonicalPaperID",
    "SameWorkDifferentVersion",
    "ArchivedAsAlternateVersion",
)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


class LiteratureArtifactWriter:
    """Write a complete, reproducible literature run without touching v0.1 artifacts."""

    def __init__(
        self,
        run_root: Path = UPGRADE_RUN_ROOT,
        *,
        allow_outside_project_for_tests: bool = False,
    ):
        self.run_root = Path(run_root).resolve()
        if not allow_outside_project_for_tests:
            self.run_root = require_output_path(self.run_root, label="Literature run artifacts")
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.downloads_dir = self.run_root / "downloads"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

    @property
    def search_request_path(self) -> Path:
        return self.run_root / "SEARCH_REQUEST.json"

    @property
    def query_log_path(self) -> Path:
        return self.run_root / "SEARCH_QUERY_LOG.csv"

    @property
    def search_results_path(self) -> Path:
        return self.run_root / "SEARCH_RESULTS.csv"

    @property
    def screening_path(self) -> Path:
        return self.run_root / "SCREENING_DECISIONS.csv"

    @property
    def download_manifest_path(self) -> Path:
        return self.run_root / "DOWNLOAD_MANIFEST.json"

    @property
    def sha256s_path(self) -> Path:
        return self.run_root / "SHA256SUMS.txt"

    @property
    def run_audit_path(self) -> Path:
        return self.run_root / "RUN_AUDIT.md"

    @property
    def handoff_path(self) -> Path:
        return self.run_root / "OBSIDIAN_HANDOFF_MANIFEST.json"

    @property
    def institutional_route_provenance_path(self) -> Path:
        return self.run_root / "manifests" / "INSTITUTIONAL_ROUTE_PROVENANCE.json"

    def write_search_request(self, request: LiteratureSearchRequest) -> Path:
        payload = {
            "SchemaVersion": "0.2",
            "GeneratedAt": datetime.now(timezone.utc).isoformat(),
            "RequestGenerationAudited": True,
            **request.as_dict(),
        }
        _atomic_text(
            self.search_request_path,
            json.dumps(sanitize_value(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return self.search_request_path

    def append_query_log(self, entry: QueryLogEntry) -> Path:
        exists = self.query_log_path.exists() and self.query_log_path.stat().st_size > 0
        self.query_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.query_log_path.open("a", encoding="utf-8-sig" if not exists else "utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=QUERY_LOG_FIELDS, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow(sanitize_value(entry.as_row()))
        return self.query_log_path

    def write_search_results(self, records: Iterable[LiteratureRecord]) -> Path:
        rows = [sanitize_value(record.as_result_row()) for record in records]
        self._write_csv(self.search_results_path, SEARCH_RESULT_FIELDS, rows)
        return self.search_results_path

    def write_screening_decisions(self, records: Iterable[LiteratureRecord]) -> Path:
        rows = []
        for record in records:
            rows.append(
                sanitize_value(
                    {
                        "PaperID": record.paper_id,
                        "Title": record.title,
                        "RelevanceScore": record.relevance_score,
                        "ScreeningDecision": record.screening_decision,
                        "ScreeningReason": record.screening_reason,
                        "AI_ASSISTED": record.ai_assisted,
                        "DuplicateDetected": record.duplicate_detected,
                        "DuplicateReason": record.duplicate_reason,
                        "CanonicalPaperID": record.canonical_paper_id,
                        "SameWorkDifferentVersion": record.same_work_different_version,
                        "ArchivedAsAlternateVersion": record.archived_as_alternate_version,
                    }
                )
            )
        self._write_csv(self.screening_path, SCREENING_FIELDS, rows)
        return self.screening_path

    def write_download_manifest(
        self,
        entries: Iterable[DownloadManifestEntry],
        *,
        institutional_routes: Iterable[Any] = (),
    ) -> Path:
        routes = [route.as_dict() if hasattr(route, "as_dict") else dict(route) for route in institutional_routes]
        payload = {
            "SchemaVersion": "0.2",
            "GeneratedAt": datetime.now(timezone.utc).isoformat(),
            "ManualAuthentication": True,
            "CredentialStorage": False,
            "Downloads": [entry.as_dict() for entry in entries],
        }
        if routes:
            payload["InstitutionalRouteSchemaVersion"] = "0.2.2"
            payload["InstitutionalRoutes"] = routes
        _atomic_text(
            self.download_manifest_path,
            json.dumps(sanitize_value(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return self.download_manifest_path

    def write_institutional_route_provenance(self, routes: Iterable[Any]) -> Path:
        serialized = [route.as_dict() if hasattr(route, "as_dict") else dict(route) for route in routes]
        payload = {
            "SchemaVersion": "0.2.2",
            "GeneratedAt": datetime.now(timezone.utc).isoformat(),
            "ManualAuthentication": True,
            "CredentialStorage": False,
            "CookiesExported": False,
            "TokensExported": False,
            "Routes": serialized,
        }
        _atomic_text(
            self.institutional_route_provenance_path,
            json.dumps(sanitize_value(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return self.institutional_route_provenance_path

    def write_sha256s(self, entries: Iterable[DownloadManifestEntry]) -> Path:
        lines: list[str] = []
        for entry in sorted(entries, key=lambda item: item.normalized_filename.casefold()):
            relative = self._relative_or_name(Path(entry.local_path))
            lines.append(f"{entry.sha256} *{relative.as_posix()}")
        _atomic_text(self.sha256s_path, ("\n".join(lines) + "\n") if lines else "")
        return self.sha256s_path

    def write_obsidian_handoff(self, records: Iterable[LiteratureRecord]) -> Path:
        destination = r"E:\AI helper\论文\00-收件箱 Inbox"
        papers = []
        for record in records:
            if not record.full_text_downloaded or record.local_path == UNKNOWN:
                continue
            papers.append(
                {
                    "SourcePDF": record.local_path,
                    "SuggestedDestination": destination,
                    "SuggestedLiteratureNoteTitle": record.title,
                    "Metadata": record.as_metadata_dict(),
                    "DOI": record.doi,
                    "SHA256": record.sha256,
                }
            )
        payload = {
            "SchemaVersion": "0.2",
            "GeneratedAt": datetime.now(timezone.utc).isoformat(),
            "OptionalObsidianHandoff": True,
            "AutoWriteToObsidian": False,
            "Papers": papers,
        }
        _atomic_text(
            self.handoff_path,
            json.dumps(sanitize_value(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return self.handoff_path

    def write_run_audit(
        self,
        *,
        status: str,
        source: str,
        query_count: int,
        result_count: int,
        download_count: int,
        errors: Iterable[str] = (),
        sensitive_log_leak_detected: bool = False,
    ) -> Path:
        error_lines = [f"- {sanitize_value(error)}" for error in errors]
        errors_section = "\n".join(error_lines) if error_lines else "- none"
        content = f"""# Literature Acquisition Run Audit

RunTimestamp={datetime.now(timezone.utc).isoformat()}
Status={status}
Source={sanitize_value(source)}
QueryCount={query_count}
ResultCount={result_count}
DownloadCount={download_count}

ManualAuthentication=true
CredentialAutomation=false
CookiesExported=false
TokensExported=false
CaptchaBypass=false
PaywallBypass=false
DRMBypass=false
AggressiveParallelScraping=false
HumanLikeRateLimit=true
SensitiveLogLeakDetected={str(sensitive_log_leak_detected).lower()}
AutoWriteToObsidian=false

## Errors

{errors_section}
"""
        _atomic_text(self.run_audit_path, content)
        return self.run_audit_path

    @staticmethod
    def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)

    def _relative_or_name(self, path: Path) -> Path:
        try:
            return path.resolve().relative_to(self.run_root)
        except (ValueError, OSError):
            return Path(path.name)
