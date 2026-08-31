from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from typing import Any

from ..models import AuthStatus, BrowserState
from ..paths import _logical_path, _windows_io_path


# Titles and opening text a bot-check interstitial shows while it is still
# deciding.  These pages clear themselves; none of them asks the visitor to do
# anything.  A page that wants a human -- a checkbox, a slider, a puzzle --
# does not match here and is left to the source adapter's challenge detection.
_INTERSTITIAL_TITLE_MARKERS = (
    "just a moment",
    "请稍候",
    "請稍候",
    "attention required",
    "checking your browser",
    "one moment",
)
_INTERSTITIAL_BODY_MARKERS = (
    "checking your browser",
    "verifying you are human",
    "verify you are human",
    "正在验证",
    "安全检查",
)

# Gates a person can clear but the Harness must not: a rendered CAPTCHA, a
# slider, an "are you a robot" page.  These are only ever waited on, and only
# when a human-in-the-loop run was explicitly asked for.
_HUMAN_GATE_MARKERS = (
    "are you a robot",
    "captcha",
    "recaptcha",
    "verify you are human",
    "security challenge",
    "机器人",
    "验证码",
    "滑块",
    "安全验证",
)

DEFAULT_INTERSTITIAL_WAIT_SECONDS = 20.0
DEFAULT_INTERSTITIAL_POLL_SECONDS = 0.5
DEFAULT_HUMAN_WAIT_POLL_SECONDS = 1.0

# Consecutive clean polls required before a gate counts as passed.  One sample
# is not enough: Cloudflare reloads its own challenge page, and the gap between
# two rotations reads clean.
DEFAULT_HUMAN_CLEAR_CONFIRMATIONS = 3
RESEARCH_CHROME_ENV = "HUNNU_RESEARCH_CHROME"


def discover_chrome_executable(explicit: Path | str | None = None) -> Path | None:
    """Find a system Chrome executable without requiring one to be installed.

    An explicit CLI path is intentional configuration, so it is returned as-is
    and Playwright will report a useful error if that path is invalid.  Paths
    supplied through the environment or inferred from the conventional Windows
    locations must exist as files before they are selected.
    """

    if explicit is not None:
        return Path(explicit)

    configured = os.environ.get(RESEARCH_CHROME_ENV)
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(os.path.expandvars(configured)).expanduser())

    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        root = os.environ.get(variable)
        if root:
            candidates.append(
                Path(os.path.expandvars(root)).expanduser()
                / "Google"
                / "Chrome"
                / "Application"
                / "chrome.exe"
            )

    return next((candidate for candidate in candidates if candidate.is_file()), None)


class PlaywrightUnavailable(RuntimeError):
    pass


class ProfileLockedError(RuntimeError):
    pass


