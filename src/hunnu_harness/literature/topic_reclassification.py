"""Explicit, optimistic, audited reclassification of an already filed WORK.

Preview is read-only. Apply uses the existing canonical writer, checks the
frozen taxonomy and managed identity, and rolls back metadata and hardlinks on
an ordinary write failure. It never edits a PDF or rebuilds a retrieval index.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..paths import _transaction_token, _windows_io_path, is_within, require_output_path
from .auto_classification import PostAcquisitionClassifier
from .classification import CLASSIFIER_VERSION, ClassificationResult
from .topic_confirmation import AssignmentSource
from .topics import TopicStoreError, TopicTaxonomy, parse_topic_label


@dataclass(frozen=True)
class ReclassificationResult:
    paper_id: str
    status: str
    old_topics: tuple[str, ...] = ()
    new_topics: tuple[str, ...] = ()
    current_fingerprint: str = ""
    previous_assignment_source: str = AssignmentSource.UNKNOWN_LEGACY.value
    classifier_context_sha256: str = ""
    classification_input_sha256: str = ""
    dry_run: bool = True
    applied: bool = False
    reason: str = ""
    classification: dict[str, Any] = field(default_factory=dict)
    navigator: dict[str, Any] = field(default_factory=dict)
    rollback_completed: bool = False
    recovery_path: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status in {"DRY_RUN", "RECLASSIFIED", "ALREADY_CURRENT"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "PaperID": self.paper_id, "ReclassificationStatus": self.status,
            "OldTopics": list(self.old_topics), "NewTopics": list(self.new_topics),
            "CurrentFingerprint": self.current_fingerprint,
            "PreviousAssignmentSource": self.previous_assignment_source,
            "ClassifierContextSHA256": self.classifier_context_sha256,
            "ClassificationInputSHA256": self.classification_input_sha256,
            "DryRun": self.dry_run, "Applied": self.applied,
            "Reason": self.reason, "ClassifierVersion": CLASSIFIER_VERSION,
            "Classification": self.classification, "RollbackCompleted": self.rollback_completed,
            "RecoveryPath": self.recovery_path, "PDFCopiesCreated": 0,
            "TopicMetadataUpdated": self.applied, "TopicViewUpdated": self.applied,
            "ProvenanceRecorded": self.applied,
            **self.navigator,
        }


class _Refusal(RuntimeError):
    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status


def _bytes(path: Path) -> bytes | None:
    io = _windows_io_path(path)
    return io.read_bytes() if io.exists() else None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_digest(value: Any) -> str:
    return _sha(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _classifier_context(service: PostAcquisitionClassifier) -> dict[str, Any]:
    from ..navigator.lexicon import CONCEPTS
    from . import classification as rules
    from .taxonomy_aliases import folded_aliases_for

    classifier = service.classifier
    return {
        "version": CLASSIFIER_VERSION,
        "implementation": f"{type(classifier).__module__}.{type(classifier).__qualname__}",
        "options": {name: getattr(classifier, name) for name in (
            "auto_assign_threshold", "secondary_margin_ratio", "review_threshold",
            "max_topics", "require_taxonomy_name_for_secondary")},
        "scoring": {name: getattr(rules, name) for name in (
            "FIELD_WEIGHTS", "MAX_FULLTEXT_CONCEPT_HITS", "MIN_NAME_TERM_CHARS",
            "NAME_TERM_UNIT_WEIGHT", "MAX_NAME_TERM_WEIGHT", "CONFIDENCE_SCALE",
            "ALIAS_WORD_CJK_EQUIVALENT")},
        "lexicon_sha256": _json_digest([asdict(concept) for concept in CONCEPTS]),
        "aliases_sha256": _json_digest({label.subtopic: folded_aliases_for(label.subtopic)
                                       for label in service.taxonomy.labels()}),
    }


def _consistent_assignment(entry: dict[str, Any] | None, paper_id: str, topics: tuple[str, ...]) -> bool:
    """Validate the formal producer schema before trusting an ownership marker."""
    required = {"paper_id", "topics", "assignment_source", "confirmation_source",
                "classification_status_before", "proposed_topics", "human_override", "confirmed_at"}
    if not isinstance(entry, dict) or not required <= entry.keys():
        return False
    source = entry["assignment_source"]
    if not isinstance(source, str) or source not in {AssignmentSource.AUTO_CLASSIFIED.value, AssignmentSource.HUMAN_CONFIRMED.value}:
        return False
    expected_marker = "HUMAN" if source == AssignmentSource.HUMAN_CONFIRMED.value else "HARNESS"
    if entry["confirmation_source"] != expected_marker or entry["paper_id"] != paper_id:
        return False
    if not isinstance(entry["topics"], list) or tuple(entry["topics"]) != topics:
        return False
    if not isinstance(entry["classification_status_before"], str) or not entry["classification_status_before"].strip():
        return False
    if not isinstance(entry["proposed_topics"], list) or any(not isinstance(v, str) or not v for v in entry["proposed_topics"]):
        return False
    if type(entry["human_override"]) is not bool or not isinstance(entry["confirmed_at"], str):
        return False
    try:
        if datetime.fromisoformat(entry["confirmed_at"]).tzinfo is None:
            return False
    except ValueError:
        return False
    change_kind = entry.get("change_kind")
    if change_kind is not None:
        extension = {"previous_topics", "previous_assignment_source", "previous_provenance", "reason",
                     "expected_current_fingerprint", "classifier_version", "classifier_context_sha256",
                     "classification_input_sha256", "classification_status_after", "human_authorization_fingerprint"}
        if source != AssignmentSource.AUTO_CLASSIFIED.value or change_kind != "TOPIC_RECLASSIFIED" or not extension <= entry.keys():
            return False
        if not isinstance(entry["previous_topics"], list) or not entry["previous_topics"] or any(not isinstance(v, str) or not v for v in entry["previous_topics"]):
            return False
        previous = entry["previous_provenance"]
        if not isinstance(previous, dict) or previous.get("assignment_source") != entry["previous_assignment_source"] or not _consistent_assignment(previous, paper_id, tuple(entry["previous_topics"])):
            return False
        for key in ("expected_current_fingerprint", "classifier_context_sha256", "classification_input_sha256"):
            if not isinstance(entry[key], str) or not re.fullmatch(r"[a-f0-9]{64}", entry[key]):
                return False
        if any(not isinstance(entry[key], str) or not entry[key].strip() for key in ("reason", "classifier_version", "classification_status_after")):
            return False
        replaces_human = entry["previous_assignment_source"] == AssignmentSource.HUMAN_CONFIRMED.value
        if entry["human_override"] != replaces_human:
            return False
        if entry["human_authorization_fingerprint"] != (entry["expected_current_fingerprint"] if replaces_human else None):
            return False
    elif source == AssignmentSource.AUTO_CLASSIFIED.value and entry["human_override"]:
        # The only legal auto assignment with this flag is a recorded,
        # specifically authorized replacement of a previous human decision.
        return False
    return True


def _restore(path: Path, data: bytes | None) -> None:
    io = _windows_io_path(path)
    if data is None:
        io.unlink(missing_ok=True)
        return
    temporary = path.with_name(f".{path.name}.{_transaction_token()}.rollback")
    try:
        _windows_io_path(temporary).write_bytes(data)
        _windows_io_path(temporary).replace(io)
    finally:
        _windows_io_path(temporary).unlink(missing_ok=True)


class TopicReclassifier:
    def __init__(self, service: PostAcquisitionClassifier | None = None) -> None:
        self.service = service or PostAcquisitionClassifier()

    def recover(
        self, recovery_path: Path, *, reason: str, expected_preimage_sha256: str,
        expected_current_fingerprint: str | None = None, dry_run: bool = True,
    ):
        """Restore only a retained transaction's view after metadata rolled back.

        This deliberately refuses changed metadata instead of overwriting a
        later assignment. Preview and apply both bind the original preimage.
        """
        from .topic_recovery import TopicRecoveryResult, recover_topic_view

        try:
            self._validate_binding()
        except (_Refusal, OSError, ValueError, RuntimeError) as exc:
            return TopicRecoveryResult(status="RECOVERY_REFUSED", recovery_path=str(recovery_path), reason=str(exc), dry_run=dry_run)
        return recover_topic_view(
            self.service, recovery_path, reason=reason,
            expected_preimage_sha256=expected_preimage_sha256,
            expected_current_fingerprint=expected_current_fingerprint, dry_run=dry_run,
        )

    def reclassify(
        self, paper_id: str, *, reason: str,
        expected_current_topics: Sequence[str] | None = None,
        expected_fingerprint: str | None = None,
        dry_run: bool = True,
        authorize_human_confirmed_fingerprint: str | None = None,
    ) -> ReclassificationResult:
        """Recompute automatically; arbitrary replacement topics are not accepted.

        Apply requires at least one precondition. If both are supplied both must
        match. A human-confirmed assignment needs additional authorization tied
        to this exact preview fingerprint. Missing/contradictory provenance is
        never assumed to be automatic and cannot be changed here.
        """
        base: dict[str, Any] = {"paper_id": paper_id, "dry_run": dry_run}
        try:
            if not isinstance(reason, str) or not reason.strip():
                raise _Refusal("REASON_REQUIRED", "An explicit non-empty reason is required")
            if not dry_run and expected_current_topics is None and expected_fingerprint is None:
                raise _Refusal("PRECONDITION_REQUIRED", "Apply requires expected current topics or a fingerprint")
            self._validate_binding()
            if dry_run:
                return self._run(paper_id, reason, expected_current_topics, expected_fingerprint,
                                 True, authorize_human_confirmed_fingerprint, base)
            with self.service.store.mutation_lock():
                return self._run(paper_id, reason, expected_current_topics, expected_fingerprint,
                                 False, authorize_human_confirmed_fingerprint, base)
        except _Refusal as exc:
            return ReclassificationResult(**base, status=exc.status, reason=str(exc))
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            return ReclassificationResult(**base, status="FAILED_SAFE", reason=f"{type(exc).__name__}: {exc}")

    def _validate_binding(self) -> None:
        service = self.service
        root = require_output_path(service.output_root, label="Reclassification Output Root")
        # A custom service must redirect the complete standard layout. A partly
        # redirected store must never mutate the real view or provenance.
        expected = (
            (service.catalog_path, root / "library/catalog/papers.jsonl"),
            (service.store.jsonl_path, root / "library/catalog/paper_topics.jsonl"),
            (service.store.csv_path, root / "library/catalog/paper_topics.csv"),
            (service.provenance.path, root / "library/topic_assignment_provenance.jsonl"),
            (service.view.view_root, root / "papers_by_topic"),
            (service.view.papers_dir, root / "library/papers"),
        )
        for actual, wanted in expected:
            require_output_path(wanted)
            if require_output_path(actual) != wanted:
                raise _Refusal("LAYOUT_MISMATCH", "Service paths must share one standard Output layout")

    def _run(self, paper_id, reason, expected_topics, expected_fingerprint, dry_run, human_authorization, base):
        service = self.service
        root = service.output_root
        taxonomy_path = require_output_path(root / "topic_taxonomy.json")
        taxonomy_bytes = _bytes(taxonomy_path)
        if taxonomy_bytes is None:
            raise _Refusal("TAXONOMY_UNAVAILABLE", "The frozen taxonomy is missing")
        fresh_taxonomy = TopicTaxonomy.load(taxonomy_path)
        if fresh_taxonomy.domains != service.taxonomy.domains or fresh_taxonomy.domains != service.classifier.taxonomy.domains:
            raise _Refusal("TAXONOMY_CHANGED", "The service taxonomy differs from the current frozen configuration")
        rows = service.store.load()
        row = service._row_for(paper_id, rows)
        catalog = service._catalog_entry(paper_id)
        if row is None or catalog is None:
            raise _Refusal("WORK_NOT_FOUND", "The WORK must exist in the canonical catalog")
        old = tuple(label.label for label in row.topics)
        base.update(old_topics=old)
        if not old:
            raise _Refusal("WORK_UNFILED", "Use acquisition classification or confirm-topics for an unfiled WORK")
        fresh_taxonomy.require_known(row.topics)
        canonical = require_output_path(Path(row.canonical_path), label="Managed full text")
        if not is_within(canonical, root / "library/papers") or not canonical.is_file():
            raise _Refusal("MANAGED_IDENTITY_UNVERIFIED", "A local managed canonical file is required")
        managed_hash = _sha(canonical.read_bytes())
        if managed_hash != row.canonical_sha256 or managed_hash != catalog.get("sha256"):
            raise _Refusal("MANAGED_IDENTITY_UNVERIFIED", "Canonical PDF hashes disagree with the catalog/topic row")
        managed_value = str(catalog.get("managed_pdf_path") or catalog.get("managed_fulltext_path") or "")
        managed_path = Path(managed_value)
        if not managed_path.is_absolute():
            managed_path = root / managed_path
        if not managed_value or require_output_path(managed_path) != canonical:
            raise _Refusal("MANAGED_IDENTITY_UNVERIFIED", "The catalog and topic row name different canonical files")
        name = row.human_readable_name or f"{paper_id}.pdf"
        if name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((".", " ")):
            raise _Refusal("VIEW_CONFLICT", "The WORK view filename is not a safe path component")
        latest = service.provenance.latest_for(paper_id)
        source = str((latest or {}).get("assignment_source", AssignmentSource.UNKNOWN_LEGACY.value))
        base.update(previous_assignment_source=source)
        # Build text exactly once: a supplied local reader can change, and the
        # evidence classified must be the same evidence the preview binds.
        classification_input = service._build_input(row)
        classifier_context = _classifier_context(service)
        base.update(classification_input_sha256=_json_digest(asdict(classification_input)),
                    classifier_context_sha256=_json_digest(classifier_context))
        fingerprint_data = {"schema": "topic-reclassification-0.2", "paper_id": paper_id,
                            "topics": old, "provenance": latest, "catalog": catalog,
                            "topic_row": row.as_mapping(), "classification_input": asdict(classification_input),
                            "classifier_context": classifier_context,
                            "managed_sha256": managed_hash, "taxonomy_sha256": _sha(taxonomy_bytes)}
        fingerprint = _json_digest(fingerprint_data)
        base.update(current_fingerprint=fingerprint)
        if expected_topics is not None:
            if isinstance(expected_topics, (str, bytes)) or tuple(expected_topics) != old:
                raise _Refusal("PRECONDITION_CONFLICT", "Current ordered topics differ from the expected topics")
        if expected_fingerprint is not None and expected_fingerprint != fingerprint:
            raise _Refusal("PRECONDITION_CONFLICT", "The current work/topics/provenance fingerprint has changed")
        classification = service.classifier.classify(classification_input, existing_topics=old)
        new = classification.assigned_labels
        base.update(new_topics=new, classification=classification.as_dict())
        if not classification.is_classified or not new:
            raise _Refusal("REVIEW_REQUIRED", "The classifier did not produce an assignable replacement; existing topics kept")
        fresh_taxonomy.require_known(tuple(parse_topic_label(value) for value in new))
        if dry_run:
            return ReclassificationResult(**base, status="DRY_RUN", reason=reason.strip(), navigator=self._readiness(paper_id))
        _entries, malformed = service.provenance.scan()
        if malformed:
            raise _Refusal("PROVENANCE_UNREADABLE", "The provenance sidecar contains unreadable records; manual audit is required")
        if not _consistent_assignment(latest, paper_id, old):
            raise _Refusal("UNKNOWN_OR_INCONSISTENT_PROVENANCE", "No consistent recorded assignment source; manual audit is required")
        if source == AssignmentSource.HUMAN_CONFIRMED.value and human_authorization != fingerprint:
            raise _Refusal("HUMAN_AUTHORIZATION_REQUIRED", "Changing HUMAN_CONFIRMED requires authorization for this exact current fingerprint")
        if old == new:
            return ReclassificationResult(**base, status="ALREADY_CURRENT", reason=reason.strip(), navigator=self._readiness(paper_id))
        return self._apply(base, row, classification, latest, reason.strip(), taxonomy_path, taxonomy_bytes, canonical, managed_hash)

    def _readiness(self, paper_id: str) -> dict[str, Any]:
        from ..navigator.catalog import CatalogReader
        from ..navigator.index import NavigatorIndex

        service = self.service
        try:
            snapshot = CatalogReader(catalog_path=service.catalog_path, topics_path=service.store.jsonl_path).load()
            status, _detail = NavigatorIndex(root=service.output_root / "paper_retrieval").resolved_status(snapshot)
            return {"NavigatorMetadataReady": any(w.paper_id == paper_id for w in snapshot.works),
                    "NavigatorTopicReady": bool(service.store.topics_for(paper_id)),
                    "NavigatorFulltextIndexStatus": status.value}
        except Exception as exc:
            return {"NavigatorMetadataReady": False, "NavigatorTopicReady": bool(service.store.topics_for(paper_id)),
                    "NavigatorFulltextIndexStatus": "UNREADABLE", "NavigatorReadinessDetail": type(exc).__name__}

    def _apply(self, base, row, classification: ClassificationResult, previous, reason, taxonomy_path, taxonomy_bytes, canonical, managed_hash):
        service = self.service
        paths = (service.store.jsonl_path, service.store.csv_path, service.view.links_path, service.provenance.path)
        before = {path: _bytes(path) for path in paths}
        name = row.human_readable_name or f"{row.paper_id}.pdf"
        old_links = set(service.view._existing_links_for(row.paper_id, name))
        new_row = row.with_topics(tuple(parse_topic_label(value) for value in classification.assigned_labels))
        try:
            wanted = {service.view._safe_view_path(service.view.view_root / label.domain / label.subtopic / name) for label in new_row.topics}
        except TopicStoreError as exc:
            raise _Refusal("VIEW_CONFLICT", str(exc)) from exc
        touched = old_links | wanted
        for path in touched:
            require_output_path(path, label="Topic hardlink")
            if not is_within(path, service.view.view_root) or path.is_symlink():
                raise _Refusal("VIEW_CONFLICT", "The topic view must contain hardlinks inside its own root")
            if path.exists() and not os.path.samefile(path, canonical):
                raise _Refusal("VIEW_CONFLICT", "A topic-view name is occupied by a different file; nothing changed")
        existing_dirs = {parent for path in touched for parent in (path.parent, path.parent.parent) if parent.exists()}
        # A retained preimage package is available if the filesystem also fails
        # during rollback. No PDF is copied into this recovery package.
        recovery = require_output_path(service.output_root / "temp/topic-reclassification" / _transaction_token())
        recovery.mkdir(parents=True, exist_ok=False)
        journal = {"paper_id": row.paper_id, "old_links": [str(p) for p in old_links],
                   "canonical_path": str(canonical), "files": []}
        for number, (path, data) in enumerate(before.items()):
            backup = f"{number}.before" if data is not None else None
            if backup:
                (recovery / backup).write_bytes(data)
            journal["files"].append({"path": str(path), "backup": backup})
        (recovery / "PREIMAGE.json").write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            applied, outcome = service.apply_classification(row.paper_id, result=classification, force=True)
            if not applied.applied or outcome.links_unavailable:
                raise OSError("The canonical apply or hardlink creation did not complete")
            if tuple(label.label for label in service.store.topics_for(row.paper_id)) != classification.assigned_labels:
                raise OSError("Canonical topics do not match the replacement")
            for path in wanted:
                if not path.exists() or path.is_symlink() or not os.path.samefile(path, canonical):
                    raise OSError("Replacement topic hardlink is missing or wrong")
            if any(path.exists() for path in old_links - wanted):
                raise OSError("An obsolete topic hardlink could not be removed")
            entry = {
                "paper_id": row.paper_id, "topics": list(classification.assigned_labels),
                "assignment_source": AssignmentSource.AUTO_CLASSIFIED.value,
                "confirmation_source": "HARNESS", "change_kind": "TOPIC_RECLASSIFIED",
                "previous_topics": list(base["old_topics"]),
                "previous_assignment_source": base["previous_assignment_source"],
                "previous_provenance": previous, "reason": reason,
                "expected_current_fingerprint": base["current_fingerprint"],
                "classifier_version": CLASSIFIER_VERSION,
                "classifier_context_sha256": base["classifier_context_sha256"],
                "classification_input_sha256": base["classification_input_sha256"],
                "classification_status_before": "HUMAN_CONFIRMED" if base["previous_assignment_source"] == AssignmentSource.HUMAN_CONFIRMED.value else previous.get("classification_status_after", previous["classification_status_before"]),
                "classification_status_after": classification.status.value,
                "proposed_topics": list(classification.proposed_labels),
                "human_override": base["previous_assignment_source"] == AssignmentSource.HUMAN_CONFIRMED.value,
                "human_authorization_fingerprint": base["current_fingerprint"] if base["previous_assignment_source"] == AssignmentSource.HUMAN_CONFIRMED.value else None,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
            service.provenance.append(entry)
            if _bytes(taxonomy_path) != taxonomy_bytes or _sha(canonical.read_bytes()) != managed_hash:
                raise OSError("Frozen taxonomy or canonical full text changed during the operation")
            result = ReclassificationResult(**base, status="RECLASSIFIED", applied=True, reason=reason, navigator=self._readiness(row.paper_id))
        except Exception as exc:
            errors = []
            for path, data in before.items():
                try:
                    _restore(path, data)
                except Exception as rollback_exc:
                    errors.append(f"{path.name}: {rollback_exc}")
            for path in touched:
                try:
                    if path in old_links:
                        if not path.exists():
                            path.parent.mkdir(parents=True, exist_ok=True)
                            os.link(_windows_io_path(canonical), _windows_io_path(path))
                    elif path.exists():
                        from ..windows_hardlinks import remove_readonly_hardlink_alias

                        remove_readonly_hardlink_alias(path, canonical)
                except Exception as rollback_exc:
                    errors.append(f"{path.name}: {rollback_exc}")
            for parent in sorted({p for path in touched for p in (path.parent, path.parent.parent)} - existing_dirs, key=lambda p: len(p.parts), reverse=True):
                try:
                    parent.rmdir()
                except OSError:
                    pass
            if errors:
                return ReclassificationResult(**base, status="RECOVERY_REQUIRED", reason=f"{exc}; rollback: {'; '.join(errors)}", recovery_path=str(recovery))
            result = ReclassificationResult(**base, status="FAILED_SAFE", reason=f"{type(exc).__name__}: {exc}", rollback_completed=True)
        try:
            # This package contains only this operation's metadata backups.
            shutil.rmtree(_windows_io_path(recovery))
        except OSError:
            # Cleanup cannot turn a committed assignment into an apparent
            # failed apply (or obscure a completed rollback).
            pass
        return result


__all__ = ["TopicReclassifier", "ReclassificationResult"]
