"""acquire-batch: many papers, one after another, through the single-paper path.

The batch exists to take the agent out of the per-paper loop, and for nothing
else: every item must still be an ordinary ``live-<source>`` run with its
pacing, ledger, identity lock and exit ladder.  Items run here through the real
``_run_live_into`` with only the browser and the workflow replaced, so these
tests see what a real item sees.  A test that fails here names the guard the
batch stopped honouring.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hunnu_harness import cli as main_cli
from hunnu_harness.browser import research_chrome_lock
from hunnu_harness.exit_codes import (
    EXIT_BUDGET_EXHAUSTED,
    EXIT_ENV_NOT_READY,
    EXIT_HUMAN_ACTION_REQUIRED,
    EXIT_OK,
    EXIT_RUN_FAILED,
)
from hunnu_harness.literature import acquire_batch as batch_module
from hunnu_harness.literature import cli as literature_cli
from hunnu_harness.literature.acquire_batch import (
    AcquireBatch,
    BatchQueueError,
    ItemRun,
    parse_queue,
)
from hunnu_harness.literature.adapters.base import SourceLayoutChanged
from hunnu_harness.literature.fetch_ledger import (
    STATUS_ATTEMPT_LIMIT_REACHED,
    STATUS_BUDGET_EXHAUSTED,
)
from hunnu_harness.literature.models import RunStatus
from hunnu_harness.navigator.catalog import CatalogReader
from hunnu_harness.navigator.index import NavigatorIndex
from hunnu_harness.navigator.search import PaperNavigator
from hunnu_harness.paths import TEMP_DIR

from test_research_chrome_lock import held_by_another_process


def _download(key: str) -> SimpleNamespace:
    return SimpleNamespace(
        paper_id=f"P-{abs(hash(key)) % 10**6:06d}",
        title=key,
        doi="unknown",
        full_text_format="PDF",
        sha256="a" * 64,
        library_disposition="NEW_PAPER",
        library_managed_path="library/papers/P.pdf",
        classification_status="CLASSIFIED",
        assigned_primary_topic="01_x\\y",
    )


def _result(kind: str, key: str) -> SimpleNamespace:
    base = {"records": [], "downloads": [], "errors": [], "action_required_reason": "unknown"}
    if kind == "download":
        return SimpleNamespace(status=RunStatus.SUCCESS, **{**base, "downloads": [_download(key)]})
    if kind == "login":
        return SimpleNamespace(
            status=RunStatus.ACTION_REQUIRED_USER_LOGIN,
            **{**base, "action_required_reason": "CNKI slider verification is showing"},
        )
    if kind == "no_results":
        return SimpleNamespace(status=RunStatus.NO_RESULTS, **base)
    if kind == "not_authorized":
        return SimpleNamespace(status=RunStatus.FULLTEXT_NOT_AUTHORIZED, **base)
    if kind == "download_failed":
        return SimpleNamespace(
            status=RunStatus.DOWNLOAD_FAILED,
            **{**base, "errors": ["DOWNLOAD_EVENT_TIMEOUT: the control started no download"]},
        )
    if kind == "repeat_limit":
        record = SimpleNamespace(error_status=STATUS_ATTEMPT_LIMIT_REACHED, error_reason="fetched twice today")
        return SimpleNamespace(status=RunStatus.PARTIAL_SUCCESS, **{**base, "records": [record]})
    if kind == "daily_budget":
        record = SimpleNamespace(error_status=STATUS_BUDGET_EXHAUSTED, error_reason="daily ceiling")
        return SimpleNamespace(status=RunStatus.PARTIAL_SUCCESS, **{**base, "records": [record]})
    raise AssertionError(f"unknown scripted outcome {kind!r}")


class _Publisher:
    """Scripts what each paper's run returns, and records what ran."""

    def __init__(self, script: dict[str, object] | None = None) -> None:
        self.script = dict(script or {})
        self.calls: list[str] = []
        self.adapters: list[object] = []
        self.browsers: list[dict] = []

    def browser_class(self):
        publisher = self

        class _Browser:
            def __init__(self, **kwargs: object) -> None:
                publisher.browsers.append(kwargs)

            async def start(self) -> None:
                return None

            async def lifecycle(self) -> dict:
                return {
                    "BrowserLaunched": True,
                    "FinalURL": "https://kns.cnki.net/verify?token=abc123",
                    "FinalPageTitle": "安全验证",
                }

            async def close(self) -> None:
                return None

        return _Browser

    def workflow_class(self):
        publisher = self

        class _Workflow:
            def __init__(self, adapter: object, **_kwargs: object) -> None:
                publisher.adapters.append(adapter)

            async def run(self, request: object) -> SimpleNamespace:
                key = (list(request.dois) or list(request.exact_titles))[0]
                publisher.calls.append(key)
                outcome = publisher.script.get(key, "download")
                if isinstance(outcome, Exception):
                    raise outcome
                return _result(str(outcome), key)

        return _Workflow

    @contextlib.contextmanager
    def active(self):
        with patch.object(literature_cli, "PlaywrightBrowser", self.browser_class()), patch.object(
            literature_cli, "LiteratureAcquisitionWorkflow", self.workflow_class()
        ):
            yield self


