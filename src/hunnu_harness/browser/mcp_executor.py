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
    BrowserTargetObservation,
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

_DOWNLOAD_FINAL_SUFFIXES = frozenset({".pdf", ".caj", ".nh", ".kdh"})
_CAJ_SIGNATURES = (b"CAJ", b"HNLC", b"KDH")


# This is intentionally a fixed, read-only observation expression.  The
# Harness does not expose arbitrary page evaluation to adapters; it asks the
# already-running MCP page only for the rendered document HTML required by
# the existing BrowserObservation contract.
_FULL_HTML_OBSERVATION_FUNCTION = (
    "() => document.documentElement ? document.documentElement.outerHTML : ''"
)


MCP_TOOL_ALLOWLIST = frozenset(
    {
        "browser_click",
        "browser_find",
        "browser_navigate",
        "browser_snapshot",
        "browser_tabs",
        "browser_evaluate",
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
            return await self._click(
                command,
                session=session,
                page=page,
                generation=generation,
                runtime_tab_index=runtime_tab_index,
            )
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
        probe_requested = bool(command.text_probes)
        reply = await self._call_tool(
            "browser_snapshot",
            {
                "boxes": probe_requested,
                "depth": 12 if probe_requested else 10,
            },
        )
        html: str | None = None
        if command.include_html:
            try:
                html_reply = await self._call_tool(
                    "browser_evaluate",
                    {"function": _FULL_HTML_OBSERVATION_FUNCTION},
                )
            except MCPUnsupportedCapability as exc:
                # Preserve the adapter's existing structured-snapshot fallback
                # when a host predates the fixed read-only observation bridge.
                raise ObservationUnavailable(
                    "Playwright MCP host does not expose the fixed full-HTML observation bridge"
                ) from exc
            html = self._parse_evaluated_html(html_reply.text)
            if html is None:
                raise ObservationUnavailable(
                    "Playwright MCP returned no usable full-HTML observation"
                )
        url, title = self._page_info(reply.text)
        target_observations, probes_complete, probes_truncated = self._parse_probe_observations(
            reply.text,
            command.text_probes,
            max_matches=command.max_probe_matches,
            frame_url=url or self._last_page_url,
        )
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
            html=html,
            visible_text=None,
            structured_content=reply.text,
            target_observations=target_observations,
            inspection_complete=probes_complete,
            metadata={
                "Backend": "MCPExecutor",
                "MCPTool": "browser_snapshot+browser_evaluate" if html is not None else "browser_snapshot",
                "ObservationId": observation_id,
                "SnapshotRefCount": len(refs),
                "SnapshotRefsScope": "single-observation-and-generation",
                "FullHTML": html is not None,
                "TextProbeCount": len(command.text_probes),
                "ProbeObservationCount": len(target_observations),
                "ProbeMatchesTruncated": probes_truncated,
            },
        )

    @staticmethod
    def _parse_evaluated_html(text: str) -> str | None:
        """Extract the JSON-encoded result from Playwright MCP evaluate output."""

        result_marker = "### Result"
        code_marker = "### Ran Playwright code"
        if result_marker not in text:
            return None
        payload = text.split(result_marker, 1)[1]
        if code_marker in payload:
            payload = payload.split(code_marker, 1)[0]
        payload = payload.strip()
        if not payload:
            return None
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            value = payload
        if not isinstance(value, str):
            return None
        return value

    @staticmethod
    def _parse_probe_observations(
        snapshot: str,
        probes: tuple[str, ...],
        *,
        max_matches: int,
        frame_url: str,
    ) -> tuple[tuple[BrowserTargetObservation, ...], bool, bool]:
        """Extract bounded, source-neutral text-probe geometry from a snapshot.

        Playwright MCP's ``boxes`` snapshot annotates nodes with viewport-relative
        ``[box=x,y,width,height]`` values.  Those coordinates are sufficient to
        prove that a node wholly above or left of the viewport is off-screen.
        They are not sufficient to prove render visibility for an on-screen
        node, so ``playwright_visible`` deliberately remains unknown and the
        source detector can fail closed.
        """

        if not probes:
            return (), True, False
        number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
        box_pattern = re.compile(
            rf"\[box=(?P<x>{number}),(?P<y>{number}),(?P<width>{number}),(?P<height>{number})\]"
        )
        lines = snapshot.splitlines()
        observations: list[BrowserTargetObservation] = []
        inspection_complete = True
        truncated = False
        for probe in probes:
            pattern = re.compile(probe)
            matching_lines = [line for line in lines if pattern.search(line)]
            if len(matching_lines) > max_matches:
                matching_lines = matching_lines[:max_matches]
                inspection_complete = False
                truncated = True
            for line in matching_lines:
                box_match = box_pattern.search(line)
                bounding_box: dict[str, float] | None = None
                if box_match:
                    bounding_box = {
                        key: float(box_match.group(key))
                        for key in ("x", "y", "width", "height")
                    }
                else:
                    inspection_complete = False
                marker_match = pattern.search(line)
                observations.append(
                    BrowserTargetObservation(
                        marker=marker_match.group(0) if marker_match else probe,
                        frame_url=frame_url or "unknown",
                        playwright_visible=None,
                        bounding_box=bounding_box,
                        client_rect=bounding_box,
                        client_width=(bounding_box or {}).get("width"),
                        client_height=(bounding_box or {}).get("height"),
                        # MCP does not expose viewport dimensions through the
                        # allow-listed command surface. A large positive bound
                        # preserves the one fact we can prove from coordinates:
                        # nodes wholly above/left of zero do not intersect it.
                        viewport_width=1_000_000,
                        viewport_height=1_000_000,
                        frame_viewport_visible=True,
                        inspection_complete=box_match is not None,
                    )
                )
        return tuple(observations), inspection_complete, truncated

    async def _click(
        self,
        command: ClickCommand,
        *,
        session: SessionHandle,
        page: PageHandle,
        generation: int,
        runtime_tab_index: int,
    ) -> BrowserActionResult:
        before_pages: tuple[BrowserPageSummary, ...] = ()
        if command.follow_new_page:
            before_pages = await self.list_pages()
            await self.select_runtime_page(runtime_tab_index)
        target, description = await self._resolve_target(command.target, session=session, page=page, generation=generation)
        reply = await self._call_tool(
            "browser_click",
            {"target": target, "element": description},
        )
        url, _title = self._page_info(reply.text)
        followed_index = runtime_tab_index
        if command.follow_new_page:
            after_pages = await self.list_pages()
            before_indexes = {item.index for item in before_pages}
            new_pages = tuple(item for item in after_pages if item.index not in before_indexes)
            if len(new_pages) > 1:
                raise MCPProtocolError(
                    "MCP click opened multiple pages; refusing to guess which page belongs to the target"
                )
            if new_pages:
                followed = new_pages[0]
                if command.close_origin_when_sole_page and len(before_pages) == 1:
                    await self._call_tool(
                        "browser_tabs",
                        {"action": "close", "index": runtime_tab_index},
                    )
                    remaining_pages = await self.list_pages()
                    if len(remaining_pages) != 1:
                        raise MCPProtocolError(
                            "MCP sole-page origin close did not leave one followed page"
                        )
                    followed = remaining_pages[0]
                followed_index = followed.index
                await self.select_runtime_page(followed_index)
                url = followed.url or url
            elif self._current_page_index != runtime_tab_index:
                raise MCPProtocolError(
                    "MCP click changed to an existing page without a unique new-page identity"
                )
            else:
                followed = next(
                    (item for item in after_pages if item.index == runtime_tab_index),
                    None,
                )
                if followed is None:
                    raise MCPProtocolError("MCP click removed the active page unexpectedly")
                url = followed.url or url
        self._last_snapshot = None
        self._last_page_url = url or self._last_page_url
        return BrowserActionResult(
            session=session,
            page=page,
            generation=generation,
            action="click",
            url=self._last_page_url,
            runtime_tab_index=followed_index,
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
        baseline = self._download_directory_state()
        capture_started_ns = time.time_ns()
        reply = await self._call_tool(
            "browser_click",
            {"target": target, "element": description},
        )
        raw_path = self._download_path(reply.text)
        filesystem_fallback_used = False
        artifact_correlation_confidence: str | None = None
        if raw_path is None:
            pending_name = self._pending_download_name(reply.text)
            if pending_name is not None:
                candidate = await self._wait_for_pending_artifact(
                    pending_name,
                    baseline=baseline,
                    timeout_ms=command.timeout_ms,
                )
                completion_signal = "downloading-event+bounded-directory-watch"
            else:
                candidate = await self._wait_for_unannounced_artifact(
                    baseline=baseline,
                    capture_started_ns=capture_started_ns,
                    timeout_ms=command.timeout_ms,
                )
                completion_signal = "filesystem-watch-after-authorized-click"
                filesystem_fallback_used = True
                artifact_correlation_confidence = "HIGH"
        else:
            candidate = await self._wait_for_artifact(raw_path, timeout_ms=command.timeout_ms)
            completion_signal = "downloaded-event+filesystem"
        downloaded_artifact_type = self._validate_download_artifact_header(candidate)
        return DownloadArtifact.from_path(
            candidate,
            suggested_filename=command.suggested_filename,
            source_url=self._last_page_url or None,
            page=page,
            metadata={
                "Backend": "MCPExecutor",
                "MCPTool": "browser_click",
                "RawReturnedPath": raw_path,
                "CompletionSignal": completion_signal,
                "MCPArtifactId": None,
                "FilesystemFallbackUsed": filesystem_fallback_used,
                "ArtifactCorrelationConfidence": artifact_correlation_confidence,
                "DownloadedArtifactType": downloaded_artifact_type,
                "ArtifactValidationLevel": (
                    "header-only" if downloaded_artifact_type is not None else None
                ),
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
        if target.text_regex is not None:
            arguments: dict[str, Any] = {"regex": target.text_regex}
        else:
            # ``browser_find`` searches the serialized snapshot node, so a
            # fully anchored regex cannot match a labelled line such as
            # ``link \"Title\" [ref=...]``.  Use its bounded text lookup and
            # enforce exactness locally against the parsed accessible label.
            arguments = {"text": target.text}
        reply = await self._call_tool("browser_find", arguments)
        refs = self._matching_find_refs(reply.text, target)
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
    def _matching_find_refs(text: str, target: BrowserTarget) -> tuple[str, ...]:
        """Return refs attached to matching nodes, excluding path ancestors.

        ``browser_find`` returns each match under its full accessibility path.
        Harvesting every ref from that output can select a root container or a
        search box instead of the requested link.  Only refs on lines whose
        accessible label actually matches the requested text are executable.
        """

        wanted_text = re.sub(r"\s+", " ", target.text or "").strip()
        wanted_regex = re.compile(target.text_regex) if target.text_regex is not None else None

        def label_matches(value: str) -> bool:
            candidate = re.sub(r"\s+", " ", value).strip()
            if wanted_regex is not None:
                return wanted_regex.search(candidate) is not None
            if target.exact_text:
                return candidate == wanted_text
            return wanted_text.casefold() in candidate.casefold()

        all_refs: list[str] = []
        matched_refs: list[tuple[int, str]] = []
        for line in text.splitlines():
            ref_match = re.search(r"\[ref=([^\]]+)\]", line)
            if ref_match is None:
                continue
            ref = ref_match.group(1)
            all_refs.append(ref)
            labels: list[str] = []
            prefix = line[: ref_match.start()]
            for quoted in re.finditer(r'"((?:\\.|[^"\\])*)"', prefix):
                raw = quoted.group(1)
                try:
                    labels.append(str(json.loads(f'"{raw}"')))
                except json.JSONDecodeError:
                    labels.append(raw.replace(r'\"', '"').replace(r"\\", "\\"))
            suffix = line[ref_match.end() :]
            suffix_match = re.search(r"(?:\s*\[[^\]]+\])*\s*:\s*(.+?)\s*$", suffix)
            if suffix_match:
                labels.append(suffix_match.group(1).strip().strip('"'))
            if any(label_matches(label) for label in labels):
                role_match = re.match(r"^\s*-\s*([A-Za-z]+)\b", line)
                role = role_match.group(1).casefold() if role_match else ""
                interactive_roles = {
                    "button",
                    "checkbox",
                    "link",
                    "menuitem",
                    "option",
                    "radio",
                    "tab",
                }
                score = 3 if role in interactive_roles else 1
                if role not in interactive_roles and "[cursor=pointer]" in line:
                    score = 2
                matched_refs.append((score, ref))

        if matched_refs:
            best_score = max(score for score, _ref in matched_refs)
            return tuple(
                dict.fromkeys(ref for score, ref in matched_refs if score == best_score)
            )
        unique = tuple(dict.fromkeys(all_refs))
        return unique if len(unique) == 1 else ()

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

    @staticmethod
    def _pending_download_name(text: str) -> str | None:
        match = re.search(
            r"Downloading\s+file\s+(.+?)(?:\s+\.\.\.|\s*$)",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if match is None:
            return None
        raw = match.group(1).strip().strip('"').strip("'")
        name = Path(raw).name
        return name if name and name == raw else None

    def _download_directory_state(self) -> dict[Path, tuple[int, int, int]]:
        if not self.downloads_dir.is_dir():
            return {}
        state: dict[Path, tuple[int, int, int]] = {}
        for candidate in self.downloads_dir.iterdir():
            try:
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                stat = resolved.stat()
            except OSError:
                continue
            state[resolved] = (stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size)
        return state

    @staticmethod
    def _normalized_download_name(value: str) -> str:
        return re.sub(r"[\W_]+", "", Path(value).name, flags=re.UNICODE).casefold()

    async def _wait_for_pending_artifact(
        self,
        pending_name: str,
        *,
        baseline: dict[Path, tuple[int, int, int]],
        timeout_ms: int,
    ) -> Path:
        wanted = self._normalized_download_name(pending_name)
        if not wanted:
            raise DownloadFailure("MCP pending-download event did not contain a safe filename")
        deadline = time.monotonic() + min(
            max(timeout_ms / 1000.0, 0.1),
            self._max_download_wait_seconds,
        )
        previous: tuple[Path, tuple[int, int, int]] | None = None
        while time.monotonic() <= deadline:
            current = self._download_directory_state()
            matches = [
                (path, metadata)
                for path, metadata in current.items()
                if self._normalized_download_name(path.name) == wanted
                and baseline.get(path) != metadata
            ]
            if len(matches) > 1:
                raise DownloadFailure(
                    "MCP pending-download event matched more than one changed artifact"
                )
            if len(matches) == 1:
                path, metadata = matches[0]
                if metadata[2] > 0 and previous == (path, metadata):
                    self._validate_download_artifact_header(path)
                    return self._resolve_artifact_path(str(path))
                previous = (path, metadata)
            else:
                previous = None
            await asyncio.sleep(0.1)
        raise DownloadFailure(
            f"MCP pending download did not stabilize as one approved local artifact: {pending_name}"
        )

    async def _wait_for_unannounced_artifact(
        self,
        *,
        baseline: dict[Path, tuple[int, int, int]],
        capture_started_ns: int,
        timeout_ms: int,
    ) -> Path:
        """Capture one browser-created final artifact when MCP omits its event.

        This is deliberately a post-click observation path.  It never fetches a
        URL or chooses an unchanged historical file; it only accepts one final
        artifact in the configured MCP output directory whose metadata changed
        during this click window and then stabilized.
        """

        deadline = time.monotonic() + min(
            max(timeout_ms / 1000.0, 0.1),
            max(self._max_download_wait_seconds, timeout_ms / 1000.0),
        )
        previous: tuple[Path, tuple[int, int, int]] | None = None
        while time.monotonic() <= deadline:
            current = self._download_directory_state()
            matches: list[tuple[Path, tuple[int, int, int]]] = []
            for path, metadata in current.items():
                if path.suffix.casefold() not in _DOWNLOAD_FINAL_SUFFIXES:
                    continue
                if baseline.get(path) == metadata:
                    continue
                created_ns, modified_ns, size = metadata
                if size <= 0 or max(created_ns, modified_ns) < capture_started_ns:
                    continue
                matches.append((path, metadata))
            if len(matches) > 1:
                raise DownloadFailure(
                    "MCP post-click filesystem capture matched more than one final artifact"
                )
            if len(matches) == 1:
                path, metadata = matches[0]
                if previous == (path, metadata):
                    self._validate_download_artifact_header(path)
                    return self._resolve_artifact_path(str(path))
                previous = (path, metadata)
            else:
                previous = None
            await asyncio.sleep(0.1)
        raise DownloadFailure(
            "MCP click returned no download event and no single stable post-click artifact"
        )

    @staticmethod
    def _validate_download_artifact_header(path: Path) -> str | None:
        """Reject obvious HTML/error masquerades and classify known full-text types."""

        suffix = path.suffix.casefold()
        if suffix not in _DOWNLOAD_FINAL_SUFFIXES:
            return None
        try:
            with path.open("rb") as stream:
                header = stream.read(512)
        except OSError as exc:
            raise DownloadFailure(f"MCP downloaded artifact could not be read: {path}") from exc
        if not header:
            raise DownloadFailure(f"MCP downloaded artifact is empty: {path}")
        lowered = header.lstrip().lower()
        if (
            lowered.startswith((b"<!doctype html", b"<html", b"<?xml", b"<head", b"<body"))
            or b"<html" in lowered
        ):
            raise DownloadFailure(f"MCP downloaded artifact is HTML rather than full text: {path}")
        if suffix == ".pdf":
            if not header.startswith(b"%PDF-"):
                raise DownloadFailure(f"MCP PDF artifact has no PDF signature: {path}")
            return "PDF"
        if not header.startswith(_CAJ_SIGNATURES):
            raise DownloadFailure(f"MCP CAJ artifact has no recognized CAJ signature: {path}")
        return "CAJ"

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
