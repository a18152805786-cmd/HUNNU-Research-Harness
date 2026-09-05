"""A dedicated Research Chrome that outlives a single Harness run.

ScienceDirect issues its authenticated session as cookies with no expiry, so the
session lives in the browser process and dies with it.  Every acquisition
command is its own process that launched Chrome, worked, and closed it in a
``finally``, which meant a signed-in session could never survive one command --
each later run arrived as an anonymous off-campus visitor, which is what the
publisher's bot gate was reacting to.

What works while the browser is up is not restarting it at all.  Chrome is
started here as a detached operating-system process holding a debugging port,
and later runs attach to it over CDP rather than launching their own.  The
session stays in that browser's memory, exactly as it does in a browser a
person leaves open, and nothing resembling a credential is ever written
anywhere new.

Across a stop and a start, the sign-in survives only if Chrome comes back
through its session-restore path: session cookies are always persisted to the
profile, but at startup Chrome deletes them unless it is continuing the
previous session (or recovering from a crash).  Editing the profile's
``session.restore_on_startup`` preference was measured not to achieve this,
and the signature of that failure names the cause: the edit verified at once
and the whole ``session`` object came back empty after one graceful lifecycle,
while the sibling ``plugins.always_open_pdf_externally`` edit survived.  On
Windows that key is one of Chrome's tracked preferences, kept in ``Secure
Preferences`` behind a machine-bound MAC, and at the next start Chrome
migrates any copy found in ``Preferences`` out of that file.  The Harness
cannot produce that MAC and must not try.  So the browser is started with
``--restore-last-session`` instead, Chrome's own switch for "continue where
you left off": it overrides the preference, needs no edit to the profile, and
is visible on the process command line.

The port is opened only when someone explicitly starts this browser.  Ordinary
runs attach if it happens to be there and otherwise launch a private browser as
before -- with the same switch, so a run on the shared profile does not delete
the cookies the persistent browser saved -- and the Harness never opens a
debugging port on its own initiative.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .pdf_preferences import DEFAULT_RESEARCH_CHROME_PROFILE, _default_profile_process_check
from ..paths import _logical_path, _windows_io_path

# Not Chrome's conventional 9222: a Harness browser should not silently adopt,
# or be adopted by, some other tool's debugging session.
DEFAULT_RESEARCH_DEBUG_PORT = 9333
RESEARCH_DEBUG_PORT_ENV = "HUNNU_RESEARCH_DEBUG_PORT"
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0
# Chrome's own switch for "continue where you left off".  It overrides the
# session.restore_on_startup preference -- the one the Harness cannot set on
# Windows, where that key is tracked, MAC-protected and migrated out of the
# Preferences file at the next start -- and makes Chrome restore the previous
# session's cookies instead of deleting them at startup.
SESSION_RESTORE_SWITCH = "--restore-last-session"
# How long a stop waits for Chrome to leave after being asked.  A graceful
# exit writes the session and the Preferences file first, and the child
# processes go a moment after the debugging port does.
DEFAULT_EXIT_TIMEOUT_SECONDS = 30.0
_EXIT_POLL_SECONDS = 0.25


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
    # True when this call started the browser with the session-restore
    # switch, False when it started it without, None when the browser was
    # already running -- its command line is not this call's to know.
    session_restore: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "PersistentBrowserRunning": self.running,
            "PersistentBrowserEndpoint": self.endpoint,
            "PersistentBrowserPort": self.port,
            "PersistentBrowserVersion": self.browser_version,
            "PersistentBrowserProfile": self.profile_dir,
            "PersistentBrowserSessionRestore": (
                "unknown" if self.session_restore is None else self.session_restore
            ),
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
    restore_last_session: bool = True,
) -> PersistentBrowserStatus:
    """Start the dedicated browser, or report the one already running.

    Deliberately not idempotent by force: an already-running browser is returned
    untouched rather than restarted, because restarting it would throw away the
    signed-in session this exists to keep.

    ``restore_last_session`` passes Chrome's ``--restore-last-session`` so the
    previous session's cookies -- the institutional sign-in -- are restored
    rather than deleted at startup.  On by default because keeping that
    sign-in is what this browser is for; off only for a deliberately fresh
    start.
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
    ]
    if restore_last_session:
        command.append(SESSION_RESTORE_SWITCH)
    command.append("about:blank")
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
                session_restore=restore_last_session,
            )
        time.sleep(0.25)
    raise PersistentBrowserError(
        f"Research Chrome did not open its debugging port within {startup_timeout_seconds:g}s"
    )


