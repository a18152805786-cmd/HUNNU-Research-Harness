"""The conftest fetch-ledger guard, tested from the inside.

Injection discipline failed twice during development -- two test runs wrote
the real audit ledger before anyone noticed.  The autouse fixture in
tests/conftest.py is the structural answer, and these tests prove the two
properties it promises: a default-constructed ledger -- the exact shape a
forgotten injection produces -- resolves inside the per-test guard directory,
and writing through it leaves the real audit ledger untouched.

The guard's failure detector was falsified separately: a probe test that
deliberately appended to the real ledger was failed by the fixture teardown
by name, and a real injection was temporarily removed from an adapter test to
confirm the redirect catches what discipline misses.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from conftest import REAL_FETCH_LEDGER_PATH
from hunnu_harness.literature.adapters.sciencedirect import ScienceDirectAdapter
from hunnu_harness.literature.fetch_ledger import FulltextFetchLedger
from hunnu_harness.paths import TEMP_DIR, is_within

from test_fetch_ledger import (
    _ExplodingBrowser,
    _sciencedirect_access,
    _sciencedirect_record,
)


class StructuralIsolationTests(unittest.TestCase):
    def test_a_default_constructed_ledger_cannot_reach_the_real_file(self) -> None:
        ledger = FulltextFetchLedger()  # what a forgotten injection would use
        self.assertNotEqual(ledger.path, REAL_FETCH_LEDGER_PATH)
        self.assertIn("ledger-guard-", str(ledger.path))
        self.assertTrue(is_within(ledger.path, TEMP_DIR))

        before = REAL_FETCH_LEDGER_PATH.exists()
        ledger.authorize_fetch(source="S", identifier="pii:guard", paper_id="PG")
        self.assertTrue(ledger.path.exists())
        self.assertEqual(REAL_FETCH_LEDGER_PATH.exists(), before)

    def test_an_uninjected_adapter_writes_only_the_guard_ledger(self) -> None:
        adapter = ScienceDirectAdapter(_ExplodingBrowser(Path("unused")))
        self.assertIsNone(adapter.fetch_ledger)  # deliberately not injected
        before = REAL_FETCH_LEDGER_PATH.exists()
        with self.assertRaises(Exception):
            asyncio.run(
                adapter.download_fulltext(_sciencedirect_record(), _sciencedirect_access())
            )
        self.assertEqual(REAL_FETCH_LEDGER_PATH.exists(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
