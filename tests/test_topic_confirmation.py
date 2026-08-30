"""Confirming the topics a person chose after REVIEW_REQUIRED.

Classification stopping at REVIEW_REQUIRED is correct and stays correct.  What
these pin is the step after it: an Agent could be told which topics were
proposed and had no operation for reporting back which one a human picked, so
the only way through was to call the topic store directly.

Every test runs against an isolated taxonomy, topic store, view and provenance
file under TEMP_DIR.  The real library is never written here.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.auto_classification import PostAcquisitionClassifier
from hunnu_harness.literature.classification import ClassificationStatus, WorkClassifier
from hunnu_harness.literature.topic_confirmation import (
    TOPIC_PROVENANCE_PATH,
    AssignmentSource,
    ConfirmationStatus,
    TopicProvenanceStore,
)
from hunnu_harness.literature.topics import (
    TopicStore,
    TopicTaxonomy,
    TopicViewBuilder,
    WorkTopicRow,
    parse_topics_field,
)
from hunnu_harness.paths import TEMP_DIR

TAXONOMY = {
    "domains": {
        "01_人工智能与数字经济": ["AI漂洗", "人工智能与机器人"],
        "04_会计审计与信息披露": ["信息披露"],
        "09_ESG与绿色发展": ["绿色金融与绿色创新"],
    },
    "generated_at": "2026-08-30T00:00:00+00:00",
}

AI_WASHING = "01_人工智能与数字经济" + chr(92) + "AI漂洗"
DISCLOSURE = "04_会计审计与信息披露" + chr(92) + "信息披露"
GREEN = "09_ESG与绿色发展" + chr(92) + "绿色金融与绿色创新"


class _Fixture:
    """An isolated library: taxonomy, topic store, view and provenance."""

    def __init__(self, root: Path) -> None:
        self.root = root
        taxonomy_path = root / "topic_taxonomy.json"
        taxonomy_path.write_text(json.dumps(TAXONOMY, ensure_ascii=False), encoding="utf-8")
        self.taxonomy = TopicTaxonomy.load(taxonomy_path)
        self.papers = root / "library" / "papers"
        self.papers.mkdir(parents=True, exist_ok=True)
        self.store = TopicStore(
            jsonl_path=root / "library" / "catalog" / "paper_topics.jsonl",
            csv_path=root / "library" / "catalog" / "paper_topics.csv",
            taxonomy=self.taxonomy,
        )
        self.view = TopicViewBuilder(view_root=root / "papers_by_topic", papers_dir=self.papers)
        self.provenance = TopicProvenanceStore(path=root / "topic_assignment_provenance.jsonl")

    def add_work(self, paper_id: str, title: str, *, keywords: str = "", topics: str = "") -> None:
        managed = self.papers / f"{paper_id}.pdf"
        managed.write_bytes(b"%PDF-1.4\n%%EOF\n")
        rows = self.store.load()
        rows[paper_id] = WorkTopicRow(
            paper_id=paper_id,
            title=title,
            keywords=keywords,
            topics=parse_topics_field(topics),
            human_readable_name=f"{paper_id}.pdf",
            canonical_path=str(managed),
            canonical_sha256="a" * 64,
        )
        self.store.commit(rows)

    def service(self) -> PostAcquisitionClassifier:
        return PostAcquisitionClassifier(
            store=self.store,
            taxonomy=self.taxonomy,
            classifier=WorkClassifier(taxonomy=self.taxonomy),
            view=self.view,
            catalog_path=self.root / "missing-catalog.jsonl",
            provenance=self.provenance,
        )


def _review_work(fixture: _Fixture, paper_id: str = "PR1") -> tuple[str, ...]:
    """An English-titled work with one lexicon-only signal, so it lands in review.

    The surface form is deliberately not an English subtopic alias -- an
    aliased phrase like "AI washing" now corroborates the taxonomy-name signal
    and classifies outright, which is the wrong starting state for a
    confirmation test.
    """

    fixture.add_work(
        paper_id, "The talk-walk gap and information asymmetry in technology narratives"
    )
    result = fixture.service().classify_work(paper_id)
    assert result.status is ClassificationStatus.REVIEW_REQUIRED, result.status
    return result.proposed_labels


class ReviewDeadEndTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_review_required_still_writes_nothing_by_itself(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-none-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            proposed = _review_work(fixture)
            self.assertTrue(proposed)
            self.assertEqual(fixture.store.topics_for("PR1"), ())

    def test_confirming_a_proposed_topic_succeeds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-ok-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            proposed = _review_work(fixture)
            self.assertIn(AI_WASHING, proposed)

            result = fixture.service().confirm_topics("PR1", [AI_WASHING])

            self.assertEqual(result.status, ConfirmationStatus.CONFIRMED)
            self.assertEqual(result.confirmed_topics, (AI_WASHING,))
            self.assertEqual(result.assignment_source, AssignmentSource.HUMAN_CONFIRMED.value)
            self.assertEqual(result.original_classification_status, "REVIEW_REQUIRED")
            self.assertTrue(result.topic_metadata_updated)
            self.assertTrue(result.provenance_recorded)
            self.assertFalse(result.human_override)

    def test_several_proposed_topics_can_be_confirmed_together(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-multi-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            proposed = _review_work(fixture)
            chosen = [t for t in (AI_WASHING, DISCLOSURE) if t in proposed]
            self.assertEqual(len(chosen), 2, proposed)

            result = fixture.service().confirm_topics("PR1", chosen)

            self.assertEqual(result.status, ConfirmationStatus.CONFIRMED)
            self.assertEqual(set(result.confirmed_topics), set(chosen))


class RejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_a_topic_outside_the_taxonomy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-unk-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)
            result = fixture.service().confirm_topics("PR1", ["99_虚构" + chr(92) + "不存在"])
            self.assertEqual(result.status, ConfirmationStatus.UNKNOWN_TOPIC)
            self.assertEqual(fixture.store.topics_for("PR1"), ())

    def test_a_valid_taxonomy_topic_that_was_not_proposed_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-notprop-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            proposed = _review_work(fixture)
            self.assertNotIn(GREEN, proposed)

            result = fixture.service().confirm_topics("PR1", [GREEN])

            self.assertEqual(result.status, ConfirmationStatus.SELECTED_TOPIC_NOT_PROPOSED)
            self.assertEqual(fixture.store.topics_for("PR1"), ())

    def test_an_override_can_confirm_a_taxonomy_topic_that_was_not_proposed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-override-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)
            result = fixture.service().confirm_topics(
                "PR1", [GREEN], allow_taxonomy_override=True
            )
            self.assertEqual(result.status, ConfirmationStatus.CONFIRMED)
            self.assertTrue(result.human_override)

    def test_an_empty_selection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-empty-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)
            result = fixture.service().confirm_topics("PR1", [])
            self.assertEqual(result.status, ConfirmationStatus.EMPTY_SELECTION)
            self.assertEqual(fixture.store.topics_for("PR1"), ())

    def test_a_work_with_no_proposals_cannot_be_confirmed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-noprop-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PN", "A study of nineteenth century maritime insurance")
            result = fixture.service().confirm_topics("PN", [AI_WASHING])
            self.assertEqual(result.status, ConfirmationStatus.NO_CONFIRMABLE_PROPOSALS)

    def test_an_unknown_paper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-nowork-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            result = fixture.service().confirm_topics("NOPE", [AI_WASHING])
            self.assertEqual(result.status, ConfirmationStatus.WORK_NOT_FOUND)

    def test_an_auto_classified_work_cannot_be_reclassified_by_confirmation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-auto-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PA", "AI漂洗与企业信息披露", keywords="AI漂洗")
            service = fixture.service()
            auto, _ = service.classify_after_ingest("PA", disposition="NEW_WORK")
            self.assertTrue(auto.applied)

            result = service.confirm_topics("PA", [GREEN], allow_taxonomy_override=True)

            self.assertEqual(result.status, ConfirmationStatus.NOT_REVIEW_REQUIRED)
            self.assertEqual(result.assignment_source, AssignmentSource.AUTO_CLASSIFIED.value)

    def test_a_work_with_topics_but_no_record_reports_unknown_legacy(self) -> None:
        """Absence of a record is not evidence the harness decided.

        P4914EEBF5A11 is the concrete case: the classifier returned
        REVIEW_REQUIRED and assigned nothing, a person picked one of the
        proposals, and an Agent wrote it through the low-level store because
        no confirmation command existed yet.  Reading its missing record as
        AUTO_CLASSIFIED would state the opposite of what happened."""

        with tempfile.TemporaryDirectory(prefix="cfm-legacy-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PA", "AI漂洗与企业信息披露", keywords="AI漂洗")
            service = fixture.service()
            # apply_classification alone is the pre-workflow write path: it
            # settles topics and records nothing.
            applied, _ = service.apply_classification("PA")
            self.assertTrue(applied.applied)
            self.assertEqual(fixture.provenance.load(), [])

            result = service.confirm_topics("PA", [GREEN], allow_taxonomy_override=True)

            self.assertEqual(result.status, ConfirmationStatus.NOT_REVIEW_REQUIRED)
            self.assertEqual(
                result.assignment_source, AssignmentSource.UNKNOWN_LEGACY.value
            )
            self.assertNotEqual(
                result.assignment_source, AssignmentSource.AUTO_CLASSIFIED.value
            )


class IdempotencyAndConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_repeating_the_same_confirmation_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-idem-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)
            service = fixture.service()
            service.confirm_topics("PR1", [AI_WASHING])
            store_bytes = fixture.store.jsonl_path.read_bytes()
            provenance_lines = len(fixture.provenance.load())

            again = service.confirm_topics("PR1", [AI_WASHING])

            self.assertEqual(again.status, ConfirmationStatus.ALREADY_CONFIRMED)
            self.assertEqual(fixture.store.jsonl_path.read_bytes(), store_bytes)
            self.assertEqual(len(fixture.provenance.load()), provenance_lines)

    def test_confirming_a_different_set_afterwards_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-conflict-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)
            service = fixture.service()
            service.confirm_topics("PR1", [AI_WASHING])

            clash = service.confirm_topics("PR1", [DISCLOSURE])

            self.assertEqual(clash.status, ConfirmationStatus.TOPIC_CONFIRMATION_CONFLICT)
            self.assertEqual(
                [label.label for label in fixture.store.topics_for("PR1")], [AI_WASHING]
            )


class CanonicalEffectTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_confirmation_writes_canonical_topics(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-canon-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)
            fixture.service().confirm_topics("PR1", [AI_WASHING])
            self.assertEqual(
                [label.label for label in fixture.store.topics_for("PR1")], [AI_WASHING]
            )

    def test_confirmation_hardlinks_the_view_and_copies_no_pdf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-view-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)
            result = fixture.service().confirm_topics("PR1", [AI_WASHING])

            self.assertEqual(result.pdf_copies_created, 0)
            self.assertTrue(result.topic_view_updated)
            canonical = fixture.papers / "PR1.pdf"
            linked = list((fixture.view.view_root).rglob("PR1.pdf"))
            self.assertTrue(linked)
            for path in linked:
                self.assertEqual(path.stat().st_ino, canonical.stat().st_ino)

    def test_confirmation_updates_the_links_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-manifest-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)
            fixture.service().confirm_topics("PR1", [AI_WASHING])
            with fixture.view.links_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = [row for row in csv.DictReader(handle) if row["paper_id"] == "PR1"]
            self.assertTrue(rows)
            self.assertEqual(rows[0]["topic"], "AI漂洗")

    def test_topics_that_were_only_proposed_are_not_written(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-onlyprop-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            proposed = _review_work(fixture)
            fixture.service().confirm_topics("PR1", [AI_WASHING])
            written = {label.label for label in fixture.store.topics_for("PR1")}
            self.assertEqual(written, {AI_WASHING})
            self.assertTrue(set(proposed) - written, "the other proposals must stay unwritten")


class ProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_provenance_records_a_human_decision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-prov-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            proposed = _review_work(fixture)
            fixture.service().confirm_topics("PR1", [AI_WASHING])

            entry = fixture.provenance.latest_for("PR1")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["assignment_source"], AssignmentSource.HUMAN_CONFIRMED.value)
            self.assertEqual(entry["confirmation_source"], "HUMAN")
            self.assertEqual(entry["classification_status_before"], "REVIEW_REQUIRED")
            self.assertEqual(entry["topics"], [AI_WASHING])
            self.assertEqual(set(entry["proposed_topics"]), set(proposed))
            self.assertIn("confirmed_at", entry)

    def test_provenance_records_no_personal_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfm-priv-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)
            fixture.service().confirm_topics("PR1", [AI_WASHING])
            raw = fixture.provenance.path.read_text(encoding="utf-8")
            for leak in ("<user>", "C:\\Users", "username", "USERNAME"):
                self.assertNotIn(leak, raw)

    def test_human_confirmed_is_recorded_after_confirmation_succeeds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prov-human-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)

            result = fixture.service().confirm_topics("PR1", [AI_WASHING])

            self.assertEqual(result.status, ConfirmationStatus.CONFIRMED)
            record = fixture.provenance.latest_for("PR1")
            self.assertEqual(record["assignment_source"], "HUMAN_CONFIRMED")
            self.assertEqual(record["classification_status_before"], "REVIEW_REQUIRED")
            self.assertEqual(record["topics"], [AI_WASHING])


class _UnwritableProvenance(TopicProvenanceStore):
    """A provenance file that cannot be written, however it is asked."""

    def ensure_writable(self) -> None:
        raise OSError(2, "No such file or directory")

    def append(self, entry) -> None:  # pragma: no cover - must never be reached
        raise OSError(2, "No such file or directory")


class FailedConfirmationTests(unittest.TestCase):
    """A confirmation that cannot record itself must change nothing.

    Provenance is part of the confirmation, not a footnote to it.  Writing
    the topics first and discovering afterwards that the record could not be
    written left the work settled with nothing accounting for it, and the
    second attempt was then refused because the work already had topics.
    """

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_unwritable_provenance_leaves_the_canonical_state_untouched(self) -> None:
        """Provenance failing must not leave topics behind with no record.

        Ordering is the whole point: the canonical write used to happen first,
        so a provenance failure left the work classified and unaccounted for,
        and a second attempt was refused because the work already had topics.
        """

        with tempfile.TemporaryDirectory(prefix="cfm-nowrite-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            proposed = _review_work(fixture)
            self.assertIn(AI_WASHING, proposed)
            service = PostAcquisitionClassifier(
                store=fixture.store,
                taxonomy=fixture.taxonomy,
                classifier=WorkClassifier(taxonomy=fixture.taxonomy),
                view=fixture.view,
                catalog_path=fixture.root / "missing-catalog.jsonl",
                provenance=_UnwritableProvenance(path=fixture.root / "unwritable.jsonl"),
            )

            result = service.confirm_topics("PR1", [AI_WASHING])

            self.assertEqual(result.status, ConfirmationStatus.FAILED_SAFE)
            self.assertFalse(result.provenance_recorded)
            self.assertEqual(result.confirmed_topics, ())
            self.assertEqual(fixture.store.topics_for("PR1"), ())
            self.assertFalse((fixture.root / "papers_by_topic").exists())
            self.assertFalse((fixture.root / "unwritable.jsonl").exists())

    def test_a_failed_confirmation_can_be_retried(self) -> None:
        """Failing safe means the next attempt still finds a confirmable work."""

        with tempfile.TemporaryDirectory(prefix="cfm-retry-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            _review_work(fixture)
            broken = PostAcquisitionClassifier(
                store=fixture.store,
                taxonomy=fixture.taxonomy,
                classifier=WorkClassifier(taxonomy=fixture.taxonomy),
                view=fixture.view,
                catalog_path=fixture.root / "missing-catalog.jsonl",
                provenance=_UnwritableProvenance(path=fixture.root / "unwritable.jsonl"),
            )
            self.assertEqual(
                broken.confirm_topics("PR1", [AI_WASHING]).status,
                ConfirmationStatus.FAILED_SAFE,
            )

            result = fixture.service().confirm_topics("PR1", [AI_WASHING])

            self.assertEqual(result.status, ConfirmationStatus.CONFIRMED)
            self.assertEqual(result.confirmed_topics, (AI_WASHING,))
            self.assertTrue(result.provenance_recorded)


class LegacyBackfillShapeTests(unittest.TestCase):
    """What a P4914-style backfill would look like, without performing one."""

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_a_backfilled_record_leaves_the_canonical_row_untouched(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prov-backfill-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work(
                "PLEGACY",
                "AI washing: Strategic disclosure and backlash",
                topics=AI_WASHING,
            )
            row_before = fixture.store.load()["PLEGACY"]

            fixture.provenance.record(
                paper_id="PLEGACY",
                topics=[AI_WASHING],
                source=AssignmentSource.HUMAN_CONFIRMED,
                classification_status_before="REVIEW_REQUIRED",
                proposed_topics=[AI_WASHING, DISCLOSURE],
            )

            row_after = fixture.store.load()["PLEGACY"]
            self.assertEqual(row_after, row_before)
            self.assertEqual(
                [label.label for label in row_after.topics], [AI_WASHING]
            )
            record = fixture.provenance.latest_for("PLEGACY")
            self.assertEqual(record["assignment_source"], "HUMAN_CONFIRMED")
            self.assertEqual(record["classification_status_before"], "REVIEW_REQUIRED")

    def test_a_backfilled_record_carries_no_personal_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prov-noident-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.provenance.record(
                paper_id="PLEGACY",
                topics=[AI_WASHING],
                source=AssignmentSource.HUMAN_CONFIRMED,
                classification_status_before="REVIEW_REQUIRED",
            )
            blob = fixture.provenance.path.read_text(encoding="utf-8")
            import getpass
            import os

            for secret in (getpass.getuser(), os.environ.get("USERNAME", "")):
                if secret:
                    self.assertNotIn(secret, blob)


class SharedApplyPathTests(unittest.TestCase):
    """Confirmation reuses the automatic write path rather than a second writer."""

    def test_human_confirmed_is_accepted_by_the_common_apply_step(self) -> None:
        from hunnu_harness.literature.classification import ClassificationResult

        result = ClassificationResult(
            paper_id="PZ", status=ClassificationStatus.HUMAN_CONFIRMED
        )
        self.assertTrue(result.is_classified)

    def test_review_required_is_still_not_applyable_on_its_own(self) -> None:
        from hunnu_harness.literature.classification import ClassificationResult

        result = ClassificationResult(
            paper_id="PZ", status=ClassificationStatus.REVIEW_REQUIRED
        )
        self.assertFalse(result.is_classified)


if __name__ == "__main__":
    unittest.main()
