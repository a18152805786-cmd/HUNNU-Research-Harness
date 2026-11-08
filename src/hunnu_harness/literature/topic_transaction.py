"""One cooperating writer for canonical topics, their view, and provenance.

The OS releases the advisory lock after process failure. The persistent empty
lock file is not a corpus input. Acquisition's browser lock is unrelated.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..paths import _windows_io_path, require_output_path

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_local = threading.local()


@contextmanager
def topic_mutation_lock(path: Path) -> Iterator[None]:
    path = require_output_path(path, label="Topic mutation lock")
    key = str(path).casefold()
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(blocking=False):
        raise OSError("TOPIC_MUTATION_BUSY: another topic writer is active")
    held = getattr(_local, "held", set())
    if key in held:
        try:
            yield
        finally:
            lock.release()
        return
    handle = None
    locked = False
    try:
        _windows_io_path(path.parent).mkdir(parents=True, exist_ok=True)
        handle = _windows_io_path(path).open("a+b")
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OSError("TOPIC_MUTATION_BUSY: another topic writer is active") from exc
        locked = True
        _local.held = held | {key}
        yield
    finally:
        try:
            if locked and handle is not None:
                _local.held = held
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    # Closing the handle also releases the OS lock. Cleanup
                    # must not turn a committed transaction into a false fail.
                    pass
        finally:
            try:
                if handle is not None:
                    handle.close()
            finally:
                lock.release()
