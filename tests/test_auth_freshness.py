"""Institutional session freshness from cookie metadata.

The gauge reads host, name, expiry, and persistence -- never a value.  These
tests build fake cookie databases under TEMP_DIR that *do* carry values
(sentinel strings), precisely so the reports can be proven never to leak one;
they also pin the fail-toward-asking posture: no database, no entitlement,
a lapsed or imminent expiry, and session-only cookies all recommend signing
in again.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hunnu_harness.auth.freshness import (
    RELOGIN_THRESHOLD_HOURS,
    AuthFreshnessReport,
    EntitlementCookie,
    read_auth_freshness,
)
from hunnu_harness.cli import build_parser
from hunnu_harness.paths import TEMP_DIR

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
VALUE_SENTINEL = "SECRET-COOKIE-VALUE-SENTINEL"
ENCRYPTED_SENTINEL = b"ENCRYPTED-COOKIE-SENTINEL"


def _chrome_us(moment: datetime) -> int:
    return int((moment - _CHROME_EPOCH).total_seconds() * 1_000_000)


def _profile(root: Path, cookies: list[tuple[str, str, datetime | None]]) -> Path:
    """A fake Research Chrome profile whose cookies all carry sentinel values."""

    profile = root / "chrome-profile"
    database = profile / "Default" / "Network" / "Cookies"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE cookies ("
        "host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
        "expires_utc INTEGER, is_persistent INTEGER)"
    )
    for host, name, expires in cookies:
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?)",
            (
                host,
                name,
                VALUE_SENTINEL,
                ENCRYPTED_SENTINEL,
                _chrome_us(expires) if expires else 0,
                1 if expires else 0,
            ),
        )
    connection.commit()
    connection.close()
    return profile


def _tmp(test: unittest.TestCase) -> Path:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    holder = tempfile.TemporaryDirectory(prefix="auth-fresh-", dir=TEMP_DIR)
    test.addCleanup(holder.cleanup)
    return Path(holder.name)


class FreshnessReadingTests(unittest.TestCase):
    def test_fresh_entitlement_reports_hours_and_no_relogin(self) -> None:
        profile = _profile(
            _tmp(self),
            [
                ("id.elsevier.com", "id.inst", NOW + timedelta(hours=40)),
                (".sciencedirect.com", "SD_REMOTEACCESS", NOW + timedelta(days=300)),
                (".oup.com", "cf_clearance", NOW + timedelta(days=300)),
            ],
        )
        report = read_auth_freshness(profile, now=lambda: NOW)
        self.assertTrue(report.readable)
        self.assertTrue(report.entitlement_present)
        # The soonest expiry is THE expiry: entitlement is a chain.
        self.assertAlmostEqual(report.hours_remaining, 40.0, places=3)
        self.assertTrue(report.cloudflare_present)
        self.assertFalse(report.relogin_recommended)

    def test_missing_entitlement_recommends_relogin(self) -> None:
        profile = _profile(
            _tmp(self),
            [(".google.com", "NID", NOW + timedelta(days=100))],
        )
        report = read_auth_freshness(profile, now=lambda: NOW)
        self.assertTrue(report.readable)
        self.assertFalse(report.entitlement_present)
        self.assertEqual(report.as_dict()["InstitutionalEntitlementExpiresAt"], "unknown")
        self.assertTrue(report.relogin_recommended)

    def test_imminent_expiry_recommends_relogin(self) -> None:
        profile = _profile(
            _tmp(self),
            [("id.elsevier.com", "id.inst", NOW + timedelta(hours=RELOGIN_THRESHOLD_HOURS / 2))],
        )
        report = read_auth_freshness(profile, now=lambda: NOW)
        self.assertTrue(report.entitlement_present)
        self.assertTrue(report.relogin_recommended)

    def test_lapsed_entitlement_recommends_relogin(self) -> None:
        profile = _profile(
            _tmp(self),
            [("id.elsevier.com", "id.inst", NOW - timedelta(hours=5))],
        )
        report = read_auth_freshness(profile, now=lambda: NOW)
        self.assertTrue(report.entitlement_present)
        self.assertLess(report.hours_remaining, 0)
        self.assertTrue(report.relogin_recommended)

    def test_session_only_entitlement_cannot_promise_a_later_run(self) -> None:
        profile = _profile(_tmp(self), [("id.elsevier.com", "id.inst", None)])
        report = read_auth_freshness(profile, now=lambda: NOW)
        self.assertTrue(report.entitlement_present)
        self.assertIsNone(report.hours_remaining)
        self.assertEqual(report.session_cookies, 1)
        self.assertTrue(report.relogin_recommended)

    def test_host_matching_holds_the_suffix_boundary(self) -> None:
        profile = _profile(
            _tmp(self),
            [
                # A lookalike host must not count as the entitlement.
                ("id.elsevier.com.attacker.example", "id.inst", NOW + timedelta(days=2)),
                ("notcnki.net", "Ecp_LoginStuts", NOW + timedelta(days=2)),
                # The dotted form of the real host does count.
                (".id.elsevier.com", "id.inst", NOW + timedelta(days=2)),
            ],
        )
        report = read_auth_freshness(profile, now=lambda: NOW)
        self.assertEqual(len(report.entitlements), 1)
        self.assertEqual(report.entitlements[0].host, ".id.elsevier.com")


class NoValueEverTests(unittest.TestCase):
    def test_report_never_contains_a_cookie_value(self) -> None:
        profile = _profile(
            _tmp(self),
            [
                ("id.elsevier.com", "id.inst", NOW + timedelta(hours=40)),
                (".cnki.net", "Ecp_LoginStuts", NOW + timedelta(days=10)),
                (".oup.com", "cf_clearance", None),
            ],
        )
        report = read_auth_freshness(profile, now=lambda: NOW)
        rendered = json.dumps(report.as_dict(), ensure_ascii=False, default=str)
        self.assertNotIn(VALUE_SENTINEL, rendered)
        self.assertNotIn(ENCRYPTED_SENTINEL.decode("ascii"), rendered)
        self.assertIs(report.as_dict()["CookieValuesRead"], False)

    def test_the_cookie_dataclass_cannot_even_hold_a_value(self) -> None:
        self.assertEqual(
            {item.name for item in fields(EntitlementCookie)},
            {"host", "name", "expires_at", "persistent"},
        )


class DegradedProfileTests(unittest.TestCase):
    def test_locked_database_is_still_read_immutably(self) -> None:
        """A running Chrome holds the database; immutable mode reads anyway."""

        profile = _profile(
            _tmp(self), [("id.elsevier.com", "id.inst", NOW + timedelta(hours=40))]
        )
        database = profile / "Default" / "Network" / "Cookies"
        holder = sqlite3.connect(database, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        report = read_auth_freshness(profile, now=lambda: NOW)
        self.assertTrue(report.readable)
        self.assertEqual(report.read_mode, "immutable-read-only")
        self.assertTrue(report.entitlement_present)

    def test_unreadable_database_is_reported_not_retried(self) -> None:
        root = _tmp(self)
        database = root / "chrome-profile" / "Default" / "Network" / "Cookies"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"this is not a sqlite database at all")
        report = read_auth_freshness(root / "chrome-profile", now=lambda: NOW)
        self.assertFalse(report.readable)
        self.assertNotEqual(report.read_error, "")
        self.assertTrue(report.relogin_recommended)

    def test_missing_profile_is_reported_as_fact(self) -> None:
        report = read_auth_freshness(_tmp(self) / "nowhere", now=lambda: NOW)
        self.assertFalse(report.readable)
        self.assertIsNone(report.cookie_database)
        self.assertEqual(report.as_dict()["CookieDatabase"], "NOT_FOUND")
        self.assertTrue(report.relogin_recommended)

    def test_older_profile_layout_is_found(self) -> None:
        root = _tmp(self)
        profile = _profile(root, [("id.elsevier.com", "id.inst", NOW + timedelta(hours=40))])
        modern = profile / "Default" / "Network" / "Cookies"
        legacy = profile / "Default" / "Cookies"
        modern.rename(legacy)
        (profile / "Default" / "Network").rmdir()
        report = read_auth_freshness(profile, now=lambda: NOW)
        self.assertTrue(report.readable)
        self.assertEqual(report.cookie_database, legacy)


class CLISurfaceTests(unittest.TestCase):
    def test_command_exists_and_takes_a_profile(self) -> None:
        args = build_parser().parse_args(
            ["browser-auth-status", "--profile", "C:/Research/profile"]
        )
        self.assertEqual(args.command, "browser-auth-status")
        self.assertEqual(Path(args.profile), Path("C:/Research/profile"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