class _Ledger:
    def __init__(self, remaining: int = 15) -> None:
        self.remaining = remaining

    def usage_today(self) -> dict:
        return {"RemainingGlobalBudget": self.remaining}


def _queue(titles: list[str], *, name: str = "TestBatch", source: str = "cnki") -> dict:
    return {"BatchName": name, "Items": [{"Source": source, "Title": title} for title in titles]}


class _BatchCase(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="acquire-batch-", dir=TEMP_DIR)
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def batch(self, queue: dict, **overrides: object) -> AcquireBatch:
        options: dict = {
            "batch_root": self.root / "batch",
            "browser_running": lambda: True,
            "navigator_factory": lambda: None,
            "ledger_factory": lambda: _Ledger(),
        }
        options.update(overrides)
        return AcquireBatch(parse_queue(queue), **options)

    def run_batch(self, queue: dict, publisher: _Publisher, **overrides: object):
        with publisher.active():
            return asyncio.run(self.batch(queue, **overrides).run())

    def state_rows(self) -> list[dict]:
        path = self.root / "batch" / batch_module.STATE_FILENAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class QueueValidationTests(unittest.TestCase):
    def test_a_queue_over_twenty_five_items_is_refused_before_anything_runs(self) -> None:
        """Rule 62: one batch fetches at most 25; a larger total is several queues."""

        with self.assertRaises(BatchQueueError) as caught:
            parse_queue(_queue([f"paper {n}" for n in range(26)]))
        self.assertIn("25", str(caught.exception))

    def test_the_same_paper_twice_in_one_queue_is_refused(self) -> None:
        queue = {
            "BatchName": "Dup",
            "Items": [
                {"Source": "springerlink", "DOI": "10.1007/ABC"},
                {"Source": "sciencedirect", "DOI": "https://doi.org/10.1007/abc"},
            ],
        }
        with self.assertRaises(BatchQueueError) as caught:
            parse_queue(queue)
        self.assertIn("same paper", str(caught.exception))

    def test_an_item_names_exactly_one_selector_and_a_supported_source(self) -> None:
        for item in (
            {"Source": "cnki"},
            {"Source": "cnki", "Title": "t", "DOI": "10.1/x"},
            {"Source": "googlescholar", "Title": "t"},
            {"Source": "cnki", "DOI": "not a doi"},
        ):
            with self.subTest(item=item), self.assertRaises(BatchQueueError):
                parse_queue({"BatchName": "B", "Items": [item]})

    def test_an_unknown_field_is_refused_so_a_typo_cannot_drop_a_selector(self) -> None:
        with self.assertRaises(BatchQueueError) as caught:
            parse_queue({"BatchName": "B", "Items": [{"Source": "cnki", "Tittle": "t", "Title": "t"}]})
        self.assertIn("tittle", str(caught.exception))

    def test_a_batch_name_cannot_climb_out_of_its_directory(self) -> None:
        for name in ("..", "../x", "a/b", ".hidden"):
            with self.subTest(name=name), self.assertRaises(BatchQueueError):
                parse_queue({"BatchName": name, "Items": [{"Source": "cnki", "Title": "t"}]})


