"""Write-ahead ledger of full-text fetches against real publishers.

The three existing checks each answer a different question and none of them
answers this one:

* the Navigator answers "is this work already in the Library" *before* search;
* ``LibraryDisposition`` answers "was this file archived before" *at* archive
  time;
* nothing answered "have we already asked this publisher for these bytes
  today".

That gap is what let one PDF be fetched 13 times in a single day: every fetch
failed *after* the bytes arrived, the paper never reached the Library, so
every in-library check truthfully said NOT_IN_LIBRARY and waved the next
attempt through.  The missing fact is the attempt itself, so the ledger
records it **before the fetch action is issued** -- a ledger written on
success would have recorded none of those 13 and waved through the 14th.

The ledger is runtime state, not corpus: it lives in ``Output Root/audit/``
and is deliberately outside every corpus-fingerprint input (the fingerprint
reads ``library/catalog``, ``library/papers`` and ``papers_by_topic`` only).
Fetching a paper twice today must not make the Library look changed.

Records are append-only JSONL, two kinds:

* ``attempt`` -- written before the fetch action; this alone supports the
  guard's decision;
* ``outcome`` -- appended after the action ends, with success/failure and the
  reason, so the *next* refusal can say what happened last time.

No personal identity ever enters a record: no username, no account, no device
name.  A record carries the source, the publisher-side stable identifier, the
Harness paper id, timestamps, and outcome text only.

Days are UTC calendar days, matching every other Harness audit timestamp.

Failure posture is closed in every direction that gates a fetch: an
unreadable or corrupt ledger, a record that cannot be appended, and a missing
identifier all refuse the fetch.  Only the after-the-fact ``outcome`` append
is best-effort, because failing it would misreport a download that already
happened.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..paths import AUDIT_DIR, _windows_io_path, require_output_path
from .models import UNKNOWN
from .normalization import normalize_doi

FETCH_LEDGER_PATH = AUDIT_DIR / "fulltext_fetch_ledger.jsonl"

# Two attempts per identifier per day: the second is a genuine transient
# retry, a third is debugging against a live publisher.  The global ceiling
# is a soft gate sized for a normal day (15 by default) and can be adjusted
# with the environment knob; AGENTS.md 62's single-batch limit of 25 in
# batching.py is a separate constraint.
PER_IDENTIFIER_DAILY_LIMIT = 2
GLOBAL_DAILY_LIMIT = 15
DAILY_FETCH_LIMIT_ENV = "HUNNU_HARNESS_DAILY_FETCH_LIMIT"
MIN_FETCH_INTERVAL_SECONDS = 15.0
MIN_FETCH_INTERVAL_ENV = "HUNNU_HARNESS_FETCH_MIN_INTERVAL_SECONDS"
BURST_WINDOW_SECONDS = 600.0
BURST_WINDOW_ENV = "HUNNU_HARNESS_FETCH_BURST_WINDOW_SECONDS"
BURST_WINDOW_LIMIT = 12
BURST_LIMIT_ENV = "HUNNU_HARNESS_FETCH_BURST_LIMIT"


def _resolve_global_limit(explicit: int | None) -> int:
    """Resolve the daily total from an explicit value, env, or the default."""

    if explicit is not None:
        return explicit
    raw = os.environ.get(DAILY_FETCH_LIMIT_ENV)
    if raw is None:
        return GLOBAL_DAILY_LIMIT
    try:
        resolved = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{DAILY_FETCH_LIMIT_ENV} must be an integer >= 1; got {raw!r}. "
            "Unset it or set it to a positive integer."
        ) from exc
    if resolved < 1:
        raise ValueError(
            f"{DAILY_FETCH_LIMIT_ENV} must be an integer >= 1; got {raw!r}. "
            "Unset it or set it to a positive integer."
        )
    return resolved


def _resolve_float_setting(
    explicit: float | None,
    *,
    env_name: str,
    default: float,
    minimum: float,
    inclusive: bool,
) -> float:
    """Resolve one finite float from an explicit value, env, or default."""

    raw: Any
    if explicit is not None:
        raw = explicit
    else:
        raw = os.environ.get(env_name)
        if raw is None:
            return default
    try:
        resolved = float(raw)
    except (TypeError, ValueError) as exc:
        comparator = ">=" if inclusive else ">"
        raise ValueError(
            f"{env_name} must be a finite float {comparator} {minimum}; got {raw!r}."
        ) from exc
    valid = math.isfinite(resolved) and (
        resolved >= minimum if inclusive else resolved > minimum
    )
    if not valid:
        comparator = ">=" if inclusive else ">"
        raise ValueError(
            f"{env_name} must be a finite float {comparator} {minimum}; got {raw!r}."
        )
    return resolved


def _resolve_burst_limit(explicit: int | None) -> int:
    """Resolve the burst ceiling from an explicit value, env, or default."""

    raw: Any
    if explicit is not None:
        raw = explicit
    else:
        raw = os.environ.get(BURST_LIMIT_ENV)
        if raw is None:
            return BURST_WINDOW_LIMIT
    try:
        # Unlike the environment, an explicit setting is type-annotated as an
        # int; reject lossy float coercions instead of silently truncating.
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError
        resolved = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{BURST_LIMIT_ENV} must be an integer >= 1; got {raw!r}."
        ) from exc
    if resolved < 1:
        raise ValueError(
            f"{BURST_LIMIT_ENV} must be an integer >= 1; got {raw!r}."
        )
    return resolved

# "attempted", never "fetched": the refused third try usually follows two
# FAILED attempts, and a name claiming success would misdescribe exactly the
# situation the ledger exists to catch.
STATUS_ATTEMPT_LIMIT_REACHED = "FULLTEXT_FETCH_ATTEMPT_LIMIT_REACHED"
STATUS_BUDGET_EXHAUSTED = "DAILY_FETCH_BUDGET_EXHAUSTED"
STATUS_IDENTIFIER_MISSING = "FETCH_IDENTIFIER_MISSING"

_RECORD_TYPES = ("attempt", "outcome")


class FetchLedgerError(RuntimeError):
    """The ledger cannot be trusted or written; the fetch must not proceed."""


class FetchIdentifierMissing(FetchLedgerError):
    """No stable identifier could be derived; the fetch must not proceed."""

    def __init__(self, message: str) -> None:
        super().__init__(f"{STATUS_IDENTIFIER_MISSING}: {message}")
        self.status = STATUS_IDENTIFIER_MISSING


class FetchBudgetExceeded(FetchLedgerError):
    """The guard refused the fetch; ``status`` says which limit was hit."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status


