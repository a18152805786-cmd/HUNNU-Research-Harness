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

from ..paths import LIBRARY_CATALOG_JSONL, OUTPUT_ROOT, _logical_path, _windows_io_path
from .classification import (
    ClassificationInput,
    ClassificationMode,
    ClassificationResult,
    ClassificationStatus,
    WorkClassifier,
)
from .models import UNKNOWN
from .topic_confirmation import (
    TOPIC_PROVENANCE_PATH,
    AssignmentSource,
    ConfirmationResult,
    ConfirmationStatus,
    TopicProvenanceStore,
    human_confirmed_result,
)
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
        provenance: TopicProvenanceStore | None = None,
    ) -> None:
        self._taxonomy = taxonomy
        self.store = store or TopicStore(taxonomy=taxonomy)
        self.provenance = provenance or TopicProvenanceStore(
            path=_provenance_beside(self.store)
        )
        self.classifier = classifier or WorkClassifier(taxonomy=taxonomy)
        self.view = view or TopicViewBuilder()
        self.catalog_path = _logical_path(catalog_path or LIBRARY_CATALOG_JSONL)
        self.fulltext_reader = fulltext_reader

    @property
    def taxonomy(self) -> TopicTaxonomy:
        if self._taxonomy is None:
            self._taxonomy = self.store.taxonomy
        return self._taxonomy

    @property
    def output_root(self) -> Path:
        """The Output Root that owns this classifier's catalog.

        ``library/catalog/papers.jsonl`` sits three levels below it.  Derived
        from the catalog for the same reason provenance is derived from the
        store: redirecting the catalog to an isolated tree must redirect
        everything that hangs off it, or the isolation is only partial and a
        managed path resolves against the real corpus instead.
        """

        return _output_root_of(self.catalog_path)

    # -- reading ----------------------------------------------------------

    def _row_for(self, paper_id: str, rows: Mapping[str, WorkTopicRow]) -> WorkTopicRow | None:
        row = rows.get(paper_id)
        if row is not None:
            return row
        catalog = self._catalog_entry(paper_id)
        if catalog is None:
            return None
        managed = str(
            catalog.get("managed_pdf_path") or catalog.get("managed_fulltext_path") or ""
        )
        return WorkTopicRow(
            paper_id=paper_id,
            title=str(catalog.get("title", UNKNOWN)),
            human_readable_name=str(catalog.get("human_readable_name", ""))
            or _readable_name(catalog, paper_id, managed),
            canonical_path=_absolute_managed(managed, output_root=self.output_root),
            canonical_sha256=str(catalog.get("sha256", "")),
        )

    def _catalog_entry(self, paper_id: str) -> Mapping[str, Any] | None:
        catalog_io = _windows_io_path(self.catalog_path)
        if not catalog_io.exists():
            return None
        import json

        try:
            text = catalog_io.read_text(encoding="utf-8")
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

    # -- human confirmation ----------------------------------------------

    def confirm_topics(
        self,
        paper_id: str,
        topics: Sequence[str],
        *,
        allow_taxonomy_override: bool = False,
    ) -> ConfirmationResult:
        """Record the topics a person chose for a WORK that carries none.

        This decides nothing.  It checks that the work is genuinely unfiled,
        that every chosen topic was actually raised by classification for
        *this* work, and that the frozen taxonomy still recognises it -- then
        writes through the same path automatic classification uses, and
        records who settled it.

        Proposals are regenerated rather than read back: classification is a
        pure function of the work's own text, so re-running it yields the same
        candidates.  What it raised is the confirmable set, whatever status it
        attaches -- the proposals of a ``REVIEW_REQUIRED`` result, and equally
        the assigned and proposed topics of a ``CLASSIFIED`` result that was
        never applied.  The second case is real: a WORK archived through a
        path that ran no classification carries nothing, and the classifier's
        confidence about it is not a filing.  Gating on the regenerated status
        instead of on the WORK's own state refused exactly those works as "not
        awaiting review", which left them with no sanctioned way to be filed.

        Confirming a different set later fails closed; changing a settled
        assignment is a reclassification, not a confirmation.
        """

        selected = _labels_from(topics)
        rows = self.store.load()
        row = self._row_for(paper_id, rows)
        if row is None:
            return ConfirmationResult(
                paper_id=paper_id,
                status=ConfirmationStatus.WORK_NOT_FOUND,
                reason="No such WORK in the catalog or the topic store",
            )

        existing = tuple(label.label for label in row.topics)
        if existing:
            return self._already_settled(paper_id, existing, selected)

        outcome = self.classifier.classify(self._build_input(row))
        original_status = outcome.status.value
        raised = _unique_labels((*outcome.assigned_labels, *outcome.proposed_labels))
        if not raised:
            return ConfirmationResult(
                paper_id=paper_id,
                status=ConfirmationStatus.NO_CONFIRMABLE_PROPOSALS,
                original_classification_status=original_status,
                reason="Nothing was proposed for this WORK; it needs taxonomy review, not confirmation",
            )
        if not selected:
            return ConfirmationResult(
                paper_id=paper_id,
                status=ConfirmationStatus.EMPTY_SELECTION,
                proposed_topics=raised,
                original_classification_status=original_status,
                reason="No topic was selected",
            )

        try:
            self.taxonomy.require_known(selected)
        except Exception as exc:
            return ConfirmationResult(
                paper_id=paper_id,
                status=ConfirmationStatus.UNKNOWN_TOPIC,
                proposed_topics=raised,
                original_classification_status=original_status,
                reason=str(exc),
            )

        unproposed = [label.label for label in selected if label.label not in set(raised)]
        if unproposed and not allow_taxonomy_override:
            return ConfirmationResult(
                paper_id=paper_id,
                status=ConfirmationStatus.SELECTED_TOPIC_NOT_PROPOSED,
                proposed_topics=raised,
                original_classification_status=original_status,
                reason=(
                    "Not proposed for this WORK: "
                    + ", ".join(sorted(unproposed))
                    + ". Pass the override flag to confirm a taxonomy topic that was not proposed."
                ),
            )
        proposed = raised

        # Provenance is part of the confirmation, not a footnote to it, so prove
        # it can be written before the canonical write happens.  Failing the
        # other way round leaves the topics in place with no record of who
        # settled them, which is exactly the state this workflow exists to
        # prevent.
        try:
            self.provenance.ensure_writable()
        except OSError as exc:
            return ConfirmationResult(
                paper_id=paper_id,
                status=ConfirmationStatus.FAILED_SAFE,
                proposed_topics=proposed,
                original_classification_status=original_status,
                reason=f"Provenance is not writable, nothing was changed: {exc}",
            )

        confirmed = human_confirmed_result(
            paper_id,
            selected,
            proposed=proposed,
            original_status=original_status,
        )
        applied, apply_outcome = self.apply_classification(paper_id, result=confirmed)
        written = tuple(label.label for label in self.store.topics_for(paper_id))
        if not applied.applied or set(written) != {label.label for label in selected}:
            return ConfirmationResult(
                paper_id=paper_id,
                status=ConfirmationStatus.FAILED_SAFE,
                confirmed_topics=written,
                proposed_topics=proposed,
                original_classification_status=original_status,
                reason=f"Canonical apply did not complete cleanly: {apply_outcome.reason}",
            )

        self.provenance.record(
            paper_id=paper_id,
            topics=written,
            source=AssignmentSource.HUMAN_CONFIRMED,
            classification_status_before=original_status,
            proposed_topics=proposed,
            human_override=bool(unproposed),
        )
        return ConfirmationResult(
            paper_id=paper_id,
            status=ConfirmationStatus.CONFIRMED,
            confirmed_topics=written,
            proposed_topics=proposed,
            assignment_source=AssignmentSource.HUMAN_CONFIRMED.value,
            original_classification_status=original_status,
            topic_metadata_updated=apply_outcome.topic_metadata_updated,
            topic_view_updated=apply_outcome.topic_view_updated,
            pdf_copies_created=apply_outcome.pdf_copies_created,
            provenance_recorded=True,
            human_override=bool(unproposed),
            reason="APPLIED",
        )

    def _already_settled(
        self,
        paper_id: str,
        existing: tuple[str, ...],
        selected: Sequence[TopicLabel],
    ) -> ConfirmationResult:
        """A work that already carries topics is not awaiting confirmation."""

        record = self.provenance.latest_for(paper_id) or {}
        # No record means nobody wrote one, which is not the same as the harness
        # having decided.  The 179 works predating provenance carry none, and at
        # least one of them was in fact settled by a person after REVIEW_REQUIRED
        # -- reading absence as AUTO_CLASSIFIED would have quietly overwritten
        # that history with a claim no evidence supports.
        source = str(record.get("assignment_source", AssignmentSource.UNKNOWN_LEGACY.value))
        if source != AssignmentSource.HUMAN_CONFIRMED.value:
            return ConfirmationResult(
                paper_id=paper_id,
                status=ConfirmationStatus.NOT_REVIEW_REQUIRED,
                confirmed_topics=existing,
                assignment_source=source,
                reason="This WORK already carries topics and is not awaiting review",
            )
        if selected and set(existing) == {label.label for label in selected}:
            return ConfirmationResult(
                paper_id=paper_id,
                status=ConfirmationStatus.ALREADY_CONFIRMED,
                confirmed_topics=existing,
                proposed_topics=tuple(record.get("proposed_topics", ())),
                assignment_source=AssignmentSource.HUMAN_CONFIRMED.value,
                original_classification_status=str(
                    record.get("classification_status_before", UNKNOWN)
                ),
                provenance_recorded=True,
                reason="These exact topics were already confirmed for this WORK",
            )
        return ConfirmationResult(
            paper_id=paper_id,
            status=ConfirmationStatus.TOPIC_CONFIRMATION_CONFLICT,
            confirmed_topics=existing,
            assignment_source=AssignmentSource.HUMAN_CONFIRMED.value,
            reason=(
                "This WORK already carries confirmed topics; changing them is a "
                "reclassification, not a confirmation"
            ),
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
            # The catalog this classifier is bound to, not the default one:
            # an isolated library must report its own readiness, and the real
            # one resolves to the same files either way.
            snapshot = CatalogReader(
                catalog_path=self.catalog_path, topics_path=self.store.jsonl_path
            ).load()
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
        applied, outcome = self.apply_classification(paper_id, result=result)
        if applied.applied:
            self._record_auto_provenance(paper_id, outcome.topics_written, result)
        return applied, outcome

    def _record_auto_provenance(
        self,
        paper_id: str,
        topics: Sequence[str],
        result: ClassificationResult,
    ) -> None:
        """Note that the harness, not a person, chose these topics.

        Best-effort on purpose, and the opposite of the confirmation path: there
        the record *is* the operation, here the paper is already archived and
        classification is an addition to it.  Losing the note is worse than not
        having it, but neither is worth undoing an archive over.  A work with no
        record still reads as automatic, which is what it is.
        """

        if not topics:
            return
        try:
            self.provenance.record(
                paper_id=paper_id,
                topics=topics,
                source=AssignmentSource.AUTO_CLASSIFIED,
                classification_status_before=result.status.value,
                proposed_topics=result.proposed_labels,
            )
        except OSError:
            return


def _provenance_beside(store: TopicStore) -> Path:
    """The provenance file that belongs to this topic store's library.

    Derived rather than fixed, because a fixed path made an isolated catalog
    only half isolated: a caller who redirected the topic store still recorded
    into the real library unless it remembered to redirect one more thing, and
    forgetting wrote test entries into the production corpus.  Following the
    store means redirecting the catalog is enough.
    """

    catalog = Path(store.jsonl_path)
    return catalog.parent.parent / Path(TOPIC_PROVENANCE_PATH).name


def _output_root_of(catalog_path: Path) -> Path:
    """The Output Root a ``library/catalog/papers.jsonl`` path belongs to."""

    parents = Path(catalog_path).parents
    if len(parents) > 2:
        return _logical_path(parents[2])
    return _logical_path(OUTPUT_ROOT)


def _absolute_managed(value: str, *, output_root: Path) -> str:
    """Resolve a catalog managed path against the Output Root that owns it.

    The catalog stores managed paths Output-Root-relative (AGENTS.md 54).
    Treating one as absolute leaves a path that does not exist, and the view
    then silently records the entry as unavailable instead of hardlinking it --
    a work filed with no readable file behind it.
    """

    if not value:
        return ""
    candidate = Path(value)
    if candidate.is_absolute():
        return str(candidate)
    return str(_logical_path(output_root / candidate))


def _unique_labels(values: Sequence[str]) -> tuple[str, ...]:
    """Topic labels in first-seen order, each once."""

    return tuple(dict.fromkeys(value for value in values if value))


def _readable_name(catalog: Mapping[str, Any], paper_id: str, managed: str) -> str:
    """A filename for the view when the catalog carries no human-readable one."""

    suffix = Path(managed).suffix or ".pdf"
    first = str(catalog.get("first_author") or "").strip()
    year = str(catalog.get("year") or "").strip()
    title = str(catalog.get("title") or "").strip()
    if not title or title == UNKNOWN:
        return f"{paper_id}{suffix}"
    stem = " - ".join(part for part in (f"{first}({year})" if first and year else "", title) if part)
    cleaned = "".join(" " if character in '<>:"/\\|?*' else character for character in stem)
    return f"{cleaned.strip()[:120]} [{paper_id}]{suffix}"


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
