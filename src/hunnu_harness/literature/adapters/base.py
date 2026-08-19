from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ...browser.transport import BrowserTransport
from ..models import AccessDecision, LiteratureRecord, LiteratureSearchRequest, RunStatus


class LiteratureSourceError(RuntimeError):
    status = RunStatus.SOURCE_UNAVAILABLE


class SourceActionRequired(LiteratureSourceError):
    status = RunStatus.ACTION_REQUIRED_USER_LOGIN


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

    def __init__(self, browser: BrowserTransport):
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
