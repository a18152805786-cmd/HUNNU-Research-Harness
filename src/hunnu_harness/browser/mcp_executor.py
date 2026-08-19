"""Command-oriented executor for an Agent-mediated Playwright MCP session.

The executor deliberately speaks only the MCP tool-call protocol.  It does
not launch a browser, attach to a profile, or manufacture Python
``Page``/``Locator``/``Download`` objects.  A host supplies an
``MCPToolClient``; the current Codex integration can mediate that client while
the MCP server remains the sole controller of Research Chrome.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .commands import (
    AuthenticatedFetchCommand,
    BrowserActionResult,
    BrowserCommand,
    BrowserCommandError,
    BrowserCommandResult,
    BrowserObservation,
    BrowserPageSummary,
    BrowserTarget,
    ClickCommand,
    DownloadArtifact,
    DownloadCommand,
    DownloadFailure,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
    PageHandle,
    SessionHandle,
    UnsupportedCommand,
)


MCP_VERSION_BASELINE = "0.0.79"


MCP_TOOL_ALLOWLIST = frozenset(
    {
        "browser_click",
        "browser_find",
        "browser_navigate",
        "browser_snapshot",
        "browser_tabs",
    }
)


class MCPExecutorError(BrowserCommandError):
    """Base class for errors at the MCP tool-call boundary."""


class MCPConnectionError(MCPExecutorError):
    """The host could not complete an MCP tool call."""


class MCPProtocolError(MCPExecutorError):
    """The MCP result was malformed or reported an execution error."""


class MCPUnsupportedCapability(UnsupportedCommand, MCPExecutorError):
    """The current MCP tool surface cannot represent a requested command."""


class StaleObservationReference(MCPExecutorError):
    """An accessibility-snapshot ref was used outside its valid scope."""


@runtime_checkable
class MCPToolClient(Protocol):
    """Small host boundary for calling one MCP tool and receiving its result."""

    async def call(self, tool: str, arguments: Mapping[str, Any]) -> Any: ...


class CallableMCPToolClient:
    """Adapt an async or sync host callback to :class:`MCPToolClient`."""

    def __init__(self, invoker: Callable[[str, Mapping[str, Any]], Any]) -> None:
        if not callable(invoker):
            raise TypeError("MCP tool invoker must be callable")
        self._invoker = invoker

    async def call(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        value = self._invoker(tool, arguments)
        return await value if inspect.isawaitable(value) else value


class JsonLineMCPToolClient:
    """A narrow Agent/MCP handoff for hosts that expose JSON lines.

    This is not a second MCP server and does not own a browser.  It emits an
    allow-listed request and waits for the host to return the corresponding
    MCP result.  It is useful for the controlled Codex runtime probe and for a
    future application-level continuation bridge.
    """

    def __init__(
        self,
        *,
        reader: Any = None,
        writer: Any = None,
        allowed_tools: Iterable[str] = MCP_TOOL_ALLOWLIST,
    ) -> None:
        self._reader = reader if reader is not None else sys.stdin
        self._writer = writer if writer is not None else sys.stdout
        self._allowed_tools = frozenset(str(item) for item in allowed_tools)
        if not self._allowed_tools.issubset(MCP_TOOL_ALLOWLIST):
            raise ValueError("JSON-line MCP client may only allow known Playwright MCP tools")
        self._lock = asyncio.Lock()

    async def call(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        if tool not in self._allowed_tools:
            raise MCPUnsupportedCapability(f"MCP tool is not allow-listed: {tool}")
        request_id = uuid.uuid4().hex
        request = {"RequestId": request_id, "Tool": tool, "Arguments": dict(arguments)}
        async with self._lock:
            self._writer.write(json.dumps(request, ensure_ascii=False) + "\n")
            flush = getattr(self._writer, "flush", None)
            if callable(flush):
                flush()
            raw = await asyncio.to_thread(self._reader.readline)
        if not raw:
            raise MCPConnectionError("MCP host closed the JSON-line tool channel")
        try:
            response = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MCPProtocolError("MCP host returned invalid JSON") from exc
        if not isinstance(response, Mapping):
            raise MCPProtocolError("MCP host response must be a JSON object")
        returned_id = response.get("RequestId")
        if returned_id is not None and returned_id != request_id:
            raise MCPProtocolError("MCP host response does not match the pending request")
        if "Result" in response:
            return response["Result"]
        return response


@dataclass(frozen=True)
class _SnapshotBinding:
    observation_id: str
    session: SessionHandle
    page: PageHandle
    generation: int
    refs: frozenset[str]


@dataclass(frozen=True)
class _MCPToolReply:
    raw: Any
    text: str


class MCPExecutor:
    """Translate Harness commands into source-neutral Playwright MCP calls."""

    def __init__(
        self,
        client: MCPToolClient,
        *,
        downloads_dir: Path,
        mcp_path_base: Path | None = None,
        artifact_roots: Iterable[Path] | None = None,
        session: SessionHandle | None = None,
        page_handle: PageHandle | None = None,
        max_download_wait_seconds: float = 8.0,
    ) -> None:
        if not isinstance(client, MCPToolClient):
            raise TypeError("MCPExecutor requires an MCPToolClient")
        if max_download_wait_seconds <= 0:
            raise ValueError("max_download_wait_seconds must be positive")
        self.client = client
        self.downloads_dir = Path(downloads_dir).expanduser().resolve()
        self.mcp_path_base = Path(mcp_path_base).expanduser().resolve() if mcp_path_base else None
        roots = tuple(Path(root).expanduser().resolve() for root in (artifact_roots or (self.downloads_dir,)))
        if not roots:
            raise ValueError("MCPExecutor requires at least one artifact root")
        self._artifact_roots = roots
        self.session = session or SessionHandle(f"mcp-{uuid.uuid4().hex[:12]}")
        self.page_handle = page_handle or PageHandle(
            "mcp-page-0",
            session=self.session,
            generation=self.session.generation,
        )
        self._max_download_wait_seconds = float(max_download_wait_seconds)
        self._last_snapshot: _SnapshotBinding | None = None
        self._current_page_index = 0
        self._last_page_url = "about:blank"
        self._last_page_title = ""

    @property
    def current_page_index(self) -> int:
        """Return the latest runtime tab index observed by this executor."""

        return self._current_page_index

    def supports(self, command_type: type[BrowserCommand]) -> bool:
        return command_type in {
            NavigateCommand,
            ObserveCommand,
            ClickCommand,
            DownloadCommand,
        }

    async def execute(self, command: BrowserCommand) -> BrowserCommandResult:
        """Execute against this executor's current logical page.

        A :class:`BrowserSessionBroker` normally calls ``execute_for_page`` so
        that the runtime tab mapping is explicit.  This method keeps the
        executor independently testable without exposing browser objects.
        """

        return await self.execute_for_page(
            command,
            session=self.session,
            page=self.page_handle,
            generation=self.page_handle.generation,
            runtime_tab_index=self._current_page_index,
        )

    async def execute_for_page(
        self,
        command: BrowserCommand,
        *,
        session: SessionHandle,
        page: PageHandle,
        generation: int,
        runtime_tab_index: int,
    ) -> BrowserCommandResult:
        if not isinstance(runtime_tab_index, int) or runtime_tab_index < 0:
            raise MCPProtocolError("MCP runtime tab index must be a non-negative integer")
        if isinstance(command, AuthenticatedFetchCommand):
            raise MCPUnsupportedCapability(
                f"Playwright MCP {MCP_VERSION_BASELINE} has no verified authenticated response-body command"
            )
        if not self.supports(type(command)):
            raise MCPUnsupportedCapability(
                f"Playwright MCP executor does not support {type(command).__name__}"
            )
        await self.select_runtime_page(runtime_tab_index)
        if isinstance(command, NavigateCommand):
            return await self._navigate(command, session=session, page=page, generation=generation)
        if isinstance(command, ObserveCommand):
            return await self._observe(command, session=session, page=page, generation=generation)
        if isinstance(command, ClickCommand):
            return await self._click(command, session=session, page=page, generation=generation)
        if isinstance(command, DownloadCommand):
            return await self._download(command, session=session, page=page, generation=generation)
        raise MCPUnsupportedCapability(f"Unhandled MCP command {type(command).__name__}")

    async def list_pages(self) -> tuple[BrowserPageSummary, ...]:
        reply = await self._call_tool("browser_tabs", {"action": "list"})
        pages, current = self._parse_pages(reply.text)
        self._current_page_index = current if current is not None else self._current_page_index
        return pages

    async def select_runtime_page(self, index: int) -> None:
        if not isinstance(index, int) or index < 0:
            raise MCPProtocolError("MCP runtime tab index must be a non-negative integer")
        await self._call_tool("browser_tabs", {"action": "select", "index": index})
        self._current_page_index = index

    async def _call_tool(self, tool: str, arguments: Mapping[str, Any]) -> _MCPToolReply:
        if tool not in MCP_TOOL_ALLOWLIST:
            raise MCPUnsupportedCapability(f"MCP tool is not allow-listed: {tool}")
        try:
            value = self.client.call(tool, arguments)
            raw = await value if inspect.isawaitable(value) else value
        except MCPExecutorError:
            raise
        except Exception as exc:
            raise MCPConnectionError(f"MCP tool call failed for {tool}: {type(exc).__name__}") from exc
        text = self._result_text(raw)
        if isinstance(raw, Mapping) and bool(raw.get("isError")):
            raise MCPProtocolError(f"MCP tool {tool} reported an execution error: {text[:240]}")
        return _MCPToolReply(raw=raw, text=text)

    @staticmethod
    def _result_text(raw: Any) -> str:
        if isinstance(raw, str):
            return raw
        content = raw.get("content") if isinstance(raw, Mapping) else getattr(raw, "content", None)
        if content is not None:
            pieces: list[str] = []
            for block in content if isinstance(content, (list, tuple)) else (content,):
                if isinstance(block, Mapping):
                    text = block.get("text")
                    if text is not None:
                        pieces.append(str(text))
                else:
                    text = getattr(block, "text", None)
                    if text is not None:
                        pieces.append(str(text))
            if pieces:
                return "\n".join(pieces)
        if isinstance(raw, Mapping) and raw.get("text") is not None:
            return str(raw["text"])
        if raw is None:
            return ""
        return str(raw)

    async def _navigate(
        self,
        command: NavigateCommand,
        *,
        session: SessionHandle,
        page: PageHandle,
        generation: int,
    ) -> BrowserObservation:
        await self._call_tool("browser_navigate", {"url": command.url})
        return await self._observe(
            ObserveCommand(include_html=False, include_visible_text=False),
            session=session,
            page=page,
            generation=generation,
        )

    async def _observe(
        self,
        command: ObserveCommand,
        *,
        session: SessionHandle,
        page: PageHandle,
        generation: int,
    ) -> BrowserObservation:
        if command.include_html:
            raise ObservationUnavailable(
                "Playwright MCP exposes a structured accessibility snapshot, not full HTML"
            )
        reply = await self._call_tool(
            "browser_snapshot",
            {
                "boxes": False,
                "depth": 10,
            },
        )
        url, title = self._page_info(reply.text)
        observation_id = f"observation-{uuid.uuid4().hex}"
        refs = frozenset(re.findall(r"\[ref=([^\]]+)\]", reply.text))
        self._last_snapshot = _SnapshotBinding(
            observation_id=observation_id,
            session=session,
            page=page,
            generation=generation,
            refs=refs,
        )
        self._last_page_url = url or self._last_page_url
        self._last_page_title = title or self._last_page_title
        return BrowserObservation(
            session=session,
            page=page,
            generation=generation,
            url=self._last_page_url,
            title=self._last_page_title,
            html=None,
            visible_text=None,
            structured_content=reply.text,
            inspection_complete=True,
            metadata={
                "Backend": "MCPExecutor",
                "MCPTool": "browser_snapshot",
                "ObservationId": observation_id,
                "SnapshotRefCount": len(refs),
                "SnapshotRefsScope": "single-observation-and-generation",
                "FullHTML": False,
            },
        )

    async def _click(
        self,
        command: ClickCommand,
        *,
        session: SessionHandle,
        page: PageHandle,
        generation: int,
    ) -> BrowserActionResult:
        target, description = await self._resolve_target(command.target, session=session, page=page, generation=generation)
        reply = await self._call_tool(
            "browser_click",
            {"target": target, "element": description},
        )
        url, _title = self._page_info(reply.text)
        self._last_page_url = url or self._last_page_url
        return BrowserActionResult(
            session=session,
            page=page,
            generation=generation,
            action="click",
            url=self._last_page_url,
        )

    async def _download(
        self,
        command: DownloadCommand,
        *,
        session: SessionHandle,
        page: PageHandle,
        generation: int,
    ) -> DownloadArtifact:
        if command.capture is not None:
            raise MCPUnsupportedCapability(
                "MCP generic click/download is not an authorized response-capture implementation"
            )
        target, description = await self._resolve_target(command.target, session=session, page=page, generation=generation)
        reply = await self._call_tool(
            "browser_click",
            {"target": target, "element": description},
        )
        raw_path = self._download_path(reply.text)
        if raw_path is None:
            raise MCPProtocolError(
                "MCP click did not return a completed download path; refusing to infer an artifact"
            )
        candidate = await self._wait_for_artifact(raw_path, timeout_ms=command.timeout_ms)
        return DownloadArtifact.from_path(
            candidate,
            suggested_filename=command.suggested_filename,
            source_url=self._last_page_url or None,
            page=page,
            metadata={
                "Backend": "MCPExecutor",
                "MCPTool": "browser_click",
                "RawReturnedPath": raw_path,
                "CompletionSignal": "downloaded-event+filesystem",
                "MCPArtifactId": None,
            },
        )

    async def _resolve_target(
        self,
        target: BrowserTarget,
        *,
        session: SessionHandle,
        page: PageHandle,
        generation: int,
    ) -> tuple[str, str]:
        if target.ref is not None:
            binding = self._last_snapshot
            if binding is None:
                raise StaleObservationReference("Snapshot ref has no current observation")
            if (
                binding.session != session
                or binding.page != page
                or binding.generation != generation
                or (target.observation_id and target.observation_id != binding.observation_id)
                or target.ref not in binding.refs
            ):
                raise StaleObservationReference("Snapshot ref is stale or belongs to another page/generation")
            return target.ref, f"snapshot ref {target.ref}"

        if target.css and (target.text is not None or target.text_regex is not None):
            raise MCPUnsupportedCapability(
                "MCP target translation cannot guarantee a CSS selector plus text filter in one call"
            )
        if target.css:
            return target.css, target.css

        if target.text is None and target.text_regex is None:
            raise MCPUnsupportedCapability("MCP target has no representable selector")
        arguments: dict[str, Any]
        if target.text_regex is not None:
            pattern = target.text_regex
        else:
            pattern = re.escape(target.text or "") if target.exact_text else None
        if pattern is not None:
            if target.exact_text:
                pattern = f"^(?:{pattern})$"
            arguments = {"regex": pattern}
        else:
            arguments = {"text": target.text}
        reply = await self._call_tool("browser_find", arguments)
        refs = tuple(dict.fromkeys(re.findall(r"\[ref=([^\]]+)\]", reply.text)))
        if not refs:
            raise MCPProtocolError("MCP browser_find returned no executable snapshot ref")
        if target.occurrence >= len(refs):
            raise MCPProtocolError(
                f"MCP browser_find occurrence {target.occurrence} is outside {len(refs)} matches"
            )
        observation_id = f"observation-{uuid.uuid4().hex}"
        self._last_snapshot = _SnapshotBinding(
            observation_id=observation_id,
            session=session,
            page=page,
            generation=generation,
            refs=frozenset(refs),
        )
        chosen = refs[target.occurrence]
        return chosen, target.text or target.text_regex or chosen

    @staticmethod
    def _page_info(text: str) -> tuple[str, str]:
        url_match = re.search(r"^\s*-\s*Page URL:\s*(.*?)\s*$", text, flags=re.MULTILINE)
        title_match = re.search(r"^\s*-\s*Page Title:\s*(.*?)\s*$", text, flags=re.MULTILINE)
        return (
            url_match.group(1).strip() if url_match else "",
            title_match.group(1).strip() if title_match else "",
        )

    @staticmethod
    def _parse_pages(text: str) -> tuple[tuple[BrowserPageSummary, ...], int | None]:
        pages: list[BrowserPageSummary] = []
        current: int | None = None
        pattern = re.compile(
            r"^\s*-\s*(\d+):\s*(?P<current>\(current\)\s*)?\[(.*?)\]\((.*)\)\s*$"
        )
        for line in text.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            index = int(match.group(1))
            pages.append(BrowserPageSummary(index=index, title=match.group(3), url=match.group(4)))
            if match.group("current"):
                current = index
        if not pages:
            raise MCPProtocolError("MCP browser_tabs list returned no parseable pages")
        return tuple(pages), current

    @staticmethod
    def _download_path(text: str) -> str | None:
        patterns = (
            r"Downloaded\s+file\s+.*?\s+to\s+\"([^\"]+)\"",
            r"Downloaded\s+file\s+.*?\s+to\s+'([^']+)'",
            r"Downloaded\s+file\s+.*?\s+to\s+([^\r\n]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip().strip('"').strip("'")
        return None

    async def _wait_for_artifact(self, raw_path: str, *, timeout_ms: int) -> Path:
        path = self._resolve_artifact_path(raw_path)
        deadline = time.monotonic() + min(
            max(timeout_ms / 1000.0, 0.1),
            self._max_download_wait_seconds,
        )
        previous_size: int | None = None
        while time.monotonic() <= deadline:
            if path.is_file():
                size = path.stat().st_size
                if previous_size == size:
                    return path
                previous_size = size
            await asyncio.sleep(0.1)
        raise DownloadFailure(f"MCP download did not stabilize as a local artifact: {path}")

    def _resolve_artifact_path(self, raw_path: str) -> Path:
        normalized = str(raw_path).strip().strip('"').strip("'")
        if not normalized:
            raise DownloadFailure("MCP returned an empty download path")
        raw = Path(normalized)
        candidates: list[Path] = []
        if raw.is_absolute():
            candidates.append(raw)
        else:
            candidates.append(self.downloads_dir / raw)
            if self.mcp_path_base is not None:
                candidates.append(self.mcp_path_base / raw)
        approved_candidates: list[Path] = []
        for candidate in candidates:
            resolved = candidate.resolve()
            if not any(resolved == root or resolved.is_relative_to(root) for root in self._artifact_roots):
                continue
            approved_candidates.append(resolved)
            if resolved.is_file():
                return resolved
        if approved_candidates:
            # The MCP event can precede the final filesystem rename. Return
            # the approved candidate and let the bounded poll observe its
            # appearance and stable size.
            return approved_candidates[0]
        roots = ", ".join(str(root) for root in self._artifact_roots)
        raise DownloadFailure(
            f"MCP returned a path that is missing or outside approved artifact roots ({roots}): {normalized}"
        )


__all__ = [
    "CallableMCPToolClient",
    "JsonLineMCPToolClient",
    "MCPConnectionError",
    "MCPExecutor",
    "MCPExecutorError",
    "MCPProtocolError",
    "MCPToolClient",
    "MCP_TOOL_ALLOWLIST",
    "MCP_VERSION_BASELINE",
    "MCPUnsupportedCapability",
    "StaleObservationReference",
]
