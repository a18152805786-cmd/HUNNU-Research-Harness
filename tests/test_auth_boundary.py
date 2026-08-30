"""The authentication boundary: page state in, nothing else.

AGENTS.md 6 and 22 freeze the line -- cookies, storage state, tokens, and
credentials are never inspected or exported -- and ``classify_auth_state()``
is the one authentication oracle, working from visible page state alone.  A
``browser-auth-status`` command briefly crossed that line by reading cookie
metadata (names, hosts, expiry) from the profile's Cookies SQLite; it was
removed rather than reworked, because under the frozen boundary (no cookie
reads, no navigation, no starting a browser) a pre-run probe can only answer
AUTH_UNKNOWN whenever the browser is closed, which is exactly when a pre-run
check happens.  These tests pin the removal so the line cannot quietly
re-cross: the command is gone, the module is gone, and no code path in the
package opens a cookie database or exports storage state.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from hunnu_harness.cli import build_parser

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "hunnu_harness"


def _source_files() -> list[Path]:
    files = sorted(SRC_ROOT.rglob("*.py"))
    assert len(files) > 50, "source tree not found where expected"
    return files


class CommandRemovedTests(unittest.TestCase):
    def test_browser_auth_status_command_does_not_exist(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["browser-auth-status"])

    def test_browser_status_still_reports_the_browser_itself(self) -> None:
        # The browser-state question keeps its one legitimate answerer.
        args = build_parser().parse_args(["browser-status"])
        self.assertEqual(args.command, "browser-status")

    def test_freshness_module_does_not_exist(self) -> None:
        self.assertIsNone(importlib.util.find_spec("hunnu_harness.auth.freshness"))

    def test_the_auth_package_holds_only_the_page_state_oracle(self) -> None:
        modules = sorted(
            path.name for path in (SRC_ROOT / "auth").glob("*.py") if path.name != "__init__.py"
        )
        self.assertEqual(modules, ["state.py"])


class NoCookieCodePathTests(unittest.TestCase):
    """No dormant inspection entry point may remain anywhere in the package."""

    def test_no_module_queries_a_cookie_database(self) -> None:
        # ``host_key`` / ``FROM cookies`` / ``expires_utc`` are the Chrome
        # cookie schema; a module mentioning any of them alongside sqlite3 is
        # reading the cookie store, whatever it claims.  Declarative audit
        # fields like ``"CookiesExported": False`` match none of these.
        for path in _source_files():
            text = path.read_text(encoding="utf-8")
            for marker in ("host_key", "FROM cookies", "expires_utc", "encrypted_value"):
                self.assertNotIn(
                    marker, text, f"{path.name} carries cookie-schema marker {marker!r}"
                )

    def test_no_module_touches_the_profile_cookie_files(self) -> None:
        for path in _source_files():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('"Network" / "Cookies"', text, path.name)
            self.assertNotIn("Network/Cookies", text, path.name)

    def test_no_module_exports_storage_state(self) -> None:
        for path in _source_files():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(".storage_state(", text, path.name)
            self.assertNotIn("storage_state=", text, path.name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
