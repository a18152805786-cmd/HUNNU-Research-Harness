"""Accepting a human's decision after classification returned REVIEW_REQUIRED.

Classification stops at REVIEW_REQUIRED on purpose: roughly two papers in five
land there, and a wrong automatic assignment is silent where a missing one is
visible.  Stopping safely is right.  Having no way to continue afterwards is
not -- an Agent could be told which topics were proposed and had no operation
for reporting back which one a person chose.

This is that operation.  It does not decide anything: the person decides, and
this records their decision through the same canonical write path automatic
classification uses, so metadata, the hardlink view and the links manifest stay
in step.  What it adds is provenance, because a topic a human settled and a
topic a classifier inferred should not look identical in the catalog.

Confirmation is deliberately narrow.  Only topics this work's own classification
proposed may be confirmed, so a lower-tier Agent cannot invent a label; the
frozen taxonomy is re-checked regardless; and confirming a second, different set
fails closed rather than quietly editing what was already settled.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..paths import LIBRARY_ROOT, _transaction_token, _windows_io_path
from .classification import (
    ClassificationEvidence,
    ClassificationResult,
    ClassificationStatus,
)
from .models import UNKNOWN
from .topics import TopicLabel, parse_topic_label

# Deliberately outside library/catalog: the library fingerprint hashes every
# file in that directory, so a provenance record written there would register as
# a change to the corpus itself.  Provenance is about how an assignment was
# made, not about what the corpus contains.
TOPIC_PROVENANCE_PATH = LIBRARY_ROOT / "topic_assignment_provenance.jsonl"

# Independent of the corpus fingerprint schema on purpose: the two answer
# different questions and must be able to move separately.
PROVENANCE_FINGERPRINT_SCHEMA_VERSION = "topic-provenance-fingerprint-0.1"


class AssignmentSource(str, Enum):
    """Who settled a topic.

    ``UNKNOWN_LEGACY`` is the answer for every assignment made before this file
    existed, and it is deliberately not ``AUTO_CLASSIFIED``.  Absence of a record
    says only that nobody recorded one; the corpus contains at least one work
    whose topic a person chose after REVIEW_REQUIRED and wrote through the
    low-level store, and treating silence as an automatic decision would state
    the opposite of what happened.
    """

    AUTO_CLASSIFIED = "AUTO_CLASSIFIED"
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    UNKNOWN_LEGACY = "UNKNOWN_LEGACY"


class ConfirmationStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    ALREADY_CONFIRMED = "ALREADY_CONFIRMED"
    NOT_REVIEW_REQUIRED = "NOT_REVIEW_REQUIRED"
    NO_CONFIRMABLE_PROPOSALS = "NO_CONFIRMABLE_PROPOSALS"
    EMPTY_SELECTION = "EMPTY_SELECTION"
    SELECTED_TOPIC_NOT_PROPOSED = "SELECTED_TOPIC_NOT_PROPOSED"
    UNKNOWN_TOPIC = "UNKNOWN_TOPIC"
    TOPIC_CONFIRMATION_CONFLICT = "TOPIC_CONFIRMATION_CONFLICT"
    WORK_NOT_FOUND = "WORK_NOT_FOUND"
    FAILED_SAFE = "FAILED_SAFE"


@dataclass(frozen=True)
class ConfirmationResult:
    paper_id: str
    status: ConfirmationStatus
    confirmed_topics: tuple[str, ...] = ()
    proposed_topics: tuple[str, ...] = ()
    assignment_source: str = UNKNOWN
    original_classification_status: str = UNKNOWN
    topic_metadata_updated: bool = False
    topic_view_updated: bool = False
    pdf_copies_created: int = 0
    provenance_recorded: bool = False
    human_override: bool = False
    reason: str = UNKNOWN

    @property
    def succeeded(self) -> bool:
        return self.status in (
            ConfirmationStatus.CONFIRMED,
            ConfirmationStatus.ALREADY_CONFIRMED,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "PaperID": self.paper_id,
            "ConfirmationStatus": self.status.value,
            "ConfirmedTopics": list(self.confirmed_topics),
            "ProposedTopics": list(self.proposed_topics),
            "AssignmentSource": self.assignment_source,
            "OriginalClassificationStatus": self.original_classification_status,
            "TopicMetadataUpdated": self.topic_metadata_updated,
            "TopicViewUpdated": self.topic_view_updated,
            "PDFCopiesCreated": self.pdf_copies_created,
            "ProvenanceRecorded": self.provenance_recorded,
            "HumanOverride": self.human_override,
            "Reason": self.reason,
        }


@dataclass(frozen=True)
class TopicProvenanceFingerprint:
    """A comparable digest of the provenance sidecar, and nothing else."""

    present: bool
    record_count: int
    paper_count: int
    malformed_lines: int
    sources: tuple[tuple[str, int], ...]
    sha256: str

    @property
    def intact(self) -> bool:
        return self.malformed_lines == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "TopicProvenancePresent": self.present,
            "TopicProvenanceRecordCount": self.record_count,
            "TopicProvenancePaperCount": self.paper_count,
            "TopicProvenanceMalformedLines": self.malformed_lines,
            "TopicProvenanceIntact": self.intact,
            "TopicProvenanceSources": dict(self.sources),
            "TopicProvenanceFingerprint": self.sha256,
        }


@dataclass
class TopicProvenanceStore:
    """Append-only record of how each work's topics were settled.

    A sidecar rather than a column on the topic row: the 179 works predating
    this carry no provenance and must not need a migration to stay readable,
    and the canonical schema is what the Navigator and the fingerprint depend
    on.  Nothing here is required to read a topic; it only answers "how did
    this get here".
    """

    path: Path = field(default_factory=lambda: Path(TOPIC_PROVENANCE_PATH))

    def load(self) -> list[dict[str, Any]]:
        path_io = _windows_io_path(self.path)
        if not path_io.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in path_io.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                entries.append(item)
        return entries

    def latest_for(self, paper_id: str) -> dict[str, Any] | None:
        latest = None
        for entry in self.load():
            if entry.get("paper_id") == paper_id:
                latest = entry
        return latest

    def _temporary(self) -> Path:
        return self.path.with_name(f".{self.path.stem}.{_transaction_token()}.tmp")

    def ensure_writable(self) -> None:
        """Prove provenance can be written before anything canonical is.

        Recording that a human settled a topic is part of the confirmation, not
        an afterthought.  Discovering the record cannot be written *after* the
        canonical write leaves the assignment in place with no account of where
        it came from -- the very state this workflow exists to prevent.
        """

        _windows_io_path(self.path.parent).mkdir(parents=True, exist_ok=True)
        probe = self._temporary()
        probe_io = _windows_io_path(probe)
        try:
            probe_io.write_text("", encoding="utf-8", newline="\n")
        finally:
            probe_io.unlink(missing_ok=True)

    def append(self, entry: Mapping[str, Any]) -> None:
        path_io = _windows_io_path(self.path)
        _windows_io_path(self.path.parent).mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(entry), ensure_ascii=False, sort_keys=True) + "\n"
        temporary = self._temporary()
        temporary_io = _windows_io_path(temporary)
        existing = path_io.read_text(encoding="utf-8") if path_io.exists() else ""
        try:
            temporary_io.write_text(existing + line, encoding="utf-8", newline="\n")
            temporary_io.replace(path_io)
        finally:
            temporary_io.unlink(missing_ok=True)

    def scan(self) -> tuple[list[dict[str, Any]], int]:
        """Every readable record, plus how many lines were not readable.

        ``load`` drops unreadable lines silently so a corrupt entry can never
        block a confirmation.  That is the right trade for the write path and
        the wrong one for an audit, so the count is surfaced here instead of
        changing what ``load`` does.
        """

        path_io = _windows_io_path(self.path)
        if not path_io.exists():
            return [], 0
        entries: list[dict[str, Any]] = []
        malformed = 0
        for line in path_io.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(item, dict):
                entries.append(item)
            else:
                malformed += 1
        return entries, malformed

    def fingerprint(self) -> TopicProvenanceFingerprint:
        """A digest of the provenance record, independent of the corpus digest.

        Kept separate on purpose.  The corpus fingerprint answers "is this the
        same set of papers and topic assignments"; backfilling a note about how
        an assignment was once made changes no paper, no PDF and no topic, so it
        must not read as a corpus change.  This answers the other question --
        "has the audit record itself changed" -- and nothing else consumes it.

        Records are canonicalised and sorted before hashing, so the digest
        depends on the set of entries rather than the order they happen to sit
        in; each entry already carries its own timestamp, so ordering carries no
        information the content does not.  Nothing machine- or user-specific
        enters the digest.
        """

        records, malformed = self.scan()
        canonical = sorted(
            json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records
        )
        payload = {
            "schema_version": PROVENANCE_FINGERPRINT_SCHEMA_VERSION,
            "records": canonical,
            "malformed_lines": malformed,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        counts: dict[str, int] = {}
        for record in records:
            key = str(record.get("assignment_source", AssignmentSource.UNKNOWN_LEGACY.value))
            counts[key] = counts.get(key, 0) + 1
        return TopicProvenanceFingerprint(
            present=_windows_io_path(self.path).exists(),
            record_count=len(records),
            paper_count=len({str(record.get("paper_id", "")) for record in records if record.get("paper_id")}),
            malformed_lines=malformed,
            sources=tuple(sorted(counts.items())),
            sha256=hashlib.sha256(blob).hexdigest(),
        )

    def record(
        self,
        *,
        paper_id: str,
        topics: Sequence[str],
        source: AssignmentSource,
        classification_status_before: str,
        proposed_topics: Sequence[str] = (),
        human_override: bool = False,
    ) -> dict[str, Any]:
        entry = {
            "paper_id": paper_id,
            "topics": list(topics),
            "assignment_source": source.value,
            # No personal identity is recorded: which human acted is not a fact
            # the catalog needs, only that a human did.
            "confirmation_source": "HUMAN" if source is AssignmentSource.HUMAN_CONFIRMED else "HARNESS",
            "classification_status_before": classification_status_before,
            "proposed_topics": list(proposed_topics),
            "human_override": human_override,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.append(entry)
        return entry


def _labels(values: Iterable[str]) -> tuple[TopicLabel, ...]:
    out: list[TopicLabel] = []
    for value in values:
        label = parse_topic_label(value)
        if label is not None and label not in out:
            out.append(label)
    return tuple(out)


def human_confirmed_result(
    paper_id: str,
    topics: Sequence[TopicLabel],
    *,
    proposed: Sequence[str],
    original_status: str,
) -> ClassificationResult:
    """A ClassificationResult standing for a decision a person already made.

    Built so the confirmation path can hand it to the same apply step automatic
    classification uses, rather than growing a second writer.  Confidence is 1.0
    because a human settled it; the evidence names the person, not a score.
    """

    evidence = tuple(
        ClassificationEvidence(
            topic=label.label,
            confidence=1.0,
            score=0.0,
            concepts=("human_confirmation",),
            fields=("human",),
            terms=(),
            signals=("HUMAN",),
        )
        for label in topics
    )
    return ClassificationResult(
        paper_id=paper_id,
        status=ClassificationStatus.HUMAN_CONFIRMED,
        assigned_topics=evidence,
        proposed_topics=(),
        overall_confidence=1.0,
        metadata_used=("human",),
        review_required=False,
        reason=f"HUMAN_CONFIRMED_FROM_{original_status}",
    )


__all__ = [
    "PROVENANCE_FINGERPRINT_SCHEMA_VERSION",
    "TOPIC_PROVENANCE_PATH",
    "AssignmentSource",
    "ConfirmationResult",
    "ConfirmationStatus",
    "TopicProvenanceFingerprint",
    "TopicProvenanceStore",
    "human_confirmed_result",
]
