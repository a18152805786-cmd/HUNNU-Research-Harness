"""Auditable cross-language concept matching backed by replaceable data.

The Navigator works offline and keeps every expansion visible to callers.  A
small JSON vocabulary is therefore easier to review, test, and replace than a
learned representation.  A distribution ships with a default vocabulary, but
an Output Root override may replace it wholesale or leave the vocabulary
empty.

Each configured concept declares a *facet*, which lets a research-question
query report roles (CORE / MECHANISM / OUTCOME / METHOD) instead of an
undifferentiated ranked list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Any

from .. import paths
from .tokenize import fold


class Facet(str, Enum):
    """What role a concept plays in a research question."""

    PHENOMENON = "PHENOMENON"
    MECHANISM = "MECHANISM"
    OUTCOME = "OUTCOME"
    METHOD = "METHOD"
    CONTEXT = "CONTEXT"


class RelevanceRole(str, Enum):
    CORE = "CORE"
    MECHANISM = "MECHANISM"
    OUTCOME = "OUTCOME"
    METHOD = "METHOD"
    BACKGROUND = "BACKGROUND"


FACET_TO_ROLE: dict[Facet, RelevanceRole] = {
    Facet.PHENOMENON: RelevanceRole.CORE,
    Facet.MECHANISM: RelevanceRole.MECHANISM,
    Facet.OUTCOME: RelevanceRole.OUTCOME,
    Facet.METHOD: RelevanceRole.METHOD,
    Facet.CONTEXT: RelevanceRole.BACKGROUND,
}


@dataclass(frozen=True)
class Concept:
    """One configured construct and its searchable surface forms."""

    key: str
    facet: Facet
    terms: tuple[str, ...]
    topics: tuple[str, ...] = ()

    def folded_terms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fold(term) for term in self.terms if fold(term)))


_DEFAULT_RESOURCE_NAME = "lexicon.default.json"
_UNSET = object()


def _invalid_lexicon(source: object, detail: str) -> ValueError:
    return ValueError(
        f"Invalid Navigator lexicon at {source}: {detail}. "
        "Repair it as JSON with a top-level 'concepts' array whose entries "
        "contain key, facet, terms, and topics, or remove the override to "
        "use the packaged default."
    )


def _read_json(source: Any) -> Any:
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise _invalid_lexicon(source, f"JSON parsing failed ({exc.msg})") from exc
    except (OSError, UnicodeError) as exc:
        raise _invalid_lexicon(source, f"the file could not be read ({exc})") from exc


def _string_list(value: Any, *, source: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _invalid_lexicon(source, f"'{field_name}' must be a JSON array of strings")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise _invalid_lexicon(source, f"'{field_name}' must contain non-empty strings only")
    return tuple(item.strip() for item in value)


def _concepts_from_payload(payload: Any, *, source: object) -> tuple[Concept, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("concepts"), list):
        raise _invalid_lexicon(source, "the top level must contain a 'concepts' array")

    concepts: list[Concept] = []
    seen_keys: set[str] = set()
    for index, item in enumerate(payload["concepts"]):
        if not isinstance(item, dict):
            raise _invalid_lexicon(source, f"concepts[{index}] must be an object")
        required = {"key", "facet", "terms", "topics"}
        missing = sorted(required - item.keys())
        if missing:
            raise _invalid_lexicon(source, f"concepts[{index}] is missing {', '.join(missing)}")
        key = item["key"]
        facet_value = item["facet"]
        if not isinstance(key, str) or not key.strip():
            raise _invalid_lexicon(source, f"concepts[{index}].key must be a non-empty string")
        normalized_key = key.strip()
        if normalized_key in seen_keys:
            raise _invalid_lexicon(source, f"duplicate concept key {normalized_key!r}")
        if not isinstance(facet_value, str):
            raise _invalid_lexicon(source, f"concepts[{index}].facet must be a string")
        try:
            facet = Facet(facet_value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in Facet)
            raise _invalid_lexicon(
                source,
                f"concepts[{index}].facet {facet_value!r} is invalid; expected one of {allowed}",
            ) from exc
        terms = _string_list(item["terms"], source=source, field_name=f"concepts[{index}].terms")
        if not terms:
            raise _invalid_lexicon(source, f"concepts[{index}].terms cannot be empty")
        topics = _string_list(item["topics"], source=source, field_name=f"concepts[{index}].topics")
        concepts.append(Concept(key=normalized_key, facet=facet, terms=terms, topics=topics))
        seen_keys.add(normalized_key)
    return tuple(concepts)


def load_concepts(
    *, override_path: Path | str | None = None, default_resource: Any = _UNSET
) -> tuple[Concept, ...]:
    """Load the user override, packaged default, or a valid empty vocabulary."""

    candidate = Path(override_path or paths.NAVIGATOR_LEXICON_JSON)
    if candidate.exists():
        return _concepts_from_payload(_read_json(candidate), source=candidate)

    resource = default_resource
    if resource is _UNSET:
        resource = resources.files(__package__).joinpath(_DEFAULT_RESOURCE_NAME)
    if resource is not None and resource.is_file():
        return _concepts_from_payload(_read_json(resource), source=resource)
    return ()


CONCEPTS: tuple[Concept, ...] = load_concepts()


CONCEPTS_BY_KEY: dict[str, Concept] = {concept.key: concept for concept in CONCEPTS}


def _build_lookup() -> tuple[dict[str, tuple[Concept, ...]], int]:
    lookup: dict[str, list[Concept]] = {}
    longest = 1
    for concept in CONCEPTS:
        for term in concept.folded_terms():
            lookup.setdefault(term, []).append(concept)
            longest = max(longest, len(term))
    return {term: tuple(items) for term, items in lookup.items()}, longest


_TERM_LOOKUP, _LONGEST_TERM = _build_lookup()


@dataclass(frozen=True)
class ConceptMatch:
    """One concept detected in a query, with the surface form that triggered it."""

    concept: Concept
    matched_term: str

    @property
    def key(self) -> str:
        return self.concept.key

    @property
    def facet(self) -> Facet:
        return self.concept.facet


def match_concepts(text: str | None) -> tuple[ConceptMatch, ...]:
    """Find every concept whose surface form occurs in *text*.

    Matching is substring-based over the folded query.  For Latin terms the
    match is additionally required to fall on a word boundary so that ``ai``
    does not fire inside ``said`` or ``domain``.  CJK has no such boundaries,
    so a substring match is the correct test there.
    """

    folded = fold(text)
    if not folded:
        return ()
    padded = f" {folded} "
    matches: list[ConceptMatch] = []
    seen: set[tuple[str, str]] = set()
    for term, concepts in _TERM_LOOKUP.items():
        if not term:
            continue
        if _is_latin_term(term):
            if f" {term} " not in padded and not _latin_boundary_hit(padded, term):
                continue
        elif term not in folded:
            continue
        for concept in concepts:
            key = (concept.key, term)
            if key in seen:
                continue
            seen.add(key)
            matches.append(ConceptMatch(concept=concept, matched_term=term))
    matches.sort(key=lambda item: (-len(item.matched_term), item.concept.key))
    return tuple(matches)


def _is_latin_term(term: str) -> bool:
    return all(ord(char) < 0x2E80 for char in term)


def _latin_boundary_hit(padded: str, term: str) -> bool:
    start = 0
    while True:
        position = padded.find(term, start)
        if position < 0:
            return False
        before = padded[position - 1] if position > 0 else " "
        after_index = position + len(term)
        after = padded[after_index] if after_index < len(padded) else " "
        if not before.isalnum() and not after.isalnum():
            return True
        start = position + 1


def expansion_terms(matches: tuple[ConceptMatch, ...], *, exclude: set[str] | None = None) -> dict[str, tuple[str, ...]]:
    """Return, per concept key, the surface forms not already in the query."""

    exclude = exclude or set()
    expansions: dict[str, tuple[str, ...]] = {}
    for match in matches:
        added = tuple(
            term
            for term in match.concept.folded_terms()
            if term not in exclude and term != match.matched_term
        )
        if added:
            expansions[match.concept.key] = added
    return expansions


def topics_for_concepts(matches: tuple[ConceptMatch, ...]) -> tuple[str, ...]:
    seen: list[str] = []
    for match in matches:
        for topic in match.concept.topics:
            if topic not in seen:
                seen.append(topic)
    return tuple(seen)


__all__ = [
    "CONCEPTS",
    "CONCEPTS_BY_KEY",
    "Concept",
    "ConceptMatch",
    "FACET_TO_ROLE",
    "Facet",
    "RelevanceRole",
    "expansion_terms",
    "match_concepts",
    "topics_for_concepts",
]
