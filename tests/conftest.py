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

    for env_name in (
        DAILY_FETCH_LIMIT_ENV,
        MIN_FETCH_INTERVAL_ENV,
        BURST_WINDOW_ENV,
        BURST_LIMIT_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)