def profile_in_use(profile_dir: Path | None = None) -> bool | None:
    """Whether any Chrome process holds the profile; None when that is unknowable.

    The debugging port answers for the browser this Harness started.  The
    profile is a separate fact: a Chrome started by hand holds it without any
    port, and after a stop the child processes let go of it a moment after the
    port closes.  A preference edit or a fresh start needs the profile, not
    the port, to be free.
    """

    profile = _logical_path(Path(profile_dir or DEFAULT_RESEARCH_CHROME_PROFILE).expanduser())
    return _default_profile_process_check(profile)


async def _close_over_cdp(endpoint: str) -> None:
    """Ask the browser to exit the way its own close button would.

    ``Browser.close`` is the protocol's graceful shutdown: Chrome saves its
    session and rewrites Preferences with ``exit_type`` Normal on the way out,
    which a hard kill of the process skips.  Playwright's ``Browser.close()``
    on a browser it merely connected to is deliberately not that -- it clears
    the contexts it created and disconnects -- and calling it here is how the
    old command left Chrome running while reporting it stopped.
    """

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(endpoint)
        try:
            session = await browser.new_browser_cdp_session()
            try:
                await session.send("Browser.close")
            except Exception:
                # The browser leaving drops the connection under the request,
                # which the driver may surface as an error.  Whether it left
                # is answered by probing the port afterwards, not by this call.
                pass
        finally:
            try:
                await browser.close()
            except Exception:
                pass


def _request_graceful_close(endpoint: str) -> None:
    asyncio.run(_close_over_cdp(endpoint))


@dataclass(frozen=True)
class PersistentBrowserStopResult:
    """What a stop can honestly say happened, fact by fact.

    ``stopped`` is claimed only when the browser was asked to leave and then
    did: the port went quiet and no Chrome process holds the profile any
    more.  ``ok`` is the end state the operator wanted regardless of who
    brought it about -- nothing listening and the profile free -- so that a
    stop finding nothing to do can still say whether the next command can
    proceed.  An unavailable process check is reported as unknown; it cannot
    veto, because the port going quiet is real evidence and a missing check
    is not evidence of anything.
    """

    endpoint: str
    port: int
    was_running: bool
    close_requested: bool
    running_after: bool
    profile_in_use: bool | None
    waited_seconds: float
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.running_after and self.profile_in_use is not True

    @property
    def stopped(self) -> bool:
        return self.close_requested and self.ok

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "PersistentBrowserEndpoint": self.endpoint,
            "PersistentBrowserPort": self.port,
            "PersistentBrowserWasRunning": self.was_running,
            "BrowserCloseRequested": self.close_requested,
            "PersistentBrowserRunning": self.running_after,
            "PersistentBrowserStopped": self.stopped,
            "ProfileInUse": "unknown" if self.profile_in_use is None else self.profile_in_use,
            "ExitWaitSeconds": round(self.waited_seconds, 2),
        }
        if self.reason:
            payload["Reason"] = self.reason
        return payload


