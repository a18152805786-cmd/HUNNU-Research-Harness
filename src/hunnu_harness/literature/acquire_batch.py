"""Run a queue of papers through the single-paper acquisition path, one at a time.

Measured before this existed, from the Output Root audit and run logs:

* 2026-09-23, an agent driving one search at a time: 63 minutes of wall
  clock, of which the Harness itself ran about 9.  Every search waited 0 s for
  pacing; the time went to the agent's own turns between runs.
* 2026-09-22, a script looping over 29 CNKI papers: 0.6 s between papers and
  a median of 104 s per paper, about half of it CNKI delivering the PDF, with
  the search rate already at the default ceiling of 6 per 10 minutes.

So the time to win is the agent's per-paper round trip, not the papers
themselves -- and running papers side by side would win nothing and break
things: every run shares one Research Chrome tab, one browser-wide download
directory, and pacing ledgers that are global.  A batch therefore runs strictly
one item after another, holding the Research Chrome lock for its whole length.

Every item is an ordinary ``live-<source>`` run (``literature.cli.
_run_live_into``): search pacing, the write-ahead fetch ledger, the identity
lock, validation, SHA-256, manifest, archive and classification are the
single-paper ones, unchanged.  A batch never passes ``--allow-refetch``, never
waits for a person, and stops entirely at the first manual gate (AGENTS.md
Rule 72).  It resumes by being run again: an item with a recorded final
outcome is skipped, so only the gated item and the ones after it run.

TO THE MODIFYING AGENT: the per-queue ceiling (Rule 62's 25), the confirmation
gate above 10 fetches (Rule 41), the stop at the first human gate, the stop
after three consecutive items without a download, and the sequential,
lock-holding execution are guards.  Running items in parallel, retrying a gated
item on its own, or dropping a stop condition weakens them and requires asking
the user first, in so many words.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from ..agent_entrypoint import HIGH_COST_DOWNLOAD_THRESHOLD
from ..batching import PER_BATCH_MAX_DOWNLOADS
from ..browser import research_chrome_lock
from ..exit_codes import (
    EXIT_BUDGET_EXHAUSTED,
    EXIT_CAPABILITY_MISSING,
    EXIT_ENV_NOT_READY,
    EXIT_HUMAN_ACTION_REQUIRED,
    EXIT_OK,
    EXIT_RUN_FAILED,
)
from ..paths import RUNS_ROOT, _logical_path, _windows_io_path, require_output_path
from .fetch_ledger import (
    STATUS_BUDGET_EXHAUSTED,
    FetchLedgerError,
    FulltextFetchLedger,
)
from .models import UNKNOWN, RunStatus
from .normalization import normalize_doi, normalize_title
from .security import sanitize_value

SOURCES = ("cnki", "sciencedirect", "springerlink", "oxfordacademic")
QUEUE_ITEM_CEILING = PER_BATCH_MAX_DOWNLOADS
# Rule 41's planned-download count above which the user must confirm; the
# Agent router's gate and this one are the same number by construction.
CONFIRMATION_THRESHOLD = HIGH_COST_DOWNLOAD_THRESHOLD
# Three items in a row that reached the publisher and came back without a file
# is no longer bad luck: the queue is wrong, or the source has started refusing
# quietly.  Either way a person should look before the batch spends more.
CONSECUTIVE_MISS_LIMIT = 3

BATCH_RUNS_ROOT = RUNS_ROOT / "AcquireBatch"
STATE_FILENAME = "BATCH_STATE.jsonl"
REPORT_FILENAME = "BATCH_REPORT.json"
QUEUE_SNAPSHOT_FILENAME = "QUEUE.json"

# Item outcomes.
DOWNLOADED = "DOWNLOADED"
ALREADY_IN_LIBRARY = "ALREADY_IN_LIBRARY"
NOT_FOUND = "NOT_FOUND"
NOT_DOWNLOADED = "NOT_DOWNLOADED"
NOT_AUTHORIZED = "NOT_AUTHORIZED"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
FAILED = "FAILED"
REPEAT_LIMIT_REACHED = "REPEAT_LIMIT_REACHED"
HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
DAILY_BUDGET_EXHAUSTED = "DAILY_BUDGET_EXHAUSTED"
ENV_NOT_READY = "ENV_NOT_READY"
CAPABILITY_MISSING = "CAPABILITY_MISSING"
STARTED = "STARTED"
PENDING = "PENDING"

# A final outcome: running the batch again skips the item.  A failed download
# is final on purpose -- Rule 71: the first fetched file is the evidence, and
# "run it once more to see" spends quota.  Re-fetching one paper is a
# deliberate single `acquire --allow-refetch` with a stated reason.
TERMINAL_OUTCOMES = frozenset(
    {
        DOWNLOADED,
        ALREADY_IN_LIBRARY,
        NOT_FOUND,
        NOT_DOWNLOADED,
        NOT_AUTHORIZED,
        DOWNLOAD_FAILED,
        FAILED,
        REPEAT_LIMIT_REACHED,
    }
)
# Outcomes that reached the publisher and came back without a file.
MISS_OUTCOMES = frozenset({NOT_FOUND, NOT_DOWNLOADED, NOT_AUTHORIZED, DOWNLOAD_FAILED, FAILED})

# Batch statuses and their exits on the shared ladder (see exit_codes).
BATCH_EXIT_CODES = {
    "COMPLETED": EXIT_OK,
    "PLANNED": EXIT_OK,
    "STOPPED_FOR_HUMAN_ACTION": EXIT_HUMAN_ACTION_REQUIRED,
    "STOPPED_DAILY_BUDGET_EXHAUSTED": EXIT_BUDGET_EXHAUSTED,
    "STOPPED_CAPABILITY_MISSING": EXIT_CAPABILITY_MISSING,
    "STOPPED_ENV_NOT_READY": EXIT_ENV_NOT_READY,
    "STOPPED_CONSECUTIVE_MISSES": EXIT_RUN_FAILED,
    "STOPPED_UNEXPECTED_ERROR": EXIT_RUN_FAILED,
    "REFUSED_INVALID_QUEUE": EXIT_RUN_FAILED,
    "REFUSED_STATE_UNREADABLE": EXIT_RUN_FAILED,
    "REFUSED_PLANNING_AND_BUDGET_GATE": EXIT_HUMAN_ACTION_REQUIRED,
    "REFUSED_DAILY_BUDGET_EXHAUSTED": EXIT_BUDGET_EXHAUSTED,
    "REFUSED_FETCH_LEDGER_UNREADABLE": EXIT_ENV_NOT_READY,
    "REFUSED_RESEARCH_CHROME_NOT_RUNNING": EXIT_ENV_NOT_READY,
    "REFUSED_RESEARCH_CHROME_BUSY": EXIT_ENV_NOT_READY,
}
_STOP_FOR_OUTCOME = {
    HUMAN_ACTION_REQUIRED: "STOPPED_FOR_HUMAN_ACTION",
    DAILY_BUDGET_EXHAUSTED: "STOPPED_DAILY_BUDGET_EXHAUSTED",
    CAPABILITY_MISSING: "STOPPED_CAPABILITY_MISSING",
    ENV_NOT_READY: "STOPPED_ENV_NOT_READY",
}

_NAME_PATTERN = re.compile(r"[\w.-]{1,80}")
_ITEM_ID_PATTERN = re.compile(r"[\w.-]{1,40}")
_QUEUE_KEYS = {"batchname", "items"}
_ITEM_KEYS = {"source", "title", "doi", "itemid", "note"}


class BatchQueueError(ValueError):
    """The queue file cannot be run as written; nothing was started."""


class BatchStateError(RuntimeError):
    """The batch state file cannot be trusted; nothing was started."""


@dataclass(frozen=True)
class BatchItem:
    index: int
    item_id: str
    source: str
    title: str
    doi: str
    key: str
    note: str = ""

    def selector(self) -> tuple[str, str]:
        return ("--doi", self.doi) if self.doi else ("--title", self.title)

    def as_dict(self) -> dict[str, Any]:
        return {
            "Index": self.index,
            "ItemID": self.item_id,
            "Source": self.source,
            "Title": self.title or None,
            "DOI": self.doi or None,
            "Note": self.note or None,
        }


@dataclass(frozen=True)
class BatchQueue:
    name: str
    items: tuple[BatchItem, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"BatchName": self.name, "Items": [item.as_dict() for item in self.items]}


def _folded(mapping: Mapping[str, Any]) -> dict[str, Any]:
    folded: dict[str, Any] = {}
    for key, value in mapping.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
        if normalized in folded:
            raise BatchQueueError(f"field {key!r} is given twice under different spellings")
        folded[normalized] = value
    return folded


def _text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def parse_queue(payload: Any, *, default_name: str = "") -> BatchQueue:
    """Validate a queue document, or raise BatchQueueError saying what to fix."""

    if not isinstance(payload, Mapping):
        raise BatchQueueError('a queue is a JSON object: {"BatchName": ..., "Items": [...]}')
    fields = _folded(payload)
    unknown = sorted(set(fields) - _QUEUE_KEYS)
    if unknown:
        raise BatchQueueError(f"unknown queue field(s): {', '.join(unknown)}")
    name = _text(fields.get("batchname")) or default_name
    if not name or not _NAME_PATTERN.fullmatch(name) or name.startswith("."):
        raise BatchQueueError(
            "BatchName must be 1-80 letters, digits, '_', '-' or '.', not starting with '.'"
        )
    raw_items = fields.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise BatchQueueError("Items must be a non-empty list")
    if len(raw_items) > QUEUE_ITEM_CEILING:
        raise BatchQueueError(
            f"a queue holds at most {QUEUE_ITEM_CEILING} items (AGENTS.md Rule 62); "
            f"this one has {len(raw_items)}. Split it into several queues and run them one after another."
        )

    items: list[BatchItem] = []
    seen_keys: dict[str, int] = {}
    seen_ids: dict[str, int] = {}
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, Mapping):
            raise BatchQueueError(f"item {index} is not an object")
        item_fields = _folded(raw)
        unknown = sorted(set(item_fields) - _ITEM_KEYS)
        if unknown:
            raise BatchQueueError(f"item {index} has unknown field(s): {', '.join(unknown)}")
        source = _text(item_fields.get("source")).casefold()
        if source not in SOURCES:
            raise BatchQueueError(
                f"item {index}: Source must be one of {', '.join(SOURCES)}; got {item_fields.get('source')!r}"
            )
        title = _text(item_fields.get("title"))
        doi_text = _text(item_fields.get("doi"))
        if bool(title) == bool(doi_text):
            raise BatchQueueError(f"item {index}: give exactly one of Title or DOI")
        doi = ""
        if doi_text:
            doi = normalize_doi(doi_text)
            if doi == UNKNOWN:
                raise BatchQueueError(f"item {index}: {doi_text!r} is not a DOI")
            key = f"doi:{doi}"
        else:
            normalized = normalize_title(title)
            if normalized == UNKNOWN:
                raise BatchQueueError(f"item {index}: the title is empty after normalization")
            key = f"title:{normalized}"
        item_id = _text(item_fields.get("itemid")) or f"item-{index:03d}"
        if not _ITEM_ID_PATTERN.fullmatch(item_id):
            raise BatchQueueError(
                f"item {index}: ItemID must be 1-40 letters, digits, '_', '-' or '.'"
            )
        if key in seen_keys:
            raise BatchQueueError(
                f"items {seen_keys[key]} and {index} name the same paper; "
                "queue each paper once"
            )
        if item_id in seen_ids:
            raise BatchQueueError(f"items {seen_ids[item_id]} and {index} share ItemID {item_id!r}")
        seen_keys[key] = index
        seen_ids[item_id] = index
        items.append(
            BatchItem(
                index=index,
                item_id=item_id,
                source=source,
                title=title,
                doi=doi,
                key=key,
                note=_text(item_fields.get("note"))[:200],
            )
        )
    return BatchQueue(name=name, items=tuple(items))


def load_queue(path: Path) -> BatchQueue:
    io_path = _windows_io_path(Path(path))
    try:
        payload = json.loads(io_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise BatchQueueError(f"cannot read the queue file: {exc}") from exc
    except ValueError as exc:
        raise BatchQueueError(f"the queue file is not JSON: {exc}") from exc
    stem = Path(path).stem
    default_name = stem if _NAME_PATTERN.fullmatch(stem) and not stem.startswith(".") else ""
    return parse_queue(payload, default_name=default_name)


class BatchState:
    """Append-only record of every item outcome, keyed by paper identity."""

    def __init__(self, path: Path) -> None:
        self.path = require_output_path(Path(path), label="Acquire-batch state")

    def latest_by_key(self) -> dict[str, dict[str, Any]]:
        """The last row for each paper, or raise on a line that is not a state row.

        The state decides which papers are skipped; a guard that ignored what
        it could not read would re-run a paper whose download already failed.
        """

        io_path = _windows_io_path(self.path)
        if not io_path.exists():
            return {}
        try:
            text = io_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BatchStateError(f"batch state is unreadable: {exc}") from exc
        latest: dict[str, dict[str, Any]] = {}
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise BatchStateError(f"batch state line {number} is not JSON: {exc}") from exc
            if (
                not isinstance(row, Mapping)
                or not str(row.get("Key", "")).strip()
                or not str(row.get("Outcome", "")).strip()
            ):
                raise BatchStateError(f"batch state line {number} is not a state row")
            latest[str(row["Key"])] = dict(row)
        return latest

    def append(self, row: Mapping[str, Any]) -> None:
        io_path = _windows_io_path(self.path)
        io_path.parent.mkdir(parents=True, exist_ok=True)
        with io_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(sanitize_value(dict(row)), ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()


@dataclass
class ItemRun:
    """What one item's single-paper run returned."""

    exit_code: int
    result: Any
    report: dict[str, Any]


