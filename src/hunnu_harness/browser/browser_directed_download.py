"""Catching a download the browser performs for us, when we only attached to it.

A browser this process launched hands Playwright a ``download`` event and the
file follows.  A persistent browser we merely attached to does not: the bytes
were measured arriving complete -- 3.7 MB, a valid PDF -- while the event never
reached the page being awaited, so the file was never saved and the run reported
that no download had happened at all.

Rather than wait for an event that does not come, this tells the browser where
to put the file and then watches the browser's own account of it.  The
browser-level protocol is what makes that safe: ``Browser.downloadWillBegin``
names both the URL the bytes actually come from and the browser's suggested
filename.  Identity must match the URL's recognised paper identifier, the exact
stem of a suggested ``.pdf``/``.caj`` filename, or a sufficiently long
bibliographic label explicitly declared by the adapter; a file merely appearing
in a directory remains insufficient.

A publisher may redirect its PDF to a delivery URL that no longer carries the
article identifier.  The delivery host therefore remains an explicit
allowlist, and host, completion-state, and landed-file checks still run after
the download has been claimed by one of these identity sources.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
import weakref
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..paths import _windows_io_path

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

# Intentional allowlist widening: attached-mode adapters that issue a bare
# DownloadCommand have no DownloadCaptureSpec.trusted_hosts declaration channel.
# CNKI's vetted single-paper controls deliver through these explicit hosts.
# Live acceptance on 2026-09-01 showed that bar.cnki.net may redirect a claimed
# single-paper order to docdown.cnki.net, which serves the actual bytes.
DIRECTED_DOWNLOAD_DEFAULT_HOSTS = ELSEVIER_PDF_HOSTS | frozenset(
    {"bar.cnki.net", "download.cnki.net", "docdown.cnki.net"}
)

_PII_IN_URL = re.compile(r"[?&]pii=([A-Za-z0-9]+)", re.IGNORECASE)
_PII_IN_PATH = re.compile(r"/pii/([A-Za-z0-9]+)", re.IGNORECASE)
_OUP_ARTICLE_PDF_PATH = re.compile(
    r"/article-pdf/(?:[^/?#]+/)+([A-Za-z0-9][A-Za-z0-9._-]*)\.pdf$",
    re.IGNORECASE,
)
_CNKI_ORDER_HOST = "bar.cnki.net"
_CNKI_ORDER_PATH = "/bar/download/order"
_DIRECTED_DOWNLOAD_IDENTITY_SUFFIXES = frozenset({".pdf", ".caj"})

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

    Existing ``pii=`` and ``/pii/`` forms keep priority.  The only additional
    identities are an OUP main-article PDF stem and a CNKI single-paper order
    id; an arbitrary PDF filename remains insufficient.
    """

    value = url or ""
    for pattern in (_PII_IN_URL, _PII_IN_PATH):
        found = pattern.search(value)
        if found:
            return found.group(1)

    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""

    found = _OUP_ARTICLE_PDF_PATH.search(parsed.path)
    if found:
        return found.group(1)

    host = (parsed.hostname or "").casefold().rstrip(".")
    if host == _CNKI_ORDER_HOST and parsed.path.casefold() == _CNKI_ORDER_PATH:
        for key, candidate in parse_qsl(parsed.query, keep_blank_values=True):
            if key.casefold() == "id" and candidate:
                return candidate
    return ""


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _host_is_allowed(host: str, allowed: frozenset[str]) -> bool:
    """Match exact hosts plus explicitly declared, dot-prefixed suffixes.

    A suffix entry such as ``.silverchair.com`` matches only subdomains via
    ``endswith``; it intentionally does not authorize the bare apex domain.
    """

    normalized = host.casefold().rstrip(".")
    return any(
        normalized.endswith(entry) if entry.startswith(".") else normalized == entry
        for entry in allowed
    )


@dataclass
class _Pending:
    guid: str
    url: str
    suggested_filename: str
    state: str = "begin"


def _suggested_filename_identity(suggested_filename: str) -> str:
    name = Path(suggested_filename).name
    if not name:
        return ""
    path = Path(name)
    if path.suffix.casefold() not in _DIRECTED_DOWNLOAD_IDENTITY_SUFFIXES:
        return ""
    return path.stem


def _normalize_declared_identity(value: str) -> str:
    """Reduce a declared label, or a filename stem, to what a filename keeps.

    Chrome names a download after the publisher's suggestion and then rewrites
    every character Windows forbids in a filename -- ``: / \\ ? * " < > |`` and
    control characters -- as ``_`` before it writes the file; other platforms
    substitute differently, and a publisher may have dropped or replaced
    punctuation of its own before the browser saw the name.  A subtitle colon,
    routine in Chinese academic titles, is therefore ``:`` in the record and
    ``_`` on disk, and a comparison that keeps punctuation fails on that one
    character while every other one agrees.  That is how a complete, valid
    CNKI PDF came to be reported as no download at all.

    Letters, digits and combining marks are what survive the trip unchanged,
    so they are all that is compared: NFKC first, so a full-width colon or
    digit folds with its ASCII form; casefold; then everything outside the
    Unicode categories L, M and N is dropped from both sides.  The category
    test is deliberate -- to ``\\w`` an underscore is a word character, so a
    strip built on ``\\W`` keeps the ``_`` Chrome wrote while removing the
    ``:`` it replaced, and the two strings still differ.
    """

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char for char in normalized if unicodedata.category(char)[0] in "LMN"
    )


