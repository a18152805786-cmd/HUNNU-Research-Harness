"""English subtopic aliases: the taxonomy-name signal for English titles.

The alias table bridges the frozen Chinese subtopic values to the standard
English terms for the same constructs, so an English title can corroborate a
classification the way a Chinese title always could.  These tests pin the
table's integrity (no drift from the frozen taxonomy, no double-crediting of
existing name terms), the matching rules (word boundaries, one hit per
subtopic), and the classifier behaviour the table must not change (Chinese
works, the closed taxonomy, and the secondary corroboration gate).

Everything runs on an isolated taxonomy under TEMP_DIR except the two checks
that read the real frozen taxonomy read-only, which skip cleanly where it is
absent.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.classification import (
    ClassificationInput,
    ClassificationStatus,
    SIGNAL_TAXONOMY_NAME,
    WorkClassifier,
    taxonomy_name_terms,
)
from hunnu_harness.literature.taxonomy_aliases import (
    ENGLISH_SUBTOPIC_ALIASES,
    best_alias_hit,
    folded_aliases_for,
)
from hunnu_harness.literature.topics import TopicTaxonomy, TopicTaxonomyError
from hunnu_harness.navigator.tokenize import fold
from hunnu_harness.paths import TEMP_DIR

TAXONOMY = {
    "domains": {
        "01_人工智能与数字经济": ["AI漂洗", "人工智能与机器人"],
        "04_会计审计与信息披露": ["信息披露", "审计与内部控制", "盈余质量与财务报告"],
        "05_资本市场与证券": ["股价与市场波动"],
        "09_ESG与绿色发展": ["ESG与社会责任", "绿色金融与绿色创新"],
    },
    "generated_at": "2026-08-30T00:00:00+00:00",
}

AI_WASHING = "01_人工智能与数字经济\\AI漂洗"
AUDIT = "04_会计审计与信息披露\\审计与内部控制"
EARNINGS = "04_会计审计与信息披露\\盈余质量与财务报告"
STOCK_PRICE = "05_资本市场与证券\\股价与市场波动"


def _classifier(root: Path) -> WorkClassifier:
    path = root / "topic_taxonomy.json"
    path.write_text(json.dumps(TAXONOMY, ensure_ascii=False), encoding="utf-8")
    return WorkClassifier(taxonomy=TopicTaxonomy.load(path))


class TableIntegrityTests(unittest.TestCase):
    def test_alias_keys_cover_the_frozen_taxonomy_exactly(self) -> None:
        """Every frozen subtopic has an entry; no entry names a ghost subtopic."""

        try:
            taxonomy = TopicTaxonomy.load()
        except TopicTaxonomyError:
            self.skipTest("real taxonomy is not present on this machine")
        frozen = {label.subtopic for label in taxonomy.labels()}
        self.assertEqual(set(ENGLISH_SUBTOPIC_ALIASES), frozen)

    def test_aliases_are_latin_only(self) -> None:
        for subtopic, aliases in ENGLISH_SUBTOPIC_ALIASES.items():
            for alias in aliases:
                self.assertTrue(
                    all(ord(char) < 0x2E80 for char in alias),
                    f"non-Latin alias {alias!r} under {subtopic!r}",
                )
                self.assertTrue(fold(alias), f"alias {alias!r} folds to nothing")

    def test_aliases_never_overlap_a_latin_taxonomy_name_term(self) -> None:
        """No alias may reuse a Latin name part of its own subtopic, even as one word.

        ``esg`` is a name part of ``ESG与社会责任`` and matches today.  An alias
        equal to it would double-count every hit; an alias merely *containing*
        it ("esg rating") is subtler and worse -- one phrase earns the name
        credit and the alias credit together and clears the gate as a single
        piece of evidence.
        """

        for subtopic, aliases in ENGLISH_SUBTOPIC_ALIASES.items():
            latin_name_terms = {
                fold(term)
                for term in taxonomy_name_terms(subtopic)
                if all(ord(char) < 0x2E80 for char in term)
            }
            for alias in aliases:
                folded = fold(alias)
                overlap = latin_name_terms & ({folded} | set(folded.split()))
                self.assertFalse(
                    overlap,
                    f"alias {alias!r} under {subtopic!r} overlaps name terms {overlap}",
                )

    def test_the_human_catch_all_takes_no_aliases(self) -> None:
        self.assertEqual(ENGLISH_SUBTOPIC_ALIASES["综合经济与管理"], ())

    def test_no_econometric_tool_words(self) -> None:
        """An applied paper naming its method must not be filed under methods."""

        method_aliases = set(folded_aliases_for("计量方法与研究设计"))
        for tool in ("difference in differences", "instrumental variable",
                     "regression discontinuity", "propensity score matching"):
            self.assertNotIn(fold(tool), method_aliases)


class MatchingTests(unittest.TestCase):
    def test_latin_aliases_match_on_word_boundaries_only(self) -> None:
        # "export" the word matches; "exporter" containing it does not.
        self.assertEqual(
            best_alias_hit(fold("Weighing the export basket"), "全球价值链与国际化"),
            "export",
        )
        self.assertIsNone(
            best_alias_hit(fold("The exporter productivity premium"), "全球价值链与国际化")
        )

    def test_one_hit_per_subtopic_and_the_most_specific_wins(self) -> None:
        # "audit" and "audit quality" both occur; only the two-word variant is
        # returned, so one construct is never credited twice.
        hit = best_alias_hit(fold("Audit quality and firm outcomes"), "审计与内部控制")
        self.assertEqual(hit, "audit quality")

    def test_text_without_aliases_hits_nothing(self) -> None:
        folded = fold("AI漂洗与审计师风险决策")
        for subtopic in ENGLISH_SUBTOPIC_ALIASES:
            self.assertIsNone(best_alias_hit(folded, subtopic))


class ClassifierBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_english_title_with_alias_support_classifies(self) -> None:
        """Lexicon plus alias is two evidence families, and that clears the gate."""

        with tempfile.TemporaryDirectory(prefix="alias-en-", dir=TEMP_DIR) as tmp:
            classifier = _classifier(Path(tmp))
            result = classifier.classify(
                ClassificationInput(
                    paper_id="PEN",
                    title="Audit effort and earnings management",
                    keywords="earnings management audit",
                )
            )
            self.assertTrue(result.is_classified, result.as_dict())
            self.assertEqual(set(result.assigned_labels), {AUDIT, EARNINGS})
            for evidence in result.assigned_topics:
                self.assertIn(SIGNAL_TAXONOMY_NAME, evidence.signals)

    def test_alias_alone_is_still_one_family_and_goes_to_review(self) -> None:
        """An alias hit with no lexicon support stays below the gate.

        The thresholds are untouched: a lone taxonomy-name signal from a title
        scores exactly what a lone Chinese name hit does.
        """

        with tempfile.TemporaryDirectory(prefix="alias-lone-", dir=TEMP_DIR) as tmp:
            classifier = _classifier(Path(tmp))
            result = classifier.classify(
                # "internal controls" is an alias; the lexicon only knows the
                # singular, which the word boundary keeps from firing, and
                # there are no keywords to add a second field.
                ClassificationInput(
                    paper_id="PLONE",
                    title="Strengthening internal controls in public organizations",
                )
            )
            self.assertEqual(result.status, ClassificationStatus.REVIEW_REQUIRED)
            self.assertEqual(result.assigned_labels, ())

    def test_chinese_title_evidence_is_untouched_by_the_alias_table(self) -> None:
        """No alias concept ever appears for a Chinese title.

        The per-language backtest holds the aggregate (162 works, coverage and
        precision bit-identical with the table on and off); this pins the
        mechanism: a CJK title simply contains no Latin alias to hit.
        """

        with tempfile.TemporaryDirectory(prefix="alias-cjk-", dir=TEMP_DIR) as tmp:
            classifier = _classifier(Path(tmp))
            result = classifier.classify(
                ClassificationInput(
                    paper_id="PCN", title="AI漂洗与审计师风险决策", keywords="AI漂洗 审计"
                )
            )
            self.assertTrue(result.is_classified)
            for evidence in result.assigned_topics + result.proposed_topics:
                for concept in evidence.concepts:
                    self.assertFalse(
                        concept.startswith("taxonomy-alias:"),
                        f"alias concept {concept!r} fired on a Chinese title",
                    )

    def test_chinese_title_with_latin_acronym_is_not_double_credited(self) -> None:
        """``ESG`` in a Chinese title is a taxonomy name part, not also an alias."""

        with tempfile.TemporaryDirectory(prefix="alias-esg-", dir=TEMP_DIR) as tmp:
            classifier = _classifier(Path(tmp))
            totals = classifier.score_topics(
                ClassificationInput(paper_id="PESG", title="ESG表现与企业绿色创新")
            )
            for label, accumulator in totals.items():
                if label.subtopic != "ESG与社会责任":
                    continue
                alias_concepts = [
                    concept
                    for concept in accumulator.concepts
                    if concept.startswith("taxonomy-alias:")
                ]
                self.assertEqual(alias_concepts, [], accumulator.concepts)

    def test_alias_for_a_subtopic_outside_the_taxonomy_creates_nothing(self) -> None:
        """The table names all 39 frozen subtopics; a taxonomy that carries
        fewer must never gain one through an alias hit."""

        with tempfile.TemporaryDirectory(prefix="alias-ghost-", dir=TEMP_DIR) as tmp:
            classifier = _classifier(Path(tmp))
            known = {label.label for label in classifier.taxonomy.labels()}
            # "supply chain resilience" aliases a subtopic this taxonomy lacks.
            result = classifier.classify(
                ClassificationInput(
                    paper_id="PGHOST", title="Supply chain resilience under stress"
                )
            )
            for evidence in result.assigned_topics + result.proposed_topics:
                self.assertIn(evidence.topic, known)

    def test_secondary_corroboration_gate_survives_in_english(self) -> None:
        """A lexicon-only English secondary is still held back for review.

        The alias table lets an English secondary *earn* corroboration; it must
        not have loosened the requirement itself.
        """

        with tempfile.TemporaryDirectory(prefix="alias-gate-", dir=TEMP_DIR) as tmp:
            classifier = _classifier(Path(tmp))
            result = classifier.classify(
                ClassificationInput(
                    paper_id="PGATE",
                    # Three firm_value lexicon terms, none of which is an
                    # alias, tie the stock-price topic with the leader --
                    # exactly the failure shape the corroboration gate exists
                    # for.
                    title="AI washing, market reaction, market value and firm value",
                )
            )
            self.assertEqual(
                result.status, ClassificationStatus.CLASSIFIED_WITH_REVIEW_SUGGESTIONS
            )
            self.assertEqual(result.assigned_labels, (AI_WASHING,))
            self.assertIn(STOCK_PRICE, result.proposed_labels)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
