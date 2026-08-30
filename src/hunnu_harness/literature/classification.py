"""WORK-level topic classification against the frozen taxonomy.

Classification is additive metadata.  It reads a WORK's identity fields and, if
available, its full text, and proposes ``domain\\subtopic`` labels that already
exist in the taxonomy.  It never invents a topic, never touches the managed
file, and never rewrites an identity.  A WORK it cannot place confidently
returns ``REVIEW_REQUIRED`` instead of a guess, because a wrong automatic
assignment is more expensive than an absent one.

The concept vocabulary and the bilingual tokenizer are the Navigator's
(``navigator.lexicon`` / ``navigator.tokenize``).  Those are the surface forms
this corpus actually uses, so classification reuses them rather than growing a
second, divergent vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..navigator.lexicon import ConceptMatch, match_concepts
from ..navigator.tokenize import fold
from .models import UNKNOWN
from .topics import TopicLabel, TopicTaxonomy

# Evidence weight by where a concept was seen.  A term in the title states what
# the paper is about; the same term deep in the body may only be cited work, so
# the body is worth less and is capped below.
FIELD_WEIGHTS: Mapping[str, float] = {
    "title": 3.0,
    "keywords": 2.0,
    "abstract": 1.5,
    "journal": 0.5,
    "fulltext": 1.0,
}

# One concept can fire on many surface forms in a long body.  Beyond a few hits
# the extra matches say little, so a concept's body evidence saturates here.
MAX_FULLTEXT_CONCEPT_HITS = 3

# A subtopic's own name is evidence for itself.  The frozen values are compounds
# ("融资约束与资本配置"), and a work whose title contains one of those parts is
# usually filed under exactly that subtopic -- a signal the concept lexicon
# misses, because it maps a term to the concept it belongs to rather than to the
# taxonomy value that happens to be named after it.
TAXONOMY_NAME_SPLIT = re.compile(r"[与和、及/]+")
MIN_NAME_TERM_CHARS = 2

# Longer name matches are more specific, so they carry more weight: a title
# containing "全球价值链" says far more than one containing "创新".
NAME_TERM_UNIT_WEIGHT = 1.5
MAX_NAME_TERM_WEIGHT = 6.0

# The two independent evidence families.  A topic supported by only one of them
# is weaker than the score alone suggests, which is what gates secondaries.
SIGNAL_LEXICON = "LEXICON"
SIGNAL_TAXONOMY_NAME = "TAXONOMY_NAME"

# Confidence saturates as score / (score + CONFIDENCE_SCALE).  Calibrated on the
# 179-work corpus; see docs/POST_ACQUISITION_CLASSIFICATION.md.
CONFIDENCE_SCALE = 3.0

# Empirical distribution over the 179 frozen works: 73 carry one topic, 68 two,
# 27 three, 9 four, and 2 carry five or six.  96% sit at three or fewer, so
# three is the ceiling and anything beyond it is label spraying.
MAX_TOPICS_PER_WORK = 3

# Calibrated on all 179 frozen works; see docs/POST_ACQUISITION_CLASSIFICATION.md.
# At these values the backtest gives precision 0.966, top-1 precision 1.000, and
# recall 0.604 over 60.9% of the corpus, leaving 39.1% for review.  Lower
# thresholds buy coverage at a precision the review queue cannot repair, because
# a wrong assignment is silent while a missing one is visible.
DEFAULT_AUTO_ASSIGN_THRESHOLD = 0.75
DEFAULT_REVIEW_THRESHOLD = 0.30

# A secondary topic must be supported nearly as strongly as the primary one.
# An absolute floor cannot express that: it admits any topic clearing a fixed
# bar however far behind the leader it sits.
DEFAULT_SECONDARY_MARGIN_RATIO = 0.70

# ...and the margin alone is not enough.  Every secondary false positive in the
# 179-work calibration was a topic the concept lexicon proposed with no
# corroboration from the taxonomy value's own name: one concept naming several
# sibling subtopics fires them all at an identical score, so they tie with the
# leader and no ratio can separate them.  Raising the ratio to 1.00 kept all
# five errors and cost 16 correct assignments (precision 0.872 -> 0.783).
# Requiring the name signal removes all five and keeps 32 of 34 (precision
# 1.000).  Secondaries therefore need both families; primaries do not, because
# top-1 already measures at 1.000 precision on its own.
REQUIRE_TAXONOMY_NAME_FOR_SECONDARY = True


class ClassificationStatus(str, Enum):
    CLASSIFIED = "CLASSIFIED"
    CLASSIFIED_WITH_REVIEW_SUGGESTIONS = "CLASSIFIED_WITH_REVIEW_SUGGESTIONS"
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SKIPPED_EXISTING = "SKIPPED_EXISTING"
    FAILED_SAFE = "FAILED_SAFE"


class ClassificationMode(str, Enum):
    METADATA_ONLY = "METADATA_ONLY"
    METADATA_AND_FULLTEXT = "METADATA_AND_FULLTEXT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ClassificationEvidence:
    """Why one topic was proposed."""

    topic: str
    confidence: float
    score: float
    concepts: tuple[str, ...]
    fields: tuple[str, ...]
    terms: tuple[str, ...]
    signals: tuple[str, ...] = ()

    @property
    def corroborated(self) -> bool:
        """Supported by the taxonomy value's own name, not by the lexicon alone."""

        return SIGNAL_TAXONOMY_NAME in self.signals

    def as_dict(self) -> dict[str, Any]:
        return {
            "Topic": self.topic,
            "Confidence": round(self.confidence, 4),
            "Score": round(self.score, 4),
            "Concepts": list(self.concepts),
            "Fields": list(self.fields),
            "Terms": list(self.terms),
            "Signals": list(self.signals),
        }


