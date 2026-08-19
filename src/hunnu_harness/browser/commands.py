"""Backend-neutral browser commands and structured observations.

The command layer deliberately describes *intent* rather than exposing a
Playwright ``Page``/``Locator``/``BrowserContext`` object graph.  A local
executor may use those objects internally, but adapters only exchange the
small typed values defined here.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


class BrowserCommandError(RuntimeError):
    """Base class for fail-closed command execution errors."""


class UnsupportedCommand(BrowserCommandError):
    """The selected backend cannot execute a command."""


class InvalidTarget(BrowserCommandError):
    """A target description is empty, ambiguous, or invalid."""


class ObservationUnavailable(BrowserCommandError):
    """The observation required by an adapter was not available."""


class DownloadFailure(BrowserCommandError):
    """A browser download did not produce a usable local artifact."""


class AuthenticatedFetchFailure(BrowserCommandError):
    """An authenticated browser-context request could not be completed."""


@dataclass(frozen=True)
class SessionHandle:
    """Harness-owned logical session identity.

    v0.2.17 only needs a local executor identity.  It is not a claim about
    MCP reconnect or cross-process lease semantics.
    """

    value: str = "local"

    def __post_init__(self) -> None:
        if not str(self.value).strip():
            raise ValueError("SessionHandle.value must not be empty")


@dataclass(frozen=True)
class PageHandle:
    """Harness-owned logical page identity for one executor session."""

    value: str = "main"

    def __post_init__(self) -> None:
        if not str(self.value).strip():
            raise ValueError("PageHandle.value must not be empty")


@dataclass(frozen=True)
class BrowserTarget:
    """Serializable target description understood by a command executor.

    Only the selector forms used by the current source adapters are present:
    CSS plus an optional visible-text filter.  Raw Locator objects and Python
    callbacks are intentionally not representable.
    """

    css: str | None = None
    text: str | None = None
    text_regex: str | None = None
    exact_text: bool = False
    occurrence: int = 0

    def __post_init__(self) -> None:
        if not any(value is not None and str(value).strip() for value in (self.css, self.text, self.text_regex)):
            raise InvalidTarget("BrowserTarget requires css, text, or text_regex")
        if self.css is not None and not str(self.css).strip():
            raise InvalidTarget("BrowserTarget.css must not be empty")
        if self.text is not None and not str(self.text).strip():
            raise InvalidTarget("BrowserTarget.text must not be empty")
        if self.text_regex is not None:
            try:
                re.compile(self.text_regex)
            except re.error as exc:
                raise InvalidTarget("BrowserTarget.text_regex is not a valid regular expression") from exc
        if self.occurrence < 0:
            raise InvalidTarget("BrowserTarget.occurrence must be non-negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "CSS": self.css,
            "Text": self.text,
            "TextRegex": self.text_regex,
            "ExactText": self.exact_text,
            "Occurrence": self.occurrence,
        }


@dataclass(frozen=True)
class BrowserPageSummary:
    index: int
    title: str
    url: str


@dataclass(frozen=True)
class BrowserTargetObservation:
    """Sanitized visibility evidence for a bounded text probe."""

    marker: str
    frame_index: int = 0
    frame_name: str = ""
    frame_url: str = "unknown"
    playwright_visible: bool | None = None
    bounding_box: Mapping[str, float] | None = None
    client_rect: Mapping[str, float] | None = None
    display: str | None = None
    visibility: str | None = None
    opacity: str | None = None
    pointer_events: str | None = None
    aria_hidden: str | None = None
    client_width: float | None = None
    client_height: float | None = None
    viewport_width: float = 0
    viewport_height: float = 0
    frame_viewport_visible: bool | None = True
    inspection_complete: bool = True
    blocking_overlay: bool = False


@dataclass(frozen=True)
class BrowserObservation:
    """Structured page observation returned by ``ObserveCommand``.

    ``html`` is optional in the shared contract.  The local backend supplies
    it for the current adapters; a future backend may return only structured
    content and must then fail closed when an adapter requires HTML parsing.
    """

    session: SessionHandle
    page: PageHandle
    generation: int
    url: str
    title: str = ""
    html: str | None = None
    visible_text: str | None = None
    structured_content: Any | None = None
    page_inventory: tuple[BrowserPageSummary, ...] = ()
    target_observations: tuple[BrowserTargetObservation, ...] = ()
    inspection_complete: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def require_html(self) -> str:
        if self.html is None:
            raise ObservationUnavailable("Full HTML observation is unavailable for this browser backend")
        return self.html


@dataclass(frozen=True)
class BrowserActionResult:
    session: SessionHandle
    page: PageHandle
    generation: int
    action: str
    url: str


@dataclass(frozen=True)
class DownloadCaptureSpec:
    """Typed security/provenance constraints for an authorized PDF action."""

    trusted_hosts: tuple[str, ...]
    provenance_host: str
    source_route: str
    expected_url: str | None = None
    require_download_event: bool = False
    allow_response_capture: bool = True
    allow_outside_output_for_tests: bool = False

    def __post_init__(self) -> None:
        hosts = tuple(str(host).casefold().rstrip(".") for host in self.trusted_hosts if str(host).strip())
        if not hosts:
            raise ValueError("DownloadCaptureSpec requires at least one trusted host")
        object.__setattr__(self, "trusted_hosts", hosts)
        if not str(self.provenance_host).strip():
            raise ValueError("DownloadCaptureSpec.provenance_host must not be empty")
        if not str(self.source_route).strip():
            raise ValueError("DownloadCaptureSpec.source_route must not be empty")


class BrowserCommand:
    """Base class for serializable browser intents."""

    kind = "BrowserCommand"

    def as_dict(self) -> dict[str, Any]:
        return {"Kind": self.kind}


@dataclass(frozen=True)
class NavigateCommand(BrowserCommand):
    url: str
    wait_until: str = "domcontentloaded"
    kind = "Navigate"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https", "about", "data", "file"}:
            raise ValueError("NavigateCommand.url must use a supported browser URL scheme")
        if self.wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
            raise ValueError("NavigateCommand.wait_until is unsupported")

    def as_dict(self) -> dict[str, Any]:
        return {"Kind": self.kind, "URL": self.url, "WaitUntil": self.wait_until}


@dataclass(frozen=True)
class ObserveCommand(BrowserCommand):
    include_html: bool = True
    include_visible_text: bool = True
    text_probes: tuple[str, ...] = ()
    max_probe_matches: int = 10
    kind = "Observe"

    def __post_init__(self) -> None:
        if self.max_probe_matches <= 0 or self.max_probe_matches > 50:
            raise ValueError("ObserveCommand.max_probe_matches must be between 1 and 50")
        for probe in self.text_probes:
            try:
                re.compile(probe)
            except re.error as exc:
                raise ValueError("ObserveCommand.text_probes must contain valid regular expressions") from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "Kind": self.kind,
            "IncludeHTML": self.include_html,
            "IncludeVisibleText": self.include_visible_text,
            "TextProbeCount": len(self.text_probes),
            "MaxProbeMatches": self.max_probe_matches,
        }


@dataclass(frozen=True)
class ClickCommand(BrowserCommand):
    target: BrowserTarget
    kind = "Click"

    def as_dict(self) -> dict[str, Any]:
        return {"Kind": self.kind, "Target": self.target.as_dict()}


@dataclass(frozen=True)
class DownloadCommand(BrowserCommand):
    target: BrowserTarget
    suggested_filename: str
    timeout_ms: int = 45_000
    capture: DownloadCaptureSpec | None = None
    kind = "Download"

    def __post_init__(self) -> None:
        if not str(self.suggested_filename).strip():
            raise ValueError("DownloadCommand.suggested_filename must not be empty")
        if self.timeout_ms <= 0:
            raise ValueError("DownloadCommand.timeout_ms must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {
            "Kind": self.kind,
            "Target": self.target.as_dict(),
            "SuggestedFilename": Path(self.suggested_filename).name,
            "TimeoutMs": self.timeout_ms,
            "CaptureMode": "authorized-pdf" if self.capture else "download-event",
        }


@dataclass(frozen=True)
class AuthenticatedFetchCommand(BrowserCommand):
    url: str
    suggested_filename: str
    timeout_ms: int = 45_000
    kind = "AuthenticatedFetch"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("AuthenticatedFetchCommand.url must be an absolute HTTP(S) URL")
        if not str(self.suggested_filename).strip():
            raise ValueError("AuthenticatedFetchCommand.suggested_filename must not be empty")
        if self.timeout_ms <= 0:
            raise ValueError("AuthenticatedFetchCommand.timeout_ms must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {
            "Kind": self.kind,
            "URL": self.url,
            "SuggestedFilename": Path(self.suggested_filename).name,
            "TimeoutMs": self.timeout_ms,
        }


@dataclass(frozen=True)
class AuthenticatedFetchResult:
    url: str
    status: int
    ok: bool
    headers: Mapping[str, str] = field(default_factory=dict)
    artifact: DownloadArtifact | None = None


@dataclass(frozen=True)
class DownloadArtifact:
    """Harness-owned local artifact returned by a download command."""

    artifact_id: str
    suggested_filename: str
    local_path: Path
    size: int
    sha256: str
    status: str = "completed"
    mime_type: str | None = None
    source_url: str | None = None
    page: PageHandle | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        suggested_filename: str | None = None,
        source_url: str | None = None,
        page: PageHandle | None = None,
        mime_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "DownloadArtifact":
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise DownloadFailure(f"Download artifact does not exist: {resolved}")
        digest = hashlib.sha256()
        size = 0
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return cls(
            artifact_id=f"artifact-{uuid.uuid4().hex}",
            suggested_filename=Path(suggested_filename or resolved.name).name,
            local_path=resolved,
            size=size,
            sha256=digest.hexdigest(),
            source_url=source_url,
            page=page,
            mime_type=mime_type,
            metadata=dict(metadata or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ArtifactId": self.artifact_id,
            "SuggestedFilename": self.suggested_filename,
            "LocalPath": str(self.local_path),
            "Size": self.size,
            "SHA256": self.sha256,
            "Status": self.status,
            "MIMEType": self.mime_type,
            "SourceURL": self.source_url,
            "Page": self.page.value if self.page else None,
            "Metadata": dict(self.metadata),
        }


BrowserCommandResult = (
    BrowserObservation
    | BrowserActionResult
    | DownloadArtifact
    | AuthenticatedFetchResult
)


__all__ = [
    "AuthenticatedFetchCommand",
    "AuthenticatedFetchFailure",
    "AuthenticatedFetchResult",
    "BrowserActionResult",
    "BrowserCommand",
    "BrowserCommandError",
    "BrowserCommandResult",
    "BrowserObservation",
    "BrowserPageSummary",
    "BrowserTarget",
    "BrowserTargetObservation",
    "ClickCommand",
    "DownloadArtifact",
    "DownloadCaptureSpec",
    "DownloadCommand",
    "DownloadFailure",
    "InvalidTarget",
    "NavigateCommand",
    "ObservationUnavailable",
    "ObserveCommand",
    "PageHandle",
    "SessionHandle",
    "UnsupportedCommand",
]