class PlanningGateTests(_BatchCase):
    def test_more_than_ten_fetches_need_the_users_confirmation_and_nothing_runs_without_it(self) -> None:
        """Rule 41: above 10 planned downloads the user decides, not the agent."""

        publisher = _Publisher()
        code, report, notes = self.run_batch(_queue([f"paper {n}" for n in range(11)]), publisher)
        self.assertEqual(code, EXIT_HUMAN_ACTION_REQUIRED)
        self.assertEqual(report["Status"], "REFUSED_PLANNING_AND_BUDGET_GATE")
        self.assertTrue(report["PlanningAndBudgetGate"])
        self.assertEqual(publisher.calls, [])
        self.assertFalse((self.root / "batch").exists(), "a refused batch leaves no files")
        self.assertTrue(any("--confirm-budget" in note for note in notes))

    def test_with_the_users_confirmation_the_same_queue_runs(self) -> None:
        publisher = _Publisher()
        code, report, _notes = self.run_batch(
            _queue([f"paper {n}" for n in range(11)]), publisher, confirm_budget=True
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["ItemsDownloaded"], 11)

    def test_a_paper_already_in_the_library_is_not_searched_and_not_counted(self) -> None:
        held_file = self.root / "held.pdf"
        held_file.write_bytes(b"%PDF-1.7 held")
        catalog = self.root / "papers.jsonl"
        catalog.write_text(
            json.dumps(
                {
                    "paper_id": "PHELD0000001",
                    "doi": "10.1000/held",
                    "title": "A paper the library holds",
                    "authors": ["A"],
                    "year": "2024",
                    "journal": "J",
                    "sha256": "b" * 64,
                    "status": "MANAGED",
                    "versions": [
                        {
                            "sha256": "b" * 64,
                            "managed_path": str(held_file),
                            "full_text_format": "PDF",
                            "version_role": "CANONICAL_VERSION",
                            "source_type": "HARNESS_DOWNLOAD",
                            "status": "MANAGED",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        def navigator() -> PaperNavigator:
            reader = CatalogReader(catalog_path=catalog, topics_path=self.root / "topics.jsonl")
            return PaperNavigator(reader=reader, index=NavigatorIndex(root=self.root / "index"))

        queue = _queue([f"paper {n}" for n in range(10)])
        queue["Items"].append({"Source": "cnki", "Title": "A paper the library holds"})
        publisher = _Publisher()
        code, report, _notes = self.run_batch(queue, publisher, navigator_factory=navigator)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["PendingFetchCount"], 10, "a held paper does not count toward Rule 41")
        self.assertNotIn("A paper the library holds", publisher.calls)
        held = [item for item in report["Items"] if item["Outcome"] == "ALREADY_IN_LIBRARY"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["LibraryMatch"]["PaperID"], "PHELD0000001")
        self.assertEqual(Path(held[0]["LibraryMatch"]["PreferredPath"]), held_file)


class ExecutionTests(_BatchCase):
    def test_items_run_in_order_through_the_single_paper_path_attach_only_and_never_refetch(self) -> None:
        publisher = _Publisher()
        code, report, _notes = self.run_batch(_queue(["one", "two", "three"]), publisher)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["Status"], "COMPLETED")
        self.assertEqual(publisher.calls, ["one", "two", "three"])
        self.assertTrue(all(kwargs["require_attach"] for kwargs in publisher.browsers))
        self.assertTrue(all(adapter.allow_refetch is False for adapter in publisher.adapters))
        run_roots = [item["RunRoot"] for item in report["Items"]]
        self.assertEqual(len(set(run_roots)), 3, "each item keeps its own run evidence")
        for run_root in run_roots:
            self.assertTrue(Path(run_root).is_relative_to(self.root / "batch" / "items"))
        self.assertFalse(report["ParallelExecution"])
        self.assertFalse(research_chrome_lock.held_here(), "the batch lets go of the browser")

    def test_the_batch_holds_the_research_chrome_for_its_whole_length(self) -> None:
        seen: list[bool] = []

        class _Checking(_Publisher):
            def workflow_class(self):
                inner = super().workflow_class()

                class _Workflow(inner):
                    async def run(self, request):
                        seen.append(research_chrome_lock.held_here())
                        return await super().run(request)

                return _Workflow

        self.run_batch(_queue(["one", "two"]), _Checking())
        self.assertEqual(seen, [True, True])

    def test_the_batch_stops_at_the_first_human_gate_and_leaves_the_rest_pending(self) -> None:
        """Rule 72: a gate is handed back; the batch does not run past it."""

        publisher = _Publisher({"two": "login"})
        code, report, notes = self.run_batch(_queue(["one", "two", "three"]), publisher)
        self.assertEqual(code, EXIT_HUMAN_ACTION_REQUIRED)
        self.assertEqual(report["Status"], "STOPPED_FOR_HUMAN_ACTION")
        self.assertEqual(publisher.calls, ["one", "two"])
        self.assertTrue(report["ACTION_REQUIRED_USER_LOGIN"])
        self.assertEqual(report["Gate"]["ItemID"], "item-002")
        self.assertIn("slider", report["Gate"]["Reason"])
        self.assertNotIn("token=abc123", json.dumps(report, ensure_ascii=False))
        outcomes = [item["Outcome"] for item in report["Items"]]
        self.assertEqual(outcomes, ["DOWNLOADED", "HUMAN_ACTION_REQUIRED", "PENDING"])
        self.assertTrue(any("HUMAN ACTION REQUIRED" in note for note in notes))

    def test_rerunning_after_a_gate_skips_finished_items_and_starts_with_the_gated_one(self) -> None:
        self.run_batch(_queue(["one", "two", "three"]), _Publisher({"two": "login"}))
        publisher = _Publisher()
        code, report, _notes = self.run_batch(_queue(["one", "two", "three"]), publisher)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(publisher.calls, ["two", "three"])
        first = report["Items"][0]
        self.assertTrue(first["FromEarlierRun"])
        self.assertEqual(first["Outcome"], "DOWNLOADED")

    def test_a_failed_download_is_final_and_a_rerun_does_not_fetch_it_again(self) -> None:
        """Rule 71: the first fetched file is the evidence; no 'once more to see'."""

        self.run_batch(_queue(["one"]), _Publisher({"one": "download_failed"}))
        publisher = _Publisher()
        code, report, _notes = self.run_batch(_queue(["one"]), publisher)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(publisher.calls, [])
        self.assertEqual(report["Items"][0]["Outcome"], "DOWNLOAD_FAILED")

    def test_a_repeat_refusal_skips_one_paper_but_the_daily_total_stops_the_batch(self) -> None:
        publisher = _Publisher({"one": "repeat_limit", "two": "daily_budget"})
        code, report, _notes = self.run_batch(_queue(["one", "two", "three"]), publisher)
        self.assertEqual(code, EXIT_BUDGET_EXHAUSTED)
        self.assertEqual(report["Status"], "STOPPED_DAILY_BUDGET_EXHAUSTED")
        self.assertEqual(publisher.calls, ["one", "two"])
        outcomes = [item["Outcome"] for item in report["Items"]]
        self.assertEqual(outcomes, ["REPEAT_LIMIT_REACHED", "DAILY_BUDGET_EXHAUSTED", "PENDING"])

    def test_three_items_in_a_row_without_a_file_stop_the_batch(self) -> None:
        publisher = _Publisher(
            {"one": "no_results", "two": "not_authorized", "three": "download_failed"}
        )
        code, report, _notes = self.run_batch(_queue(["one", "two", "three", "four"]), publisher)
        self.assertEqual(code, EXIT_RUN_FAILED)
        self.assertEqual(report["Status"], "STOPPED_CONSECUTIVE_MISSES")
        self.assertNotIn("four", publisher.calls)

    def test_a_download_between_misses_resets_the_count(self) -> None:
        publisher = _Publisher(
            {"one": "no_results", "two": "no_results", "four": "no_results", "five": "no_results"}
        )
        code, report, _notes = self.run_batch(
            _queue(["one", "two", "three", "four", "five"]), publisher
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["Status"], "COMPLETED")
        self.assertEqual(len(publisher.calls), 5)

    def test_an_environment_failure_stops_the_batch(self) -> None:
        publisher = _Publisher({"one": SourceLayoutChanged("institutional route is not resolved")})
        code, report, _notes = self.run_batch(_queue(["one", "two"]), publisher)
        self.assertEqual(code, EXIT_ENV_NOT_READY)
        self.assertEqual(report["Status"], "STOPPED_ENV_NOT_READY")
        self.assertEqual(publisher.calls, ["one"])

    def test_an_unexpected_error_stops_the_batch_and_is_on_the_record(self) -> None:
        async def exploding(_item, _run_root) -> ItemRun:
            raise KeyError("a defect")

        batch = self.batch(_queue(["one", "two"]), item_runner=exploding)
        code, report, _notes = asyncio.run(batch.run())
        self.assertEqual(code, EXIT_RUN_FAILED)
        self.assertEqual(report["Status"], "STOPPED_UNEXPECTED_ERROR")
        self.assertEqual([row["Outcome"] for row in self.state_rows()], ["STARTED", "FAILED"])
        self.assertFalse(research_chrome_lock.held_here())

    def test_an_interrupted_item_is_run_again_and_said_so(self) -> None:
        self.run_batch(_queue(["one"]), _Publisher({"one": "login"}))
        state = self.root / "batch" / batch_module.STATE_FILENAME
        rows = self.state_rows()
        started = [row for row in rows if row["Outcome"] == "STARTED"][-1]
        with state.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(started, ensure_ascii=False) + "\n")
        publisher = _Publisher()
        code, _report, notes = self.run_batch(_queue(["one"]), publisher)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(publisher.calls, ["one"])
        self.assertTrue(any("interrupted" in note for note in notes))


class RefusalTests(_BatchCase):
    def test_without_a_running_research_chrome_the_batch_refuses_rather_than_launching(self) -> None:
        publisher = _Publisher()
        code, report, notes = self.run_batch(
            _queue(["one"]), publisher, browser_running=lambda: False
        )
        self.assertEqual(code, EXIT_ENV_NOT_READY)
        self.assertEqual(report["Status"], "REFUSED_RESEARCH_CHROME_NOT_RUNNING")
        self.assertEqual(publisher.calls, [])
        self.assertTrue(any("browser-start" in note for note in notes))

    def test_a_batch_refuses_while_another_process_drives_the_research_chrome(self) -> None:
        publisher = _Publisher()
        with held_by_another_process(research_chrome_lock.LOCK_PATH):
            code, report, _notes = self.run_batch(_queue(["one"]), publisher)
        self.assertEqual(code, EXIT_ENV_NOT_READY)
        self.assertEqual(report["Status"], "REFUSED_RESEARCH_CHROME_BUSY")
        self.assertEqual(publisher.calls, [])

    def test_a_spent_daily_budget_refuses_before_the_browser(self) -> None:
        publisher = _Publisher()
        code, report, _notes = self.run_batch(
            _queue(["one"]), publisher, ledger_factory=lambda: _Ledger(remaining=0)
        )
        self.assertEqual(code, EXIT_BUDGET_EXHAUSTED)
        self.assertEqual(report["Status"], "REFUSED_DAILY_BUDGET_EXHAUSTED")
        self.assertEqual(publisher.calls, [])

    def test_an_unreadable_state_refuses_rather_than_guessing_what_is_done(self) -> None:
        (self.root / "batch").mkdir(parents=True)
        (self.root / "batch" / batch_module.STATE_FILENAME).write_text("not json\n", encoding="utf-8")
        publisher = _Publisher()
        code, report, _notes = self.run_batch(_queue(["one"]), publisher)
        self.assertEqual(code, EXIT_RUN_FAILED)
        self.assertEqual(report["Status"], "REFUSED_STATE_UNREADABLE")
        self.assertEqual(publisher.calls, [])

    def test_a_dry_run_touches_no_browser_and_writes_nothing(self) -> None:
        publisher = _Publisher()
        with publisher.active():
            code, report, _notes = asyncio.run(self.batch(_queue(["one", "two"])).run(dry_run=True))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["Status"], "PLANNED")
        self.assertEqual(publisher.calls, [])
        self.assertEqual(publisher.browsers, [])
        self.assertFalse((self.root / "batch").exists())


class CommandLineTests(_BatchCase):
    def _main(self, argv: list[str]) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main_cli.main(argv)
        return code, json.loads(out.getvalue())

    def test_an_invalid_queue_is_one_json_document_and_exit_one(self) -> None:
        queue = self.root / "queue.json"
        queue.write_text(json.dumps({"BatchName": "B", "Items": []}), encoding="utf-8")
        code, payload = self._main(["acquire-batch", "--queue", str(queue)])
        self.assertEqual(code, EXIT_RUN_FAILED)
        self.assertEqual(payload["Status"], "REFUSED_INVALID_QUEUE")

    def test_the_command_plans_a_queue_without_running_it(self) -> None:
        queue = self.root / "queue.json"
        queue.write_text(json.dumps(_queue(["one", "two"]), ensure_ascii=False), encoding="utf-8")
        with patch.object(batch_module, "_research_chrome_running", lambda: False), patch.object(
            batch_module, "_default_navigator", lambda: None
        ):
            code, payload = self._main(
                [
                    "acquire-batch",
                    "--queue",
                    str(queue),
                    "--batch-root",
                    str(self.root / "batch"),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["Status"], "PLANNED")
        self.assertEqual(payload["PendingFetchCount"], 2)
        self.assertTrue(any("browser-start" in note for note in payload["HumanNotes"]))

    def test_a_batch_root_outside_the_output_root_is_refused(self) -> None:
        queue = self.root / "queue.json"
        queue.write_text(json.dumps(_queue(["one"])), encoding="utf-8")
        code, payload = self._main(
            [
                "acquire-batch",
                "--queue",
                str(queue),
                "--batch-root",
                str(Path(__file__).resolve().parent / "not-here"),
                "--dry-run",
            ]
        )
        self.assertEqual(code, EXIT_RUN_FAILED)
        self.assertIn("Output Root", payload["Reason"])


if __name__ == "__main__":
    unittest.main()
