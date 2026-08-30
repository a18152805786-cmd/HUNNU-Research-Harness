"""Recording how a work's topics were settled.

The catalog says what a paper is filed under and never says who decided.  That
gap is not academic: the corpus contains a work whose topic a person chose after
classification declined to, and one whose topic the classifier assigned, and
nothing in the metadata told them apart.

What is pinned here is the record and its own integrity -- that absence of a
record is never read as "the harness decided", that the digest is separate from
the corpus digest, and that a corrupt line is detected rather than quietly
dropped.  Every test runs against an isolated library under TEMP_DIR.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.auto_classification import PostAcquisitionClassifier
from hunnu_harness.literature.classification import ClassificationStatus, WorkClassifier
from hunnu_harness.literature.topic_confirmation import (
    TOPIC_PROVENANCE_PATH,
    AssignmentSource,
    TopicProvenanceStore,
)
from hunnu_harness.literature.topics import (
    TopicStore,
    TopicTaxonomy,
    TopicViewBuilder,
    WorkTopicRow,
    parse_topics_field,
)
from hunnu_harness.navigator.fingerprint import LibraryFingerprinter
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

    def service(self, **overrides) -> PostAcquisitionClassifier:
        return PostAcquisitionClassifier(
            store=self.store,
            taxonomy=self.taxonomy,
            classifier=WorkClassifier(taxonomy=self.taxonomy),
            view=self.view,
            catalog_path=self.root / "missing-catalog.jsonl",
            **({"provenance": self.provenance} | overrides),
        )


class _UnwritableProvenance(TopicProvenanceStore):
    """A provenance file that cannot be written, however it is asked."""

    def ensure_writable(self) -> None:
        raise OSError(2, "No such file or directory")

    def append(self, entry) -> None:  # pragma: no cover - must never be reached
        raise OSError(2, "No such file or directory")


class _NullCatalogReader:
    """A catalog reader for fixtures that have no Navigator catalog."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self):
        from hunnu_harness.navigator.catalog import CatalogSnapshot

        return CatalogSnapshot(
            works=(),
            degraded=(),
            catalog_path=self.root / "papers.jsonl",
            topics_path=self.root / "paper_topics.jsonl",
            topics_present=False,
        )


def temp_root(prefix: str):
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=TEMP_DIR)


class MissingRecordTests(unittest.TestCase):
    """Silence is not a decision."""

    def test_a_missing_sidecar_reports_no_recorded_provenance(self) -> None:
        with temp_root("prov-absent-") as tmp:
            store = TopicProvenanceStore(path=Path(tmp) / "prov.jsonl")
            fingerprint = store.fingerprint()
            self.assertFalse(fingerprint.present)
            self.assertEqual(fingerprint.record_count, 0)
            self.assertIsNone(store.latest_for("PANY"))
            self.assertTrue(fingerprint.intact)

    def test_unknown_legacy_exists_and_is_not_auto_classified(self) -> None:
        """The 179 works predating this file must not be claimed as automatic."""

        self.assertIn(AssignmentSource.UNKNOWN_LEGACY, AssignmentSource)
        self.assertNotEqual(
            AssignmentSource.UNKNOWN_LEGACY, AssignmentSource.AUTO_CLASSIFIED
        )