@dataclass(frozen=True)
class ClassificationResult:
    paper_id: str
    status: ClassificationStatus
    assigned_topics: tuple[ClassificationEvidence, ...] = ()
    proposed_topics: tuple[ClassificationEvidence, ...] = ()
    existing_topics: tuple[str, ...] = ()
    overall_confidence: float = 0.0
    mode: ClassificationMode = ClassificationMode.UNAVAILABLE
    metadata_used: tuple[str, ...] = ()
    fulltext_used: bool = False
    review_required: bool = False
    reason: str = UNKNOWN
    applied: bool = False

    @property
    def assigned_labels(self) -> tuple[str, ...]:
        return tuple(item.topic for item in self.assigned_topics)

    @property
    def primary_topic(self) -> str:
        return self.assigned_topics[0].topic if self.assigned_topics else UNKNOWN

    @property
    def secondary_labels(self) -> tuple[str, ...]:
        return tuple(item.topic for item in self.assigned_topics[1:])

    @property
    def proposed_labels(self) -> tuple[str, ...]:
        return tuple(item.topic for item in self.proposed_topics)

    @property
    def is_classified(self) -> bool:
        """A primary topic is settled, whatever remains for review.

        HUMAN_CONFIRMED belongs here so a decision a person made reaches the
        same canonical write path as an automatic one, instead of needing a
        second writer.  It never arises from classification itself -- only the
        confirmation workflow constructs it.
        """

        return self.status in (
            ClassificationStatus.CLASSIFIED,
            ClassificationStatus.CLASSIFIED_WITH_REVIEW_SUGGESTIONS,
            ClassificationStatus.HUMAN_CONFIRMED,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "PaperID": self.paper_id,
            "ClassificationStatus": self.status.value,
            "PrimaryTopic": self.primary_topic,
            "AssignedTopics": [item.as_dict() for item in self.assigned_topics],
            "AssignedSecondaryTopics": list(self.secondary_labels),
            "ProposedTopics": [item.as_dict() for item in self.proposed_topics],
            "ExistingTopics": list(self.existing_topics),
            "OverallConfidence": round(self.overall_confidence, 4),
            "ClassificationMode": self.mode.value,
            "MetadataUsed": list(self.metadata_used),
            "FullTextUsed": self.fulltext_used,
            "ReviewRequired": self.review_required,
            "Reason": self.reason,
            "Applied": self.applied,
        }


@dataclass(frozen=True)
class ClassificationInput:
    """The text a WORK offers for classification, by provenance."""

    paper_id: str
    title: str = ""
    keywords: str = ""
    journal: str = ""
    abstract: str = ""
    fulltext: str = ""

    def populated_fields(self) -> tuple[str, ...]:
        present = []
        for name in ("title", "keywords", "journal", "abstract", "fulltext"):
            if (getattr(self, name) or "").strip():
                present.append(name)
        return tuple(present)

    @property
    def has_identity_text(self) -> bool:
        return bool((self.title or "").strip() or (self.keywords or "").strip())


@dataclass
class _TopicAccumulator:
    score: float = 0.0
    concepts: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)

    def add(self, *, weight: float, concept: str, field_name: str, term: str, signal: str) -> None:
        self.score += weight
        if signal not in self.signals:
            self.signals.append(signal)
        if concept not in self.concepts:
            self.concepts.append(concept)
        if field_name not in self.fields:
            self.fields.append(field_name)
        if term not in self.terms:
            self.terms.append(term)


