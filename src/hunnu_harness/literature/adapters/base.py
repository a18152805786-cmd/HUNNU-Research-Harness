from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any

from ...browser.authorized_file_capture import (
    AcquisitionMethod,
    AuthorizedFileCaptureResult,
)
from ...browser.commands import DownloadArtifact
from ...browser.port import BrowserCommandPort, ensure_browser_command_port
from ...browser.transport import BrowserTransportError
from ..models import AccessDecision, LiteratureRecord, LiteratureSearchRequest, RunStatus


class LiteratureSourceError(RuntimeError):
    status = RunStatus.SOURCE_UNAVAILABLE


class HumanActionReason(str, Enum):
    """Why a human is needed, kept separate from what was actually seen."""

    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    VISIBLE_CHALLENGE = "VISIBLE_CHALLENGE"
    CHALLENGE_TEXT_UNVERIFIED = "CHALLENGE_TEXT_UNVERIFIED"
    UNSPECIFIED = "UNSPECIFIED"


class SourceActionRequired(LiteratureSourceError):
    """A human must act before acquisition can continue.

    The reason and the evidence behind it are separate facts.  Challenge words
    in a page body are not a seen challenge: the same scan fires on a footer
    that mentions CAPTCHAs and on a paper whose own title is about them.  A
    verdict may only claim a visible challenge when a live browser observation
    actually reported one, so ``challenge_observed`` records whether the HTML
    came from an observed page at all, and ``challenge_visible`` whether that
    observation found the challenge rendered and blocking.

    Static analysis therefore yields CHALLENGE_TEXT_UNVERIFIED, never
    VISIBLE_CHALLENGE, and an Agent must not upgrade the former into a claim
    that the user is looking at a CAPTCHA.
    """

    status = RunStatus.ACTION_REQUIRED_USER_LOGIN

    def __init__(
        self,
        message: str,
        *,
        reason: "HumanActionReason" = HumanActionReason.UNSPECIFIED,
        challenge_observed: bool = False,
        challenge_visible: bool = False,
        challenge_blocking: bool = False,
        browser_ready_for_manual_action: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.challenge_observed = challenge_observed
        # A challenge cannot be visible or blocking unless it was observed.
        self.challenge_visible = challenge_visible and challenge_observed
        self.challenge_blocking = challenge_blocking and challenge_observed
        self.browser_ready_for_manual_action = browser_ready_for_manual_action

    def as_dict(self) -> dict[str, Any]:
        return {
            "HumanActionRequired": True,
            "HumanActionReason": self.reason.value,
            "ChallengeActuallyObserved": self.challenge_observed,
            "ChallengeVisible": self.challenge_visible,
            "ChallengeBlocking": self.challenge_blocking,
            "BrowserReadyForManualAction": self.browser_ready_for_manual_action,
            "Detail": str(self),
        }


class SourceUserDownloadRequired(LiteratureSourceError):
    status = RunStatus.ACTION_REQUIRED_USER_DOWNLOAD

    def __init__(self, message: str, *, handoff_state: Any = None) -> None:
        super().__init__(message)
        self.handoff_state = handoff_state


class SourceLayoutChanged(LiteratureSourceError):
    status = RunStatus.SOURCE_LAYOUT_CHANGED


class SourceUnavailable(LiteratureSourceError):
    status = RunStatus.SOURCE_UNAVAILABLE


class LiteratureSourceAdapter(ABC):
    """Source-specific browser contract, kept separate from v0.1 data adapters."""

    name: str
    human_like_delay_seconds: float = 1.0
    supports_search: bool = True
    supports_fulltext_access_check: bool = True
    supports_authorized_download: bool = True
    supports_unattended_download: bool = False
    supports_preflight: bool = False

    def __init__(self, browser: BrowserCommandPort | Any | None):
        # ``None`` remains valid for parser-only/finalizer construction.  Any
        # live adapter path is normalized here so source adapters only ever
        # see the command port, including when an old v0.2.16 transport is
        # supplied through the compatibility API.
        if browser is None:
            self.browser = None
        else:
            try:
                self.browser = ensure_browser_command_port(browser)
            except BrowserTransportError:
                # Parser/preflight-only callers from v0.2.16 sometimes
                # construct an adapter with a sentinel object and never enter
                # live execution.  Preserve construction compatibility, but
                # leave the sentinel untouched: the factory/broker validates
                # the command port before any workflow is created, and a live
                # method cannot silently fall back to it.
                self.browser = browser

    @abstractmethod
    async def search(
        self,
        query: str,
        request: LiteratureSearchRequest,
    ) -> list[LiteratureRecord]:
        raise NotImplementedError

    @abstractmethod
    async def open_result(self, record: LiteratureRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    async def extract_metadata(self, *, search_query: str) -> LiteratureRecord:
        raise NotImplementedError

    @abstractmethod
    async def extract_abstract(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def check_fulltext_access(self) -> AccessDecision:
        raise NotImplementedError

    @abstractmethod
    async def download_fulltext(self, record: LiteratureRecord, access: AccessDecision) -> Path:
        raise NotImplementedError

    @abstractmethod
    async def get_citation(self) -> dict[str, Any]:
        raise NotImplementedError


def authorized_capture_result_from_artifact(
    artifact: DownloadArtifact,
) -> AuthorizedFileCaptureResult:
    """Rehydrate legacy capture metadata without exposing browser objects.

    The existing workflow records ``AuthorizedFileCaptureResult`` details.
    The command layer transports those details as sanitized artifact metadata,
    so this small compatibility conversion keeps the downstream record schema
    stable while adapters remain page/locator/context free.
    """

    metadata = dict(artifact.metadata)
    try:
        method = AcquisitionMethod(str(metadata["AcquisitionMethod"]))
        source_host = str(metadata["SourceHost"])
        source_route = str(metadata["SourceRoute"])
    except (KeyError, ValueError) as exc:
        raise SourceUnavailable("Authorized browser artifact is missing capture provenance") from exc
    return AuthorizedFileCaptureResult(
        path=artifact.local_path,
        acquisition_method=method,
        source_host=source_host,
        source_route=source_route,
        download_event_emitted=bool(metadata.get("DownloadEventEmitted", False)),
        authorized_pdf_response_captured=bool(metadata.get("AuthorizedPDFResponseCaptured", False)),
    )
