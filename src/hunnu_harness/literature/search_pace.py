"""Cross-process pacing for publisher *search* requests.

The full-text fetch ledger in ``fetch_ledger.py`` paces and budgets downloads,
and nothing paced search.  That gap is not theoretical: on 2026-09-19 a run
fired seven ScienceDirect searches from seven separate ``acquire`` processes
inside a few minutes and Elsevier answered with its block page
(``CPE00001``/``CLOUDFLARE_ERROR_1000S_BOX``) twice, costing the user's session
rather than any quota.  In-process delays cannot prevent that, because each
search was a new process that knew nothing about the ones before it -- so the
pacing state, like the fetch budget's, lives in a file.

This is a throttle, not a budget.  It never refuses a search and never fails a
run: the only thing it does is wait.  Refusal belongs to the fetch ledger,
where the thing being spent is the user's institutional download quota; what is
being protected here is the session itself, and a search that arrives late is
strictly better than one that arrives into a block page.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..paths import AUDIT_DIR, _windows_io_path, require_output_path
from .fetch_ledger import _resolve_burst_limit, _resolve_float_setting
from .security import sanitize_text

SEARCH_PACE_LEDGER_PATH = AUDIT_DIR / "search_pace_ledger.jsonl"

# Publisher search endpoints tolerate far less automation than their article
# pages: the default spacing is deliberately wider than the 15s between
# downloads, and the burst allowance is half the fetch ledger's twelve.
MIN_SEARCH_INTERVAL_SECONDS = 20.0
MIN_SEARCH_INTERVAL_ENV = "HUNNU_HARNESS_SEARCH_MIN_INTERVAL_SECONDS"
SEARCH_BURST_WINDOW_SECONDS = 600.0
SEARCH_BURST_WINDOW_ENV = "HUNNU_HARNESS_SEARCH_BURST_WINDOW_SECONDS"
SEARCH_BURST_LIMIT = 6
SEARCH_BURST_LIMIT_ENV = "HUNNU_HARNESS_SEARCH_BURST_LIMIT"

# A row per search is enough to pace by, and an unbounded file is not: only
# rows that can still affect a wait are kept when the ledger is rewritten.
_RETENTION_MULTIPLIER = 4


class SearchPaceLedgerError(RuntimeError):
    """The ledger on disk could not be read as pacing state."""


class SearchPaceLedger:
    """Append-only search timestamps, shared by every Harness process."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        min_interval_seconds: float | None = None,
        burst_window_seconds: float | None = None,
        burst_limit: int | None = None,
        sleeper: Callable[[float], None] | None = None,
        allow_outside_output_for_tests: bool = False,
    ) -> None:
        candidate = Path(path or SEARCH_PACE_LEDGER_PATH)
        # The Output-Root boundary holds for every real run.  An isolated test
        # tree may sit elsewhere, and it opts out explicitly -- the same escape
        # ``GlobalPaperLibrary`` gives its own tests, never a default.
        self.path = (
            candidate
            if allow_outside_output_for_tests
            else require_output_path(candidate, label="Search pace ledger")
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.min_interval_seconds = _resolve_float_setting(
            min_interval_seconds,
            env_name=MIN_SEARCH_INTERVAL_ENV,
            default=MIN_SEARCH_INTERVAL_SECONDS,
            minimum=0.0,
            inclusive=True,
        )
        self.burst_window_seconds = _resolve_float_setting(
            burst_window_seconds,
            env_name=SEARCH_BURST_WINDOW_ENV,
            default=SEARCH_BURST_WINDOW_SECONDS,
            minimum=0.0,
            inclusive=False,
        )
        self.burst_limit = _resolve_burst_limit(
            burst_limit,
            env_name=SEARCH_BURST_LIMIT_ENV,
            default=SEARCH_BURST_LIMIT,
        )
        self._sleeper = sleeper if sleeper is not None else time.sleep

    # -- reading -----------------------------------------------------------

    def read_timestamps(self) -> list[datetime]:
        """Every recorded search time, oldest first.

        A row that cannot be read as a timestamp is a lie about pacing state,
        and pacing that silently treats it as "no recent search" would pace
        nothing.  It raises instead.
        """

        io_path = _windows_io_path(self.path)
        if not io_path.exists():
            return []
        stamps: list[datetime] = []
        with io_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SearchPaceLedgerError(
                        f"search pace ledger has a line that is not JSON: {line[:120]!r}"
                    ) from exc
                raw = str(row.get("timestamp", "")).strip()
                try:
                    stamp = datetime.fromisoformat(raw)
                except (TypeError, ValueError) as exc:
                    raise SearchPaceLedgerError(
                        f"search pace ledger row has an invalid ISO timestamp: {raw!r}"
                    ) from exc
                if stamp.tzinfo is None:
                    raise SearchPaceLedgerError(
                        f"search pace ledger row timestamp has no timezone: {raw!r}"
                    )
                stamps.append(stamp.astimezone(timezone.utc))
        stamps.sort()
        return stamps

    def wait_seconds(self) -> float:
        """How long the next search must wait, without waiting or recording."""

        return self._wait_for(self.read_timestamps())

    def _wait_for(self, stamps: list[datetime]) -> float:
        now = self._now().astimezone(timezone.utc)

        by_interval = 0.0
        if self.min_interval_seconds > 0 and stamps:
            by_interval = self.min_interval_seconds - (now - stamps[-1]).total_seconds()

        by_burst = 0.0
        in_window = [
            stamp
            for stamp in stamps
            if (now - stamp).total_seconds() < self.burst_window_seconds
        ]
        if len(in_window) >= self.burst_limit:
            # The oldest of the most recent ``burst_limit`` searches: once it
            # falls out of the window there is room for one more.
            oldest_that_counts = in_window[-self.burst_limit]
            by_burst = self.burst_window_seconds - (now - oldest_that_counts).total_seconds()

        return max(0.0, by_interval, by_burst)

    # -- writing -----------------------------------------------------------

    def pace(self, *, source: str, query: str) -> float:
        """Wait out the interval and burst window, then record this search.

        Returns the seconds actually waited, so a caller can report it.  The
        row is written *after* the wait so that the recorded time is when the
        search really went out, which is what the next process must pace from.
        """

        waited = self._wait_for(self.read_timestamps())
        if waited > 0:
            self._sleeper(waited)
        self._append(source=source, query=query, waited_seconds=waited)
        return waited

    def _append(self, *, source: str, query: str, waited_seconds: float) -> None:
        row = {
            "timestamp": self._now().astimezone(timezone.utc).isoformat(),
            "source": sanitize_text(str(source))[:80],
            # The query is research content, kept short: this file is pacing
            # state, not a search history, and it never carries an account,
            # a device name, or a credential.
            "query": sanitize_text(str(query))[:200],
            "waited_seconds": round(float(waited_seconds), 3),
        }
        io_path = _windows_io_path(self.path)
        io_path.parent.mkdir(parents=True, exist_ok=True)
        with io_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._prune()

    def _prune(self) -> None:
        """Drop rows too old to affect any future wait.

        Best effort: pacing must not fail because housekeeping did.
        """

        io_path = _windows_io_path(self.path)
        keep_after = self.burst_window_seconds * _RETENTION_MULTIPLIER
        try:
            lines = io_path.read_text(encoding="utf-8").splitlines()
            if len(lines) <= self.burst_limit * _RETENTION_MULTIPLIER:
                return
            now = self._now().astimezone(timezone.utc)
            kept: list[str] = []
            for line in lines:
                if not line.strip():
                    continue
                row: dict[str, Any] = json.loads(line)
                stamp = datetime.fromisoformat(str(row["timestamp"])).astimezone(timezone.utc)
                if (now - stamp).total_seconds() <= keep_after:
                    kept.append(line)
            if len(kept) != len(lines):
                io_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return


__all__ = [
    "MIN_SEARCH_INTERVAL_ENV",
    "MIN_SEARCH_INTERVAL_SECONDS",
    "SEARCH_BURST_LIMIT",
    "SEARCH_BURST_LIMIT_ENV",
    "SEARCH_BURST_WINDOW_ENV",
    "SEARCH_BURST_WINDOW_SECONDS",
    "SEARCH_PACE_LEDGER_PATH",
    "SearchPaceLedger",
    "SearchPaceLedgerError",
]
