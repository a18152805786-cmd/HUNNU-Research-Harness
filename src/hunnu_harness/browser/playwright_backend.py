from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..models import AuthStatus, BrowserState


class PlaywrightUnavailable(RuntimeError):
    pass


class ProfileLockedError(RuntimeError):
    pass


class PlaywrightBrowser:
    """Optional Playwright backend using a dedicated persistent profile."""

    def __init__(self, *, profile_dir: Path, downloads_dir: Path, executable_path: Path | None = None, headless: bool = False):
        self.profile_dir = Path(profile_dir)
        self.downloads_dir = Path(downloads_dir)
        self.executable_path = Path(executable_path) if executable_path else None
        self.headless = headless
        self._playwright: Any = None
        self.context: Any = None
        self.page: Any = None

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PlaywrightUnavailable("Install the optional browser extra: python -m pip install -e .[browser]") from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        lock_files = tuple(self.profile_dir.glob("Singleton*"))
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
        self.context = await self._playwright.chromium.launch_persistent_context(**launch_args)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def goto(self, url: str) -> None:
        if not self.page:
            raise RuntimeError("Browser is not started")
        await self.page.goto(url, wait_until="domcontentloaded")

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

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self._playwright:
            await self._playwright.stop()
        self.context = None
        self.page = None
        self._playwright = None

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
        await download.save_as(str(target))
        return target


def run(coro: Any) -> Any:
    """Small CLI helper; libraries should use async APIs directly."""
    return asyncio.run(coro)
