from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class AuthStatus(str, Enum):
    AUTH_UNKNOWN = "AUTH_UNKNOWN"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_IN_PROGRESS = "AUTH_IN_PROGRESS"
    AUTH_SUCCESS = "AUTH_SUCCESS"
    SESSION_EXPIRED = "SESSION_EXPIRED"


@dataclass(frozen=True)
class BrowserState:
    url: str = ""
    title: str = ""
    body_text: str = ""
    database: str | None = None
    module: str | None = None
    table: str | None = None
    auth_status: AuthStatus = AuthStatus.AUTH_UNKNOWN


@dataclass(frozen=True)
class DownloadRequest:
    database: str
    module: str
    table: str
    stocks: tuple[str, ...] = ()
    date_start: str | None = None
    date_end: str | None = None
    fields: tuple[str, ...] = ()
    output_format: str = "csv"
    source_url: str = ""


@dataclass
class DownloadRecord:
    database: str
    institution: str
    access_type: str
    download_time: str
    module: str
    table: str
    query: dict[str, Any]
    original_filename: str
    original_path: str
    archived_path: str
    sha256: str
    source_url: str
    agent: str = "HUNNU Research Harness"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "institution": self.institution,
            "access_type": self.access_type,
            "download_time": self.download_time,
            "module": self.module,
            "table": self.table,
            "query": self.query,
            "original_filename": self.original_filename,
            "original_path": self.original_path,
            "archived_path": self.archived_path,
            "sha256": self.sha256,
            "source_url": self.source_url,
            "agent": self.agent,
            "notes": self.notes,
        }
