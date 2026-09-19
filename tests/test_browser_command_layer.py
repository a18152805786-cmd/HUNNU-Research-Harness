from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from hunnu_harness.browser.commands import (
    AuthenticatedFetchCommand,
    AuthenticatedFetchResult,
    BrowserObservation,
    BrowserPageSummary,
    BrowserTarget,
    BrowserTargetObservation,
    ClickCommand,
    DownloadArtifact,
    DownloadCommand,
    InvalidTarget,
    NavigateCommand,
    ObserveCommand,
    PageHandle,
    SessionHandle,
    UnsupportedCommand,
)
from hunnu_harness.browser.local_executor import LocalPlaywrightExecutor
from hunnu_harness.literature.adapters import (
    CNKIAdapter,
    OxfordAcademicAdapter,
    ScienceDirectAdapter,
    SpringerLinkAdapter,
)
from hunnu_harness.literature.models import LiteratureRecord, LiteratureSearchRequest


class _Download:
    suggested_filename = "fixture.pdf"

    async def save_as(self, destination: str) -> None:
        Path(destination).write_bytes(b"%PDF-1.4\ncommand-layer-fixture\n")


class _DownloadInfo:
    def __init__(self, page: "_Page") -> None:
        self.page = page

    @property
    def value(self) -> _Download:
        return self.page.download

    async def __aenter__(self) -> "_DownloadInfo":
        self.page.download_info = self
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Locator:
    def __init__(self, page: "_Page") -> None:
        self.page = page

    def filter(self, **_kwargs: object) -> "_Locator":
        return self

    @property
    def first(self) -> "_Locator":
        return self

    async def click(self) -> None:
        self.page.clicked = True


class _Response:
    ok = True
    status = 200
    headers = {"content-type": "application/pdf"}

    async def body(self) -> bytes:
        return b"%PDF-1.4\nauthenticated-fetch\n"


class _Request:
    async def get(self, _url: str, *, timeout: int) -> _Response:
        assert timeout > 0
        return _Response()


class _Context:
    def __init__(self) -> None:
        self.pages: list[_Page] = []
        self.request = _Request()


class _Page:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.url = "about:blank"
        self.html = "<html><title>fixture</title><body>fixture</body></html>"
        self.clicked = False
        self.download = _Download()
        self.download_info: _DownloadInfo | None = None
        context.pages.append(self)

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url

    async def title(self) -> str:
        return "fixture"

    async def content(self) -> str:
        return self.html

    def locator(self, _selector: str) -> _Locator:
        return _Locator(self)

    def expect_download(self, *, timeout: int) -> _DownloadInfo:
        assert timeout > 0
        return _DownloadInfo(self)


class LocalExecutorContractTests(unittest.IsolatedAsyncioTestCase):
    def test_command_models_validate_and_serialize_without_playwright_objects(self) -> None:
        with self.assertRaises(InvalidTarget):
            BrowserTarget()
        with self.assertRaises(ValueError):
            NavigateCommand("ftp://example.test/file")
        with self.assertRaises(InvalidTarget):
            BrowserTarget(css="a", text_regex="[")

        command = DownloadCommand(
            target=BrowserTarget(css="a", text_regex=r"download"),
            suggested_filename="nested/path/paper.pdf",
        )
        payload = command.as_dict()
        self.assertEqual(payload["Kind"], "Download")
        self.assertEqual(payload["SuggestedFilename"], "paper.pdf")
        self.assertEqual(payload["Target"]["TextRegex"], r"download")

    async def test_navigate_observe_click_download_and_authenticated_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = _Context()
            page = _Page(context)
            executor = LocalPlaywrightExecutor(page=page, context=context, downloads_dir=root)

            navigated = await executor.execute(NavigateCommand("https://example.test/fixture"))
            self.assertIsInstance(navigated, BrowserObservation)
            self.assertEqual(navigated.url, "https://example.test/fixture")

            observed = await executor.execute(ObserveCommand())
            self.assertEqual(observed.require_html(), page.html)

            clicked = await executor.execute(ClickCommand(BrowserTarget(css="a", text="Download")))
            self.assertEqual(clicked.action, "click")
            self.assertTrue(page.clicked)

            artifact = await executor.execute(
                DownloadCommand(
                    target=BrowserTarget(css="a", text_regex=r"download"),
                    suggested_filename="paper.pdf",
                )
            )
            self.assertTrue(artifact.local_path.is_file())
            self.assertEqual(artifact.sha256, hashlib.sha256(artifact.local_path.read_bytes()).hexdigest())
            self.assertTrue(artifact.artifact_id.startswith("artifact-"))

            response = await executor.execute(
                AuthenticatedFetchCommand(
                    url="https://example.test/paper.pdf",
                    suggested_filename="authenticated.pdf",
                )
            )
            self.assertIsInstance(response, AuthenticatedFetchResult)
            self.assertTrue(response.ok)
            self.assertIsNotNone(response.artifact)
            self.assertTrue(response.artifact.local_path.is_file())

    async def test_unsupported_command_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = _Context()
            executor = LocalPlaywrightExecutor(
                page=_Page(context),
                context=context,
                downloads_dir=Path(temporary),
            )

            class _Unknown:
                pass

            with self.assertRaises(UnsupportedCommand):
                await executor.execute(_Unknown())  # type: ignore[arg-type]