def taxonomy_name_terms(subtopic: str) -> tuple[str, ...]:
    """Split a compound subtopic value into the parts worth matching on."""

    parts = [
        part.strip()
        for part in TAXONOMY_NAME_SPLIT.split(subtopic or "")
        if len(part.strip()) >= MIN_NAME_TERM_CHARS
    ]
    return tuple(parts) if parts else ((subtopic,) if subtopic else ())


def _confidence(score: float) -> float:
    if score <= 0:
        return 0.0
    return score / (score + CONFIDENCE_SCALE)


class WorkClassifier:
    """Propose taxonomy topics for one WORK from its own text."""

    def __init__(
        self,
        *,
        taxonomy: TopicTaxonomy | None = None,
        auto_assign_threshold: float = DEFAULT_AUTO_ASSIGN_THRESHOLD,
        secondary_margin_ratio: float = DEFAULT_SECONDARY_MARGIN_RATIO,
        review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
        max_topics: int = MAX_TOPICS_PER_WORK,
        require_taxonomy_name_for_secondary: bool = REQUIRE_TAXONOMY_NAME_FOR_SECONDARY,
    ) -> None:
        self._taxonomy = taxonomy
        self.auto_assign_threshold = auto_assign_threshold
        self.secondary_margin_ratio = secondary_margin_ratio
        self.review_threshold = review_threshold
        self.max_topics = max_topics
        self.require_taxonomy_name_for_secondary = require_taxonomy_name_for_secondary

    @property
    def taxonomy(self) -> TopicTaxonomy:
        if self._taxonomy is None:
            self._taxonomy = TopicTaxonomy.load()
        return self._taxonomy

    def score_topics(self, payload: ClassificationInput) -> dict[TopicLabel, _TopicAccumulator]:
        """Accumulate evidence per taxonomy label across every populated field."""

        totals: dict[TopicLabel, _TopicAccumulator] = {}
        for field_name, weight in FIELD_WEIGHTS.items():
            text = getattr(payload, field_name, "") or ""
            if not text.strip():
                continue
            matches = match_concepts(text)
            per_concept: dict[str, int] = {}
            for match in matches:
                hits = per_concept.get(match.key, 0)
                if field_name == "fulltext" and hits >= MAX_FULLTEXT_CONCEPT_HITS:
                    continue
                per_concept[match.key] = hits + 1
                self._credit(totals, match, weight=weight, field_name=field_name)
            self._credit_taxonomy_names(totals, text, weight=weight, field_name=field_name)
        return totals

    def _credit_taxonomy_names(
        self,
        totals: dict[TopicLabel, _TopicAccumulator],
        text: str,
        *,
        weight: float,
        field_name: str,
    ) -> None:
        folded = fold(text)
        if not folded:
            return
        for label in self.taxonomy.labels():
            for term in taxonomy_name_terms(label.subtopic):
                folded_term = fold(term)
                if not folded_term or folded_term not in folded:
                    continue
                specificity = min(
                    len(folded_term) * NAME_TERM_UNIT_WEIGHT, MAX_NAME_TERM_WEIGHT
                )
                totals.setdefault(label, _TopicAccumulator()).add(
                    weight=weight * specificity / FIELD_WEIGHTS["title"],
                    concept=f"taxonomy:{label.subtopic}",
                    field_name=field_name,
                    term=term,
                    signal=SIGNAL_TAXONOMY_NAME,
                )

    def _credit(
        self,
        totals: dict[TopicLabel, _TopicAccumulator],
        match: ConceptMatch,
        *,
        weight: float,
        field_name: str,
    ) -> None:
        for subtopic in match.concept.topics:
            label = self.taxonomy.resolve_subtopic(subtopic)
            if label is None:
                # A lexicon topic outside the frozen taxonomy is a vocabulary
                # drift bug, not permission to create the value.
                continue
            totals.setdefault(label, _TopicAccumulator()).add(
                weight=weight,
                concept=match.key,
                field_name=field_name,
                term=match.matched_term,
                signal=SIGNAL_LEXICON,
            )

    def classify(
        self,
        payload: ClassificationInput,
        *,
        existing_topics: Sequence[str] = (),
    ) -> ClassificationResult:
        """Classify one WORK without writing anything."""

        populated = payload.populated_fields()
        fulltext_used = bool((payload.fulltext or "").strip())
        mode = (
            ClassificationMode.METADATA_AND_FULLTEXT
            if fulltext_used
            else ClassificationMode.METADATA_ONLY
        )
        if not payload.has_identity_text:
            return ClassificationResult(
                paper_id=payload.paper_id,
                status=ClassificationStatus.REVIEW_REQUIRED,
                existing_topics=tuple(existing_topics),
                mode=ClassificationMode.UNAVAILABLE,
                metadata_used=populated,
                fulltext_used=fulltext_used,
                review_required=True,
                reason="NO_IDENTITY_TEXT_AVAILABLE",
            )

        totals = self.score_topics(payload)
        ranked = sorted(
            (
                ClassificationEvidence(
                    topic=label.label,
                    confidence=_confidence(acc.score),
                    score=acc.score,
                    concepts=tuple(acc.concepts),
                    fields=tuple(acc.fields),
                    terms=tuple(acc.terms),
                    signals=tuple(acc.signals),
                )
                for label, acc in totals.items()
            ),
            key=lambda item: (-item.score, item.topic),
        )
        if not ranked:
            return ClassificationResult(
                paper_id=payload.paper_id,
                status=ClassificationStatus.REVIEW_REQUIRED,
                existing_topics=tuple(existing_topics),
                mode=mode,
                metadata_used=populated,
                fulltext_used=fulltext_used,
                review_required=True,
                reason="NO_TAXONOMY_CONCEPT_MATCHED",
            )

        leader = ranked[0]
        if leader.confidence < self.auto_assign_threshold:
            reason = (
                "BELOW_REVIEW_THRESHOLD"
                if leader.confidence < self.review_threshold
                else "BELOW_AUTO_ASSIGN_THRESHOLD"
            )
            return ClassificationResult(
                paper_id=payload.paper_id,
                status=ClassificationStatus.REVIEW_REQUIRED,
                proposed_topics=tuple(ranked[: self.max_topics]),
                existing_topics=tuple(existing_topics),
                overall_confidence=leader.confidence,
                mode=mode,
                metadata_used=populated,
                fulltext_used=fulltext_used,
                review_required=True,
                reason=reason,
            )

        assigned = [leader]
        suggested: list[ClassificationEvidence] = []
        floor = leader.score * self.secondary_margin_ratio
        for candidate in ranked[1:]:
            if candidate.score < floor:
                continue
            corroborated = candidate.corroborated or not self.require_taxonomy_name_for_secondary
            if corroborated and len(assigned) < self.max_topics:
                assigned.append(candidate)
            else:
                # Well supported by score, but by one evidence family only: it is
                # a suggestion for a human, never a silent canonical write.
                suggested.append(candidate)
        held_back = tuple(
            item for item in ranked if item not in assigned and item not in suggested
        )
        proposed = tuple(suggested) + held_back
        status = (
            ClassificationStatus.CLASSIFIED_WITH_REVIEW_SUGGESTIONS
            if suggested
            else ClassificationStatus.CLASSIFIED
        )
        return ClassificationResult(
            paper_id=payload.paper_id,
            status=status,
            assigned_topics=tuple(assigned),
            proposed_topics=proposed[: self.max_topics],
            existing_topics=tuple(existing_topics),
            overall_confidence=leader.confidence,
            mode=mode,
            metadata_used=populated,
            fulltext_used=fulltext_used,
            review_required=False,
            reason="AUTO_ASSIGNED_FROM_FROZEN_TAXONOMY",
        )


