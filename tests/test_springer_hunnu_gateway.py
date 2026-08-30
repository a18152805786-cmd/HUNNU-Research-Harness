from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from hunnu_harness.literature import cli as literature_cli
from hunnu_harness.literature.adapters.base import SourceLayoutChanged
from hunnu_harness.literature.adapters.springerlink import SpringerLinkAdapter
from hunnu_harness.literature.artifacts import LiteratureArtifactWriter
from hunnu_harness.literature.downloads import LiteratureDownloadManager
from hunnu_harness.literature.institutional import (
    InstitutionalResolutionTrigger,
    InstitutionalRouteResult,
    InstitutionalRouteStep,
)
from hunnu_harness.literature.models import (
    AccessType,
    LiteratureRecord,
    LiteratureRunResult,
    LiteratureSearchRequest,
    RunStatus,
)
from hunnu_harness.literature.security import scan_text_for_sensitive_leaks
from hunnu_harness.literature.workflow import finalize_captured_hunnu_springer_acceptance
from hunnu_harness.paths import TEMP_DIR

from hunnu_harness.literature.fetch_ledger import FulltextFetchLedger

from literature_test_support import minimal_pdf_with_text_bytes, write_minimal_pdf_with_text


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
TITLE = "Gateway-safe Springer article"
DOI = "10.1007/s00000-026-00001-2"
ARTICLE_URL = f"https://link.springer.com/article/{DOI}"
GATEWAY = (
    "https://yclib.hunnu.edu.cn/vpn/fixture-springer-route/"
    "?route_marker=SYNTHETIC_ONLY"
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def gateway_route(*, source: str = "SpringerLink") -> InstitutionalRouteResult:
    return InstitutionalRouteResult(
        requested_source=source,
        resolution_trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
        institutional_route_resolved=True,
        institutional_target_database_match=True,
        route_result="SUCCESS",
        reason="synthetic verified fixture route",
        route_steps=(
            InstitutionalRouteStep(
                1,
                "HUNNU Official Portal",
                "湖南师范大学",
                "www.hunnu.edu.cn",
                "https://www.hunnu.edu.cn/",
                "official portal reached",
            ),
            InstitutionalRouteStep(
                2,
                f"{source} / Publisher",
                "Springer Nature Link",
                "yclib.hunnu.edu.cn",
                "https://yclib.hunnu.edu.cn/vpn/",
                "verified publisher reached through HUNNU route",
            ),
        ),
        publisher_entry_url="https://yclib.hunnu.edu.cn/vpn/",
        publisher_navigation_url=GATEWAY,
    )


def target_record() -> LiteratureRecord:
    return LiteratureRecord(
        paper_id="P-SPRINGER-GATEWAY",
        title=TITLE,
        authors=("Ada Researcher",),
        year="2026",
        journal="Journal of Synthetic Fixtures",
        doi=DOI,
        source_database="SpringerLink",
        source_page=ARTICLE_URL,
        stable_identifier=DOI,
        search_query=TITLE,
    )


def target_request() -> LiteratureSearchRequest:
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": f"Find exact Springer paper: {TITLE}",
            "ExactTitles": [TITLE],
            "DOIs": [DOI],
            "MaxSearchResults": 1,
            "MaxResultsPerSource": 1,
            "MaxDownloads": 1,
            "MaxDownloadsPerRun": 1,
            "RequireFullText": True,
        }
    )


def gateway_article_url() -> str:
    return f"{GATEWAY.split('?', 1)[0]}article/{DOI}?route_marker=SYNTHETIC_ONLY"


def search_fixture() -> str:
    return f'<html><body><a href="/article/{DOI}">{TITLE}</a></body></html>'


class _Emitter:
    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}

    def on(self, event: str, callback: Any) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        callbacks = self.listeners.get(event, [])
        if callback in callbacks:
            callbacks.remove(callback)

    def emit(self, event: str, value: Any) -> None:
        for callback in tuple(self.listeners.get(event, ())):
            callback(value)