class AutomaticPathTests(unittest.TestCase):
    """When the harness decides, it says so; when it does not, it stays quiet."""

    def test_ingest_records_that_the_harness_chose_the_topics(self) -> None:
        with temp_root("prov-auto-") as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PA", "AI漂洗与企业信息披露", keywords="AI漂洗")

            applied, _ = fixture.service().classify_after_ingest("PA", disposition="NEW_WORK")

            self.assertTrue(applied.applied)
            record = fixture.provenance.latest_for("PA")
            self.assertEqual(record["assignment_source"], "AUTO_CLASSIFIED")
            self.assertEqual(record["confirmation_source"], "HARNESS")
            self.assertFalse(record["human_override"])
            self.assertEqual(record["topics"], list(applied.assigned_labels))

    def test_auto_classified_is_recorded_only_after_the_write_succeeds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prov-auto-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PA", "AI漂洗与企业信息披露", keywords="AI漂洗")

            applied, _ = fixture.service().classify_after_ingest(
                "PA", disposition="NEW_WORK"
            )

            self.assertTrue(applied.applied)
            self.assertTrue(fixture.store.topics_for("PA"))
            record = fixture.provenance.latest_for("PA")
            self.assertEqual(record["assignment_source"], "AUTO_CLASSIFIED")

    def test_review_required_records_nothing_at_all(self) -> None:
        """A proposal is not an assignment, so it earns no record."""

        with temp_root("prov-review-") as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PR1", "AI washing: strategic disclosure and backlash")

            result, outcome = fixture.service().classify_after_ingest(
                "PR1", disposition="NEW_WORK"
            )

            self.assertEqual(result.status, ClassificationStatus.REVIEW_REQUIRED)
            self.assertFalse(result.applied)
            self.assertEqual(outcome.reason, "NOT_APPLIED_REVIEW_REQUIRED")
            self.assertEqual(fixture.provenance.load(), [])

    def test_a_dry_run_records_nothing(self) -> None:
        with temp_root("prov-dry-") as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PA", "AI漂洗与企业信息披露", keywords="AI漂洗")

            fixture.service().classify_after_ingest("PA", disposition="NEW_WORK", dry_run=True)

            self.assertEqual(fixture.provenance.load(), [])
            self.assertEqual(fixture.store.topics_for("PA"), ())

    def test_a_lost_record_never_undoes_an_archive(self) -> None:
        """The paper is already archived; a bookkeeping failure must not undo it."""

        with temp_root("prov-lost-") as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PA", "AI漂洗与企业信息披露", keywords="AI漂洗")
            service = fixture.service(
                provenance=_UnwritableProvenance(path=fixture.root / "unwritable.jsonl")
            )

            applied, outcome = service.classify_after_ingest("PA", disposition="NEW_WORK")

            self.assertTrue(applied.applied)
            self.assertEqual(outcome.reason, "APPLIED")
            self.assertTrue(fixture.store.topics_for("PA"))


class IsolationTests(unittest.TestCase):
    """Redirecting the catalog has to be enough to stay out of the real library."""

    def test_the_default_service_uses_the_canonical_provenance_file(self) -> None:
        self.assertEqual(
            PostAcquisitionClassifier().provenance.path, Path(TOPIC_PROVENANCE_PATH)
        )

    def test_an_isolated_catalog_gets_an_isolated_provenance_file(self) -> None:
        with temp_root("prov-isolated-") as tmp:
            fixture = _Fixture(Path(tmp))
            service = PostAcquisitionClassifier(
                store=fixture.store,
                taxonomy=fixture.taxonomy,
                classifier=WorkClassifier(taxonomy=fixture.taxonomy),
                view=fixture.view,
                catalog_path=fixture.root / "missing-catalog.jsonl",
            )

            self.assertNotEqual(service.provenance.path, Path(TOPIC_PROVENANCE_PATH))
            self.assertTrue(service.provenance.path.is_relative_to(fixture.root))

            # Compared rather than asserted absent: the real file legitimately
            # exists once anyone records an assignment for real.
            real = Path(TOPIC_PROVENANCE_PATH)
            before = real.read_bytes() if real.exists() else None

            fixture.add_work("PA", "AI漂洗与企业信息披露", keywords="AI漂洗")
            service.classify_after_ingest("PA", disposition="NEW_WORK")

            self.assertTrue(service.provenance.path.exists())
            after = real.read_bytes() if real.exists() else None
            self.assertEqual(after, before)


