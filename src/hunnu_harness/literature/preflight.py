"""Dynamic, all-sources preflight coordination for unattended research runs.

The coordinator is intentionally source-agnostic.  Source-specific browser and
download behavior remains in registered handlers backed by existing adapters.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from ..paths import V028_RUN_ROOT, require_output_path
from .adapters.base import SourceActionRequired, SourceUserDownloadRequired
from .models import LiteratureRunResult, LiteratureSearchRequest, RunStatus, UNKNOWN
from .security import sanitize_value
from .workflow import LiteratureAcquisitionWorkflow


PREFLIGHT_MAX_DOWNLOADS_PER_SOURCE = 1
DEFAULT_HIGH_COST_SOURCE_THRESHOLD = 10


class PreflightStatus(str, Enum):
    READY_FOR_UNATTENDED = "READY_FOR_UNATTENDED"
    ACTION_REQUIRED_USER = "ACTION_REQUIRED_USER"
    NOT_READY = "NOT_READY"


class AuthenticationSweepStatus(str, Enum):
    READY_TO_CONTINUE = "READY_TO_CONTINUE"
    ACTION_REQUIRED_USER = "ACTION_REQUIRED_USER"
    NOT_READY = "NOT_READY"


class PreflightPhase(str, Enum):
    AUTHENTICATION_SWEEP = "AUTHENTICATION_SWEEP"
    DOWNLOAD_PREFLIGHT = "DOWNLOAD_PREFLIGHT"


class RequiredActionType(str, Enum):
    NONE = "NONE"
    CAPTCHA = "CAPTCHA"
    SSO = "SSO"
    LOGIN = "LOGIN"
    MFA = "MFA"
    SECURITY_CHALLENGE = "SECURITY_CHALLENGE"
    MANUAL_AUTHENTICATION = "MANUAL_AUTHENTICATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SourcePreflightCapabilities:
    source: str
    supports_search: bool
    supports_fulltext_access_check: bool
    supports_authorized_download: bool
    supports_unattended_download: bool
    supports_preflight: bool

    @property
    def supports_unattended_preflight(self) -> bool:
        return all(
            (
                self.supports_search,
                self.supports_fulltext_access_check,
                self.supports_authorized_download,
                self.supports_unattended_download,
                self.supports_preflight,
            )
        )

    @classmethod
    def from_adapter_type(cls, source: str, adapter_type: type[Any]) -> "SourcePreflightCapabilities":
        return cls(
            source=source,
            supports_search=bool(getattr(adapter_type, "supports_search", True)),
            supports_fulltext_access_check=bool(
                getattr(adapter_type, "supports_fulltext_access_check", True)
            ),
            supports_authorized_download=bool(
                getattr(adapter_type, "supports_authorized_download", True)
            ),
            supports_unattended_download=bool(
                getattr(adapter_type, "supports_unattended_download", False)
            ),
            supports_preflight=bool(getattr(adapter_type, "supports_preflight", False)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "Source": self.source,
            "SupportsSearch": self.supports_search,
            "SupportsFullTextAccessCheck": self.supports_fulltext_access_check,
            "SupportsAuthorizedDownload": self.supports_authorized_download,
            "SupportsUnattendedDownload": self.supports_unattended_download,
            "SupportsPreflight": self.supports_preflight,
        }


class SourceCapabilityRegistry:
    """Registry-driven source capabilities; the coordinator has no site branches."""

    def __init__(self) -> None:
        self._capabilities: dict[str, SourcePreflightCapabilities] = {}

    def register(self, capabilities: SourcePreflightCapabilities) -> None:
        self._capabilities[capabilities.source] = capabilities

    def register_adapter(self, source: str, adapter_type: type[Any]) -> None:
        self.register(SourcePreflightCapabilities.from_adapter_type(source, adapter_type))

    @classmethod
    def from_adapter_registry(cls, adapters: Mapping[str, type[Any]]) -> "SourceCapabilityRegistry":
        registry = cls()
        for source, adapter_type in adapters.items():
            registry.register_adapter(source, adapter_type)
        return registry

    def get(self, source: str) -> SourcePreflightCapabilities | None:
        return self._capabilities.get(source)

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(self._capabilities)


@dataclass(frozen=True)
class SourceAuthenticationSweepResult:
    source: str
    status: AuthenticationSweepStatus
    source_reachable: bool = False
    institutional_route_ready: bool = False
    authentication_ready: bool = False
    page_identity_confirmed: bool = False
    user_action_currently_required: bool = False
    required_action_type: RequiredActionType = RequiredActionType.NONE
    browser_ready_for_manual_action: bool = False
    reason: str = UNKNOWN
    route_provenance: tuple[str, ...] = ()
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return sanitize_value(
            {
                "Source": self.source,
                "Status": self.status.value,
                "SourceReachable": self.source_reachable,
                "InstitutionalRouteReady": self.institutional_route_ready,
                "AuthenticationReady": self.authentication_ready,
                "PageIdentityConfirmed": self.page_identity_confirmed,
                "UserActionCurrentlyRequired": self.user_action_currently_required,
                "RequiredActionType": self.required_action_type.value,
                "BrowserReadyForManualAction": self.browser_ready_for_manual_action,
                "Reason": self.reason,
                "RouteProvenance": list(self.route_provenance),
                "Timestamp": self.timestamp,
            }
        )


@dataclass(frozen=True)
class SourcePreflightResult:
    source: str
    status: PreflightStatus
    phase: PreflightPhase = PreflightPhase.DOWNLOAD_PREFLIGHT
    source_reached: bool = False
    authentication_ready: bool = False
    search_passed: bool = False
    target_article_reached: bool = False
    full_text_access_confirmed: bool = False
    download_passed: bool = False
    file_validated: bool = False
    target_identity_confirmed: bool = False
    current_session_download_verified: bool = False
    downloads_attempted: int = 0
    downloads_completed: int = 0
    preflight_paper_is_research_candidate: bool = False
    preflight_paper_purpose: str = "SOURCE_READINESS_CHECK"
    paper_id: str = UNKNOWN
    title: str = UNKNOWN
    doi: str = UNKNOWN
    local_path: str = UNKNOWN
    sha256: str = UNKNOWN
    user_action_currently_required: bool = False
    required_action_type: RequiredActionType = RequiredActionType.NONE
    browser_ready_for_manual_action: bool = False
    reason: str = UNKNOWN
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if self.downloads_attempted > PREFLIGHT_MAX_DOWNLOADS_PER_SOURCE:
            raise ValueError("PreflightMaxDownloadsPerSource=1")
        if self.downloads_completed > PREFLIGHT_MAX_DOWNLOADS_PER_SOURCE:
            raise ValueError("PreflightMaxDownloadsPerSource=1")

    @property
    def readiness_evidence_complete(self) -> bool:
        return all(
            (
                self.source_reached,
                self.authentication_ready,
                self.search_passed,
                self.target_article_reached,
                self.full_text_access_confirmed,
                self.download_passed,
                self.file_validated,
                self.target_identity_confirmed,
                self.current_session_download_verified,
                self.downloads_completed == 1,
                not self.user_action_currently_required,
            )
        )

    @classmethod
    def ready(
        cls,
        source: str,
        *,
        paper_id: str,
        title: str,
        doi: str,
        local_path: str,
        sha256: str,
        research_candidate: bool,
        purpose: str | None = None,
    ) -> "SourcePreflightResult":
        result = cls(
            source=source,
            status=PreflightStatus.READY_FOR_UNATTENDED,
            source_reached=True,
            authentication_ready=True,
            search_passed=True,
            target_article_reached=True,
            full_text_access_confirmed=True,
            download_passed=True,
            file_validated=True,
            target_identity_confirmed=True,
            current_session_download_verified=True,
            downloads_attempted=1,
            downloads_completed=1,
            preflight_paper_is_research_candidate=research_candidate,
            preflight_paper_purpose=purpose
            or ("RESEARCH_CANDIDATE" if research_candidate else "SOURCE_READINESS_CHECK"),
            paper_id=paper_id,
            title=title,
            doi=doi,
            local_path=local_path,
            sha256=sha256,
        )
        if not result.readiness_evidence_complete:
            raise ValueError("READY_FOR_UNATTENDED requires complete current-session evidence")
        return result

    def as_dict(self) -> dict[str, Any]:
        return sanitize_value(
            {
                "Source": self.source,
                "PreflightStatus": self.status.value,
                "Phase": self.phase.value,
                "SourceReached": self.source_reached,
                "AuthenticationReady": self.authentication_ready,
                "SearchPassed": self.search_passed,
                "TargetArticleReached": self.target_article_reached,
                "FullTextAccessConfirmed": self.full_text_access_confirmed,
                "DownloadPassed": self.download_passed,
                "FileValidated": self.file_validated,
                "TargetIdentityConfirmed": self.target_identity_confirmed,
                "CurrentSessionDownloadVerified": self.current_session_download_verified,
                "DownloadsAttempted": self.downloads_attempted,
                "DownloadsCompleted": self.downloads_completed,
                "PreflightPaperIsResearchCandidate": self.preflight_paper_is_research_candidate,
                "PreflightPaperPurpose": self.preflight_paper_purpose,
                "PaperID": self.paper_id,
                "Title": self.title,
                "DOI": self.doi,
                "LocalPath": self.local_path,
                "SHA256": self.sha256,
                "UserActionCurrentlyRequired": self.user_action_currently_required,
                "RequiredActionType": self.required_action_type.value,
                "BrowserReadyForManualAction": self.browser_ready_for_manual_action,
                "Reason": self.reason,
                "Timestamp": self.timestamp,
            }
        )


@dataclass(frozen=True)
class SourcePreflightContext:
    research_request_id: str
    source: str
    run_root: Path
    max_downloads: int = PREFLIGHT_MAX_DOWNLOADS_PER_SOURCE
    formal_download_cap: int = 0


class SourcePreflightHandler(Protocol):
    async def authentication_sweep(
        self, context: SourcePreflightContext
    ) -> SourceAuthenticationSweepResult: ...

    async def download_preflight(self, context: SourcePreflightContext) -> SourcePreflightResult: ...


AuthenticationProbe = Callable[
    [SourcePreflightContext], Awaitable[SourceAuthenticationSweepResult]
]


class LiteratureAdapterPreflightHandler:
    """Thin adapter/workflow bridge; no source DOM or download logic is duplicated."""

    def __init__(
        self,
        *,
        adapter: Any,
        request: LiteratureSearchRequest,
        authentication_probe: AuthenticationProbe,
        institutional_resolver: Any = None,
        institutional_trigger: Any = None,
        research_candidate: bool = True,
    ) -> None:
        self.adapter = adapter
        self.request = request
        self.authentication_probe = authentication_probe
        self.institutional_resolver = institutional_resolver
        self.institutional_trigger = institutional_trigger
        self.research_candidate = research_candidate

    async def authentication_sweep(
        self, context: SourcePreflightContext
    ) -> SourceAuthenticationSweepResult:
        return await self.authentication_probe(context)

    async def download_preflight(self, context: SourcePreflightContext) -> SourcePreflightResult:
        request = replace(
            self.request,
            max_search_results=1,
            max_results_per_source=1,
            max_downloads=1,
            max_downloads_per_run=1,
            require_full_text=True,
        )
        started_at_ns = datetime.now(timezone.utc).timestamp() * 1_000_000_000
        workflow = LiteratureAcquisitionWorkflow(
            self.adapter,
            run_root=context.run_root,
            institutional_resolver=self.institutional_resolver,
            institutional_trigger=self.institutional_trigger,
        )
        try:
            result = await workflow.run(request)
        except SourceActionRequired as exc:
            return _action_required_result(context.source, str(exc))
        except SourceUserDownloadRequired as exc:
            return _not_ready_result(
                context.source,
                "UNATTENDED_DOWNLOAD_UNSUPPORTED",
                reason=str(exc),
            )
        return _result_from_literature_run(
            context.source,
            result,
            started_at_ns=int(started_at_ns),
            research_candidate=self.research_candidate,
        )


def _required_action_type(reason: str) -> RequiredActionType:
    lowered = reason.casefold()
    if any(marker in lowered for marker in ("captcha", "slider", "滑块", "验证码")):
        return RequiredActionType.CAPTCHA
    if any(marker in lowered for marker in ("2fa", "mfa", "otp", "短信")):
        return RequiredActionType.MFA
    if any(marker in lowered for marker in ("sso", "carsi", "cas", "统一身份")):
        return RequiredActionType.SSO
    if any(marker in lowered for marker in ("security challenge", "security verification", "安全验证")):
        return RequiredActionType.SECURITY_CHALLENGE
    if any(marker in lowered for marker in ("login", "sign in", "登录")):
        return RequiredActionType.LOGIN
    return RequiredActionType.MANUAL_AUTHENTICATION


def _action_required_result(source: str, reason: str) -> SourcePreflightResult:
    return SourcePreflightResult(
        source=source,
        status=PreflightStatus.ACTION_REQUIRED_USER,
        user_action_currently_required=True,
        required_action_type=_required_action_type(reason),
        browser_ready_for_manual_action=True,
        reason=reason,
    )


def _not_ready_result(source: str, code: str, *, reason: str | None = None) -> SourcePreflightResult:
    return SourcePreflightResult(
        source=source,
        status=PreflightStatus.NOT_READY,
        reason=reason or code,
    )


def _result_from_literature_run(
    source: str,
    result: LiteratureRunResult,
    *,
    started_at_ns: int,
    research_candidate: bool,
) -> SourcePreflightResult:
    if result.status == RunStatus.ACTION_REQUIRED_USER_LOGIN:
        return _action_required_result(source, result.action_required_reason)
    if result.status == RunStatus.ACTION_REQUIRED_USER_DOWNLOAD:
        return _not_ready_result(
            source,
            "UNATTENDED_DOWNLOAD_UNSUPPORTED",
            reason=result.action_required_reason,
        )
    if result.status != RunStatus.SUCCESS or len(result.downloads) != 1:
        return _not_ready_result(source, f"WORKFLOW_STATUS_{result.status.value}")
    entry = result.downloads[0]
    local = Path(entry.local_path)
    current_session = False
    try:
        current_session = local.exists() and local.stat().st_mtime_ns >= started_at_ns
    except OSError:
        current_session = False
    unattended_action = not any(
        (
            entry.manual_download_handoff_used,
            entry.human_download_action,
            entry.user_native_viewer_click_required,
        )
    )
    evidence_complete = all(
        (
            entry.authorized_access,
            entry.file_validation_passed,
            entry.target_identity_confirmed,
            current_session,
            unattended_action,
            bool(entry.sha256 and entry.sha256 != UNKNOWN),
        )
    )
    if not evidence_complete:
        return SourcePreflightResult(
            source=source,
            status=PreflightStatus.NOT_READY,
            source_reached=True,
            authentication_ready=True,
            search_passed=bool(result.records),
            target_article_reached=bool(result.records),
            full_text_access_confirmed=entry.authorized_access,
            download_passed=True,
            file_validated=entry.file_validation_passed,
            target_identity_confirmed=entry.target_identity_confirmed,
            current_session_download_verified=current_session,
            downloads_attempted=1,
            downloads_completed=1,
            reason="INCOMPLETE_UNATTENDED_READINESS_EVIDENCE",
        )
    return SourcePreflightResult.ready(
        source,
        paper_id=entry.paper_id,
        title=entry.title,
        doi=entry.doi,
        local_path=entry.local_path,
        sha256=entry.sha256,
        research_candidate=research_candidate,
    )


@dataclass(frozen=True)
class MultiSourcePreflightAggregate:
    research_request_id: str
    planned_sources: tuple[str, ...]
    authentication_results: tuple[SourceAuthenticationSweepResult, ...]
    source_results: tuple[SourcePreflightResult, ...]
    initial_authentication_results: tuple[SourceAuthenticationSweepResult, ...] = ()
    planning_and_budget_gate: bool = False
    proceed_with_partial_sources: bool = False
    formal_research_run_started: bool = False
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def sources_requiring_user_action(self) -> tuple[SourcePreflightResult, ...]:
        return tuple(
            item for item in self.source_results if item.status == PreflightStatus.ACTION_REQUIRED_USER
        )

    @property
    def not_ready_sources(self) -> tuple[SourcePreflightResult, ...]:
        return tuple(item for item in self.source_results if item.status == PreflightStatus.NOT_READY)

    @property
    def ready_sources(self) -> tuple[SourcePreflightResult, ...]:
        return tuple(
            item for item in self.source_results if item.status == PreflightStatus.READY_FOR_UNATTENDED
        )

    @property
    def unattended_run_clearance(self) -> bool:
        return (
            bool(self.planned_sources)
            and len(self.ready_sources) == len(self.planned_sources)
            and not self.sources_requiring_user_action
            and not self.not_ready_sources
            and not self.planning_and_budget_gate
        )

    @property
    def formal_research_run_allowed(self) -> bool:
        if self.unattended_run_clearance:
            return True
        return (
            self.proceed_with_partial_sources
            and bool(self.ready_sources)
            and not self.sources_requiring_user_action
            and not self.planning_and_budget_gate
        )

    def as_dict(self) -> dict[str, Any]:
        initial_results = self.initial_authentication_results or self.authentication_results
        initially_ready = sum(
            item.status == AuthenticationSweepStatus.READY_TO_CONTINUE
            for item in initial_results
        )
        initially_action = sum(
            item.status == AuthenticationSweepStatus.ACTION_REQUIRED_USER
            for item in initial_results
        )
        initially_not_ready = sum(
            item.status == AuthenticationSweepStatus.NOT_READY
            for item in initial_results
        )
        return sanitize_value(
            {
                "SchemaVersion": "0.2.8",
                "ResearchRequestID": self.research_request_id,
                "GeneratedAt": self.generated_at,
                "PlannedSources": list(self.planned_sources),
                "PlannedSourceCount": len(self.planned_sources),
                "PreflightMaxDownloadsPerSource": PREFLIGHT_MAX_DOWNLOADS_PER_SOURCE,
                "MaximumPreflightDownloads": len(self.planned_sources),
                "AuthenticationSweepCompleted": len(self.authentication_results)
                == len(self.planned_sources),
                "SourcesInitiallyReady": initially_ready,
                "SourcesInitiallyRequiringUserAction": initially_action,
                "SourcesInitiallyNotReady": initially_not_ready,
                "AuthenticationResults": [item.as_dict() for item in self.authentication_results],
                "SourceResults": [item.as_dict() for item in self.source_results],
                "PreflightPassedSourceCount": len(self.ready_sources),
                "SourcesRequiringUserAction": len(self.sources_requiring_user_action),
                "SourcesRequiringUserActionList": [
                    item.source for item in self.sources_requiring_user_action
                ],
                "NotReadySourceCount": len(self.not_ready_sources),
                "NotReadySources": [item.source for item in self.not_ready_sources],
                "BatchUserActionRequired": bool(self.sources_requiring_user_action),
                "BrowserReadyForManualAction": bool(self.sources_requiring_user_action),
                "UnattendedRunClearance": self.unattended_run_clearance,
                "SafeForUserToLeave": self.unattended_run_clearance,
                "ProceedWithPartialSources": self.proceed_with_partial_sources,
                "FormalResearchRunAllowed": self.formal_research_run_allowed,
                "FormalResearchRunStarted": self.formal_research_run_started,
                "PlanningAndBudgetGate": self.planning_and_budget_gate,
                "CandidateDownloadsCountedTowardFormalCap": sum(
                    item.downloads_completed
                    for item in self.ready_sources
                    if item.preflight_paper_is_research_candidate
                ),
                "ReadinessCheckDownloadOverhead": sum(
                    item.downloads_completed
                    for item in self.ready_sources
                    if not item.preflight_paper_is_research_candidate
                ),
                "ManualAuthentication": True,
                "ExistingLoginStatePreserved": True,
                "CredentialStorage": False,
                "CredentialsLogged": False,
                "CookiesExported": False,
                "TokensExported": False,
                "BrowserStateExported": False,
                "CaptchaBypass": False,
                "AuthenticationBypass": False,
                "PaywallBypass": False,
                "DRMBypass": False,
                "DownloadLimitBypass": False,
                "RateLimitBypass": False,
            }
        )


class MultiSourcePreflightCoordinator:
    """Coordinate dynamic N-source preflight without source-specific branches."""

    def __init__(
        self,
        *,
        capability_registry: SourceCapabilityRegistry,
        handlers: Mapping[str, SourcePreflightHandler],
        run_root: Path = V028_RUN_ROOT,
        high_cost_source_threshold: int = DEFAULT_HIGH_COST_SOURCE_THRESHOLD,
    ) -> None:
        self.registry = capability_registry
        self.handlers = dict(handlers)
        self.run_root = require_output_path(Path(run_root), label="Multi-source preflight run")
        self.high_cost_source_threshold = high_cost_source_threshold
        self._planned_sources: tuple[str, ...] = ()
        self._auth_results: dict[str, SourceAuthenticationSweepResult] = {}
        self._initial_auth_results: dict[str, SourceAuthenticationSweepResult] = {}
        self._source_results: dict[str, SourcePreflightResult] = {}
        self._formal_download_cap = 0
        self._research_request_id = UNKNOWN

    async def run_initial(
        self,
        *,
        research_request_id: str,
        planned_sources: tuple[str, ...] | list[str],
        formal_download_cap: int,
        budget_confirmed: bool = False,
        proceed_with_partial_sources: bool = False,
    ) -> MultiSourcePreflightAggregate:
        self._planned_sources = tuple(dict.fromkeys(str(item) for item in planned_sources))
        self._research_request_id = research_request_id
        self._formal_download_cap = max(0, int(formal_download_cap))
        self._auth_results.clear()
        self._initial_auth_results.clear()
        self._source_results.clear()
        if len(self._planned_sources) > self.high_cost_source_threshold and not budget_confirmed:
            return self._aggregate(
                planning_and_budget_gate=True,
                proceed_with_partial_sources=proceed_with_partial_sources,
            )
        await self._authentication_sweep(self._planned_sources)
        self._initial_auth_results = dict(self._auth_results)
        if self._has_auth_blocker():
            self._materialize_auth_blockers()
            return self._aggregate(proceed_with_partial_sources=proceed_with_partial_sources)
        await self._download_preflight(self._planned_sources)
        self._enforce_formal_candidate_cap()
        return self._aggregate(proceed_with_partial_sources=proceed_with_partial_sources)

    async def resume_batch(
        self,
        *,
        proceed_with_partial_sources: bool = False,
    ) -> MultiSourcePreflightAggregate:
        if not self._planned_sources:
            raise RuntimeError("run_initial must be called before resume_batch")
        auth_sources = tuple(
            source
            for source, result in self._auth_results.items()
            if result.status == AuthenticationSweepStatus.ACTION_REQUIRED_USER
        )
        if auth_sources:
            await self._authentication_sweep(auth_sources)
        if self._has_auth_blocker():
            self._materialize_auth_blockers()
            return self._aggregate(proceed_with_partial_sources=proceed_with_partial_sources)
        download_sources = tuple(
            source
            for source in self._planned_sources
            if self._source_results.get(source) is None
            or self._source_results[source].status == PreflightStatus.ACTION_REQUIRED_USER
        )
        if download_sources:
            await self._download_preflight(download_sources)
        self._enforce_formal_candidate_cap()
        return self._aggregate(proceed_with_partial_sources=proceed_with_partial_sources)

    async def _authentication_sweep(self, sources: tuple[str, ...]) -> None:
        for source in sources:
            capabilities = self.registry.get(source)
            handler = self.handlers.get(source)
            if capabilities is None:
                self._auth_results[source] = SourceAuthenticationSweepResult(
                    source=source,
                    status=AuthenticationSweepStatus.NOT_READY,
                    reason="ADAPTER_MISSING",
                )
                continue
            if not capabilities.supports_unattended_preflight:
                self._auth_results[source] = SourceAuthenticationSweepResult(
                    source=source,
                    status=AuthenticationSweepStatus.NOT_READY,
                    reason="UNATTENDED_PREFLIGHT_UNSUPPORTED",
                )
                continue
            if handler is None:
                self._auth_results[source] = SourceAuthenticationSweepResult(
                    source=source,
                    status=AuthenticationSweepStatus.NOT_READY,
                    reason="PREFLIGHT_HANDLER_MISSING",
                )
                continue
            context = self._context(source)
            try:
                result = await handler.authentication_sweep(context)
            except SourceActionRequired as exc:
                result = SourceAuthenticationSweepResult(
                    source=source,
                    status=AuthenticationSweepStatus.ACTION_REQUIRED_USER,
                    source_reachable=True,
                    user_action_currently_required=True,
                    required_action_type=_required_action_type(str(exc)),
                    browser_ready_for_manual_action=True,
                    reason=str(exc),
                )
            except Exception as exc:
                result = SourceAuthenticationSweepResult(
                    source=source,
                    status=AuthenticationSweepStatus.NOT_READY,
                    reason=f"AUTHENTICATION_SWEEP_FAILED:{type(exc).__name__}",
                )
            self._auth_results[source] = result
            existing = self._source_results.get(source)
            if (
                result.status == AuthenticationSweepStatus.READY_TO_CONTINUE
                and existing is not None
                and existing.phase == PreflightPhase.AUTHENTICATION_SWEEP
            ):
                self._source_results.pop(source, None)

    async def _download_preflight(self, sources: tuple[str, ...]) -> None:
        for source in sources:
            handler = self.handlers[source]
            try:
                result = await handler.download_preflight(self._context(source))
            except SourceActionRequired as exc:
                result = _action_required_result(source, str(exc))
            except SourceUserDownloadRequired as exc:
                result = _not_ready_result(
                    source,
                    "UNATTENDED_DOWNLOAD_UNSUPPORTED",
                    reason=str(exc),
                )
            except Exception as exc:
                result = _not_ready_result(
                    source,
                    "DOWNLOAD_PREFLIGHT_FAILED",
                    reason=f"DOWNLOAD_PREFLIGHT_FAILED:{type(exc).__name__}",
                )
            if result.status == PreflightStatus.READY_FOR_UNATTENDED and not result.readiness_evidence_complete:
                result = replace(
                    result,
                    status=PreflightStatus.NOT_READY,
                    reason="INCOMPLETE_UNATTENDED_READINESS_EVIDENCE",
                )
            self._source_results[source] = result

    def _context(self, source: str) -> SourcePreflightContext:
        return SourcePreflightContext(
            research_request_id=self._research_request_id,
            source=source,
            run_root=self.run_root / "sources" / source,
            formal_download_cap=self._formal_download_cap,
        )

    def _has_auth_blocker(self) -> bool:
        return any(
            result.status != AuthenticationSweepStatus.READY_TO_CONTINUE
            for result in self._auth_results.values()
        )

    def _materialize_auth_blockers(self) -> None:
        for source in self._planned_sources:
            auth = self._auth_results.get(source)
            if auth is None or auth.status == AuthenticationSweepStatus.READY_TO_CONTINUE:
                continue
            if auth.status == AuthenticationSweepStatus.ACTION_REQUIRED_USER:
                self._source_results[source] = SourcePreflightResult(
                    source=source,
                    status=PreflightStatus.ACTION_REQUIRED_USER,
                    phase=PreflightPhase.AUTHENTICATION_SWEEP,
                    source_reached=auth.source_reachable,
                    authentication_ready=auth.authentication_ready,
                    user_action_currently_required=True,
                    required_action_type=auth.required_action_type,
                    browser_ready_for_manual_action=auth.browser_ready_for_manual_action,
                    reason=auth.reason,
                )
            else:
                self._source_results[source] = SourcePreflightResult(
                    source=source,
                    status=PreflightStatus.NOT_READY,
                    phase=PreflightPhase.AUTHENTICATION_SWEEP,
                    source_reached=auth.source_reachable,
                    authentication_ready=auth.authentication_ready,
                    reason=auth.reason,
                )

    def _enforce_formal_candidate_cap(self) -> None:
        remaining = self._formal_download_cap
        for source in self._planned_sources:
            result = self._source_results.get(source)
            if result is None or not result.preflight_paper_is_research_candidate:
                continue
            if remaining > 0:
                remaining -= result.downloads_completed
                continue
            self._source_results[source] = replace(
                result,
                preflight_paper_is_research_candidate=False,
                preflight_paper_purpose="SOURCE_READINESS_CHECK",
            )

    def _aggregate(
        self,
        *,
        planning_and_budget_gate: bool = False,
        proceed_with_partial_sources: bool = False,
    ) -> MultiSourcePreflightAggregate:
        aggregate = MultiSourcePreflightAggregate(
            research_request_id=self._research_request_id,
            planned_sources=self._planned_sources,
            authentication_results=tuple(
                self._auth_results[source]
                for source in self._planned_sources
                if source in self._auth_results
            ),
            source_results=tuple(
                self._source_results[source]
                for source in self._planned_sources
                if source in self._source_results
            ),
            initial_authentication_results=tuple(
                self._initial_auth_results[source]
                for source in self._planned_sources
                if source in self._initial_auth_results
            ),
            planning_and_budget_gate=planning_and_budget_gate,
            proceed_with_partial_sources=proceed_with_partial_sources,
        )
        PreflightArtifactWriter(self.run_root).write(aggregate)
        return aggregate


class PreflightArtifactWriter:
    def __init__(self, run_root: Path = V028_RUN_ROOT) -> None:
        self.run_root = require_output_path(Path(run_root), label="Preflight artifacts")

    def write(self, aggregate: MultiSourcePreflightAggregate) -> tuple[Path, Path | None]:
        manifest = self.run_root / "manifests" / "MULTISOURCE_PREFLIGHT_MANIFEST.json"
        self._atomic_json(manifest, aggregate.as_dict())
        action_path = self.run_root / "manifests" / "BATCH_USER_ACTION_REQUIRED.json"
        action_history = self._action_history(action_path)
        if aggregate.sources_requiring_user_action:
            current_actions = [
                {
                    "Source": item.source,
                    "Action": item.required_action_type.value,
                    "PageReady": item.browser_ready_for_manual_action,
                    "Phase": item.phase.value,
                    "Reason": item.reason,
                }
                for item in aggregate.sources_requiring_user_action
            ]
            for action in current_actions:
                if action not in action_history:
                    action_history.append(action)
            payload = {
                "SchemaVersion": "0.2.8",
                "ResearchRequestID": aggregate.research_request_id,
                "BatchUserActionRequired": True,
                "Resolved": False,
                "SourcesRequiringUserAction": len(aggregate.sources_requiring_user_action),
                "RequiredUserActions": current_actions,
                "UserActionHistory": action_history,
                "FormalResearchRunStarted": False,
                "UnattendedRunClearance": False,
            }
            self._atomic_json(action_path, payload)
            return manifest, action_path
        if action_path.exists():
            payload = {
                "SchemaVersion": "0.2.8",
                "ResearchRequestID": aggregate.research_request_id,
                "BatchUserActionRequired": False,
                "Resolved": True,
                "SourcesRequiringUserAction": 0,
                "RequiredUserActions": [],
                "UserActionHistory": action_history,
                "FormalResearchRunStarted": aggregate.formal_research_run_started,
                "UnattendedRunClearance": aggregate.unattended_run_clearance,
            }
            self._atomic_json(action_path, payload)
            return manifest, action_path
        return manifest, None

    @staticmethod
    def _action_history(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        history = list(existing.get("UserActionHistory", []))
        for action in existing.get("RequiredUserActions", []):
            if isinstance(action, dict) and action not in history:
                history.append(action)
        return history

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(sanitize_value(dict(payload)), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
