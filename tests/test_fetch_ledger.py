"""The write-ahead full-text fetch budget.

One PDF was fetched 13 times in a day because every fetch failed *after* the
bytes arrived: the paper never reached the Library, so every in-library check
truthfully answered NOT_IN_LIBRARY and waved the next attempt through.  The
ledger closes that gap by recording the attempt before the fetch action is
issued, so these tests are mostly about ordering and refusal: the attempt row
must exist even when the fetch explodes, the third attempt for one identifier
must be refused, and nothing here may ever touch the real audit ledger or the
corpus fingerprint.

Everything runs on ledgers under TEMP_DIR.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hunnu_harness.cli import build_parser as build_harness_parser
from hunnu_harness.literature.adapters.sciencedirect import ScienceDirectAdapter
from hunnu_harness.literature.cli import build_parser as build_literature_parser
from hunnu_harness.literature.fetch_ledger import (
    FETCH_LEDGER_PATH,
    GLOBAL_DAILY_LIMIT,
    PER_IDENTIFIER_DAILY_LIMIT,
    STATUS_ALREADY_FETCHED,
    STATUS_BUDGET_EXHAUSTED,
    FetchBudgetExceeded,
    FetchIdentifierMissing,
    FetchLedgerError,
    FulltextFetchLedger,
    ledger_identifier,
)
from hunnu_harness.literature.models import (
    AccessDecision,
    AccessType,
    FullTextFormat,
    LiteratureRecord,
    RunStatus,
)
from hunnu_harness.paths import (
    AUDIT_DIR,
    LIBRARY_CATALOG_DIR,
    LIBRARY_PAPERS_DIR,
    PAPERS_BY_TOPIC_DIR,
    TEMP_DIR,
    is_within,
)

PII = "S1544612326004149"
ARTICLE_URL = f"https://www.sciencedirect.com/science/article/pii/{PII}"


def _ledger(root: Path, **kwargs) -> FulltextFetchLedger:
    return FulltextFetchLedger(path=Path(root) / "fetch_ledger.jsonl", **kwargs)


def _tmp(test: unittest.TestCase) -> Path:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    holder = tempfile.TemporaryDirectory(prefix="ledger-", dir=TEMP_DIR)
    test.addCleanup(holder.cleanup)
    return Path(holder.name)


def _sciencedirect_record() -> LiteratureRecord:
    return LiteratureRecord(paper_id="PTEST", source_page=ARTICLE_URL)


def _sciencedirect_access() -> AccessDecision:
    return AccessDecision(
        full_text_accessible=True,
        access_type=AccessType.INSTITUTIONAL_AUTHENTICATED,
        authorized_access=True,
        status=RunStatus.SUCCESS,
        reason="authorized",
        full_text_format=FullTextFormat.PDF,
        download_url=f"{ARTICLE_URL}/pdfft",
    )


class _ExplodingBrowser:
    """A fetch action that fails -- after proving the attempt was on disk."""

    def __init__(self, ledger_path: Path) -> None:
        self.ledger_path = ledger_path
        self.attempt_was_on_disk_before_fetch: bool | None = None

    async def execute(self, command):
        text = (
            self.ledger_path.read_text(encoding="utf-8")
            if self.ledger_path.exists()
            else ""
        )
        self.attempt_was_on_disk_before_fetch = '"record_type": "attempt"' in text
        raise RuntimeError("bytes arrived and then this step failed")


class WriteAheadOrderingTests(unittest.TestCase):
    """4.4: the attempt is written before the action, not after success."""

    def test_attempt_is_on_disk_before_the_fetch_action_runs(self) -> None:
        root = _tmp(self)
        ledger = _ledger(root)
        browser = _ExplodingBrowser(ledger.path)
        adapter = ScienceDirectAdapter(browser)
        adapter.fetch_ledger = ledger

        with self.assertRaises(Exception):
            asyncio.run(
                adapter.download_fulltext(_sciencedirect_record(), _sciencedirect_access())
            )

        self.assertTrue(browser.attempt_was_on_disk_before_fetch)
        records = ledger.read_records()
        self.assertEqual([item["record_type"] for item in records], ["attempt", "outcome"])
        self.assertFalse(records[1]["ok"])
        self.assertIn("failed", records[1]["detail"])

    def test_two_failed_fetches_block_the_third(self) -> None:
        """The 13-fetch shape: failure after the bytes still consumes budget."""

        root = _tmp(self)
        ledger = _ledger(root)
        adapter = ScienceDirectAdapter(_ExplodingBrowser(ledger.path))
        adapter.fetch_ledger = ledger

        for _ in range(PER_IDENTIFIER_DAILY_LIMIT):
            with self.assertRaises(Exception):
                asyncio.run(
                    adapter.download_fulltext(
                        _sciencedirect_record(), _sciencedirect_access()
                    )
                )
        with self.assertRaises(FetchBudgetExceeded) as caught:
            asyncio.run(
                adapter.download_fulltext(_sciencedirect_record(), _sciencedirect_access())
            )
        self.assertEqual(caught.exception.status, STATUS_ALREADY_FETCHED)
        # The refusal reports what happened last time, so "run it again to
        # see" is never the only diagnostic left.
        self.assertIn("last outcome", str(caught.exception))


class BudgetRuleTests(unittest.TestCase):
    def test_second_attempt_passes_and_the_third_is_refused(self) -> None:
        ledger = _ledger(_tmp(self))
        for _ in range(PER_IDENTIFIER_DAILY_LIMIT):
            ledger.authorize_fetch(source="ScienceDirect", identifier="pii:x", paper_id="P1")
        with self.assertRaises(FetchBudgetExceeded) as caught:
            ledger.authorize_fetch(source="ScienceDirect", identifier="pii:x", paper_id="P1")
        self.assertEqual(caught.exception.status, STATUS_ALREADY_FETCHED)

    def test_a_refusal_writes_no_attempt(self) -> None:
        """A blocked fetch consumed no budget and must not eat the ledger."""

        ledger = _ledger(_tmp(self))
        for _ in range(PER_IDENTIFIER_DAILY_LIMIT):
            ledger.authorize_fetch(source="S", identifier="pii:x", paper_id="P1")
        before = ledger.path.read_bytes()
        with self.assertRaises(FetchBudgetExceeded):
            ledger.authorize_fetch(source="S", identifier="pii:x", paper_id="P1")
        self.assertEqual(ledger.path.read_bytes(), before)

    def test_identifiers_are_scoped_per_source(self) -> None:
        ledger = _ledger(_tmp(self))
        for _ in range(PER_IDENTIFIER_DAILY_LIMIT):
            ledger.authorize_fetch(source="ScienceDirect", identifier="10.1/x", paper_id="P1")
        # The same identifier under another source is another publisher ask.
        ledger.authorize_fetch(source="SpringerLink", identifier="10.1/x", paper_id="P1")

    def test_global_daily_ceiling_is_refused(self) -> None:
        ledger = _ledger(_tmp(self))
        for index in range(GLOBAL_DAILY_LIMIT):
            ledger.authorize_fetch(source="S", identifier=f"id-{index}", paper_id="P")
        with self.assertRaises(FetchBudgetExceeded) as caught:
            ledger.authorize_fetch(source="S", identifier="id-fresh", paper_id="P")
        self.assertEqual(caught.exception.status, STATUS_BUDGET_EXHAUSTED)
        self.assertIn("AGENTS.md 62", str(caught.exception))

    def test_allow_refetch_passes_and_is_recorded_on_the_attempt(self) -> None:
        ledger = _ledger(_tmp(self))
        for _ in range(PER_IDENTIFIER_DAILY_LIMIT):
            ledger.authorize_fetch(source="S", identifier="pii:x", paper_id="P1")
        ledger.authorize_fetch(
            source="S", identifier="pii:x", paper_id="P1", allow_refetch=True
        )
        attempts = [
            item for item in ledger.read_records() if item["record_type"] == "attempt"
        ]
        self.assertEqual(len(attempts), PER_IDENTIFIER_DAILY_LIMIT + 1)
        self.assertTrue(attempts[-1]["allow_refetch"])

    def test_missing_identifier_fails_closed(self) -> None:
        ledger = _ledger(_tmp(self))
        with self.assertRaises(FetchIdentifierMissing):
            ledger.authorize_fetch(source="S", identifier="", paper_id="P1")
        with self.assertRaises(FetchIdentifierMissing):
            ledger.authorize_fetch(source="S", identifier="unknown", paper_id="P1")
        record = LiteratureRecord(paper_id="P1")  # no stable id, no DOI
        with self.assertRaises(FetchIdentifierMissing):
            ledger_identifier(record)
        self.assertFalse(ledger.path.exists())

    def test_identifier_falls_back_to_the_normalized_doi(self) -> None:
        record = LiteratureRecord(paper_id="P1", doi="https://doi.org/10.1016/J.FRL.2026.109884")
        self.assertEqual(ledger_identifier(record), "10.1016/j.frl.2026.109884")
        preferred = ledger_identifier(record, prefer="pii:abc")
        self.assertEqual(preferred, "pii:abc")

    def test_the_budget_is_a_utc_day(self) -> None:
        moments = [datetime(2026, 8, 30, 23, 50, tzinfo=timezone.utc)]
        ledger = _ledger(_tmp(self), now=lambda: moments[-1])
        for _ in range(PER_IDENTIFIER_DAILY_LIMIT):
            ledger.authorize_fetch(source="S", identifier="pii:x", paper_id="P1")
        with self.assertRaises(FetchBudgetExceeded):
            ledger.authorize_fetch(source="S", identifier="pii:x", paper_id="P1")
        moments.append(moments[-1] + timedelta(hours=1))  # next UTC day
        ledger.authorize_fetch(source="S", identifier="pii:x", paper_id="P1")


class LedgerIntegrityTests(unittest.TestCase):
    def test_a_malformed_line_is_detected_not_silently_dropped(self) -> None:
        ledger = _ledger(_tmp(self))
        ledger.authorize_fetch(source="S", identifier="pii:x", paper_id="P1")
        with ledger.path.open("a", encoding="utf-8") as handle:
            handle.write("this is not a ledger record\n")
        with self.assertRaises(FetchLedgerError):
            ledger.read_records()
        # A guard that cannot read its ledger refuses the fetch.
        with self.assertRaises(FetchLedgerError):
            ledger.authorize_fetch(source="S", identifier="pii:y", paper_id="P2")

    def test_a_wellformed_line_that_is_not_a_record_is_also_detected(self) -> None:
        ledger = _ledger(_tmp(self))
        ledger.path.parent.mkdir(parents=True, exist_ok=True)
        ledger.path.write_text(json.dumps({"record_type": "surprise"}) + "\n", encoding="utf-8")
        with self.assertRaises(FetchLedgerError):
            ledger.read_records()

    def test_records_carry_no_personal_identity(self) -> None:
        """4.2: no username, no account, no device name -- field allowlist."""

        ledger = _ledger(_tmp(self))
        ticket = ledger.authorize_fetch(source="S", identifier="pii:x", paper_id="P1")
        ticket.record_outcome(ok=True, detail="completed")
        allowed = {
            "attempt": {
                "record_type", "date", "timestamp", "source", "identifier",
                "paper_id", "allow_refetch",
            },
            "outcome": {
                "record_type", "date", "timestamp", "source", "identifier",
                "paper_id", "ok", "detail",
            },
        }
        for item in ledger.read_records():
            self.assertEqual(set(item), allowed[item["record_type"]], item)

    def test_the_real_ledger_lives_outside_every_fingerprint_input(self) -> None:
        """4.2: fetching twice must not make the Library look changed.

        The corpus fingerprint reads the catalog directory, the managed papers
        directory, and the topic view -- the ledger sits in audit/, inside
        none of them.
        """

        self.assertTrue(is_within(FETCH_LEDGER_PATH, AUDIT_DIR))
        for fingerprint_input in (LIBRARY_CATALOG_DIR, LIBRARY_PAPERS_DIR, PAPERS_BY_TOPIC_DIR):
            self.assertFalse(is_within(FETCH_LEDGER_PATH, fingerprint_input))

    def test_usage_report_summarises_the_day(self) -> None:
        ledger = _ledger(_tmp(self))
        ticket = ledger.authorize_fetch(source="S", identifier="pii:x", paper_id="P1")
        ticket.record_outcome(ok=False, detail="validation failed")
        usage = ledger.usage_today()
        self.assertEqual(usage["AttemptsToday"], 1)
        self.assertEqual(usage["RemainingGlobalBudget"], GLOBAL_DAILY_LIMIT - 1)
        self.assertEqual(len(usage["Identifiers"]), 1)
        entry = usage["Identifiers"][0]
        self.assertEqual(entry["attempts"], 1)
        self.assertIn("validation failed", entry["last_outcome"])


class WorkflowBehaviourTests(unittest.TestCase):
    def test_max_downloads_behaviour_is_unchanged_with_the_guard_in_place(self) -> None:
        """One bounded run still downloads exactly once and records one attempt."""

        from test_acquisition_command_sequence import RecordingBrowser, run_acquisition

        browser, result = run_acquisition(RecordingBrowser, max_downloads=1)
        self.assertEqual(len(result.downloads), 1)
        downloads = [kind for kind, _ in browser.commands if kind == "DownloadCommand"]
        self.assertEqual(len(downloads), 1)

    def test_zero_downloads_never_reaches_the_guard(self) -> None:
        from test_acquisition_command_sequence import RecordingBrowser, run_acquisition

        browser, result = run_acquisition(RecordingBrowser, max_downloads=0)
        self.assertEqual(len(result.downloads), 0)
        downloads = [kind for kind, _ in browser.commands if kind == "DownloadCommand"]
        self.assertEqual(downloads, [])


class CLISurfaceTests(unittest.TestCase):
    def test_budget_command_exists(self) -> None:
        args = build_harness_parser().parse_args(["library-fetch-budget"])
        self.assertEqual(args.command, "library-fetch-budget")

    def test_live_commands_accept_allow_refetch_and_default_it_off(self) -> None:
        parser = build_literature_parser()
        for command in (
            "live-sciencedirect",
            "live-springerlink",
            "live-cnki",
            "live-oxfordacademic",
        ):
            args = parser.parse_args([command, "--title", "t", "--allow-refetch"])
            self.assertTrue(args.allow_refetch, command)
            args = parser.parse_args([command, "--title", "t"])
            self.assertFalse(args.allow_refetch, command)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
