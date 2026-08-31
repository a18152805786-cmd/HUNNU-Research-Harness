from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hunnu_harness.browser.playwright_backend import (
    RESEARCH_CHROME_ENV,
    PlaywrightBrowser,
    discover_chrome_executable,
)
from hunnu_harness.cli import build_parser as build_harness_parser
from hunnu_harness.literature.cli import build_parser as build_literature_parser
from hunnu_harness.paths import TEMP_DIR


class ChromeDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_explicit_cli_path_has_highest_priority(self) -> None:
        explicit = Path("E:/tools/custom-chrome.exe")
        with patch.dict(
            os.environ,
            {RESEARCH_CHROME_ENV: "E:/env/chrome.exe", "ProgramFiles": "E:/program-files"},
            clear=True,
        ):
            self.assertEqual(discover_chrome_executable(explicit), explicit)

    def test_environment_path_wins_over_common_install_locations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="chrome-discovery-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            configured = root / "configured-chrome.exe"
            common = root / "program-files" / "Google" / "Chrome" / "Application" / "chrome.exe"
            configured.write_bytes(b"")
            common.parent.mkdir(parents=True, exist_ok=True)
            common.write_bytes(b"")
            with patch.dict(
                os.environ,
                {RESEARCH_CHROME_ENV: str(configured), "ProgramFiles": str(common.parents[3])},
                clear=True,
            ):
                self.assertEqual(discover_chrome_executable(), configured)

    def test_common_install_locations_are_checked_in_documented_order(self) -> None:
        with tempfile.TemporaryDirectory(prefix="chrome-discovery-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            program_files = root / "program-files"
            program_files_x86 = root / "program-files-x86"
            local_app_data = root / "local-app-data"
            expected = program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe"
            expected.parent.mkdir(parents=True, exist_ok=True)
            expected.write_bytes(b"")
            with patch.dict(
                os.environ,
                {
                    "ProgramFiles": str(program_files),
                    "ProgramFiles(x86)": str(program_files_x86),
                    "LocalAppData": str(local_app_data),
                },
                clear=True,
            ):
                self.assertEqual(discover_chrome_executable(), expected)

    def test_missing_chrome_returns_none(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(discover_chrome_executable())

    def test_both_cli_defaults_use_discovered_chrome_and_explicit_cli_wins(self) -> None:
        with tempfile.TemporaryDirectory(prefix="chrome-discovery-", dir=TEMP_DIR) as temporary:
            configured = Path(temporary) / "configured-chrome.exe"
            configured.write_bytes(b"")
            with patch.dict(os.environ, {RESEARCH_CHROME_ENV: str(configured)}):
                harness_args = build_harness_parser().parse_args(["browser-start"])
                literature_args = build_literature_parser().parse_args(
                    ["live-cnki", "--title", "example"]
                )
                explicit = Path(temporary) / "explicit-chrome.exe"
                explicit_args = build_harness_parser().parse_args(
                    ["browser-start", "--chrome", str(explicit)]
                )

            self.assertEqual(harness_args.chrome, configured)
            self.assertEqual(literature_args.chrome, configured)
            self.assertEqual(explicit_args.chrome, explicit)

    def test_bundled_chromium_fallback_explains_how_to_install_it(self) -> None:
        class FakeContext:
            def __init__(self) -> None:
                self.pages: list[object] = []
                self.closed = False

            async def new_page(self) -> object:
                page = object()
                self.pages.append(page)
                return page

            async def close(self) -> None:
                self.closed = True

        class FakePlaywright:
            def __init__(self) -> None:
                self.chromium = self
                self.context: FakeContext | None = None

            async def launch_persistent_context(self, **kwargs: object) -> FakeContext:
                self.context = FakeContext()
                return self.context

            async def stop(self) -> None:
                return None

        class Starter:
            def __init__(self, playwright: FakePlaywright) -> None:
                self.playwright = playwright

            async def start(self) -> FakePlaywright:
                return self.playwright

        with tempfile.TemporaryDirectory(prefix="chrome-discovery-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            browser = PlaywrightBrowser(
                profile_dir=root / "profile",
                downloads_dir=root / "downloads",
            )
            fake = FakePlaywright()
            stderr = io.StringIO()
            with patch(
                "hunnu_harness.browser.persistent_browser.probe",
                return_value=SimpleNamespace(running=False),
            ), patch("playwright.async_api.async_playwright", return_value=Starter(fake)):
                with redirect_stderr(stderr):
                    try:
                        asyncio.run(browser.start())
                    finally:
                        asyncio.run(browser.close())

            self.assertIn("System Chrome was not found", stderr.getvalue())
            self.assertIn("Google Chrome", stderr.getvalue())
            self.assertIn("python -m playwright install chromium", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
