from __future__ import annotations

import unittest
from pathlib import Path

from hunnu_harness.agent_entrypoint import (
    AgentRequestRouter,
    LITERATURE_ADAPTER_REGISTRY,
    SUPPORTED_LITERATURE_SOURCES,
)
from hunnu_harness.literature.adapters.oxfordacademic import OxfordAcademicAdapter
from hunnu_harness.literature.institutional import (
    HUNNUInstitutionalAccessResolver,
    InstitutionalResolutionTrigger,
)


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
GATEWAY = (
    "https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE-OXFORD-ROUTE/"
    "?opaque=REDACTED_TEST_VALUE"
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _MockPage:
    def __init__(self, browser: "_MockBrowser") -> None:
        self.browser = browser

    @property
    def url(self) -> str:
        return self.browser.current_url

    async def content(self) -> str:
        return self.browser.current_html

    async def title(self) -> str:
        return HUNNUInstitutionalAccessResolver._parse(self.browser.current_html).title


class _MockBrowser:
    def __init__(self) -> None:
        self.current_url = "about:blank"
        self.current_html = "<html><title>blank</title></html>"
        self.page = _MockPage(self)
        self.pages = {
            "https://www.hunnu.edu.cn/": fixture("hunnu_portal.html"),
            "https://lib.hunnu.edu.cn/": fixture("hunnu_library.html"),
            "https://lib.hunnu.edu.cn/resource/foreign": fixture("hunnu_oxford_foreign_databases.html"),
            "https://wisdom.chaoxing.com/detail/oxford": fixture("hunnu_oxford_detail_gateway.html"),
            GATEWAY: fixture("oxford_gateway_home.html"),
        }

    async def goto(self, url: str) -> None:
        if url not in self.pages:
            raise AssertionError(f"Unexpected fixture navigation: {url}")
        self.current_url = url
        self.current_html = self.pages[url]


class OxfordInstitutionalResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_hunnu_route_reaches_verified_oxford_gateway_without_persisting_opaque_url(self) -> None:
        browser = _MockBrowser()
        resolver = HUNNUInstitutionalAccessResolver(browser)
        route = await resolver.resolve(
            "Oxford Journals Collection",
            trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
        )
        self.assertTrue(route.institutional_route_resolved)
        self.assertIs(route.institutional_target_database_match, True)
        self.assertEqual(route.requested_source, "OxfordAcademic")
        self.assertEqual(route.publisher_entry_url, "https://yclib.hunnu.edu.cn/vpn/")
        serialized = str(route.as_dict())
        self.assertNotIn("OPAQUE-OXFORD-ROUTE", serialized)
        self.assertNotIn("REDACTED_TEST_VALUE", serialized)

    async def test_adapter_accepts_gateway_only_after_binding_verified_route(self) -> None:
        browser = _MockBrowser()
        route = await HUNNUInstitutionalAccessResolver(browser).resolve(
            "OUP",
            trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
        )
        adapter = OxfordAcademicAdapter(browser)
        self.assertFalse(adapter._gateway_trusted())
        adapter.bind_institutional_route(route)
        self.assertTrue(adapter._gateway_trusted())


class OxfordAgentRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = AgentRequestRouter()

    def test_oxford_is_registered_as_a_supported_source(self) -> None:
        self.assertIn("OxfordAcademic", SUPPORTED_LITERATURE_SOURCES)
        self.assertIs(LITERATURE_ADAPTER_REGISTRY["OxfordAcademic"], OxfordAcademicAdapter)

    def test_each_agent_alias_routes_to_oxford_adapter_capability(self) -> None:
        for alias in (
            "OxfordAcademic",
            "Oxford Academic",
            "Oxford Journals",
            "Oxford Journals Collection",
            "OUP",
            "Oxford University Press",
        ):
            decision = self.router.route(
                {
                    "TaskType": "literature_search",
                    "Query": "double machine learning",
                    "PreferredSources": alias,
                    "MaxCandidates": 1,
                    "MaxDownloads": 0,
                }
            )
            self.assertTrue(decision.harness_capability_available, alias)
            self.assertEqual(decision.selected_sources, ("OxfordAcademic",), alias)


if __name__ == "__main__":
    unittest.main()