def _matching_declared_label(
    pending: _Pending, locked_labels: tuple[str, ...]
) -> str:
    if not locked_labels:
        return ""
    filename_identity = _suggested_filename_identity(pending.suggested_filename)
    if not filename_identity:
        return ""
    normalized_stem = _normalize_declared_identity(filename_identity)
    for label in locked_labels:
        normalized_label = _normalize_declared_identity(label)
        if (
            normalized_label
            and len(normalized_label) >= 6
            and normalized_label in normalized_stem
        ):
            return label
    return ""


def _pending_claimed_identity(
    pending: _Pending,
    locked_pii: str,
    locked_labels: tuple[str, ...] = (),
) -> str:
    """Return the first bounded event identity source naming the target."""

    locked = locked_pii.casefold()
    if locked:
        url_pii = pii_from_url(pending.url)
        if url_pii.casefold() == locked:
            return url_pii
        filename_identity = _suggested_filename_identity(
            pending.suggested_filename
        )
        if filename_identity.casefold() == locked:
            return filename_identity
    return _matching_declared_label(pending, locked_labels)


def _pending_names_locked_identity(
    pending: _Pending,
    locked_pii: str,
    locked_labels: tuple[str, ...] = (),
) -> bool:
    return bool(_pending_claimed_identity(pending, locked_pii, locked_labels))


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
    locked_labels: tuple[str, ...] = ()
    allowed_hosts: frozenset[str] = DIRECTED_DOWNLOAD_DEFAULT_HOSTS
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
        _windows_io_path(self.download_dir).mkdir(parents=True, exist_ok=True)
        self._target = asyncio.get_running_loop().create_future()
        self._armed = True
        self.session.on("Browser.downloadWillBegin", self._on_will_begin)
        self.session.on("Browser.downloadProgress", self._on_progress)
        await self.session.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(_windows_io_path(self.download_dir)),
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
        # Every download is recorded, and only the one naming this paper in a
        # bounded event identity source is adopted.  A second, unrelated
        # download starting in the same browser must not be mistaken for the
        # one being waited on.
        self._pending[guid] = pending
        if self._target is None or self._target.done():
            return
        if _pending_names_locked_identity(
            pending, self.locked_pii, self.locked_labels
        ):
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
            # Name what did arrive.  A download this guard declined to claim
            # is still bytes in the run directory, and an operator reading
            # "started no download" while the PDF sits there will spend a
            # second fetch on it -- the very thing the fetch budget exists to
            # prevent.  Filenames only: a delivery URL can carry a signed
            # token, which has no business in a report.
            unclaimed = [
                Path(p.suggested_filename).name or "<unnamed>"
                for p in self._pending.values()
            ]
            detail = ""
            if unclaimed:
                shown = ", ".join(repr(name) for name in unclaimed[:3])
                more = f", +{len(unclaimed) - 3} more" if len(unclaimed) > 3 else ""
                detail = (
                    f"; other downloads seen: {len(unclaimed)}; "
                    f"not claimed for this target: {shown}{more}"
                )
            raise DirectedDownloadFailure(
                f"{DirectedDownloadOutcome.DOWNLOAD_EVENT_TIMEOUT.value}: the authorized "
                f"control started no download naming PII {self.locked_pii}; "
                f"declared labels: {len(self.locked_labels)}{detail}"
            ) from exc

        host = host_of(pending.url)
        if not _host_is_allowed(host, self.allowed_hosts):
            raise DirectedDownloadFailure(
                f"{DirectedDownloadOutcome.DOWNLOAD_SOURCE_HOST_REJECTED.value}: "
                f"{host or 'unknown host'} is not a recognised publisher PDF origin"
            )
        url_pii = pii_from_url(pending.url)
        suggested_name = Path(pending.suggested_filename).name
        source_pii = _pending_claimed_identity(
            pending, self.locked_pii, self.locked_labels
        )
        if not source_pii:
            raise DirectedDownloadFailure(
                f"{DirectedDownloadOutcome.DOWNLOAD_SOURCE_IDENTITY_MISMATCH.value}: "
                f"download URL names {url_pii or 'no PII'} and suggested filename "
                f"is {suggested_name!r}; declared labels: {len(self.locked_labels)}; "
                f"locked target is {self.locked_pii}"
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
        if not _windows_io_path(path).is_file():
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
    "DIRECTED_DOWNLOAD_DEFAULT_HOSTS",
    "ELSEVIER_PDF_HOSTS",
    "BrowserDirectedDownload",
    "DirectedDownloadFailure",
    "DirectedDownloadOutcome",
    "DirectedDownloadResult",
    "host_of",
    "lease_for",
    "pii_from_url",
]
