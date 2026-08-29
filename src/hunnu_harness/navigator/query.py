"""Query normalisation, field parsing, and auditable expansion.

Two rules govern this module.  First, an expansion the agent cannot see is a
liability: every response carries the full record of what the query became.
Second, an expanded term must never outrank a literal one, so expansions enter
the scorer at a fixed discount rather than as equals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..literature.models import UNKNOWN
from ..literature.normalization import normalize_doi
from .lexicon import ConceptMatch, Facet, expansion_terms, match_concepts, topics_for_concepts
from .tokenize import fold, normalize_person_query, tokenize


PAPER_ID_RE = re.compile(r"^P[0-9A-Fa-f]{12}$")
_FIELD_RE = re.compile(
    r"\b(doi|paper_id|paperid|id|title|author|authors|journal|year|topic|keyword)\s*[:：]\s*",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# An expanded (synonym) term contributes at this fraction of a literal term's
# weight.  A cross-language hit is real evidence, but never as strong as the
# words the researcher actually typed.
EXPANSION_WEIGHT = 0.6

# In a research question of the form "X affects Y through Z", X is what a paper
# has to be *about*; Y and Z say which papers about X are relevant.  Terms
# belonging to a phenomenon concept therefore carry more weight, otherwise a
# four-concept question dilutes its own subject and surfaces papers that share
# only its secondary vocabulary.  Reported in the query audit like every other
# transformation.
PHENOMENON_WEIGHT = 1.5


@dataclass(frozen=True)
class QueryTerm:
    """One scoring term with its provenance."""

    token: str
    weight: float
    source: str          # "literal" | "expansion"
    concept: str | None = None
    surface: str | None = None


@dataclass
class ParsedQuery:
    """Everything downstream stages need, plus a full audit trail."""

    original: str
    normalized: str
    field_filters: dict[str, str] = field(default_factory=dict)
    free_text: str = ""
    literal_tokens: tuple[str, ...] = ()
    terms: tuple[QueryTerm, ...] = ()
    concept_matches: tuple[ConceptMatch, ...] = ()
    expansions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    concept_topics: tuple[str, ...] = ()
    doi: str = UNKNOWN
    paper_id: str = ""
    years: tuple[str, ...] = ()
    author_candidates: tuple[str, ...] = ()

    @property
    def facets(self) -> set[Facet]:
        return {match.facet for match in self.concept_matches}

    def concept_keys(self) -> tuple[str, ...]:
        seen: list[str] = []
        for match in self.concept_matches:
            if match.key not in seen:
                seen.append(match.key)
        return tuple(seen)

    def facets_for_concept(self, key: str) -> Facet | None:
        for match in self.concept_matches:
            if match.key == key:
                return match.facet
        return None

    def audit(self) -> dict[str, Any]:
        """The record that makes expansion reviewable rather than magical."""

        return {
            "original": self.original,
            "normalized": self.normalized,
            "field_filters": dict(sorted(self.field_filters.items())),
            "concepts_matched": [
                {"concept": match.key, "facet": match.facet.value, "matched_term": match.matched_term}
                for match in self.concept_matches
            ],
            "expansions": [
                {"concept": key, "added": list(added)}
                for key, added in sorted(self.expansions.items())
            ],
            "expansion_weight": EXPANSION_WEIGHT,
            "phenomenon_weight": PHENOMENON_WEIGHT,
            "phenomenon_boosted_terms": sorted(
                {term.token for term in self.terms if term.weight >= PHENOMENON_WEIGHT}
            ),
            "concept_topics": list(self.concept_topics),
            "detected": {
                "doi": self.doi,
                "paper_id": self.paper_id,
                "years": list(self.years),
                "author_candidates": list(self.author_candidates),
            },
        }


def _split_field_filters(text: str) -> tuple[dict[str, str], str]:
    """Pull ``field:value`` prefixes out of a query, leaving the free text.

    A value runs to the next recognised field key or the end of the string, so
    ``author:王海森 李纲 year:2026`` keeps both names on the author filter.
    """

    filters: dict[str, str] = {}
    matches = list(_FIELD_RE.finditer(text))
    if not matches:
        return filters, text

    remainder_parts: list[str] = []
    if matches[0].start() > 0:
        remainder_parts.append(text[: matches[0].start()])

    for index, match in enumerate(matches):
        key = match.group(1).lower()
        key = {"paperid": "paper_id", "id": "paper_id", "authors": "author"}.get(key, key)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip().strip('"').strip("'").strip()
        if value:
            filters[key] = f"{filters[key]} {value}".strip() if key in filters else value
    return filters, " ".join(part.strip() for part in remainder_parts if part.strip())


def parse_query(text: str, *, expand: bool = True) -> ParsedQuery:
    """Normalise, parse, and (optionally) expand a natural-language query."""

    original = (text or "").strip()
    filters, remainder = _split_field_filters(original)
    free_text = remainder if remainder else (original if not filters else "")

    searchable = " ".join(
        part for part in [free_text, *(value for key, value in filters.items() if key != "year")] if part
    ).strip()
    normalized = fold(searchable or original)

    doi = normalize_doi(filters.get("doi") or original)
    paper_id = ""
    for candidate in [filters.get("paper_id", ""), *original.split()]:
        token = candidate.strip().strip(",.;:")
        if PAPER_ID_RE.match(token):
            paper_id = token.upper()
            break

    year_source = f"{original} {filters.get('year', '')}"
    years = tuple(dict.fromkeys(match.group(0) for match in _YEAR_RE.finditer(year_source)))

    literal_tokens = tuple(dict.fromkeys(tokenize(searchable or original)))

    concept_matches: tuple[ConceptMatch, ...] = ()
    expansions: dict[str, tuple[str, ...]] = {}
    phenomenon_tokens: set[str] = set()
    boosted: dict[str, str] = {}

    if expand:
        concept_matches = match_concepts(searchable or original)
        expansions = expansion_terms(concept_matches, exclude=set())
        for match in concept_matches:
            if match.facet is not Facet.PHENOMENON:
                continue
            for token in tokenize(match.matched_term):
                phenomenon_tokens.add(token)
                boosted[token] = match.key

    terms: list[QueryTerm] = []
    for token in literal_tokens:
        if token in phenomenon_tokens:
            terms.append(
                QueryTerm(
                    token=token,
                    weight=PHENOMENON_WEIGHT,
                    source="literal",
                    concept=boosted.get(token),
                )
            )
        else:
            terms.append(QueryTerm(token=token, weight=1.0, source="literal"))

    if expand:
        seen = set(literal_tokens)
        for key, added in expansions.items():
            concept_facet = next(
                (match.facet for match in concept_matches if match.key == key), Facet.CONTEXT
            )
            weight = EXPANSION_WEIGHT * (
                PHENOMENON_WEIGHT if concept_facet is Facet.PHENOMENON else 1.0
            )
            for surface in added:
                for token in dict.fromkeys(tokenize(surface)):
                    if token in seen:
                        continue
                    seen.add(token)
                    terms.append(
                        QueryTerm(
                            token=token,
                            weight=weight,
                            source="expansion",
                            concept=key,
                            surface=surface,
                        )
                    )

    author_candidates: tuple[str, ...] = ()
    if "author" in filters:
        author_candidates = tuple(normalize_person_query(filters["author"]))

    return ParsedQuery(
        original=original,
        normalized=normalized,
        field_filters=filters,
        free_text=free_text,
        literal_tokens=literal_tokens,
        terms=tuple(terms),
        concept_matches=concept_matches,
        expansions=expansions,
        concept_topics=topics_for_concepts(concept_matches),
        doi=doi,
        paper_id=paper_id,
        years=years,
        author_candidates=author_candidates,
    )


__all__ = [
    "EXPANSION_WEIGHT",
    "PAPER_ID_RE",
    "PHENOMENON_WEIGHT",
    "ParsedQuery",
    "QueryTerm",
    "parse_query",
]
