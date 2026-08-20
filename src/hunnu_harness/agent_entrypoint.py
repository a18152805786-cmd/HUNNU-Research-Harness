"""Stable agent-facing routing and adapter-first execution for Harness.

This module does not launch a browser, create a profile, or reimplement any
source adapter.  It validates a bounded request and delegates live literature
work through the Harness-controlled adapter factory and execution broker.
The browser session remains caller-owned; the Python runtime accepts the
backend-neutral BrowserCommandPort and wraps the old local Playwright-shaped
transport only at the compatibility boundary.  It still has no Codex MCP
bridge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .browser.port import BrowserCommandPort
from .browser.transport import BrowserTransport
from .batching import BoundedBatchPlanner, MultiBatchPlan, PER_BATCH_MAX_DOWNLOADS
from .literature.models import LiteratureRunResult, LiteratureSearchRequest, RunStatus
from .literature.security import sanitize_value
from .literature.workflow import LiteratureAcquisitionWorkflow
from .literature.preflight import SourceCapabilityRegistry
from .literature.execution import AdapterExecutionBroker, LiteratureAdapterFactory
from .literature.adapters import (
    CNKIAdapter,
    OxfordAcademicAdapter,
    ScienceDirectAdapter,
    SpringerLinkAdapter,
)
from .models import DownloadRequest
from .official_web import (
    OFFICIAL_WEB_ADAPTER_REGISTRY,
    OfficialWebExecutionBroker,
    OfficialWebRequest,
)
from .paths import OUTPUT_ROOT, V0217_RUN_ROOT, require_output_path
from .workflows import run_cnrds_download

if TYPE_CHECKING:
    from .downloads.manager import DownloadManager
    from .literature.adapters.base import LiteratureSourceAdapter
    from .literature.institutional import (
        InstitutionalAccessResolver,
        InstitutionalResolutionTrigger,
    )


SUPPORTED_LITERATURE_SOURCES = ("CNKI", "SpringerLink", "ScienceDirect", "OxfordAcademic")
LITERATURE_ADAPTER_REGISTRY = {
    "CNKI": CNKIAdapter,
    "SpringerLink": SpringerLinkAdapter,
    "ScienceDirect": ScienceDirectAdapter,
    "OxfordAcademic": OxfordAcademicAdapter,
}
SOURCE_CAPABILITY_REGISTRY = SourceCapabilityRegistry.from_adapter_registry(
    LITERATURE_ADAPTER_REGISTRY
)
SUPPORTED_DATA_SOURCES = ("CNRDS",)
SUPPORTED_OFFICIAL_WEB_SOURCES = ("OfficialWeb",)
DEFAULT_MAX_CANDIDATES = 30
DEFAULT_MAX_DOWNLOADS = 0
HIGH_COST_CANDIDATE_THRESHOLD = 30
HIGH_COST_DOWNLOAD_THRESHOLD = 10


class AgentRequestSecurityError(ValueError):
    """Raised when a request attempts to carry authentication material."""


def _canonical(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _field(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    values = {_canonical(key): value for key, value in mapping.items()}
    for name in names:
        if _canonical(name) in values:
            return values[_canonical(name)]
    return default


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = re.split(r"[\n,;；，]+", value)
    else:
        values = list(value)
    return tuple(str(item).strip() for item in values if str(item).strip())


def _boolean(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    raise ValueError(f"Expected a boolean value, received {value!r}")


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int, label: str) -> int:
    if value is None or value == "":
        return default
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _contains_auth_material(mapping: Mapping[str, Any]) -> bool:
    forbidden = (
        "password",
        "passwd",
        "cookie",
        "authorization",
        "bearer",
        "session",
        "token",
        "saml",
        "oauth",
        "credential",
        "otp",
        "mfa",
    )
    for key, value in mapping.items():
        canonical_key = _canonical(key)
        if any(term in canonical_key for term in forbidden):
            return True
        if isinstance(value, Mapping) and _contains_auth_material(value):
            return True
    return False


def _source_name(value: str) -> str | None:
    aliases = {
        "cnki": "CNKI",
        "中国知网": "CNKI",
        "知网": "CNKI",
        "springer": "SpringerLink",
        "springerlink": "SpringerLink",
        "springernature": "SpringerLink",
        "springernaturelink": "SpringerLink",
        "sciencedirect": "ScienceDirect",
        "elsevier": "ScienceDirect",
        "oxfordacademic": "OxfordAcademic",
        "oxford": "OxfordAcademic",
        "oxfordjournals": "OxfordAcademic",
        "oxfordjournalscollection": "OxfordAcademic",
        "oxforduniversitypress": "OxfordAcademic",
        "oup": "OxfordAcademic",
        "officialweb": "OfficialWeb",
        "officialwebsite": "OfficialWeb",
        "publicofficialweb": "OfficialWeb",
        "官方网站": "OfficialWeb",
        "官方网页": "OfficialWeb",
        "cnrds": "CNRDS",
        "cnfs": "CNRDS",
    }
    return aliases.get(re.sub(r"[\s_\-]+", "", value.casefold()))


def _normalize_languages(value: Any, original_request: str) -> tuple[str, ...]:
    requested = tuple(item.casefold() for item in _strings(value))
    aliases = {
        "zh": "zh",
        "chinese": "zh",
        "中文": "zh",
        "en": "en",
        "english": "en",
        "英文": "en",
    }
    normalized = tuple(dict.fromkeys(aliases[item] for item in requested if item in aliases))
    if normalized:
        return normalized
    if "中文" in original_request and "英文" not in original_request and "中英文" not in original_request:
        return ("zh",)
    if "英文" in original_request and "中文" not in original_request and "中英文" not in original_request:
        return ("en",)
    return ("zh", "en")


def _text_requests_download(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in ("下载", "全文", "download", "full text"))


def _distribute_cap(total: int, sources: tuple[str, ...]) -> dict[str, int]:
    """Split a global cap across sources without ever expanding it."""

    if not sources:
        return {}
    active = sources[: min(total, len(sources))] if total else sources
    if not active:
        return {}
    quotient, remainder = divmod(total, len(active))
    return {
        source: quotient + (1 if index < remainder else 0)
        for index, source in enumerate(active)
    }


@dataclass(frozen=True)
class LiteratureSourcePlan:
    source: str
    request: LiteratureSearchRequest
    max_candidates: int
    max_downloads: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "Source": self.source,
            "MaxCandidates": self.max_candidates,
            "MaxDownloads": self.max_downloads,
            "InvocationTarget": "AdapterExecutionBroker -> LiteratureAcquisitionWorkflow",
            "AdapterResolution": "LITERATURE_ADAPTER_REGISTRY[Source]",
            "InstitutionalResolver": "HUNNUInstitutionalAccessResolver(existing fallback)",
            "Request": self.request.as_dict(),
        }


@dataclass(frozen=True)
class AgentRoutingDecision:
    task_type: str
    status: str
    original_request: str = ""
    research_acquisition_intent_detected: bool = False
    harness_selected: bool = False
    harness_capability_available: bool = False
    request_validated: bool = False
    correct_router_selected: bool = False
    missing_capability: str = "unknown"
    missing_request_details: str = "unknown"
    selected_sources: tuple[str, ...] = ()
    literature_plans: tuple[LiteratureSourcePlan, ...] = ()
    data_request: DownloadRequest | None = None
    official_web_request: OfficialWebRequest | None = None
    multi_batch_plan: MultiBatchPlan | None = None
    planning_and_budget_gate: bool = False
    estimated_candidates: int = 0
    estimated_downloads: int = 0
    estimated_local_files_to_read: int = 0
    estimated_high_cost_risk: str = "Low"
    full_text_reading_mode: str = "LocalFile"
    authorized_full_text_only: bool = True
    screening_first: bool = True
    network_acquisition_performed: bool = False
    unattended_execution_requested: bool = False
    run_multi_source_preflight: bool = False

    @property
    def is_routable(self) -> bool:
        return (
            self.harness_selected
            and self.harness_capability_available
            and self.request_validated
            and not self.planning_and_budget_gate
        )

    def as_dict(self) -> dict[str, Any]:
        data_request = None
        if self.data_request is not None:
            data_request = {
                "Database": self.data_request.database,
                "Module": self.data_request.module,
                "Table": self.data_request.table,
                "Stocks": list(self.data_request.stocks),
                "DateStart": self.data_request.date_start,
                "DateEnd": self.data_request.date_end,
                "Fields": list(self.data_request.fields),
                "OutputFormat": self.data_request.output_format,
            }
        return sanitize_value(
            {
                "SchemaVersion": "0.2.9",
                "TaskType": self.task_type,
                "Status": self.status,
                "OriginalResearchRequest": self.original_request,
                "ResearchAcquisitionIntentDetected": self.research_acquisition_intent_detected,
                "HarnessSelected": self.harness_selected,
                "HarnessCapabilityAvailable": self.harness_capability_available,
                "RequestValidated": self.request_validated,
                "CorrectRouterSelected": self.correct_router_selected,
                "MissingCapability": self.missing_capability,
                "MissingRequestDetails": self.missing_request_details,
                "SelectedSources": list(self.selected_sources),
                "LiteraturePlans": [plan.as_dict() for plan in self.literature_plans],
                "DataRequest": data_request,
                "OfficialWebRequest": (
                    {
                        "Source": "OfficialWeb",
                        "URLs": list(self.official_web_request.urls),
                        "AllowedDomains": list(self.official_web_request.allowed_domains),
                        "OfficialDomainClaims": [
                            {
                                "Domain": claim.domain,
                                "SourceType": claim.source_type,
                                "Relationship": claim.relationship,
                            }
                            for claim in self.official_web_request.official_domain_claims
                        ],
                        "AllowDiscovery": self.official_web_request.allow_discovery,
                        "MaxPages": self.official_web_request.max_pages,
                        "InvocationTarget": "OfficialWebExecutionBroker -> PublicOfficialWebAdapter",
                        "BrowserExecution": "BrowserCommandPort Navigate/Observe",
                    }
                    if self.official_web_request is not None
                    else None
                ),
                "MultiBatchPlan": (
                    self.multi_batch_plan.as_dict() if self.multi_batch_plan is not None else None
                ),
                "OutputRoot": str(OUTPUT_ROOT),
                "PlanningAndBudgetGate": self.planning_and_budget_gate,
                "EstimatedSources": len(self.selected_sources),
                "EstimatedCandidates": self.estimated_candidates,
                "EstimatedDownloads": self.estimated_downloads,
                "EstimatedLocalFilesToRead": self.estimated_local_files_to_read,
                "EstimatedHighCostRisk": self.estimated_high_cost_risk,
                "DefaultFullTextReadingMode": "LocalFile",
                "FullTextReadingMode": self.full_text_reading_mode,
                "BrowserFullTextReadingPreferred": False,
                "AuthorizedDownloadPreferred": True,
                "AuthorizedFullTextOnly": self.authorized_full_text_only,
                "ScreeningFirst": self.screening_first,
                "NetworkAcquisitionPerformed": self.network_acquisition_performed,
                "UnattendedExecutionRequested": self.unattended_execution_requested,
                "RunMultiSourcePreflight": self.run_multi_source_preflight,
                "LiteratureExecutionEntryPoint": "AdapterExecutionBroker",
                "DirectBrowserFallbackForLiterature": False,
                "DirectTemporaryCrawlerAdded": False,
                "MCPExecutorImplemented": True,
                "BrowserSessionBrokerImplemented": True,
                "PlaywrightMCPTransportImplemented": True,
                "MCPTransportMode": "Agent-mediated typed MCP tool boundary; no Python Playwright object graph",
                "AuthenticatedFetchMCP": False,
                "PreflightCoordinator": (
                    "MultiSourcePreflightCoordinator"
                    if self.run_multi_source_preflight
                    else "not_required"
                ),
                "ManualDownloadHandoffSupported": "OxfordAcademic" in self.selected_sources,
                "TryUnattendedDownload": "OxfordAcademic" in self.selected_sources,
                "ManualDownloadHandoffIsFallback": "OxfordAcademic" in self.selected_sources,
                "ManualDownloadRequiredStatus": RunStatus.ACTION_REQUIRED_USER_DOWNLOAD.value,
                "ManualDownloadInstruction": (
                    "Official PDF is open; click the Chrome PDF Viewer native Download button once, "
                    "then Harness will automatically detect, validate, identity-lock, hash, and archive it."
                    if "OxfordAcademic" in self.selected_sources
                    else "not_applicable"
                ),
                "ManualAuthentication": True,
                "ExistingLoginStatePreserved": True,
                "CredentialStorage": False,
                "CredentialsLogged": False,
                "CookiesExported": False,
                "TokensExported": False,
                "BrowserStateExported": False,
                "CaptchaBypass": False,
                "PaywallBypass": False,
                "DRMBypass": False,
                "AuthenticationBypass": False,
                "DownloadLimitBypass": False,
                "RateLimitBypass": False,
                "UnauthorizedMirrorUse": False,
            }
        )


class AgentRequestRouter:
    """Route bounded requests and enforce Harness-controlled literature execution."""

    def __init__(
        self,
        *,
        adapter_factory: LiteratureAdapterFactory | None = None,
        adapter_registry: Mapping[str, type[Any]] | None = None,
    ) -> None:
        if adapter_factory is not None and adapter_registry is not None:
            raise ValueError("Pass adapter_factory or adapter_registry, not both")
        self.adapter_factory = adapter_factory or LiteratureAdapterFactory(
            adapter_registry or LITERATURE_ADAPTER_REGISTRY
        )
        self.adapter_execution_broker = AdapterExecutionBroker(self.adapter_factory)
        self.official_web_execution_broker = OfficialWebExecutionBroker(
            OFFICIAL_WEB_ADAPTER_REGISTRY
        )

    def route(self, request: Mapping[str, Any] | str) -> AgentRoutingDecision:
        payload: Mapping[str, Any] = {"Query": request} if isinstance(request, str) else request
        if _contains_auth_material(payload):
            return AgentRoutingDecision(
                task_type="SecurityBoundary",
                status="SECURITY_BOUNDARY_REJECTED",
                research_acquisition_intent_detected=False,
                harness_selected=False,
                harness_capability_available=False,
                missing_capability="Authentication material is not accepted by the Agent entry point",
                missing_request_details="unknown",
            )

        original = str(
            _field(
                payload,
                "OriginalResearchRequest",
                "Query",
                "ResearchQuestion",
                "Text",
                default="",
            )
        ).strip()
        explicit_task = _canonical(_field(payload, "TaskType", "task_type", default="auto"))
        task_type = self._classify_task(payload, original, explicit_task)

        if task_type == "LiteratureAcquisition":
            return self._route_literature(payload, original)
        if task_type == "OfficialWebAcquisition":
            return self._route_official_web(payload, original)
        if task_type == "DataAcquisition":
            return self._route_data(payload, original)
        if task_type == "Unsupported":
            return AgentRoutingDecision(
                task_type="Unsupported",
                status="UNSUPPORTED_CAPABILITY",
                original_request=original,
                research_acquisition_intent_detected=True,
                harness_selected=True,
                harness_capability_available=False,
                missing_capability="Requested TaskType is not implemented by HUNNU Research Harness",
            )
        return AgentRoutingDecision(
            task_type="NonAcquisition",
            status="NON_ACQUISITION_TASK",
            original_request=original,
            missing_capability="No research-acquisition intent detected",
        )

    @staticmethod
    def _classify_task(payload: Mapping[str, Any], original: str, explicit_task: str) -> str:
        literature_types = {"literature", "literaturesearch", "literatureacquisition", "papersearch"}
        official_web_types = {
            "officialweb",
            "officialwebacquisition",
            "publicofficialweb",
            "officialwebsite",
        }
        data_types = {"data", "dataacquisition", "researchdata", "researchdataacquisition"}
        if explicit_task in literature_types:
            return "LiteratureAcquisition"
        if explicit_task in official_web_types:
            return "OfficialWebAcquisition"
        if explicit_task in data_types:
            return "DataAcquisition"
        if explicit_task not in {"", "auto"}:
            return "Unsupported"

        source_values = _strings(_field(payload, "PreferredSources", "Sources", "Source", default=""))
        known_sources = {_source_name(value) for value in source_values}
        if known_sources & set(SUPPORTED_LITERATURE_SOURCES):
            return "LiteratureAcquisition"
        if known_sources & set(SUPPORTED_OFFICIAL_WEB_SOURCES):
            return "OfficialWebAcquisition"
        if "CNRDS" in known_sources:
            return "DataAcquisition"

        lowered = original.casefold()
        non_acquisition_markers = (
            "修改论文",
            "润色论文",
            "修改稿",
            "manuscript revision",
            "revise manuscript",
        )
        if any(marker in lowered for marker in non_acquisition_markers):
            return "NonAcquisition"
        literature_markers = (
            "论文",
            "文献",
            "知网",
            "cnki",
            "springer",
            "sciencedirect",
            "全文",
            "摘要",
            "筛选",
            "doi",
            "literature",
            "paper",
            "full text",
            "metadata",
            "screening",
        )
        data_markers = (
            "cnrds",
            "cnfs",
            "科研数据",
            "研究数据",
            "数据库数据",
            "数据获取",
            "获取数据",
            "research data",
            "database data",
        )
        if any(marker in lowered for marker in literature_markers):
            return "LiteratureAcquisition"
        if any(marker in lowered for marker in data_markers):
            return "DataAcquisition"
        return "NonAcquisition"

    @staticmethod
    def _route_official_web(
        payload: Mapping[str, Any], original: str
    ) -> AgentRoutingDecision:
        try:
            request = OfficialWebRequest.from_mapping(payload)
        except (TypeError, ValueError) as exc:
            return AgentRoutingDecision(
                task_type="OfficialWebAcquisition",
                status="INVALID_REQUEST",
                original_request=original,
                research_acquisition_intent_detected=True,
                harness_selected=True,
                harness_capability_available=True,
                correct_router_selected=True,
                missing_request_details=str(exc),
            )
        return AgentRoutingDecision(
            task_type="OfficialWebAcquisition",
            status="ROUTED",
            original_request=original,
            research_acquisition_intent_detected=True,
            harness_selected=True,
            harness_capability_available=True,
            request_validated=True,
            correct_router_selected=True,
            missing_capability="unknown",
            selected_sources=("OfficialWeb",),
            official_web_request=request,
            estimated_candidates=len(request.urls),
            estimated_high_cost_risk="Low",
            authorized_full_text_only=True,
            screening_first=True,
        )

    def _route_literature(self, payload: Mapping[str, Any], original: str) -> AgentRoutingDecision:
        if not original:
            return self._invalid_literature_request(original, "Query or OriginalResearchRequest is required")
        try:
            write_obsidian = _boolean(_field(payload, "WriteObsidian", default=False), default=False)
            if write_obsidian:
                return self._unsupported_literature_request(
                    original,
                    "Automatic Obsidian writing is not available through the Agent entry point",
                )
            authorized_only = _boolean(
                _field(payload, "AuthorizedFullTextOnly", default=True),
                default=True,
            )
            if not authorized_only:
                return self._unsupported_literature_request(
                    original,
                    "Only institutionally authorized full-text retrieval is supported",
                )
            reading_mode = str(_field(payload, "FullTextReadingMode", default="local")).strip().casefold()
            if reading_mode not in {"local", "localfile", "local_file"}:
                return self._unsupported_literature_request(
                    original,
                    "FullTextReadingMode must be LocalFile; browser full-text reading is not the default workflow",
                )
            screening_first = _boolean(_field(payload, "ScreeningFirst", default=True), default=True)
            if not screening_first:
                return self._invalid_literature_request(
                    original,
                    "ScreeningFirst=false is not supported by the bounded Agent workflow",
                )

            parsed = LiteratureSearchRequest.from_natural_language(original)
            max_candidates = _bounded_int(
                _field(payload, "MaxCandidates", "MaxSearchResults", default=parsed.max_search_results),
                default=DEFAULT_MAX_CANDIDATES,
                minimum=1,
                maximum=200,
                label="MaxCandidates",
            )
            supplied_downloads = _field(payload, "MaxDownloads", "MaxDownloadsPerRun", default=None)
            if supplied_downloads is None:
                max_downloads = parsed.max_downloads if _text_requests_download(original) else DEFAULT_MAX_DOWNLOADS
            else:
                max_downloads = _bounded_int(
                    supplied_downloads,
                    default=DEFAULT_MAX_DOWNLOADS,
                    minimum=0,
                    maximum=25,
                    label="MaxDownloads",
                )
            effective_downloads = min(max_downloads, max_candidates)
            total_download_value = _field(
                payload,
                "TotalDownloadBudget",
                "TotalRequestedDownloads",
                default=None,
            )
            total_candidate_value = _field(
                payload, "TotalCandidateBudget", default=max_candidates
            )
            multi_batch_plan = None
            if total_download_value is not None:
                total_download_budget = _bounded_int(
                    total_download_value,
                    default=effective_downloads,
                    minimum=0,
                    maximum=500,
                    label="TotalDownloadBudget",
                )
                total_candidate_budget = _bounded_int(
                    total_candidate_value,
                    default=max_candidates,
                    minimum=1,
                    maximum=2000,
                    label="TotalCandidateBudget",
                )
                per_batch_download_budget = _bounded_int(
                    _field(
                        payload,
                        "PerBatchDownloadBudget",
                        default=PER_BATCH_MAX_DOWNLOADS,
                    ),
                    default=PER_BATCH_MAX_DOWNLOADS,
                    minimum=1,
                    maximum=PER_BATCH_MAX_DOWNLOADS,
                    label="PerBatchDownloadBudget",
                )
                per_batch_candidate_budget = _bounded_int(
                    _field(payload, "PerBatchCandidateBudget", default=200),
                    default=200,
                    minimum=1,
                    maximum=200,
                    label="PerBatchCandidateBudget",
                )
                max_retries = _bounded_int(
                    _field(payload, "MaxRetries", default=1),
                    default=1,
                    minimum=0,
                    maximum=3,
                    label="MaxRetries",
                )
                raw_quotas = _field(payload, "QuotaGroups", default=None)
                if raw_quotas is not None and not isinstance(raw_quotas, Mapping):
                    raise ValueError("QuotaGroups must be an object mapping group names to quotas")
                multi_batch_plan = BoundedBatchPlanner().plan(
                    total_candidate_budget=total_candidate_budget,
                    total_download_budget=total_download_budget,
                    per_batch_candidate_budget=per_batch_candidate_budget,
                    per_batch_download_budget=per_batch_download_budget,
                    max_retries=max_retries,
                    quota_groups=raw_quotas,
                )
            languages = _normalize_languages(
                _field(payload, "Languages", "PreferredLanguages", default=None),
                original,
            )
            base_mapping = parsed.as_dict()
            base_mapping.update(
                {
                    "OriginalResearchRequest": original,
                    "ResearchQuestion": str(_field(payload, "ResearchQuestion", default=original)).strip(),
                    "PreferredLanguages": list(languages),
                    "PeerReviewedPreferred": _boolean(
                        _field(payload, "PeerReviewedPreferred", default=True), default=True
                    ),
                    "MaxSearchResults": max_candidates,
                    "MaxResultsPerSource": max_candidates,
                    "MaxDownloads": effective_downloads,
                    "MaxDownloadsPerRun": effective_downloads,
                    "RequireFullText": effective_downloads > 0,
                    "AI_ASSISTED": _boolean(_field(payload, "AI_ASSISTED", default=False), default=False),
                }
            )
            passthrough = {
                "KeywordsCN": ("KeywordsCN", "keywords_cn"),
                "KeywordsEN": ("KeywordsEN", "keywords_en"),
                "ExactTitles": ("ExactTitles", "exact_titles"),
                "Authors": ("Authors", "authors"),
                "DOIs": ("DOIs", "dois"),
                "YearStart": ("YearStart", "year_start"),
                "YearEnd": ("YearEnd", "year_end"),
                "PreferredPublicationTypes": ("PreferredPublicationTypes", "preferred_publication_types"),
                "JournalPriority": ("JournalPriority", "journal_priority"),
            }
            for target, aliases in passthrough.items():
                value = _field(payload, *aliases, default=None)
                if value is not None:
                    base_mapping[target] = value
            base_request = LiteratureSearchRequest.from_mapping(base_mapping)
        except (TypeError, ValueError) as exc:
            return self._invalid_literature_request(original, str(exc))

        source_selection = self._select_literature_sources(payload, original, base_request.preferred_languages)
        if isinstance(source_selection, str):
            return self._unsupported_literature_request(original, source_selection)
        candidate_budgets = _distribute_cap(max_candidates, source_selection)
        download_budgets = _distribute_cap(effective_downloads, source_selection)
        plans = tuple(
            LiteratureSourcePlan(
                source=source,
                request=replace(
                    base_request,
                    max_search_results=candidate_budgets[source],
                    max_results_per_source=candidate_budgets[source],
                    max_downloads=download_budgets.get(source, 0),
                    max_downloads_per_run=download_budgets.get(source, 0),
                    require_full_text=download_budgets.get(source, 0) > 0,
                ),
                max_candidates=candidate_budgets[source],
                max_downloads=download_budgets.get(source, 0),
            )
            for source in source_selection
            if candidate_budgets.get(source, 0) > 0
        )
        total_estimated_downloads = (
            multi_batch_plan.total_download_budget
            if multi_batch_plan is not None
            else max_downloads
        )
        total_estimated_candidates = (
            multi_batch_plan.total_candidate_budget
            if multi_batch_plan is not None
            else max_candidates
        )
        high_cost = (
            total_estimated_candidates > HIGH_COST_CANDIDATE_THRESHOLD
            or total_estimated_downloads > HIGH_COST_DOWNLOAD_THRESHOLD
        )
        unattended_requested = _boolean(
            _field(payload, "UnattendedExecutionRequested", default=None),
            default=any(
                marker in original.casefold()
                for marker in (
                    "无人值守",
                    "我要离开电脑",
                    "我等下要走",
                    "先把验证码处理完",
                    "先把这些数据库都准备好",
                    "unattended",
                    "leave the computer",
                    "preflight all sources",
                )
            ),
        )
        run_multi_source_preflight = unattended_requested and len(plans) > 1
        return AgentRoutingDecision(
            task_type="LiteratureAcquisition",
            status="PLANNING_AND_BUDGET_GATE" if high_cost else "ROUTED",
            original_request=original,
            research_acquisition_intent_detected=True,
            harness_selected=True,
            harness_capability_available=True,
            request_validated=True,
            correct_router_selected=True,
            missing_capability="unknown",
            selected_sources=tuple(plan.source for plan in plans),
            literature_plans=plans,
            multi_batch_plan=multi_batch_plan,
            planning_and_budget_gate=high_cost,
            estimated_candidates=total_estimated_candidates,
            estimated_downloads=total_estimated_downloads,
            estimated_local_files_to_read=total_estimated_downloads,
            estimated_high_cost_risk="High" if high_cost else "Low",
            full_text_reading_mode="LocalFile",
            authorized_full_text_only=True,
            screening_first=True,
            unattended_execution_requested=unattended_requested,
            run_multi_source_preflight=run_multi_source_preflight,
        )

    @staticmethod
    def _select_literature_sources(
        payload: Mapping[str, Any],
        original: str,
        languages: tuple[str, ...],
    ) -> tuple[str, ...] | str:
        requested = _strings(_field(payload, "PreferredSources", "Sources", "Source", default="auto"))
        explicit = tuple(item for item in requested if item.casefold() != "auto")
        if explicit:
            normalized = tuple(_source_name(item) for item in explicit)
            unknown = [item for item, source in zip(explicit, normalized) if source not in SUPPORTED_LITERATURE_SOURCES]
            if unknown:
                return f"Unsupported literature source: {', '.join(unknown)}"
            return tuple(dict.fromkeys(source for source in normalized if source is not None))

        lowered = original.casefold()
        hinted = tuple(
            source
            for source, markers in (
                ("CNKI", ("cnki", "知网", "中国知网")),
                ("SpringerLink", ("springer",)),
                ("ScienceDirect", ("sciencedirect", "elsevier")),
                ("OxfordAcademic", ("oxford academic", "oxford journals", "oup")),
            )
            if any(marker in lowered for marker in markers)
        )
        if hinted:
            return hinted
        sources: list[str] = []
        if "zh" in languages:
            sources.append("CNKI")
        if "en" in languages:
            sources.extend(("SpringerLink", "ScienceDirect", "OxfordAcademic"))
        return tuple(sources or SUPPORTED_LITERATURE_SOURCES)

    def _route_data(self, payload: Mapping[str, Any], original: str) -> AgentRoutingDecision:
        raw_database = _field(payload, "Database", "DataSource", default=None)
        if raw_database is None:
            sources = _strings(_field(payload, "PreferredSources", "Sources", "Source", default=""))
            raw_database = sources[0] if sources and sources[0].casefold() != "auto" else None
        if raw_database is None and any(marker in original.casefold() for marker in ("cnrds", "cnfs")):
            raw_database = "CNRDS"
        if raw_database is not None and _source_name(str(raw_database)) != "CNRDS":
            return AgentRoutingDecision(
                task_type="DataAcquisition",
                status="UNSUPPORTED_CAPABILITY",
                original_request=original,
                research_acquisition_intent_detected=True,
                harness_selected=True,
                harness_capability_available=False,
                correct_router_selected=True,
                missing_capability=f"Unsupported research-data source: {raw_database}",
            )
        module = str(_field(payload, "Module", default="")).strip()
        table = str(_field(payload, "Table", default="")).strip()
        if raw_database is None or not module or not table:
            return AgentRoutingDecision(
                task_type="DataAcquisition",
                status="NEEDS_STRUCTURED_DATA_REQUEST",
                original_request=original,
                research_acquisition_intent_detected=True,
                harness_selected=True,
                harness_capability_available=True,
                request_validated=False,
                correct_router_selected=True,
                missing_capability="unknown",
                missing_request_details="Database, Module, and Table are required before data acquisition",
                estimated_high_cost_risk="Unknown",
            )
        request = DownloadRequest(
            database="CNRDS",
            module=module,
            table=table,
            stocks=_strings(_field(payload, "Stocks", default=None)),
            date_start=_field(payload, "DateStart", default=None),
            date_end=_field(payload, "DateEnd", default=None),
            fields=_strings(_field(payload, "Fields", default=None)),
            output_format=str(_field(payload, "OutputFormat", default="csv")),
            source_url=str(_field(payload, "SourceURL", default="")),
        )
        return AgentRoutingDecision(
            task_type="DataAcquisition",
            status="ROUTED",
            original_request=original,
            research_acquisition_intent_detected=True,
            harness_selected=True,
            harness_capability_available=True,
            request_validated=True,
            correct_router_selected=True,
            missing_capability="unknown",
            selected_sources=("CNRDS",),
            data_request=request,
            estimated_downloads=1,
            estimated_high_cost_risk="Low",
        )

    @staticmethod
    def _invalid_literature_request(original: str, reason: str) -> AgentRoutingDecision:
        return AgentRoutingDecision(
            task_type="LiteratureAcquisition",
            status="INVALID_REQUEST",
            original_request=original,
            research_acquisition_intent_detected=True,
            harness_selected=True,
            harness_capability_available=True,
            correct_router_selected=True,
            missing_capability="unknown",
            missing_request_details=reason,
        )

    @staticmethod
    def _unsupported_literature_request(original: str, reason: str) -> AgentRoutingDecision:
        return AgentRoutingDecision(
            task_type="LiteratureAcquisition",
            status="UNSUPPORTED_CAPABILITY",
            original_request=original,
            research_acquisition_intent_detected=True,
            harness_selected=True,
            harness_capability_available=False,
            correct_router_selected=True,
            missing_capability=reason,
        )

    async def invoke_literature(
        self,
        plan: LiteratureSourcePlan,
        *,
        browser: BrowserCommandPort | BrowserTransport | None = None,
        adapter: "LiteratureSourceAdapter | None" = None,
        run_root: Path,
        institutional_resolver: "InstitutionalAccessResolver | None" = None,
        institutional_trigger: "InstitutionalResolutionTrigger | None" = None,
    ):
        """Execute one source plan through the registered source adapter.

        The preferred path supplies a ``BrowserCommandPort`` (or a v0.2.16
        local transport that Harness wraps once) and lets Harness instantiate
        the adapter selected by ``plan.source``.  The legacy ``adapter=`` path
        remains available only after strict registry, source-name, and command
        port identity validation.
        """

        return await self.adapter_execution_broker.execute(
            plan,
            browser=browser,
            adapter=adapter,
            run_root=run_root,
            workflow_factory=LiteratureAcquisitionWorkflow,
            institutional_resolver=institutional_resolver,
            institutional_trigger=institutional_trigger,
        )

    async def invoke_official_web(
        self,
        decision: AgentRoutingDecision,
        *,
        browser: BrowserCommandPort | BrowserTransport,
    ):
        """Execute a routed public-official-web request through its registry."""

        if (
            decision.task_type != "OfficialWebAcquisition"
            or not decision.is_routable
            or decision.official_web_request is None
        ):
            raise ValueError("A validated, routable OfficialWeb decision is required")
        return await self.official_web_execution_broker.execute(
            decision.official_web_request,
            browser=browser,
        )

    @staticmethod
    def describe_literature_result(result: LiteratureRunResult) -> dict[str, Any]:
        manual = result.status == RunStatus.ACTION_REQUIRED_USER_DOWNLOAD
        login = result.status == RunStatus.ACTION_REQUIRED_USER_LOGIN
        unattended = bool(
            result.status == RunStatus.SUCCESS
            and any(
                entry.oxford_unattended_download_ready
                and entry.automatic_download_initiation
                and entry.automatic_download_detection
                and not entry.manual_download_handoff_used
                and not entry.human_download_action
                for entry in result.downloads
            )
        )
        return sanitize_value(
            {
                "Status": result.status.value,
                "ACTION_REQUIRED_USER_LOGIN": login,
                "ACTION_REQUIRED_USER_DOWNLOAD": manual,
                "BrowserReadyForManualDownload": manual,
                "TryUnattendedDownload": True,
                "ManualDownloadHandoffIsFallback": True,
                "OxfordUnattendedDownloadReady": unattended,
                "UserNativeViewerClickRequired": manual,
                "Instruction": (
                    "Official PDF is open; click the Chrome PDF Viewer native Download button once. "
                    "Harness is waiting for the lawful local file and will finalize it automatically."
                    if manual
                    else "not_applicable"
                ),
                "Reason": result.action_required_reason,
                "WindowsGUIFallback": False,
                "NativePDFViewerAutomation": False,
                "SignedURLReplay": False,
            }
        )

    async def invoke_data(
        self,
        decision: AgentRoutingDecision,
        *,
        adapter: Any,
        manager: "DownloadManager",
    ):
        """Delegate an approved CNRDS request to the existing v0.1 workflow."""

        if decision.data_request is None or not decision.is_routable:
            raise ValueError("A validated, routable CNRDS AgentRoutingDecision is required")
        return await run_cnrds_download(adapter, manager, decision.data_request)


def write_dry_run_result(
    decision: AgentRoutingDecision,
    *,
    run_root: Path = V0217_RUN_ROOT,
) -> Path:
    """Persist a sanitized, no-network routing proof under the Output Root."""

    root = require_output_path(Path(run_root), label="Agent dry-run artifacts")
    path = root / "results" / "AGENT_REQUEST_DRY_RUN.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "GeneratedAt": datetime.now(timezone.utc).isoformat(),
        "DryRun": True,
        **decision.as_dict(),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(sanitize_value(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)
    return path


def add_agent_route_arguments(parser: argparse.ArgumentParser) -> None:
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--request-json", type=Path, help="Structured Agent request JSON")
    selector.add_argument("--text", help="Natural-language research request")
    parser.add_argument("--dry-run", action="store_true", help="Write a sanitized no-network routing proof")
    parser.add_argument("--run-root", type=Path, default=V0217_RUN_ROOT)


def route_from_cli_args(args: argparse.Namespace) -> int:
    if getattr(args, "request_json", None):
        payload: Mapping[str, Any] = json.loads(args.request_json.read_text(encoding="utf-8-sig"))
    else:
        payload = {"Query": args.text}
    decision = AgentRequestRouter().route(payload)
    result = decision.as_dict()
    if args.dry_run:
        result["DryRunArtifact"] = str(write_dry_run_result(decision, run_root=args.run_root))
    result["DryRun"] = bool(args.dry_run)
    print(json.dumps(sanitize_value(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if decision.is_routable else 2


def main() -> int:
    parser = argparse.ArgumentParser(prog="hunnu-harness-agent")
    add_agent_route_arguments(parser)
    return route_from_cli_args(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