class StoreIntegrityTests(unittest.TestCase):
    def test_the_store_appends_rather_than_replaces(self) -> None:
        with temp_root("prov-append-") as tmp:
            store = TopicProvenanceStore(path=Path(tmp) / "prov.jsonl")
            store.record(
                paper_id="PX",
                topics=[AI_WASHING],
                source=AssignmentSource.AUTO_CLASSIFIED,
                classification_status_before="CLASSIFIED",
            )
            store.record(
                paper_id="PY",
                topics=[DISCLOSURE],
                source=AssignmentSource.HUMAN_CONFIRMED,
                classification_status_before="REVIEW_REQUIRED",
            )
            self.assertEqual(len(store.load()), 2)
            self.assertEqual(store.latest_for("PX")["assignment_source"], "AUTO_CLASSIFIED")
            self.assertEqual(store.latest_for("PY")["assignment_source"], "HUMAN_CONFIRMED")

    def test_a_deep_library_can_still_record_provenance(self) -> None:
        """The failure was path length, not permissions, so pin the length."""

        with temp_root("prov-deep-") as tmp:
            root = Path(tmp)
            padding = 199 - len(str(root)) - 1
            if padding < 1:
                self.skipTest("TEMP_DIR is already too deep to build the case")
            deep = root / ("d" * min(padding, 120))
            while len(str(deep)) < 199:
                deep = deep / ("d" * min(199 - len(str(deep)) - 1, 60))
            deep.mkdir(parents=True, exist_ok=True)

            store = TopicProvenanceStore(path=deep / "topic_assignment_provenance.jsonl")
            store.ensure_writable()
            store.record(
                paper_id="PDEEP",
                topics=[AI_WASHING],
                source=AssignmentSource.HUMAN_CONFIRMED,
                classification_status_before="REVIEW_REQUIRED",
            )

            self.assertEqual(len(store.load()), 1)
            self.assertEqual(store.latest_for("PDEEP")["topics"], [AI_WASHING])

    def test_a_record_carries_no_personal_identity(self) -> None:
        with temp_root("prov-noident-") as tmp:
            import getpass
            import os

            store = TopicProvenanceStore(path=Path(tmp) / "prov.jsonl")
            store.record(
                paper_id="PX",
                topics=[AI_WASHING],
                source=AssignmentSource.HUMAN_CONFIRMED,
                classification_status_before="REVIEW_REQUIRED",
            )
            blob = store.path.read_text(encoding="utf-8")
            for secret in (getpass.getuser(), os.environ.get("USERNAME", "")):
                if secret:
                    self.assertNotIn(secret, blob)

    def test_the_official_sidecar_sits_outside_every_fingerprinted_directory(self) -> None:
        from hunnu_harness.paths import (
            LIBRARY_CATALOG_DIR,
            LIBRARY_PAPERS_DIR,
            PAPERS_BY_TOPIC_DIR,
        )

        sidecar = Path(TOPIC_PROVENANCE_PATH)
        for directory in (LIBRARY_CATALOG_DIR, LIBRARY_PAPERS_DIR, PAPERS_BY_TOPIC_DIR):
            self.assertFalse(
                sidecar.is_relative_to(Path(directory)),
                f"provenance must not live under {directory}",
            )


