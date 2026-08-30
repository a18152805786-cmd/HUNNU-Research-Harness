"""Institutional session freshness, read from cookie *metadata* only.

The institutional entitlement cookies expire on the order of two days
(``id.inst`` on ``id.elsevier.com`` most prominently), and until now the only
way to discover a stale sign-in was to start an acquisition run and hit the
SSO redirect halfway through -- wasting the run and, since the fetch budget
counts attempts, download allowance too.  This module answers "is the sign-in
still fresh" *before* a run starts, offline.

What it reads, and the hard lines it never crosses:

* It opens the dedicated Research Chrome profile's cookie database read-only
  and **immutable** (``mode=ro&immutable=1``), so a database held open by a
  running Chrome can still be read without taking any lock; the price is a
  possibly slightly stale snapshot, which the report says out loud.  If the
  open or the query fails anyway, that is reported as fact -- never retried
  against a writable handle.
* It selects ``host_key``, ``name``, ``expires_utc``, ``is_persistent`` and
  nothing else.  Cookie *values* -- plain or encrypted -- are never read,
  never logged, never returned.
* It never refreshes, injects, or copies a cookie, and never starts a browser
  or navigates anywhere.  Deciding to sign in again is the user's move; this
  is the gauge, not the hand.

Freshness is judged on a small, explicit table of entitlement cookies.  The
soonest expiry among those present is *the* expiry: entitlement is a chain,
and the first link to lapse is when the next run starts failing.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# Chrome stores times as microseconds since 1601-01-01 UTC.
_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

# The cookies that carry the institutional entitlement, as (host suffix,
# cookie name).  Observed on the real Research Chrome profile; extend the
# table when a new source joins the harness.
INSTITUTIONAL_ENTITLEMENT_COOKIES: tuple[tuple[str, str], ...] = (
    ("id.elsevier.com", "id.inst"),
    ("id.elsevier.com", "id.wayf.result.acct"),
    ("sciencedirect.com", "SD_REMOTEACCESS"),
    ("oauth2.hunnu.edu.cn", "token"),
    ("cnki.net", "Ecp_LoginStuts"),
    ("cnki.net", "c_m_expire"),
)

# Cloudflare gate cookies: their absence does not break entitlement, but a
# missing clearance often means a challenge before the first page.
CLOUDFLARE_COOKIE_NAMES = ("cf_clearance", "__cf_bm")

# Below this many hours of remaining entitlement, recommend signing in again
# before starting a run: an acquisition batch takes hours, and an entitlement
# that lapses mid-run wastes the run and its fetch budget.
RELOGIN_THRESHOLD_HOURS = 12.0

DEFAULT_PROFILE_DIR = Path(
    os.environ.get(
        "HUNNU_RESEARCH_PROFILE", Path.home() / "ResearchHarness" / "chrome-profile"
    )
)

# Newer Chrome keeps the cookie database under Default/Network/; older
# profiles keep it directly under Default/.
_COOKIE_DB_CANDIDATES = (
    Path("Default") / "Network" / "Cookies",
    Path("Default") / "Cookies",
)


def _chrome_time(expires_utc: int) -> datetime | None:
    if not expires_utc:
        return None
    return _CHROME_EPOCH + timedelta(microseconds=int(expires_utc))


@dataclass(frozen=True)
class EntitlementCookie:
    """One entitlement cookie's metadata.  No value, ever."""

    host: str
    name: str
    expires_at: datetime | None
    persistent: bool

    def hours_remaining(self, now: datetime) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - now).total_seconds() / 3600.0

    def as_dict(self, now: datetime) -> dict[str, Any]:
        hours = self.hours_remaining(now)
        return {
            "Host": self.host,
            "Name": self.name,
            "ExpiresAt": self.expires_at.isoformat() if self.expires_at else "session",
            "HoursRemaining": round(hours, 1) if hours is not None else "session",
            "Persistent": self.persistent,
        }


