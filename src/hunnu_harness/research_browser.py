"""Convenience lifecycle functions for the dedicated research browser."""

from __future__ import annotations

from pathlib import Path

from .browser.session import ResearchBrowser


_active_browser: ResearchBrowser | None = None


async def start(*, profile_dir: Path, downloads_dir: Path, chrome_executable: Path | None = None, headless: bool = False) -> ResearchBrowser:
    global _active_browser
    if _active_browser is not None:
        return _active_browser
    _active_browser = ResearchBrowser(profile_dir=profile_dir, downloads_dir=downloads_dir, chrome_executable=chrome_executable, headless=headless)
    await _active_browser.start()
    return _active_browser


async def status():
    if _active_browser is None:
        return None
    return await _active_browser.status()


async def stop() -> None:
    global _active_browser
    if _active_browser is not None:
        await _active_browser.stop()
        _active_browser = None
