from __future__ import annotations

import unittest

from hunnu_harness.agent_entrypoint import AgentRequestRouter
from hunnu_harness.batching import (
    BoundedBatchCoordinator,
    BoundedBatchPlanner,
    PER_BATCH_MAX_DOWNLOADS,
    RetryableBatchError,
)


class BoundedBatchPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = BoundedBatchPlanner()

    def plan(self, downloads: int):
        return self.planner.plan(
            total_candidate_budget=max(downloads, 60),
            total_download_budget=downloads,
            per_batch_download_budget=25,
        )

    def test_required_download_boundaries(self) -> None:
        expected = {24: 1, 25: 1, 26: 2, 36: 2}
        for total, batch_count in expected.items():
            with self.subTest(total=total):
                plan = self.plan(total)
                self.assertEqual(len(plan.batches), batch_count)
                self.assertEqual(sum(batch.download_budget for batch in plan.batches), total)
                self.assertLessEqual(
                    max(batch.download_budget for batch in plan.batches),
                    PER_BATCH_MAX_DOWNLOADS,
                )

    def test_36_is_balanced_as_18_plus_18(self) -> None:
        plan = self.plan(36)
        self.assertEqual([batch.download_budget for batch in plan.batches], [18, 18])

    def test_quota_groups_are_an_extension_point_and_remain_bounded(self) -> None:
        groups = {"CIE": 9, "FR": 9, "WE": 9, "ITR": 9}
        plan = self.planner.plan(
            total_candidate_budget=80,
            total_download_budget=36,
            quota_groups=groups,
        )
        self.assertTrue(plan.quota_extension_supported)
        self.assertEqual(
            {batch.quota_group: batch.download_budget for batch in plan.batches},
            groups,
        )
        self.assertTrue(all(batch.download_budget <= 25 for batch in plan.batches))

    def test_per_batch_limit_cannot_be_raised(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 25"):
            self.planner.plan(
                total_candidate_budget=80,
                total_download_budget=36,
                per_batch_download_budget=26,
            )

    def test_router_exposes_multi_batch_without_relaxing_single_batch(self) -> None:
        decision = AgentRequestRouter().route(
            {
                "TaskType": "literature_search",
                "Query": "bounded candidates",
                "PreferredSources": ["CNKI"],
                "MaxCandidates": 80,
                "MaxDownloads": 25,
                "TotalCandidateBudget": 80,
                "TotalDownloadBudget": 36,
                "PerBatchDownloadBudget": 25,
            }
        )
        self.assertEqual(decision.status, "PLANNING_AND_BUDGET_GATE")
        self.assertEqual(decision.literature_plans[0].max_downloads, 25)
        self.assertEqual(decision.multi_batch_plan.total_download_budget, 36)
        self.assertEqual(len(decision.multi_batch_plan.batches), 2)


class BoundedBatchCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_total_never_exceeds_requested_budget(self) -> None:
        plan = BoundedBatchPlanner().plan(
            total_candidate_budget=80, total_download_budget=36
        )
        summary = await BoundedBatchCoordinator().execute(
            plan, lambda batch, _attempt: batch.download_budget
        )
        self.assertEqual(summary.actual_downloads, 36)
        self.assertLessEqual(summary.actual_downloads, plan.total_download_budget)

    async def test_failures_have_bounded_retries_and_no_new_batches(self) -> None:
        plan = BoundedBatchPlanner().plan(
            total_candidate_budget=80,
            total_download_budget=36,
            max_retries=2,
        )
        calls = []

        def fail(batch, attempt):
            calls.append((batch.batch_id, attempt))
            raise RetryableBatchError("bounded failure", downloads_completed=0)

        summary = await BoundedBatchCoordinator().execute(plan, fail)
        self.assertEqual(len(summary.results), len(plan.batches))
        self.assertTrue(all(result.attempts == 3 for result in summary.results))
        self.assertEqual(len(calls), len(plan.batches) * 3)
        self.assertEqual(summary.actual_downloads, 0)

    async def test_unknown_failure_is_not_retried(self) -> None:
        plan = BoundedBatchPlanner().plan(
            total_candidate_budget=24,
            total_download_budget=24,
            max_retries=3,
        )
        calls = []

        def unknown_failure(batch, attempt):
            calls.append((batch.batch_id, attempt))
            raise RuntimeError("commit state unknown")

        summary = await BoundedBatchCoordinator().execute(plan, unknown_failure)
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary.results[0].attempts, 1)
        self.assertFalse(summary.results[0].success)

    async def test_retryable_partial_failure_is_counted_in_total_budget(self) -> None:
        plan = BoundedBatchPlanner().plan(
            total_candidate_budget=24,
            total_download_budget=24,
            max_retries=1,
        )

        def partial_then_complete(batch, attempt):
            if attempt == 1:
                raise RetryableBatchError("partial", downloads_completed=4)
            return batch.download_budget - 4

        summary = await BoundedBatchCoordinator().execute(plan, partial_then_complete)
        self.assertEqual(summary.actual_downloads, 24)
        self.assertEqual(summary.results[0].downloads_completed, 24)
        self.assertTrue(summary.results[0].success)

    async def test_runner_cannot_report_more_than_batch_budget(self) -> None:
        plan = BoundedBatchPlanner().plan(
            total_candidate_budget=24,
            total_download_budget=24,
            max_retries=0,
        )
        summary = await BoundedBatchCoordinator().execute(
            plan, lambda batch, _attempt: batch.download_budget + 1
        )
        self.assertFalse(summary.results[0].success)
        self.assertEqual(summary.actual_downloads, 0)


if __name__ == "__main__":
    unittest.main()