@dataclass(frozen=True)
class AuthFreshnessReport:
    """The gauge: what the cookie metadata says about the sign-in."""

    profile_dir: Path
    cookie_database: Path | None
    readable: bool
    read_mode: str
    read_error: str = ""
    entitlements: tuple[EntitlementCookie, ...] = ()
    session_cookies: int = 0
    persistent_cookies: int = 0
    cloudflare_present: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def entitlement_present(self) -> bool:
        return bool(self.entitlements)

    @property
    def soonest_expiry(self) -> datetime | None:
        expiries = [item.expires_at for item in self.entitlements if item.expires_at]
        return min(expiries) if expiries else None

    @property
    def hours_remaining(self) -> float | None:
        soonest = self.soonest_expiry
        if soonest is None:
            return None
        return (soonest - self.now).total_seconds() / 3600.0

    @property
    def relogin_recommended(self) -> bool:
        """Sign in again before a run?  Fail toward asking.

        An unreadable database, no entitlement at all, an already-lapsed
        cookie, and one inside the threshold all answer yes; only a readable
        database showing comfortable time left answers no.
        """

        if not self.readable or not self.entitlement_present:
            return True
        hours = self.hours_remaining
        if hours is None:
            # Entitlement cookies exist but all are session-scoped: they die
            # with the browser, so freshness cannot be promised to a later run.
            return True
        return hours < RELOGIN_THRESHOLD_HOURS

    def as_dict(self) -> dict[str, Any]:
        soonest = self.soonest_expiry
        hours = self.hours_remaining
        return {
            "ProfileDir": str(self.profile_dir),
            "CookieDatabase": str(self.cookie_database) if self.cookie_database else "NOT_FOUND",
            "CookieDatabaseReadable": self.readable,
            "CookieDatabaseReadMode": self.read_mode,
            "CookieDatabaseReadError": self.read_error or "none",
            "CookieValuesRead": False,
            "InstitutionalEntitlementPresent": self.entitlement_present,
            "InstitutionalEntitlementExpiresAt": soonest.isoformat() if soonest else "unknown",
            "HoursRemaining": round(hours, 1) if hours is not None else "unknown",
            "EntitlementCookies": [item.as_dict(self.now) for item in self.entitlements],
            "SessionCookiesPresent": self.session_cookies,
            "PersistentCookiesPresent": self.persistent_cookies,
            "CloudflareCookiePresent": self.cloudflare_present,
            "ReloginThresholdHours": RELOGIN_THRESHOLD_HOURS,
            "ReloginRecommended": self.relogin_recommended,
        }


def _find_cookie_database(profile_dir: Path) -> Path | None:
    for candidate in _COOKIE_DB_CANDIDATES:
        path = profile_dir / candidate
        if path.is_file():
            return path
    return None


def _is_entitlement(host: str, name: str) -> bool:
    normalized = host.lstrip(".").casefold()
    for suffix, cookie_name in INSTITUTIONAL_ENTITLEMENT_COOKIES:
        if name == cookie_name and (
            normalized == suffix or normalized.endswith("." + suffix)
        ):
            return True
    return False


def read_auth_freshness(
    profile_dir: Path | None = None,
    *,
    now: Callable[[], datetime] | None = None,
) -> AuthFreshnessReport:
    """Read the gauge.  Read-only, immutable, values never touched."""

    profile = Path(profile_dir or DEFAULT_PROFILE_DIR)
    moment = (now or (lambda: datetime.now(timezone.utc)))()
    database = _find_cookie_database(profile)
    if database is None:
        return AuthFreshnessReport(
            profile_dir=profile,
            cookie_database=None,
            readable=False,
            read_mode="immutable-read-only",
            read_error="no cookie database found under the profile",
            now=moment,
        )

    # ``immutable=1`` promises SQLite the file will not change underneath it,
    # which lets a database locked by a running Chrome be read without taking
    # any lock -- at the cost of possibly seeing a slightly older snapshot.
    # That trade is exactly right for a freshness gauge.
    uri = f"file:{database.as_posix()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                "SELECT host_key, name, expires_utc, is_persistent FROM cookies"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return AuthFreshnessReport(
            profile_dir=profile,
            cookie_database=database,
            readable=False,
            read_mode="immutable-read-only",
            read_error=f"{type(exc).__name__}: {exc}",
            now=moment,
        )

    entitlements: list[EntitlementCookie] = []
    session_count = 0
    persistent_count = 0
    cloudflare = False
    for host_key, name, expires_utc, is_persistent in rows:
        host = str(host_key or "")
        cookie_name = str(name or "")
        if is_persistent:
            persistent_count += 1
        else:
            session_count += 1
        if cookie_name in CLOUDFLARE_COOKIE_NAMES:
            cloudflare = True
        if _is_entitlement(host, cookie_name):
            entitlements.append(
                EntitlementCookie(
                    host=host,
                    name=cookie_name,
                    expires_at=_chrome_time(expires_utc) if is_persistent else None,
                    persistent=bool(is_persistent),
                )
            )
    entitlements.sort(
        key=lambda item: (
            item.expires_at is None,
            item.expires_at or datetime.max.replace(tzinfo=timezone.utc),
        )
    )
    return AuthFreshnessReport(
        profile_dir=profile,
        cookie_database=database,
        readable=True,
        read_mode="immutable-read-only",
        entitlements=tuple(entitlements),
        session_cookies=session_count,
        persistent_cookies=persistent_count,
        cloudflare_present=cloudflare,
        now=moment,
    )


__all__ = [
    "CLOUDFLARE_COOKIE_NAMES",
    "DEFAULT_PROFILE_DIR",
    "INSTITUTIONAL_ENTITLEMENT_COOKIES",
    "RELOGIN_THRESHOLD_HOURS",
    "AuthFreshnessReport",
    "EntitlementCookie",
    "read_auth_freshness",
]
