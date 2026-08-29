"""BM25F scoring with per-term provenance.

Two properties matter more here than raw ranking quality.  The scorer must
explain itself -- every contribution is recorded with the field it came from and
the term that caused it -- and it must be deterministic, so an acceptance test
can assert an ordering rather than a fuzzy neighbourhood.

Field weights come from what the corpus actually contains: curated topics are
dense (337 assignments, no work without one) and hand-checked, which makes them
the highest-precision signal after the title itself.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .query import QueryTerm
from .tokenize import tokenize


BM25_K1 = 1.2
BM25_B = 0.75

#: Field name -> weight applied to term frequency before BM25 saturation.
DEFAULT_FIELD_WEIGHTS: dict[str, float] = {
    "title": 3.0,
    "topics": 2.5,
    "keywords": 2.0,
    "authors": 1.6,
    "journal": 1.0,
    "human_name": 0.8,
}

#: Field weights for a full-text chunk document.
PASSAGE_FIELD_WEIGHTS: dict[str, float] = {"text": 1.0}


@dataclass
class FieldDocument:
    """One work rendered as weighted, tokenised fields."""

    doc_id: str
    fields: dict[str, tuple[str, ...]]

    _counts: dict[str, Counter[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._counts = {name: Counter(tokens) for name, tokens in self.fields.items()}

    def count(self, field_name: str, token: str) -> int:
        counter = self._counts.get(field_name)
        return counter.get(token, 0) if counter else 0

    def length(self, field_name: str) -> int:
        return len(self.fields.get(field_name, ()))

    def tokens(self) -> set[str]:
        merged: set[str] = set()
        for tokens in self.fields.values():
            merged.update(tokens)
        return merged


@dataclass(frozen=True)
class MatchRecord:
    """Why one term contributed, and how much."""

    field_name: str
    token: str
    weight: float
    contribution: float
    source: str
    concept: str | None = None
    surface: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "signal": self.field_name,
            "term": self.token,
            "weight": round(self.weight, 4),
            "contribution": round(self.contribution, 4),
            "source": self.source,
        }
        if self.concept:
            payload["concept"] = self.concept
        if self.surface and self.surface != self.token:
            payload["expanded_from"] = self.surface
        return payload


@dataclass
class ScoredDocument:
    doc_id: str
    score: float
    matches: tuple[MatchRecord, ...]

    def matched_fields(self) -> tuple[str, ...]:
        seen: list[str] = []
        for match in self.matches:
            if match.field_name not in seen:
                seen.append(match.field_name)
        return tuple(seen)

    def concepts_hit(self) -> tuple[str, ...]:
        seen: list[str] = []
        for match in self.matches:
            if match.concept and match.concept not in seen:
                seen.append(match.concept)
        return tuple(seen)


class BM25FIndex:
    """In-memory BM25F over a small document set.

    At 179 works a full scan is a few milliseconds, so the index keeps the
    documents themselves rather than a compressed posting structure: it stays
    inspectable, and it costs nothing at this scale.  The inverted map exists
    only to skip documents that share no term with the query.
    """

    def __init__(
        self,
        documents: Iterable[FieldDocument],
        *,
        field_weights: Mapping[str, float] | None = None,
    ) -> None:
        self.documents: list[FieldDocument] = list(documents)
        self.field_weights = dict(field_weights or DEFAULT_FIELD_WEIGHTS)
        self._by_id = {document.doc_id: document for document in self.documents}
        self._postings: dict[str, set[str]] = {}
        self._document_frequency: Counter[str] = Counter()
        self._average_length: dict[str, float] = {}
        self._build()

    def _build(self) -> None:
        for name in self.field_weights:
            lengths = [document.length(name) for document in self.documents]
            total = sum(lengths)
            self._average_length[name] = (total / len(lengths)) if lengths and total else 1.0
        for document in self.documents:
            for token in document.tokens():
                self._postings.setdefault(token, set()).add(document.doc_id)
                self._document_frequency[token] += 1

    @property
    def size(self) -> int:
        return len(self.documents)

    def get(self, doc_id: str) -> FieldDocument | None:
        return self._by_id.get(doc_id)

    def _idf(self, token: str) -> float:
        n = len(self.documents)
        df = self._document_frequency.get(token, 0)
        if df == 0:
            return 0.0
        # BM25 probabilistic idf, floored so a term in every document still
        # contributes a little rather than turning negative.
        return max(0.05, math.log(1.0 + (n - df + 0.5) / (df + 0.5)))

    def candidates(self, terms: Sequence[QueryTerm]) -> set[str]:
        matched: set[str] = set()
        for term in terms:
            matched.update(self._postings.get(term.token, ()))
        return matched

    def score(
        self,
        terms: Sequence[QueryTerm],
        *,
        restrict_to: Iterable[str] | None = None,
        minimum_score: float = 0.0,
    ) -> list[ScoredDocument]:
        if not terms:
            return []
        doc_ids = set(restrict_to) if restrict_to is not None else self.candidates(terms)
        results: list[ScoredDocument] = []
        for doc_id in doc_ids:
            document = self._by_id.get(doc_id)
            if document is None:
                continue
            scored = self._score_document(document, terms)
            if scored.score > minimum_score:
                results.append(scored)
        # Deterministic ordering: score first, then doc_id so equal scores never
        # depend on set iteration order.
        results.sort(key=lambda item: (-item.score, item.doc_id))
        return results

    def _score_document(self, document: FieldDocument, terms: Sequence[QueryTerm]) -> ScoredDocument:
        total = 0.0
        matches: list[MatchRecord] = []
        for term in terms:
            idf = self._idf(term.token)
            if idf <= 0.0:
                continue
            weighted_frequency = 0.0
            normalizer = 0.0
            per_field: list[tuple[str, float]] = []
            for field_name, field_weight in self.field_weights.items():
                raw = document.count(field_name, term.token)
                if not raw:
                    continue
                length = document.length(field_name) or 1
                average = self._average_length.get(field_name) or 1.0
                denominator = 1.0 - BM25_B + BM25_B * (length / average)
                contribution = field_weight * raw / denominator
                weighted_frequency += contribution
                normalizer += contribution
                per_field.append((field_name, contribution))
            if weighted_frequency <= 0.0:
                continue
            saturated = weighted_frequency / (BM25_K1 + weighted_frequency)
            term_score = idf * saturated * (BM25_K1 + 1.0) * term.weight
            total += term_score
            for field_name, contribution in per_field:
                share = (contribution / normalizer) if normalizer else 0.0
                matches.append(
                    MatchRecord(
                        field_name=field_name,
                        token=term.token,
                        weight=self.field_weights.get(field_name, 1.0),
                        contribution=term_score * share,
                        source=term.source,
                        concept=term.concept,
                        surface=term.surface,
                    )
                )
        matches.sort(key=lambda item: (-item.contribution, item.field_name, item.token))
        return ScoredDocument(doc_id=document.doc_id, score=total, matches=tuple(matches))


def build_field_document(doc_id: str, raw_fields: Mapping[str, str | Sequence[str]]) -> FieldDocument:
    """Tokenise a mapping of field name -> text (or list of text) into a document."""

    fields: dict[str, tuple[str, ...]] = {}
    for name, value in raw_fields.items():
        if isinstance(value, (list, tuple)):
            text = " ".join(str(item) for item in value)
        else:
            text = str(value or "")
        fields[name] = tuple(tokenize(text))
    return FieldDocument(doc_id=doc_id, fields=fields)


__all__ = [
    "BM25FIndex",
    "BM25_B",
    "BM25_K1",
    "DEFAULT_FIELD_WEIGHTS",
    "FieldDocument",
    "MatchRecord",
    "PASSAGE_FIELD_WEIGHTS",
    "ScoredDocument",
    "build_field_document",
]