ItemRunner = Callable[[BatchItem, Path], Awaitable[ItemRun]]


def live_item_runner(*, daily_limit: int | None = None) -> ItemRunner:
    """Run an item exactly as ``hunnu-harness acquire`` would, attach-only."""

    async def run(item: BatchItem, run_root: Path) -> ItemRun:
        from ..cli_output import CliReport
        from .cli import _run_live_into, build_parser

        flag, value = item.selector()
        # "--flag=value" so a title that starts with "-" is not read as an option.
        argv = [f"live-{item.source}", f"{flag}={value}", f"--run-root={run_root}"]
        if daily_limit is not None:
            argv.append(f"--daily-limit={daily_limit}")
        args = build_parser().parse_args(argv)
        args.require_attached_browser = True
        report = CliReport(True)
        exit_code, result = await _run_live_into(args, report)
        return ItemRun(exit_code=exit_code, result=result, report=report.payload())

    return run


def _default_navigator() -> Any:
    from ..navigator.catalog import LibraryUnavailable
    from ..navigator.search import PaperNavigator

    try:
        return PaperNavigator()
    except LibraryUnavailable:
        # No catalog yet: nothing can be held, which is an answer, not a failure.
        return None


def held_match(navigator: Any, item: BatchItem) -> dict[str, Any] | None:
    """The library's copy of this exact paper, when one with full text is held.

    Exact identity only -- a DOI, or a title that normalizes to the same text
    and names exactly one work.  A near miss is never a reason to skip a paper,
    and a work whose file is missing does not count as held (Rule 29).
    """

    if navigator is None:
        return None
    from ..navigator.resolver import FullTextStatus

    snapshot = navigator.snapshot
    if item.doi:
        work = snapshot.by_doi(item.doi)
        candidates = [(work, "doi")] if work is not None else []
    else:
        works = snapshot.by_normalized_title(item.title)
        candidates = [(works[0], "title")] if len(works) == 1 else []
    for work, matched_on in candidates:
        resolution = navigator.resolution_for(work)
        if resolution.fulltext_status in {
            FullTextStatus.AVAILABLE,
            FullTextStatus.AVAILABLE_NOT_MACHINE_READABLE,
        }:
            preferred = resolution.preferred
            return {
                "PaperID": work.paper_id,
                "Title": work.title,
                "DOI": work.doi,
                "Journal": work.journal,
                "Year": work.year,
                "MatchedOn": matched_on,
                "FulltextStatus": resolution.fulltext_status.value,
                # Rule 65: the only path to open is the one the Navigator names.
                "PreferredPath": preferred.as_dict().get("absolute_path") if preferred else None,
                "Topics": list(work.topic_labels),
            }
    return None


