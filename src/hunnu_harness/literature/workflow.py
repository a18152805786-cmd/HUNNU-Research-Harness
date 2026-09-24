from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from ..paths import TEMP_DIR, _windows_io_path, is_within
from .adapters.base import (
    LiteratureSourceAdapter,
    LiteratureSourceError,
    SourceActionRequired,
    SourceLayoutChanged,
    SourceUserDownloadRequired,
)
from .artifacts import (
    CNKI_UPGRADE_RUN_ROOT,
    INSTITUTIONAL_UPGRADE_RUN_ROOT,
    LiteratureArtifactWriter,
    UPGRADE_RUN_ROOT,
)
from .dedupe import LiteratureDeduplicator
from .downloads import (
    InvalidFullTextDownload,
    InvalidPDFDownload,
    LiteratureDownloadManager,
    UnauthorizedFullTextError,
)
from .fetch_ledger import FetchBudgetExceeded, FetchLedgerError
from .fulltext import AuthorizedFullTextValidator, infer_full_text_format
from .auto_classification import PostAcquisitionClassifier
from .library import GlobalPaperLibrary
from .models import (
    AccessDecision,
    AccessType,
    DownloadManifestEntry,
    LiteratureRecord,
    LiteratureRunResult,
    LiteratureSearchRequest,
    QueryLogEntry,
    RunStatus,
    ScreeningDecision,
    UNKNOWN,
)
from .planning import LiteratureSearchPlanner
from .normalization import normalize_doi, normalize_person, normalize_title
from .screening import LiteratureScreener
from .search_pace import SearchPaceLedger
from .security import LiteratureAuditLogger, scan_files_for_sensitive_leaks

if TYPE_CHECKING:
    from .institutional import (
        InstitutionalAccessResolver,
        InstitutionalResolutionTrigger,
        InstitutionalRouteResult,
    )


def _download_manager_for_run(
    writer: LiteratureArtifactWriter,
    *,
    allow_outside_project_for_tests: bool,
) -> LiteratureDownloadManager:
    isolated_test_library = allow_outside_project_for_tests or is_within(writer.run_root, TEMP_DIR)
    if isolated_test_library:
        library = GlobalPaperLibrary(
            writer.run_root.parent / "library",
            allow_outside_output_for_tests=True,
            make_managed_read_only=False,
        )
    else:
        library = GlobalPaperLibrary()
    # Topic classification writes canonical metadata, so it is attached only to
    # the real Library.  An isolated test run archives into its own tree and
    # must never reach the frozen taxonomy or the shared topic store.
    classifier = None if isolated_test_library else PostAcquisitionClassifier()
    return LiteratureDownloadManager(
        writer.downloads_dir,
        allow_outside_project_for_tests=allow_outside_project_for_tests,
        make_archive_read_only=not allow_outside_project_for_tests,
        global_library=library,
        topic_classifier=classifier,
    )


def _search_pacer_for_run(
    explicit: "SearchPaceLedger | None",
    writer: LiteratureArtifactWriter,
    *,
    allow_outside_project_for_tests: bool,
) -> SearchPaceLedger:
    """The throttle a run should use, isolated the way its library is.

    A real run shares one ledger across processes -- that is the whole point,
    since the searches that tripped the publisher came from separate ones.  An
    isolated test run must never reach it, and must never actually sleep: it
    gets a ledger inside its own tree with pacing switched off, matching how
    ``_download_manager_for_run`` keeps the real Library out of tests.
    """

    if explicit is not None:
        return explicit
    isolated = allow_outside_project_for_tests or is_within(writer.run_root, TEMP_DIR)
    if not isolated:
        return SearchPaceLedger()
    return SearchPaceLedger(
        writer.run_root / "audit" / "search_pace_ledger.jsonl",
        min_interval_seconds=0.0,
        burst_limit=10**6,
        sleeper=lambda _seconds: None,
        allow_outside_output_for_tests=allow_outside_project_for_tests,
    )


def _lock_search_result_to_detail(
    found: Iterable[LiteratureRecord],
    detail: LiteratureRecord,
    *,
    title_key: Callable[[str], str] = normalize_title,
) -> LiteratureRecord | None:
    """Return one auditable search/detail identity match, preferring DOI.

    ``title_key`` is ``normalize_title`` unless the live run's adapter is one
    whose own parsers spell one title two ways (see ``_search_detail_title_key``).
    """

    detail_doi = normalize_doi(detail.doi)
    detail_title = title_key(detail.title)
    for candidate in found:
        candidate_doi = normalize_doi(candidate.doi)
        if detail_doi != UNKNOWN and candidate_doi != UNKNOWN:
            if candidate_doi == detail_doi:
                return candidate
            continue
        if detail_title != UNKNOWN and title_key(candidate.title) == detail_title:
            if (
                candidate.year == UNKNOWN
                or detail.year == UNKNOWN
                or candidate.year == detail.year
            ):
                return candidate
    return None


def _search_detail_title_key(adapter: LiteratureSourceAdapter) -> Callable[[str], str]:
    """The title comparison the search/detail lock uses for ``adapter``.

    Only the registered CNKI adapter -- by exact type, as the registry checks
    it -- compares through its own result-page spacing rule.  Every other
    adapter, a CNKIAdapter subclass included, keeps plain ``normalize_title``.
    """

    from .adapters.cnki import CNKIAdapter

    if type(adapter) is CNKIAdapter:
        return CNKIAdapter.search_detail_title_key
    return normalize_title


def _matches_requested_target_identity(
    record: LiteratureRecord,
    request: LiteratureSearchRequest,
) -> bool:
    """Fail closed when an explicit citation request does not match a detail page.

    Search/detail relocking only proves that the opened page belongs to the
    selected search result.  Explicit DOI, title, author, and year constraints
    separately identify the citation requested by the user.
    """

    requested_dois = {
        normalized
        for value in request.dois
        if (normalized := normalize_doi(value)) != UNKNOWN
    }
    requested_titles = {
        normalized
        for value in request.exact_titles
        if (normalized := normalize_title(value)) != UNKNOWN
    }
    requested_authors = tuple(
        normalized
        for value in request.authors
        if (normalized := normalize_person(value)) != UNKNOWN
    )
    has_explicit_identity = bool(requested_dois or requested_titles or requested_authors)
    if not has_explicit_identity:
        return True

    if requested_dois and normalize_doi(record.doi) not in requested_dois:
        return False
    if requested_titles and normalize_title(record.title) not in requested_titles:
        return False

    if request.year_start is not None or request.year_end is not None:
        if not str(record.year).isdigit():
            return False
        year = int(record.year)
        if request.year_start is not None and year < request.year_start:
            return False
        if request.year_end is not None and year > request.year_end:
            return False

    record_authors = tuple(
        normalized
        for value in record.authors
        if (normalized := normalize_person(value)) != UNKNOWN
    )
    if requested_authors and record_authors:
        def author_matches(requested: str) -> bool:
            return any(
                requested == observed or requested in observed.split()
                for observed in record_authors
            )

        if requested_dois or requested_titles:
            if not any(author_matches(author) for author in requested_authors):
                return False
        elif not all(author_matches(author) for author in requested_authors):
            return False
    elif requested_authors and not (requested_dois or requested_titles):
        return False

    return True


