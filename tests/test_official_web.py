from __future__ import annotations

import unittest
from pathlib import Path

from hunnu_harness.agent_entrypoint import AgentRequestRouter
from hunnu_harness.browser.commands import (
    BrowserActionResult,
    BrowserObservation,
    BrowserTargetObservation,
    NavigateCommand,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from hunnu_harness.official_web import (
    DomainNotAllowed,
    OfficialDomainClaim,
    OfficialWebExecutionBroker,
    OfficialWebRequest,
    OfficialWebStatus,
    OfficialityStatus,
    PublicOfficialWebAdapter,
)


class FakeOfficialWebBrowser:
    downloads_dir = None
    navigation_provenance = ("configured candidate URL", "controlled Navigate/Observe")

    def __init__(
        self,
        *,
        final_url: str = "https://journal.example.edu/rules",
        title: str = "投稿须知",
        html: str | None = None,
        text: str | None = "本刊由示例大学主办，欢迎规范研究投稿。",
        structured: str | None = None,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        target_observations=(),
    ) -> None:
        self.session = SessionHandle("official-web-test")
        self.page_handle = PageHandle("main", session=self.session)
        self.final_url = final_url
        self.title = title
        self.html = html
        self.text = text
        self.structured = text if structured is None else structured
        self.status = status
        self.content_type = content_type
        self.target_observations = tuple(target_observations)
        self.commands = []

    async def execute(self, command):
        self.commands.append(command)
        if isinstance(command, NavigateCommand):
            return BrowserActionResult(
                session=self.session,
                page=self.page_handle,
                generation=0,
                action="navigate",
                url=self.final_url,
            )
        if isinstance(command, ObserveCommand):
            return BrowserObservation(
                session=self.session,
                page=self.page_handle,
                generation=0,
                url=self.final_url,
                title=self.title,
                html=self.html,
                visible_text=self.text,
                structured_content=self.structured,
                target_observations=self.target_observations,
                metadata={
                    "HTTPStatus": self.status,
                    "ContentType": self.content_type,
                },
            )
        raise AssertionError(type(command).__name__)


def request(**overrides) -> OfficialWebRequest:
    values = {
        "urls": ("https://journal.example.edu/rules",),
        "allowed_domains": ("example.edu",),
        "official_domain_claims": (
            OfficialDomainClaim(
                domain="journal.example.edu",
                source_type="JournalOfficialWebsite",
                relationship="configured journal-owned domain",
            ),
        ),
    }
    values.update(overrides)
    return OfficialWebRequest(**values)


class OfficialWebRoutingTests(unittest.TestCase):
    def test_official_web_is_registered_and_routed_separately(self) -> None:
        decision = AgentRequestRouter().route(
            {
                "TaskType": "official_web",
                "Query": "查找投稿须知",
                "URLs": ["https://journal.example.edu/rules"],
                "AllowedDomains": ["example.edu"],
                "OfficialDomainClaims": [
                    {
                        "Domain": "journal.example.edu",
                        "SourceType": "JournalOfficialWebsite",
                        "Relationship": "journal-owned",
                    }
                ],
            }
        )
        self.assertEqual(decision.status, "ROUTED")
        self.assertEqual(decision.selected_sources, ("OfficialWeb",))
        self.assertEqual(decision.literature_plans, ())
        self.assertEqual(
            decision.as_dict()["OfficialWebRequest"]["InvocationTarget"],
            "OfficialWebExecutionBroker -> PublicOfficialWebAdapter",
        )

    def test_allowed_domains_are_required(self) -> None:
        decision = AgentRequestRouter().route(
            {
                "TaskType": "official_web",
                "Query": "rules",
                "URLs": ["https://journal.example.edu/rules"],
            }
        )
        self.assertEqual(decision.status, "INVALID_REQUEST")
        self.assertIn("AllowedDomains", decision.missing_request_details)


class OfficialWebAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_page_fetch_returns_complete_evidence_metadata(self) -> None:
        browser = FakeOfficialWebBrowser(
            html=(
                '<html><head><link rel="canonical" '
                'href="https://journal.example.edu/submission"></head>'
                "<body><h1>投稿须知</h1><script>ignore()</script><p>公开规则正文</p></body></html>"
            ),
            text=None,
        )
        evidence = await PublicOfficialWebAdapter(browser).fetch(
            "https://journal.example.edu/rules", request()
        )
        self.assertEqual(evidence.officiality_status, OfficialityStatus.OFFICIAL_CONFIRMED)
        self.assertEqual(evidence.domain, "journal.example.edu")
        self.assertEqual(evidence.page_title, "投稿须知")
        self.assertEqual(evidence.http_status, 200)
        self.assertEqual(evidence.content_type, "text/html")
        self.assertEqual(evidence.canonical_url, "https://journal.example.edu/submission")
        self.assertIn("公开规则正文", evidence.text_content)
        self.assertNotIn("ignore", evidence.text_content)
        self.assertTrue(evidence.fetched_at)
        self.assertEqual(
            [type(command) for command in browser.commands],
            [NavigateCommand, ObserveCommand],
        )

    async def test_allowlisted_without_relationship_is_only_probable(self) -> None:
        evidence = await PublicOfficialWebAdapter(FakeOfficialWebBrowser()).fetch(
            "https://journal.example.edu/rules",
            request(official_domain_claims=()),
        )
        self.assertEqual(evidence.officiality_status, OfficialityStatus.OFFICIAL_PROBABLE)

    async def test_configured_claim_without_navigation_provenance_is_only_probable(self) -> None:
        browser = FakeOfficialWebBrowser()
        browser.navigation_provenance = ()
        evidence = await PublicOfficialWebAdapter(browser).fetch(
            "https://journal.example.edu/rules", request()
        )
        self.assertEqual(evidence.officiality_status, OfficialityStatus.OFFICIAL_PROBABLE)
        self.assertIn("navigation provenance unavailable", evidence.officiality_basis)

    async def test_initial_unsupported_domain_fails_before_navigation(self) -> None:
        browser = FakeOfficialWebBrowser()
        adapter = PublicOfficialWebAdapter(browser)
        with self.assertRaises(DomainNotAllowed):
            await adapter.fetch(
                "https://untrusted.example.com/rules", request()
            )
        self.assertEqual(browser.commands, [])

    async def test_redirect_outside_allowlist_fails_closed(self) -> None:
        browser = FakeOfficialWebBrowser(final_url="https://login.other.test/signin")
        with self.assertRaises(DomainNotAllowed):
            await PublicOfficialWebAdapter(browser).fetch(
                "https://journal.example.edu/rules", request()
            )

    async def test_login_and_captcha_are_manual_authentication_gates(self) -> None:
        for text in ("请输入密码后继续", "请拖动下方拼图完成验证"):
            browser = FakeOfficialWebBrowser(text=text)
            result = await OfficialWebExecutionBroker().execute(request(), browser=browser)
            self.assertEqual(result.status, OfficialWebStatus.MANUAL_AUTH_REQUIRED)
            self.assertEqual(result.evidence, ())

    async def test_proven_offscreen_challenge_does_not_block_public_page(self) -> None:
        browser = FakeOfficialWebBrowser(
            text="公开规则正文",
            html="<div style='position:absolute;top:-1000000px'>拖动下方拼图完成验证</div>",
            target_observations=(
                BrowserTargetObservation(
                    marker="拖动下方拼图完成验证",
                    bounding_box={"x": 10, "y": -1000, "width": 100, "height": 20},
                    client_rect={"x": 10, "y": -1000, "width": 100, "height": 20},
                    inspection_complete=True,
                ),
            ),
        )
        evidence = await PublicOfficialWebAdapter(browser).fetch(
            "https://journal.example.edu/rules", request()
        )
        self.assertEqual(evidence.officiality_status, OfficialityStatus.OFFICIAL_CONFIRMED)

    async def test_mcp_style_snapshot_with_proven_offscreen_challenge_is_allowed(self) -> None:
        browser = FakeOfficialWebBrowser(
            text=None,
            html=None,
            structured=(
                "### Snapshot - heading \"投稿须知\" "
                "- text: 公开规则正文 - generic: 拖动下方拼图完成验证"
            ),
            target_observations=(
                BrowserTargetObservation(
                    marker="拖动下方拼图完成验证",
                    bounding_box={"x": 10, "y": -1000, "width": 100, "height": 20},
                    client_rect={"x": 10, "y": -1000, "width": 100, "height": 20},
                    inspection_complete=True,
                ),
            ),
        )
        evidence = await PublicOfficialWebAdapter(browser).fetch(
            "https://journal.example.edu/rules", request()
        )
        self.assertIn("公开规则正文", evidence.text_content)

    async def test_content_type_and_http_access_restrictions_fail_closed(self) -> None:
        for browser in (
            FakeOfficialWebBrowser(content_type="application/pdf"),
            FakeOfficialWebBrowser(status=403, text="Forbidden"),
        ):
            result = await OfficialWebExecutionBroker().execute(request(), browser=browser)
            self.assertEqual(result.status, OfficialWebStatus.ACCESS_RESTRICTED)

    async def test_router_invocation_uses_official_web_broker(self) -> None:
        router = AgentRequestRouter()
        decision = router.route(
            {
                "TaskType": "official_web",
                "Query": "rules",
                "URLs": ["https://journal.example.edu/rules"],
                "AllowedDomains": ["example.edu"],
                "OfficialDomainClaims": [
                    {
                        "Domain": "journal.example.edu",
                        "SourceType": "JournalOfficialWebsite",
                        "Relationship": "journal-owned",
                    }
                ],
            }
        )
        result = await router.invoke_official_web(decision, browser=FakeOfficialWebBrowser())
        self.assertEqual(result.status, OfficialWebStatus.SUCCESS)
        self.assertEqual(len(result.evidence), 1)


if __name__ == "__main__":
    unittest.main()
