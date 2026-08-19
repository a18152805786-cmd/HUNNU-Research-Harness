"""Harness-owned logical session and page state for browser executors.

``BrowserSessionBroker`` deliberately treats MCP tab indexes as ephemeral
runtime mappings.  Adapters receive only the existing command-port surface;
they never see an MCP ref, tab index, or browser object.  When the mapping is
uncertain, the broker advances its generation and fails closed until an
explicit rebind establishes a new logical mapping.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from .commands import (
    BrowserActionResult,
    BrowserCommand,
    BrowserCommandResult,
    BrowserObservation,
    BrowserPageSummary,
    DownloadArtifact,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from .mcp_executor import MCPConnectionError, MCPExecutorError, MCPProtocolError


class SessionBrokerError(MCPExecutorError):
    """Base class for logical session/binding failures."""


class SessionUnavailable(SessionBrokerError):
    """The broker has no safe active mapping to the MCP runtime."""


class StalePageHandle(SessionBrokerError):
    """A page handle belongs to an older generation or another session."""


@runtime_checkable
class MCPRuntimeExecutor(Protocol):
    downloads_dir: Path

    async def execute_for_page(
        self,
        command: BrowserCommand,
        *,
        session: SessionHandle,
        page: PageHandle,
        generation: int,
        runtime_tab_index: int,
    ) -> BrowserCommandResult: ...

    async def list_pages(self) -> tuple[BrowserPageSummary, ...]: ...

    async def select_runtime_page(self, index: int) -> None: ...


@dataclass(frozen=True)
class LogicalPage:
    """A logical Harness page and its current ephemeral tab mapping."""

    handle: PageHandle
    summary: BrowserPageSummary
    active: bool = False

    @property
    def runtime_tab_index(self) -> int:
        return self.summary.index


@dataclass(frozen=True)
class _PageBinding:
    handle: PageHandle
    summary: BrowserPageSummary


class BrowserSessionBroker:
    """Expose a guarded :class:`BrowserCommandPort` over an MCP executor."""

    def __init__(
        self,
        executor: MCPRuntimeExecutor,
        *,
        session_id: str | None = None,
    ) -> None:
        if not isinstance(executor, MCPRuntimeExecutor):
            raise TypeError("BrowserSessionBroker requires an MCP page executor")
        self.executor = executor
        self.downloads_dir = Path(executor.downloads_dir).expanduser().resolve()
        self._session_id = session_id or f"mcp-session-{uuid.uuid4().hex[:12]}"
        self._generation = 0
        self._session = SessionHandle(self._session_id, self._generation)
        self._page_handle = PageHandle(
            "mcp-page-0",
            session=self._session,
            generation=self._generation,
        )
        self._bindings: dict[str, _PageBinding] = {}
        self._active_page_id = self._page_handle.value
        self._state = "uninitialized"
        self._invalid_reason = ""

    @property
    def session(self) -> SessionHandle:
        return self._session

    @property
    def page_handle(self) -> PageHandle:
        return self._page_handle

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def state(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        return self._state == "active"

    def validate_page_handle(self, handle: PageHandle) -> PageHandle:
        if not isinstance(handle, PageHandle):
            raise StalePageHandle("A Harness PageHandle is required")
        if handle.generation != self._generation:
            raise StalePageHandle(
                f"PageHandle generation {handle.generation} is stale; current generation is {self._generation}"
            )
        if self._state != "active":
            raise SessionUnavailable(f"MCP session is not active (state={self._state})")
        if handle.session is not None and handle.session != self._session:
            raise StalePageHandle("PageHandle belongs to a different Harness session")
        binding = self._bindings.get(handle.value)
        if binding is None or binding.handle != handle:
            raise StalePageHandle(f"PageHandle is not bound in the current MCP session: {handle.value}")
        return handle

    async def execute(self, command: BrowserCommand) -> BrowserCommandResult:
        """Execute one adapter command through the current page mapping."""

        await self._ensure_active_mapping()
        binding = self._bindings.get(self._active_page_id)
        if binding is None:
            self.invalidate("active page mapping disappeared")
            raise SessionUnavailable("Active MCP page mapping is unavailable")
        try:
            result = await self.executor.execute_for_page(
                command,
                session=self._session,
                page=binding.handle,
                generation=self._generation,
                runtime_tab_index=binding.summary.index,
            )
        except MCPConnectionError:
            self.invalidate("MCP connection state became unknown")
            raise
        except MCPProtocolError:
            # A malformed result does not prove that the browser is gone, but
            # it is never safe to invent an observation or artifact.  The
            # current mapping remains active for an explicit retry.
            raise
        return self._decorate_result(result, binding)

    async def enumerate_pages(self) -> tuple[LogicalPage, ...]:
        """Refresh page inventory and return logical handles, not tab IDs."""

        if self._state == "uncertain":
            raise SessionUnavailable("MCP session requires explicit rebind")
        try:
            pages = await self.executor.list_pages()
        except MCPConnectionError:
            self.invalidate("MCP connection state became unknown while enumerating pages")
            raise
        if not pages:
            self.invalidate("MCP returned an empty page inventory")
            raise SessionUnavailable("MCP returned no pages")
        current_index = getattr(self.executor, "current_page_index", pages[0].index)
        self._install_inventory(pages, current_index=current_index, initial=self._state == "uninitialized")
        return self._logical_pages()

    async def select_page(self, handle: PageHandle) -> BrowserObservation:
        """Select a known logical page and return a fresh structured observation."""

        await self._ensure_active_mapping()
        self.validate_page_handle(handle)
        binding = self._bindings[handle.value]
        try:
            await self.executor.select_runtime_page(binding.summary.index)
            self._active_page_id = handle.value
            self._page_handle = handle
            result = await self.executor.execute_for_page(
                ObserveCommand(include_html=False, include_visible_text=False),
                session=self._session,
                page=handle,
                generation=self._generation,
                runtime_tab_index=binding.summary.index,
            )
        except MCPConnectionError:
            self.invalidate("MCP page selection state became unknown")
            raise
        if not isinstance(result, BrowserObservation):
            raise SessionBrokerError("MCP page selection did not return an observation")
        return self._decorate_result(result, binding)

    async def rebind(self) -> tuple[LogicalPage, ...]:
        """Explicitly establish a new mapping after invalidation.

        Rebind never guesses from URL/title.  It enumerates the current MCP
        tabs and allocates new logical handles in the current generation.
        """

        if self._state != "uncertain":
            return await self.enumerate_pages()
        pages = await self.executor.list_pages()
        if not pages:
            raise SessionUnavailable("Cannot rebind an MCP session with no pages")
        current_index = getattr(self.executor, "current_page_index", pages[0].index)
        self._install_inventory(pages, current_index=current_index, initial=True)
        return self._logical_pages()

    def invalidate(self, reason: str) -> None:
        """Invalidate all old page/ref mappings and advance the generation."""

        self._generation += 1
        self._session = SessionHandle(self._session_id, self._generation)
        self._page_handle = PageHandle(
            f"mcp-page-invalidated-{uuid.uuid4().hex[:8]}",
            session=self._session,
            generation=self._generation,
        )
        self._active_page_id = self._page_handle.value
        self._bindings = {}
        self._invalid_reason = str(reason).strip() or "unknown"
        self._state = "uncertain"

    async def _ensure_active_mapping(self) -> None:
        if self._state == "uncertain" or self._state.startswith("uncertain:"):
            raise SessionUnavailable(
                f"MCP session requires explicit rebind (reason={self._invalid_reason})"
            )
        if self._state == "uninitialized":
            await self.enumerate_pages()
        if self._state != "active" or not self._bindings:
            raise SessionUnavailable(f"MCP session is not ready (state={self._state})")

    def _install_inventory(
        self,
        pages: tuple[BrowserPageSummary, ...],
        *,
        current_index: int,
        initial: bool,
    ) -> None:
        by_index = {summary.index: summary for summary in pages}
        if current_index not in by_index:
            raise SessionBrokerError(
                f"MCP current tab index {current_index} is absent from its page inventory"
            )
        if not initial:
            old_indices = {binding.summary.index for binding in self._bindings.values()}
            if not old_indices.issubset(by_index):
                self.invalidate("a previously mapped MCP page disappeared")
                raise SessionUnavailable("Existing MCP page mapping cannot be safely preserved")

        new_bindings: dict[str, _PageBinding] = {}
        existing_by_index = {
            binding.summary.index: binding for binding in self._bindings.values()
        }
        for index, summary in sorted(by_index.items()):
            existing = existing_by_index.get(index)
            if existing is not None and not initial:
                new_bindings[existing.handle.value] = _PageBinding(existing.handle, summary)
                continue
            handle = PageHandle(
                f"mcp-page-{index}-{uuid.uuid4().hex[:8]}",
                session=self._session,
                generation=self._generation,
            )
            new_bindings[handle.value] = _PageBinding(handle, summary)
        self._bindings = new_bindings
        active_binding = next(
            binding for binding in self._bindings.values() if binding.summary.index == current_index
        )
        self._active_page_id = active_binding.handle.value
        self._page_handle = active_binding.handle
        self._state = "active"
        self._invalid_reason = ""

    def _logical_pages(self) -> tuple[LogicalPage, ...]:
        return tuple(
            LogicalPage(
                handle=binding.handle,
                summary=binding.summary,
                active=binding.handle.value == self._active_page_id,
            )
            for binding in sorted(self._bindings.values(), key=lambda item: item.summary.index)
        )

    def _decorate_result(self, result: BrowserCommandResult, binding: _PageBinding) -> BrowserCommandResult:
        if isinstance(result, BrowserObservation):
            inventory = tuple(
                binding_item.summary
                for binding_item in sorted(self._bindings.values(), key=lambda item: item.summary.index)
            )
            metadata = dict(result.metadata)
            metadata.update(
                {
                    "Broker": "BrowserSessionBroker",
                    "LogicalPageId": binding.handle.value,
                    "RuntimeTabIndex": binding.summary.index,
                }
            )
            return replace(
                result,
                session=self._session,
                page=binding.handle,
                generation=self._generation,
                page_inventory=inventory,
                metadata=metadata,
            )
        if isinstance(result, BrowserActionResult):
            return replace(
                result,
                session=self._session,
                page=binding.handle,
                generation=self._generation,
            )
        if isinstance(result, DownloadArtifact):
            metadata = dict(result.metadata)
            metadata.update(
                {
                    "Broker": "BrowserSessionBroker",
                    "LogicalPageId": binding.handle.value,
                    "RuntimeTabIndex": binding.summary.index,
                }
            )
            return replace(result, page=binding.handle, metadata=metadata)
        return result


__all__ = [
    "BrowserSessionBroker",
    "LogicalPage",
    "MCPRuntimeExecutor",
    "SessionBrokerError",
    "SessionUnavailable",
    "StalePageHandle",
]