def _is_fulltext_acquisition_candidate(record: LiteratureRecord) -> bool:
    """Require both identity confirmation and a final KEEP decision.

    Search/detail identity confirmation proves that the inspected detail page
    belongs to the selected search result.  It does not turn a MAYBE screening
    result into permission to enter the full-text acquisition stage.
    """

    return (
        record.target_identity_confirmed
        and record.screening_decision == ScreeningDecision.KEEP.value
        and not record.duplicate_detected
    )


class LiteratureAcquisitionWorkflow:
    """Serial, bounded acquisition with durable artifacts on every exit path."""

    def __init__(
        self,
        adapter: LiteratureSourceAdapter,
        *,
        run_root: Path = UPGRADE_RUN_ROOT,
        max_queries: int = 8,
        human_like_delay_seconds: float | None = None,
        allow_outside_project_for_tests: bool = False,
        institutional_resolver: "InstitutionalAccessResolver | None" = None,
        institutional_trigger: "InstitutionalResolutionTrigger | None" = None,
        search_pacer: "SearchPaceLedger | None" = None,
    ):
        self.adapter = adapter
        self.writer = LiteratureArtifactWriter(
            run_root,
            allow_outside_project_for_tests=allow_outside_project_for_tests,
        )
        self.search_pacer = _search_pacer_for_run(
            search_pacer,
            self.writer,
            allow_outside_project_for_tests=allow_outside_project_for_tests,
        )
        self.download_manager = _download_manager_for_run(
            self.writer,
            allow_outside_project_for_tests=allow_outside_project_for_tests,
        )
        self.planner = LiteratureSearchPlanner(max_queries=max_queries)
        self.screener = LiteratureScreener()
        self.deduplicator = LiteratureDeduplicator()
        self.delay = (
            adapter.human_like_delay_seconds
            if human_like_delay_seconds is None
            else max(0.0, human_like_delay_seconds)
        )
        self.event_log = self.writer.run_root / "audit" / "LITERATURE_EVENTS.jsonl"
        self.logger = LiteratureAuditLogger(self.event_log)
        self.institutional_resolver = institutional_resolver
        self.institutional_trigger = institutional_trigger
        self.institutional_routes: list["InstitutionalRouteResult"] = []

    async def run(self, request: LiteratureSearchRequest) -> LiteratureRunResult:
        self.writer.write_search_request(request)
        self.logger.log(
            "literature_request_created",
            status=RunStatus.SUCCESS.value,
            request=request.as_dict(),
            manual_authentication=True,
        )
        records: list[LiteratureRecord] = []
        downloads: list[DownloadManifestEntry] = []
        errors: list[str] = []
        action_required_reason = UNKNOWN
        status = RunStatus.SUCCESS

        if request.restricted_search and not getattr(self.adapter, "supports_restricted_search", False):
            # Searching anyway would return unrestricted results under a
            # restricted request -- the failure this refusal exists to prevent.
            errors.append(
                f"RESTRICTED_SEARCH_UNSUPPORTED: {self.adapter.name} cannot confine a search to "
                "ResourceType/SourceJournals; no search was sent"
            )
            return self._finalize(
                request=request,
                status=RunStatus.SOURCE_UNAVAILABLE,
                records=records,
                downloads=downloads,
                errors=errors,
                query_count=0,
            )

        plans = self.planner.plan(request)
        if not plans:
            errors.append("No bounded query could be generated from the request")
            return self._finalize(
                request=request,
                status=RunStatus.NO_RESULTS,
                records=records,
                downloads=downloads,
                errors=errors,
                query_count=0,
            )

        stop_for_auth = False
        stop_searching = False
        for plan in plans:
            if len(records) >= request.max_search_results or stop_for_auth or stop_searching:
                break
            query_results: list[LiteratureRecord] = []
            inspected = 0
            query_error = UNKNOWN
            try:
                # Wait out the publisher-search throttle before the request
                # goes out, not after: the block page this prevents is served
                # to the query, and by then the session is already spent.
                self.search_pacer.pace(source=self.adapter.name, query=plan.query)
                query_results = await self.adapter.search(plan.query, request)
                remaining = request.max_search_results - len(records)
                query_results = query_results[:remaining]
                for search_record in query_results:
                    try:
                        await self.adapter.open_result(search_record)
                        extracted = await self.adapter.extract_metadata(search_query=plan.query)
                        if _lock_search_result_to_detail(
                            (search_record,),
                            extracted,
                            title_key=_search_detail_title_key(self.adapter),
                        ) is None:
                            raise SourceLayoutChanged(
                                "Search/detail target identity lock failed before acquisition"
                            )
                        extracted.target_identity_confirmed = _matches_requested_target_identity(
                            extracted,
                            request,
                        )
                        extracted.canonical_paper_id = extracted.paper_id
                        access = await self.adapter.check_fulltext_access()
                        extracted.full_text_accessible = access.full_text_accessible
                        extracted.access_type = access.access_type.value
                        extracted.full_text_format = access.full_text_format.value
                        records.append(extracted)
                        inspected += 1
                        self.logger.log(
                            "literature_result_inspected",
                            status=access.status.value,
                            paper_id=extracted.paper_id,
                            source=extracted.source_database,
                            stable_identifier=extracted.stable_identifier,
                            full_text_accessible=access.full_text_accessible,
                            access_type=access.access_type.value,
                            target_identity_confirmed=extracted.target_identity_confirmed,
                        )
                    except SourceUserDownloadRequired as exc:
                        query_error = str(exc)
                        action_required_reason = str(exc)
                        status = RunStatus.ACTION_REQUIRED_USER_DOWNLOAD
                        stop_for_auth = True
                        break
                    except SourceActionRequired as exc:
                        query_error = str(exc)
                        action_required_reason = str(exc)
                        status = RunStatus.ACTION_REQUIRED_USER_LOGIN
                        stop_for_auth = True
                        break
                    except LiteratureSourceError as exc:
                        search_record.error_status = exc.status.value
                        search_record.error_reason = str(exc)
                        records.append(search_record)
                        inspected += 1
                        query_error = str(exc)
                        errors.append(str(exc))
                        status = RunStatus.PARTIAL_SUCCESS
                    except Exception as exc:
                        search_record.error_status = RunStatus.SOURCE_LAYOUT_CHANGED.value
                        search_record.error_reason = f"Metadata inspection failed: {type(exc).__name__}"
                        records.append(search_record)
                        inspected += 1
                        query_error = search_record.error_reason
                        errors.append(search_record.error_reason)
                        status = RunStatus.PARTIAL_SUCCESS
                    await self._rate_limit()
            except SourceUserDownloadRequired as exc:
                query_error = str(exc)
                action_required_reason = str(exc)
                status = RunStatus.ACTION_REQUIRED_USER_DOWNLOAD
                stop_for_auth = True
            except SourceActionRequired as exc:
                query_error = str(exc)
                action_required_reason = str(exc)
                status = RunStatus.ACTION_REQUIRED_USER_LOGIN
                stop_for_auth = True
            except LiteratureSourceError as exc:
                query_error = str(exc)
                errors.append(str(exc))
                status = exc.status
                stop_searching = exc.halts_remaining_searches
            except Exception as exc:
                query_error = f"Search failed: {type(exc).__name__}"
                errors.append(query_error)
                status = RunStatus.PARTIAL_SUCCESS if records else RunStatus.SOURCE_UNAVAILABLE
            finally:
                filters = dict(plan.filters)
                if request.restricted_search:
                    # What the source confirmed and discarded for this query,
                    # beside what was asked of it, whichever way it ended.
                    report_for = getattr(self.adapter, "search_restriction_report", None)
                    restriction = report_for(plan.query) if callable(report_for) else None
                    if restriction is not None:
                        filters["RestrictionOutcome"] = restriction
                        self.logger.log(
                            "literature_search_restriction_checked",
                            status=(
                                RunStatus.SUCCESS.value
                                if restriction.get("ScopeConfirmed")
                                else RunStatus.SOURCE_LAYOUT_CHANGED.value
                            ),
                            source=self.adapter.name,
                            **restriction,
                        )
                self.writer.append_query_log(
                    QueryLogEntry(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        source=self.adapter.name,
                        original_research_request=request.original_research_request,
                        generated_query=plan.query,
                        filters=json.dumps(filters, ensure_ascii=False, sort_keys=True),
                        results_returned=len(query_results),
                        results_inspected=inspected,
                        errors=query_error,
                    )
                )
                self.writer.write_search_results(records)

            await self._rate_limit()

        if status in {
            RunStatus.ACTION_REQUIRED_USER_LOGIN,
            RunStatus.ACTION_REQUIRED_USER_DOWNLOAD,
        }:
            errors.append(action_required_reason)
            result = self._finalize(
                request=request,
                status=status,
                records=records,
                downloads=downloads,
                errors=errors,
                query_count=min(len(plans), self._query_log_count()),
                action_required_reason=action_required_reason,
            )
            return result

        if not records:
            final_status = status if status in {RunStatus.SOURCE_UNAVAILABLE, RunStatus.SOURCE_LAYOUT_CHANGED} else RunStatus.NO_RESULTS
            return self._finalize(
                request=request,
                status=final_status,
                records=records,
                downloads=downloads,
                errors=errors,
                query_count=self._query_log_count(),
            )

        for record in records:
            self.screener.screen(record, request)
        self.deduplicator.deduplicate(records)
        self.writer.write_screening_decisions(records)
        self.writer.write_search_results(records)

        max_downloads = min(request.max_downloads, request.max_downloads_per_run)
        candidates: list[LiteratureRecord] = []
        if max_downloads > 0:
            candidates = [
                record
                for record in records
                if _is_fulltext_acquisition_candidate(record)
            ]
            for record in candidates:
                if len(downloads) >= max_downloads:
                    break
                try:
                    # The screening phase has already navigated to this record,
                    # locked its identity, and confirmed access.  Navigating
                    # there again is redundant whenever the browser never left,
                    # and it is the command an Agent sees as "go somewhere"
                    # immediately after the target was locked.  Ask the adapter
                    # -- by observation, not navigation -- whether we are still
                    # on the locked target, and re-navigate only when we are
                    # not, which keeps genuine recovery working.
                    #
                    # This never re-searches: recovery goes back to this
                    # record's own locked page.  Identity is not weakened
                    # either -- the probe compares the source identifier, and
                    # ``download_fulltext`` still fails closed unless the PDF
                    # control belongs to the locked article.
                    already_on_target = await self.adapter.current_target_matches(record)
                    if already_on_target:
                        self.logger.log(
                            "target_navigation_reused",
                            status=RunStatus.SUCCESS.value,
                            paper_id=record.paper_id,
                            source=record.source_database,
                            stable_identifier=record.stable_identifier,
                            renavigation_skipped=True,
                        )
                    else:
                        await self.adapter.open_result(record)
                    access = await self.adapter.check_fulltext_access()
                    record.full_text_accessible = access.full_text_accessible
                    record.access_type = access.access_type.value
                    record.full_text_format = access.full_text_format.value
                    if (
                        (not access.full_text_accessible or not access.authorized_access)
                        and self.institutional_resolver is not None
                        and self.institutional_trigger is not None
                    ):
                        route, access = await self.institutional_resolver.resolve_and_recheck(
                            self.adapter,
                            record,
                            trigger=self.institutional_trigger,
                        )
                        self.institutional_routes.append(route)
                        record.full_text_accessible = access.full_text_accessible
                        record.access_type = access.access_type.value
                        record.full_text_format = access.full_text_format.value
                        self.logger.log(
                            "institutional_route_access_rechecked",
                            status=access.status.value,
                            paper_id=record.paper_id,
                            requested_source=route.requested_source,
                            institutional_route_resolved=route.institutional_route_resolved,
                            institutional_target_database_match=route.institutional_target_database_match,
                            full_text_access_rechecked=route.full_text_access_rechecked,
                            full_text_accessible=route.full_text_accessible,
                        )
                    if not access.full_text_accessible or not access.authorized_access:
                        if access.status in (RunStatus.FULLTEXT_NOT_AUTHORIZED, RunStatus.SUCCESS):
                            record.error_status = RunStatus.FULLTEXT_NOT_AUTHORIZED.value
                            record.error_reason = access.reason
                            self.logger.log(
                                "fulltext_not_authorized",
                                status=RunStatus.FULLTEXT_NOT_AUTHORIZED.value,
                                paper_id=record.paper_id,
                                reason=access.reason,
                            )
                        else:
                            record.error_status = access.status.value
                            record.error_reason = access.reason
                            errors.append(access.reason)
                            status = RunStatus.PARTIAL_SUCCESS
                            self.logger.log(
                                "fulltext_access_undetermined",
                                status=access.status.value,
                                paper_id=record.paper_id,
                                access_type=access.access_type.value,
                                reason=access.reason,
                            )
                        continue
                    source = await self.adapter.download_fulltext(record, access)
                    entry = self.download_manager.archive_authorized_fulltext(source, record, access)
                    downloads.append(entry)
                    self.logger.log(
                        "authorized_fulltext_archived",
                        status=RunStatus.SUCCESS.value,
                        paper_id=record.paper_id,
                        source=record.source_database,
                        access_type=record.access_type,
                        normalized_filename=record.normalized_filename,
                        sha256=record.sha256,
                        file_size_bytes=record.file_size_bytes,
                        full_text_format=record.full_text_format,
                    )
                except SourceUserDownloadRequired as exc:
                    action_required_reason = str(exc)
                    errors.append(str(exc))
                    status = RunStatus.ACTION_REQUIRED_USER_DOWNLOAD
                    break
                except SourceActionRequired as exc:
                    action_required_reason = str(exc)
                    errors.append(str(exc))
                    status = RunStatus.ACTION_REQUIRED_USER_LOGIN
                    break
                except (UnauthorizedFullTextError, PermissionError) as exc:
                    record.error_status = RunStatus.FULLTEXT_NOT_AUTHORIZED.value
                    record.error_reason = str(exc)
                    status = RunStatus.PARTIAL_SUCCESS
                except FetchBudgetExceeded as exc:
                    # A budget refusal is not a download failure: the run goes
                    # on, but the record carries the ledger's own status so the
                    # CLI can report -- and exit on -- "budget", not "failed".
                    record.error_status = exc.status
                    record.error_reason = str(exc)
                    errors.append(str(exc))
                    status = RunStatus.PARTIAL_SUCCESS
                    self.logger.log(
                        "fulltext_fetch_refused_by_budget",
                        status=exc.status,
                        paper_id=record.paper_id,
                        reason=str(exc),
                    )
                except (
                    InvalidFullTextDownload,
                    LiteratureSourceError,
                    FetchLedgerError,
                    OSError,
                ) as exc:
                    record.error_status = RunStatus.DOWNLOAD_FAILED.value
                    record.error_reason = str(exc)
                    errors.append(str(exc))
                    status = RunStatus.PARTIAL_SUCCESS
                    self.logger.log(
                        "fulltext_download_failed",
                        status=RunStatus.DOWNLOAD_FAILED.value,
                        paper_id=record.paper_id,
                        reason=str(exc),
                    )
                await self._rate_limit()

        self.deduplicator.deduplicate(records)
        if request.require_full_text and not downloads and status == RunStatus.SUCCESS:
            status = RunStatus.NO_RESULTS if max_downloads > 0 and not candidates else RunStatus.FULLTEXT_NOT_AUTHORIZED
        elif errors and status == RunStatus.SUCCESS:
            status = RunStatus.PARTIAL_SUCCESS
        return self._finalize(
            request=request,
            status=status,
            records=records,
            downloads=downloads,
            errors=errors,
            query_count=self._query_log_count(),
            action_required_reason=action_required_reason,
        )

    async def _rate_limit(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)

    def _query_log_count(self) -> int:
        query_log_io = _windows_io_path(self.writer.query_log_path)
        if not query_log_io.exists():
            return 0
        with query_log_io.open("r", encoding="utf-8-sig") as handle:
            return max(0, sum(1 for _ in handle) - 1)

    def _finalize(
        self,
        *,
        request: LiteratureSearchRequest,
        status: RunStatus,
        records: list[LiteratureRecord],
        downloads: list[DownloadManifestEntry],
        errors: list[str],
        query_count: int,
        action_required_reason: str = UNKNOWN,
    ) -> LiteratureRunResult:
        self.writer.write_search_results(records)
        self.writer.write_screening_decisions(records)
        self.writer.write_download_manifest(
            downloads,
            institutional_routes=self.institutional_routes,
        )
        if self.institutional_routes:
            self.writer.write_institutional_route_provenance(self.institutional_routes)
        self.writer.write_sha256s(downloads)
        self.writer.write_obsidian_handoff(records)

        scan_targets = [
            self.event_log,
            self.writer.search_request_path,
            self.writer.query_log_path,
            self.writer.search_results_path,
            self.writer.screening_path,
            self.writer.download_manifest_path,
            self.writer.handoff_path,
        ]
        if self.institutional_routes:
            scan_targets.append(self.writer.institutional_route_provenance_path)
        findings = scan_files_for_sensitive_leaks(scan_targets)
        if findings:
            errors.append(f"Sensitive credential-shaped content detected in {len(findings)} artifact location(s)")
            if status == RunStatus.SUCCESS:
                status = RunStatus.PARTIAL_SUCCESS
        self.writer.write_run_audit(
            status=status.value,
            source=self.adapter.name,
            query_count=query_count,
            result_count=len(records),
            download_count=len(downloads),
            errors=errors,
            sensitive_log_leak_detected=bool(findings),
        )
        self.logger.log(
            "literature_run_finalized",
            status=status.value,
            result_count=len(records),
            download_count=len(downloads),
            sensitive_log_leak_detected=bool(findings),
        )
        return LiteratureRunResult(
            status=status,
            records=records,
            downloads=downloads,
            errors=errors,
            action_required_reason=action_required_reason,
        )


