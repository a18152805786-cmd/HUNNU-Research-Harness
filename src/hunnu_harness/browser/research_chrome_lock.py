"""One Harness process at a time drives the Research Chrome.

Everything a run does to the Research Chrome is shared with every other run:
an attached run takes over the browser's first tab, points the browser-wide
download directory at its own staging folder, and listens to browser-wide
download events.  Two runs at once therefore navigate each other's page and
receive each other's PDFs -- and the fetch and search ledgers, whose
read-wait-append steps are not atomic, let both through the same pacing gap.
The ledgers document the premise this lock makes true: "this Harness has one
serial workflow".

The lock is an operating-system file lock on ``Output Root/audit/
research_chrome.lock``.  The OS drops it when the holding process exits, so a
crash cannot leave a stale lock behind, and nothing ever has to be deleted by
hand.  Within one process it is re-entrant: a batch holds it across all of its
items, and each item's browser start/close nests inside that hold.

A refusal never waits.  An agent that wants several papers should put them in
one ``acquire-batch`` queue, which runs them one after another under a single
hold; a second process that finds the lock taken is told who holds it and
stops.

TO THE MODIFYING AGENT: this lock is what keeps parallel acquisition from
corrupting downloads and slipping past the pacing ledgers.  Making it wait,
time out into a pass, or apply only to some entry points weakens a guard, and
requires asking the user first, in so many words.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import AUDIT_DIR, _logical_path, _windows_io_path, require_output_path

LOCK_PATH = AUDIT_DIR / "research_chrome.lock"
_HOLDER_SUFFIX = ".holder.json"


class ResearchChromeBusy(RuntimeError):
    """Another Harness process is driving the Research Chrome right now."""


@dataclass
class _Hold:
    fd: int
    count: int
    holder_path: Path


_holds: dict[str, _Hold] = {}
_guard = threading.Lock()


def _key(path: Path) -> str:
    return os.path.normcase(str(_logical_path(path)))


def _try_os_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _os_unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _read_holder(holder_path: Path) -> str:
    try:
        payload = json.loads(_windows_io_path(holder_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "holder unknown"
    return (
        f"pid {payload.get('pid', 'unknown')}, since {payload.get('since', 'unknown')}, "
        f"{payload.get('purpose', 'unknown purpose')}"
    )


def acquire(purpose: str, *, path: Path | None = None) -> Path:
    """Take the Research Chrome lock for this process, or raise ResearchChromeBusy.

    Returns the lock path, which is what ``release`` takes.  A process that
    already holds the lock only deepens its hold.
    """

    lock_path = require_output_path(Path(path or LOCK_PATH), label="Research Chrome lock")
    key = _key(lock_path)
    with _guard:
        held = _holds.get(key)
        if held is not None:
            held.count += 1
            return lock_path
        io_path = _windows_io_path(lock_path)
        io_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(io_path), os.O_RDWR | os.O_CREAT, 0o644)
        if not _try_os_lock(fd):
            os.close(fd)
            holder = _read_holder(lock_path.with_name(lock_path.name + _HOLDER_SUFFIX))
            raise ResearchChromeBusy(
                "Another Harness acquisition is driving the Research Chrome "
                f"({holder}). Wait for it to finish; to fetch several papers, put "
                "them in one `hunnu-harness acquire-batch` queue instead of "
                "starting runs side by side."
            )
        holder_path = lock_path.with_name(lock_path.name + _HOLDER_SUFFIX)
        try:
            _windows_io_path(holder_path).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "since": datetime.now(timezone.utc).isoformat(),
                        "purpose": str(purpose)[:200],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            # The holder note only explains a refusal to the next process;
            # the lock itself is already held.
            pass
        _holds[key] = _Hold(fd=fd, count=1, holder_path=holder_path)
        return lock_path


def release(path: Path) -> None:
    """Undo one ``acquire``; the OS lock goes when the outermost hold ends."""

    key = _key(path)
    with _guard:
        held = _holds.get(key)
        if held is None:
            return
        held.count -= 1
        if held.count > 0:
            return
        del _holds[key]
        try:
            _windows_io_path(held.holder_path).unlink()
        except OSError:
            pass
        _os_unlock(held.fd)
        os.close(held.fd)


def held_here(path: Path | None = None) -> bool:
    """Whether this process currently holds the lock at ``path``."""

    with _guard:
        return _key(Path(path or LOCK_PATH)) in _holds


def _release_all_for_tests() -> None:
    """Drop every hold this process has; for test teardown only."""

    with _guard:
        holds: list[Any] = list(_holds.values())
        _holds.clear()
    for held in holds:
        _os_unlock(held.fd)
        try:
            os.close(held.fd)
        except OSError:
            pass


__all__ = [
    "LOCK_PATH",
    "ResearchChromeBusy",
    "acquire",
    "held_here",
    "release",
]