def stop_persistent_browser(
    *,
    profile_dir: Path | None = None,
    port: int | None = None,
    exit_timeout_seconds: float | None = None,
    request_close: Callable[[str], None] | None = None,
    profile_in_use_check: Callable[[Path | None], bool | None] | None = None,
) -> PersistentBrowserStopResult:
    """Close the dedicated browser gracefully and report only what was confirmed.

    The old command reported ``PersistentBrowserStopped=true`` on the strength
    of having disconnected from Chrome, while the main process and a dozen
    children carried on; the documented next step then refused with "profile
    is in use" and the operator closed Chrome by hand.  Here the browser is
    asked to exit through the protocol, and "stopped" means the debugging
    port has gone quiet and the profile is no longer held -- checked, not
    inferred from the request having been sent.  A browser that does not
    leave within the wait (it may be holding a dialog over an unfinished
    download) is reported as still running, so nothing downstream builds on
    a stop that did not happen.
    """

    resolved_port = research_debug_port() if port is None else port
    endpoint = endpoint_for(resolved_port)
    profile = _logical_path(Path(profile_dir or DEFAULT_RESEARCH_CHROME_PROFILE).expanduser())
    in_use = profile_in_use_check or profile_in_use
    # Read from the module at call time rather than bound as a default, so
    # the wait is one adjustable place instead of a value frozen at import.
    if exit_timeout_seconds is None:
        exit_timeout_seconds = DEFAULT_EXIT_TIMEOUT_SECONDS

    if not probe(resolved_port).running:
        held = in_use(profile)
        reason = "No persistent Research Chrome is listening"
        if held is True:
            reason += (
                ", but a Chrome without a debugging port still holds the profile; "
                "close that window by hand before editing the profile or starting the persistent one"
            )
        return PersistentBrowserStopResult(
            endpoint=endpoint,
            port=resolved_port,
            was_running=False,
            close_requested=False,
            running_after=False,
            profile_in_use=held,
            waited_seconds=0.0,
            reason=reason,
        )

    try:
        (request_close or _request_graceful_close)(endpoint)
    except Exception as exc:
        # The request could not be made at all (no driver, connection
        # refused).  That is a different fact from the browser declining to
        # leave, and it is reported as such rather than as a traceback.
        return PersistentBrowserStopResult(
            endpoint=endpoint,
            port=resolved_port,
            was_running=True,
            close_requested=False,
            running_after=probe(resolved_port).running,
            profile_in_use=None,
            waited_seconds=0.0,
            reason=f"Could not ask Research Chrome to close: {type(exc).__name__}: {exc}",
        )

    started = time.monotonic()
    deadline = started + exit_timeout_seconds
    running = True
    held: bool | None = None
    while True:
        running = probe(resolved_port, timeout_seconds=1.0).running
        if not running:
            # Children release the profile a moment after the port closes;
            # only once the port is quiet is the profile worth asking about.
            held = in_use(profile)
            if held is not True:
                break
        if time.monotonic() >= deadline:
            break
        time.sleep(_EXIT_POLL_SECONDS)
    waited = time.monotonic() - started

    reason = ""
    if running:
        reason = (
            f"Research Chrome is still listening {waited:.0f}s after Browser.close was sent; "
            "it may be holding a dialog (an unfinished download, an unload prompt) -- "
            "finish or dismiss it in the window, then run browser-stop again"
        )
    elif held is True:
        reason = (
            f"The debugging port closed but Chrome processes still held the profile "
            f"{waited:.0f}s later; wait for them to exit before editing the profile"
        )
    return PersistentBrowserStopResult(
        endpoint=endpoint,
        port=resolved_port,
        was_running=True,
        close_requested=True,
        running_after=running,
        profile_in_use=held,
        waited_seconds=waited,
        reason=reason,
    )


__all__ = [
    "DEFAULT_EXIT_TIMEOUT_SECONDS",
    "DEFAULT_RESEARCH_DEBUG_PORT",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "PersistentBrowserError",
    "PersistentBrowserStatus",
    "PersistentBrowserStopResult",
    "RESEARCH_DEBUG_PORT_ENV",
    "SESSION_RESTORE_SWITCH",
    "endpoint_for",
    "probe",
    "profile_in_use",
    "research_debug_port",
    "start_persistent_browser",
    "stop_persistent_browser",
]
