"""Catching a download the browser performs for us, when we only attached to it.

A browser this process launched hands Playwright a ``download`` event and the
file follows.  A persistent browser we merely attached to does not: the bytes
were measured arriving complete -- 3.7 MB, a valid PDF -- while the event never
reached the page being awaited, so the file was never saved and the run reported
that no download had happened at all.

Rather than wait for an event that does not come, this tells the browser where
to put the file and then watches the browser's own account of it.  The
browser-level protocol is what makes that safe: ``Browser.downloadWillBegin``
names the URL the bytes actually come from, so nothing here is accepted on the
strength of a filename or a file merely appearing in a directory.

Identity is checked against the URL and nothing else.  A publisher serves its
PDFs from a delivery host that is not the article host -- ScienceDirect uses
``pdf.sciencedirectassets.com`` -- so the host check is an explicit allowlist
rather than "same host as the article", and the paper's identifier must appear
in the URL of the very download being accepted.
"""

from __future__ import annotations

import asyncio
import re
import weakref
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Where a publisher is allowed to serve the bytes from.  Kept explicit and
# small: widening it is a decision someone should have to make on purpose.
ELSEVIER_PDF_HOSTS = frozenset(
    {
        "www.sciencedirect.com",
        "sciencedirect.com",
        "pdf.sciencedirectassets.com",
        "ars.els-cdn.com",
    }
)

_PII_IN_URL = re.compile(r"[?&]pii=([A-Za-z0-9]+)", re.IGNORECASE)
_PII_IN_PATH = re.compile(r"/pii/([A-Za-z0-9]+)", re.IGNORECASE)

DEFAULT_WILL_BEGIN_TIMEOUT_SECONDS = 45.0
DEFAULT_COMPLETION_TIMEOUT_SECONDS = 120.0
DEFAULT_SETTLE_SECONDS = 1.0


class DirectedDownloadFailure(RuntimeError):
    """A directed download did not happen, or did not happen for this paper."""


class DirectedDownloadOutcome(str, Enum):
    COMPLETED = "COMPLETED"
    DOWNLOAD_EVENT_TIMEOUT = "DOWNLOAD_EVENT_TIMEOUT"
    DOWNLOAD_COMPLETION_TIMEOUT = "DOWNLOAD_COMPLETION_TIMEOUT"
    DOWNLOAD_CANCELED = "DOWNLOAD_CANCELED"
    DOWNLOAD_SOURCE_IDENTITY_MISMATCH = "DOWNLOAD_SOURCE_IDENTITY_MISMATCH"
    DOWNLOAD_SOURCE_HOST_REJECTED = "DOWNLOAD_SOURCE_HOST_REJECTED"
    DOWNLOAD_FILE_MISSING = "DOWNLOAD_FILE_MISSING"


@dataclass(frozen=True)
class DirectedDownloadResult:
    path: Path
    guid: str
    source_url_host: str
    source_pii: str
    suggested_filename: str
    outcome: DirectedDownloadOutcome = DirectedDownloadOutcome.COMPLETED

    def as_dict(self) -> dict[str, Any]:
        return {
            "DownloadGUID": self.guid,
            "DownloadSourceHost": self.source_url_host,
            "DownloadSourcePII": self.source_pii,
            "SuggestedFilename": self.suggested_filename,
            "DownloadProgressState": self.outcome.value,
        }


def pii_from_url(url: str) -> str:
    """The paper identifier a download URL names, or empty when it names none.

    The query parameter is preferred over the path: a delivery URL carries the
    article's own identifier in ``pii=`` while its path is the storage layout.
    """

    for pattern in (_PII_IN_URL, _PII_IN_PATH):
        found = pattern.search(url or "")
        if found:
            return found.group(1)
    return ""


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


@dataclass
class _Pending:
    guid: str
    url: str
    suggested_filename: str
    state: str = "begin"


