"""A literature request that carries only the agent-route `Query` field.

Found live on 2026-11-21: `acquire --source cnki --request-json` was given
{"Query": "AI漂洗 分析师", ...}.  The literature request reads KeywordsCN /
KeywordsEN / ExactTitles / ... and has no `Query` field, so the planner made no
query, the run came back NO_RESULTS, and the Research Chrome never left the
ScienceDirect page it had restored -- which read like "CNKI found nothing".
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hunnu_harness.cli_output import CliReport
from hunnu_harness.exit_codes import EXIT_RUN_FAILED
from hunnu_harness.literature.cli import _run_live_into, build_parser
from hunnu_harness.literature.models import LiteratureSearchRequest
from hunnu_harness.literature.planning import LiteratureSearchPlanner
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow
from hunnu_harness.paths import TEMP_DIR

ORIGINAL = "示例议题的已发表中文期刊论文（写法样本）"


class QueryOnlyRequestTests(unittest.TestCase):
    def test_a_query_only_request_is_refused_and_names_the_fields_to_use(self) -> None:
        # If this fails, a request that would search nothing is accepted again
        # and comes back as a silent NO_RESULTS.
        with self.assertRaises(ValueError) as caught:
            LiteratureSearchRequest.from_mapping({"OriginalResearchRequest": ORIGINAL, "Query": "示例议题"})
        self.assertIn("KeywordsCN", str(caught.exception))
        self.assertIn("agent-route", str(caught.exception))

    def test_query_alongside_keywords_is_still_accepted_and_plans_the_keywords(self) -> None:
        # If this fails, the refusal became broader than the silent case it targets.
        request = LiteratureSearchRequest.from_mapping(
            {"OriginalResearchRequest": ORIGINAL, "Query": "示例议题", "KeywordsCN": ["示例议题"]}
        )
        plans = LiteratureSearchPlanner().plan(request)
        self.assertEqual([plan.query for plan in plans], ["示例议题"])

    def test_query_alongside_a_research_question_is_still_accepted(self) -> None:
        # The verbatim ResearchQuestion fallback still plans a search, so this
        # request is not the silent case.
        request = LiteratureSearchRequest.from_mapping(
            {"OriginalResearchRequest": ORIGINAL, "Query": "示例议题", "ResearchQuestion": "示例议题研究"}
        )
        self.assertTrue(LiteratureSearchPlanner().plan(request))

    def test_acquire_reports_a_query_only_request_as_invalid_before_chrome_starts(self) -> None:
        # If this fails, the Research Chrome is started (or a traceback escapes)
        # for a request that can search nothing.
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="query-only-", dir=TEMP_DIR) as raw:
            request_path = Path(raw) / "request.json"
            request_path.write_text(
                json.dumps({"OriginalResearchRequest": ORIGINAL, "Query": "示例议题"}, ensure_ascii=False),
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                ["live-cnki", f"--request-json={request_path}", f"--run-root={Path(raw) / 'run'}"]
            )
            report = CliReport(True)
            with patch(
                "hunnu_harness.literature.cli.PlaywrightBrowser",
                side_effect=AssertionError("the Research Chrome must not be touched"),
            ), patch.object(LiteratureAcquisitionWorkflow, "_rate_limit", new=AsyncMock()):
                exit_code, result = asyncio.run(_run_live_into(args, report))
        payload = report.payload()
        self.assertEqual(exit_code, EXIT_RUN_FAILED)
        self.assertIsNone(result)
        self.assertEqual(payload["Status"], "INVALID_REQUEST")
        self.assertIn("KeywordsCN", payload["Reason"])


if __name__ == "__main__":
    unittest.main()
