from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.agent_entrypoint import (
    AgentRequestRouter,
    LITERATURE_ADAPTER_REGISTRY,
    LiteratureSourcePlan,
)
from hunnu_harness.browser.transport import (
    BrowserTransport,
    BrowserTransportError,
)
from hunnu_harness.browser.commands import BrowserActionResult, PageHandle, SessionHandle
from hunnu_harness.literature.adapters import (
    CNKIAdapter,
    LiteratureSourceAdapter,
    OxfordAcademicAdapter,
    ScienceDirectAdapter,
    SpringerLinkAdapter,
)
from hunnu_harness.literature.execution import (
    AdapterIdentityError,
    AdapterResolutionError,
    LiteratureAdapterFactory,
)
from hunnu_harness.literature.models import (
    AccessDecision,
    LiteratureRecord,
    LiteratureSearchRequest,
)
from hunnu_harness.paths import TEMP_DIR


class _FakeContext:
    pages: tuple[object, ...] = ()

    class _Request:
        async def get(self, _url: str, *, timeout: int):
            del timeout
            return object()

    request = _Request()

    def on(self, _event: str, _callback) -> None:
        return None


class _FakePage:
    url = "about:blank"
    context = _FakeContext()

    async def content(self) -> str:
        return "<html></html>"

    def locator(self, _selector: str):
        return object()

    def expect_download(self, *, timeout: int):
        del timeout
        return object()

    def on(self, _event: str, _callback) -> None:
        return None


class _FakeTransport:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.page = _FakePage()
        self.downloads_dir = TEMP_DIR / "adapter-enforcement-downloads"

    async def goto(self, _url: str) -> None:
        self.events.append("browser.goto")


class _TracingAdapter(LiteratureSourceAdapter):
    name = "CNKI"
    human_like_delay_seconds = 0

    def __init__(self, browser) -> None:
        super().__init__(browser)
        browser.events.append("adapter.init")

    async def search(self, _query: str, _request: LiteratureSearchRequest) -> list[LiteratureRecord]:
        self.browser.events.append("adapter.search")
        await self.browser.goto("https://example.test/search")
        return []

    async def open_result(self, _record: LiteratureRecord) -> None:
        return None

    async def extract_metadata(self, *, search_query: str) -> LiteratureRecord:
        return LiteratureRecord(search_query=search_query, source_database=self.name)

    async def extract_abstract(self) -> str:
        return ""

    async def check_fulltext_access(self) -> AccessDecision:
        raise AssertionError("not used by the adapter-first ordering test")

    async def download_fulltext(self, _record: LiteratureRecord, _access: AccessDecision) -> Path:
        raise AssertionError("not used by the adapter-first ordering test")

    async def get_citation(self) -> dict[str, str]:
        return {}


class _BrokenAdapter(_TracingAdapter):
    def __init__(self, _browser) -> None:
        raise RuntimeError("construction failed")


class _CNKISubclass(CNKIAdapter):
    pass


class _AffinityCommandPort:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.downloads_dir = TEMP_DIR / "adapter-affinity-downloads"
        self.session = SessionHandle("adapter-affinity-session", 0)
        self.page_handle = PageHandle("adapter-affinity-page", session=self.session, generation=0)

    async def execute(self, command) -> BrowserActionResult:
        self.events.append(f"browser.execute:{command.kind}")
        return BrowserActionResult(action=command.kind, success=True)

    async def select_source_page(self, *, source: str, source_origin: str) -> None:
        self.events.append(f"affinity.select:{source}:{source_origin}")


class _AffinityTracingAdapter(_TracingAdapter):
    search_origin = "https://target-source.test"


def _request() -> LiteratureSearchRequest:
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": "adapter enforcement test",
            "ResearchQuestion": "adapter enforcement test",
            "MaxSearchResults": 1,
            "MaxResultsPerSource": 1,
            "MaxDownloads": 0,
            "MaxDownloadsPerRun": 0,
        }
    )


def _plan(source: str) -> LiteratureSourcePlan:
    return LiteratureSourcePlan(
        source=source,
        request=_request(),
        max_candidates=1,
        max_downloads=0,
    )


class AdapterResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_registry_resolves_and_instantiates_each_registered_adapter(self) -> None:
        factory = LiteratureAdapterFactory(LITERATURE_ADAPTER_REGISTRY)
        expected = {
            "CNKI": CNKIAdapter,
            "SpringerLink": SpringerLinkAdapter,
            "ScienceDirect": ScienceDirectAdapter,
            "OxfordAcademic": OxfordAcademicAdapter,
        }

        for source, adapter_type in expected.items():
            with self.subTest(source=source):
                adapter = factory.create(source, browser=_FakeTransport())
                self.assertIs(type(adapter), adapter_type)
                self.assertEqual(adapter.name, source)

    def test_legacy_adapter_requires_exact_registered_class(self) -> None:
        factory = LiteratureAdapterFactory(LITERATURE_ADAPTER_REGISTRY)
        transport = _FakeTransport()
        exact = CNKIAdapter(transport)

        self.assertIs(factory.validate_instance("CNKI", exact, browser=transport), exact)
        with self.assertRaises(AdapterIdentityError):
            factory.validate_instance("CNKI", _CNKISubclass(transport), browser=transport)

    def test_legacy_adapter_rejects_a_different_browser_binding(self) -> None:
        factory = LiteratureAdapterFactory(LITERATURE_ADAPTER_REGISTRY)
        adapter = CNKIAdapter(_FakeTransport())

        with self.assertRaises(AdapterIdentityError):
            factory.validate_instance("CNKI", adapter, browser=_FakeTransport())

    def test_source_normalization_is_used_for_resolution_and_identity(self) -> None:
        factory = LiteratureAdapterFactory(LITERATURE_ADAPTER_REGISTRY)
        adapter = factory.create(" CNKI ", browser=_FakeTransport())

        self.assertIs(type(adapter), CNKIAdapter)
        self.assertEqual(adapter.name, "CNKI")

    def test_router_plan_and_factory_resolution_cover_all_registered_sources(self) -> None:
        router = AgentRequestRouter()
        for source in ("CNKI", "SpringerLink", "ScienceDirect", "OxfordAcademic"):
            with self.subTest(source=source):
                decision = router.route(
                    {
                        "TaskType": "literature_search",
                        "Query": "source resolution",
                        "PreferredSources": source,
                        "MaxCandidates": 1,
                        "MaxDownloads": 0,
                    }
                )
                self.assertEqual(decision.selected_sources, (source,))
                adapter = router.adapter_factory.create(source, browser=_FakeTransport())
                self.assertEqual(adapter.name, decision.literature_plans[0].source)

    def test_wrong_registered_adapter_is_rejected_before_workflow_creation(self) -> None:
        router = AgentRequestRouter()
        decision = router.route(
            {
                "TaskType": "literature_search",
                "Query": "wrong adapter",
                "PreferredSources": "CNKI",
                "MaxCandidates": 1,
                "MaxDownloads": 0,
            }
        )
        wrong_adapter = SpringerLinkAdapter(_FakeTransport())

        with tempfile.TemporaryDirectory(prefix="adapter-enforcement-", dir=TEMP_DIR) as temporary:
            with patch("hunnu_harness.agent_entrypoint.LiteratureAcquisitionWorkflow") as workflow_type:
                with self.assertRaises(AdapterIdentityError):
                    asyncio.run(
                        router.invoke_literature(
                            decision.literature_plans[0],
                            adapter=wrong_adapter,
                            run_root=Path(temporary) / "run",
                        )
                    )
            workflow_type.assert_not_called()

    def test_unknown_source_fails_closed_without_browser_fallback(self) -> None:
        router = AgentRequestRouter()

        with tempfile.TemporaryDirectory(prefix="adapter-enforcement-", dir=TEMP_DIR) as temporary:
            with self.assertRaises(AdapterResolutionError):
                asyncio.run(
                    router.invoke_literature(
                        _plan("UnregisteredSource"),
                        browser=_FakeTransport(),
                        run_root=Path(temporary) / "run",
                    )
                )

    def test_incomplete_transport_fails_closed(self) -> None:
        factory = LiteratureAdapterFactory(LITERATURE_ADAPTER_REGISTRY)

        with self.assertRaises(BrowserTransportError):
            factory.create("CNKI", browser=object())

    def test_adapter_construction_failure_does_not_fall_back_to_browser(self) -> None:
        router = AgentRequestRouter(adapter_factory=LiteratureAdapterFactory({"CNKI": _BrokenAdapter}))
        decision = router.route(
            {
                "TaskType": "literature_search",
                "Query": "construction failure",
                "PreferredSources": "CNKI",
                "MaxCandidates": 1,
                "MaxDownloads": 0,
            }
        )
        transport = _FakeTransport()

        with tempfile.TemporaryDirectory(prefix="adapter-enforcement-", dir=TEMP_DIR) as temporary:
            with patch("hunnu_harness.agent_entrypoint.LiteratureAcquisitionWorkflow") as workflow_type:
                with self.assertRaises(AdapterResolutionError):
                    asyncio.run(
                        router.invoke_literature(
                            decision.literature_plans[0],
                            browser=transport,
                            run_root=Path(temporary) / "run",
                        )
                    )
            workflow_type.assert_not_called()
        self.assertEqual(transport.events, [])


class AdapterFirstExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_browser_transport_satisfies_the_minimal_contract(self) -> None:
        self.assertIsInstance(_FakeTransport(), BrowserTransport)

    def test_execution_resolves_adapter_before_any_browser_operation(self) -> None:
        events: list[str] = []
        transport = _FakeTransport(events)
        factory = LiteratureAdapterFactory({"CNKI": _TracingAdapter})
        router = AgentRequestRouter(adapter_factory=factory)
        decision = router.route(
            {
                "TaskType": "literature_search",
                "Query": "adapter first",
                "PreferredSources": "CNKI",
                "MaxCandidates": 1,
                "MaxDownloads": 0,
            }
        )

        class _Workflow:
            def __init__(self, adapter, **_kwargs) -> None:
                events.append("workflow.init")
                self.adapter = adapter

            async def run(self, request) -> str:
                events.append("workflow.run")
                await self.adapter.search("adapter first", request)
                return "executed"

        with tempfile.TemporaryDirectory(prefix="adapter-enforcement-", dir=TEMP_DIR) as temporary:
            with patch("hunnu_harness.agent_entrypoint.LiteratureAcquisitionWorkflow", _Workflow):
                result = asyncio.run(
                    router.invoke_literature(
                        decision.literature_plans[0],
                        browser=transport,
                        run_root=Path(temporary) / "run",
                    )
                )

        self.assertEqual(result, "executed")
        self.assertEqual(
            events,
            ["adapter.init", "workflow.init", "workflow.run", "adapter.search", "browser.goto"],
        )

    def test_execution_binds_resolved_source_page_before_workflow_entry(self) -> None:
        events: list[str] = []
        port = _AffinityCommandPort(events)
        router = AgentRequestRouter(
            adapter_factory=LiteratureAdapterFactory({"CNKI": _AffinityTracingAdapter})
        )
        decision = router.route(
            {
                "TaskType": "literature_search",
                "Query": "source affinity",
                "PreferredSources": "CNKI",
                "MaxCandidates": 1,
                "MaxDownloads": 0,
            }
        )

        class _Workflow:
            def __init__(self, adapter, **_kwargs) -> None:
                events.append("workflow.init")
                self.adapter = adapter

            async def run(self, _request) -> str:
                events.append("workflow.run")
                return "executed"

        with tempfile.TemporaryDirectory(prefix="adapter-affinity-", dir=TEMP_DIR) as temporary:
            with patch("hunnu_harness.agent_entrypoint.LiteratureAcquisitionWorkflow", _Workflow):
                result = asyncio.run(
                    router.invoke_literature(
                        decision.literature_plans[0],
                        browser=port,
                        run_root=Path(temporary) / "run",
                    )
                )

        self.assertEqual(result, "executed")
        self.assertEqual(
            events,
            [
                "adapter.init",
                "affinity.select:CNKI:https://target-source.test",
                "workflow.init",
                "workflow.run",
            ],
        )


class AgentPolicyDocumentationTests(unittest.TestCase):
    def test_harness_literature_policy_is_not_mcp_first(self) -> None:
        root = Path(__file__).resolve().parents[1]
        documents = (root / "AGENTS.md", root / "docs" / "AGENT_INTEGRATION.md")
        forbidden_phrases = (
            "For each database, first open its official site on the dedicated Playwright MCP page",
            "The Agent first attempts unattended download with the exact existing Research Chrome profile",
            "handlers backed by the dedicated Playwright MCP pages",
        )
        for document in documents:
            content = document.read_text(encoding="utf-8")
            with self.subTest(document=document):
                self.assertIn(
                    "Adapter resolution comes before any Harness-managed publisher navigation.",
                    content,
                )
                self.assertIn("MCPExecutor", content)
                self.assertIn("manual/diagnostic browser work", content)
                for phrase in forbidden_phrases:
                    self.assertNotIn(phrase, content)
