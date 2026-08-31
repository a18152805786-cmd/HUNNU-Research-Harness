"""Task 3.3: the graded exit ladder for the acquisition surface.

An agent that never parses JSON must still stop at the right gates: 2 means
"get a human", 3 means "today's budget is spent -- the knob belongs to the
user", 4 means "install the missing capability", 5 means "fix the
environment". Retrying past any of these is exactly the failure mode the
ladder exists to prevent.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hunnu_harness.exit_codes import (
    EXIT_BUDGET_EXHAUSTED,
    EXIT_CAPABILITY_MISSING,
    EXIT_ENV_NOT_READY,
    EXIT_HUMAN_ACTION_REQUIRED,
    EXIT_OK,
    EXIT_RUN_FAILED,
)
from hunnu_harness.literature import cli as literature_cli
from hunnu_harness.literature.cli import _live_exit_code
from hunnu_harness.literature.fetch_ledger import (
    STATUS_BUDGET_EXHAUSTED,
    FetchBudgetExceeded,
)
from hunnu_harness.literature.models import RunStatus
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow

from test_literature_workflow import _MockAdapter, request


def _result(status: RunStatus, *, downloads: int = 0) -> SimpleNamespace:
    return SimpleNamespace(status=status, downloads=[object()] * downloads, records=[])


class LiveExitLadderTests(unittest.TestCase):
    def test_human_gates_exit_two(self) -> None:
        for status in (RunStatus.ACTION_REQUIRED_USER_LOGIN, RunStatus.ACTION_REQUIRED_USER_DOWNLOAD):
            self.assertEqual(_live_exit_code(_result(status), 0), EXIT_HUMAN_ACTION_REQUIRED)

    def test_budget_exit_three_only_when_nothing_was_downloaded(self) -> None:
        partial = _result(RunStatus.PARTIAL_SUCCESS)
        self.assertEqual(_live_exit_code(partial, 2), EXIT_BUDGET_EXHAUSTED)
        partial_with_file = _result(RunStatus.PARTIAL_SUCCESS, downloads=1)
        self.assertEqual(_live_exit_code(partial_with_file, 2), EXIT_OK)

    def test_completed_runs_exit_zero_including_zero_hits(self) -> None:
        for status in (RunStatus.SUCCESS, RunStatus.PARTIAL_SUCCESS, RunStatus.NO_RESULTS):
            self.assertEqual(_live_exit_code(_result(status, downloads=1), 0), EXIT_OK)

    def test_environment_statuses_exit_five(self) -> None:
        for status in (RunStatus.SOURCE_UNAVAILABLE, RunStatus.SOURCE_LAYOUT_CHANGED):
            self.assertEqual(_live_exit_code(_result(status), 0), EXIT_ENV_NOT_READY)

    def test_everything_else_exits_one(self) -> None:
        self.assertEqual(_live_exit_code(_result(RunStatus.DOWNLOAD_FAILED), 0), EXIT_RUN_FAILED)


class _BudgetRefusedAdapter(_MockAdapter):
    async def download_fulltext(self, record, access):
        raise FetchBudgetExceeded(
            STATUS_BUDGET_EXHAUSTED,
            "You have fetched 15 full texts from publishers today.",
        )


class WorkflowBudgetBranchTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_budget_refusal_carries_the_ledger_status_not_download_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = LiteratureAcquisitionWorkflow(
                _BudgetRefusedAdapter(root),
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            )
            result = await workflow.run(request())
        self.assertEqual(result.status, RunStatus.PARTIAL_SUCCESS)
        self.assertEqual(result.downloads, [])
        self.assertEqual(result.records[0].error_status, STATUS_BUDGET_EXHAUSTED)


class LiveCliBudgetExitTests(unittest.TestCase):
    def test_a_fully_refused_run_exits_three_and_reports_the_refusals(self) -> None:
        class _QuietBrowser:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def start(self) -> None:
                return None

            async def lifecycle(self) -> dict:
                return {}

            async def close(self) -> None:
                return None

        refused_record = SimpleNamespace(error_status=STATUS_BUDGET_EXHAUSTED)
        fake_result = SimpleNamespace(
            status=RunStatus.PARTIAL_SUCCESS, records=[refused_record], downloads=[]
        )

        class _FakeWorkflow:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            async def run(self, _request: object) -> SimpleNamespace:
                return fake_result

        out = io.StringIO()
        with patch.object(literature_cli, "PlaywrightBrowser", _QuietBrowser), patch.object(
            literature_cli, "LiteratureAcquisitionWorkflow", _FakeWorkflow
        ):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = literature_cli.main(["live-cnki", "--title", "x", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, EXIT_BUDGET_EXHAUSTED)
        self.assertEqual(payload["FetchBudgetRefusals"], 1)
        self.assertEqual(payload["Downloads"], 0)


class ExceptionLadderTests(unittest.TestCase):
    def test_missing_playwright_is_a_missing_capability(self) -> None:
        class _Unavailable:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def start(self) -> None:
                raise literature_cli.PlaywrightUnavailable("install the browser extra")

            async def lifecycle(self) -> dict:
                return {}

            async def close(self) -> None:
                return None

        with patch.object(literature_cli, "PlaywrightBrowser", _Unavailable):
            with contextlib.redirect_stdout(io.StringIO()):
                code = literature_cli.main(["live-cnki", "--title", "x"])
        self.assertEqual(code, EXIT_CAPABILITY_MISSING)

    def test_a_locked_profile_is_an_unready_environment(self) -> None:
        class _Locked:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def start(self) -> None:
                raise literature_cli.ProfileLockedError("profile appears locked")

            async def lifecycle(self) -> dict:
                return {}

            async def close(self) -> None:
                return None

        with patch.object(literature_cli, "PlaywrightBrowser", _Locked):
            with contextlib.redirect_stdout(io.StringIO()):
                code = literature_cli.main(["live-cnki", "--title", "x"])
        self.assertEqual(code, EXIT_ENV_NOT_READY)


if __name__ == "__main__":
    unittest.main()
