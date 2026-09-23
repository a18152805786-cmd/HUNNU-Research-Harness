"""Structural guarantees for the whole test suite.

The real write-ahead fetch ledger (``Output Root/audit/
fulltext_fetch_ledger.jsonl``) is live operational state: budget counted
against real publisher quota.  During development it was polluted twice by
tests that forgot to inject an isolated ledger -- discipline alone is not a
guardrail.  The autouse fixture below makes the real ledger structurally
unreachable from any test, injected or not:

* every test runs with ``fetch_ledger.FETCH_LEDGER_PATH`` repointed into a
  throwaway directory under TEMP_DIR (still inside the Output Root, so
  ``require_output_path`` keeps holding), which is what any
  default-constructed ``FulltextFetchLedger()`` resolves; and
* the real ledger file is snapshotted before each test and compared after --
  any write to it fails that test by name, loudly, instead of surfacing as a
  mystery file after the suite.

Explicit ``adapter.fetch_ledger = ...`` injections in tests remain good
practice (they document intent and isolate tests from each other), but
forgetting one is no longer able to touch the real file.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from hunnu_harness.literature import fetch_ledger as _fetch_ledger_module
from hunnu_harness.literature import search_pace as _search_pace_module

# Captured once at import time, before any test can repoint the module
# constant: this is the file no test may ever touch.
REAL_FETCH_LEDGER_PATH = Path(_fetch_ledger_module.FETCH_LEDGER_PATH)


def _ledger_snapshot() -> tuple[bool, int, int] | None:
    try:
        stat = os.stat(REAL_FETCH_LEDGER_PATH)
    except OSError:
        return None
    return (True, stat.st_size, stat.st_mtime_ns)


@pytest.fixture(autouse=True)
def isolated_fetch_ledger_path(monkeypatch):
    """No test can reach the real fetch ledger, with or without an injection."""

    from hunnu_harness.paths import TEMP_DIR

    before = _ledger_snapshot()
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ledger-guard-", dir=TEMP_DIR) as tmp:
        monkeypatch.setattr(
            _fetch_ledger_module,
            "FETCH_LEDGER_PATH",
            Path(tmp) / "fulltext_fetch_ledger.jsonl",
        )
        yield
    after = _ledger_snapshot()
    if before != after:
        pytest.fail(
            "this test touched the real fetch ledger at "
            f"{REAL_FETCH_LEDGER_PATH} (before={before}, after={after}); "
            "publisher fetch budget is live state and tests must never write it"
        )


@pytest.fixture(autouse=True)
def clear_fetch_ledger_tuning_environment(monkeypatch):
    """Assert shipped defaults so user-set fetch knobs cannot break self-checks.

    Tests using ``patch.dict`` still take effect after this fixture's setup.
    """

    from hunnu_harness.literature.fetch_ledger import (
        BURST_LIMIT_ENV,
        BURST_WINDOW_ENV,
        DAILY_FETCH_LIMIT_ENV,
        MIN_FETCH_INTERVAL_ENV,
    )

    from hunnu_harness.literature.search_pace import (
        MIN_SEARCH_INTERVAL_ENV,
        SEARCH_BURST_LIMIT_ENV,
        SEARCH_BURST_WINDOW_ENV,
    )

    for env_name in (
        DAILY_FETCH_LIMIT_ENV,
        MIN_FETCH_INTERVAL_ENV,
        BURST_WINDOW_ENV,
        BURST_LIMIT_ENV,
        MIN_SEARCH_INTERVAL_ENV,
        SEARCH_BURST_WINDOW_ENV,
        SEARCH_BURST_LIMIT_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)


# The search pace ledger is the same kind of live state as the fetch ledger:
# a test row in the real file makes the user's next real search wait for a
# search that never happened.  Same guard, same reasons.
REAL_SEARCH_PACE_LEDGER_PATH = Path(_search_pace_module.SEARCH_PACE_LEDGER_PATH)


def _search_pace_snapshot() -> tuple[bool, int, int] | None:
    try:
        stat = os.stat(REAL_SEARCH_PACE_LEDGER_PATH)
    except OSError:
        return None
    return (True, stat.st_size, stat.st_mtime_ns)


@pytest.fixture(autouse=True)
def isolated_search_pace_ledger_path(monkeypatch):
    """No test can reach the real search pace ledger, with or without an injection."""

    from hunnu_harness.paths import TEMP_DIR

    before = _search_pace_snapshot()
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="search-pace-guard-", dir=TEMP_DIR) as tmp:
        monkeypatch.setattr(
            _search_pace_module,
            "SEARCH_PACE_LEDGER_PATH",
            Path(tmp) / "search_pace_ledger.jsonl",
        )
        yield
    after = _search_pace_snapshot()
    if before != after:
        pytest.fail(
            "this test touched the real search pace ledger at "
            f"{REAL_SEARCH_PACE_LEDGER_PATH} (before={before}, after={after}); "
            "search pacing is live state and tests must never write it"
        )


# The Research Chrome lock is live in a different way: a test holding the real
# lock refuses the user's real acquisition running beside the suite, and a
# real batch holding it would fail every test that starts a browser.
@pytest.fixture(autouse=True)
def isolated_research_chrome_lock(monkeypatch):
    """Every test takes the Research Chrome lock in its own throwaway directory."""

    from hunnu_harness.browser import research_chrome_lock
    from hunnu_harness.paths import TEMP_DIR

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chrome-lock-guard-", dir=TEMP_DIR) as tmp:
        monkeypatch.setattr(
            research_chrome_lock,
            "LOCK_PATH",
            Path(tmp) / "research_chrome.lock",
        )
        try:
            yield
        finally:
            # A test that started a browser without closing it still holds the
            # lock; drop the hold before its directory is removed.
            research_chrome_lock._release_all_for_tests()