def ledger_identifier(record: Any, *, prefer: str | None = None) -> str:
    """The publisher-side stable identifier for one fetch, or fail closed.

    Preference order: the adapter's own identifier (ScienceDirect's PII, a
    CNKI stable identifier), then the record's ``stable_identifier``, then the
    normalized DOI.  A fetch with no identifier at all cannot be budgeted, so
    it is refused rather than waved through -- an unbudgeted fetch is exactly
    the hole this ledger closes.
    """

    for candidate in (
        prefer,
        str(getattr(record, "stable_identifier", "") or ""),
        normalize_doi(str(getattr(record, "doi", "") or "")),
    ):
        value = (candidate or "").strip()
        if value and value != UNKNOWN:
            return value
    raise FetchIdentifierMissing(
        "no stable identifier, no DOI; refusing an unbudgetable publisher fetch"
    )


@dataclass(frozen=True)
class FetchTicket:
    """Proof that one attempt is on the ledger; carries the outcome back."""

    ledger: "FulltextFetchLedger"
    source: str
    identifier: str
    paper_id: str

    def record_outcome(self, *, ok: bool, detail: str) -> bool:
        """Append the outcome; best-effort by design.

        The fetch has already happened by the time this runs.  Refusing to
        report a completed download because the outcome row could not be
        written would misstate what occurred, so a failed append returns
        ``False`` instead of raising.
        """

        try:
            self.ledger.append_record(
                record_type="outcome",
                source=self.source,
                identifier=self.identifier,
                paper_id=self.paper_id,
                ok=ok,
                detail=detail,
            )
        except (OSError, FetchLedgerError):
            return False
        return True


