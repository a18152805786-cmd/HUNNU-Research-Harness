from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from hunnu_harness.agent_entrypoint import (
    AgentRequestRouter,
    route_from_cli_args,
    write_dry_run_result,
)
from hunnu_harness.cli import build_parser as build_harness_parser
from hunnu_harness.literature.security import scan_text_for_sensitive_leaks
from hunnu_harness.literature.adapters.cnki import CNKIAdapter
from hunnu_harness.paths import CORE_ROOT, OUTPUT_ROOT, TEMP_DIR, V0216_RUN_ROOT, is_within


class AgentIntentRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = AgentRequestRouter()

    def test_chinese_paper_request_routes_to_existing_literature_layer(self) -> None:
        decision = self.router.route("帮我找论文")
        self.assertEqual(decision.task_type, "LiteratureAcquisition")
        self.assertTrue(decision.research_acquisition_intent_detected)
        self.assertTrue(decision.harness_selected)
        self.assertTrue(decision.harness_capability_available)
        self.assertTrue(decision.correct_router_selected)

    def test_cnki_request_routes_only_to_cnki_adapter(self) -> None:
        decision = self.router.route("从 CNKI 找这篇论文：人工智能治理研究")
        self.assertEqual(decision.selected_sources, ("CNKI",))
        self.assertEqual(decision.literature_plans[0].source, "CNKI")

    def test_springer_authorized_fulltext_request_routes_to_existing_adapter(self) -> None:
        decision = self.router.route("下载当前账号有权访问的 Springer 全文")
        self.assertEqual(decision.selected_sources, ("SpringerLink",))
        self.assertTrue(decision.authorized_full_text_only)
        self.assertGreater(decision.estimated_downloads, 0)

    def test_research_database_request_routes_to_data_handler_without_assuming_details(self) -> None:
        decision = self.router.route("找科研数据库数据")
        self.assertEqual(decision.task_type, "DataAcquisition")
        self.assertTrue(decision.harness_selected)
        self.assertTrue(decision.harness_capability_available)
        self.assertTrue(decision.correct_router_selected)
        self.assertFalse(decision.request_validated)
        self.assertEqual(decision.status, "NEEDS_STRUCTURED_DATA_REQUEST")

    def test_pure_knowledge_question_does_not_route_to_acquisition(self) -> None:
        decision = self.router.route("解释 DDML 是什么")
        self.assertEqual(decision.task_type, "NonAcquisition")
        self.assertFalse(decision.harness_selected)

    def test_manuscript_edit_request_does_not_route_to_acquisition(self) -> None:
        decision = self.router.route("帮我修改论文这一段")
        self.assertEqual(decision.task_type, "NonAcquisition")
        self.assertFalse(decision.harness_selected)

    def test_unsupported_source_stops_without_ad_hoc_browser_fallback(self) -> None:
        decision = self.router.route(
            {
                "TaskType": "literature_search",
                "Query": "find a paper",
                "PreferredSources": "Wiley",
            }
        )
        self.assertTrue(decision.harness_selected)
        self.assertFalse(decision.harness_capability_available)
        self.assertEqual(decision.status, "UNSUPPORTED_CAPABILITY")
        self.assertIn("Wiley", decision.missing_capability)
        self.assertFalse(decision.network_acquisition_performed)


class AgentRequestSafetyAndCapsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = AgentRequestRouter()

    def test_global_candidate_and_download_caps_are_not_expanded_across_sources(self) -> None:
        decision = self.router.route(
            {
                "TaskType": "literature_search",
                "Query": "AI washing audit monitoring",
                "Languages": ["zh", "en"],
                "PreferredSources": "auto",
                "MaxCandidates": 20,
                "MaxDownloads": 5,
                "ScreeningFirst": True,
            }
        )
        self.assertTrue(decision.is_routable)
        self.assertEqual(sum(plan.max_candidates for plan in decision.literature_plans), 20)
        self.assertEqual(sum(plan.max_downloads for plan in decision.literature_plans), 5)
        self.assertTrue(all(plan.max_candidates <= 20 for plan in decision.literature_plans))
        self.assertTrue(all(plan.max_downloads <= 5 for plan in decision.literature_plans))

    def test_default_request_is_screening_first_and_has_no_implicit_download(self) -> None:
        decision = self.router.route("帮我找 AI washing 相关论文")
        self.assertTrue(decision.screening_first)
        self.assertEqual(decision.estimated_downloads, 0)
        self.assertEqual(decision.full_text_reading_mode, "LocalFile")

    def test_large_request_enters_budget_gate_before_any_acquisition(self) -> None:
        decision = self.router.route(
            {
                "TaskType": "literature_search",
                "Query": "AI washing",
                "MaxCandidates": 31,
                "MaxDownloads": 0,
            }
        )
        self.assertTrue(decision.planning_and_budget_gate)
        self.assertFalse(decision.is_routable)
        self.assertEqual(decision.status, "PLANNING_AND_BUDGET_GATE")
        self.assertEqual(decision.estimated_candidates, 31)

    def test_browser_fulltext_reading_is_not_accepted_as_agent_default(self) -> None:
        decision = self.router.route(
            {
                "TaskType": "literature_search",
                "Query": "AI washing",
                "FullTextReadingMode": "browser",
            }
        )
        self.assertFalse(decision.harness_capability_available)
        self.assertIn("LocalFile", decision.missing_capability)

    def test_auto_obsidian_write_is_not_enabled_by_agent_entrypoint(self) -> None:
        decision = self.router.route(
            {
                "TaskType": "literature_search",
                "Query": "AI washing",
                "WriteObsidian": True,
            }
        )
        self.assertFalse(decision.harness_capability_available)
        self.assertIn("Obsidian", decision.missing_capability)

    def test_authentication_material_is_rejected_and_never_serialized(self) -> None:
        decision = self.router.route(
            {
                "TaskType": "literature_search",
                "Query": "AI washing",
                "Cookie": "not-a-real-secret",
            }
        )
        serialized = json.dumps(decision.as_dict(), ensure_ascii=False)
        self.assertEqual(decision.status, "SECURITY_BOUNDARY_REJECTED")
        self.assertNotIn("not-a-real-secret", serialized)
        self.assertFalse(scan_text_for_sensitive_leaks(serialized))

    def test_decision_preserves_manual_authentication_and_login_state_policy(self) -> None:
        decision = self.router.route("从 CNKI 找论文")
        payload = decision.as_dict()
        self.assertTrue(payload["ManualAuthentication"])
        self.assertTrue(payload["ExistingLoginStatePreserved"])
        self.assertFalse(payload["CookiesExported"])
        self.assertFalse(payload["BrowserStateExported"])
        self.assertFalse(payload["AuthenticationBypass"])


class AgentEntrypointAndOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self.router = AgentRequestRouter()

    def test_v0216_run_root_is_inside_output_root_and_outside_core(self) -> None:
        self.assertTrue(is_within(V0216_RUN_ROOT, OUTPUT_ROOT))
        self.assertFalse(is_within(V0216_RUN_ROOT, CORE_ROOT))

    def test_pytest_cache_is_configured_outside_the_core_root(self) -> None:
        config = (CORE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('cache_dir = "../HUNNU-Research-Harness-Output/temp/pytest-cache"', config)

    def test_dry_run_writes_sanitized_artifact_only_under_output_root(self) -> None:
        decision = self.router.route(
            {
                "TaskType": "literature_search",
                "Query": "test harness routing",
                "MaxCandidates": 3,
                "MaxDownloads": 0,
            }
        )
        with tempfile.TemporaryDirectory(prefix="v0216-agent-", dir=TEMP_DIR) as temporary:
            path = write_dry_run_result(decision, run_root=Path(temporary) / "run")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(is_within(path, OUTPUT_ROOT))
            self.assertFalse(is_within(path, CORE_ROOT))
            self.assertTrue(payload["DryRun"])
            self.assertTrue(payload["HarnessSelected"])
            self.assertTrue(payload["RequestValidated"])
            self.assertTrue(payload["CorrectRouterSelected"])
            self.assertFalse(payload["NetworkAcquisitionPerformed"])
            self.assertFalse(scan_text_for_sensitive_leaks(path.read_text(encoding="utf-8")))

    def test_dry_run_refuses_a_core_destination(self) -> None:
        decision = self.router.route("帮我找论文")
        with self.assertRaises(ValueError):
            write_dry_run_result(decision, run_root=CORE_ROOT / "runs" / "forbidden-agent-run")

    def test_root_cli_exposes_stable_agent_route_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v0216-agent-cli-", dir=TEMP_DIR) as temporary:
            request_path = Path(temporary) / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "TaskType": "literature_search",
                        "Query": "test harness routing",
                        "MaxCandidates": 3,
                        "MaxDownloads": 0,
                    }
                ),
                encoding="utf-8",
            )
            args = build_harness_parser().parse_args(
                [
                    "agent-route",
                    "--request-json",
                    str(request_path),
                    "--dry-run",
                    "--run-root",
                    str(Path(temporary) / "run"),
                ]
            )
            output = io.StringIO()
            with redirect_stdout(output):
                status = route_from_cli_args(args)
            payload = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertTrue(payload["HarnessSelected"])
            self.assertTrue(payload["RequestValidated"])
            self.assertFalse(payload["NetworkAcquisitionPerformed"])
            self.assertTrue(is_within(Path(payload["DryRunArtifact"]), OUTPUT_ROOT))


class AgentDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self.router = AgentRequestRouter()

    def test_literature_plan_delegates_to_existing_workflow_without_creating_browser(self) -> None:
        decision = self.router.route(
            {
                "TaskType": "literature_search",
                "Query": "AI washing",
                "PreferredSources": "CNKI",
                "MaxCandidates": 1,
                "MaxDownloads": 0,
            }
        )
        plan = decision.literature_plans[0]
        browser = MagicMock()
        browser.goto = AsyncMock()
        browser.downloads_dir = TEMP_DIR / "agent-delegation-downloads"
        browser.page = MagicMock()
        browser.page.url = "about:blank"
        browser.page.content = AsyncMock(return_value="<html></html>")
        browser.page.context = MagicMock()
        adapter = CNKIAdapter(browser)
        with tempfile.TemporaryDirectory(prefix="v0216-delegate-lit-", dir=TEMP_DIR) as temporary:
            run_root = Path(temporary) / "run"
            with patch("hunnu_harness.agent_entrypoint.LiteratureAcquisitionWorkflow") as workflow_type:
                workflow_type.return_value.run = AsyncMock(return_value="delegated-literature-result")
                result = asyncio.run(
                    self.router.invoke_literature(plan, adapter=adapter, run_root=run_root)
                )
            self.assertEqual(result, "delegated-literature-result")
            self.assertIs(workflow_type.call_args.args[0], adapter)
            self.assertEqual(workflow_type.call_args.kwargs["run_root"], run_root.resolve())
            workflow_type.return_value.run.assert_awaited_once_with(plan.request)

    def test_complete_data_request_delegates_to_existing_cnrds_workflow(self) -> None:
        decision = self.router.route(
            {
                "TaskType": "data_acquisition",
                "Query": "download CNRDS CNFS data",
                "Database": "CNRDS",
                "Module": "CNFS",
                "Table": "cashflow",
            }
        )
        adapter = object()
        manager = object()
        with patch("hunnu_harness.agent_entrypoint.run_cnrds_download", new=AsyncMock()) as delegate:
            delegate.return_value = "delegated-data-result"
            result = asyncio.run(self.router.invoke_data(decision, adapter=adapter, manager=manager))
        self.assertEqual(result, "delegated-data-result")
        delegate.assert_awaited_once_with(adapter, manager, decision.data_request)


if __name__ == "__main__":
    unittest.main()