class _RecordingPort:
    def __init__(self) -> None:
        self.downloads_dir = Path(tempfile.gettempdir()) / "hunnu-v0217-command-tests"
        self.session = SessionHandle()
        self.page_handle = PageHandle()
        self.commands: list[object] = []
        self.current_url = "about:blank"
        self.title = "CNKI"

    async def execute(self, command: object) -> Any:
        self.commands.append(command)
        if isinstance(command, NavigateCommand):
            self.current_url = command.url
            return BrowserObservation(
                session=SessionHandle(),
                page=PageHandle(),
                generation=len(self.commands),
                url=self.current_url,
                title=self.title,
            )
        if isinstance(command, ObserveCommand):
            if "cnki.net" in self.current_url:
                body = "未找到相关结果"
            elif "sciencedirect.com" in self.current_url:
                # An explicit terminal state, as for CNKI.  ScienceDirect no longer
                # reports a page that never decided as an empty search, so the
                # stub has to be a page a real search can end on.
                body = "No results found"
            else:
                body = "中文文献"
            return BrowserObservation(
                session=SessionHandle(),
                page=PageHandle(),
                generation=len(self.commands),
                url=self.current_url,
                title=self.title,
                html=f"<html><body>{body}</body></html>",
                page_inventory=(BrowserPageSummary(0, self.title, self.current_url),),
                target_observations=tuple(
                    BrowserTargetObservation(marker=probe, playwright_visible=False)
                    for probe in command.text_probes
                ),
            )
        raise AssertionError(f"Unexpected command in adapter search probe: {command!r}")


class AdapterCommandMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_four_adapters_enter_through_command_port(self) -> None:
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "command migration",
                "MaxSearchResults": 1,
                "MaxResultsPerSource": 1,
                "MaxDownloads": 0,
                "MaxDownloadsPerRun": 0,
            }
        )
        adapters = (
            (CNKIAdapter, "CNKI", "CNKI"),
            (SpringerLinkAdapter, "SpringerLink", "Springer"),
            (ScienceDirectAdapter, "ScienceDirect", "ScienceDirect"),
            (OxfordAcademicAdapter, "OxfordAcademic", "Oxford"),
        )
        for adapter_type, source, title in adapters:
            with self.subTest(source=source):
                port = _RecordingPort()
                port.title = title
                adapter = adapter_type(port)
                await adapter.search("command migration", request)
                self.assertIsInstance(port.commands[0], NavigateCommand)
                self.assertIsInstance(port.commands[1], ObserveCommand)
                self.assertTrue(all(not isinstance(item, str) for item in port.commands))

    async def test_command_failure_does_not_fall_back_to_raw_page(self) -> None:
        class _FailingPort:
            downloads_dir = Path(tempfile.gettempdir())
            session = SessionHandle()
            page_handle = PageHandle()

            async def execute(self, _command: object) -> Any:
                raise UnsupportedCommand("test transport failure")

        adapter = ScienceDirectAdapter(_FailingPort())
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "fail closed",
                "MaxSearchResults": 1,
                "MaxResultsPerSource": 1,
                "MaxDownloads": 0,
                "MaxDownloadsPerRun": 0,
            }
        )
        with self.assertRaises(UnsupportedCommand):
            await adapter.search("fail closed", request)

    def test_source_adapters_do_not_reference_playwright_object_graph(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "hunnu_harness" / "literature" / "adapters"
        forbidden = (
            'getattr(self.browser, "page"',
            "self.browser.page",
            'getattr(self.browser, "context"',
            "self.browser.context",
            ".locator(",
            ".expect_download",
            ".request.get",
            ".save_as(",
        )
        for name in ("cnki.py", "springerlink.py", "sciencedirect.py", "oxfordacademic.py"):
            source = (root / name).read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(file=name, token=token):
                    self.assertNotIn(token, source)

    def test_executor_has_no_publisher_specific_branches(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hunnu_harness"
            / "browser"
            / "local_executor.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("CNKI", source)
        self.assertNotIn("Springer", source)
        self.assertNotIn("ScienceDirect", source)
        self.assertNotIn("Oxford", source)

    def test_institutional_resolver_uses_command_observations(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hunnu_harness"
            / "literature"
            / "institutional.py"
        ).read_text(encoding="utf-8")
        self.assertIn("NavigateCommand", source)
        self.assertIn("ObserveCommand", source)
        self.assertIn("ensure_browser_command_port", source)
        self.assertNotIn('getattr(self.browser, "page"', source)
        self.assertNotIn("page.content()", source)


if __name__ == "__main__":
    unittest.main()
