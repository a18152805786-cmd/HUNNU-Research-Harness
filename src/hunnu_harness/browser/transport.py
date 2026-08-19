"""Legacy local Playwright-shaped compatibility input.

The formal v0.2.17 adapter boundary is ``BrowserCommandPort``.  This module
retains the previous Python-Playwright-shaped contract only so the Harness can
wrap an existing local backend in ``LocalPlaywrightExecutor`` during the
transition.  It is not an MCP bridge and is not a formal adapter surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class BrowserTransportError(ValueError):
    """Raised when a caller supplies no usable adapter browser transport."""


@runtime_checkable
class BrowserPage(Protocol):
    """Minimum page surface exercised by the current literature adapters."""

    url: str
    context: Any

    async def content(self) -> str: ...

    def locator(self, selector: str) -> Any: ...

    def expect_download(self, *, timeout: int) -> Any: ...

    def on(self, event: str, callback: Any) -> Any: ...


@runtime_checkable
class BrowserTransport(Protocol):
    """Legacy transport accepted only at the Harness compatibility boundary.

    ``page`` intentionally remains an opaque, Playwright-compatible page
    handle.  Adapters must not receive this object graph on the v0.2.17 path;
    ``LocalPlaywrightExecutor`` is responsible for translating it into typed
    commands and observations.
    """

    page: BrowserPage
    downloads_dir: Path

    async def goto(self, url: str) -> None: ...


def validate_browser_transport(value: Any) -> BrowserTransport:
    """Validate the concrete capabilities required before adapter execution.

    Validation is structural and side-effect free.  It never starts a
    browser, opens a page, changes a profile, or calls an MCP tool.
    """

    if value is None:
        raise BrowserTransportError(
            "A BrowserTransport is required; literature execution cannot use a bare browser fallback"
        )

    missing_transport = [
        name
        for name in ("goto", "page", "downloads_dir")
        if not hasattr(value, name) or getattr(value, name, None) is None
    ]
    if missing_transport or not callable(getattr(value, "goto", None)):
        detail = ", ".join(missing_transport or ("goto",))
        raise BrowserTransportError(
            f"BrowserTransport is missing required capability/capabilities: {detail}"
        )

    page = getattr(value, "page")
    page_requirements = ("url", "context", "content", "locator", "expect_download", "on")
    missing_page = [
        name
        for name in page_requirements
        if not hasattr(page, name) or (name != "url" and getattr(page, name, None) is None)
    ]
    if missing_page or any(
        not callable(getattr(page, name, None))
        for name in ("content", "locator", "expect_download", "on")
    ):
        detail = ", ".join(missing_page or ("page methods",))
        raise BrowserTransportError(
            f"BrowserTransport.page is missing required capability/capabilities: {detail}"
        )

    context = getattr(page, "context")
    if not callable(getattr(context, "on", None)):
        raise BrowserTransportError("BrowserTransport.page.context must expose the event API")
    request = getattr(context, "request", None)
    if request is None or not callable(getattr(request, "get", None)):
        raise BrowserTransportError(
            "BrowserTransport.page.context.request.get is required by the Springer official-PDF path"
        )

    return value


__all__ = [
    "BrowserPage",
    "BrowserTransport",
    "BrowserTransportError",
    "validate_browser_transport",
]
