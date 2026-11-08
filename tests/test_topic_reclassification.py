"""Audited replacement is separate from confirming an unfiled work.

All files are invented fixtures in Output/temp. No real library or PDF is read.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from hunnu_harness.literature.auto_classification import PostAcquisitionClassifier
from hunnu_harness.literature.classification import ClassificationInput, ClassificationStatus, WorkClassifier
from hunnu_harness.literature.topic_confirmation import AssignmentSource, TopicProvenanceStore
from hunnu_harness.literature.topic_reclassification import TopicReclassifier
from hunnu_harness.literature.topics import TopicStore, TopicTaxonomy, TopicViewBuilder, WorkTopicRow, parse_topic_label
from hunnu_harness.paths import TEMP_DIR

AI = "01_数字技术与创新\\人工智能与机器人"
METHOD = "08_研究方法与综合管理\\计量方法与研究设计"
DML = "Double/debiased machine learning for treatment and structural parameters"
DOMAINS = {"01_数字技术与创新": ["人工智能与机器人"], "08_研究方法与综合管理": ["计量方法与研究设计"]}


@pytest.fixture
def library():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reclassification-", dir=TEMP_DIR) as tmp:
        root = Path(tmp)
        taxonomy_path = root / "topic_taxonomy.json"
        taxonomy_path.write_text(json.dumps({"domains": DOMAINS}, ensure_ascii=False), encoding="utf-8")
        taxonomy = TopicTaxonomy.load(taxonomy_path)
        papers = root / "library/papers"
        papers.mkdir(parents=True)
        pdf = papers / "PTEST.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfixture never parsed\n%%EOF\n")
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        catalog = root / "library/catalog/papers.jsonl"
        catalog.parent.mkdir()
        catalog.write_text(json.dumps({"paper_id": "PTEST", "title": DML,
            "journal": "The Econometrics Journal", "sha256": sha, "status": "MANAGED",
            "managed_pdf_path": "library/papers/PTEST.pdf", "managed_fulltext_path": "library/papers/PTEST.pdf"}) + "\n", encoding="utf-8")
        store = TopicStore(jsonl_path=catalog.parent / "paper_topics.jsonl", csv_path=catalog.parent / "paper_topics.csv", taxonomy=taxonomy)
        row = WorkTopicRow(paper_id="PTEST", title=DML, topics=(parse_topic_label(AI),), human_readable_name="PTEST.pdf", canonical_path=str(pdf), canonical_sha256=sha)
        store.commit({"PTEST": row})
        view = TopicViewBuilder(view_root=root / "papers_by_topic", papers_dir=papers)
        view.sync_work(row)
        view.write_links_manifest(store.load())
        provenance = TopicProvenanceStore(path=root / "library/topic_assignment_provenance.jsonl")
        provenance.record(paper_id="PTEST", topics=[AI], source=AssignmentSource.AUTO_CLASSIFIED, classification_status_before="CLASSIFIED")
        service = PostAcquisitionClassifier(store=store, taxonomy=taxonomy, view=view, catalog_path=catalog, provenance=provenance)
        yield root, service, TopicReclassifier(service)


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and "temp" not in p.relative_to(root).parts}


@pytest.mark.parametrize("title", [DML, "Double machine learning for treatment effects", "Debiased machine learning for semiparametric inference", "双重机器学习估计与推断"])
def test_explicit_method_titles_file_under_methods_not_ai(title):
    classifier = WorkClassifier(taxonomy=TopicTaxonomy.from_mapping({"domains": DOMAINS}))
    result = classifier.classify(ClassificationInput(paper_id="PX", title=title))
    assert result.status is ClassificationStatus.CLASSIFIED
    assert result.assigned_labels == (METHOD,)
    assert "METHOD_CONTEXT" in result.assigned_topics[0].signals
    assert result.assigned_topics[0].fields == ("title",)


@pytest.mark.parametrize("title", [
    "Machine learning and corporate investment",
    "Artificial intelligence adoption: evidence from double machine learning",
    "Double machine learning for treatment effects of digital transformation",
    "Digital transformation and productivity: a double machine learning approach",
    "Machine learning for credit risk prediction",
    "双重机器学习视角下人工智能对企业创新的影响",
])
def test_application_titles_do_not_earn_method_title_override(title):
    classifier = WorkClassifier(taxonomy=TopicTaxonomy.from_mapping({"domains": DOMAINS}))
    result = classifier.classify(ClassificationInput(paper_id="PX", title=title))
    assert METHOD not in result.assigned_labels
    assert all("METHOD_CONTEXT" not in item.signals for item in (*result.assigned_topics, *result.proposed_topics))


def test_method_phrase_only_in_body_cannot_redirect_identity():
    result = WorkClassifier(taxonomy=TopicTaxonomy.from_mapping({"domains": DOMAINS})).classify(
        ClassificationInput(paper_id="PX", title="Machine learning and corporate investment", fulltext=DML * 20))
    assert METHOD not in result.assigned_labels
    assert AI in result.assigned_labels


def test_missing_method_taxonomy_never_invents_the_label():
    result = WorkClassifier(taxonomy=TopicTaxonomy.from_mapping({"domains": {"D": ["人工智能与机器人"]}})).classify(
        ClassificationInput(paper_id="PX", title=DML))
    assert not result.assigned_labels
    assert result.status is ClassificationStatus.REVIEW_REQUIRED


def test_default_preview_changes_no_files_and_exposes_old_new_source(library):
    root, _service, api = library
    before = snapshot(root)
    result = api.reclassify("PTEST", reason="Correct a methodological title misfiled as AI")
    assert result.status == "DRY_RUN"
    assert result.old_topics == (AI,) and result.new_topics == (METHOD,)
    assert result.previous_assignment_source == "AUTO_CLASSIFIED"
    assert len(result.current_fingerprint) == 64
    assert not result.applied
    assert snapshot(root) == before


@pytest.mark.parametrize("args,status", [
    ({"reason": ""}, "REASON_REQUIRED"),
    ({"reason": "review", "dry_run": False}, "PRECONDITION_REQUIRED"),
    ({"reason": "review", "dry_run": False, "expected_current_topics": [METHOD]}, "PRECONDITION_CONFLICT"),
    ({"reason": "review", "dry_run": False, "expected_fingerprint": "0" * 64}, "PRECONDITION_CONFLICT"),
])
def test_rejected_apply_preserves_metadata_and_pdf(library, args, status):
    root, _service, api = library
    before = snapshot(root)
    assert api.reclassify("PTEST", **args).status == status
    assert snapshot(root) == before


def test_apply_changes_canonical_view_and_audit_but_not_pdf_taxonomy_or_catalog(library):
    root, service, api = library
    before = snapshot(root)
    preview = api.reclassify("PTEST", reason="Fix method context")
    result = api.reclassify("PTEST", reason="Fix method context", expected_fingerprint=preview.current_fingerprint, dry_run=False)
    assert result.status == "RECLASSIFIED" and result.applied
    assert tuple(label.label for label in service.store.topics_for("PTEST")) == (METHOD,)
    old_view = root / "papers_by_topic/01_数字技术与创新/人工智能与机器人/PTEST.pdf"
    new_view = root / "papers_by_topic/08_研究方法与综合管理/计量方法与研究设计/PTEST.pdf"
    assert not old_view.exists()
    assert os.path.samefile(new_view, root / "library/papers/PTEST.pdf")
    for name in ("topic_taxonomy.json", "library/papers/PTEST.pdf", "library/catalog/papers.jsonl"):
        assert (root / name).read_bytes() == before[name.replace("/", os.sep)]
    record = service.provenance.latest_for("PTEST")
    assert record["change_kind"] == "TOPIC_RECLASSIFIED"
    assert record["previous_topics"] == [AI] and record["topics"] == [METHOD]
    assert record["previous_assignment_source"] == record["assignment_source"] == "AUTO_CLASSIFIED"
    assert record["reason"] == "Fix method context" and record["classifier_version"]
    assert record["expected_current_fingerprint"] == preview.current_fingerprint
    assert result.as_dict()["NavigatorTopicReady"]
    assert result.as_dict()["NavigatorFulltextIndexStatus"] == "ABSENT"
    assert not list((root / "temp/topic-reclassification").iterdir())


def test_reclassification_invalidates_existing_derived_index_without_rewriting_it(library):
    root, service, api = library
    index_manifest = root / "paper_retrieval/index/manifest.json"
    index_manifest.parent.mkdir(parents=True)
    index_manifest.write_text(json.dumps({"catalog_sha256": hashlib.sha256(service.catalog_path.read_bytes()).hexdigest(),
        "topics_sha256": hashlib.sha256(service.store.jsonl_path.read_bytes()).hexdigest(), "work_count": 1}), encoding="utf-8")
    before = index_manifest.read_bytes()
    assert api.reclassify("PTEST", reason="review").as_dict()["NavigatorFulltextIndexStatus"] == "FRESH"
    result = api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False)
    assert result.status == "RECLASSIFIED"
    assert result.as_dict()["NavigatorFulltextIndexStatus"] == "STALE"
    assert index_manifest.read_bytes() == before


def test_changed_provenance_rejects_old_preview_even_with_same_topics(library):
    root, service, api = library
    old = api.reclassify("PTEST", reason="review")
    service.provenance.record(paper_id="PTEST", topics=[AI], source=AssignmentSource.HUMAN_CONFIRMED, classification_status_before="REVIEW_REQUIRED")
    before = snapshot(root)
    result = api.reclassify("PTEST", reason="review", expected_fingerprint=old.current_fingerprint, dry_run=False)
    assert result.status == "PRECONDITION_CONFLICT"
    assert snapshot(root) == before


def test_human_decision_needs_exact_targeted_authorization(library):
    root, service, api = library
    service.provenance.record(paper_id="PTEST", topics=[AI], source=AssignmentSource.HUMAN_CONFIRMED, classification_status_before="REVIEW_REQUIRED")
    preview = api.reclassify("PTEST", reason="Review a prior human decision")
    before = snapshot(root)
    for auth in (None, "yes", "0" * 64):
        result = api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False, authorize_human_confirmed_fingerprint=auth)
        assert result.status == "HUMAN_AUTHORIZATION_REQUIRED"
        assert snapshot(root) == before
    result = api.reclassify("PTEST", reason="Specifically authorized method correction", expected_fingerprint=preview.current_fingerprint,
        dry_run=False, authorize_human_confirmed_fingerprint=preview.current_fingerprint)
    assert result.status == "RECLASSIFIED"
    assert service.provenance.latest_for("PTEST")["previous_assignment_source"] == "HUMAN_CONFIRMED"


@pytest.mark.parametrize("record", [None, {"assignment_source": "UNKNOWN_LEGACY", "topics": [AI]}, {"assignment_source": "AUTO_CLASSIFIED", "topics": [METHOD]}])
def test_unknown_or_conflicting_provenance_is_never_guessed_automatic(library, record):
    root, service, api = library
    service.provenance.path.unlink()
    if record is not None:
        service.provenance.append({"paper_id": "PTEST", **record})
    before = snapshot(root)
    assert api.reclassify("PTEST", reason="review").status == "DRY_RUN"
    assert api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False).status == "UNKNOWN_OR_INCONSISTENT_PROVENANCE"
    assert snapshot(root) == before


@pytest.mark.parametrize("failure_point", ["csv", "view_manifest", "provenance_after_replace"])
def test_partial_write_failures_restore_exact_metadata_and_original_hardlinks(library, failure_point):
    root, service, api = library
    before = snapshot(root)
    real_append = service.provenance.append
    def failed_append(entry):
        real_append(entry)
        raise OSError("injected failure after provenance replacement")
    if failure_point == "csv":
        target = patch("hunnu_harness.literature.topics._atomic_csv", side_effect=OSError("injected CSV failure"))
    elif failure_point == "view_manifest":
        target = patch.object(service.view, "write_links_manifest", side_effect=OSError("injected links manifest failure"))
    else:
        target = patch.object(service.provenance, "append", side_effect=failed_append)
    with target:
        result = api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False)
    assert result.status == "FAILED_SAFE" and result.rollback_completed
    assert snapshot(root) == before
    old_view = root / "papers_by_topic/01_数字技术与创新/人工智能与机器人/PTEST.pdf"
    assert os.path.samefile(old_view, root / "library/papers/PTEST.pdf")


def test_failed_new_hardlink_does_not_leave_new_metadata(library):
    root, _service, api = library
    before = snapshot(root)
    real_link = os.link
    def conditional_link(source, destination, *args, **kwargs):
        if "计量方法与研究设计" in str(destination):
            raise OSError("injected new link failure")
        return real_link(source, destination, *args, **kwargs)
    with patch("os.link", side_effect=conditional_link):
        result = api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False)
    assert result.status == "FAILED_SAFE" and result.rollback_completed
    assert snapshot(root) == before


def test_existing_different_file_in_view_is_preserved_and_refused(library):
    root, _service, api = library
    conflict = root / "papers_by_topic/08_研究方法与综合管理/计量方法与研究设计/PTEST.pdf"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"belongs to somebody else")
    before = snapshot(root)
    assert api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False).status == "VIEW_CONFLICT"
    assert snapshot(root) == before


def test_concurrent_topic_writer_refuses_apply_without_wait_or_mutation(library):
    root, service, api = library
    before = snapshot(root)
    results = []
    with service.store.mutation_lock():
        worker = threading.Thread(target=lambda: results.append(api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False)))
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
    assert results[0].status == "FAILED_SAFE" and "TOPIC_MUTATION_BUSY" in results[0].reason
    assert snapshot(root) == before


def test_old_confirmation_guard_still_rejects_settled_replacement(library):
    root, service, _api = library
    before = snapshot(root)
    result = service.confirm_topics("PTEST", [METHOD], allow_taxonomy_override=True)
    assert not result.succeeded
    assert snapshot(root) == before


def test_changed_frozen_configuration_invalidates_preview_and_cached_service(library):
    root, _service, api = library
    preview = api.reclassify("PTEST", reason="review")
    path = root / "topic_taxonomy.json"
    path.write_text(json.dumps({"domains": {**DOMAINS, "09_Extra": ["Other"]}}, ensure_ascii=False), encoding="utf-8")
    before = snapshot(root)
    result = api.reclassify("PTEST", reason="review", expected_fingerprint=preview.current_fingerprint, dry_run=False)
    assert result.status == "TAXONOMY_CHANGED"
    assert snapshot(root) == before


def test_corrupt_provenance_blocks_apply_without_discarding_unreadable_records(library):
    root, service, api = library
    with service.provenance.path.open("a", encoding="utf-8") as handle:
        handle.write("not valid JSON\n")
    before = snapshot(root)
    result = api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False)
    assert result.status == "PROVENANCE_UNREADABLE"
    assert snapshot(root) == before


def test_canonical_file_changed_refuses_metadata_reclassification(library):
    root, _service, api = library
    (root / "library/papers/PTEST.pdf").write_bytes(b"changed PDF identity")
    before = snapshot(root)
    assert api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False).status == "MANAGED_IDENTITY_UNVERIFIED"
    assert snapshot(root) == before


def test_service_cannot_partly_redirect_into_another_library(library):
    root, service, api = library
    service.provenance = TopicProvenanceStore(path=root / "elsewhere.jsonl")
    before = snapshot(root)
    assert api.reclassify("PTEST", reason="review").status == "LAYOUT_MISMATCH"
    assert snapshot(root) == before


def test_noop_reclassification_keeps_existing_audit_bytes(library):
    root, service, api = library
    first = api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False)
    assert first.status == "RECLASSIFIED"
    before = snapshot(root)
    second = api.reclassify("PTEST", reason="Already fixed", expected_current_topics=[METHOD], dry_run=False)
    assert second.status == "ALREADY_CURRENT" and not second.applied
    assert snapshot(root) == before
    assert len(service.provenance.load()) == 2


def test_rollback_failure_retains_metadata_preimages_and_reports_recovery_required(library):
    root, service, api = library
    with patch.object(service.provenance, "append", side_effect=OSError("write failed")), \
         patch("hunnu_harness.literature.topic_reclassification._restore", side_effect=OSError("restore failed")):
        result = api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False)
    assert result.status == "RECOVERY_REQUIRED"
    assert not result.applied and not result.rollback_completed
    recovery = Path(result.recovery_path)
    assert recovery.is_relative_to(root / "temp/topic-reclassification")
    journal = json.loads((recovery / "PREIMAGE.json").read_text(encoding="utf-8"))
    assert journal["paper_id"] == "PTEST"
    assert len(journal["files"]) == 4
    assert all(not item["path"].endswith(".pdf") for item in journal["files"])


def test_unsafe_work_view_filename_is_refused_before_scanning_or_mutating(library):
    root, service, api = library
    from dataclasses import replace
    rows = service.store.load()
    rows["PTEST"] = replace(rows["PTEST"], human_readable_name="../../library/papers/PTEST.pdf")
    service.store.commit(rows)
    before = snapshot(root)
    assert api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False).status == "VIEW_CONFLICT"
    assert snapshot(root) == before


def test_a_symlink_is_not_accepted_as_a_topic_hardlink(library):
    root, _service, api = library
    wanted = root / "papers_by_topic/08_研究方法与综合管理/计量方法与研究设计/PTEST.pdf"
    wanted.parent.mkdir(parents=True)
    try:
        wanted.symlink_to(root / "library/papers/PTEST.pdf")
    except OSError:
        pytest.skip("The current Windows account cannot create a test symlink")
    before = snapshot(root)
    assert api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False).status == "VIEW_CONFLICT"
    assert wanted.is_symlink()
    assert snapshot(root) == before


@pytest.mark.parametrize("field,value", [("keywords", "artificial intelligence adoption"), ("title", "Double machine learning for causal inference"), ("metadata_notes", "New audited metadata")])
def test_any_topic_row_change_invalidates_a_previous_fingerprint(library, field, value):
    from dataclasses import replace
    root, service, api = library
    preview = api.reclassify("PTEST", reason="review")
    service.store.upsert(replace(service.store.load()["PTEST"], **{field: value}))
    before = snapshot(root)
    fresh = api.reclassify("PTEST", reason="review")
    assert fresh.current_fingerprint != preview.current_fingerprint
    result = api.reclassify("PTEST", reason="review", expected_fingerprint=preview.current_fingerprint, dry_run=False)
    assert result.status == "PRECONDITION_CONFLICT"
    assert snapshot(root) == before


def test_classifier_option_change_invalidates_preview_even_when_labels_stay_the_same(library):
    root, service, api = library
    preview = api.reclassify("PTEST", reason="review")
    service.classifier.auto_assign_threshold = 0.74
    fresh = api.reclassify("PTEST", reason="review")
    assert fresh.new_topics == preview.new_topics
    assert fresh.current_fingerprint != preview.current_fingerprint
    before = snapshot(root)
    assert api.reclassify("PTEST", reason="review", expected_fingerprint=preview.current_fingerprint, dry_run=False).status == "PRECONDITION_CONFLICT"
    assert snapshot(root) == before


def test_classification_uses_exactly_the_one_input_bound_by_the_preview(library):
    _root, service, api = library
    calls = []
    def local_reader(paper_id, row):
        calls.append(paper_id)
        return "First local evidence" if len(calls) == 1 else "Changed evidence that must not be classified"
    service.fulltext_reader = local_reader
    preview = api.reclassify("PTEST", reason="review")
    assert preview.status == "DRY_RUN"
    assert calls == ["PTEST"]
    assert preview.classification_input_sha256


def test_changed_local_text_invalidates_preview_without_copying_or_storing_text(library):
    root, service, api = library
    service.fulltext_reader = lambda _id, _row: "Original local text"
    preview = api.reclassify("PTEST", reason="review")
    service.fulltext_reader = lambda _id, _row: "Different local text"
    before = snapshot(root)
    assert api.reclassify("PTEST", reason="review", expected_fingerprint=preview.current_fingerprint, dry_run=False).status == "PRECONDITION_CONFLICT"
    assert snapshot(root) == before


@pytest.mark.parametrize("mutation", [
    {"confirmation_source": "HUMAN"},
    {"confirmation_source": "UNKNOWN"},
    {"assignment_source": ["AUTO_CLASSIFIED"]},
    {"human_override": True},
    {"human_override": "false"},
    {"confirmed_at": "not a timestamp"},
])
def test_contradictory_or_malformed_auto_ownership_is_refused_without_rewriting_it(library, mutation):
    root, service, api = library
    latest = service.provenance.latest_for("PTEST")
    service.provenance.append({**latest, **mutation})
    before = snapshot(root)
    result = api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False)
    assert result.status == "UNKNOWN_OR_INCONSISTENT_PROVENANCE"
    assert snapshot(root) == before


@pytest.mark.parametrize("field", ["confirmation_source", "classification_status_before", "proposed_topics", "human_override", "confirmed_at"])
def test_missing_formal_ownership_fields_are_not_assumed(library, field):
    root, service, api = library
    latest = service.provenance.latest_for("PTEST")
    latest.pop(field)
    service.provenance.append(latest)
    before = snapshot(root)
    assert api.reclassify("PTEST", reason="review", expected_current_topics=[AI], dry_run=False).status == "UNKNOWN_OR_INCONSISTENT_PROVENANCE"
    assert snapshot(root) == before


def test_a_legally_authorized_human_replacement_keeps_valid_ownership_for_next_preview(library):
    _root, service, api = library
    service.provenance.record(paper_id="PTEST", topics=[AI], source=AssignmentSource.HUMAN_CONFIRMED, classification_status_before="REVIEW_REQUIRED")
    preview = api.reclassify("PTEST", reason="review")
    result = api.reclassify("PTEST", reason="Specifically authorized", expected_fingerprint=preview.current_fingerprint,
        authorize_human_confirmed_fingerprint=preview.current_fingerprint, dry_run=False)
    assert result.status == "RECLASSIFIED"
    assert api.reclassify("PTEST", reason="Check current state", expected_current_topics=[METHOD], dry_run=False).status == "ALREADY_CURRENT"


def test_reclassified_history_cannot_hide_its_human_replacement_marker(library):
    root, service, api = library
    service.provenance.record(paper_id="PTEST", topics=[AI], source=AssignmentSource.HUMAN_CONFIRMED, classification_status_before="REVIEW_REQUIRED")
    preview = api.reclassify("PTEST", reason="review")
    assert api.reclassify("PTEST", reason="Authorized correction", expected_fingerprint=preview.current_fingerprint,
        authorize_human_confirmed_fingerprint=preview.current_fingerprint, dry_run=False).status == "RECLASSIFIED"
    record = service.provenance.latest_for("PTEST")
    record["human_override"] = False
    service.provenance.append(record)
    before = snapshot(root)
    assert api.reclassify("PTEST", reason="Check contradictory history", expected_current_topics=[METHOD], dry_run=False).status == "UNKNOWN_OR_INCONSISTENT_PROVENANCE"
    assert snapshot(root) == before


def recovery_package(root, service):
    package = root / "temp/topic-reclassification/1234abcd"
    package.mkdir(parents=True)
    row = service.store.load()["PTEST"]
    journal = {"paper_id": "PTEST", "old_links": [str(p) for p in service.view._existing_links_for("PTEST", row.human_readable_name)],
               "canonical_path": row.canonical_path, "files": []}
    for number, path in enumerate((service.store.jsonl_path, service.store.csv_path, service.view.links_path, service.provenance.path)):
        backup = f"{number}.before"
        (package / backup).write_bytes(path.read_bytes())
        journal["files"].append({"path": str(path), "backup": backup})
    raw = json.dumps(journal, ensure_ascii=False).encode("utf-8")
    (package / "PREIMAGE.json").write_bytes(raw)
    extra = root / "papers_by_topic/08_研究方法与综合管理/计量方法与研究设计/PTEST.pdf"
    extra.parent.mkdir(parents=True)
    os.link(root / "library/papers/PTEST.pdf", extra)
    return package, hashlib.sha256(raw).hexdigest(), extra


def test_recovery_defaults_to_preview_and_preserves_preimage_package(library):
    root, service, api = library
    package, sha, extra = recovery_package(root, service)
    before = snapshot(root)
    package_before = {p.name: p.read_bytes() for p in package.iterdir()}
    preview = api.recover(package, reason="Restore failed operation's original hardlink view", expected_preimage_sha256=sha)
    assert preview.status == "DRY_RUN" and not preview.recovered
    assert preview.remove_aliases == (str(extra),) and not preview.restore_aliases
    assert snapshot(root) == before
    assert {p.name: p.read_bytes() for p in package.iterdir()} == package_before


@pytest.mark.parametrize("kind,expected", [("missing", "PRECONDITION_REQUIRED"), ("wrong", "PRECONDITION_CONFLICT"), ("preimage", "PREIMAGE_CONFLICT")])
def test_recovery_requires_both_reviewed_preimage_and_current_fingerprint(library, kind, expected):
    root, service, api = library
    package, sha, _extra = recovery_package(root, service)
    before = snapshot(root)
    result = api.recover(package, reason="Restore original view", expected_preimage_sha256="0" * 64 if kind == "preimage" else sha,
        expected_current_fingerprint=None if kind == "missing" else "0" * 64, dry_run=False)
    assert result.status == expected
    assert snapshot(root) == before


def test_recovery_refuses_metadata_changed_since_failed_transaction(library):
    from dataclasses import replace
    root, service, api = library
    package, sha, extra = recovery_package(root, service)
    service.store.upsert(replace(service.store.load()["PTEST"], metadata_notes="A later audited metadata update"))
    before = snapshot(root)
    assert api.recover(package, reason="Restore original view", expected_preimage_sha256=sha).status == "METADATA_CHANGED"
    assert snapshot(root) == before and extra.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only hardlink semantics")
def test_actual_windows_readonly_alias_reclassification_preserves_canonical_attributes(library):
    root, service, api = library
    canonical = root / "library/papers/PTEST.pdf"
    original_bytes = canonical.read_bytes()
    os.chmod(canonical, stat.S_IREAD)
    original_attrs = canonical.stat().st_file_attributes
    try:
        assert original_attrs & stat.FILE_ATTRIBUTE_READONLY
        preview = api.reclassify("PTEST", reason="Correct method title")
        result = api.reclassify("PTEST", reason="Correct method title", expected_fingerprint=preview.current_fingerprint, dry_run=False)
        assert result.status == "RECLASSIFIED", result.reason
        assert canonical.read_bytes() == original_bytes
        assert canonical.stat().st_file_attributes == original_attrs
        assert canonical.stat().st_nlink == 2
        assert not (root / "papers_by_topic/01_数字技术与创新/人工智能与机器人/PTEST.pdf").exists()
        assert os.path.samefile(root / "papers_by_topic/08_研究方法与综合管理/计量方法与研究设计/PTEST.pdf", canonical)
    finally:
        os.chmod(canonical, stat.S_IWRITE | stat.S_IREAD)


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only hardlink semantics")
def test_actual_windows_readonly_recovery_removes_only_extra_alias_keeps_original_state_and_package(library):
    root, service, api = library
    package, sha, extra = recovery_package(root, service)
    canonical = root / "library/papers/PTEST.pdf"
    original_bytes = canonical.read_bytes()
    originals = {path: path.read_bytes() for path in (service.store.jsonl_path, service.store.csv_path, service.view.links_path, service.provenance.path)}
    preimage_before = {p.name: p.read_bytes() for p in package.iterdir()}
    os.chmod(canonical, stat.S_IREAD)
    original_attrs = canonical.stat().st_file_attributes
    try:
        preview = api.recover(package, reason="Restore original view", expected_preimage_sha256=sha)
        result = api.recover(package, reason="Restore original view", expected_preimage_sha256=sha,
            expected_current_fingerprint=preview.current_fingerprint, dry_run=False)
        assert result.status == "RECOVERED", result.reason
        assert result.recovered and not extra.exists()
        assert canonical.read_bytes() == original_bytes
        assert canonical.stat().st_file_attributes == original_attrs
        assert canonical.stat().st_nlink == 2
        assert all(path.read_bytes() == content for path, content in originals.items())
        assert all((package / name).read_bytes() == content for name, content in preimage_before.items())
        assert Path(result.audit_path).is_file()
        assert api.recover(package, reason="Check recovered view", expected_preimage_sha256=sha).remove_aliases == ()
    finally:
        os.chmod(canonical, stat.S_IWRITE | stat.S_IREAD)


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only hardlink semantics")
def test_readonly_alias_rollback_restores_original_view_after_audit_write_failure(library):
    root, service, api = library
    canonical = root / "library/papers/PTEST.pdf"
    before = snapshot(root)
    os.chmod(canonical, stat.S_IREAD)
    attrs = canonical.stat().st_file_attributes
    try:
        with patch.object(service.provenance, "append", side_effect=OSError("injected audit failure")):
            result = api.reclassify("PTEST", reason="Correct method title", expected_current_topics=[AI], dry_run=False)
        assert result.status == "FAILED_SAFE" and result.rollback_completed, result.reason
        assert snapshot(root) == before
        assert canonical.stat().st_file_attributes == attrs
        assert canonical.stat().st_nlink == 2
    finally:
        os.chmod(canonical, stat.S_IWRITE | stat.S_IREAD)