class PlaywrightBrowser:
    """Optional Playwright backend using a dedicated persistent profile."""

    def __init__(
        self,
        *,
        profile_dir: Path,
        downloads_dir: Path,
        executable_path: Path | None = None,
        headless: bool = False,
        interstitial_wait_seconds: float = DEFAULT_INTERSTITIAL_WAIT_SECONDS,
        interstitial_poll_seconds: float = DEFAULT_INTERSTITIAL_POLL_SECONDS,
        human_wait_seconds: float = 0.0,
        human_wait_poll_seconds: float = DEFAULT_HUMAN_WAIT_POLL_SECONDS,
        human_clear_confirmations: int = DEFAULT_HUMAN_CLEAR_CONFIRMATIONS,
    ):
        self.profile_dir = _logical_path(profile_dir)
        self.downloads_dir = _logical_path(downloads_dir)
        self.executable_path = _logical_path(executable_path) if executable_path else None
        self.headless = headless
        self.interstitial_wait_seconds = interstitial_wait_seconds
        self.interstitial_poll_seconds = interstitial_poll_seconds
        self.human_wait_seconds = human_wait_seconds
        self.human_wait_poll_seconds = human_wait_poll_seconds
        self.human_clear_confirmations = max(1, int(human_clear_confirmations))
        self._playwright: Any = None
        self.context: Any = None
        self.page: Any = None
        self._browser: Any = None
        self._cdp: Any = None
        # Where downloads are sent, and what the browser said about them, when
        # this run attached to a browser instead of launching one.
        self.downloads_directed = False
        self.download_events: list[dict[str, str]] = []
        # True when this run attached to a browser somebody else started.  It
        # decides whether close() may end that browser, and getting it wrong
        # would defeat the point: the first run would kill the signed-in session
        # every later run depends on.
        self.attached = False

    async def lifecycle(self) -> dict[str, Any]:
        """What the browser actually did, for the agent-facing report.

        Every misread in this area came from an Agent inferring browser state
        from the absence of output.  Reporting it removes the guesswork.
        """

        started = self.page is not None
        url = ""
        title = ""
        if started:
            try:
                url = self.page.url or ""
                title = (await self.page.title()) or ""
            except Exception:
                pass
        return {
            "BrowserLaunched": started,
            "BrowserHeadless": self.headless,
            "FinalURL": url,
            "FinalPageTitle": title,
        }

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PlaywrightUnavailable("Install the optional browser extra: python -m pip install -e .[browser]") from exc
        _windows_io_path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        _windows_io_path(self.downloads_dir).mkdir(parents=True, exist_ok=True)

        # A persistent Research Chrome, if one is running, is preferred over a
        # fresh browser: it is holding the institutional session, and launching
        # a second browser on the same profile would both fail on the lock and
        # start out signed in to nothing.
        from .persistent_browser import probe

        persistent = probe()
        if persistent.running:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(persistent.endpoint)
            contexts = self._browser.contexts
            self.context = contexts[0] if contexts else await self._browser.new_context()
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.attached = True
            await self._direct_downloads_here()
            return

        lock_files = tuple(_windows_io_path(self.profile_dir).glob("Singleton*"))
        if lock_files:
            raise ProfileLockedError(f"Dedicated Chrome profile appears locked: {', '.join(p.name for p in lock_files)}")
        self._playwright = await async_playwright().start()
        launch_args: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "accept_downloads": True,
            "downloads_path": str(self.downloads_dir),
        }
        if self.executable_path:
            launch_args["executable_path"] = str(self.executable_path)
        else:
            print(
                "System Chrome was not found; falling back to Playwright's bundled Chromium. "
                "If it is not installed, install Google Chrome or run "
                "python -m playwright install chromium.",
                file=sys.stderr,
                flush=True,
            )
        self.context = await self._playwright.chromium.launch_persistent_context(**launch_args)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def goto(self, url: str) -> None:
        if not self.page:
            raise RuntimeError("Browser is not started")
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.settle_page_gates()

    async def settle_page_gates(self) -> bool:
        """Clear what can be cleared, then optionally wait for a person.

        Two different things gate a page, and they need different answers.  An
        interstitial decides on its own, so waiting is enough and is what any
        browser does.  A rendered challenge -- a CAPTCHA, a slider -- decides
        nothing by itself and is not ours to answer; the only honest options
        are to stop, or, when a human-in-the-loop run was asked for, to hold
        the page open while that person answers it.

        Keeping these separate matters: an unattended run must never sit
        waiting on a challenge nobody is there to solve.
        """

        settled = await self.settle_automated_interstitial()
        if self.human_wait_seconds <= 0:
            return settled
        if not await self._page_is_gated():
            return True
        return await self.await_human_clearance()

    async def settle_automated_interstitial(self) -> bool:
        """Wait out a bot-check interstitial that clears by itself.

        ``domcontentloaded`` returns the moment the interstitial's own DOM is
        ready, which is exactly while the check is still running.  Reading the
        page then yields "Just a moment…" rather than the site, so acquisition
        sees no results and the run ends before the check would have passed.

        This waits, and only waits.  It never clicks, never types, never
        touches a checkbox or slider: passing an interstitial that clears on
        its own is what any browser does, whereas acting on a challenge that
        asks for a human is not ours to do.  A page that is still gated when
        the budget runs out is left exactly as it is, for the source adapter's
        challenge detection to judge.

        Returns True when the page no longer looks like an interstitial.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.interstitial_wait_seconds
        while True:
            if not await self._looks_like_interstitial():
                return True
            if loop.time() >= deadline:
                break
            await asyncio.sleep(self.interstitial_poll_seconds)
        return False

    async def await_human_clearance(self) -> bool:
        """Hold the page open while a person passes the gate themselves.

        Only reached when ``human_wait_seconds`` is set, which is an explicit
        request for a human-in-the-loop run.  The browser stays on screen and
        this polls until the gate is gone; it still never clicks, types, or
        answers anything.  Solving the challenge is the person's action, and
        the Harness only notices that it happened.

        A gate that is still up when the budget expires is left alone and
        reported, exactly as in the unattended path.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.human_wait_seconds
        announced = False
        clear_streak = 0
        while True:
            if await self._page_is_gated():
                clear_streak = 0
            else:
                # Cloudflare rotates its own challenge page (the __cf_chl_rt_tk
                # reload), so a single clean sample lands in the gap between two
                # rotations and is not evidence the gate is gone.  Require the
                # page to stay clear across consecutive polls before continuing.
                clear_streak += 1
                if clear_streak >= self.human_clear_confirmations:
                    return True
            if not announced:
                announced = True
                try:
                    title = (await self.page.title()) or ""
                except Exception:
                    title = ""
                print(
                    "HumanActionRequired=true\n"
                    f"HumanActionReason=PAGE_GATE_AWAITING_HUMAN\n"
                    f"GatePageTitle={title}\n"
                    f"GateURL={getattr(self.page, 'url', '')}\n"
                    f"WaitingSeconds={self.human_wait_seconds:.0f}\n"
                    "Chrome is open. Pass the check in that window; "
                    "acquisition resumes by itself once the page clears.",
                    flush=True,
                )
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(self.human_wait_poll_seconds)

    async def _page_is_gated(self) -> bool:
        """Interstitial *or* a rendered challenge -- a human can clear either."""

        if await self._looks_like_interstitial():
            return True
        try:
            title = (await self.page.title()) or ""
            body = await self.page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        haystack = f"{title} {body[:600]}".casefold()
        return any(marker in haystack for marker in _HUMAN_GATE_MARKERS)

    async def _looks_like_interstitial(self) -> bool:
        try:
            title = (await self.page.title()) or ""
        except Exception:
            return False
        haystack = title.casefold()
        if any(marker in haystack for marker in _INTERSTITIAL_TITLE_MARKERS):
            return True
        try:
            body = await self.page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        head = body[:400].casefold()
        return any(marker in head for marker in _INTERSTITIAL_BODY_MARKERS)

    async def state(self, *, database: str | None = None, module: str | None = None, table: str | None = None) -> BrowserState:
        if not self.page:
            raise RuntimeError("Browser is not started")
        body_text = ""
        try:
            body_text = await self.page.locator("body").inner_text(timeout=3000)
        except Exception:
            pass
        return BrowserState(
            url=self.page.url,
            title=await self.page.title(),
            body_text=body_text[:20000],
            database=database,
            module=module,
            table=table,
            auth_status=AuthStatus.AUTH_UNKNOWN,
        )

    async def _direct_downloads_here(self) -> None:
        """Tell the attached browser to write downloads into this run's directory.

        A browser this process launched is configured through Playwright, and a
        download then arrives as a ``download`` event on the page.  A browser we
        merely attached to is not: the bytes were measured arriving in full, and
        staged, while the event never reached the page being awaited, so the
        acquisition timed out with a complete PDF sitting in a temporary folder.

        Saying where to put the file removes the dependency on that event.  The
        browser-level session is deliberate: ``Browser.downloadWillBegin`` carries
        the URL the bytes actually came from -- for ScienceDirect that is
        ``pdf.sciencedirectassets.com``, not the article host -- and provenance
        must not be traded away for convenience.
        """

        self.download_events = []
        if self._browser is None:
            return
        try:
            session = await self._browser.new_browser_cdp_session()
        except Exception:
            # An older browser without this endpoint keeps the previous
            # behaviour rather than failing the run.
            return
        self._cdp = session
        session.on(
            "Browser.downloadWillBegin",
            lambda event: self.download_events.append(
                {
                    "guid": event.get("guid", ""),
                    "url": event.get("url", ""),
                    "suggested_filename": event.get("suggestedFilename", ""),
                    "state": "begin",
                }
            ),
        )
        session.on(
            "Browser.downloadProgress",
            lambda event: self.download_events.append(
                {"guid": event.get("guid", ""), "state": event.get("state", "")}
            ),
        )
        _windows_io_path(self.downloads_dir).mkdir(parents=True, exist_ok=True)
        try:
            await session.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(self.downloads_dir),
                    "eventsEnabled": True,
                },
            )
            self.downloads_directed = True
        except Exception:
            self.downloads_directed = False

    async def close(self) -> None:
        """End this run's use of the browser without ending the browser.

        When this run attached to a persistent Research Chrome, closing means
        letting go of the connection.  Closing the context instead would take
        the signed-in session down with it, which is precisely the failure this
        whole arrangement exists to prevent -- so the attached case only stops
        the Playwright driver, and Chrome carries on.
        """

        if self.attached:
            if self._playwright:
                await self._playwright.stop()
        else:
            if self.context:
                await self.context.close()
            if self._playwright:
                await self._playwright.stop()
        self.context = None
        self.page = None
        self._playwright = None
        self._browser = None
        self.attached = False

    async def click_text(self, text: str, *, exact: bool = True) -> None:
        if not self.page:
            raise RuntimeError("Browser is not started")
        locator = self.page.get_by_text(text, exact=exact).first
        await locator.click()

    async def fill_by_label(self, label: str, value: str) -> None:
        if not self.page:
            raise RuntimeError("Browser is not started")
        await self.page.get_by_label(label, exact=False).first.fill(value)

    async def select_options_by_label(self, label: str, values: list[str]) -> None:
        if not self.page:
            raise RuntimeError("Browser is not started")
        await self.page.get_by_label(label, exact=False).first.select_option(values)

    async def download_by_text(self, text: str, *, timeout_ms: int = 30000) -> Path:
        if not self.page:
            raise RuntimeError("Browser is not started")
        async with self.page.expect_download(timeout=timeout_ms) as download_info:
            await self.click_text(text)
        download = await download_info.value
        target = self.downloads_dir / (download.suggested_filename or "download.bin")
        await download.save_as(str(_windows_io_path(target)))
        return target


def run(coro: Any) -> Any:
    """Small CLI helper; libraries should use async APIs directly."""
    return asyncio.run(coro)
