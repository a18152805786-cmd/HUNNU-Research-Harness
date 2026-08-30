"""Post-acquisition topic classification for one WORK.

This is the layer an Agent and the acquisition pipeline both call.  It joins the
canonical topic store, the frozen taxonomy, the classifier, and the derived
``papers_by_topic`` view into one operation with a single decision:

* a WORK that already carries topics is left exactly as it is
  (``SKIPPED_EXISTING``) -- re-downloading a second PDF or a final version of a
  paper already in the Library must not rewrite the topics a human settled;
* a WORK with no topics is classified, and only assigned when the evidence
  clears the calibrated threshold;
* anything less confident returns ``REVIEW_REQUIRED`` with its proposals
  recorded but nothing written.

Classification never owns the paper.  A failure here leaves the download, the
identity lock, and the archived file untouched: it is a metadata outcome, not an
acquisition outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..paths import LIBRARY_CATALOG_JSONL
from .classification import (
    ClassificationInput,
    ClassificationMode,
    ClassificationResult,
    ClassificationStatus,
    WorkClassifier,
)
from .models import UNKNOWN
from .topics import (
    TopicLabel,
    TopicStore,
    TopicTaxonomy,
    TopicViewBuilder,
    WorkTopicRow,
    parse_topic_label,
)

FullTextReader = Callable[[str, WorkTopicRow], str]


@dataclass(frozen=True)
class NavigatorReadiness:
    """What the Navigator can actually answer about a work, capability by capability.

    Catalog and topic metadata are read straight from the canonical files, so
    they are current the moment classification commits.  Full-text passages come
    from the derived index, which is a separate artifact: it does not contain a
    newly archived work until it is rebuilt.  Collapsing all three into one
    ``NavigatorReady`` flag would say "ready" while passage retrieval silently
    misses the paper, so they are reported separately and the index keeps the
    Navigator's own authoritative status value.
    """

    metadata_ready: bool = False
    topic_ready: bool = False
    fulltext_index_status: str = "ABSENT"
    fulltext_index_covers_work: bool = False
    detail: str = UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "NavigatorMetadataReady": self.metadata_ready,
            "NavigatorTopicReady": self.topic_ready,
            "NavigatorFulltextIndexStatus": self.fulltext_index_status,
            "NavigatorFulltextIndexCoversWork": self.fulltext_index_covers_work,
            "NavigatorReadinessDetail": self.detail,
        }


@dataclass(frozen=True)
class ApplyOutcome:
    """What actually changed on disk for one WORK."""

    paper_id: str
    topics_written: tuple[str, ...] = ()
    topic_metadata_updated: bool = False
    topic_view_updated: bool = False
    links_created: tuple[str, ...] = ()
    links_removed: tuple[str, ...] = ()
    links_unavailable: tuple[str, ...] = ()
    pdf_copies_created: int = 0
    reason: str = UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "PaperID": self.paper_id,
            "TopicsWritten": list(self.topics_written),
            "TopicMetadataUpdated": self.topic_metadata_updated,
            "TopicViewUpdated": self.topic_view_updated,
            "LinksCreated": list(self.links_created),
            "LinksRemoved": list(self.links_removed),
            "LinksUnavailable": list(self.links_unavailable),
            "PDFCopiesCreated": self.pdf_copies_created,
            "Reason": self.reason,
        }


class PostAcquisitionClassifier:
    """Classify and file one WORK against the frozen taxonomy."""

    def __init__(
        self,
        *,
        store: TopicStore | None = None,
        taxonomy: TopicTaxonomy | None = None,
        classifier: WorkClassifier | None = None,
        view: TopicViewBuilder | None = None,
        catalog_path: Path | None = None,
        fulltext_reader: FullTextReader | None = None,
    ) -> None:
        self._taxonomy = taxonomy
        self.store = store or TopicStore(taxonomy=taxonomy)
        self.classifier = classifier or WorkClassifier(taxonomy=taxonomy)
        self.view = view or TopicViewBuilder()
        self.catalog_path = Path(catalog_path or LIBRARY_CATALOG_JSONL)
        self.fulltext_reader = fulltext_reader

    @property
    def taxonomy(self) -> TopicTaxonomy:
        if self._taxonomy is None:
            self._taxonomy = self.store.taxonomy
        return self._taxonomy

    # -- reading ----------------------------------------------------------

    def _row_for(self, paper_id: str, rows: Mapping[str, WorkTopicRow]) -> WorkTopicRow | None:
        row = rows.get(paper_id)
        if row is not None:
            return row
        catalog = self._catalog_entry(paper_id)
        if catalog is None:
            return None
        return WorkTopicRow(
            paper_id=paper_id,
            title=str(catalog.get("title", UNKNOWN)),
            human_readable_name=str(catalog.get("human_readable_name", "")),
            canonical_path=str(catalog.get("managed_pdf_path") or catalog.get("managed_fulltext_path") or ""),
            canonical_sha256=str(catalog.get("sha256", "")),
        )

    def _catalog_entry(self, paper_id: str) -> Mapping[str, Any] | None:
        if not self.catalog_path.exists():
            return None
        import json

        try:
            text = self.catalog_path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or paper_id not in stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(item, Mapping) and item.get("paper_id") == paper_id:
                return item
        return None

    def _build_input(self, row: WorkTopicRow) -> ClassificationInput:
        catalog = self._catalog_entry(row.paper_id) or {}
        fulltext = ""
        if self.fulltext_reader is not None:
            try:
                fulltext = self.fulltext_reader(row.paper_id, row) or ""
            except Exception:
                # Unreadable, scanned, or CAJ-only full text simply lowers the
                # evidence available; it is never a classification failure.
                fulltext = ""
        return ClassificationInput(
            paper_id=row.paper_id,
            title=_text(row.title),
            keywords=_text(row.keywords).replace(";", " "),
            journal=_text(catalog.get("journal", "")),
            fulltext=fulltext,
        )

    # -- classify ---------------------------------------------------------

    def classify_work(self, paper_id: str, *, force: bool = False) -> ClassificationResult:
        """Classify one WORK and write nothing.

        ``force`` re-runs classification for a WORK that already has topics.  It
        still does not write; applying the result remains a separate, explicit
        step.
        """

        rows = self.store.load()
        row = self._row_for(paper_id, rows)
        if row is None:
            return ClassificationResult(
                paper_id=paper_id,
                status=ClassificationStatus.FAILED_SAFE,
                mode=ClassificationMode.UNAVAILABLE,
                reason="WORK_NOT_FOUND_IN_LIBRARY",
            )
        existing = tuple(label.label for label in row.topics)
        if existing and not force:
            return ClassificationResult(
                paper_id=paper_id,
                status=ClassificationStatus.SKIPPED_EXISTING,
                existing_topics=existing,
                mode=ClassificationMode.UNAVAILABLE,
                reason="WORK_ALREADY_CARRIES_TOPICS",
            )
        return self.classifier.classify(self._build_input(row), existing_topics=existing)

    # -- apply ------------------------------------------------------------

    def apply_classification(
        self,
        paper_id: str,
        *,
        result: ClassificationResult | None = None,
        force: bool = False,
    ) -> tuple[ClassificationResult, ApplyOutcome]:
        """Write an assignable classification to the canonical store and view."""

        outcome = result or self.classify_work(paper_id, force=force)
        if not outcome.is_classified:
            return outcome, ApplyOutcome(
                paper_id=paper_id,
                reason=f"NOT_APPLIED_{outcome.status.value}",
            )

        rows = self.store.load()
        row = self._row_for(paper_id, rows)
        if row is None:
            return (
                replace(
                    outcome,
                    status=ClassificationStatus.FAILED_SAFE,
                    reason="WORK_NOT_FOUND_IN_LIBRARY",
                ),
                ApplyOutcome(paper_id=paper_id, reason="WORK_NOT_FOUND_IN_LIBRARY"),
            )

        labels = _labels_from(outcome.assigned_labels)
        self.taxonomy.require_known(labels)
        updated = row.with_topics(labels)
        if updated.topics == row.topics and paper_id in rows:
            # Already exactly these topics: nothing to write, and the view is
            # reconciled rather than rebuilt so a repeat call stays a no-op.
            view_outcome = self.view.sync_work(updated)
            return replace(outcome, applied=True), ApplyOutcome(
                paper_id=paper_id,
                topics_written=outcome.assigned_labels,
                topic_metadata_updated=False,
                topic_view_updated=bool(view_outcome["links_created"] or view_outcome["links_removed"]),
                links_created=view_outcome["links_created"],
                links_removed=view_outcome["links_removed"],
                links_unavailable=view_outcome["links_unavailable"],
                reason="ALREADY_CURRENT",
            )

        rows[paper_id] = updated
        self.store.commit(rows)
        view_outcome = self.view.sync_work(updated)
        self.view.write_links_manifest(rows)
        return replace(outcome, applied=True), ApplyOutcome(
            paper_id=paper_id,
            topics_written=outcome.assigned_labels,
            topic_metadata_updated=True,
            topic_view_updated=True,
            links_created=view_outcome["links_created"],
            links_removed=view_outcome["links_removed"],
            links_unavailable=view_outcome["links_unavailable"],
            reason="APPLIED",
        )

    # -- acquisition entry point -----------------------------------------

    def navigator_readiness(self, paper_id: str) -> NavigatorReadiness:
        """Report each Navigator capability for one work, without rebuilding anything.

        A work absent from the derived full-text index makes that index STALE for
        this purpose even when its manifest digests still match, because the
        index no longer covers the corpus.  Rebuilding is a separate, explicit
        operation and is never triggered here.
        """

        from ..navigator.catalog import CatalogReader
        from ..navigator.index import IndexStatus, NavigatorIndex

        metadata_ready = False
        topic_ready = False
        covers = False
        status = IndexStatus.ABSENT.value
        detail = UNKNOWN
        try:
            snapshot = CatalogReader().load()
            metadata_ready = any(work.paper_id == paper_id for work in snapshot.works)
            topic_ready = bool(self.store.topics_for(paper_id))
        except Exception as exc:
            return NavigatorReadiness(detail=f"CATALOG_UNREADABLE_{type(exc).__name__}")
        try:
            index = NavigatorIndex()
            resolved, index_detail = index.resolved_status(snapshot)
            fulltext, _status, _detail = index.load_fulltext(snapshot)
            covers = fulltext.covers(paper_id)
            if resolved is IndexStatus.FRESH and not covers:
                status = IndexStatus.STALE.value
                detail = "INDEX_DOES_NOT_COVER_WORK"
            else:
                status = resolved.value
                detail = str(index_detail.get("reason", UNKNOWN))
        except Exception as exc:
            status = IndexStatus.UNREADABLE.value
            detail = f"INDEX_UNREADABLE_{type(exc).__name__}"
        return NavigatorReadiness(
            metadata_ready=metadata_ready,
            topic_ready=topic_ready,
            fulltext_index_status=status,
            fulltext_index_covers_work=covers,
            detail=detail,
        )

    def classify_after_ingest(
        self,
        paper_id: str,
        *,
        disposition: str,
        dry_run: bool = False,
    ) -> tuple[ClassificationResult, ApplyOutcome]:
        """Run classification for one just-archived WORK.

        Only a genuinely new WORK, or an existing one carrying no topics at all,
        is classified.  Every other disposition -- an exact duplicate, another
        version of a WORK already held -- reuses what the WORK already has.
        """

        result = self.classify_work(paper_id)
        if result.status is ClassificationStatus.SKIPPED_EXISTING:
            return result, ApplyOutcome(
                paper_id=paper_id,
                topics_written=result.existing_topics,
                reason="REUSED_EXISTING_WORK_TOPICS",
            )
        if dry_run or not result.is_classified:
            return result, ApplyOutcome(
                paper_id=paper_id,
                reason=f"NOT_APPLIED_{result.status.value}" if not dry_run else "DRY_RUN",
            )
        return self.apply_classification(paper_id, result=result)


def _labels_from(values: Sequence[str]) -> tuple[TopicLabel, ...]:
    labels: list[TopicLabel] = []
    for value in values:
        label = parse_topic_label(value)
        if label is not None:
            labels.append(label)
    return tuple(labels)


def _text(value: Any) -> str:
    text = "" if value is None else str(value)
    return "" if text == UNKNOWN else text


__all__ = [
    "ApplyOutcome",
    "NavigatorReadiness",
    "FullTextReader",
    "PostAcquisitionClassifier",
]
