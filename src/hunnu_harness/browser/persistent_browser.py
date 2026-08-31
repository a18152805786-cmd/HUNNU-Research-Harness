"""A dedicated Research Chrome that outlives a single Harness run.

ScienceDirect issues its authenticated session as cookies with no expiry, so the
session lives in the browser process and dies with it.  Every acquisition
command is its own process that launched Chrome, worked, and closed it in a
``finally``, which meant a signed-in session could never survive one command --
each later run arrived as an anonymous off-campus visitor, which is what the
publisher's bot gate was reacting to.

Chrome's own "continue where you left off" preference does not fix this: it was
measured, and Playwright's ``launch_persistent_context`` does not start Chrome
through the session-restore path, so the session cookies are still dropped.

What does work is not restarting the browser at all.  Chrome is started here as
a detached operating-system process holding a debugging port, and later runs
attach to it over CDP rather than launching their own.  The session stays in
that browser's memory, exactly as it does in a browser a person leaves open, and
nothing resembling a credential is ever written anywhere new.

The port is opened only when someone explicitly starts this browser.  Ordinary
runs attach if it happens to be there and otherwise launch a private browser as
before, so the Harness never opens a debugging port on its own initiative.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .pdf_preferences import DEFAULT_RESEARCH_CHROME_PROFILE
from ..paths import _logical_path, _windows_io_path

# Not Chrome's conventional 9222: a Harness browser should not silently adopt,
# or be adopted by, some other tool's debugging session.
DEFAULT_RESEARCH_DEBUG_PORT = 9333
RESEARCH_DEBUG_PORT_ENV = "HUNNU_RESEARCH_DEBUG_PORT"
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0


class PersistentBrowserError(RuntimeError):
    pass


def research_debug_port() -> int:
    raw = os.environ.get(RESEARCH_DEBUG_PORT_ENV)
    if not raw:
        return DEFAULT_RESEARCH_DEBUG_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise PersistentBrowserError(f"{RESEARCH_DEBUG_PORT_ENV} is not a port number: {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise PersistentBrowserError(f"{RESEARCH_DEBUG_PORT_ENV} is out of range: {port}")
    return port


def endpoint_for(port: int) -> str:
    # Loopback only.  The address is stated rather than left to Chrome's default
    # so the intent is visible: this port is never offered to the network.
    return f"http://127.0.0.1:{port}"


@dataclass(frozen=True)
class PersistentBrowserStatus:
    running: bool
    endpoint: str
    port: int
    browser_version: str = ""
    profile_dir: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "PersistentBrowserRunning": self.running,
            "PersistentBrowserEndpoint": self.endpoint,
            "PersistentBrowserPort": self.port,
            "PersistentBrowserVersion": self.browser_version,
            "PersistentBrowserProfile": self.profile_dir,
        }


def probe(port: int | None = None, *, timeout_seconds: float = 2.0) -> PersistentBrowserStatus:
    """Ask whether a Research Chrome is already listening, without starting one."""

    resolved = research_debug_port() if port is None else port
    endpoint = endpoint_for(resolved)
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return PersistentBrowserStatus(running=False, endpoint=endpoint, port=resolved)
    return PersistentBrowserStatus(
        running=True,
        endpoint=endpoint,
        port=resolved,
        browser_version=str(payload.get("Browser", "")),
    )


def _spawn_detached(command: list[str]) -> None:
    """Start Chrome so that it is not a child this process can take down with it."""

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        command,
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=os.name != "nt",
    )


def start_persistent_browser(
    *,
    profile_dir: Path | None = None,
    chrome_executable: Path,
    port: int | None = None,
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    spawn: object = None,
) -> PersistentBrowserStatus:
    """Start the dedicated browser, or report the one already running.

    Deliberately not idempotent by force: an already-running browser is returned
    untouched rather than restarted, because restarting it would throw away the
    signed-in session this exists to keep.
    """

    resolved_port = research_debug_port() if port is None else port
    existing = probe(resolved_port)
    if existing.running:
        return existing

    profile = _logical_path(Path(profile_dir or DEFAULT_RESEARCH_CHROME_PROFILE).expanduser())
    locks = tuple(_windows_io_path(profile).glob("Singleton*"))
    if locks:
        raise PersistentBrowserError(
            "Research Chrome profile is already held by a browser without a debugging port; "
            "close it before starting the persistent one"
        )
    _windows_io_path(profile).mkdir(parents=True, exist_ok=True)
    chrome = Path(chrome_executable)
    if not _windows_io_path(chrome).is_file():
        raise PersistentBrowserError(f"Chrome executable not found: {chrome}")

    command = [
        str(chrome),
        f"--remote-debugging-port={resolved_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    (spawn or _spawn_detached)(command)

    deadline = time.monotonic() + startup_timeout_seconds
    while time.monotonic() < deadline:
        status = probe(resolved_port)
        if status.running:
            return PersistentBrowserStatus(
                running=True,
                endpoint=status.endpoint,
                port=status.port,
                browser_version=status.browser_version,
                profile_dir=str(profile),
            )
        time.sleep(0.25)
    raise PersistentBrowserError(
        f"Research Chrome did not open its debugging port within {startup_timeout_seconds:g}s"
    )


__all__ = [
    "DEFAULT_RESEARCH_DEBUG_PORT",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "PersistentBrowserError",
    "PersistentBrowserStatus",
    "RESEARCH_DEBUG_PORT_ENV",
    "endpoint_for",
    "probe",
    "research_debug_port",
    "start_persistent_browser",
]