def input_from_topic_row(
    row: Any,
    *,
    journal: str = "",
    abstract: str = "",
    fulltext: str = "",
) -> ClassificationInput:
    """Build classifier input from a ``WorkTopicRow``-shaped object."""

    return ClassificationInput(
        paper_id=getattr(row, "paper_id", UNKNOWN),
        title=_clean(getattr(row, "title", "")),
        keywords=_clean(getattr(row, "keywords", "")).replace(";", " "),
        journal=_clean(journal),
        abstract=_clean(abstract),
        fulltext=_clean(fulltext),
    )


def _clean(value: Any) -> str:
    text = "" if value is None else str(value)
    return "" if text == UNKNOWN else text


__all__ = [
    "CONFIDENCE_SCALE",
    "DEFAULT_AUTO_ASSIGN_THRESHOLD",
    "DEFAULT_REVIEW_THRESHOLD",
    "DEFAULT_SECONDARY_MARGIN_RATIO",
    "FIELD_WEIGHTS",
    "MAX_TOPICS_PER_WORK",
    "REQUIRE_TAXONOMY_NAME_FOR_SECONDARY",
    "SIGNAL_LEXICON",
    "SIGNAL_TAXONOMY_NAME",
    "TAXONOMY_NAME_SPLIT",
    "ClassificationEvidence",
    "ClassificationInput",
    "ClassificationMode",
    "ClassificationResult",
    "ClassificationStatus",
    "WorkClassifier",
    "input_from_topic_row",
    "taxonomy_name_terms",
]
