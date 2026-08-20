"""Bounded multi-batch planning and execution for research downloads."""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping


PER_BATCH_MAX_DOWNLOADS = 25
DEFAULT_PER_BATCH_MAX_CANDIDATES = 200


@dataclass(frozen=True)
class ResearchBatch:
    batch_id: str
    candidate_budget: int
    download_budget: int
    quota_group: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "BatchID": self.batch_id,
            "QuotaGroup": self.quota_group or "UNASSIGNED",
            "CandidateBudget": self.candidate_budget,
            "DownloadBudget": self.download_budget,
        }


@dataclass(frozen=True)
class MultiBatchPlan:
    total_candidate_budget: int
    total_download_budget: int
    per_batch_candidate_budget: int
    per_batch_download_budget: int
    max_retries: int
    batches: tuple[ResearchBatch, ...]
    quota_extension_supported: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "TotalCandidateBudget": self.total_candidate_budget,
            "TotalDownloadBudget": self.total_download_budget,
            "PerBatchCandidateBudget": self.per_batch_candidate_budget,
            "PerBatchDownloadBudget": self.per_batch_download_budget,
            "MaxRetries": self.max_retries,
            "BatchCount": len(self.batches),
            "QuotaExtensionSupported": self.quota_extension_supported,
            "Batches": [batch.as_dict() for batch in self.batches],
        }


class BoundedBatchPlanner:
    def plan(
        self,
        *,
        total_candidate_budget: int,
        total_download_budget: int,
        per_batch_candidate_budget: int = DEFAULT_PER_BATCH_MAX_CANDIDATES,
        per_batch_download_budget: int = PER_BATCH_MAX_DOWNLOADS,
        max_retries: int = 1,
        quota_groups: Mapping[str, int] | None = None,
    ) -> MultiBatchPlan:
        if total_candidate_budget < 0 or total_download_budget < 0:
            raise ValueError("Total budgets must be non-negative")
        if total_download_budget > total_candidate_budget:
            raise ValueError("TotalDownloadBudget cannot exceed TotalCandidateBudget")
        if per_batch_download_budget < 1 or per_batch_download_budget > PER_BATCH_MAX_DOWNLOADS:
            raise ValueError("PerBatchDownloadBudget must be between 1 and 25")
        if per_batch_candidate_budget < 1:
            raise ValueError("PerBatchCandidateBudget must be positive")
        if max_retries < 0 or max_retries > 3:
            raise ValueError("MaxRetries must be between 0 and 3")

        groups = dict(quota_groups or {})
        if groups:
            if any(not str(group).strip() or int(quota) < 0 for group, quota in groups.items()):
                raise ValueError("Quota groups require non-empty names and non-negative quotas")
            if sum(int(quota) for quota in groups.values()) != total_download_budget:
                raise ValueError("Quota group totals must equal TotalDownloadBudget")
            download_parts: list[tuple[str | None, int]] = []
            for group, quota_value in groups.items():
                quota = int(quota_value)
                while quota > 0:
                    current = min(quota, per_batch_download_budget)
                    download_parts.append((str(group), current))
                    quota -= current
        elif total_download_budget:
            count = math.ceil(total_download_budget / per_batch_download_budget)
            quotient, remainder = divmod(total_download_budget, count)
            download_parts = [
                (None, quotient + (1 if index < remainder else 0))
                for index in range(count)
            ]
        else:
            download_parts = [(None, 0)]

        required_candidate_batches = max(1, math.ceil(total_candidate_budget / per_batch_candidate_budget))
        while len(download_parts) < required_candidate_batches:
            download_parts.append((None, 0))
        candidate_quotient, candidate_remainder = divmod(total_candidate_budget, len(download_parts))
        if candidate_quotient + bool(candidate_remainder) > per_batch_candidate_budget:
            raise ValueError("Candidate budget could not be bounded by the planned batches")
        batches = tuple(
            ResearchBatch(
                batch_id=f"batch-{index + 1:03d}",
                candidate_budget=candidate_quotient + (1 if index < candidate_remainder else 0),
                download_budget=downloads,
                quota_group=group,
            )
            for index, (group, downloads) in enumerate(download_parts)
        )
        if any(batch.download_budget > PER_BATCH_MAX_DOWNLOADS for batch in batches):
            raise AssertionError("Planner produced a batch above the hard safety limit")
        return MultiBatchPlan(
            total_candidate_budget=total_candidate_budget,
            total_download_budget=total_download_budget,
            per_batch_candidate_budget=per_batch_candidate_budget,
            per_batch_download_budget=per_batch_download_budget,
            max_retries=max_retries,
            batches=batches,
        )


