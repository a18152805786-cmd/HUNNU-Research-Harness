"""The search throttle that was missing when a publisher blocked the session.

On 2026-09-19 seven ScienceDirect searches went out from seven separate
``acquire`` processes within a few minutes and Elsevier answered with its block
page twice.  The fetch ledger paced downloads and nothing paced search, so
these tests pin the two properties that would have prevented it: the state is
read from disk (so a fresh process still waits) and the wait is taken before
the query goes out.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hunnu_harness.literature.search_pace import (
    SEARCH_BURST_LIMIT_ENV,
    MIN_SEARCH_INTERVAL_ENV,
    SearchPaceLedger,
    SearchPaceLedgerError,
)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _ledger(tmp_path, clock, **kwargs):
    slept: list[float] = []
    ledger = SearchPaceLedger(
        tmp_path / "search_pace_ledger.jsonl",
        now=clock,
        sleeper=slept.append,
        allow_outside_output_for_tests=True,
        **kwargs,
    )
    return ledger, slept


def test_first_search_never_waits(tmp_path):
    clock = _Clock(datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
    ledger, slept = _ledger(tmp_path, clock, min_interval_seconds=20.0)

    assert ledger.pace(source="ScienceDirect", query="greenwashing") == 0.0
    assert slept == []


def test_second_search_waits_out_the_minimum_interval(tmp_path):
    clock = _Clock(datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
    ledger, slept = _ledger(tmp_path, clock, min_interval_seconds=20.0)

    ledger.pace(source="ScienceDirect", query="first")
    clock.advance(5)
    waited = ledger.pace(source="ScienceDirect", query="second")

    assert waited == pytest.approx(15.0)
    assert slept == [pytest.approx(15.0)]


def test_a_fresh_process_still_waits_because_state_is_on_disk(tmp_path):
    """This failing means the throttle became per-process and stops nothing.

    Every search that tripped the publisher came from its own CLI process.
    """

    clock = _Clock(datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
    first, _ = _ledger(tmp_path, clock, min_interval_seconds=20.0)
    first.pace(source="ScienceDirect", query="first")

    clock.advance(4)
    # A different object over the same file is what a second `acquire` is.
    second, slept = _ledger(tmp_path, clock, min_interval_seconds=20.0)
    waited = second.pace(source="ScienceDirect", query="second")

    assert waited == pytest.approx(16.0)
    assert slept == [pytest.approx(16.0)]


def test_burst_window_holds_the_seventh_search_back(tmp_path):
    """Six searches inside ten minutes are allowed; the seventh waits."""

    clock = _Clock(datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
    ledger, slept = _ledger(
        tmp_path, clock, min_interval_seconds=0.0, burst_window_seconds=600.0, burst_limit=6
    )

    for index in range(6):
        assert ledger.pace(source="ScienceDirect", query=f"q{index}") == 0.0
        clock.advance(10)

    waited = ledger.pace(source="ScienceDirect", query="q6")

    # The oldest of the six went out 60s ago, so the window clears in 540s.
    assert waited == pytest.approx(540.0)
    assert slept == [pytest.approx(540.0)]


def test_pacing_never_refuses_a_search(tmp_path):
    """The throttle only ever waits; refusal belongs to the fetch budget."""

    clock = _Clock(datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
    ledger, _ = _ledger(tmp_path, clock, min_interval_seconds=20.0, burst_limit=1)

    for index in range(5):
        ledger.pace(source="ScienceDirect", query=f"q{index}")

    rows = [
        json.loads(line)
        for line in (tmp_path / "search_pace_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 5


def test_recorded_time_is_when_the_search_went_out_not_when_it_was_queued(tmp_path):
    """The next process must pace from the real request time, not the wait's start."""

    clock = _Clock(datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
    ledger, _ = _ledger(tmp_path, clock, min_interval_seconds=20.0)
    ledger.pace(source="ScienceDirect", query="first")

    clock.advance(5)
    # A real sleeper moves the clock; this one records the wait and the
    # ledger's own clock is advanced to match.
    ledger._sleeper = lambda seconds: clock.advance(seconds)
    ledger.pace(source="ScienceDirect", query="second")

    rows = [
        json.loads(line)
        for line in (tmp_path / "search_pace_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    gap = datetime.fromisoformat(rows[1]["timestamp"]) - datetime.fromisoformat(rows[0]["timestamp"])
    assert gap.total_seconds() == pytest.approx(20.0)


def test_a_corrupt_ledger_is_not_silently_read_as_no_recent_search(tmp_path):
    """This failing means a damaged file quietly disables pacing."""

    clock = _Clock(datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
    path = tmp_path / "search_pace_ledger.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")
    ledger, _ = _ledger(tmp_path, clock)

    with pytest.raises(SearchPaceLedgerError):
        ledger.pace(source="ScienceDirect", query="q")


def test_knobs_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(MIN_SEARCH_INTERVAL_ENV, "45")
    monkeypatch.setenv(SEARCH_BURST_LIMIT_ENV, "3")
    clock = _Clock(datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
    ledger, _ = _ledger(tmp_path, clock)

    assert ledger.min_interval_seconds == pytest.approx(45.0)
    assert ledger.burst_limit == 3


def test_the_ledger_carries_no_account_or_device_name(tmp_path):
    clock = _Clock(datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
    ledger, _ = _ledger(tmp_path, clock)
    ledger.pace(source="ScienceDirect", query="greenwashing")

    row = json.loads((tmp_path / "search_pace_ledger.jsonl").read_text(encoding="utf-8").strip())
    assert set(row) == {"timestamp", "source", "query", "waited_seconds"}
