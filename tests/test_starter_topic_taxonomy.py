"""Optional starter topics stay compatible with the shipped matching vocabulary."""

import json
from pathlib import Path

import pytest

from hunnu_harness import paths
from hunnu_harness.literature import taxonomy_aliases, topics
from hunnu_harness.literature.classification import ClassificationInput, ClassificationStatus, WorkClassifier
from hunnu_harness.navigator import lexicon


TEMPLATE = paths.CORE_ROOT / "config" / "topic_taxonomy.default.json"
ALIASES = paths.CORE_ROOT / "src" / "hunnu_harness" / "literature" / "taxonomy_aliases.default.json"
LEXICON = paths.CORE_ROOT / "src" / "hunnu_harness" / "navigator" / "lexicon.default.json"


def test_starter_is_identified_as_a_new_optional_design_and_all_labels_round_trip():
    payload = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    assert payload["template_version"] == "general-empirical-economics-v1"
    assert payload["generated_at"] == "2026-11-08"
    assert payload["provenance"]["kind"] == "NEW_OPTIONAL_TEMPLATE"
    assert "不是恢复" in payload["provenance"]["description"]
    taxonomy = topics.TopicTaxonomy.load(TEMPLATE)
    assert 5 <= len(taxonomy.domains) <= 8
    assert len(taxonomy.subtopics()) == len(set(taxonomy.subtopics()))
    labels = taxonomy.labels()
    restored = topics.parse_topics_field(topics.format_topics_field(labels))
    assert taxonomy.require_known(restored) == labels


def test_starter_covers_every_shipped_alias_and_lexicon_topic_without_orphan_categories():
    taxonomy = topics.TopicTaxonomy.load(TEMPLATE)
    aliases = json.loads(ALIASES.read_text(encoding="utf-8"))["aliases"]
    concepts = json.loads(LEXICON.read_text(encoding="utf-8"))["concepts"]
    configured = set(taxonomy.subtopics())
    concept_targets = {name for concept in concepts for name in concept["topics"]}
    assert set(aliases) <= configured
    assert concept_targets <= configured
    assert configured == set(aliases) | concept_targets
    assert sum(bool(values) for values in aliases.values()) == len(configured) - 1


def test_starter_is_not_loaded_silently_when_active_configuration_is_missing(tmp_path, monkeypatch):
    missing = tmp_path / "topic_taxonomy.json"
    monkeypatch.setattr(topics, "TOPIC_TAXONOMY_JSON", missing)
    with pytest.raises(topics.TopicTaxonomyError, match="not present"):
        topics.TopicTaxonomy.load()
    assert not missing.exists()
    assert TEMPLATE.is_file()


@pytest.fixture
def starter_classifier(monkeypatch):
    # A user's runtime vocabulary override must not change tests of the shipped
    # starter. Reuse the real loaders and matching code with the shipped data.
    concepts = lexicon._concepts_from_payload(json.loads(LEXICON.read_text(encoding="utf-8")), source=LEXICON)
    monkeypatch.setattr(lexicon, "CONCEPTS", concepts)
    lookup, longest = lexicon._build_lookup()
    monkeypatch.setattr(lexicon, "_TERM_LOOKUP", lookup)
    monkeypatch.setattr(lexicon, "_LONGEST_TERM", longest)
    aliases = taxonomy_aliases._aliases_from_payload(json.loads(ALIASES.read_text(encoding="utf-8")), source=ALIASES)
    monkeypatch.setattr(taxonomy_aliases, "ENGLISH_SUBTOPIC_ALIASES", aliases)
    monkeypatch.setattr(taxonomy_aliases, "_FOLDED_ALIASES", taxonomy_aliases._folded_table())
    return WorkClassifier(taxonomy=topics.TopicTaxonomy.load(TEMPLATE))


@pytest.mark.parametrize("title,subtopic", [
    ("Digital transformation and stock liquidity", "数字化转型"),
    ("Audit quality and internal controls in listed firms", "审计与内部控制"),
    ("Human capital accumulation and firm growth", "人力资本"),
    ("Green finance and corporate green innovation", "绿色金融与绿色创新"),
    ("Supply chain resilience under production disruptions", "供应链韧性"),
    ("Analyst coverage and capital market information", "分析师与资本市场"),
])
def test_representative_research_titles_use_the_expected_primary_topic(starter_classifier, title, subtopic):
    result = starter_classifier.classify(ClassificationInput(paper_id="FIXTURE", title=title))
    assert result.is_classified
    assert result.primary_topic.endswith("\\" + subtopic)
    assert not result.applied


def test_current_cnki_title_gets_a_digital_transformation_primary_without_forced_siblings(starter_classifier):
    result = starter_classifier.classify(ClassificationInput(
        paper_id="P85C57D263C83",
        title="企业数字化转型与资本市场表现——来自股票流动性的经验证据",
        journal="管理世界",
    ))
    assert result.primary_topic == "01_数字技术与创新\\数字化转型"
    assert result.secondary_labels == ()
    assert not result.applied


def test_no_matching_evidence_remains_for_review_instead_of_falling_into_general_management(starter_classifier):
    result = starter_classifier.classify(ClassificationInput(
        paper_id="UNRELATED", title="A descriptive essay on an unrelated subject"
    ))
    assert result.status is ClassificationStatus.REVIEW_REQUIRED
    assert not result.assigned_topics
    assert result.primary_topic == "unknown"
