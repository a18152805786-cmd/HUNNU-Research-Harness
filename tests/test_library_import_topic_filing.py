"""Topic filing on the manual import path.

A PDF that enters the Library through ``library-stage`` / ``library-import``
is an acquisition like any other (AGENTS.md 69), yet the importer ran no
classification and its result said nothing about topics.  A WORK came out
``MANAGED`` with no topic at all, while ``library-confirm-topics`` -- keyed on
the classifier's regenerated verdict rather than on the WORK's own state --
refused it as "not awaiting review" because the classifier happened to be
confident.  That WORK had no sanctioned way to be filed.

These tests pin both halves of the loop: the import files the WORK or hands
it over with its proposals, the result says which, and the handover is
reachable through the confirmation command.

Every test runs on an isolated library, taxonomy, topic store, view and
provenance file under TEMP_DIR.  The real Library is never written; the one
subprocess test points ``HUNNU_HARNESS_OUTPUT_ROOT`` at a throwaway root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.auto_classification import PostAcquisitionClassifier
from hunnu_harness.literature.classification import ClassificationStatus, WorkClassifier
from hunnu_harness.literature.library import (
    TOPIC_FILING_NO_CLASSIFIER,
    TOPIC_FILING_NOT_ATTEMPTED,
    ExternalPaperImporter,
    GlobalPaperLibrary,
    LibraryDisposition,
    LibraryIngestResult,
)
from hunnu_harness.literature.topic_confirmation import (
    AssignmentSource,
    ConfirmationStatus,
    TopicProvenanceStore,
)
from hunnu_harness.literature.topics import TopicStore, TopicTaxonomy, TopicViewBuilder
from hunnu_harness.paths import LIBRARY_ROOT, TEMP_DIR

from literature_test_support import write_minimal_pdf_with_text

TAXONOMY = {
    "domains": {
        "01_人工智能与数字经济": ["AI漂洗", "数字化转型"],
        "04_会计审计与信息披露": ["信息披露"],
        "09_ESG与绿色发展": ["绿色金融与绿色创新"],
    },
    "generated_at": "2026-08-30T00:00:00+00:00",
}

AI_WASHING = "01_人工智能与数字经济" + chr(92) + "AI漂洗"
DISCLOSURE = "04_会计审计与信息披露" + chr(92) + "信息披露"
GREEN = "09_ESG与绿色发展" + chr(92) + "绿色金融与绿色创新"

# An English alias of a subtopic name corroborates the lexicon signal, so this
# title clears the auto-assign gate on its own.
CONFIDENT_TITLE = "AI washing and firm innovation"
# One lexicon-only signal: proposals, but nothing clears the gate.
REVIEW_TITLE = "The talk-walk gap and information asymmetry in technology narratives"
# Nothing in the taxonomy is named or implied here.
UNPLACEABLE_TITLE = "A study of nineteenth century maritime insurance"

_FILED = {
    ClassificationStatus.CLASSIFIED.value,
    ClassificationStatus.CLASSIFIED_WITH_REVIEW_SUGGESTIONS.value,
}


class _ImportFixture:
    """An isolated library with the classifier the real importer would attach."""

    def __init__(self, root: Path) -> None:
        self.root = root
        taxonomy_path = root / "topic_taxonomy.json"
        taxonomy_path.write_text(json.dumps(TAXONOMY, ensure_ascii=False), encoding="utf-8")
        self.taxonomy = TopicTaxonomy.load(taxonomy_path)
        self.library = GlobalPaperLibrary(
            root / "library",
            allow_outside_output_for_tests=True,
            make_managed_read_only=False,
        )
        self.store = TopicStore(
            jsonl_path=self.library.catalog_dir / "paper_topics.jsonl",
            csv_path=self.library.catalog_dir / "paper_topics.csv",
            taxonomy=self.taxonomy,
        )
        self.view = TopicViewBuilder(
            view_root=root / "papers_by_topic", papers_dir=self.library.papers_dir
        )
        self.provenance = TopicProvenanceStore(
            path=self.library.library_root / "topic_assignment_provenance.jsonl"
        )
        self.classifier = PostAcquisitionClassifier(
            store=self.store,
            taxonomy=self.taxonomy,
            classifier=WorkClassifier(taxonomy=self.taxonomy),
            view=self.view,
            catalog_path=self.library.catalog_jsonl_path,
            provenance=self.provenance,
        )
        self.importer = ExternalPaperImporter(self.library, topic_classifier=self.classifier)

    @staticmethod
    def metadata(title: str) -> dict[str, object]:
        return {
            "Title": title,
            "Authors": ["Ada Author"],
            "Year": "2026",
            "Journal": "Journal of Topic Filing",
            "SourceLocator": "SyntheticExternalInventory",
        }

    def import_pdf(
        self,
        title: str,
        *,
        text: str | None = None,
        name: str = "candidate.pdf",
        importer: ExternalPaperImporter | None = None,
    ) -> LibraryIngestResult:
        source = write_minimal_pdf_with_text(self.root / name, text or title)
        active = importer or self.importer
        staged = active.stage_pdf(source)
        return active.import_staged_pdf(staged.staged_path, self.metadata(title))


class ImportFilesTheWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_a_confident_import_is_filed_and_the_result_says_so(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-filed-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            result = fixture.import_pdf(CONFIDENT_TITLE)

            self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
            self.assertIn(result.classification_status, _FILED)
            self.assertIn(AI_WASHING, result.assigned_topics)
            self.assertEqual(result.assigned_primary_topic, result.assigned_topics[0])
            self.assertFalse(result.topic_review_required)
            self.assertTrue(result.topic_metadata_updated)
            self.assertEqual(
                [label.label for label in fixture.store.topics_for(result.paper_id)],
                list(result.assigned_topics),
            )

    def test_a_filed_import_records_that_the_harness_decided(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-prov-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            result = fixture.import_pdf(CONFIDENT_TITLE)

            record = fixture.provenance.latest_for(result.paper_id)
            self.assertIsNotNone(record)
            self.assertEqual(record["assignment_source"], AssignmentSource.AUTO_CLASSIFIED.value)
            self.assertEqual(record["topics"], list(result.assigned_topics))

    def test_a_filed_import_hardlinks_the_view_to_the_managed_file(self) -> None:
        """The catalog path is Output-Root-relative; the view must resolve it
        against the root that owns *this* catalog, or the entry is recorded as
        unavailable and the paper is filed with no readable file behind it."""

        with tempfile.TemporaryDirectory(prefix="imp-view-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            result = fixture.import_pdf(CONFIDENT_TITLE)

            self.assertTrue(result.topic_view_updated)
            linked = list(fixture.view.view_root.rglob("*.pdf"))
            self.assertTrue(linked)
            for path in linked:
                self.assertEqual(path.stat().st_ino, result.managed_path.stat().st_ino)

    def test_a_filed_import_reports_navigator_readiness_of_its_own_library(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-nav-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            result = fixture.import_pdf(CONFIDENT_TITLE)

            self.assertTrue(result.navigator_metadata_ready)
            self.assertTrue(result.navigator_topic_ready)
            self.assertIn(
                result.navigator_fulltext_index_status,
                {"FRESH", "STALE", "ABSENT", "UNREADABLE"},
            )

    def test_the_result_carries_the_manifest_field_set(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-fields-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            payload = fixture.import_pdf(CONFIDENT_TITLE).as_dict()
            for field_name in (
                "ClassificationStatus",
                "AssignedTopics",
                "AssignedPrimaryTopic",
                "AssignedSecondaryTopics",
                "ProposedTopics",
                "TopicReviewRequired",
                "TopicMetadataUpdated",
                "TopicViewUpdated",
                "ClassificationReason",
                "NavigatorMetadataReady",
                "NavigatorTopicReady",
                "NavigatorFulltextIndexStatus",
            ):
                self.assertIn(field_name, payload)


class ImportHandsOverToReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_an_import_below_the_gate_lands_in_review_with_proposals(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-review-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            result = fixture.import_pdf(REVIEW_TITLE)

            self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
            self.assertEqual(
                result.classification_status, ClassificationStatus.REVIEW_REQUIRED.value
            )
            self.assertTrue(result.topic_review_required)
            self.assertIn(AI_WASHING, result.proposed_topics)
            self.assertEqual(result.assigned_topics, ())
            self.assertFalse(result.topic_metadata_updated)
            self.assertEqual(fixture.store.topics_for(result.paper_id), ())

    def test_the_handover_is_reachable_through_confirmation(self) -> None:
        """The whole loop through sanctioned operations: import, then confirm."""

        with tempfile.TemporaryDirectory(prefix="imp-loop-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            result = fixture.import_pdf(REVIEW_TITLE)
            chosen = [topic for topic in result.proposed_topics if topic == AI_WASHING]
            self.assertEqual(chosen, [AI_WASHING])

            confirmed = fixture.classifier.confirm_topics(result.paper_id, chosen)

            self.assertEqual(confirmed.status, ConfirmationStatus.CONFIRMED)
            self.assertEqual(
                [label.label for label in fixture.store.topics_for(result.paper_id)],
                [AI_WASHING],
            )
            self.assertEqual(
                fixture.provenance.latest_for(result.paper_id)["assignment_source"],
                AssignmentSource.HUMAN_CONFIRMED.value,
            )

    def test_an_unplaceable_import_is_still_archived_and_still_reachable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-none-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            result = fixture.import_pdf(UNPLACEABLE_TITLE)

            self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
            self.assertTrue(result.managed_path.is_file())
            self.assertEqual(
                result.classification_status, ClassificationStatus.REVIEW_REQUIRED.value
            )
            self.assertTrue(result.topic_review_required)
            # Nothing was raised, so only the override can file it -- but it
            # can be filed.
            refused = fixture.classifier.confirm_topics(result.paper_id, [GREEN])
            self.assertEqual(refused.status, ConfirmationStatus.NO_CONFIRMABLE_PROPOSALS)
            self.assertEqual(fixture.store.topics_for(result.paper_id), ())


class ImportWithoutAClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_an_importer_without_a_classifier_says_it_filed_nothing(self) -> None:
        """Silence was the defect: a MANAGED WORK with no topic and no field
        on the result admitting it."""

        with tempfile.TemporaryDirectory(prefix="imp-nocls-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            bare = ExternalPaperImporter(fixture.library, topic_classifier=None)
            result = fixture.import_pdf(CONFIDENT_TITLE, importer=bare)

            self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
            self.assertEqual(result.classification_status, TOPIC_FILING_NOT_ATTEMPTED)
            self.assertEqual(result.classification_reason, TOPIC_FILING_NO_CLASSIFIER)
            self.assertTrue(result.topic_review_required)
            self.assertEqual(result.assigned_topics, ())
            self.assertEqual(fixture.store.topics_for(result.paper_id), ())

    def test_an_isolated_library_gets_no_classifier_by_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-default-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            importer = ExternalPaperImporter(fixture.library)
            self.assertIsNone(importer.topic_classifier)
            self.assertFalse(ExternalPaperImporter.classifies_by_default(fixture.library.library_root))

    def test_only_the_real_library_root_attaches_the_default_classifier(self) -> None:
        self.assertTrue(ExternalPaperImporter.classifies_by_default(LIBRARY_ROOT))
        self.assertTrue(ExternalPaperImporter.classifies_by_default(Path(str(LIBRARY_ROOT))))
        self.assertFalse(ExternalPaperImporter.classifies_by_default(LIBRARY_ROOT / "nested"))
        self.assertFalse(ExternalPaperImporter.classifies_by_default(TEMP_DIR / "library"))

    def test_an_explicit_none_is_never_replaced_by_the_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-none2-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            importer = ExternalPaperImporter(fixture.library, topic_classifier=None)
            self.assertIsNone(importer.topic_classifier)
            self.assertIsNone(importer.topic_classifier)


class ImportSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_a_classification_failure_never_fails_the_import(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-broken-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))

            class _Broken:
                def classify_after_ingest(self, paper_id, *, disposition, dry_run=False):
                    raise RuntimeError("classification subsystem down")

            broken = ExternalPaperImporter(fixture.library, topic_classifier=_Broken())
            result = fixture.import_pdf(CONFIDENT_TITLE, importer=broken)

            self.assertEqual(result.disposition, LibraryDisposition.NEW_PAPER)
            self.assertEqual(result.status, "MANAGED")
            self.assertTrue(result.managed_path.is_file())
            self.assertEqual(
                result.classification_status, ClassificationStatus.FAILED_SAFE.value
            )
            self.assertTrue(result.topic_review_required)
            self.assertIn("classification failed", result.classification_reason.casefold())
            # Readiness must not be invented when classification never ran.
            self.assertFalse(result.navigator_topic_ready)

    def test_a_second_version_of_a_filed_work_reuses_its_topics(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-version-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            first = fixture.import_pdf(CONFIDENT_TITLE)
            self.assertIn(first.classification_status, _FILED)

            second = fixture.import_pdf(
                CONFIDENT_TITLE,
                text=f"{CONFIDENT_TITLE} -- second version, different bytes",
                name="candidate-v2.pdf",
            )

            self.assertEqual(second.disposition, LibraryDisposition.SAME_WORK_DIFFERENT_VERSION)
            self.assertEqual(second.paper_id, first.paper_id)
            self.assertEqual(
                second.classification_status, ClassificationStatus.SKIPPED_EXISTING.value
            )
            self.assertEqual(second.assigned_topics, first.assigned_topics)
            self.assertFalse(second.topic_review_required)
            self.assertFalse(second.topic_metadata_updated)
            self.assertEqual(len(fixture.provenance.load()), 1)

    def test_a_rejected_import_reports_no_topic_filing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-reject-", dir=TEMP_DIR) as tmp:
            fixture = _ImportFixture(Path(tmp))
            result = fixture.import_pdf(
                "Corporate tax avoidance and audit quality", text=CONFIDENT_TITLE
            )

            self.assertEqual(result.disposition, LibraryDisposition.IDENTITY_CONFLICT)
            self.assertEqual(result.classification_status, "unknown")
            self.assertFalse(result.topic_review_required)
            self.assertEqual(fixture.store.load(), {})


class CommandLineImportTests(unittest.TestCase):
    """The shipped command, end to end, against a throwaway Output Root.

    The defect was at exactly this level: ``library-import`` on the real
    Library attached no classifier and printed nothing about topics.  With a
    taxonomy present under the redirected root, the default importer must
    file the WORK and say so.
    """

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def _run(self, root: Path, *arguments: str) -> dict:
        env = dict(os.environ)
        env["HUNNU_HARNESS_OUTPUT_ROOT"] = str(root)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "hunnu_harness.cli", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_library_import_files_the_work_and_reports_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imp-cli-", dir=TEMP_DIR) as tmp:
            root = Path(tmp)
            (root / "topic_taxonomy.json").write_text(
                json.dumps(TAXONOMY, ensure_ascii=False), encoding="utf-8"
            )
            source = write_minimal_pdf_with_text(root / "candidate.pdf", CONFIDENT_TITLE)
            metadata_path = root / "metadata.json"
            metadata_path.write_text(
                json.dumps(_ImportFixture.metadata(CONFIDENT_TITLE)), encoding="utf-8"
            )

            staged = self._run(root, "library-stage", "--source", str(source))
            imported = self._run(
                root,
                "library-import",
                "--source",
                staged["StagedPath"],
                "--metadata-json",
                str(metadata_path),
            )

            self.assertEqual(imported["Disposition"], "NEW_PAPER")
            self.assertEqual(imported["Status"], "MANAGED")
            self.assertIn(imported["ClassificationStatus"], _FILED)
            self.assertIn(AI_WASHING, imported["AssignedTopics"])
            self.assertFalse(imported["TopicReviewRequired"])
            self.assertTrue(imported["TopicMetadataUpdated"])
            topics_file = root / "library" / "catalog" / "paper_topics.jsonl"
            self.assertIn(imported["PaperID"], topics_file.read_text(encoding="utf-8"))
            provenance = root / "library" / "topic_assignment_provenance.jsonl"
            self.assertIn(AssignmentSource.AUTO_CLASSIFIED.value, provenance.read_text(encoding="utf-8"))

    def test_library_import_without_a_taxonomy_fails_safe_out_loud(self) -> None:
        """A missing taxonomy is a classification failure, reported as one --
        the import still succeeds and the result still says the WORK is
        unfiled, instead of saying nothing."""

        with tempfile.TemporaryDirectory(prefix="imp-cli-notax-", dir=TEMP_DIR) as tmp:
            root = Path(tmp)
            source = write_minimal_pdf_with_text(root / "candidate.pdf", CONFIDENT_TITLE)
            metadata_path = root / "metadata.json"
            metadata_path.write_text(
                json.dumps(_ImportFixture.metadata(CONFIDENT_TITLE)), encoding="utf-8"
            )

            staged = self._run(root, "library-stage", "--source", str(source))
            imported = self._run(
                root,
                "library-import",
                "--source",
                staged["StagedPath"],
                "--metadata-json",
                str(metadata_path),
            )

            self.assertEqual(imported["Disposition"], "NEW_PAPER")
            self.assertEqual(imported["ClassificationStatus"], "FAILED_SAFE")
            self.assertTrue(imported["TopicReviewRequired"])
            self.assertIn("classification failed", imported["ClassificationReason"].casefold())
            self.assertTrue(Path(imported["ManagedPath"]).is_file())


if __name__ == "__main__":
    unittest.main()
