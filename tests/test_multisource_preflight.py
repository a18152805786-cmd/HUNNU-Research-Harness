from __future__ import annotations

import asyncio
import json
import math
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hunnu_harness.agent_entrypoint import (
    AgentRequestRouter,
    LITERATURE_ADAPTER_REGISTRY,
    SOURCE_CAPABILITY_REGISTRY,
)
from hunnu_harness.literature.adapters.base import SourceActionRequired
from hunnu_harness.literature.adapters.oxfordacademic import OxfordAcademicAdapter
from hunnu_harness.literature.models import (
    AccessType,
    DownloadManifestEntry,
    LiteratureRunResult,
    LiteratureRecord,
    LiteratureSearchRequest,
    RunStatus,
)
from hunnu_harness.literature.preflight import (
    AuthenticationSweepStatus,
    LiteratureAdapterPreflightHandler,
    MultiSourcePreflightCoordinator,
    PreflightPhase,
    PreflightStatus,
    RequiredActionType,
    SourceAuthenticationSweepResult,
    SourceCapabilityRegistry,
    SourcePreflightCapabilities,
    SourcePreflightResult,
    _result_from_literature_run,
)
from hunnu_harness.literature.security import sanitize_value, scan_text_for_sensitive_leaks
from hunnu_harness.paths import CORE_ROOT, OUTPUT_ROOT, TEMP_DIR, V028_RUN_ROOT, is_within
from hunnu_harness.literature.workflow import _lock_search_result_to_detail


def _capability(source: str, *, ready: bool = True) -> SourcePreflightCapabilities:
    return SourcePreflightCapabilities(
        source=source,
        supports_search=True,
        supports_fulltext_access_check=True,
        supports_authorized_download=True,
        supports_unattended_download=ready,
        supports_preflight=ready,
    )


def _auth_ready(source: str) -> SourceAuthenticationSweepResult:
    return SourceAuthenticationSweepResult(
        source=source,
        status=AuthenticationSweepStatus.READY_TO_CONTINUE,
        source_reachable=True,
        institutional_route_ready=True,
        authentication_ready=True,
        page_identity_confirmed=True,
    )


def _auth_action(source: str, action: RequiredActionType = RequiredActionType.CAPTCHA):
    return SourceAuthenticationSweepResult(
        source=source,
        status=AuthenticationSweepStatus.ACTION_REQUIRED_USER,
        source_reachable=True,
        user_action_currently_required=True,
        required_action_type=action,
        browser_ready_for_manual_action=True,
        reason=f"{action.value} requires user action",
    )


def _auth_not_ready(source: str) -> SourceAuthenticationSweepResult:
    return SourceAuthenticationSweepResult(
        source=source,
        status=AuthenticationSweepStatus.NOT_READY,
        source_reachable=False,
        reason="SOURCE_UNAVAILABLE",
    )


def _ready(source: str, *, candidate: bool = True) -> SourcePreflightResult:
    return SourcePreflightResult.ready(
        source,
        paper_id=f"P-{source}",
        title=f"Research candidate from {source}",
        doi="unknown",
        local_path=f"C:/output/{source}.pdf",
        sha256="a" * 64,
        research_candidate=candidate,
    )


def _download_action(source: str) -> SourcePreflightResult:
    return SourcePreflightResult(
        source=source,
        status=PreflightStatus.ACTION_REQUIRED_USER,
        user_action_currently_required=True,
        required_action_type=RequiredActionType.SECURITY_CHALLENGE,
        browser_ready_for_manual_action=True,
        reason="security challenge",
    )


class _Handler:
    def __init__(self, source: str, auth_results=None, download_results=None):
        self.source = source
        self.auth_results = list(auth_results or [_auth_ready(source)])
        self.download_results = list(download_results or [_ready(source)])
        self.auth_calls = 0
        self.download_calls = 0

    async def authentication_sweep(self, _context):
        index = min(self.auth_calls, len(self.auth_results) - 1)
        self.auth_calls += 1
        result = self.auth_results[index]
        if isinstance(result, Exception):
            raise result
        return result

    async def download_preflight(self, _context):
        index = min(self.download_calls, len(self.download_results) - 1)
        self.download_calls += 1
        result = self.download_results[index]
        if isinstance(result, Exception):
            raise result
        return result


class MultiSourcePreflightTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def _run_root(self, name: str) -> Path:
        return TEMP_DIR / f"v028-{name}"

    def _coordinator(self, names, handlers, name="test", *, threshold=10):
        registry = SourceCapabilityRegistry()
        for source in names:
            registry.register(_capability(source))
        return MultiSourcePreflightCoordinator(
            capability_registry=registry,
            handlers=handlers,
            run_root=self._run_root(name),
            high_cost_source_threshold=threshold,
        )

    def test_dynamic_source_counts_one_two_four_and_seven(self) -> None:
        for count in (1, 2, 4, 7):
            sources = tuple(f"Source{index}" for index in range(count))
            handlers = {source: _Handler(source) for source in sources}
            coordinator = self._coordinator(sources, handlers, f"dynamic-{count}")
            result = asyncio.run(
                coordinator.run_initial(
                    research_request_id=f"R-{count}",
                    planned_sources=sources,
                    formal_download_cap=count,
                )
            )
            self.assertEqual(len(result.ready_sources), count)
            self.assertTrue(result.unattended_run_clearance)
            self.assertTrue(all(handler.download_calls == 1 for handler in handlers.values()))

    def test_current_session_download_evidence_boolean_is_not_redacted(self) -> None:
        payload = sanitize_value({"CurrentSessionDownloadVerified": True})
        self.assertIs(payload["CurrentSessionDownloadVerified"], True)

    def test_only_planned_sources_are_preflighted(self) -> None:
        installed = ("A", "B", "C", "D", "E")
        handlers = {source: _Handler(source) for source in installed}
        coordinator = self._coordinator(installed, handlers, "planned-only")
        result = asyncio.run(
            coordinator.run_initial(
                research_request_id="R-planned",
                planned_sources=("B", "D"),
                formal_download_cap=2,
            )
        )
        self.assertEqual(result.planned_sources, ("B", "D"))
        self.assertEqual(handlers["B"].download_calls, 1)
        self.assertEqual(handlers["D"].download_calls, 1)
        self.assertEqual(sum(handlers[item].auth_calls for item in ("A", "C", "E")), 0)

    def test_duplicate_planned_sources_are_deduplicated_in_order(self) -> None:
        sources = ("A", "B")
        handlers = {source: _Handler(source) for source in sources}
        result = asyncio.run(
            self._coordinator(sources, handlers, "dedupe").run_initial(
                research_request_id="R-dedupe",
                planned_sources=("A", "B", "A"),
                formal_download_cap=2,
            )
        )
        self.assertEqual(result.planned_sources, ("A", "B"))
        self.assertEqual(handlers["A"].download_calls, 1)

    def test_phase_a_batches_all_user_actions_without_early_exit(self) -> None:
        sources = ("A", "B", "C", "D")
        handlers = {
            "A": _Handler("A"),
            "B": _Handler("B", auth_results=[_auth_action("B")]),
            "C": _Handler("C", auth_results=[_auth_action("C", RequiredActionType.SSO)]),
            "D": _Handler("D"),
        }
        result = asyncio.run(
            self._coordinator(sources, handlers, "auth-batch").run_initial(
                research_request_id="R-auth",
                planned_sources=sources,
                formal_download_cap=4,
            )
        )
        self.assertEqual({item.source for item in result.sources_requiring_user_action}, {"B", "C"})
        self.assertEqual(sum(handler.auth_calls for handler in handlers.values()), 4)
        self.assertEqual(sum(handler.download_calls for handler in handlers.values()), 0)
        self.assertFalse(result.unattended_run_clearance)

    def test_resume_rechecks_only_auth_sources_then_downloads_all(self) -> None:
        sources = ("A", "B", "C", "D")
        handlers = {
            "A": _Handler("A"),
            "B": _Handler("B", auth_results=[_auth_action("B"), _auth_ready("B")]),
            "C": _Handler("C", auth_results=[_auth_action("C"), _auth_ready("C")]),
            "D": _Handler("D"),
        }
        coordinator = self._coordinator(sources, handlers, "resume-auth")
        asyncio.run(
            coordinator.run_initial(
                research_request_id="R-resume",
                planned_sources=sources,
                formal_download_cap=4,
            )
        )
        resumed = asyncio.run(coordinator.resume_batch())
        self.assertEqual(handlers["A"].auth_calls, 1)
        self.assertEqual(handlers["D"].auth_calls, 1)
        self.assertEqual(handlers["B"].auth_calls, 2)
        self.assertEqual(handlers["C"].auth_calls, 2)
        self.assertTrue(resumed.unattended_run_clearance)
        self.assertTrue(all(handler.download_calls == 1 for handler in handlers.values()))
        self.assertEqual(resumed.as_dict()["SourcesInitiallyRequiringUserAction"], 2)

    def test_download_phase_batches_and_resumes_only_action_sources(self) -> None:
        sources = ("A", "B", "C", "D")
        handlers = {
            "A": _Handler("A"),
            "B": _Handler("B", download_results=[_download_action("B"), _ready("B")]),
            "C": _Handler("C", download_results=[_download_action("C"), _ready("C")]),
            "D": _Handler("D"),
        }
        coordinator = self._coordinator(sources, handlers, "resume-download")
        first = asyncio.run(
            coordinator.run_initial(
                research_request_id="R-download",
                planned_sources=sources,
                formal_download_cap=4,
            )
        )
        self.assertEqual(len(first.sources_requiring_user_action), 2)
        resumed = asyncio.run(coordinator.resume_batch())
        self.assertTrue(resumed.unattended_run_clearance)
        self.assertEqual(handlers["A"].download_calls, 1)
        self.assertEqual(handlers["D"].download_calls, 1)
        self.assertEqual(handlers["B"].download_calls, 2)
        self.assertEqual(handlers["C"].download_calls, 2)

    def test_not_ready_blocks_clearance(self) -> None:
        handlers = {"A": _Handler("A"), "B": _Handler("B", auth_results=[_auth_not_ready("B")])}
        result = asyncio.run(
            self._coordinator(("A", "B"), handlers, "not-ready").run_initial(
                research_request_id="R-notready",
                planned_sources=("A", "B"),
                formal_download_cap=2,
            )
        )
        self.assertEqual(len(result.not_ready_sources), 1)
        self.assertFalse(result.unattended_run_clearance)
        self.assertFalse(result.formal_research_run_allowed)

    def test_partial_run_requires_explicit_opt_in(self) -> None:
        handlers = {
            "A": _Handler("A"),
            "B": _Handler("B", download_results=[SourcePreflightResult(
                source="B", status=PreflightStatus.NOT_READY, reason="ACCESS_DENIED"
            )]),
        }
        coordinator = self._coordinator(("A", "B"), handlers, "partial")
        result = asyncio.run(
            coordinator.run_initial(
                research_request_id="R-partial",
                planned_sources=("A", "B"),
                formal_download_cap=2,
                proceed_with_partial_sources=True,
            )
        )
        self.assertFalse(result.unattended_run_clearance)
        self.assertTrue(result.formal_research_run_allowed)
        self.assertFalse(result.formal_research_run_started)

    def test_download_cap_rejects_more_than_one_attempt(self) -> None:
        with self.assertRaisesRegex(ValueError, "PreflightMaxDownloadsPerSource=1"):
            SourcePreflightResult(
                source="A",
                status=PreflightStatus.NOT_READY,
                downloads_attempted=2,
            )

    def test_research_candidates_are_capped_and_excess_becomes_overhead(self) -> None:
        sources = ("A", "B", "C", "D")
        handlers = {source: _Handler(source) for source in sources}
        result = asyncio.run(
            self._coordinator(sources, handlers, "candidate-cap").run_initial(
                research_request_id="R-cap",
                planned_sources=sources,
                formal_download_cap=2,
            )
        )
        self.assertEqual(
            sum(item.preflight_paper_is_research_candidate for item in result.ready_sources), 2
        )
        self.assertEqual(
            sum(item.preflight_paper_purpose == "SOURCE_READINESS_CHECK" for item in result.ready_sources),
            2,
        )

    def test_unsupported_future_source_returns_not_ready(self) -> None:
        registry = SourceCapabilityRegistry()
        registry.register(_capability("Known"))
        coordinator = MultiSourcePreflightCoordinator(
            capability_registry=registry,
            handlers={"Known": _Handler("Known")},
            run_root=self._run_root("unsupported"),
        )
        result = asyncio.run(
            coordinator.run_initial(
                research_request_id="R-future",
                planned_sources=("FutureSource",),
                formal_download_cap=1,
            )
        )
        self.assertEqual(result.not_ready_sources[0].reason, "ADAPTER_MISSING")
        self.assertFalse(result.unattended_run_clearance)

    def test_declared_source_without_unattended_capability_is_not_ready(self) -> None:
        registry = SourceCapabilityRegistry()
        registry.register(_capability("MetadataOnly", ready=False))
        result = asyncio.run(
            MultiSourcePreflightCoordinator(
                capability_registry=registry,
                handlers={"MetadataOnly": _Handler("MetadataOnly")},
                run_root=self._run_root("metadata-only"),
            ).run_initial(
                research_request_id="R-meta",
                planned_sources=("MetadataOnly",),
                formal_download_cap=1,
            )
        )
        self.assertEqual(result.not_ready_sources[0].reason, "UNATTENDED_PREFLIGHT_UNSUPPORTED")

    def test_large_planned_source_count_enters_budget_gate_without_browser_calls(self) -> None:
        sources = tuple(f"S{index}" for index in range(11))
        handlers = {source: _Handler(source) for source in sources}
        result = asyncio.run(
            self._coordinator(sources, handlers, "cost-gate").run_initial(
                research_request_id="R-cost",
                planned_sources=sources,
                formal_download_cap=11,
            )
        )
        self.assertTrue(result.planning_and_budget_gate)
        self.assertEqual(sum(handler.auth_calls for handler in handlers.values()), 0)

    def test_source_action_exception_is_batched_as_manual_gate(self) -> None:
        handlers = {
            "A": _Handler("A", auth_results=[SourceActionRequired("CAPTCHA visible")]),
            "B": _Handler("B"),
        }
        result = asyncio.run(
            self._coordinator(("A", "B"), handlers, "exception-gate").run_initial(
                research_request_id="R-exception",
                planned_sources=("A", "B"),
                formal_download_cap=2,
            )
        )
        action = result.sources_requiring_user_action[0]
        self.assertEqual(action.required_action_type, RequiredActionType.CAPTCHA)
        self.assertTrue(action.browser_ready_for_manual_action)


class PreflightEvidenceAndArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _entry(path: Path, **overrides) -> DownloadManifestEntry:
        values = dict(
            paper_id="P1",
            source="OxfordAcademic",
            title="A paper",
            doi="10.0000/test",
            access_type=AccessType.INSTITUTIONAL_AUTHENTICATED.value,
            authorized_access=True,
            original_url_or_stable_identifier="stable-id",
            original_filename=path.name,
            normalized_filename=path.name,
            download_timestamp="2026-08-15T00:00:00+00:00",
            file_size_bytes=path.stat().st_size,
            sha256="b" * 64,
            local_path=str(path),
            pdf_validation_passed=True,
            file_validation_passed=True,
            target_identity_confirmed=True,
            acquisition_method="PLAYWRIGHT_DOWNLOAD_EVENT",
        )
        values.update(overrides)
        return DownloadManifestEntry(**values)

    def test_old_local_pdf_cannot_prove_current_session_download(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v028-old-", dir=TEMP_DIR) as temporary:
            path = Path(temporary) / "old.pdf"
            path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            result = _result_from_literature_run(
                "OxfordAcademic",
                LiteratureRunResult(
                    status=RunStatus.SUCCESS,
                    records=[],
                    downloads=[self._entry(path)],
                ),
                started_at_ns=path.stat().st_mtime_ns + 1_000_000_000,
                research_candidate=False,
            )
            self.assertEqual(result.status, PreflightStatus.NOT_READY)
            self.assertFalse(result.current_session_download_verified)

    def test_manual_download_handoff_cannot_be_unattended_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v028-manual-", dir=TEMP_DIR) as temporary:
            path = Path(temporary) / "manual.pdf"
            path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            result = _result_from_literature_run(
                "OxfordAcademic",
                LiteratureRunResult(
                    status=RunStatus.SUCCESS,
                    records=[],
                    downloads=[self._entry(path, manual_download_handoff_used=True, human_download_action=True)],
                ),
                started_at_ns=0,
                research_candidate=False,
            )
            self.assertEqual(result.status, PreflightStatus.NOT_READY)

    def test_current_download_with_complete_evidence_is_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v028-new-", dir=TEMP_DIR) as temporary:
            path = Path(temporary) / "new.pdf"
            path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            result = _result_from_literature_run(
                "OxfordAcademic",
                LiteratureRunResult(
                    status=RunStatus.SUCCESS,
                    records=[],
                    downloads=[self._entry(path)],
                ),
                started_at_ns=0,
                research_candidate=True,
            )
            self.assertEqual(result.status, PreflightStatus.READY_FOR_UNATTENDED)
            self.assertTrue(result.current_session_download_verified)

    def test_artifacts_are_under_output_root_and_sanitized(self) -> None:
        run_root = TEMP_DIR / "v028-artifact"
        handlers = {
            "A": _Handler(
                "A",
                auth_results=[SourceActionRequired("login required; token=redacted_test_value")],
            )
        }
        coordinator = MultiSourcePreflightCoordinator(
            capability_registry=self._registry("A"),
            handlers=handlers,
            run_root=run_root,
        )
        result = asyncio.run(
            coordinator.run_initial(
                research_request_id="R-artifact",
                planned_sources=("A",),
                formal_download_cap=1,
            )
        )
        manifest = run_root / "manifests" / "MULTISOURCE_PREFLIGHT_MANIFEST.json"
        actions = run_root / "manifests" / "BATCH_USER_ACTION_REQUIRED.json"
        self.assertTrue(manifest.exists())
        self.assertTrue(actions.exists())
        self.assertTrue(is_within(manifest, OUTPUT_ROOT))
        self.assertFalse(is_within(manifest, CORE_ROOT))
        self.assertFalse(scan_text_for_sensitive_leaks(manifest.read_text(encoding="utf-8")))
        self.assertFalse(scan_text_for_sensitive_leaks(actions.read_text(encoding="utf-8")))
        self.assertFalse(result.unattended_run_clearance)

    def test_resolved_batch_manifest_preserves_sanitized_action_history(self) -> None:
        run_root = TEMP_DIR / "v028-resolved-action"
        coordinator = MultiSourcePreflightCoordinator(
            capability_registry=self._registry("A"),
            handlers={"A": _Handler("A", download_results=[_download_action("A"), _ready("A")])},
            run_root=run_root,
        )
        asyncio.run(
            coordinator.run_initial(
                research_request_id="R-resolved-action",
                planned_sources=("A",),
                formal_download_cap=1,
            )
        )
        result = asyncio.run(coordinator.resume_batch())
        payload = json.loads(
            (run_root / "manifests" / "BATCH_USER_ACTION_REQUIRED.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(result.unattended_run_clearance)
        self.assertFalse(payload["BatchUserActionRequired"])
        self.assertTrue(payload["Resolved"])
        self.assertEqual(payload["RequiredUserActions"], [])
        self.assertEqual(len(payload["UserActionHistory"]), 1)

    @staticmethod
    def _registry(*sources):
        registry = SourceCapabilityRegistry()
        for source in sources:
            registry.register(_capability(source))
        return registry

    def test_manifest_keeps_clearance_independent_from_route_readiness(self) -> None:
        run_root = TEMP_DIR / "v028-independence"
        coordinator = MultiSourcePreflightCoordinator(
            capability_registry=self._registry("A"),
            handlers={"A": _Handler("A", download_results=[_download_action("A")])},
            run_root=run_root,
        )
        result = asyncio.run(
            coordinator.run_initial(
                research_request_id="R-independent",
                planned_sources=("A",),
                formal_download_cap=1,
            )
        )
        payload = json.loads(
            (run_root / "manifests" / "MULTISOURCE_PREFLIGHT_MANIFEST.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(payload["AuthenticationSweepCompleted"])
        self.assertFalse(payload["UnattendedRunClearance"])
        self.assertEqual(result.source_results[0].phase, PreflightPhase.DOWNLOAD_PREFLIGHT)

    def test_v028_root_is_output_only(self) -> None:
        self.assertTrue(is_within(V028_RUN_ROOT, OUTPUT_ROOT))
        self.assertFalse(is_within(V028_RUN_ROOT, CORE_ROOT))

    def test_search_detail_identity_lock_prefers_exact_doi(self) -> None:
        wrong = LiteratureRecord(paper_id="wrong", title="Same title", doi="10.1000/wrong")
        right = LiteratureRecord(paper_id="right", title="Publisher title", doi="10.1000/right")
        detail = LiteratureRecord(paper_id="detail", title="Publisher title", doi="https://doi.org/10.1000/right")
        self.assertIs(_lock_search_result_to_detail((wrong, right), detail), right)

    def test_search_detail_identity_lock_rejects_mismatch(self) -> None:
        search = LiteratureRecord(paper_id="search", title="One paper", doi="10.1000/one")
        detail = LiteratureRecord(paper_id="detail", title="Another paper", doi="10.1000/two")
        self.assertIsNone(_lock_search_result_to_detail((search,), detail))


class RegistryAndAgentIntegrationTests(unittest.TestCase):
    def test_existing_adapter_registry_drives_preflight_capabilities(self) -> None:
        self.assertEqual(set(SOURCE_CAPABILITY_REGISTRY.sources), set(LITERATURE_ADAPTER_REGISTRY))
        self.assertTrue(
            all(
                SOURCE_CAPABILITY_REGISTRY.get(source).supports_unattended_preflight
                for source in LITERATURE_ADAPTER_REGISTRY
            )
        )

    def test_coordinator_source_contains_no_four_site_branching(self) -> None:
        source = Path(
            MultiSourcePreflightCoordinator.__init__.__code__.co_filename
        ).read_text(encoding="utf-8")
        coordinator_section = source.split("class MultiSourcePreflightCoordinator", 1)[1]
        for site in ("CNKI", "SpringerLink", "ScienceDirect", "OxfordAcademic"):
            self.assertNotIn(site, coordinator_section)

    def test_unattended_multi_source_request_selects_preflight(self) -> None:
        decision = AgentRequestRouter().route(
            {
                "TaskType": "literature_search",
                "Query": "AI washing audit monitoring，我要离开电脑，无人值守运行",
                "PreferredSources": ["CNKI", "SpringerLink", "ScienceDirect", "OxfordAcademic"],
                "MaxCandidates": 4,
                "MaxDownloads": 4,
            }
        )
        self.assertTrue(decision.unattended_execution_requested)
        self.assertTrue(decision.run_multi_source_preflight)
        self.assertTrue(decision.as_dict()["RunMultiSourcePreflight"])

    def test_normal_multi_source_request_does_not_force_preflight(self) -> None:
        decision = AgentRequestRouter().route(
            {
                "TaskType": "literature_search",
                "Query": "find literature",
                "PreferredSources": ["CNKI", "SpringerLink"],
                "MaxCandidates": 2,
                "MaxDownloads": 0,
            }
        )
        self.assertFalse(decision.unattended_execution_requested)
        self.assertFalse(decision.run_multi_source_preflight)

    def test_single_source_unattended_request_uses_normal_source_flow(self) -> None:
        decision = AgentRequestRouter().route(
            {
                "TaskType": "literature_search",
                "Query": "无人值守下载",
                "PreferredSources": ["CNKI"],
                "MaxCandidates": 1,
                "MaxDownloads": 1,
            }
        )
        self.assertTrue(decision.unattended_execution_requested)
        self.assertFalse(decision.run_multi_source_preflight)

    def test_policy_payload_preserves_login_and_forbids_state_export(self) -> None:
        payload = AgentRequestRouter().route(
            {
                "TaskType": "literature_search",
                "Query": "无人值守找论文",
                "PreferredSources": ["CNKI", "SpringerLink"],
                "MaxCandidates": 2,
                "MaxDownloads": 2,
            }
        ).as_dict()
        self.assertTrue(payload["ExistingLoginStatePreserved"])
        self.assertTrue(payload["ManualAuthentication"])
        self.assertFalse(payload["CookiesExported"])
        self.assertFalse(payload["BrowserStateExported"])
        self.assertFalse(payload["AuthenticationBypass"])

    def test_adapter_handler_delegates_to_existing_workflow_with_one_download(self) -> None:
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "test paper",
                "ResearchQuestion": "test paper",
                "MaxSearchResults": 5,
                "MaxResultsPerSource": 5,
                "MaxDownloads": 5,
                "MaxDownloadsPerRun": 5,
            }
        )

        async def auth(context):
            return _auth_ready(context.source)

        with tempfile.TemporaryDirectory(prefix="v028-handler-", dir=TEMP_DIR) as temporary:
            output = Path(temporary) / "new.pdf"

            async def run(_request):
                output.write_bytes(b"%PDF-1.4\n%%EOF\n")
                entry = PreflightEvidenceAndArtifactTests._entry(output)
                return LiteratureRunResult(status=RunStatus.SUCCESS, records=[], downloads=[entry])

            handler = LiteratureAdapterPreflightHandler(
                adapter=OxfordAcademicAdapter(object()),
                request=request,
                authentication_probe=auth,
            )
            context_run = Path(temporary) / "run"
            with patch(
                "hunnu_harness.literature.preflight.LiteratureAcquisitionWorkflow"
            ) as workflow_type:
                workflow_type.return_value.run = AsyncMock(side_effect=run)
                context = type(
                    "Context",
                    (),
                    {
                        "source": "OxfordAcademic",
                        "run_root": context_run,
                    },
                )()
                result = asyncio.run(handler.download_preflight(context))
                delegated_request = workflow_type.return_value.run.await_args.args[0]
            self.assertEqual(delegated_request.max_search_results, 1)
            self.assertEqual(delegated_request.max_downloads, 1)
            self.assertEqual(result.status, PreflightStatus.READY_FOR_UNATTENDED)


class PreflightSessionBoundaryClockTests(unittest.TestCase):
    """Pins the arithmetic of the current-session download boundary (AGENTS.md 48).

    `st_mtime_ns` is an exact integer, so the boundary it is compared against must
    be one too. A boundary derived from `datetime.now().timestamp() * 1_000_000_000`
    is a float64 in seconds scaled to nanoseconds: it needs ~19 significant digits
    where float64 carries ~15-16, so it can round ahead of the true instant and
    reject a file written immediately afterwards.
    """

    @staticmethod
    def _success_run(path: Path) -> LiteratureRunResult:
        entry = PreflightEvidenceAndArtifactTests._entry(path)
        return LiteratureRunResult(status=RunStatus.SUCCESS, records=[], downloads=[entry])

    @staticmethod
    def _handler() -> LiteratureAdapterPreflightHandler:
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "boundary probe",
                "ResearchQuestion": "boundary probe",
                "MaxSearchResults": 5,
                "MaxResultsPerSource": 5,
                "MaxDownloads": 5,
                "MaxDownloadsPerRun": 5,
            }
        )

        async def auth(context):
            return _auth_ready(context.source)

        return LiteratureAdapterPreflightHandler(
            adapter=OxfordAcademicAdapter(object()),
            request=request,
            authentication_probe=auth,
        )

    def _run_preflight(self, temporary: str, name: str) -> SourcePreflightResult:
        output = Path(temporary) / name

        async def run(_request):
            output.write_bytes(b"%PDF-1.4\n%%EOF\n")
            return self._success_run(output)

        with patch(
            "hunnu_harness.literature.preflight.LiteratureAcquisitionWorkflow"
        ) as workflow_type:
            workflow_type.return_value.run = AsyncMock(side_effect=run)
            context = type(
                "Context",
                (),
                {"source": "OxfordAcademic", "run_root": Path(temporary) / "run"},
            )()
            return asyncio.run(self._handler().download_preflight(context))

    def test_float_seconds_clock_cannot_represent_epoch_nanoseconds(self) -> None:
        # Why an integer clock is required: at the current epoch magnitude one
        # float64 step is far wider than the nanosecond the comparison resolves.
        now_ns = time.time_ns()
        self.assertGreater(math.ulp(float(now_ns)), 1.0)
        float_boundary = datetime.now(timezone.utc).timestamp() * 1_000_000_000
        self.assertGreater(math.ulp(float_boundary), 1.0)
        # A float boundary is blind to the nanosecond the mtime comparison resolves:
        # adding one nanosecond to it rounds straight back to the same value.
        self.assertEqual(float_boundary + 1.0, float_boundary)

    def test_session_boundary_comes_from_exact_integer_nanosecond_clock(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="v028-clock-", dir=TEMP_DIR) as temporary:
            # A boundary in the future must reject the download; a boundary in the
            # past must accept it. Both only hold if time.time_ns() is the source.
            # create=True so a module that never consults time.time_ns() fails on the
            # behavioural assertion below rather than on the patch target.
            future = SimpleNamespace(time_ns=lambda: time.time_ns() + 10_000_000_000)
            with patch("hunnu_harness.literature.preflight.time", future, create=True):
                result = self._run_preflight(temporary, "future.pdf")
            self.assertEqual(
                result.status,
                PreflightStatus.NOT_READY,
                "boundary ignored time.time_ns(); it is not the exact integer clock",
            )
            self.assertFalse(result.current_session_download_verified)

            past = SimpleNamespace(time_ns=lambda: time.time_ns() - 10_000_000_000)
            with patch("hunnu_harness.literature.preflight.time", past, create=True):
                result = self._run_preflight(temporary, "past.pdf")
            self.assertEqual(result.status, PreflightStatus.READY_FOR_UNATTENDED)
            self.assertTrue(result.current_session_download_verified)

    def test_file_written_at_the_boundary_instant_is_current_session(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="v028-edge-", dir=TEMP_DIR) as temporary:
            path = Path(temporary) / "exact.pdf"
            path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            result = _result_from_literature_run(
                "OxfordAcademic",
                self._success_run(path),
                started_at_ns=path.stat().st_mtime_ns,
                research_candidate=True,
            )
            self.assertEqual(result.status, PreflightStatus.READY_FOR_UNATTENDED)
            self.assertTrue(result.current_session_download_verified)

    def test_boundary_has_no_backdating_tolerance(self) -> None:
        # Rule 48 fails closed: a file older than the boundary is not evidence, and
        # no tolerance window may be introduced to soften that. One nanosecond is
        # the tightest statement of the property.
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="v028-tolerance-", dir=TEMP_DIR) as temporary:
            path = Path(temporary) / "stale.pdf"
            path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            for backdate_ns in (1, 1_000, 1_000_000, 10_000_000):
                with self.subTest(backdate_ns=backdate_ns):
                    result = _result_from_literature_run(
                        "OxfordAcademic",
                        self._success_run(path),
                        started_at_ns=path.stat().st_mtime_ns + backdate_ns,
                        research_candidate=True,
                    )
                    self.assertEqual(result.status, PreflightStatus.NOT_READY)
                    self.assertFalse(result.current_session_download_verified)

    def test_repeated_download_preflight_never_flakes_on_session_boundary(self) -> None:
        # The original flake: the file is written immediately after the boundary is
        # captured. With float64 seconds this failed ~4-5% of the time.
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        not_ready = []
        with tempfile.TemporaryDirectory(prefix="v028-repeat-", dir=TEMP_DIR) as temporary:
            for index in range(300):
                result = self._run_preflight(temporary, f"repeat{index}.pdf")
                if result.status is not PreflightStatus.READY_FOR_UNATTENDED:
                    not_ready.append(index)
        self.assertEqual(not_ready, [], f"session-boundary flake on iterations {not_ready}")


if __name__ == "__main__":
    unittest.main()