def _download_summaries(result: Any) -> list[dict[str, Any]]:
    summaries = []
    for entry in list(getattr(result, "downloads", None) or []):
        summaries.append(
            {
                "PaperID": getattr(entry, "paper_id", UNKNOWN),
                "Title": getattr(entry, "title", UNKNOWN),
                "DOI": getattr(entry, "doi", UNKNOWN),
                "Format": getattr(entry, "full_text_format", UNKNOWN),
                "SHA256": getattr(entry, "sha256", UNKNOWN),
                "LibraryDisposition": getattr(entry, "library_disposition", UNKNOWN),
                "LibraryManagedPath": getattr(entry, "library_managed_path", UNKNOWN),
                "ClassificationStatus": getattr(entry, "classification_status", UNKNOWN),
                "PrimaryTopic": getattr(entry, "assigned_primary_topic", UNKNOWN),
            }
        )
    return summaries


def _run_reason(run: ItemRun) -> str:
    reason = run.report.get("Reason")
    if reason and reason != UNKNOWN:
        return str(reason)
    result = run.result
    for error in list(getattr(result, "errors", None) or []):
        if error and error != UNKNOWN:
            return str(error)
    for record in list(getattr(result, "records", None) or []):
        detail = getattr(record, "error_reason", None)
        if detail and detail != UNKNOWN:
            return str(detail)
    return ""