@dataclass(frozen=True)
class BatchExecutionResult:
    batch_id: str
    attempts: int
    downloads_completed: int
    success: bool
    error: str = ""


@dataclass(frozen=True)
class MultiBatchExecutionSummary:
    planned_downloads: int
    actual_downloads: int
    results: tuple[BatchExecutionResult, ...]


BatchRunner = Callable[[ResearchBatch, int], int | Awaitable[int]]


class RetryableBatchError(RuntimeError):
    """A bounded failure with an explicit committed-download count."""

    def __init__(self, message: str, *, downloads_completed: int = 0) -> None:
        super().__init__(message)
        if downloads_completed < 0:
            raise ValueError("downloads_completed must be non-negative")
        self.downloads_completed = downloads_completed


class BoundedBatchCoordinator:
    async def execute(self, plan: MultiBatchPlan, runner: BatchRunner) -> MultiBatchExecutionSummary:
        actual_total = 0
        results: list[BatchExecutionResult] = []
        for batch in plan.batches:
            attempts = 0
            completed = 0
            error = ""
            success = False
            for attempt in range(1, plan.max_retries + 2):
                attempts = attempt
                try:
                    value = runner(batch, attempt)
                    newly_completed = int(await value) if inspect.isawaitable(value) else int(value)
                    remaining = batch.download_budget - completed
                    if newly_completed < 0 or newly_completed > remaining:
                        raise ValueError("Batch runner exceeded its download budget")
                    if actual_total + newly_completed > plan.total_download_budget:
                        raise ValueError("Multi-batch execution exceeded TotalDownloadBudget")
                    actual_total += newly_completed
                    completed += newly_completed
                    success = True
                    break
                except RetryableBatchError as exc:
                    committed = int(exc.downloads_completed)
                    remaining = batch.download_budget - completed
                    if committed > remaining:
                        error = "RetryableBatchError: committed downloads exceeded remaining batch budget"
                        break
                    if actual_total + committed > plan.total_download_budget:
                        error = "RetryableBatchError: committed downloads exceeded total budget"
                        break
                    completed += committed
                    actual_total += committed
                    error = f"{type(exc).__name__}: {exc}"
                    if completed >= batch.download_budget:
                        success = True
                        error = ""
                        break
                except Exception as exc:
                    # An arbitrary exception cannot prove whether downloads
                    # were committed. Stop this batch instead of risking an
                    # unaccounted duplicate through automatic retry.
                    error = f"{type(exc).__name__}: {exc}"
                    break
            results.append(
                BatchExecutionResult(
                    batch_id=batch.batch_id,
                    attempts=attempts,
                    downloads_completed=completed,
                    success=success,
                    error=error if not success else "",
                )
            )
        return MultiBatchExecutionSummary(
            planned_downloads=plan.total_download_budget,
            actual_downloads=actual_total,
            results=tuple(results),
        )


__all__ = [
    "BoundedBatchCoordinator",
    "BoundedBatchPlanner",
    "MultiBatchExecutionSummary",
    "MultiBatchPlan",
    "PER_BATCH_MAX_DOWNLOADS",
    "ResearchBatch",
    "RetryableBatchError",
]
