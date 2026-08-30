"""How each work's topics came to be assigned.

A topic a person chose and a topic a classifier inferred are not the same fact,
and the catalog cannot tell them apart: it records what a work is filed under,
never who decided.  This is the record that answers "how did this get here".

It is a sidecar rather than a column on the topic row.  The works that predate
it carry no provenance and must stay readable without a migration, and the
canonical schema is what the Navigator and the corpus fingerprint depend on --
nothing here is required in order to read a topic.

The file lives beside the catalog rather than inside it on purpose.  The corpus
fingerprint hashes every file in the catalog directory, so a note about how an
assignment was made would otherwise register as a change to the corpus itself.
Its own digest is kept separately, for the same reason in reverse: changing the
record should be visible, without pretending the papers changed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..paths import LIBRARY_ROOT

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
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
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
        # Short on purpose.  A pid plus a full uuid hex added around forty
        # characters and pushed the whole path past the legacy Windows limit,
        # so the write failed even though the directory was there.
        return self.path.with_name(f".{self.path.stem}.{uuid.uuid4().hex[:8]}.tmp")

    def ensure_writable(self) -> None:
        """Prove provenance can be written before anything canonical is.

        Recording that a human settled a topic is part of the confirmation, not
        an afterthought.  Discovering the record cannot be written *after* the
        canonical write leaves the assignment in place with no account of where
        it came from -- the very state this workflow exists to prevent.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        probe = self._temporary()
        try:
            probe.write_text("", encoding="utf-8", newline="\n")
        finally:
            probe.unlink(missing_ok=True)

    def append(self, entry: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(entry), ensure_ascii=False, sort_keys=True) + "\n"
        temporary = self._temporary()
        existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        try:
            temporary.write_text(existing + line, encoding="utf-8", newline="\n")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def scan(self) -> tuple[list[dict[str, Any]], int]:
        """Every readable record, plus how many lines were not readable.

        ``load`` drops unreadable lines silently so a corrupt entry can never
        block a confirmation.  That is the right trade for the write path and
        the wrong one for an audit, so the count is surfaced here instead of
        changing what ``load`` does.
        """

        if not self.path.exists():
            return [], 0
        entries: list[dict[str, Any]] = []
        malformed = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
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
            present=self.path.exists(),
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


__all__ = [
    "PROVENANCE_FINGERPRINT_SCHEMA_VERSION",
    "TOPIC_PROVENANCE_PATH",
    "AssignmentSource",
    "TopicProvenanceFingerprint",
    "TopicProvenanceStore",
]