class FingerprintTests(unittest.TestCase):
    """The provenance digest is separate from the corpus digest, and stable."""

    def _store(self, tmp: str) -> TopicProvenanceStore:
        store = TopicProvenanceStore(path=Path(tmp) / "prov.jsonl")
        store.record(
            paper_id="PX",
            topics=[AI_WASHING],
            source=AssignmentSource.AUTO_CLASSIFIED,
            classification_status_before="CLASSIFIED",
        )
        store.record(
            paper_id="PY",
            topics=[DISCLOSURE],
            source=AssignmentSource.HUMAN_CONFIRMED,
            classification_status_before="REVIEW_REQUIRED",
        )
        return store

    def test_the_same_records_always_give_the_same_digest(self) -> None:
        with temp_root("prov-stable-") as tmp:
            store = self._store(tmp)
            self.assertEqual(store.fingerprint().sha256, store.fingerprint().sha256)
            first = store.fingerprint()
            self.assertEqual(first.record_count, 2)
            self.assertEqual(first.paper_count, 2)
            self.assertEqual(
                dict(first.sources), {"AUTO_CLASSIFIED": 1, "HUMAN_CONFIRMED": 1}
            )

    def test_reordering_the_file_does_not_change_the_digest(self) -> None:
        """Each record carries its own timestamp, so physical order says nothing."""

        with temp_root("prov-order-") as tmp:
            store = self._store(tmp)
            before = store.fingerprint().sha256
            lines = store.path.read_text(encoding="utf-8").splitlines()
            store.path.write_text(
                chr(10).join(reversed(lines)) + chr(10), encoding="utf-8", newline=chr(10)
            )
            self.assertEqual(store.fingerprint().sha256, before)

    def test_changing_a_record_changes_the_digest(self) -> None:
        with temp_root("prov-change-") as tmp:
            store = self._store(tmp)
            before = store.fingerprint().sha256
            store.record(
                paper_id="PZ",
                topics=[GREEN],
                source=AssignmentSource.HUMAN_CONFIRMED,
                classification_status_before="REVIEW_REQUIRED",
            )
            self.assertNotEqual(store.fingerprint().sha256, before)

    def test_a_corrupt_line_is_detected_rather_than_silently_dropped(self) -> None:
        with temp_root("prov-corrupt-") as tmp:
            store = self._store(tmp)
            clean = store.fingerprint()
            with store.path.open("a", encoding="utf-8", newline=chr(10)) as handle:
                handle.write("{not json at all" + chr(10))

            damaged = store.fingerprint()

            self.assertEqual(damaged.malformed_lines, 1)
            self.assertFalse(damaged.intact)
            self.assertNotEqual(damaged.sha256, clean.sha256)
            # The readable records still read, so nothing downstream is blocked.
            self.assertEqual(len(store.load()), 2)

    def test_the_digest_carries_no_machine_or_user_specific_value(self) -> None:
        with temp_root("prov-here-") as one:
            with temp_root("prov-there-") as two:
                first = self._store(one)
                other = TopicProvenanceStore(path=Path(two) / "elsewhere.jsonl")
                other.path.write_text(
                    first.path.read_text(encoding="utf-8"), encoding="utf-8", newline=chr(10)
                )
                self.assertEqual(other.fingerprint().sha256, first.fingerprint().sha256)

    def test_provenance_alone_never_moves_the_corpus_fingerprint(self) -> None:
        """A note about how an assignment was made is not a corpus change."""

        with temp_root("prov-corpus-") as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PA", "AI漂洗与企业信息披露", keywords="AI漂洗")
            fixture.service().classify_after_ingest("PA", disposition="NEW_WORK")

            fingerprinter = LibraryFingerprinter(
                catalog_dir=fixture.root / "library" / "catalog",
                papers_dir=fixture.papers,
                by_topic_dir=fixture.root / "papers_by_topic",
                reader=_NullCatalogReader(fixture.root),
            )
            corpus_before = fingerprinter.capture().as_dict()["fingerprint_sha256"]
            provenance_before = fixture.provenance.fingerprint().sha256

            fixture.provenance.record(
                paper_id="PLEGACY",
                topics=[AI_WASHING],
                source=AssignmentSource.HUMAN_CONFIRMED,
                classification_status_before="REVIEW_REQUIRED",
            )

            self.assertEqual(
                fingerprinter.capture().as_dict()["fingerprint_sha256"], corpus_before
            )
            self.assertNotEqual(fixture.provenance.fingerprint().sha256, provenance_before)


if __name__ == "__main__":
    unittest.main()