class FulltextFetchLedger:
    """The append-only daily budget for real publisher full-text fetches."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        per_identifier_limit: int = PER_IDENTIFIER_DAILY_LIMIT,
        global_limit: int | None = None,
        min_interval_seconds: float | None = None,
        burst_window_seconds: float | None = None,
        burst_limit: int | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.path = require_output_path(
            Path(path or FETCH_LEDGER_PATH), label="Full-text fetch ledger"
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.per_identifier_limit = per_identifier_limit
        self.global_limit = _resolve_global_limit(global_limit)
        self.min_interval_seconds = _resolve_float_setting(
            min_interval_seconds,
            env_name=MIN_FETCH_INTERVAL_ENV,
            default=MIN_FETCH_INTERVAL_SECONDS,
            minimum=0.0,
            inclusive=True,
        )
        self.burst_window_seconds = _resolve_float_setting(
            burst_window_seconds,
            env_name=BURST_WINDOW_ENV,
            default=BURST_WINDOW_SECONDS,
            minimum=0.0,
            inclusive=False,
        )
        self.burst_limit = _resolve_burst_limit(burst_limit)
        self._sleeper = sleeper if sleeper is not None else time.sleep

    # -- reading -----------------------------------------------------------

    def _today(self) -> str:
        return self._now().astimezone(timezone.utc).date().isoformat()

    def read_records(self) -> list[dict[str, Any]]:
        """Every record in the ledger, or raise on the first line that lies.

        A malformed line means something other than this module wrote the
        ledger, and a guard that skips what it cannot read is a guard that can
        be talked past.  Detection is loud on purpose.
        """

        path_io = _windows_io_path(self.path)
        if not path_io.exists():
            return []
        try:
            text = path_io.read_text(encoding="utf-8")
        except OSError as exc:
            raise FetchLedgerError(f"fetch ledger is unreadable: {exc}") from exc
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise FetchLedgerError(
                    f"fetch ledger line {line_number} is not JSON: {exc}"
                ) from exc
            if (
                not isinstance(item, Mapping)
                or item.get("record_type") not in _RECORD_TYPES
                or not str(item.get("source", "")).strip()
                or not str(item.get("identifier", "")).strip()
                or not str(item.get("date", "")).strip()
            ):
                raise FetchLedgerError(
                    f"fetch ledger line {line_number} is not a ledger record"
                )
            records.append(dict(item))
        return records

    def _attempts_today(self) -> list[dict[str, Any]]:
        today = self._today()
        return [
            item
            for item in self.read_records()
            if item["record_type"] == "attempt" and item["date"] == today
        ]

    def _last_outcome_today(self, source: str, identifier: str) -> dict[str, Any] | None:
        today = self._today()
        last = None
        for item in self.read_records():
            if (
                item["record_type"] == "outcome"
                and item["date"] == today
                and item["source"] == source
                and item["identifier"] == identifier
            ):
                last = item
        return last

    def _pace(self, attempts: list[dict[str, Any]]) -> None:
        """Wait for the minimum interval and sliding burst window, if needed."""

        now = self._now().astimezone(timezone.utc)
        timestamps: list[datetime] = []
        for item in attempts:
            raw_timestamp = str(item.get("timestamp", "")).strip()
            try:
                timestamp = datetime.fromisoformat(raw_timestamp)
            except (TypeError, ValueError) as exc:
                raise FetchLedgerError(
                    f"fetch ledger attempt has an invalid ISO timestamp: {raw_timestamp!r}"
                ) from exc
            if timestamp.tzinfo is None:
                raise FetchLedgerError(
                    f"fetch ledger attempt timestamp has no timezone: {raw_timestamp!r}"
                )
            timestamps.append(timestamp.astimezone(timezone.utc))
        timestamps.sort()

        wait1 = 0.0
        if self.min_interval_seconds > 0 and timestamps:
            wait1 = self.min_interval_seconds - (now - timestamps[-1]).total_seconds()

        wait2 = 0.0
        window_timestamps = [
            timestamp
            for timestamp in timestamps
            if (now - timestamp).total_seconds() < self.burst_window_seconds
        ]
        if len(window_timestamps) >= self.burst_limit:
            # ``[-burst_limit]`` is the oldest timestamp among the most recent
            # burst_limit attempts; once it leaves the window, only
            # burst_limit - 1 of those recent attempts remain.
            threshold = window_timestamps[-self.burst_limit]
            wait2 = self.burst_window_seconds - (now - threshold).total_seconds()

        wait = max(0.0, wait1, wait2)
        if wait > 0:
            self._sleeper(wait)

    # -- writing -----------------------------------------------------------

    def append_record(
        self,
        *,
        record_type: str,
        source: str,
        identifier: str,
        paper_id: str,
        **extra: Any,
    ) -> None:
        if record_type not in _RECORD_TYPES:
            raise FetchLedgerError(f"unknown ledger record type {record_type!r}")
        moment = self._now().astimezone(timezone.utc)
        record = {
            "record_type": record_type,
            "date": moment.date().isoformat(),
            "timestamp": moment.isoformat(),
            "source": source,
            "identifier": identifier,
            "paper_id": paper_id,
            **extra,
        }
        _windows_io_path(self.path.parent).mkdir(parents=True, exist_ok=True)
        with _windows_io_path(self.path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()

    # -- the guard ---------------------------------------------------------

    def authorize_fetch(
        self,
        *,
        source: str,
        identifier: str,
        paper_id: str,
        allow_refetch: bool = False,
    ) -> FetchTicket:
        """Check the daily budget, then write the attempt, then permit.

        The attempt row is on disk before this returns; the caller may only
        issue the fetch action against the returned ticket.  A refusal writes
        nothing -- a blocked fetch consumed no budget.
        """

        identifier = (identifier or "").strip()
        if not identifier or identifier == UNKNOWN:
            raise FetchIdentifierMissing(
                "no stable identifier, no DOI; refusing an unbudgetable publisher fetch"
            )

        attempts = self._attempts_today()
        same = [
            item
            for item in attempts
            if item["source"] == source and item["identifier"] == identifier
        ]
        if len(same) >= self.per_identifier_limit and not allow_refetch:
            outcome = self._last_outcome_today(source, identifier)
            last_seen = same[-1].get("timestamp", UNKNOWN)
            last_result = (
                f"ok={outcome.get('ok')} detail={outcome.get('detail', UNKNOWN)}"
                if outcome
                else "no outcome was recorded (the fetch action may not have finished)"
            )
            raise FetchBudgetExceeded(
                STATUS_ATTEMPT_LIMIT_REACHED,
                f"{source} already fetched {identifier} {len(same)} times today; "
                f"last attempt {last_seen}, last outcome: {last_result}. "
                "Diagnose offline from the first fetched file; pass "
                "--allow-refetch to explicitly fetch it again.",
            )
        # ``allow_refetch`` deliberately has no effect here: it exempts only
        # the per-identifier repeat check above.  Exempting the daily total as
        # well would turn the one explicit "fetch this paper again" switch
        # into a general budget bypass.
        if len(attempts) >= self.global_limit:
            raise FetchBudgetExceeded(
                STATUS_BUDGET_EXHAUSTED,
                f"{len(attempts)} publisher fetch attempts already recorded today "
                f"(daily ceiling {self.global_limit}).",
            )
        # Reject first, so a request that is already over quota is not made to
        # wait.  Pace next, then put the attempt on the ledger before the
        # caller is permitted to issue the fetch action.
        # Pacing intentionally considers only today's UTC attempts; crossing
        # midnight clears both controls.  The injected sleeper is synchronous,
        # so the default time.sleep blocks an async CLI event loop; this Harness
        # has one serial workflow, making that trade-off acceptable.
        self._pace(attempts)
        self.append_record(
            record_type="attempt",
            source=source,
            identifier=identifier,
            paper_id=paper_id,
            allow_refetch=bool(allow_refetch),
        )
        return FetchTicket(
            ledger=self, source=source, identifier=identifier, paper_id=paper_id
        )

    # -- reporting ---------------------------------------------------------

    def usage_today(self) -> dict[str, Any]:
        """What the budget looks like right now, for people and Agents."""

        attempts = self._attempts_today()
        per_identifier: dict[tuple[str, str], dict[str, Any]] = {}
        for item in attempts:
            key = (str(item["source"]), str(item["identifier"]))
            entry = per_identifier.setdefault(
                key,
                {
                    "source": key[0],
                    "identifier": key[1],
                    "paper_id": str(item.get("paper_id", UNKNOWN)),
                    "attempts": 0,
                    "last_attempt": UNKNOWN,
                    "last_outcome": UNKNOWN,
                },
            )
            entry["attempts"] += 1
            entry["last_attempt"] = str(item.get("timestamp", UNKNOWN))
        for key, entry in per_identifier.items():
            outcome = self._last_outcome_today(*key)
            if outcome is not None:
                entry["last_outcome"] = (
                    f"ok={outcome.get('ok')} detail={outcome.get('detail', UNKNOWN)}"
                )
        return {
            "LedgerPath": str(self.path),
            "LedgerDateUTC": self._today(),
            "PerIdentifierDailyLimit": self.per_identifier_limit,
            "GlobalDailyLimit": self.global_limit,
            "MinIntervalSeconds": self.min_interval_seconds,
            "BurstWindowSeconds": self.burst_window_seconds,
            "BurstLimit": self.burst_limit,
            "AttemptsToday": len(attempts),
            "RemainingGlobalBudget": max(self.global_limit - len(attempts), 0),
            "Identifiers": sorted(
                per_identifier.values(),
                key=lambda entry: (entry["source"], entry["identifier"]),
            ),
        }


__all__ = [
    "BURST_LIMIT_ENV",
    "BURST_WINDOW_ENV",
    "BURST_WINDOW_LIMIT",
    "BURST_WINDOW_SECONDS",
    "DAILY_FETCH_LIMIT_ENV",
    "FETCH_LEDGER_PATH",
    "GLOBAL_DAILY_LIMIT",
    "MIN_FETCH_INTERVAL_ENV",
    "MIN_FETCH_INTERVAL_SECONDS",
    "PER_IDENTIFIER_DAILY_LIMIT",
    "STATUS_ATTEMPT_LIMIT_REACHED",
    "STATUS_BUDGET_EXHAUSTED",
    "STATUS_IDENTIFIER_MISSING",
    "FetchBudgetExceeded",
    "FetchIdentifierMissing",
    "FetchLedgerError",
    "FetchTicket",
    "FulltextFetchLedger",
    "ledger_identifier",
]
