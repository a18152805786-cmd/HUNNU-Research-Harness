"""Post-acquisition WORK-level topic classification.

Every test here works on an isolated taxonomy, topic store, and view under
TEMP_DIR.  The canonical Library is read in exactly one place -- the regression
that re-runs the backtest -- and never written.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.auto_classification import (
    NavigatorReadiness,
    PostAcquisitionClassifier,
)
from hunnu_harness.literature.classification import (
    ClassificationInput,
    ClassificationMode,
    ClassificationStatus,
    SIGNAL_LEXICON,
    SIGNAL_TAXONOMY_NAME,
    WorkClassifier,
    taxonomy_name_terms,
)
from hunnu_harness.literature.topics import (
    LINK_TYPE_HARDLINK,
    TopicLabel,
    TopicStore,
    TopicTaxonomy,
    TopicTaxonomyError,
    TopicViewBuilder,
    UnknownTopicValue,
    WorkTopicRow,
    format_topics_field,
    parse_topics_field,
)
from hunnu_harness.paths import TEMP_DIR

TAXONOMY = {
    "domains": {
        "01_人工智能与数字经济": ["AI漂洗", "数字化转型"],
        "04_会计审计与信息披露": ["信息披露", "审计与内部控制"],
        "09_ESG与绿色发展": ["绿色金融与绿色创新"],
    },
    "generated_at": "2026-08-30T00:00:00+00:00",
}


def _write_taxonomy(root: Path) -> Path:
    path = root / "topic_taxonomy.json"
    path.write_text(json.dumps(TAXONOMY, ensure_ascii=False), encoding="utf-8")
    return path


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%%EOF\n"


def _manifest_entry():
    """A minimal manifest entry, for asserting on the agent-facing field set."""

    from hunnu_harness.literature.models import DownloadManifestEntry

    return DownloadManifestEntry(
        paper_id="PX",
        source="Test",
        title="title",
        doi="unknown",
        access_type="OPEN_ACCESS",
        authorized_access=True,
        original_url_or_stable_identifier="unknown",
        original_filename="a.pdf",
        normalized_filename="a.pdf",
        download_timestamp="2026-08-30T00:00:00+00:00",
        file_size_bytes=1,
        sha256="a" * 64,
        local_path="a.pdf",
        pdf_validation_passed=True,
    )


class _Fixture:
    """An isolated taxonomy + topic store + view rooted in one temp directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.taxonomy = TopicTaxonomy.load(_write_taxonomy(root))
        self.papers = root / "library" / "papers"
        self.papers.mkdir(parents=True, exist_ok=True)
        self.store = TopicStore(
            jsonl_path=root / "library" / "catalog" / "paper_topics.jsonl",
            csv_path=root / "library" / "catalog" / "paper_topics.csv",
            taxonomy=self.taxonomy,
        )
        self.view = TopicViewBuilder(
            view_root=root / "papers_by_topic",
            papers_dir=self.papers,
        )

    def add_work(self, paper_id: str, title: str, *, keywords: str = "", topics: str = "") -> WorkTopicRow:
        managed = self.papers / f"{paper_id}.pdf"
        managed.write_bytes(_pdf_bytes())
        row = WorkTopicRow(
            paper_id=paper_id,
            title=title,
            keywords=keywords,
            topics=parse_topics_field(topics),
            human_readable_name=f"{paper_id}.pdf",
            canonical_path=str(managed),
            canonical_sha256="a" * 64,
        )
        rows = self.store.load()
        rows[paper_id] = row
        self.store.commit(rows)
        return row

    def classifier(self, **kwargs) -> PostAcquisitionClassifier:
        return PostAcquisitionClassifier(
            store=self.store,
            taxonomy=self.taxonomy,
            classifier=WorkClassifier(taxonomy=self.taxonomy, **kwargs),
            view=self.view,
            catalog_path=self.root / "missing-catalog.jsonl",
        )


class TaxonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_taxonomy_rejects_subtopic_reused_across_domains(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-tax-", dir=TEMP_DIR) as tmp:
            path = Path(tmp) / "topic_taxonomy.json"
            path.write_text(
                json.dumps({"domains": {"A": ["shared"], "B": ["shared"]}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TopicTaxonomyError, "not unique"):
                TopicTaxonomy.load(path)

    def test_compound_subtopic_splits_into_matchable_parts(self) -> None:
        self.assertEqual(
            taxonomy_name_terms("融资约束与资本配置"), ("融资约束", "资本配置")
        )
        self.assertEqual(taxonomy_name_terms("信息披露"), ("信息披露",))

    def test_topics_field_round_trips(self) -> None:
        raw = "01_人工智能与数字经济\\AI漂洗;04_会计审计与信息披露\\信息披露"
        labels = parse_topics_field(raw)
        self.assertEqual(len(labels), 2)
        self.assertEqual(format_topics_field(labels), raw)

    def test_duplicate_items_collapse(self) -> None:
        raw = "01_人工智能与数字经济\\AI漂洗;01_人工智能与数字经济\\AI漂洗"
        self.assertEqual(len(parse_topics_field(raw)), 1)


class ClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_known_work_dry_run_classifies_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-dry-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("P1", "AI漂洗与企业信息披露质量", keywords="AI漂洗")
            before = fixture.store.jsonl_path.read_bytes()

            result = fixture.classifier().classify_work("P1")

            self.assertEqual(result.status, ClassificationStatus.CLASSIFIED)
            self.assertIn("01_人工智能与数字经济\\AI漂洗", result.assigned_labels)
            self.assertFalse(result.applied)
            self.assertEqual(fixture.store.jsonl_path.read_bytes(), before)
            self.assertFalse(fixture.view.view_root.exists())

    def test_a_single_uncorroborated_signal_is_not_enough_to_auto_assign(self) -> None:
        # One concept hit in the title alone reaches confidence 0.5, below the
        # calibrated 0.75 gate.  Precision is bought exactly here: a lone signal
        # goes to review rather than becoming a silent assignment.  The title
        # uses a lexicon surface form that is not an English subtopic alias, so
        # the lexicon really is the only evidence family firing.
        with tempfile.TemporaryDirectory(prefix="cls-weak-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PW", "The talk-walk gap in corporate technology narratives")
            result = fixture.classifier().classify_work("PW")
            self.assertEqual(result.status, ClassificationStatus.REVIEW_REQUIRED)
            self.assertEqual(result.reason, "BELOW_AUTO_ASSIGN_THRESHOLD")
            self.assertEqual(result.assigned_labels, ())
            self.assertTrue(result.proposed_topics)

    def test_multi_label_classification_stays_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-multi-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work(
                "P2",
                "AI漂洗、信息披露与绿色金融与绿色创新的关系",
                keywords="AI漂洗;信息披露;绿色金融",
            )
            result = fixture.classifier().classify_work("P2")
            self.assertEqual(result.status, ClassificationStatus.CLASSIFIED)
            self.assertGreater(len(result.assigned_labels), 1)
            self.assertLessEqual(len(result.assigned_labels), 3)

    def test_classifier_never_creates_a_topic_outside_the_taxonomy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-closed-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            known = {label.label for label in fixture.taxonomy.labels()}
            for index, title in enumerate(
                (
                    "AI washing and audit risk",
                    "区块链与元宇宙治理",
                    "Quantum computing in supply chains",
                    "信息披露质量研究",
                )
            ):
                paper_id = f"PC{index}"
                fixture.add_work(paper_id, title)
                result = fixture.classifier().classify_work(paper_id)
                for label in result.assigned_labels + tuple(
                    item.topic for item in result.proposed_topics
                ):
                    self.assertIn(label, known)

    def test_low_confidence_returns_review_required_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-low-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("P3", "A study of nineteenth century maritime insurance")
            before = fixture.store.jsonl_path.read_bytes()

            result, outcome = fixture.classifier().apply_classification("P3")

            self.assertEqual(result.status, ClassificationStatus.REVIEW_REQUIRED)
            self.assertTrue(result.review_required)
            self.assertEqual(result.assigned_labels, ())
            self.assertFalse(outcome.topic_metadata_updated)
            self.assertEqual(fixture.store.jsonl_path.read_bytes(), before)

    def test_metadata_only_work_still_classifies(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-meta-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("P4", "AI漂洗对企业信息披露的影响")
            result = fixture.classifier().classify_work("P4")
            self.assertEqual(result.mode, ClassificationMode.METADATA_ONLY)
            self.assertFalse(result.fulltext_used)
            self.assertEqual(result.status, ClassificationStatus.CLASSIFIED)

    def test_work_without_identity_text_fails_to_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-empty-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("P5", "")
            result = fixture.classifier().classify_work("P5")
            self.assertEqual(result.status, ClassificationStatus.REVIEW_REQUIRED)
            self.assertEqual(result.mode, ClassificationMode.UNAVAILABLE)
            self.assertEqual(result.reason, "NO_IDENTITY_TEXT_AVAILABLE")

    def test_missing_work_fails_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-missing-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            result = fixture.classifier().classify_work("NOPE")
            self.assertEqual(result.status, ClassificationStatus.FAILED_SAFE)

    def test_unreadable_fulltext_degrades_to_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-scan-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("P6", "AI漂洗与信息披露")

            def explode(paper_id, row):
                raise OSError("scanned PDF yields no text")

            service = PostAcquisitionClassifier(
                store=fixture.store,
                taxonomy=fixture.taxonomy,
                classifier=WorkClassifier(taxonomy=fixture.taxonomy),
                view=fixture.view,
                catalog_path=Path(tmp) / "missing.jsonl",
                fulltext_reader=explode,
            )
            result = service.classify_work("P6")
            self.assertEqual(result.status, ClassificationStatus.CLASSIFIED)
            self.assertFalse(result.fulltext_used)


class ExistingWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_existing_topics_are_preserved_and_not_reclassified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-exist-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work(
                "P7",
                "AI washing and disclosure",
                topics="09_ESG与绿色发展\\绿色金融与绿色创新",
            )
            before = fixture.store.jsonl_path.read_bytes()

            result, outcome = fixture.classifier().classify_after_ingest(
                "P7", disposition="SAME_WORK_DIFFERENT_VERSION"
            )

            self.assertEqual(result.status, ClassificationStatus.SKIPPED_EXISTING)
            self.assertEqual(
                result.existing_topics, ("09_ESG与绿色发展\\绿色金融与绿色创新",)
            )
            self.assertFalse(outcome.topic_metadata_updated)
            self.assertEqual(fixture.store.jsonl_path.read_bytes(), before)

    def test_one_work_many_versions_classifies_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-vers-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("P8", "AI漂洗与企业信息披露")
            service = fixture.classifier()

            first, _ = service.classify_after_ingest("P8", disposition="NEW_PAPER")
            self.assertEqual(first.status, ClassificationStatus.CLASSIFIED)
            assigned = fixture.store.topics_for("P8")

            for disposition in ("EXACT_DUPLICATE", "SAME_WORK_DIFFERENT_VERSION"):
                again, outcome = service.classify_after_ingest("P8", disposition=disposition)
                self.assertEqual(again.status, ClassificationStatus.SKIPPED_EXISTING)
                self.assertFalse(outcome.topic_metadata_updated)
            self.assertEqual(fixture.store.topics_for("P8"), assigned)

    def test_existing_work_with_zero_topics_is_classified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-zero-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("P9", "AI漂洗与信息披露", topics="")
            result, outcome = fixture.classifier().classify_after_ingest(
                "P9", disposition="SAME_WORK_DIFFERENT_VERSION"
            )
            self.assertEqual(result.status, ClassificationStatus.CLASSIFIED)
            self.assertTrue(outcome.topic_metadata_updated)


class ApplyAndViewTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-idem-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PA", "AI漂洗与信息披露")
            service = fixture.classifier()

            service.apply_classification("PA")
            first_rows = fixture.store.jsonl_path.read_bytes()
            first_links = fixture.view.links_path.read_bytes()
            first_topics = fixture.store.topics_for("PA")

            second, outcome = service.apply_classification("PA", force=True)

            self.assertEqual(fixture.store.topics_for("PA"), first_topics)
            self.assertEqual(fixture.store.jsonl_path.read_bytes(), first_rows)
            self.assertEqual(fixture.view.links_path.read_bytes(), first_links)
            self.assertEqual(outcome.links_created, ())
            self.assertEqual(len(parse_topics_field(format_topics_field(first_topics))), len(first_topics))

    def test_topic_view_uses_hardlinks_and_copies_no_pdf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-link-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PB", "AI漂洗与信息披露", keywords="AI漂洗;信息披露")
            _result, outcome = fixture.classifier().apply_classification("PB")

            canonical = fixture.papers / "PB.pdf"
            self.assertEqual(outcome.pdf_copies_created, 0)
            self.assertGreaterEqual(len(outcome.links_created), 1)
            for link in outcome.links_created:
                path = Path(link)
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_ino, canonical.stat().st_ino)
            self.assertGreater(canonical.stat().st_nlink, 1)

    def test_topic_metadata_and_view_stay_consistent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-cons-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PD", "AI漂洗与信息披露", keywords="AI漂洗;信息披露")
            fixture.classifier().apply_classification("PD")

            stored = {label.label for label in fixture.store.topics_for("PD")}
            with fixture.view.links_path.open("r", encoding="utf-8-sig", newline="") as handle:
                linked = {
                    f"{row['domain']}\\{row['topic']}"
                    for row in csv.DictReader(handle)
                    if row["paper_id"] == "PD"
                }
            self.assertEqual(stored, linked)
            self.assertTrue(all(row for row in stored))

    def test_link_manifest_records_hardlink_type(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-type-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PE", "AI漂洗研究")
            fixture.classifier().apply_classification("PE")
            with fixture.view.links_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = [row for row in csv.DictReader(handle) if row["paper_id"] == "PE"]
            self.assertTrue(rows)
            self.assertTrue(all(row["link_type"] == LINK_TYPE_HARDLINK for row in rows))

    def test_unrelated_work_topics_are_untouched_by_a_neighbour(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-neigh-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work(
                "PKEEP", "既有论文", topics="04_会计审计与信息披露\\审计与内部控制"
            )
            fixture.add_work("PNEW", "AI漂洗与信息披露")
            fixture.classifier().apply_classification("PNEW")
            self.assertEqual(
                [label.label for label in fixture.store.topics_for("PKEEP")],
                ["04_会计审计与信息披露\\审计与内部控制"],
            )

    def test_store_rejects_topic_outside_taxonomy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-bad-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            row = fixture.add_work("PF", "标题")
            invented = row.with_topics((TopicLabel(domain="99_虚构", subtopic="不存在"),))
            with self.assertRaises(UnknownTopicValue):
                fixture.store.upsert(invented)

    def test_primary_and_secondary_domains_follow_assigned_topics(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-dom-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PG", "AI漂洗与信息披露", keywords="AI漂洗;信息披露")
            fixture.classifier().apply_classification("PG")
            row = fixture.store.load()["PG"]
            self.assertEqual(row.primary_domain, row.topics[0].domain)
            self.assertNotIn(row.primary_domain, row.secondary_domains.split(";"))


class AcquisitionHookTests(unittest.TestCase):
    """The hook contract, exercised without touching the real acquisition chain."""

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_new_work_is_classified_after_ingest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-hook-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PH", "AI漂洗与企业信息披露")
            result, outcome = fixture.classifier().classify_after_ingest(
                "PH", disposition="NEW_PAPER"
            )
            self.assertEqual(result.status, ClassificationStatus.CLASSIFIED)
            self.assertTrue(outcome.topic_metadata_updated)
            self.assertTrue(outcome.topic_view_updated)
            self.assertTrue(fixture.store.topics_for("PH"))

    def test_classification_failure_leaves_the_archived_paper_intact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-fail-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("PI", "AI漂洗与信息披露")
            managed = fixture.papers / "PI.pdf"
            before = managed.read_bytes()

            class _Exploding(WorkClassifier):
                def classify(self, payload, *, existing_topics=()):
                    raise RuntimeError("classifier is broken")

            service = PostAcquisitionClassifier(
                store=fixture.store,
                taxonomy=fixture.taxonomy,
                classifier=_Exploding(taxonomy=fixture.taxonomy),
                view=fixture.view,
                catalog_path=Path(tmp) / "missing.jsonl",
            )
            with self.assertRaises(RuntimeError):
                service.classify_after_ingest("PI", disposition="NEW_PAPER")

            self.assertTrue(managed.is_file())
            self.assertEqual(managed.read_bytes(), before)

    def test_download_manager_reports_classification_without_failing_download(self) -> None:
        from hunnu_harness.literature.downloads import LiteratureDownloadManager

        with tempfile.TemporaryDirectory(prefix="cls-mgr-", dir=TEMP_DIR) as tmp:
            class _Broken:
                def classify_after_ingest(self, paper_id, *, disposition, dry_run=False):
                    raise RuntimeError("classification subsystem down")

            manager = LiteratureDownloadManager(
                Path(tmp) / "downloads",
                allow_outside_project_for_tests=True,
                topic_classifier=_Broken(),
            )
            reported = manager._classify_archived_work("PJ", "NEW_PAPER")
            self.assertEqual(reported["status"], "FAILED_SAFE")
            self.assertEqual(reported["assigned"], ())
            self.assertEqual(reported["primary"], "unknown")
            self.assertTrue(reported["review_required"])
            self.assertFalse(reported["metadata_updated"])
            self.assertFalse(reported["view_updated"])
            self.assertIn("classification failed", reported["reason"].casefold())
            # Readiness must not be invented when classification never ran.
            self.assertFalse(reported["nav_metadata_ready"])
            self.assertFalse(reported["nav_topic_ready"])
            self.assertEqual(reported["nav_fulltext_status"], "unknown")


class CanonicalLibraryBacktestTests(unittest.TestCase):
    """Calibration evidence, re-measured against the real frozen corpus.

    Reads the canonical topic store; writes nothing.  Skips cleanly where the
    real Library is not present.
    """

    def setUp(self) -> None:
        try:
            self.taxonomy = TopicTaxonomy.load()
        except TopicTaxonomyError:
            self.skipTest("real taxonomy is not present on this machine")
        self.rows = TopicStore(taxonomy=self.taxonomy).load()
        if len(self.rows) < 100:
            self.skipTest("real Global Paper Library is not present on this machine")

    def _measure(self) -> dict[str, float]:
        classifier = WorkClassifier(taxonomy=self.taxonomy)
        tp = fp = fn = 0
        auto = review = 0
        top1_hits = top1_total = 0
        for paper_id, row in self.rows.items():
            truth = {label.label for label in row.topics}
            result = classifier.classify(
                ClassificationInput(
                    paper_id=paper_id,
                    title=row.title or "",
                    keywords=(row.keywords or "").replace(";", " "),
                )
            )
            if result.status is not ClassificationStatus.CLASSIFIED:
                review += 1
                continue
            auto += 1
            predicted = set(result.assigned_labels)
            tp += len(predicted & truth)
            fp += len(predicted - truth)
            fn += len(truth - predicted)
            top1_total += 1
            top1_hits += 1 if result.assigned_labels[0] in truth else 0
        return {
            "precision": tp / (tp + fp) if tp + fp else 0.0,
            "recall": tp / (tp + fn) if tp + fn else 0.0,
            "top1": top1_hits / top1_total if top1_total else 0.0,
            "coverage": auto / len(self.rows),
            "review": review / len(self.rows),
        }

    def test_default_thresholds_hold_their_calibrated_precision(self) -> None:
        measured = self._measure()
        # The calibration run recorded precision 0.966 and top-1 1.000.  These
        # floors sit just below that, so ordinary lexicon growth is fine but a
        # real precision regression fails.
        self.assertGreaterEqual(measured["precision"], 0.93, measured)
        self.assertGreaterEqual(measured["top1"], 0.95, measured)

    def test_english_aliases_hold_their_own_calibration(self) -> None:
        """Per-language floors for the English alias table, plus stability.

        The alias calibration measured Chinese coverage 0.654 (bit-identical
        with the table on and off), English coverage 0.895, and precision 1.000
        for both languages over the 181-work corpus.  The floors sit below
        those numbers the same way the global precision floor does, and the
        run is repeated to prove the backtest is deterministic -- the numbers
        an alias edit is judged by must not wobble between runs.

        The coverage floors were recalibrated on 2026-09-24, from 0.60 and
        0.75, at the user's decision.  Over the 255-work corpus Chinese
        coverage measured 0.590 (128/217) and English 0.684 (26/38), with
        precision still 1.000 in both.  The classifier did not change: the
        works held before that date measure exactly as they did (0.610 and
        0.765).  Twenty-eight works had entered with a title and no keywords
        and had their topics confirmed by a person; from the title alone the
        classifier auto-files eight of them and sends the other twenty to
        review, as designed.  Like the corpus seal, moving a floor is a
        deliberate act recorded here, not a way to get a green suite.
        """

        from hunnu_harness.navigator.tokenize import contains_cjk

        classifier = WorkClassifier(taxonomy=self.taxonomy)

        def measure() -> dict[str, dict[str, float]]:
            buckets = {
                lang: {"works": 0, "auto": 0, "tp": 0, "fp": 0}
                for lang in ("cjk", "latin")
            }
            for paper_id, row in sorted(self.rows.items()):
                truth = {label.label for label in row.topics}
                result = classifier.classify(
                    ClassificationInput(
                        paper_id=paper_id,
                        title=row.title or "",
                        keywords=(row.keywords or "").replace(";", " "),
                    )
                )
                bucket = buckets["cjk" if contains_cjk(row.title or "") else "latin"]
                bucket["works"] += 1
                if not result.is_classified:
                    continue
                bucket["auto"] += 1
                predicted = set(result.assigned_labels)
                bucket["tp"] += len(predicted & truth)
                bucket["fp"] += len(predicted - truth)
            return buckets

        first = measure()
        self.assertEqual(first, measure(), "the backtest must be deterministic")

        for lang, coverage_floor in (("cjk", 0.55), ("latin", 0.65)):
            bucket = first[lang]
            self.assertGreaterEqual(
                bucket["auto"] / bucket["works"], coverage_floor, (lang, bucket)
            )
            assigned = bucket["tp"] + bucket["fp"]
            self.assertGreaterEqual(bucket["tp"] / assigned, 0.95, (lang, bucket))

    def test_review_absorbs_what_cannot_be_classified_confidently(self) -> None:
        measured = self._measure()
        self.assertGreater(measured["review"], 0.0, measured)
        self.assertAlmostEqual(measured["coverage"] + measured["review"], 1.0, places=6)

    def test_backtest_writes_nothing_to_the_canonical_store(self) -> None:
        store = TopicStore(taxonomy=self.taxonomy)
        before = store.jsonl_path.read_bytes()
        self._measure()
        self.assertEqual(store.jsonl_path.read_bytes(), before)


class SecondaryHardeningTests(unittest.TestCase):
    """A secondary needs corroboration from the taxonomy value own name.

    During calibration every secondary false positive came from the concept
    lexicon alone: one concept naming several sibling subtopics fires them all
    at the same score, so they tie with the leader and no margin ratio can tell
    them apart.
    """

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_high_confidence_primary_is_auto_assigned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-prim-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("SA", "AI漂洗与企业绩效", keywords="AI漂洗")
            result = fixture.classifier().classify_work("SA")
            self.assertTrue(result.is_classified)
            self.assertEqual(result.primary_topic, "01_人工智能与数字经济\\AI漂洗")
            self.assertIn(SIGNAL_TAXONOMY_NAME, result.assigned_topics[0].signals)

    def test_strongly_corroborated_secondary_is_auto_assigned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-sec-ok-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("SB", "AI漂洗与信息披露", keywords="AI漂洗;信息披露")
            result = fixture.classifier().classify_work("SB")
            self.assertTrue(result.is_classified)
            self.assertIn("04_会计审计与信息披露\信息披露", result.assigned_labels)
            for evidence in result.assigned_topics:
                self.assertTrue(evidence.corroborated, evidence.topic)

    def test_relaxing_the_gate_can_only_assign_more_never_fewer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-sec-prop-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("SC", "AI漂洗与信息披露", keywords="AI漂洗;信息披露")
            strict = fixture.classifier()
            relaxed = PostAcquisitionClassifier(
                store=fixture.store,
                taxonomy=fixture.taxonomy,
                classifier=WorkClassifier(
                    taxonomy=fixture.taxonomy,
                    require_taxonomy_name_for_secondary=False,
                ),
                view=fixture.view,
                catalog_path=Path(tmp) / "missing.jsonl",
            )
            strict_result = strict.classify_work("SC")
            relaxed_result = relaxed.classify_work("SC")
            self.assertGreaterEqual(
                len(relaxed_result.assigned_labels), len(strict_result.assigned_labels)
            )
            for evidence in strict_result.assigned_topics[1:]:
                self.assertIn(SIGNAL_TAXONOMY_NAME, evidence.signals)

    def test_uncorroborated_secondary_becomes_a_review_suggestion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-sugg-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("SD", "AI漂洗与信息披露", keywords="AI漂洗;信息披露")
            result = fixture.classifier().classify_work("SD")
            if result.status is ClassificationStatus.CLASSIFIED_WITH_REVIEW_SUGGESTIONS:
                self.assertTrue(result.proposed_labels)
                self.assertFalse(
                    set(result.proposed_labels) & set(result.assigned_labels)
                )

    def test_review_suggestions_do_not_mean_the_paper_is_unclassified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-notfail-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("SE", "AI漂洗与信息披露", keywords="AI漂洗;信息披露")
            result, outcome = fixture.classifier().apply_classification("SE")
            self.assertTrue(result.is_classified)
            self.assertNotEqual(result.status, ClassificationStatus.REVIEW_REQUIRED)
            self.assertFalse(result.review_required)
            self.assertTrue(outcome.topic_metadata_updated)
            self.assertTrue(fixture.store.topics_for("SE"))

    def test_proposed_topics_are_never_written_to_canonical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-noprop-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("SF", "AI漂洗与信息披露与绿色金融", keywords="AI漂洗;信息披露")
            result, _outcome = fixture.classifier().apply_classification("SF")
            written = {label.label for label in fixture.store.topics_for("SF")}
            self.assertEqual(written, set(result.assigned_labels))
            self.assertFalse(written & set(result.proposed_labels))

    def test_secondary_gate_creates_no_new_taxonomy_value(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-notax-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            known = {label.label for label in fixture.taxonomy.labels()}
            fixture.add_work("SG", "AI漂洗、信息披露与绿色金融与绿色创新")
            result = fixture.classifier().classify_work("SG")
            for label in result.assigned_labels + result.proposed_labels:
                self.assertIn(label, known)

    def test_hardened_secondary_stays_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-sec-idem-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("SH", "AI漂洗与信息披露", keywords="AI漂洗;信息披露")
            service = fixture.classifier()
            service.apply_classification("SH")
            first = fixture.store.jsonl_path.read_bytes()
            _result, outcome = service.apply_classification("SH", force=True)
            self.assertEqual(fixture.store.jsonl_path.read_bytes(), first)
            self.assertEqual(outcome.links_created, ())

    def test_existing_work_topics_survive_the_hardened_gate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-sec-keep-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work(
                "SI",
                "AI漂洗与信息披露",
                topics="09_ESG与绿色发展\绿色金融与绿色创新",
            )
            before = fixture.store.jsonl_path.read_bytes()
            result, _ = fixture.classifier().classify_after_ingest(
                "SI", disposition="SAME_WORK_DIFFERENT_VERSION"
            )
            self.assertEqual(result.status, ClassificationStatus.SKIPPED_EXISTING)
            self.assertEqual(fixture.store.jsonl_path.read_bytes(), before)


class NavigatorReadinessTests(unittest.TestCase):
    """Readiness is reported per capability, never as one collapsed flag."""

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_readiness_reports_three_independent_capabilities(self) -> None:
        readiness = NavigatorReadiness(
            metadata_ready=True, topic_ready=True, fulltext_index_status="STALE"
        )
        payload = readiness.as_dict()
        self.assertEqual(payload["NavigatorMetadataReady"], True)
        self.assertEqual(payload["NavigatorTopicReady"], True)
        self.assertEqual(payload["NavigatorFulltextIndexStatus"], "STALE")
        self.assertNotIn("NavigatorReady", payload)

    def test_metadata_and_topic_ready_do_not_imply_a_fresh_index(self) -> None:
        readiness = NavigatorReadiness(
            metadata_ready=True, topic_ready=True, fulltext_index_status="STALE"
        )
        self.assertTrue(readiness.metadata_ready and readiness.topic_ready)
        self.assertNotEqual(readiness.fulltext_index_status, "FRESH")

    def test_index_status_uses_navigator_status_values_only(self) -> None:
        from hunnu_harness.navigator.index import IndexStatus

        allowed = {status.value for status in IndexStatus}
        self.assertEqual(allowed, {"FRESH", "STALE", "ABSENT", "UNREADABLE"})
        self.assertNotIn("READY", allowed)

    def test_manifest_exposes_split_readiness_fields(self) -> None:
        payload = _manifest_entry().as_dict()
        for field_name in (
            "NavigatorMetadataReady",
            "NavigatorTopicReady",
            "NavigatorFulltextIndexStatus",
            "AssignedPrimaryTopic",
            "AssignedSecondaryTopics",
            "ProposedTopics",
        ):
            self.assertIn(field_name, payload)
        self.assertNotIn("NavigatorReady", payload)

    def test_work_missing_from_the_index_is_reported_stale_not_fresh(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-nav-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("NV", "AI漂洗与信息披露")
            readiness = fixture.classifier().navigator_readiness("NV")
            self.assertFalse(readiness.fulltext_index_covers_work)
            self.assertNotEqual(readiness.fulltext_index_status, "FRESH")

    def test_readiness_never_raises_when_the_index_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cls-nav2-", dir=TEMP_DIR) as tmp:
            fixture = _Fixture(Path(tmp))
            fixture.add_work("NW", "AI漂洗")
            readiness = fixture.classifier().navigator_readiness("NW")
            self.assertIsInstance(readiness, NavigatorReadiness)
            self.assertIn(
                readiness.fulltext_index_status,
                {"FRESH", "STALE", "ABSENT", "UNREADABLE", "unknown"},
            )


if __name__ == "__main__":
    unittest.main()