class _HTTPResponse:
    ok = True
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def body(self) -> bytes:
        return self.payload


class _Request:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[str] = []

    async def get(self, url: str, **_: Any) -> _HTTPResponse:
        self.calls.append(url)
        return _HTTPResponse(self.payload)


class _Download:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def save_as(self, destination: str) -> None:
        Path(destination).write_bytes(self.payload)


class _Locator:
    def __init__(self, page: "_Page") -> None:
        self.page = page
        self.clicked = False

    def filter(self, **_: Any) -> "_Locator":
        return self

    @property
    def first(self) -> "_Locator":
        return self

    async def click(self) -> None:
        self.clicked = True
        self.page.emit("download", _Download(self.page.download_payload))


class _Context(_Emitter):
    def __init__(self, payload: bytes) -> None:
        super().__init__()
        self.pages: list[_Page] = []
        self.request = _Request(payload)


class _Page(_Emitter):
    def __init__(self, context: _Context, payload: bytes) -> None:
        super().__init__()
        self.context = context
        self.download_payload = payload
        self.url = "about:blank"
        self.html = "<html><title>blank</title></html>"
        self.action = _Locator(self)
        context.pages.append(self)

    async def content(self) -> str:
        return self.html

    def locator(self, _: str) -> _Locator:
        return self.action


class _Browser:
    def __init__(self, downloads_dir: Path, *, article_html: str, pdf_text: str) -> None:
        payload = minimal_pdf_with_text_bytes(pdf_text)
        self.context = _Context(payload)
        self.page = _Page(self.context, payload)
        self.downloads_dir = downloads_dir
        self.article_html = article_html
        self.history: list[str] = []

    async def goto(self, url: str) -> None:
        self.history.append(url)
        self.page.url = url
        self.page.html = self.article_html


class SpringerHUNNUGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    async def _bound_adapter(
        self,
        root: Path,
        *,
        article_html: str = "springerlink_article_hunnu_gateway.html",
        pdf_text: str | None = None,
    ) -> tuple[SpringerLinkAdapter, _Browser, LiteratureRecord]:
        browser = _Browser(
            root,
            article_html=fixture(article_html),
            pdf_text=pdf_text or f"{TITLE} Ada Researcher DOI: {DOI}",
        )
        adapter = SpringerLinkAdapter(
            browser,
            allow_capture_outside_output_for_tests=True,
            capture_timeout_ms=2_000,
        )
        adapter.fetch_ledger = FulltextFetchLedger(path=root / "fetch_ledger.jsonl")
        adapter.bind_institutional_route(gateway_route())
        record = target_record()
        await adapter.open_result(record)
        return adapter, browser, record

    async def test_direct_springer_open_access_keeps_existing_http_download_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="springer-direct-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            title = (
                "Accounting for the middle: motivations, extent, and limitations of "
                "middle managers’ earnings management"
            )
            doi = "10.1007/s11573-023-01162-8"
            browser = _Browser(
                root,
                article_html=fixture("springerlink_article_open_access.html"),
                pdf_text=f"Direct OA fixture Sebastian Wagener DOI: {doi}",
            )
            adapter = SpringerLinkAdapter(browser)
            adapter.fetch_ledger = FulltextFetchLedger(path=root / "fetch_ledger.jsonl")
            record = LiteratureRecord(
                paper_id="DIRECT",
                title=title,
                authors=("Sebastian Wagener",),
                doi=doi,
                source_database="SpringerLink",
                source_page=f"https://link.springer.com/article/{doi}",
                stable_identifier=doi,
                search_query=title,
            )
            await adapter.open_result(record)
            detail = await adapter.extract_metadata(search_query=title)
            access = await adapter.check_fulltext_access()
            downloaded = await adapter.download_fulltext(detail, access)
            self.assertEqual(access.access_type, AccessType.OPEN_ACCESS)
            self.assertEqual(len(browser.context.request.calls), 1)
            self.assertFalse(browser.page.action.clicked)
            self.assertTrue(downloaded.read_bytes().startswith(b"%PDF-"))

    async def test_verified_gateway_preserves_canonical_identity_and_accepts_institution_signal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="springer-gateway-", dir=TEMP_DIR) as temporary:
            adapter, browser, expected = await self._bound_adapter(Path(temporary))
            self.assertTrue(adapter._gateway_trusted())
            self.assertTrue(browser.history[-1].startswith(GATEWAY.split("?", 1)[0] + "article/"))
            detail = await adapter.extract_metadata(search_query=TITLE)
            access = await adapter.check_fulltext_access()
            self.assertEqual(detail.source_page, ARTICLE_URL)
            self.assertTrue(detail.navigation_url.startswith("https://yclib.hunnu.edu.cn/vpn/"))
            self.assertTrue(detail.target_identity_confirmed)
            self.assertEqual(expected.paper_id, detail.paper_id)
            self.assertTrue(access.authorized_access)
            self.assertEqual(access.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)
            self.assertTrue(access.download_url.startswith(GATEWAY.split("?", 1)[0] + "content/pdf/"))

    async def test_gateway_accepts_non_hunnu_display_institution_signal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="springer-gateway-alias-", dir=TEMP_DIR) as temporary:
            browser = _Browser(
                Path(temporary),
                article_html=fixture("springerlink_article_hunnu_gateway.html").replace(
                    "Access provided by Hunan Normal University",
                    "Access provided by 10847 SLCC Central China – Hunan",
                ),
                pdf_text=f"{TITLE} Ada Researcher DOI: {DOI}",
            )
            adapter = SpringerLinkAdapter(
                browser,
                allow_capture_outside_output_for_tests=True,
                capture_timeout_ms=2_000,
            )
            adapter.bind_institutional_route(gateway_route())
            record = target_record()
            await adapter.open_result(record)
            detail = await adapter.extract_metadata(search_query=TITLE)
            access = await adapter.check_fulltext_access()
            self.assertTrue(detail.target_identity_confirmed)
            self.assertTrue(access.authorized_access)
            self.assertEqual(access.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)

    async def test_unverified_and_overbroad_gateway_routes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="springer-untrusted-", dir=TEMP_DIR) as temporary:
            browser = _Browser(
                Path(temporary),
                article_html=fixture("springerlink_article_hunnu_gateway.html"),
                pdf_text=f"{TITLE} Ada Researcher DOI: {DOI}",
            )
            adapter = SpringerLinkAdapter(browser)
            for value in (
                "https://hunnu.edu.cn/vpn/article/10.1007/example",
                "https://yclib.hunnu.edu.cn/vpn/unbound/article/10.1007/example",
                "https://yclib.hunnu.edu.cn/article/10.1007/example",
                "https://proxy.example.test/vpn/article/10.1007/example",
            ):
                self.assertFalse(adapter._is_trusted_url(value), value)
            malformed = gateway_route()
            object.__setattr__(malformed, "publisher_entry_url", "https://yclib.hunnu.edu.cn/not-vpn/")
            with self.assertRaises(SourceLayoutChanged):
                adapter.bind_institutional_route(malformed)

    async def test_unbound_gateway_page_cannot_be_extracted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="springer-unbound-page-", dir=TEMP_DIR) as temporary:
            browser = _Browser(
                Path(temporary),
                article_html=fixture("springerlink_article_hunnu_gateway.html"),
                pdf_text=f"{TITLE} Ada Researcher DOI: {DOI}",
            )
            browser.page.url = GATEWAY + f"article/{DOI}"
            browser.page.html = fixture("springerlink_article_hunnu_gateway.html")
            adapter = SpringerLinkAdapter(browser)
            text_only_access = SpringerLinkAdapter.check_fulltext_access_html(
                fixture("springerlink_article_hunnu_gateway.html"),
                source_url=GATEWAY + f"article/{DOI}",
            )
            self.assertFalse(text_only_access.authorized_access)
            with self.assertRaisesRegex(SourceLayoutChanged, "left the trusted route"):
                await adapter.extract_metadata(search_query=TITLE)

    async def test_gateway_article_identity_mismatch_is_rejected_before_access(self) -> None:
        with tempfile.TemporaryDirectory(prefix="springer-article-mismatch-", dir=TEMP_DIR) as temporary:
            adapter, _, _ = await self._bound_adapter(
                Path(temporary),
                article_html="springerlink_article_hunnu_gateway_mismatch.html",
            )
            with self.assertRaisesRegex(SourceLayoutChanged, "target identity lock failed"):
                await adapter.extract_metadata(search_query=TITLE)

    async def test_institutional_download_uses_official_click_and_download_event_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="springer-download-", dir=TEMP_DIR) as temporary:
            adapter, browser, _ = await self._bound_adapter(Path(temporary))
            detail = await adapter.extract_metadata(search_query=TITLE)
            access = await adapter.check_fulltext_access()
            downloaded = await adapter.download_fulltext(detail, access)
            self.assertTrue(downloaded.exists())
            self.assertTrue(browser.page.action.clicked)
            self.assertEqual(browser.context.request.calls, [])
            self.assertNotIn("response", browser.context.listeners)
            self.assertEqual(detail.acquisition_method, "PLAYWRIGHT_DOWNLOAD_EVENT")
            self.assertTrue(detail.download_event_emitted)
            self.assertFalse(detail.authorized_pdf_response_captured)
            self.assertTrue(detail.target_doi_matched)
            self.assertTrue(detail.target_identity_confirmed)

    async def test_wrong_downloaded_pdf_is_quarantined_and_rejected(self) -> None:
        wrong_text = fixture("springerlink_wrong_pdf_identity.txt").strip()
        with tempfile.TemporaryDirectory(prefix="springer-wrong-pdf-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            adapter, browser, _ = await self._bound_adapter(root, pdf_text=wrong_text)
            detail = await adapter.extract_metadata(search_query=TITLE)
            access = await adapter.check_fulltext_access()
            with self.assertRaisesRegex(SourceLayoutChanged, "failed local target identity"):
                await adapter.download_fulltext(detail, access)
            self.assertEqual(browser.context.request.calls, [])
            self.assertFalse(detail.target_identity_confirmed)
            self.assertEqual(len(list((root / "quarantine").glob("*.pdf"))), 1)

    async def test_gateway_artifacts_keep_canonical_url_and_remove_runtime_route(self) -> None:
        with tempfile.TemporaryDirectory(prefix="springer-artifact-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            adapter, _, _ = await self._bound_adapter(root / "capture")
            detail = await adapter.extract_metadata(search_query=TITLE)
            access = await adapter.check_fulltext_access()
            captured = await adapter.download_fulltext(detail, access)
            manager = LiteratureDownloadManager(
                root / "run" / "downloads",
                allow_outside_project_for_tests=True,
                make_archive_read_only=False,
            )
            entry = manager.archive_authorized_pdf(captured, detail, access)
            writer = LiteratureArtifactWriter(root / "run", allow_outside_project_for_tests=True)
            writer.write_download_manifest((entry,), institutional_routes=(gateway_route(),))
            text = writer.download_manifest_path.read_text(encoding="utf-8")
            payload = json.loads(text)
            self.assertEqual(payload["Downloads"][0]["OriginalURLOrStableIdentifier"], DOI)
            self.assertEqual(payload["Downloads"][0]["Institution"], "湖南师范大学")
            self.assertTrue(payload["Downloads"][0]["InstitutionalRouteUsed"])
            self.assertNotIn("fixture-springer-route", text)
            self.assertNotIn("route_marker", text)
            self.assertNotIn("SYNTHETIC_ONLY", text)
            self.assertEqual(scan_text_for_sensitive_leaks(text), [])


class SpringerHUNNUFinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_verified_gateway_finalizer_reuses_bound_gateway_access_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="springer-finalizer-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            captured = write_minimal_pdf_with_text(
                root / "captured.pdf",
                f"{TITLE} Ada Researcher DOI: {DOI}",
            )
            run_root = root / "run"
            with patch.object(
                SpringerLinkAdapter,
                "check_fulltext_access_html",
                side_effect=AssertionError("direct-only access checker must not run"),
            ):
                result = finalize_captured_hunnu_springer_acceptance(
                    request=target_request(),
                    query=TITLE,
                    search_html=search_fixture(),
                    article_html=fixture("springerlink_article_hunnu_gateway.html"),
                    article_url=gateway_article_url(),
                    downloaded_pdf=captured,
                    institutional_route=gateway_route(),
                    expected_title=TITLE,
                    expected_doi=DOI,
                    run_root=run_root,
                )

            manifest_text = (run_root / "DOWNLOAD_MANIFEST.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertEqual(result.status, RunStatus.SUCCESS)
            self.assertEqual(len(result.downloads), 1)
            self.assertEqual(result.records[0].source_page, ARTICLE_URL)
            self.assertTrue(result.records[0].institutional_route_used)
            self.assertTrue(result.records[0].target_doi_matched)
            self.assertTrue(result.records[0].target_identity_confirmed)
            self.assertTrue(result.downloads[0].file_validation_passed)
            self.assertEqual(len(result.downloads[0].sha256), 64)
            self.assertEqual(
                manifest["Downloads"][0]["OriginalURLOrStableIdentifier"],
                DOI,
            )
            self.assertNotIn("fixture-springer-route", manifest_text)
            self.assertNotIn("route_marker", manifest_text)
            self.assertNotIn("SYNTHETIC_ONLY", manifest_text)
            self.assertEqual(scan_text_for_sensitive_leaks(manifest_text), [])

    def test_gateway_finalizer_rejects_unbound_and_arbitrary_hunnu_pages(self) -> None:
        for article_url in (
            f"https://yclib.hunnu.edu.cn/vpn/different-route/article/{DOI}",
            f"https://random.hunnu.edu.cn/vpn/fixture/article/{DOI}",
        ):
            with self.subTest(article_url=article_url), tempfile.TemporaryDirectory(
                prefix="springer-finalizer-untrusted-",
                dir=TEMP_DIR,
            ) as temporary:
                root = Path(temporary)
                captured = write_minimal_pdf_with_text(
                    root / "captured.pdf",
                    f"{TITLE} Ada Researcher DOI: {DOI}",
                )
                run_root = root / "run"
                with self.assertRaises(SourceLayoutChanged):
                    finalize_captured_hunnu_springer_acceptance(
                        request=target_request(),
                        query=TITLE,
                        search_html=search_fixture(),
                        article_html=fixture("springerlink_article_hunnu_gateway.html"),
                        article_url=article_url,
                        downloaded_pdf=captured,
                        institutional_route=gateway_route(),
                        expected_title=TITLE,
                        expected_doi=DOI,
                        run_root=run_root,
                    )
                self.assertFalse((run_root / "downloads" / "archive").exists())

    def test_gateway_finalizer_rejects_wrong_downloaded_pdf_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="springer-finalizer-wrong-pdf-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            captured = write_minimal_pdf_with_text(
                root / "captured.pdf",
                "Different Springer article DOI: 10.1007/s99999-026-99999-9",
            )
            run_root = root / "run"
            with self.assertRaisesRegex(SourceLayoutChanged, "did not match"):
                finalize_captured_hunnu_springer_acceptance(
                    request=target_request(),
                    query=TITLE,
                    search_html=search_fixture(),
                    article_html=fixture("springerlink_article_hunnu_gateway.html"),
                    article_url=gateway_article_url(),
                    downloaded_pdf=captured,
                    institutional_route=gateway_route(),
                    expected_title=TITLE,
                    expected_doi=DOI,
                    run_root=run_root,
                )
            self.assertFalse((run_root / "downloads" / "archive").exists())


class _FakeLiveBrowser:
    def __init__(self, **_: Any) -> None:
        self.page = object()
        self.downloads_dir = TEMP_DIR / "springer-cli-downloads"
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


class _FakeAdapter:
    name = "SpringerLink"

    def __init__(self, browser: Any) -> None:
        self.browser = browser
        self.bound_route: InstitutionalRouteResult | None = None

    def bind_institutional_route(self, route: InstitutionalRouteResult) -> None:
        self.bound_route = route


class _FakeOxfordAdapter(_FakeAdapter):
    name = "OxfordAcademic"


class _FakeResolver:
    def __init__(self, browser: Any) -> None:
        self.browser = browser
        self.resolve_calls = 0

    async def resolve(self, requested_source: str, *, trigger: InstitutionalResolutionTrigger):
        self.resolve_calls += 1
        return gateway_route(source=requested_source)


class _FakeWorkflow:
    instances: list["_FakeWorkflow"] = []

    def __init__(self, adapter: Any, **kwargs: Any) -> None:
        self.adapter = adapter
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    async def run(self, _: Any) -> LiteratureRunResult:
        return LiteratureRunResult(status=RunStatus.SUCCESS, records=[], downloads=[])


def _live_args(command: str, run_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        command=command,
        request_json=None,
        text=None,
        title=TITLE,
        doi=None,
        max_results=1,
        max_downloads=0,
        run_root=run_root,
        profile=Path("C:/fixture/research-profile"),
        chrome=Path("C:/fixture/chrome.exe"),
        headless=False,
    )


class SpringerCLIWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_springer_cli_wires_resolver_without_eager_gateway_navigation(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        _FakeWorkflow.instances.clear()
        resolver = _FakeResolver(object())
        with patch.object(literature_cli, "PlaywrightBrowser", _FakeLiveBrowser), patch.object(
            literature_cli, "SpringerLinkAdapter", _FakeAdapter
        ), patch.object(
            literature_cli, "HUNNUInstitutionalAccessResolver", return_value=resolver
        ), patch.object(literature_cli, "LiteratureAcquisitionWorkflow", _FakeWorkflow):
            code = await literature_cli._run_live(
                _live_args("live-springerlink", TEMP_DIR / "springer-cli-run")
            )
        self.assertEqual(code, 0)
        self.assertEqual(resolver.resolve_calls, 0)
        workflow = _FakeWorkflow.instances[-1]
        self.assertIs(workflow.kwargs["institutional_resolver"], resolver)
        self.assertEqual(
            workflow.kwargs["institutional_trigger"],
            InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
        )
        self.assertIsNone(workflow.adapter.bound_route)

    async def test_oxford_cli_still_resolves_and_binds_before_workflow(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        _FakeWorkflow.instances.clear()
        resolver = _FakeResolver(object())
        with patch.object(literature_cli, "PlaywrightBrowser", _FakeLiveBrowser), patch.object(
            literature_cli, "OxfordAcademicAdapter", _FakeOxfordAdapter
        ), patch.object(
            literature_cli, "HUNNUInstitutionalAccessResolver", return_value=resolver
        ), patch.object(literature_cli, "LiteratureAcquisitionWorkflow", _FakeWorkflow):
            code = await literature_cli._run_live(
                _live_args("live-oxfordacademic", TEMP_DIR / "oxford-cli-run")
            )
        self.assertEqual(code, 0)
        self.assertEqual(resolver.resolve_calls, 1)
        workflow = _FakeWorkflow.instances[-1]
        self.assertIsNotNone(workflow.adapter.bound_route)
        self.assertEqual(
            workflow.kwargs["institutional_trigger"],
            InstitutionalResolutionTrigger.DIRECT_ROUTE_UNAVAILABLE,
        )


if __name__ == "__main__":
    unittest.main()
