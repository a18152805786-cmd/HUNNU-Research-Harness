from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import BrowserState, DownloadRequest


class UnexpectedPageState(RuntimeError):
    pass


class DatabaseAdapter(ABC):
    name: str

    def __init__(self, browser: Any):
        self.browser = browser

    @abstractmethod
    async def detect(self) -> BrowserState:
        raise NotImplementedError

    @abstractmethod
    async def open(self) -> BrowserState:
        raise NotImplementedError

    @abstractmethod
    async def download(self, request: DownloadRequest) -> Any:
        raise NotImplementedError