class _DownloadLease:
    """One directed download at a time, per browser.

    The persistent browser is shared by every run, and download behaviour is a
    property of the browser rather than of a run.  Two runs pointing it at their
    own directories at once would each be liable to catch the other's file, so
    they take turns.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.holder: str | None = None

    async def acquire(self, holder: str) -> None:
        await self._lock.acquire()
        self.holder = holder

    def release(self) -> None:
        self.holder = None
        if self._lock.locked():
            self._lock.release()

    @property
    def held(self) -> bool:
        return self._lock.locked()


# Keyed by the browser object itself, not its id: CPython reuses an id once the
# object behind it is collected, so an id-keyed table can hand a brand new
# browser the lease -- possibly a held one -- belonging to a dead predecessor.
_LEASES: "weakref.WeakKeyDictionary[Any, _DownloadLease]" = weakref.WeakKeyDictionary()


# The fallback for anything that cannot be weakly referenced.  Sharing one lease
# serialises more than strictly necessary, which is the safe direction to err in:
# the alternative is two runs aiming the same browser at different directories.
_SHARED_LEASE = _DownloadLease()


def lease_for(browser: Any) -> _DownloadLease:
    try:
        lease = _LEASES.get(browser)
    except TypeError:
        return _SHARED_LEASE
    if lease is None:
        lease = _DownloadLease()
        try:
            _LEASES[browser] = lease
        except TypeError:
            return _SHARED_LEASE
    return lease


@dataclass
class BrowserDirectedDownload:
    """Point an attached browser's downloads at one run, and watch what lands."""

    session: Any
    download_dir: Path
    locked_pii: str
    allowed_hosts: frozenset[str] = ELSEVIER_PDF_HOSTS
    will_begin_timeout: float = DEFAULT_WILL_BEGIN_TIMEOUT_SECONDS
    completion_timeout: float = DEFAULT_COMPLETION_TIMEOUT_SECONDS
    settle_seconds: float = DEFAULT_SETTLE_SECONDS
    lease: _DownloadLease | None = None
    _pending: dict[str, _Pending] = field(default_factory=dict)
    _target: asyncio.Future | None = None
    _armed: bool = False
    configured: bool = False

    # -- lifecycle -------------------------------------------------------

    async def arm(self) -> None:
        """Take the lease, aim the browser at this run, and start listening."""

        if self.lease is not None:
            await self.lease.acquire(str(self.download_dir))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._target = asyncio.get_running_loop().create_future()
        self._armed = True
        self.session.on("Browser.downloadWillBegin", self._on_will_begin)
        self.session.on("Browser.downloadProgress", self._on_progress)
        await self.session.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(self.download_dir),
                "eventsEnabled": True,
            },
        )
        self.configured = True

    async def release(self) -> None:
        """Stop listening and stop aiming the browser at a run that is over.

        Leaving the policy in place would drop somebody's later download into a
        finished run's folder, which is both confusing and a way for one run's
        directory to accumulate files nobody asked it to hold.
        """

        self._armed = False
        for event, handler in (
            ("Browser.downloadWillBegin", self._on_will_begin),
            ("Browser.downloadProgress", self._on_progress),
        ):
            remove = getattr(self.session, "remove_listener", None)
            if callable(remove):
                try:
                    remove(event, handler)
                except Exception:
                    pass
        if self.configured:
            try:
                await self.session.send(
                    "Browser.setDownloadBehavior", {"behavior": "default"}
                )
            except Exception:
                pass
            self.configured = False
        if self.lease is not None:
            self.lease.release()

    # -- events ----------------------------------------------------------

    def _on_will_begin(self, event: dict) -> None:
        if not self._armed:
            return
        guid = str(event.get("guid", ""))
        pending = _Pending(
            guid=guid,
            url=str(event.get("url", "")),
            suggested_filename=str(event.get("suggestedFilename", "")),
        )
        # Every download is recorded, and only the one naming this paper is
        # adopted.  A second, unrelated download starting in the same browser
        # must not be mistaken for the one being waited on.
        self._pending[guid] = pending
        if self._target is None or self._target.done():
            return
        if pii_from_url(pending.url).casefold() == self.locked_pii.casefold():
            self._target.set_result(pending)

    def _on_progress(self, event: dict) -> None:
        if not self._armed:
            return
        guid = str(event.get("guid", ""))
        pending = self._pending.get(guid)
        if pending is not None:
            pending.state = str(event.get("state", pending.state))

    # -- waiting ---------------------------------------------------------

    async def await_download(self) -> DirectedDownloadResult:
        """Wait for this paper's download, and prove it is this paper's."""

        if self._target is None:
            raise DirectedDownloadFailure("Directed download was never armed")
        try:
            pending = await asyncio.wait_for(
                asyncio.shield(self._target), timeout=self.will_begin_timeout
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            others = [p.url for p in self._pending.values()]
            detail = (
                f"; other downloads seen: {len(others)}" if others else ""
            )
            raise DirectedDownloadFailure(
                f"{DirectedDownloadOutcome.DOWNLOAD_EVENT_TIMEOUT.value}: the authorized "
                f"control started no download naming PII {self.locked_pii}{detail}"
            ) from exc

        host = host_of(pending.url)
        if host not in self.allowed_hosts:
            raise DirectedDownloadFailure(
                f"{DirectedDownloadOutcome.DOWNLOAD_SOURCE_HOST_REJECTED.value}: "
                f"{host or 'unknown host'} is not a recognised publisher PDF origin"
            )
        source_pii = pii_from_url(pending.url)
        if source_pii.casefold() != self.locked_pii.casefold():
            raise DirectedDownloadFailure(
                f"{DirectedDownloadOutcome.DOWNLOAD_SOURCE_IDENTITY_MISMATCH.value}: "
                f"download names {source_pii or 'no PII'}, locked target is {self.locked_pii}"
            )

        state = await self._await_completion(pending)
        if state != "completed":
            outcome = (
                DirectedDownloadOutcome.DOWNLOAD_CANCELED
                if state in {"canceled", "cancelled", "interrupted"}
                else DirectedDownloadOutcome.DOWNLOAD_COMPLETION_TIMEOUT
            )
            raise DirectedDownloadFailure(
                f"{outcome.value}: the browser reported state {state!r} for this download"
            )

        path = self.download_dir / Path(pending.suggested_filename).name
        if not path.is_file():
            raise DirectedDownloadFailure(
                f"{DirectedDownloadOutcome.DOWNLOAD_FILE_MISSING.value}: "
                f"the browser reported completion but {path.name} is not in the run directory"
            )
        return DirectedDownloadResult(
            path=path,
            guid=pending.guid,
            source_url_host=host,
            source_pii=source_pii,
            suggested_filename=pending.suggested_filename,
        )

    async def _await_completion(self, pending: _Pending) -> str:
        """Only the browser saying 'completed' counts, for this GUID alone."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.completion_timeout
        while loop.time() < deadline:
            if pending.state in {"completed", "canceled", "cancelled", "interrupted"}:
                if pending.state == "completed":
                    # The browser announces completion as it finishes closing
                    # the file; give the write a moment to land.
                    await asyncio.sleep(self.settle_seconds)
                return pending.state
            await asyncio.sleep(0.25)
        return pending.state


__all__ = [
    "DEFAULT_COMPLETION_TIMEOUT_SECONDS",
    "DEFAULT_WILL_BEGIN_TIMEOUT_SECONDS",
    "ELSEVIER_PDF_HOSTS",
    "BrowserDirectedDownload",
    "DirectedDownloadFailure",
    "DirectedDownloadOutcome",
    "DirectedDownloadResult",
    "host_of",
    "lease_for",
    "pii_from_url",
]
