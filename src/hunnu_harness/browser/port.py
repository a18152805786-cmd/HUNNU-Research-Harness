"""The source-neutral command execution port used by literature adapters."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .commands import BrowserCommand, BrowserCommandResult, PageHandle, SessionHandle
from .transport import BrowserTransportError


@runtime_checkable
class BrowserCommandPort(Protocol):
    """Minimal async boundary between an adapter and a browser backend."""

    downloads_dir: Path | None
    session: SessionHandle
    page_handle: PageHandle

    async def execute(self, command: BrowserCommand) -> BrowserCommandResult: ...


def validate_browser_command_port(value: Any) -> BrowserCommandPort:
    """Validate a command port without starting a browser or doing I/O."""

    if value is None:
        raise BrowserTransportError(
            "A BrowserCommandPort is required; literature execution cannot use a bare browser fallback"
        )
    missing = [
        name
        for name in ("execute", "downloads_dir", "session", "page_handle")
        if not hasattr(value, name)
    ]
    if missing or not inspect.iscoroutinefunction(getattr(value, "execute", None)):
        detail = ", ".join(missing or ("execute",))
        raise BrowserTransportError(
            f"BrowserCommandPort is missing required capability/capabilities: {detail}"
        )
    directory = getattr(value, "downloads_dir", None)
    if directory is not None:
        try:
            Path(directory)
        except (TypeError, ValueError) as exc:
            raise BrowserTransportError("BrowserCommandPort.downloads_dir must be path-like") from exc
    return value


def ensure_browser_command_port(value: Any) -> BrowserCommandPort:
    """Return a command port, wrapping the v0.2.16 transport only for compatibility.

    The wrapper is deliberately created at the Harness boundary.  Adapters
    never receive the old ``BrowserTransport.page`` surface and therefore
    cannot fall back to direct Playwright operations.
    """

    try:
        return validate_browser_command_port(value)
    except BrowserTransportError:
        pass

    try:
        cached = vars(value).get("_hunnu_command_port") if value is not None else None
    except TypeError:
        cached = None
    if cached is not None:
        try:
            return validate_browser_command_port(cached)
        except BrowserTransportError:
            pass

    from .local_executor import LocalPlaywrightExecutor

    try:
        wrapped = LocalPlaywrightExecutor(legacy_browser=value)
    except Exception as exc:
        if isinstance(exc, BrowserTransportError):
            raise
        raise BrowserTransportError(
            "The supplied browser is neither a BrowserCommandPort nor a compatible legacy BrowserTransport"
        ) from exc
    try:
        setattr(value, "_hunnu_command_port", wrapped)
    except Exception:
        pass
    return validate_browser_command_port(wrapped)


__all__ = [
    "BrowserCommandPort",
    "ensure_browser_command_port",
    "validate_browser_command_port",
]
