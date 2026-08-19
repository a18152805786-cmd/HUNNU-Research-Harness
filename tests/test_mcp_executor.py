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

            with self.assertRaises(ObservationUnavailable):
                await executor.execute(ObserveCommand(include_html=True))

    async def test_text_target_is_resolved_to_short_lived_mcp_ref(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hunnu-v018-mcp-") as temporary:
            client = _FakeMCPClient()
            executor = MCPExecutor(client, downloads_dir=Path(temporary))

            result = await executor.execute(ClickCommand(BrowserTarget(text="Click probe")))
            self.assertEqual(result.action, "click")
            self.assertEqual(client.calls[-2][0], "browser_find")
            self.assertEqual(client.calls[-1][0], "browser_click")
            self.assertEqual(client.calls[-1][1]["target"], "e3")

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