def classify_item_run(run: ItemRun) -> str:
    """Map one single-paper run onto a batch item outcome."""

    code = run.exit_code
    if code == EXIT_HUMAN_ACTION_REQUIRED:
        return HUMAN_ACTION_REQUIRED
    if code == EXIT_BUDGET_EXHAUSTED:
        refusals = {
            getattr(record, "error_status", None)
            for record in list(getattr(run.result, "records", None) or [])
        }
        # The daily total stops every later item too; a per-paper repeat
        # refusal is a verdict about this paper alone.
        return DAILY_BUDGET_EXHAUSTED if STATUS_BUDGET_EXHAUSTED in refusals else REPEAT_LIMIT_REACHED
    if code == EXIT_CAPABILITY_MISSING:
        return CAPABILITY_MISSING
    if code == EXIT_ENV_NOT_READY:
        return ENV_NOT_READY
    status = str(run.report.get("Status", ""))
    if code == EXIT_OK:
        if list(getattr(run.result, "downloads", None) or []):
            return DOWNLOADED
        if status == RunStatus.NO_RESULTS.value:
            return NOT_FOUND
        return NOT_DOWNLOADED
    if status == RunStatus.FULLTEXT_NOT_AUTHORIZED.value:
        return NOT_AUTHORIZED
    if status == RunStatus.DOWNLOAD_FAILED.value:
        return DOWNLOAD_FAILED
    return FAILED


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AcquireBatch:
    """Plan, then run, one queue under a single Research Chrome hold."""

    def __init__(
        self,
        queue: BatchQueue,
        *,
        batch_root: Path | None = None,
        confirm_budget: bool = False,
        daily_limit: int | None = None,
        item_runner: ItemRunner | None = None,
        navigator_factory: Callable[[], Any] | None = None,
        browser_running: Callable[[], bool] | None = None,
        ledger_factory: Callable[[], FulltextFetchLedger] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.queue = queue
        self.batch_root = require_output_path(
            Path(batch_root) if batch_root is not None else BATCH_RUNS_ROOT / queue.name,
            label="Acquire-batch root",
        )
        self.state = BatchState(self.batch_root / STATE_FILENAME)
        self.confirm_budget = confirm_budget
        self.daily_limit = daily_limit
        self._run_item = item_runner or live_item_runner(daily_limit=daily_limit)
        self._navigator_factory = navigator_factory or _default_navigator
        self._browser_running = browser_running or _research_chrome_running
        self._ledger_factory = ledger_factory or (
            lambda: FulltextFetchLedger(global_limit=daily_limit)
        )
        self._now = now or _utc_now

    # -- helpers -----------------------------------------------------------

    def _stamp(self) -> str:
        return self._now().astimezone(timezone.utc).isoformat()

    def _item_run_root(self, item: BatchItem) -> Path:
        stamp = self._now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = self.batch_root / "items" / f"{item.index:03d}_{item.source}_{stamp}"
        candidate = base
        suffix = 2
        while _windows_io_path(candidate).exists():
            candidate = base.with_name(f"{base.name}_{suffix}")
            suffix += 1
        return candidate

    def _entry(self, item: BatchItem, outcome: str, **fields: Any) -> dict[str, Any]:
        return {
            **item.as_dict(),
            "Key": item.key,
            "Outcome": outcome,
            "Terminal": outcome in TERMINAL_OUTCOMES,
            **fields,
        }

    def _library(self) -> tuple[Any, str]:
        try:
            navigator = self._navigator_factory()
        except Exception as exc:  # the check is advisory; the archive still reconciles
            return None, f"UNAVAILABLE: {type(exc).__name__}: {exc}"[:300]
        if navigator is None:
            return None, "NO_CATALOG"
        degraded = navigator.snapshot.degraded_dicts()
        return navigator, ("PARTIAL" if degraded else "COMPLETE")

    # -- the run -----------------------------------------------------------

    async def run(self, *, dry_run: bool = False) -> tuple[int, dict[str, Any], list[str]]:
        """Plan and (unless ``dry_run``) execute; returns exit code, report, notes."""

        notes: list[str] = []
        report: dict[str, Any] = {
            "BatchName": self.queue.name,
            "BatchRoot": str(self.batch_root),
            "DryRun": dry_run,
            "ItemsTotal": len(self.queue.items),
            "ParallelExecution": False,
            "AllowRefetch": False,
        }

        try:
            latest = self.state.latest_by_key()
        except BatchStateError as exc:
            report["Reason"] = str(exc)
            notes.append(
                f"Inspect {self.state.path} by hand; the batch will not guess which papers are done."
            )
            return self._finish("REFUSED_STATE_UNREADABLE", report, [], notes, write_files=False)

        navigator, library_check = self._library()
        report["LibraryCheck"] = library_check

        entries: dict[str, dict[str, Any]] = {}
        to_fetch: list[BatchItem] = []
        for item in self.queue.items:
            earlier = latest.get(item.key)
            if earlier is not None and earlier.get("Outcome") in TERMINAL_OUTCOMES:
                entries[item.key] = {**earlier, **item.as_dict(), "FromEarlierRun": True}
                continue
            match = held_match(navigator, item)
            if match is not None:
                entries[item.key] = self._entry(item, ALREADY_IN_LIBRARY, LibraryMatch=match)
                continue
            if earlier is not None and earlier.get("Outcome") == STARTED:
                notes.append(
                    f"{item.item_id} was interrupted mid-run earlier "
                    f"(started {earlier.get('StartedAt', 'unknown')}); it runs again."
                )
            entries[item.key] = self._entry(item, PENDING)
            to_fetch.append(item)

        report["PendingFetchCount"] = len(to_fetch)
        gate = len(to_fetch) > CONFIRMATION_THRESHOLD and not self.confirm_budget
        report["PlanningAndBudgetGate"] = gate
        report["ResearchChromeRunning"] = bool(self._browser_running()) if to_fetch else None

        remaining: int | None = None
        if to_fetch:
            try:
                usage = self._ledger_factory().usage_today()
                remaining = int(usage.get("RemainingGlobalBudget", 0))
                report["RemainingDailyFetchBudget"] = remaining
            except (FetchLedgerError, ValueError) as exc:
                report["Reason"] = f"fetch ledger unreadable: {exc}"
                notes.append("A ledger that cannot be read refuses every fetch; fix it before any batch.")
                return self._finish(
                    "REFUSED_FETCH_LEDGER_UNREADABLE", report, self._ordered(entries), notes, write_files=False
                )

        if gate:
            report["Reason"] = (
                f"{len(to_fetch)} papers need fetching, above the {CONFIRMATION_THRESHOLD} "
                "that may run without the user's confirmation (AGENTS.md Rule 41)"
            )
            notes.append(
                "Ask the user to confirm this many downloads; only then rerun with --confirm-budget."
            )
            return self._finish(
                "REFUSED_PLANNING_AND_BUDGET_GATE", report, self._ordered(entries), notes, write_files=False
            )

        if dry_run:
            if to_fetch and remaining is not None and remaining < len(to_fetch):
                notes.append(
                    f"Only {remaining} fetches remain in today's budget; the batch would stop there."
                )
            if to_fetch and not report["ResearchChromeRunning"]:
                notes.append(
                    "The dedicated Research Chrome is not running; start it with "
                    "`hunnu-harness browser-start` before the real run."
                )
            return self._finish("PLANNED", report, self._ordered(entries), notes, write_files=False)

        if not to_fetch:
            return self._finish("COMPLETED", report, self._ordered(entries), notes, write_files=True)

        if remaining is not None and remaining <= 0:
            report["Reason"] = "today's full-text fetch budget is already spent"
            notes.append(
                "The daily total is the user's knob (--daily-limit / HUNNU_HARNESS_DAILY_FETCH_LIMIT); "
                "it is theirs to raise, not the agent's."
            )
            return self._finish(
                "REFUSED_DAILY_BUDGET_EXHAUSTED", report, self._ordered(entries), notes, write_files=False
            )
        if remaining is not None and remaining < len(to_fetch):
            notes.append(
                f"Only {remaining} fetches remain in today's budget; the batch stops when the ledger refuses."
            )

        if not report["ResearchChromeRunning"]:
            report["Reason"] = "the dedicated Research Chrome is not running"
            notes.append(
                "Start it with `hunnu-harness browser-start` (sign in there if the institution asks), "
                "then run this batch again. A batch never launches a browser of its own."
            )
            return self._finish(
                "REFUSED_RESEARCH_CHROME_NOT_RUNNING", report, self._ordered(entries), notes, write_files=False
            )

        try:
            lock_path = research_chrome_lock.acquire(f"acquire-batch {self.queue.name}")
        except research_chrome_lock.ResearchChromeBusy as exc:
            report["Reason"] = str(exc)
            return self._finish(
                "REFUSED_RESEARCH_CHROME_BUSY", report, self._ordered(entries), notes, write_files=False
            )
        try:
            status = await self._execute(to_fetch, entries, report, notes)
        finally:
            research_chrome_lock.release(lock_path)
        return self._finish(status, report, self._ordered(entries), notes, write_files=True)

    async def _execute(
        self,
        to_fetch: list[BatchItem],
        entries: dict[str, dict[str, Any]],
        report: dict[str, Any],
        notes: list[str],
    ) -> str:
        _windows_io_path(self.batch_root).mkdir(parents=True, exist_ok=True)
        _windows_io_path(self.batch_root / QUEUE_SNAPSHOT_FILENAME).write_text(
            json.dumps(sanitize_value(self.queue.as_dict()), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        # Items the plan found in the library are final without a fetch; put
        # them on the record before any network work starts.
        for entry in entries.values():
            if entry["Outcome"] == ALREADY_IN_LIBRARY and not entry.get("FromEarlierRun"):
                self.state.append({**entry, "RecordedAt": self._stamp()})

        misses = 0
        for item in to_fetch:
            # The plan's library look is re-taken per item: a paper fetched
            # earlier in this batch under a different identifier is held now.
            navigator, _status = self._library()
            match = held_match(navigator, item)
            if match is not None:
                entry = self._entry(item, ALREADY_IN_LIBRARY, LibraryMatch=match)
                entries[item.key] = entry
                self.state.append({**entry, "RecordedAt": self._stamp()})
                continue

            run_root = self._item_run_root(item)
            started = self._stamp()
            # Write-ahead, like the fetch ledger: a crash mid-item leaves a
            # STARTED row, and the next run says so before running it again.
            self.state.append(
                {**self._entry(item, STARTED), "StartedAt": started, "RunRoot": str(run_root)}
            )
            try:
                run = await self._run_item(item, run_root)
            except Exception as exc:
                # The single-paper path grades every failure it knows about;
                # anything reaching here is a defect, so nothing further runs.
                reason = f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:300]
                entry = self._entry(
                    item,
                    FAILED,
                    Reason=reason,
                    RunRoot=str(run_root),
                    StartedAt=started,
                    FinishedAt=self._stamp(),
                )
                entries[item.key] = entry
                self.state.append(entry)
                report["StoppedAtItem"] = item.item_id
                report["Reason"] = reason
                return "STOPPED_UNEXPECTED_ERROR"
            outcome = classify_item_run(run)
            entry = self._entry(
                item,
                outcome,
                ExitCode=run.exit_code,
                Status=run.report.get("Status", UNKNOWN),
                Reason=_run_reason(run),
                RunRoot=str(run_root),
                StartedAt=started,
                FinishedAt=self._stamp(),
                Downloads=_download_summaries(run.result),
                ItemReport=run.report,
            )
            entries[item.key] = entry
            self.state.append(entry)

            stop = _STOP_FOR_OUTCOME.get(outcome)
            if stop is not None:
                self._report_stop(stop, item, entry, report, notes)
                return stop
            if outcome == DOWNLOADED:
                misses = 0
            elif outcome in MISS_OUTCOMES:
                misses += 1
                if misses >= CONSECUTIVE_MISS_LIMIT:
                    report["Reason"] = (
                        f"{CONSECUTIVE_MISS_LIMIT} items in a row reached the publisher and came back "
                        "without a file"
                    )
                    notes.append(
                        "Read the three item reasons before running this batch again: a wrong queue "
                        "(titles the source does not index) and a source that has started refusing "
                        "look alike from here, and the second must not be run into (Rules 71 and 74)."
                    )
                    return "STOPPED_CONSECUTIVE_MISSES"
        return "COMPLETED"

    def _report_stop(
        self,
        stop: str,
        item: BatchItem,
        entry: dict[str, Any],
        report: dict[str, Any],
        notes: list[str],
    ) -> None:
        item_report = entry.get("ItemReport") or {}
        report["StoppedAtItem"] = item.item_id
        report["Reason"] = entry.get("Reason") or item_report.get("Reason", UNKNOWN)
        for note in item_report.get("HumanNotes") or []:
            if note not in notes:
                notes.append(note)
        if stop == "STOPPED_FOR_HUMAN_ACTION":
            for flag in ("ACTION_REQUIRED_USER_LOGIN", "ACTION_REQUIRED_USER_DOWNLOAD"):
                if flag in item_report:
                    report[flag] = item_report[flag]
            report["Gate"] = {
                "ItemID": item.item_id,
                "Source": item.source,
                "Title": item.title or None,
                "DOI": item.doi or None,
                "Reason": report["Reason"],
                "FinalURL": item_report.get("FinalURL"),
                "FinalPageTitle": item_report.get("FinalPageTitle"),
            }
            notes.append(
                "HUMAN ACTION REQUIRED. Hand this gate to the user (Rule 72): keep the Research "
                "Chrome open (`hunnu-harness browser-start` if it is not running), name the page and "
                "the step, and wait. Start no other acquisition in the meantime. When they are done, "
                "run the same acquire-batch command again: finished items are skipped and "
                f"{item.item_id} runs first."
            )
        elif stop == "STOPPED_DAILY_BUDGET_EXHAUSTED":
            notes.append(
                "Today's fetch total is spent. It is the user's knob (--daily-limit / "
                "HUNNU_HARNESS_DAILY_FETCH_LIMIT); rerun the batch tomorrow or after they raise it."
            )
        elif stop == "STOPPED_ENV_NOT_READY":
            notes.append(
                "The environment stopped this item (browser, profile, or source unavailable). Fix "
                "that first; rerunning unchanged will not help, and an unreadable publisher page is "
                "not something to retry into."
            )

    @staticmethod
    def _ordered(entries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(entries.values(), key=lambda entry: int(entry.get("Index") or 0))

    def _finish(
        self,
        status: str,
        report: dict[str, Any],
        items: list[dict[str, Any]],
        notes: list[str],
        *,
        write_files: bool,
    ) -> tuple[int, dict[str, Any], list[str]]:
        """Seal the report; only a batch that ran leaves files behind."""

        counts: dict[str, int] = {}
        for entry in items:
            counts[entry["Outcome"]] = counts.get(entry["Outcome"], 0) + 1
        report["Status"] = status
        report["OutcomeCounts"] = dict(sorted(counts.items()))
        report["ItemsDownloaded"] = counts.get(DOWNLOADED, 0)
        report["ItemsRemaining"] = sum(
            1 for entry in items if entry["Outcome"] not in TERMINAL_OUTCOMES
        )
        report["Items"] = items
        exit_code = BATCH_EXIT_CODES[status]
        if report["ItemsRemaining"] and write_files and status != "COMPLETED":
            notes.append(
                "To continue, run the same acquire-batch command again; it skips every item with a "
                "final outcome."
            )
        payload = sanitize_value(report)
        if write_files:
            _windows_io_path(self.batch_root).mkdir(parents=True, exist_ok=True)
            _windows_io_path(self.batch_root / REPORT_FILENAME).write_text(
                json.dumps({**payload, "HumanNotes": notes}, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        return exit_code, payload, notes


def _research_chrome_running() -> bool:
    from ..browser.persistent_browser import probe

    return bool(probe().running)


def _emit(payload: Mapping[str, Any], notes: list[str]) -> None:
    import sys

    for note in notes:
        print(note, file=sys.stderr)
    print(
        json.dumps(
            {**sanitize_value(dict(payload)), "HumanNotes": notes},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def run_batch_cli(args: Any) -> int:
    """``hunnu-harness acquire-batch``: one JSON document on stdout, graded exit."""

    import asyncio

    try:
        queue = load_queue(args.queue)
    except BatchQueueError as exc:
        _emit({"Status": "REFUSED_INVALID_QUEUE", "Reason": str(exc), "Queue": str(args.queue)}, [])
        return BATCH_EXIT_CODES["REFUSED_INVALID_QUEUE"]
    try:
        batch = AcquireBatch(
            queue,
            batch_root=args.batch_root,
            confirm_budget=bool(args.confirm_budget),
            daily_limit=args.daily_limit,
        )
    except ValueError as exc:
        _emit({"Status": "REFUSED_INVALID_QUEUE", "Reason": str(exc), "Queue": str(args.queue)}, [])
        return BATCH_EXIT_CODES["REFUSED_INVALID_QUEUE"]
    exit_code, payload, notes = asyncio.run(batch.run(dry_run=bool(args.dry_run)))
    _emit(payload, notes)
    return exit_code


__all__ = [
    "AcquireBatch",
    "BATCH_RUNS_ROOT",
    "BatchItem",
    "BatchQueue",
    "BatchQueueError",
    "BatchState",
    "BatchStateError",
    "CONFIRMATION_THRESHOLD",
    "CONSECUTIVE_MISS_LIMIT",
    "ItemRun",
    "QUEUE_ITEM_CEILING",
    "classify_item_run",
    "held_match",
    "live_item_runner",
    "load_queue",
    "parse_queue",
    "run_batch_cli",
]
