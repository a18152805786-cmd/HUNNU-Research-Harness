from __future__ import annotations

import asyncio
from pathlib import Path

from ..auth.state import AuthenticationController, ManualLoginRequired, classify_auth_state
from ..models import AuthStatus, BrowserState
from .playwright_backend import PlaywrightBrowser


class ResearchBrowser:
    def __init__(self, *, profile_dir: Path, downloads_dir: Path, chrome_executable: Path | None = None, headless: bool = False):
        self.backend = PlaywrightBrowser(profile_dir=profile_dir, downloads_dir=downloads_dir, executable_path=chrome_executable, headless=headless)
        self._last_state = BrowserState()
        self.auth = AuthenticationController(self._read_state)

    def _read_state(self) -> BrowserState:
        return self._last_state

    async def start(self) -> None:
        await self.backend.start()

    async def status(self) -> BrowserState:
        state = await self.backend.state()
        state = BrowserState(**{**state.__dict__, "auth_status": classify_auth_state(state)})
        self._last_state = state
        self.auth.status = state.auth_status
        return state

    async def check_auth_status(self) -> AuthStatus:
        return (await self.status()).auth_status

    async def wait_for_manual_login(self, *, timeout_seconds: float = 300, poll_seconds: float = 2) -> AuthStatus:
        self.auth.status = AuthStatus.AUTH_IN_PROGRESS
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            current = await self.check_auth_status()
            if current == AuthStatus.AUTH_SUCCESS:
                return current
            if current == AuthStatus.SESSION_EXPIRED:
                self.auth.status = current
                raise ManualLoginRequired("Manual login is required because the session expired.")
            await asyncio.sleep(poll_seconds)
        self.auth.status = AuthStatus.AUTH_REQUIRED
        raise ManualLoginRequired("ManualLoginRequired=true; human authentication did not complete before timeout.")

    async def resume_after_login(self) -> BrowserState:
        state = await self.status()
        if state.auth_status != AuthStatus.AUTH_SUCCESS:
            raise ManualLoginRequired("ManualLoginRequired=true; complete school authentication before resuming.")
        return state

    async def stop(self) -> None:
        await self.backend.close()
