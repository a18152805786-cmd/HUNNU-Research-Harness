from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from hunnu_harness.browser import (
    BrowserSessionBroker,
    BrowserTarget,
    DownloadArtifact,
    DownloadCommand,
    DownloadFailure,
    MCPConnectionError,
    MCPExecutor,
    MCPUnsupportedCapability,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
    PageAffinityAmbiguous,
    SessionUnavailable,
    StalePageHandle,
    StaleObservationReference,
    validate_browser_command_port,
)
from hunnu_harness.browser.commands import AuthenticatedFetchCommand, BrowserObservation, ClickCommand


def _result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class _FakeMCPClient:
    def __init__(self, downloads_dir: Path | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.downloads_dir = downloads_dir
        self.clicked = False

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(arguments)))
        if tool == "browser_tabs":
            if arguments.get("action") == "list":
                return _result(
                    "### Result\n"
                    "- 0: (current) [Controlled](http://controlled.test/)\n"
                    "- 1: [Second](http://second.test/)"
                )
            return _result("### Result\n- 0: [Controlled](http://controlled.test/)")
        if tool == "browser_navigate":
            return _result(
                "### Page\n- Page URL: "
                f"{arguments['url']}\n- Page Title: Controlled"
            )
        if tool == "browser_evaluate":
            return _result(
                '### Result\n"<html><head><title>Controlled</title></head>'
                '<body><main>controlled</main></body></html>"\n'
                "### Ran Playwright code\n"
            )
        if tool == "browser_snapshot":
            status = "clicked" if self.clicked else "not clicked"
            return _result(
                "### Page\n- Page URL: http://controlled.test/\n"
                "- Page Title: Controlled\n### Snapshot\n"
                "- generic [ref=e1]\n"
                "  - button \"Click probe\" [ref=e3]\n"
                f"  - paragraph [ref=e4]: {status}\n"
                "  - link \"Download probe\" [ref=e5]"
            )
        if tool == "browser_find":
            if arguments.get("text") == "Missing":
                return _result("### Result\nNo matches")
            ref = "e5" if arguments.get("text") == "Download probe" else "e3"
            return _result(f'### Snapshot\n- button "match" [ref={ref}]')
        if tool == "browser_click":
            if arguments.get("target") == "e3":
                self.clicked = True
                return _result("### Page\n- Page URL: http://controlled.test/\n- Page Title: Controlled")
            if arguments.get("target") == "e5":
                return _result(
                    "### Events\n"
                    "- Downloading file probe.txt ...\n"
                    '- Downloaded file probe.txt to "probe.txt"'
                )
            return _result("### Page\n- Page URL: http://controlled.test/\n- Page Title: Controlled")
        raise AssertionError(f"Unexpected MCP tool: {tool} {arguments}")


class _SourceAffinityMCPClient(_FakeMCPClient):
    def __init__(self, pages: list[tuple[str, str]], *, current: int) -> None:
        super().__init__()
        self.pages = list(pages)
        self.current = current

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(arguments)))
        if tool == "browser_tabs":
            if arguments.get("action") == "select":
                self.current = int(arguments["index"])
            rows = [
                f"- {index}: {'(current) ' if index == self.current else ''}[{title}]({url})"
                for index, (title, url) in enumerate(self.pages)
            ]
            return _result("### Result\n" + "\n".join(rows))
        if tool == "browser_snapshot":
            title, url = self.pages[self.current]
            return _result(
                f"### Page\n- Page URL: {url}\n- Page Title: {title}\n"
                "### Snapshot\n- main [ref=e1]"
            )
        return await super().call(tool, arguments)


class MCPExecutorUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_to_mcp_mapping_returns_structured_observation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-") as temporary:
            client = _FakeMCPClient()
            executor = MCPExecutor(client, downloads_dir=Path(temporary))

            result = await executor.execute(NavigateCommand("http://controlled.test/"))
            self.assertIsInstance(result, BrowserObservation)
            self.assertEqual(result.url, "http://controlled.test/")
            self.assertIsNone(result.html)
            self.assertIn("[ref=e3]", result.structured_content)
            self.assertEqual([call[0] for call in client.calls], [
                "browser_tabs",
                "browser_navigate",
                "browser_snapshot",
            ])

            observed = await executor.execute(ObserveCommand(include_html=True))
            self.assertEqual(
                observed.require_html(),
                "<html><head><title>Controlled</title></head>"
                "<body><main>controlled</main></body></html>",
            )
            self.assertTrue(observed.metadata["FullHTML"])
            self.assertEqual(client.calls[-1][0], "browser_evaluate")
            self.assertEqual(
                client.calls[-1][1]["function"],
                "() => document.documentElement ? document.documentElement.outerHTML : ''",
            )

    async def test_text_probe_geometry_preserves_far_offscreen_evidence(self) -> None:
        class _ProbeClient(_FakeMCPClient):
            async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                if tool == "browser_snapshot":
                    self.calls.append((tool, dict(arguments)))
                    return _result(
                        "### Page\n- Page URL: https://controlled.test/results\n"
                        "- Page Title: Controlled\n### Snapshot\n"
                        "- generic [ref=e1] [box=0,0,1200,800]\n"
                        "  - generic [ref=e2] [box=15,-999985,188,18]: dormant marker"
                    )
                return await super().call(tool, arguments)

        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-") as temporary:
            client = _ProbeClient()
            executor = MCPExecutor(client, downloads_dir=Path(temporary))
            observation = await executor.execute(
                ObserveCommand(
                    include_html=False,
                    include_visible_text=False,
                    text_probes=(r"dormant marker",),
                )
            )
            self.assertTrue(client.calls[-1][1]["boxes"])
            self.assertEqual(len(observation.target_observations), 1)
            evidence = observation.target_observations[0]
            self.assertEqual(evidence.bounding_box["y"], -999985.0)
            self.assertIsNone(evidence.playwright_visible)
            self.assertTrue(evidence.inspection_complete)
            self.assertTrue(observation.inspection_complete)

    async def test_text_target_is_resolved_to_short_lived_mcp_ref(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-") as temporary:
            client = _FakeMCPClient()
            executor = MCPExecutor(client, downloads_dir=Path(temporary))

            result = await executor.execute(ClickCommand(BrowserTarget(text="Click probe")))
            self.assertEqual(result.action, "click")
            self.assertEqual(client.calls[-2][0], "browser_find")
            self.assertEqual(client.calls[-1][0], "browser_click")
            self.assertEqual(client.calls[-1][1]["target"], "e3")

    async def test_find_context_selects_matching_leaf_instead_of_ancestor(self) -> None:
        class _PathClient(_FakeMCPClient):
            async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                if tool == "browser_find":
                    self.calls.append((tool, dict(arguments)))
                    return _result(
                        "### Result\n"
                        "- generic [ref=e1]\n"
                        "  - textbox \"Search\" [ref=e2]: Target paper\n"
                        "  - main [ref=e3]\n"
                        "    - table [ref=e4]\n"
                        "      - row [ref=e5]\n"
                        '        - link "Target paper" [ref=e9] [cursor=pointer]\n'
                    )
                return await super().call(tool, arguments)

        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-") as temporary:
            client = _PathClient()
            executor = MCPExecutor(client, downloads_dir=Path(temporary))
            await executor.execute(
                ClickCommand(BrowserTarget(text="Target paper", exact_text=True))
            )
            find_call = next(call for call in client.calls if call[0] == "browser_find")
            self.assertEqual(find_call[1], {"text": "Target paper"})
            self.assertEqual(client.calls[-1][0], "browser_click")
            self.assertEqual(client.calls[-1][1]["target"], "e9")

    async def test_follow_new_page_can_close_a_sole_origin_without_losing_binding(self) -> None:
        class _PopupClient(_FakeMCPClient):
            def __init__(self) -> None:
                super().__init__()
                self.pages = [("Search", "http://controlled.test/search")]
                self.current = 0

            async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                self.calls.append((tool, dict(arguments)))
                if tool == "browser_tabs":
                    action = arguments.get("action")
                    if action == "select":
                        self.current = int(arguments["index"])
                    elif action == "close":
                        closed = int(arguments["index"])
                        self.pages.pop(closed)
                        self.current = min(self.current - (self.current > closed), len(self.pages) - 1)
                    rows = [
                        f"- {index}: {'(current) ' if index == self.current else ''}"
                        f"[{title}]({url})"
                        for index, (title, url) in enumerate(self.pages)
                    ]
                    return _result("### Result\n" + "\n".join(rows))
                if tool == "browser_find":
                    return _result(
                        "### Result\n"
                        "- generic [ref=e1]\n"
                        '  - link "Target paper" [ref=e9]'
                    )
                if tool == "browser_click":
                    self.pages.append(("Detail", "http://controlled.test/detail"))
                    self.current = len(self.pages) - 1
                    return _result(
                        "### Page\n"
                        "- Page URL: http://controlled.test/detail\n"
                        "- Page Title: Detail"
                    )
                if tool == "browser_snapshot":
                    title, url = self.pages[self.current]
                    return _result(
                        f"### Page\n- Page URL: {url}\n- Page Title: {title}\n"
                        "### Snapshot\n- main [ref=e20]"
                    )
                raise AssertionError(f"Unexpected MCP tool: {tool} {arguments}")

        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-") as temporary:
            client = _PopupClient()
            broker = BrowserSessionBroker(
                MCPExecutor(client, downloads_dir=Path(temporary)),
                session_id="follow-popup",
            )
            original = (await broker.enumerate_pages())[0]
            result = await broker.execute(
                ClickCommand(
                    BrowserTarget(text="Target paper", exact_text=True),
                    follow_new_page=True,
                    close_origin_when_sole_page=True,
                )
            )
            self.assertEqual(result.page, original.handle)
            self.assertEqual(result.runtime_tab_index, 0)
            observation = await broker.execute(
                ObserveCommand(include_html=False, include_visible_text=False)
            )
            self.assertEqual(observation.url, "http://controlled.test/detail")
            self.assertEqual(observation.page_inventory[0].url, "http://controlled.test/detail")
            pages = await broker.enumerate_pages()
            self.assertEqual(len(pages), 1)
            self.assertTrue(any(item.active and item.handle == original.handle for item in pages))
            self.assertTrue(any(call[1].get("action") == "close" for call in client.calls))

    async def test_navigation_refreshes_broker_inventory_before_identity_checks(self) -> None:
        class _NavigationClient(_FakeMCPClient):
            def __init__(self) -> None:
                super().__init__()
                self.url = "http://controlled.test/origin"

            async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                self.calls.append((tool, dict(arguments)))
                if tool == "browser_tabs":
                    return _result(
                        "### Result\n"
                        f"- 0: (current) [Controlled]({self.url})"
                    )
                if tool == "browser_navigate":
                    self.url = str(arguments["url"])
                    return _result(
                        "### Page\n"
                        f"- Page URL: {self.url}\n- Page Title: Controlled"
                    )
                if tool == "browser_snapshot":
                    return _result(
                        "### Page\n"
                        f"- Page URL: {self.url}\n- Page Title: Controlled\n"
                        "### Snapshot\n- main [ref=e20]"
                    )
                raise AssertionError(f"Unexpected MCP tool: {tool} {arguments}")

        with tempfile.TemporaryDirectory(prefix="hunnu-v018-broker-nav-") as temporary:
            client = _NavigationClient()
            broker = BrowserSessionBroker(
                MCPExecutor(client, downloads_dir=Path(temporary)),
                session_id="navigate-refresh",
            )
            original = (await broker.enumerate_pages())[0]
            navigated = await broker.execute(
                NavigateCommand("http://controlled.test/search")
            )
            self.assertEqual(navigated.page, original.handle)
            self.assertEqual(navigated.url, "http://controlled.test/search")
            self.assertEqual(
                navigated.page_inventory[0].url,
                "http://controlled.test/search",
            )
            observed = await broker.execute(
                ObserveCommand(include_html=False, include_visible_text=False)
            )
            self.assertEqual(observed.url, "http://controlled.test/search")
            self.assertEqual(
                observed.page_inventory[0].url,
                "http://controlled.test/search",
            )

    async def test_download_path_is_materialized_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-") as temporary:
            root = Path(temporary)
            payload = b"mcp-controlled-artifact\n"
            (root / "probe.txt").write_bytes(payload)
            client = _FakeMCPClient(root)
            executor = MCPExecutor(client, downloads_dir=root)

            artifact = await executor.execute(
                DownloadCommand(
                    target=BrowserTarget(text="Download probe"),
                    suggested_filename="downloaded.txt",
                )
            )
            self.assertIsInstance(artifact, DownloadArtifact)
            self.assertEqual(artifact.local_path, (root / "probe.txt").resolve())
            self.assertEqual(artifact.size, len(payload))
            self.assertEqual(artifact.sha256, hashlib.sha256(payload).hexdigest())
            self.assertTrue(artifact.artifact_id.startswith("artifact-"))
            self.assertEqual(artifact.metadata["CompletionSignal"], "downloaded-event+filesystem")

    async def test_pending_download_event_is_bounded_to_one_changed_named_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-pending-") as temporary:
            root = Path(temporary)
            payload = b"%PDF-1.7\npending-download-artifact\n"

            class _PendingDownloadClient(_FakeMCPClient):
                async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if tool == "browser_click" and arguments.get("target") == "e5":
                        self.calls.append((tool, dict(arguments)))

                        async def complete() -> None:
                            await asyncio.sleep(0.15)
                            (root / "Target-paper.pdf").write_bytes(payload)

                        asyncio.create_task(complete())
                        return _result("### Events\n- Downloading file Target_paper.pdf ...")
                    return await super().call(tool, arguments)

            executor = MCPExecutor(
                _PendingDownloadClient(root),
                downloads_dir=root,
                max_download_wait_seconds=2.0,
            )
            artifact = await executor.execute(
                DownloadCommand(
                    target=BrowserTarget(text="Download probe", exact_text=True),
                    suggested_filename="target.pdf",
                )
            )
            self.assertEqual(artifact.local_path, (root / "Target-paper.pdf").resolve())
            self.assertEqual(artifact.sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(
                artifact.metadata["CompletionSignal"],
                "downloading-event+bounded-directory-watch",
            )

    async def test_filesystem_fallback_captures_completed_pdf_without_mcp_event(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v021-mcp-fallback-pdf-") as temporary:
            root = Path(temporary)
            payload = b"%PDF-1.7\ncontrolled fallback pdf\n"

            class _FilesystemOnlyClient(_FakeMCPClient):
                async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if tool == "browser_click" and arguments.get("target") == "e5":
                        self.calls.append((tool, dict(arguments)))

                        async def complete() -> None:
                            await asyncio.sleep(0.15)
                            (root / "Target-paper.pdf").write_bytes(payload)

                        asyncio.create_task(complete())
                        return _result("### Ran Playwright code\n")
                    return await super().call(tool, arguments)

            artifact = await MCPExecutor(
                _FilesystemOnlyClient(root),
                downloads_dir=root,
                max_download_wait_seconds=2.0,
            ).execute(
                DownloadCommand(
                    target=BrowserTarget(text="Download probe", exact_text=True),
                    suggested_filename="target.pdf",
                )
            )
            self.assertEqual(artifact.local_path, (root / "Target-paper.pdf").resolve())
            self.assertEqual(artifact.metadata["CompletionSignal"], "filesystem-watch-after-authorized-click")
            self.assertTrue(artifact.metadata["FilesystemFallbackUsed"])
            self.assertEqual(artifact.metadata["ArtifactCorrelationConfidence"], "HIGH")
            self.assertEqual(artifact.metadata["DownloadedArtifactType"], "PDF")
            self.assertEqual(artifact.metadata["ArtifactValidationLevel"], "header-only")

    async def test_filesystem_fallback_honors_command_window_for_late_browser_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v021-mcp-fallback-late-") as temporary:
            root = Path(temporary)
            payload = b"%PDF-1.7\nlate browser artifact\n"

            class _LateFilesystemClient(_FakeMCPClient):
                async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if tool == "browser_click" and arguments.get("target") == "e5":
                        self.calls.append((tool, dict(arguments)))

                        async def complete() -> None:
                            await asyncio.sleep(0.25)
                            (root / "Late-paper.pdf").write_bytes(payload)

                        asyncio.create_task(complete())
                        return _result("### Ran Playwright code\n")
                    return await super().call(tool, arguments)

            artifact = await MCPExecutor(
                _LateFilesystemClient(root),
                downloads_dir=root,
                max_download_wait_seconds=0.1,
            ).execute(
                DownloadCommand(
                    target=BrowserTarget(text="Download probe", exact_text=True),
                    suggested_filename="late.pdf",
                    timeout_ms=1000,
                )
            )
            self.assertEqual(artifact.local_path, (root / "Late-paper.pdf").resolve())
            self.assertEqual(artifact.metadata["DownloadedArtifactType"], "PDF")

    async def test_filesystem_fallback_waits_for_crdownload_to_become_pdf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v021-mcp-fallback-temp-") as temporary:
            root = Path(temporary)
            payload = b"%PDF-1.7\nrenamed after browser completion\n"

            class _RenamingClient(_FakeMCPClient):
                async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if tool == "browser_click" and arguments.get("target") == "e5":
                        self.calls.append((tool, dict(arguments)))

                        async def complete() -> None:
                            partial = root / "Target-paper.pdf.crdownload"
                            partial.write_bytes(payload[:8])
                            await asyncio.sleep(0.15)
                            partial.write_bytes(payload)
                            await asyncio.sleep(0.15)
                            partial.replace(root / "Target-paper.pdf")

                        asyncio.create_task(complete())
                        return _result("### Ran Playwright code\n")
                    return await super().call(tool, arguments)

            artifact = await MCPExecutor(
                _RenamingClient(root),
                downloads_dir=root,
                max_download_wait_seconds=2.0,
            ).execute(
                DownloadCommand(
                    target=BrowserTarget(text="Download probe", exact_text=True),
                    suggested_filename="target.pdf",
                )
            )
            self.assertEqual(artifact.local_path, (root / "Target-paper.pdf").resolve())
            self.assertEqual(artifact.metadata["DownloadedArtifactType"], "PDF")

    async def test_filesystem_fallback_classifies_caj_without_calling_it_pdf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v021-mcp-fallback-caj-") as temporary:
            root = Path(temporary)
            payload = b"KDH 2.00 Copyright(C) 2000 CAJCD\ncontrolled caj\n"

            class _CAJClient(_FakeMCPClient):
                async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if tool == "browser_click" and arguments.get("target") == "e5":
                        self.calls.append((tool, dict(arguments)))
                        (root / "Target-paper.caj").write_bytes(payload)
                        return _result("### Ran Playwright code\n")
                    return await super().call(tool, arguments)

            artifact = await MCPExecutor(_CAJClient(root), downloads_dir=root).execute(
                DownloadCommand(
                    target=BrowserTarget(text="Download probe", exact_text=True),
                    suggested_filename="target.caj",
                )
            )
            self.assertEqual(artifact.metadata["DownloadedArtifactType"], "CAJ")
            self.assertNotEqual(artifact.metadata["DownloadedArtifactType"], "PDF")
            self.assertEqual(artifact.metadata["ArtifactValidationLevel"], "header-only")

    async def test_filesystem_fallback_rejects_html_disguised_as_pdf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v021-mcp-fallback-html-") as temporary:
            root = Path(temporary)

            class _HTMLClient(_FakeMCPClient):
                async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if tool == "browser_click" and arguments.get("target") == "e5":
                        self.calls.append((tool, dict(arguments)))
                        (root / "Target-paper.pdf").write_bytes(
                            b"<!doctype html><html><body>login page</body></html>"
                        )
                        return _result("### Ran Playwright code\n")
                    return await super().call(tool, arguments)

            with self.assertRaisesRegex(DownloadFailure, "HTML"):
                await MCPExecutor(_HTMLClient(root), downloads_dir=root).execute(
                    DownloadCommand(
                        target=BrowserTarget(text="Download probe", exact_text=True),
                        suggested_filename="target.pdf",
                    )
                )

    async def test_filesystem_fallback_does_not_reuse_old_pdf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v021-mcp-fallback-old-") as temporary:
            root = Path(temporary)
            (root / "old.pdf").write_bytes(b"%PDF-1.7\nold artifact\n")

            class _NoEventClient(_FakeMCPClient):
                async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if tool == "browser_click" and arguments.get("target") == "e5":
                        self.calls.append((tool, dict(arguments)))
                        return _result("### Ran Playwright code\n")
                    return await super().call(tool, arguments)

            with self.assertRaises(DownloadFailure):
                await MCPExecutor(_NoEventClient(root), downloads_dir=root).execute(
                    DownloadCommand(
                        target=BrowserTarget(text="Download probe", exact_text=True),
                        suggested_filename="target.pdf",
                        timeout_ms=100,
                    )
                )

    async def test_filesystem_fallback_rejects_ambiguous_new_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v021-mcp-fallback-ambiguous-") as temporary:
            root = Path(temporary)

            class _AmbiguousClient(_FakeMCPClient):
                async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if tool == "browser_click" and arguments.get("target") == "e5":
                        self.calls.append((tool, dict(arguments)))
                        (root / "Target-one.pdf").write_bytes(b"%PDF-1.7\none\n")
                        (root / "Target-two.pdf").write_bytes(b"%PDF-1.7\ntwo\n")
                        return _result("### Ran Playwright code\n")
                    return await super().call(tool, arguments)

            with self.assertRaisesRegex(DownloadFailure, "more than one"):
                await MCPExecutor(_AmbiguousClient(root), downloads_dir=root).execute(
                    DownloadCommand(
                        target=BrowserTarget(text="Download probe", exact_text=True),
                        suggested_filename="target.pdf",
                    )
                )

    async def test_missing_download_artifact_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-") as temporary:
            executor = MCPExecutor(_FakeMCPClient(), downloads_dir=Path(temporary))
            with self.assertRaises(DownloadFailure):
                await executor.execute(
                    DownloadCommand(
                        target=BrowserTarget(text="Download probe"),
                        suggested_filename="missing.txt",
                        timeout_ms=100,
                    )
                )

    async def test_unsupported_authenticated_fetch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-") as temporary:
            executor = MCPExecutor(_FakeMCPClient(), downloads_dir=Path(temporary))
            with self.assertRaises(MCPUnsupportedCapability):
                await executor.execute(
                    AuthenticatedFetchCommand(
                        "https://controlled.test/private", "private.bin"
                    )
                )

    async def test_stale_snapshot_ref_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-") as temporary:
            executor = MCPExecutor(_FakeMCPClient(), downloads_dir=Path(temporary))
            observation = await executor.execute(ObserveCommand(include_html=False, include_visible_text=False))
            ref = BrowserTarget(
                ref="e3",
                observation_id=observation.metadata["ObservationId"],
            )
            await executor.execute(ObserveCommand(include_html=False, include_visible_text=False))
            with self.assertRaises(StaleObservationReference):
                await executor.execute(ClickCommand(ref))


class BrowserSessionBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def _affinity_broker(
        self,
        temporary: str,
        pages: list[tuple[str, str]],
        *,
        current: int,
    ) -> tuple[BrowserSessionBroker, _SourceAffinityMCPClient]:
        client = _SourceAffinityMCPClient(pages, current=current)
        broker = BrowserSessionBroker(
            MCPExecutor(client, downloads_dir=Path(temporary)),
            session_id="source-affinity-session",
        )
        await broker.enumerate_pages()
        return broker, client

    async def test_source_affinity_selects_cnki_when_springer_is_active(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-source-affinity-") as temporary:
            broker, client = await self._affinity_broker(
                temporary,
                [
                    ("Springer", "https://link.springer.com/search?query=test"),
                    ("CNKI", "https://kns.cnki.net/kns8s/defaultresult/index"),
                ],
                current=0,
            )

            observation = await broker.select_source_page(
                source="CNKI",
                source_origin="https://kns.cnki.net",
            )

            self.assertIsNotNone(observation)
            self.assertEqual(observation.url, "https://kns.cnki.net/kns8s/defaultresult/index")
            self.assertIn(("browser_tabs", {"action": "select", "index": 1}), client.calls)

    async def test_source_affinity_selects_springer_when_cnki_is_active(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-source-affinity-") as temporary:
            broker, _client = await self._affinity_broker(
                temporary,
                [
                    ("CNKI", "https://kns.cnki.net/kns8s/defaultresult/index"),
                    ("Springer", "https://link.springer.com/search?query=test"),
                ],
                current=0,
            )

            observation = await broker.select_source_page(
                source="SpringerLink",
                source_origin="https://link.springer.com",
            )

            self.assertIsNotNone(observation)
            self.assertEqual(observation.url, "https://link.springer.com/search?query=test")

    async def test_source_affinity_selects_unique_match_from_unrelated_active_page(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-source-affinity-") as temporary:
            broker, _client = await self._affinity_broker(
                temporary,
                [
                    ("Unrelated", "https://example.test/"),
                    ("Target", "https://target.test/business"),
                ],
                current=0,
            )

            observation = await broker.select_source_page(
                source="TargetSource",
                source_origin="https://target.test",
            )

            self.assertIsNotNone(observation)
            self.assertEqual(observation.url, "https://target.test/business")

    async def test_source_affinity_no_match_preserves_safe_adapter_navigation_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-source-affinity-") as temporary:
            broker, client = await self._affinity_broker(
                temporary,
                [("Unrelated", "https://example.test/")],
                current=0,
            )
            before = broker.page_handle

            observation = await broker.select_source_page(
                source="MissingSource",
                source_origin="https://missing.test",
            )

            self.assertIsNone(observation)
            self.assertEqual(broker.page_handle, before)
            self.assertFalse(
                any(
                    tool == "browser_tabs" and arguments.get("action") == "select"
                    for tool, arguments in client.calls
                )
            )

    async def test_source_affinity_multiple_inactive_matches_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-source-affinity-") as temporary:
            broker, client = await self._affinity_broker(
                temporary,
                [
                    ("Unrelated", "https://example.test/"),
                    ("Target one", "https://target.test/one"),
                    ("Target two", "https://target.test/two"),
                ],
                current=0,
            )

            with self.assertRaises(PageAffinityAmbiguous):
                await broker.select_source_page(
                    source="TargetSource",
                    source_origin="https://target.test",
                )
            self.assertFalse(
                any(
                    tool == "browser_tabs" and arguments.get("action") == "select"
                    for tool, arguments in client.calls
                )
            )

    async def test_source_affinity_multiple_matches_reuse_active_compatible_page(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-source-affinity-") as temporary:
            broker, _client = await self._affinity_broker(
                temporary,
                [
                    ("Target current", "https://target.test/current"),
                    ("Target other", "https://target.test/other"),
                ],
                current=0,
            )

            observation = await broker.select_source_page(
                source="TargetSource",
                source_origin="https://target.test",
            )

            self.assertIsNotNone(observation)
            self.assertEqual(observation.url, "https://target.test/current")

    async def test_source_affinity_reuses_known_binding_after_switching_sources(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-source-affinity-") as temporary:
            broker, client = await self._affinity_broker(
                temporary,
                [
                    ("Target owned", "https://target.test/owned"),
                    ("Target other", "https://target.test/other"),
                    ("Other source", "https://other.test/business"),
                ],
                current=0,
            )
            owned = await broker.select_source_page(
                source="TargetSource",
                source_origin="https://target.test",
            )
            pages = await broker.enumerate_pages()
            other = next(page for page in pages if page.summary.url.startswith("https://other.test"))
            await broker.select_page(other.handle)

            rebound = await broker.select_source_page(
                source="TargetSource",
                source_origin="https://target.test",
            )

            self.assertIsNotNone(owned)
            self.assertIsNotNone(rebound)
            self.assertEqual(rebound.url, "https://target.test/owned")
            self.assertIn(("browser_tabs", {"action": "select", "index": 0}), client.calls)

    async def test_broker_exposes_command_port_and_logical_page_handles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v018-broker-") as temporary:
            client = _FakeMCPClient()
            executor = MCPExecutor(client, downloads_dir=Path(temporary))
            broker = BrowserSessionBroker(executor, session_id="test-mcp-session")
            validate_browser_command_port(broker)

            pages = await broker.enumerate_pages()
            self.assertEqual(len(pages), 2)
            self.assertTrue(pages[0].active)
            self.assertNotEqual(pages[0].handle.value, pages[1].handle.value)
            self.assertEqual(pages[0].runtime_tab_index, 0)
            self.assertEqual(pages[1].runtime_tab_index, 1)

            observation = await broker.execute(
                ObserveCommand(include_html=False, include_visible_text=False)
            )
            self.assertEqual(observation.session.value, "test-mcp-session")
            self.assertEqual(observation.page, pages[0].handle)
            self.assertEqual(observation.generation, 0)
            selected = await broker.select_page(pages[1].handle)
            self.assertEqual(selected.page, pages[1].handle)
            self.assertTrue(broker.page_handle == pages[1].handle)

    async def test_invalidation_advances_generation_and_rejects_stale_handle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v018-broker-") as temporary:
            executor = MCPExecutor(_FakeMCPClient(), downloads_dir=Path(temporary))
            broker = BrowserSessionBroker(executor)
            await broker.enumerate_pages()
            old_handle = broker.page_handle
            broker.invalidate("test disconnect")
            self.assertEqual(broker.generation, 1)
            with self.assertRaises(StalePageHandle):
                broker.validate_page_handle(old_handle)
            with self.assertRaises(SessionUnavailable):
                await broker.execute(ObserveCommand(include_html=False, include_visible_text=False))

            rebound = await broker.rebind()
            self.assertTrue(rebound)
            self.assertEqual(broker.generation, 1)
            self.assertNotEqual(rebound[0].handle, old_handle)
            observation = await broker.execute(
                ObserveCommand(include_html=False, include_visible_text=False)
            )
            self.assertEqual(observation.generation, 1)

    async def test_connection_failure_invalidates_lazy_mapping(self) -> None:
        class _DisconnectedClient:
            async def call(self, _tool: str, _arguments: dict[str, Any]) -> Any:
                raise OSError("simulated MCP disconnect")

        with tempfile.TemporaryDirectory(prefix="hunnu-v018-broker-") as temporary:
            executor = MCPExecutor(_DisconnectedClient(), downloads_dir=Path(temporary))
            broker = BrowserSessionBroker(executor)
            with self.assertRaises(MCPConnectionError):
                await broker.execute(ObserveCommand(include_html=False, include_visible_text=False))
            self.assertEqual(broker.state, "uncertain")
            self.assertEqual(broker.generation, 1)

    def test_mcp_layers_are_source_neutral_and_do_not_add_object_graph(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "hunnu_harness" / "browser"
        source = (root / "mcp_executor.py").read_text(encoding="utf-8")
        source += (root / "session_broker.py").read_text(encoding="utf-8")
        for publisher in ("CNKI", "Springer", "ScienceDirect", "Oxford"):
            self.assertNotIn(publisher, source)
        for forbidden in ("MCPPage", "MCPLocator", "MCPContext", "MCPDownload", "MCPResponse"):
            self.assertNotIn(forbidden, source)

    def test_agent_entrypoint_has_no_raw_mcp_fallback(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hunnu_harness"
            / "agent_entrypoint.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("browser_navigate", source)
        self.assertNotIn("browser_click", source)
        self.assertNotIn("browser_snapshot", source)


if __name__ == "__main__":
    unittest.main()