def finalize_captured_sciencedirect_acceptance(
    *,
    request: LiteratureSearchRequest,
    query: str,
    search_html: str,
    article_html: str,
    article_url: str,
    downloaded_pdf: Path | None,
    run_root: Path = UPGRADE_RUN_ROOT,
) -> LiteratureRunResult:
    """Finalize MCP-captured public DOM evidence through the production pipeline.

    Only a sanitized subset of public metadata/controls should be supplied; this
    function never consumes cookies, headers, storage state, or credentials.
    """

    from .adapters.sciencedirect import ScienceDirectAdapter

    writer = LiteratureArtifactWriter(run_root)
    manager = _download_manager_for_run(writer, allow_outside_project_for_tests=False)
    logger = LiteratureAuditLogger(writer.run_root / "audit" / "LITERATURE_EVENTS.jsonl")
    writer.write_search_request(request)
    found = ScienceDirectAdapter.parse_search_results_html(
        search_html,
        query=query,
        source_url="https://www.sciencedirect.com/search",
        max_results=request.max_results_per_source,
    )
    record = ScienceDirectAdapter.parse_article_html(
        article_html,
        source_url=article_url,
        search_query=query,
    )
    access = ScienceDirectAdapter.check_fulltext_access_html(article_html, source_url=article_url)
    record.full_text_accessible = access.full_text_accessible
    record.access_type = access.access_type.value
    LiteratureScreener().screen(record, request)
    records = [record]
    LiteratureDeduplicator().deduplicate(records)
    downloads: list[DownloadManifestEntry] = []
    errors: list[str] = []
    status = RunStatus.SUCCESS
    identity_match = _lock_search_result_to_detail(found, record)
    if identity_match is None:
        status = RunStatus.SOURCE_LAYOUT_CHANGED
        record.error_status = status.value
        record.error_reason = "ScienceDirect search/detail target identity lock failed"
        errors.append(record.error_reason)
    else:
        record.paper_id = identity_match.paper_id
        record.canonical_paper_id = identity_match.paper_id
        record.target_identity_confirmed = True
    if downloaded_pdf is not None and record.target_identity_confirmed:
        try:
            downloads.append(manager.archive_authorized_pdf(downloaded_pdf, record, access))
        except (InvalidPDFDownload, UnauthorizedFullTextError, OSError) as exc:
            status = RunStatus.DOWNLOAD_FAILED
            record.error_status = status.value
            record.error_reason = str(exc)
            errors.append(str(exc))
    elif request.require_full_text:
        status = RunStatus.FULLTEXT_NOT_AUTHORIZED if not access.authorized_access else RunStatus.DOWNLOAD_FAILED
        errors.append("No downloaded PDF was supplied for the captured acceptance run")

    writer.append_query_log(
        QueryLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=ScienceDirectAdapter.name,
            original_research_request=request.original_research_request,
            generated_query=query,
            filters=json.dumps(
                {
                    "YearStart": request.year_start or UNKNOWN,
                    "YearEnd": request.year_end or UNKNOWN,
                    "MaxResultsPerSource": request.max_results_per_source,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            results_returned=len(found),
            results_inspected=1,
            errors=UNKNOWN if not errors else "; ".join(errors),
        )
    )
    writer.write_search_results(records)
    writer.write_screening_decisions(records)
    writer.write_download_manifest(downloads)
    writer.write_sha256s(downloads)
    writer.write_obsidian_handoff(records)
    scan_targets = (
        writer.search_request_path,
        writer.query_log_path,
        writer.search_results_path,
        writer.screening_path,
        writer.download_manifest_path,
        writer.handoff_path,
    )
    findings = scan_files_for_sensitive_leaks(scan_targets)
    if findings:
        status = RunStatus.PARTIAL_SUCCESS
        errors.append(f"Sensitive credential-shaped content detected in {len(findings)} artifact location(s)")
    writer.write_run_audit(
        status=status.value,
        source=ScienceDirectAdapter.name,
        query_count=1,
        result_count=1,
        download_count=len(downloads),
        errors=errors,
        sensitive_log_leak_detected=bool(findings),
    )
    logger.log(
        "captured_live_acceptance_finalized",
        status=status.value,
        source=ScienceDirectAdapter.name,
        stable_identifier=record.stable_identifier,
        authorized_access=access.authorized_access,
        pdf_downloaded=bool(downloads),
        pdf_validated=bool(downloads),
    )
    return LiteratureRunResult(status=status, records=records, downloads=downloads, errors=errors)


def finalize_captured_springerlink_acceptance(
    *,
    request: LiteratureSearchRequest,
    query: str,
    search_html: str,
    article_html: str,
    article_url: str,
    downloaded_pdf: Path | None,
    run_root: Path = UPGRADE_RUN_ROOT,
) -> LiteratureRunResult:
    """Finalize sanitized Springer evidence and a downloaded PDF.

    This mirrors the normal workflow while accepting only public DOM evidence
    and a local file. Browser cookies, headers, storage state, and signed URLs
    are deliberately outside the interface.
    """

    from .adapters.springerlink import SpringerLinkAdapter

    found = SpringerLinkAdapter.parse_search_results_html(
        search_html,
        query=query,
        source_url="https://link.springer.com/search",
        max_results=request.max_results_per_source,
    )
    record = SpringerLinkAdapter.parse_article_html(
        article_html,
        source_url=article_url,
        search_query=query,
    )
    access = SpringerLinkAdapter.check_fulltext_access_html(article_html, source_url=article_url)

    return _finalize_captured_springerlink_evidence(
        request=request,
        query=query,
        found=found,
        record=record,
        access=access,
        downloaded_pdf=downloaded_pdf,
        run_root=run_root,
    )


def _finalize_captured_springerlink_evidence(
    *,
    request: LiteratureSearchRequest,
    query: str,
    found: list[LiteratureRecord],
    record: LiteratureRecord,
    access: AccessDecision,
    downloaded_pdf: Path | None,
    run_root: Path,
) -> LiteratureRunResult:
    """Archive already source-validated Springer evidence without reclassifying access."""

    writer = LiteratureArtifactWriter(run_root)
    manager = _download_manager_for_run(writer, allow_outside_project_for_tests=False)
    logger = LiteratureAuditLogger(writer.run_root / "audit" / "LITERATURE_EVENTS.jsonl")
    writer.write_search_request(request)
    record.full_text_accessible = access.full_text_accessible
    record.access_type = access.access_type.value
    LiteratureScreener().screen(record, request)
    records = [record]
    LiteratureDeduplicator().deduplicate(records)
    downloads: list[DownloadManifestEntry] = []
    errors: list[str] = []
    status = RunStatus.SUCCESS
    identity_match = _lock_search_result_to_detail(found, record)
    if identity_match is None:
        status = RunStatus.SOURCE_LAYOUT_CHANGED
        record.error_status = status.value
        record.error_reason = "SpringerLink search/detail target identity lock failed"
        errors.append(record.error_reason)
    else:
        record.paper_id = identity_match.paper_id
        record.canonical_paper_id = identity_match.paper_id
        record.target_identity_confirmed = True
    if downloaded_pdf is not None and record.target_identity_confirmed:
        try:
            downloads.append(manager.archive_authorized_pdf(downloaded_pdf, record, access))
        except (InvalidPDFDownload, UnauthorizedFullTextError, OSError) as exc:
            status = RunStatus.DOWNLOAD_FAILED
            record.error_status = status.value
            record.error_reason = str(exc)
            errors.append(str(exc))
    elif request.require_full_text:
        status = RunStatus.FULLTEXT_NOT_AUTHORIZED if not access.authorized_access else RunStatus.DOWNLOAD_FAILED
        errors.append("No downloaded PDF was supplied for the captured acceptance run")

    writer.append_query_log(
        QueryLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=record.source_database,
            original_research_request=request.original_research_request,
            generated_query=query,
            filters=json.dumps(
                {
                    "YearStart": request.year_start or UNKNOWN,
                    "YearEnd": request.year_end or UNKNOWN,
                    "MaxResultsPerSource": request.max_results_per_source,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            results_returned=len(found),
            results_inspected=1,
            errors=UNKNOWN if not errors else "; ".join(errors),
        )
    )
    writer.write_search_results(records)
    writer.write_screening_decisions(records)
    writer.write_download_manifest(downloads)
    writer.write_sha256s(downloads)
    writer.write_obsidian_handoff(records)
    scan_targets = (
        writer.search_request_path,
        writer.query_log_path,
        writer.search_results_path,
        writer.screening_path,
        writer.download_manifest_path,
        writer.handoff_path,
    )
    findings = scan_files_for_sensitive_leaks(scan_targets)
    if findings:
        status = RunStatus.PARTIAL_SUCCESS
        errors.append(f"Sensitive credential-shaped content detected in {len(findings)} artifact location(s)")
    writer.write_run_audit(
        status=status.value,
        source=record.source_database,
        query_count=1,
        result_count=1,
        download_count=len(downloads),
        errors=errors,
        sensitive_log_leak_detected=bool(findings),
    )
    logger.log(
        "captured_live_acceptance_finalized",
        status=status.value,
        source=record.source_database,
        stable_identifier=record.stable_identifier,
        authorized_access=access.authorized_access,
        pdf_downloaded=bool(downloads),
        pdf_validated=bool(downloads),
    )
    return LiteratureRunResult(status=status, records=records, downloads=downloads, errors=errors)


def finalize_captured_hunnu_springer_acceptance(
    *,
    request: LiteratureSearchRequest,
    query: str,
    search_html: str,
    article_html: str,
    article_url: str,
    downloaded_pdf: Path | None,
    institutional_route: "InstitutionalRouteResult",
    expected_title: str = UNKNOWN,
    expected_doi: str = UNKNOWN,
    run_root: Path = INSTITUTIONAL_UPGRADE_RUN_ROOT,
) -> LiteratureRunResult:
    """Finalize one identity-locked HUNNU → Springer Playwright MCP capture.

    The caller supplies only sanitized public DOM evidence, the local browser
    download, and non-sensitive route provenance. The function validates the
    route and target identity *before* it allows the existing Springer finalizer
    to archive a file.
    """

    from .adapters.springerlink import SpringerLinkAdapter
    from .institutional import HUNNUInstitutionalAccessResolver

    if not institutional_route.institutional_route_resolved:
        raise SourceLayoutChanged("InstitutionalRouteResolved=false; archive was not attempted")
    if institutional_route.institutional_target_database_match is not True:
        raise SourceLayoutChanged("InstitutionalTargetDatabaseMatch is not unambiguously true")
    if HUNNUInstitutionalAccessResolver.canonical_source(
        institutional_route.requested_source
    ) != SpringerLinkAdapter.name:
        raise SourceLayoutChanged("Institutional route requested a source other than SpringerLink")
    route_destination_title = (
        institutional_route.route_steps[-1].page_title
        if institutional_route.route_steps
        else UNKNOWN
    )
    if not HUNNUInstitutionalAccessResolver.is_verified_source_destination(
        SpringerLinkAdapter.name,
        institutional_route.publisher_entry_url,
        page_title=route_destination_title,
    ):
        raise SourceLayoutChanged(
            "Institutional route did not terminate on a verified Springer destination"
        )

    expected_normalized_doi = normalize_doi(expected_doi)
    expected_normalized_title = normalize_title(expected_title)
    if expected_normalized_doi == UNKNOWN and expected_normalized_title == UNKNOWN:
        raise ValueError("Expected DOI or title is required for target identity lock")

    found = SpringerLinkAdapter.parse_search_results_html(
        search_html,
        query=query,
        source_url="https://link.springer.com/search",
        max_results=request.max_results_per_source,
    )
    adapter = SpringerLinkAdapter(browser=None)
    direct_springer_page = HUNNUInstitutionalAccessResolver.is_official_source_url(
        SpringerLinkAdapter.name,
        article_url,
    )
    if not direct_springer_page:
        adapter.bind_institutional_route(institutional_route)

    article_record = adapter.parse_article_html(
        article_html,
        source_url=article_url,
        search_query=query,
    )

    def matches_expected(record: LiteratureRecord) -> bool:
        if expected_normalized_doi != UNKNOWN and normalize_doi(record.doi) != expected_normalized_doi:
            return False
        if expected_normalized_title != UNKNOWN and normalize_title(record.title) != expected_normalized_title:
            return False
        return True

    if not matches_expected(article_record):
        raise SourceLayoutChanged("Publisher article did not match the expected title/DOI")
    search_match = next(
        (
            item
            for item in found
            if matches_expected(item)
            and HUNNUInstitutionalAccessResolver.records_match_identity(item, article_record)
        ),
        None,
    )
    if search_match is None:
        raise SourceLayoutChanged("Search result → publisher article target identity lock failed")

    article_record.paper_id = search_match.paper_id
    article_record.canonical_paper_id = search_match.paper_id
    article_record.target_identity_confirmed = True
    if direct_springer_page:
        access = adapter.check_fulltext_access_html(article_html, source_url=article_url)
    else:
        access = adapter._check_gateway_fulltext_access_html(
            article_html,
            source_url=article_url,
        )
        article_record.institutional_route_used = adapter._gateway_trusted()
        article_record.institution = institutional_route.institution
        article_record.source_route = "HUNNU_GATEWAY_TO_SPRINGER"

    if downloaded_pdf is not None:
        pdf_identity = adapter.validate_pdf_identity(downloaded_pdf, article_record)
        article_record.target_title_matched = pdf_identity.title_matched
        article_record.target_doi_matched = pdf_identity.doi_matched
        article_record.target_identity_confirmed = pdf_identity.confirmed
        if not pdf_identity.confirmed:
            raise SourceLayoutChanged(
                "Captured Springer PDF did not match the expected article DOI/title"
            )

    rechecked_route = institutional_route.with_access_decision(access)
    result = _finalize_captured_springerlink_evidence(
        request=request,
        query=query,
        found=found,
        record=article_record,
        access=access,
        downloaded_pdf=downloaded_pdf,
        run_root=run_root,
    )

    record = result.records[0]
    record.paper_id = search_match.paper_id
    record.canonical_paper_id = search_match.paper_id
    record.target_identity_confirmed = True
    result.downloads = [
        replace(
            entry,
            paper_id=search_match.paper_id,
            target_identity_confirmed=True,
        )
        for entry in result.downloads
    ]

    writer = LiteratureArtifactWriter(run_root)
    writer.write_search_results(result.records)
    writer.write_screening_decisions(result.records)
    writer.write_download_manifest(
        result.downloads,
        institutional_routes=(rechecked_route,),
    )
    writer.write_institutional_route_provenance((rechecked_route,))
    writer.write_sha256s(result.downloads)
    writer.write_obsidian_handoff(result.records)
    scan_targets = (
        writer.search_request_path,
        writer.query_log_path,
        writer.search_results_path,
        writer.screening_path,
        writer.download_manifest_path,
        writer.handoff_path,
        writer.institutional_route_provenance_path,
    )
    findings = scan_files_for_sensitive_leaks(scan_targets)
    if findings:
        result.status = RunStatus.PARTIAL_SUCCESS
        result.errors.append(
            f"Sensitive credential-shaped content detected in {len(findings)} artifact location(s)"
        )
    writer.write_run_audit(
        status=result.status.value,
        source=SpringerLinkAdapter.name,
        query_count=1,
        result_count=len(result.records),
        download_count=len(result.downloads),
        errors=result.errors,
        sensitive_log_leak_detected=bool(findings),
    )
    LiteratureAuditLogger(writer.run_root / "audit" / "LITERATURE_EVENTS.jsonl").log(
        "hunnu_institutional_springer_acceptance_finalized",
        status=result.status.value,
        paper_id=record.paper_id,
        stable_identifier=record.stable_identifier,
        institutional_route_resolved=rechecked_route.institutional_route_resolved,
        institutional_target_database_match=rechecked_route.institutional_target_database_match,
        full_text_access_rechecked=rechecked_route.full_text_access_rechecked,
        full_text_accessible=rechecked_route.full_text_accessible,
        target_identity_confirmed=record.target_identity_confirmed,
        downloaded=bool(result.downloads),
        file_validated=bool(result.downloads and result.downloads[0].file_validation_passed),
    )
    return result


def finalize_captured_cnki_acceptance(
    *,
    request: LiteratureSearchRequest,
    query: str,
    search_html: str,
    article_html: str,
    article_url: str,
    downloaded_fulltext: Path | None,
    run_root: Path = CNKI_UPGRADE_RUN_ROOT,
    authorized_browser_download_confirmed: bool = False,
    allow_outside_project_for_tests: bool = False,
) -> LiteratureRunResult:
    """Finalize sanitized CNKI evidence and an explicitly confirmed browser download."""

    from .adapters.cnki import CNKIAdapter

    writer = LiteratureArtifactWriter(
        run_root,
        allow_outside_project_for_tests=allow_outside_project_for_tests,
    )
    manager = _download_manager_for_run(
        writer,
        allow_outside_project_for_tests=allow_outside_project_for_tests,
    )
    logger = LiteratureAuditLogger(writer.run_root / "audit" / "LITERATURE_EVENTS.jsonl")
    writer.write_search_request(request)
    found = CNKIAdapter.parse_search_results_html(
        search_html,
        query=query,
        source_url="https://kns.cnki.net/kns8s/defaultresult/index",
        max_results=request.max_results_per_source,
    )
    record = CNKIAdapter.parse_article_html(article_html, source_url=article_url, search_query=query)
    access = CNKIAdapter.check_fulltext_access_html(article_html, source_url=article_url)
    capture_prevalidation_error: str | None = None
    if downloaded_fulltext is not None and authorized_browser_download_confirmed and not access.authorized_access:
        if access.download_locator == UNKNOWN:
            capture_prevalidation_error = "Captured file was not tied to an official single-paper CNKI control"
        else:
            captured_format = infer_full_text_format(downloaded_fulltext, access.full_text_format)
            captured_validation = AuthorizedFullTextValidator.validate(
                downloaded_fulltext,
                captured_format,
                record=record,
            )
            if not captured_validation.passed:
                capture_prevalidation_error = captured_validation.error
            else:
                access = AccessDecision(
                    full_text_accessible=True,
                    access_type=AccessType.INSTITUTIONAL_AUTHENTICATED,
                    authorized_access=True,
                    status=RunStatus.SUCCESS,
                    reason=(
                        "User-confirmed normal CNKI single-paper browser download; "
                        "captured file passed format validation"
                    ),
                    download_url=access.download_url,
                    download_locator=access.download_locator,
                    full_text_format=captured_format,
                )
    record.full_text_accessible = access.full_text_accessible
    record.access_type = access.access_type.value
    record.full_text_format = access.full_text_format.value
    identity_match = next((item for item in found if CNKIAdapter.identity_matches(item, record)[0]), None)
    errors: list[str] = []
    status = RunStatus.SUCCESS
    if identity_match is None:
        record.target_identity_confirmed = False
        record.error_status = RunStatus.SOURCE_LAYOUT_CHANGED.value
        record.error_reason = "CNKI search/detail target identity lock failed"
        errors.append(record.error_reason)
        status = RunStatus.SOURCE_LAYOUT_CHANGED
    else:
        record.paper_id = identity_match.paper_id
        record.canonical_paper_id = identity_match.paper_id
        record.target_identity_confirmed = True

    LiteratureScreener().screen(record, request)
    records = [record]
    LiteratureDeduplicator().deduplicate(records)
    downloads: list[DownloadManifestEntry] = []
    if downloaded_fulltext is not None and record.target_identity_confirmed:
        try:
            if capture_prevalidation_error is not None:
                raise InvalidFullTextDownload(capture_prevalidation_error)
            downloads.append(manager.archive_authorized_fulltext(downloaded_fulltext, record, access))
        except (InvalidFullTextDownload, UnauthorizedFullTextError, OSError) as exc:
            status = RunStatus.DOWNLOAD_FAILED
            record.error_status = status.value
            record.error_reason = str(exc)
            errors.append(str(exc))
    elif request.require_full_text and status == RunStatus.SUCCESS:
        status = RunStatus.FULLTEXT_NOT_AUTHORIZED if not access.authorized_access else RunStatus.DOWNLOAD_FAILED
        errors.append("No authorized CNKI full-text file was supplied for captured acceptance")

    writer.append_query_log(
        QueryLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=CNKIAdapter.name,
            original_research_request=request.original_research_request,
            generated_query=query,
            filters=json.dumps(
                {
                    "YearStart": request.year_start or UNKNOWN,
                    "YearEnd": request.year_end or UNKNOWN,
                    "MaxResultsPerSource": request.max_results_per_source,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            results_returned=len(found),
            results_inspected=1,
            errors=UNKNOWN if not errors else "; ".join(errors),
        )
    )
    writer.write_search_results(records)
    writer.write_screening_decisions(records)
    writer.write_download_manifest(downloads)
    writer.write_sha256s(downloads)
    writer.write_obsidian_handoff(records)
    scan_targets = (
        writer.search_request_path,
        writer.query_log_path,
        writer.search_results_path,
        writer.screening_path,
        writer.download_manifest_path,
        writer.handoff_path,
    )
    findings = scan_files_for_sensitive_leaks(scan_targets)
    if findings:
        status = RunStatus.PARTIAL_SUCCESS
        errors.append(f"Sensitive credential-shaped content detected in {len(findings)} artifact location(s)")
    writer.write_run_audit(
        status=status.value,
        source=CNKIAdapter.name,
        query_count=1,
        result_count=1,
        download_count=len(downloads),
        errors=errors,
        sensitive_log_leak_detected=bool(findings),
    )
    logger.log(
        "captured_live_acceptance_finalized",
        status=status.value,
        source=CNKIAdapter.name,
        stable_identifier=record.stable_identifier,
        authorized_access=access.authorized_access,
        full_text_format=access.full_text_format.value,
        downloaded=bool(downloads),
        file_validated=bool(downloads and downloads[0].file_validation_passed),
        target_identity_confirmed=record.target_identity_confirmed,
    )
    return LiteratureRunResult(status=status, records=records, downloads=downloads, errors=errors)


def finalize_manual_download_handoff_acceptance(
    *,
    request: LiteratureSearchRequest,
    record: LiteratureRecord,
    access: AccessDecision,
    staged_pdf: Path,
    run_root: Path,
    institutional_routes: Iterable[Any] = (),
    allow_outside_project_for_tests: bool = False,
) -> LiteratureRunResult:
    """Finalize one identity-locked manual browser download through production.

    The user-visible native Viewer click and filesystem detection happen before
    this function. The supplied file must already be a controlled staging copy,
    and the source adapter must have confirmed local target identity. This
    function reuses the existing validator, download manager, SHA256 manifest,
    artifact writer, and archive layout; it performs no browser or network work.
    """

    writer = LiteratureArtifactWriter(
        run_root,
        allow_outside_project_for_tests=allow_outside_project_for_tests,
    )
    manager = _download_manager_for_run(
        writer,
        allow_outside_project_for_tests=allow_outside_project_for_tests,
    )
    logger = LiteratureAuditLogger(writer.run_root / "audit" / "LITERATURE_EVENTS.jsonl")
    writer.write_search_request(request)
    routes = tuple(institutional_routes)
    record.full_text_accessible = access.full_text_accessible
    record.access_type = access.access_type.value
    record.full_text_format = access.full_text_format.value
    LiteratureScreener().screen(record, request)
    records = [record]
    LiteratureDeduplicator().deduplicate(records)
    downloads: list[DownloadManifestEntry] = []
    errors: list[str] = []
    status = RunStatus.SUCCESS
    if record.acquisition_method != "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF":
        errors.append("Manual handoff acquisition method was not established")
        status = RunStatus.DOWNLOAD_FAILED
    elif not record.target_identity_confirmed:
        errors.append("Manual handoff target identity was not confirmed")
        status = RunStatus.DOWNLOAD_FAILED
    elif not record.manual_download_detected or not record.human_download_action:
        errors.append("Manual handoff download detection or human action evidence is missing")
        status = RunStatus.DOWNLOAD_FAILED
    else:
        try:
            downloads.append(manager.archive_authorized_pdf(staged_pdf, record, access))
        except (InvalidPDFDownload, UnauthorizedFullTextError, OSError) as exc:
            errors.append(str(exc))
            record.error_status = RunStatus.DOWNLOAD_FAILED.value
            record.error_reason = str(exc)
            status = RunStatus.DOWNLOAD_FAILED

    writer.append_query_log(
        QueryLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=record.source_database,
            original_research_request=request.original_research_request,
            generated_query=record.search_query,
            filters=json.dumps(
                {
                    "AcquisitionMethod": "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF",
                    "MaxDownloads": 1,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            results_returned=1,
            results_inspected=1,
            errors=UNKNOWN if not errors else "; ".join(errors),
        )
    )
    writer.write_search_results(records)
    writer.write_screening_decisions(records)
    writer.write_download_manifest(downloads, institutional_routes=routes)
    if routes:
        writer.write_institutional_route_provenance(routes)
    writer.write_sha256s(downloads)
    writer.write_obsidian_handoff(records)
    scan_targets = [
        writer.search_request_path,
        writer.query_log_path,
        writer.search_results_path,
        writer.screening_path,
        writer.download_manifest_path,
        writer.handoff_path,
    ]
    if routes:
        scan_targets.append(writer.institutional_route_provenance_path)
    findings = scan_files_for_sensitive_leaks(scan_targets)
    if findings:
        errors.append(f"Sensitive credential-shaped content detected in {len(findings)} artifact location(s)")
        status = RunStatus.PARTIAL_SUCCESS
    writer.write_run_audit(
        status=status.value,
        source=record.source_database,
        query_count=1,
        result_count=1,
        download_count=len(downloads),
        errors=errors,
        sensitive_log_leak_detected=bool(findings),
    )
    logger.log(
        "manual_download_handoff_finalized",
        status=status.value,
        source=record.source_database,
        stable_identifier=record.stable_identifier,
        acquisition_method=record.acquisition_method,
        manual_download_detected=record.manual_download_detected,
        human_download_action=record.human_download_action,
        original_manual_download_preserved=record.original_manual_download_preserved,
        file_validated=bool(downloads and downloads[0].file_validation_passed),
        target_identity_confirmed=record.target_identity_confirmed,
    )
    return LiteratureRunResult(status=status, records=records, downloads=downloads, errors=errors)


def finalize_unattended_download_acceptance(
    *,
    request: LiteratureSearchRequest,
    record: LiteratureRecord,
    access: AccessDecision,
    staged_pdf: Path,
    run_root: Path,
    institutional_routes: Iterable[Any] = (),
    allow_outside_project_for_tests: bool = False,
) -> LiteratureRunResult:
    """Finalize one automatically initiated, identity-locked browser download.

    Browser interaction and local PDF identity validation happen before this
    function. The supplied completed PDF is passed through the existing
    production validator, SHA256 manifest, and archive pipeline. No browser,
    network, native-viewer, or manual-download action is performed here.
    """

    writer = LiteratureArtifactWriter(
        run_root,
        allow_outside_project_for_tests=allow_outside_project_for_tests,
    )
    manager = _download_manager_for_run(
        writer,
        allow_outside_project_for_tests=allow_outside_project_for_tests,
    )
    logger = LiteratureAuditLogger(writer.run_root / "audit" / "LITERATURE_EVENTS.jsonl")
    writer.write_search_request(request)
    routes = tuple(institutional_routes)
    record.full_text_accessible = access.full_text_accessible
    record.access_type = access.access_type.value
    record.full_text_format = access.full_text_format.value
    LiteratureScreener().screen(record, request)
    records = [record]
    LiteratureDeduplicator().deduplicate(records)
    downloads: list[DownloadManifestEntry] = []
    errors: list[str] = []
    status = RunStatus.SUCCESS
    allowed_methods = {"PLAYWRIGHT_DOWNLOAD_EVENT", "AUTHORIZED_PDF_RESPONSE"}
    if record.acquisition_method not in allowed_methods:
        errors.append("Unattended browser acquisition method was not established")
        status = RunStatus.DOWNLOAD_FAILED
    elif not record.target_identity_confirmed:
        errors.append("Unattended download target identity was not confirmed")
        status = RunStatus.DOWNLOAD_FAILED
    elif not (
        record.unattended_download_attempted
        and record.automatic_download_initiation
        and record.automatic_download_detection
        and record.oxford_unattended_download_ready
    ):
        errors.append("Unattended download initiation or detection evidence is missing")
        status = RunStatus.DOWNLOAD_FAILED
    elif (
        record.user_native_viewer_click_required
        or record.manual_download_handoff_used
        or record.human_download_action
    ):
        errors.append("Human download action cannot satisfy unattended acceptance")
        status = RunStatus.DOWNLOAD_FAILED
    else:
        try:
            downloads.append(manager.archive_authorized_pdf(staged_pdf, record, access))
        except (InvalidPDFDownload, UnauthorizedFullTextError, OSError) as exc:
            errors.append(str(exc))
            record.error_status = RunStatus.DOWNLOAD_FAILED.value
            record.error_reason = str(exc)
            status = RunStatus.DOWNLOAD_FAILED

    writer.append_query_log(
        QueryLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=record.source_database,
            original_research_request=request.original_research_request,
            generated_query=record.search_query,
            filters=json.dumps(
                {
                    "AcquisitionMethod": record.acquisition_method,
                    "MaxDownloads": 1,
                    "UserNativeViewerClickRequired": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            results_returned=1,
            results_inspected=1,
            errors=UNKNOWN if not errors else "; ".join(errors),
        )
    )
    writer.write_search_results(records)
    writer.write_screening_decisions(records)
    writer.write_download_manifest(downloads, institutional_routes=routes)
    if routes:
        writer.write_institutional_route_provenance(routes)
    writer.write_sha256s(downloads)
    writer.write_obsidian_handoff(records)
    scan_targets = [
        writer.search_request_path,
        writer.query_log_path,
        writer.search_results_path,
        writer.screening_path,
        writer.download_manifest_path,
        writer.handoff_path,
    ]
    if routes:
        scan_targets.append(writer.institutional_route_provenance_path)
    findings = scan_files_for_sensitive_leaks(scan_targets)
    if findings:
        errors.append(f"Sensitive credential-shaped content detected in {len(findings)} artifact location(s)")
        status = RunStatus.PARTIAL_SUCCESS
    writer.write_run_audit(
        status=status.value,
        source=record.source_database,
        query_count=1,
        result_count=1,
        download_count=len(downloads),
        errors=errors,
        sensitive_log_leak_detected=bool(findings),
    )
    logger.log(
        "unattended_download_finalized",
        status=status.value,
        source=record.source_database,
        stable_identifier=record.stable_identifier,
        acquisition_method=record.acquisition_method,
        automatic_download_initiation=record.automatic_download_initiation,
        automatic_download_detection=record.automatic_download_detection,
        user_native_viewer_click_required=record.user_native_viewer_click_required,
        file_validated=bool(downloads and downloads[0].file_validation_passed),
        target_identity_confirmed=record.target_identity_confirmed,
    )
    return LiteratureRunResult(status=status, records=records, downloads=downloads, errors=errors)
