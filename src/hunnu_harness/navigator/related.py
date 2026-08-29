"""WORK-level relatedness.

Similarity is computed between logical works, so a paper held as four physical
files can never appear four times: there is nothing in this module that could
produce a duplicate, because a version is never a candidate.

The score is a sum of named, separately reported components.  A caller can see
that two papers are related because they share two curated subtopics and a
methodology vocabulary, rather than being handed a cosine and asked to trust it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .catalog import PaperWork
from .query import QueryTerm
from .search import PaperNavigator
from .tokenize import display_terms, fold, tokenize


#: Component weights.  Curated topics dominate because they are hand-assigned
#: and dense (337 assignments over 179 works, none empty); lexical similarity is
#: the tie-breaker, not the driver.
WEIGHT_SUBTOPIC = 3.0
WEIGHT_DOMAIN = 1.0
WEIGHT_KEYWORD = 1.5
WEIGHT_AUTHOR = 2.0
WEIGHT_JOURNAL = 0.5
WEIGHT_LEXICAL = 1.0

#: Cap on the lexical component so a long shared title cannot swamp topics.
LEXICAL_CAP = 6.0


@dataclass
class RelatedWork:
    work: PaperWork
    score: float
    shared_subtopics: tuple[str, ...]
    shared_domains: tuple[str, ...]
    shared_keywords: tuple[str, ...]
    shared_authors: tuple[str, ...]
    same_journal: bool
    lexical_score: float
    lexical_terms: tuple[str, ...]

    def reason(self) -> str:
        parts: list[str] = []
        if self.shared_subtopics:
            parts.append("subtopics " + ", ".join(f"「{item}」" for item in self.shared_subtopics))
        if self.shared_domains:
            parts.append("domains " + ", ".join(f"「{item}」" for item in self.shared_domains))
        if self.shared_authors:
            parts.append("shared author " + ", ".join(f"「{item}」" for item in self.shared_authors))
        if self.shared_keywords:
            parts.append("keywords " + ", ".join(f"「{item}」" for item in self.shared_keywords[:4]))
        if self.same_journal:
            parts.append(f"same journal 「{self.work.journal}」")
        if self.lexical_terms:
            shown = display_terms(self.lexical_terms)
            if shown:
                parts.append("shared terms " + ", ".join(f"「{item}」" for item in shown))
        return "; ".join(parts) if parts else "weak overlap"

    def as_dict(self, resolution: Any) -> dict[str, Any]:
        preferred = resolution.preferred
        return {
            "paper_id": self.work.paper_id,
            "title": self.work.title,
            "authors": list(self.work.authors),
            "year": self.work.year,
            "journal": self.work.journal,
            "doi": self.work.doi,
            "topics": list(self.work.topic_labels),
            "relatedness_score": round(self.score, 4),
            "relatedness_reason": self.reason(),
            "matched_by": {
                "shared_subtopics": list(self.shared_subtopics),
                "shared_domains": list(self.shared_domains),
                "shared_keywords": list(self.shared_keywords),
                "shared_authors": list(self.shared_authors),
                "same_journal": self.same_journal,
                "lexical_score": round(self.lexical_score, 4),
                "lexical_terms": list(self.lexical_terms),
            },
            "fulltext_status": resolution.fulltext_status.value,
            "preferred_version": preferred.as_dict() if preferred else None,
        }


class RelatedWorkFinder:
    """Rank other WORKS by their overlap with one seed WORK."""

    def __init__(self, navigator: PaperNavigator) -> None:
        self.navigator = navigator

    def find(self, paper_id: str, *, top: int = 10) -> dict[str, Any]:
        snapshot = self.navigator.snapshot
        seed = snapshot.by_id(paper_id)
        payload = self.navigator.envelope()
        if seed is None:
            payload.update(
                {
                    "status": "NOT_IN_LIBRARY",
                    "paper_id": str(paper_id).upper(),
                    "reason": "no catalog record with this paper_id",
                }
            )
            return payload

        lexical = self._lexical_scores(seed)
        seed_subtopics = set(seed.subtopics)
        seed_domains = set(seed.domains)
        seed_keywords = {fold(item) for item in seed.keywords if fold(item)}
        seed_authors = set(seed.normalized_authors)
        seed_journal = fold(seed.journal)

        candidates: list[RelatedWork] = []
        for work in snapshot.works:
            if work.paper_id == seed.paper_id:
                continue
            shared_subtopics = tuple(sorted(seed_subtopics & set(work.subtopics)))
            shared_domains = tuple(sorted(seed_domains & set(work.domains)))
            work_keywords = {fold(item): item for item in work.keywords if fold(item)}
            shared_keywords = tuple(
                sorted(work_keywords[key] for key in (seed_keywords & set(work_keywords)))
            )
            shared_authors = tuple(
                sorted(
                    original
                    for original, normalized in zip(work.authors, work.normalized_authors)
                    if normalized in seed_authors
                )
            )
            same_journal = bool(seed_journal) and seed_journal == fold(work.journal)
            lexical_score, lexical_terms = lexical.get(work.paper_id, (0.0, ()))
            capped_lexical = min(LEXICAL_CAP, lexical_score)

            score = (
                WEIGHT_SUBTOPIC * len(shared_subtopics)
                + WEIGHT_DOMAIN * len(shared_domains)
                + WEIGHT_KEYWORD * len(shared_keywords)
                + WEIGHT_AUTHOR * len(shared_authors)
                + WEIGHT_JOURNAL * float(same_journal)
                + WEIGHT_LEXICAL * capped_lexical
            )
            if score <= 0.0:
                continue
            candidates.append(
                RelatedWork(
                    work=work,
                    score=score,
                    shared_subtopics=shared_subtopics,
                    shared_domains=shared_domains,
                    shared_keywords=shared_keywords,
                    shared_authors=shared_authors,
                    same_journal=same_journal,
                    lexical_score=capped_lexical,
                    lexical_terms=lexical_terms,
                )
            )

        candidates.sort(key=lambda item: (-item.score, item.work.paper_id))
        selected = candidates[:top]
        seed_resolution = self.navigator.resolution_for(seed)
        payload.update(
            {
                "status": "FOUND",
                "seed": {
                    **seed.identity(),
                    "topics": list(seed.topic_labels),
                    "keywords": list(seed.keywords),
                    "fulltext_status": seed_resolution.fulltext_status.value,
                },
                "result_count": len(selected),
                "results": [
                    item.as_dict(self.navigator.resolution_for(item.work)) for item in selected
                ],
            }
        )
        return payload

    def _lexical_scores(self, seed: PaperWork) -> dict[str, tuple[float, tuple[str, ...]]]:
        """Score every other work against the seed's own metadata text.

        Reuses the same BM25F index the search pipeline uses, so relatedness and
        search agree about what a term is worth.
        """

        index = self.navigator._metadata_index()  # noqa: SLF001 - same package, one owner
        text = " ".join([seed.title, " ".join(seed.keywords), " ".join(seed.subtopics)])
        terms = [
            QueryTerm(token=token, weight=1.0, source="literal")
            for token in dict.fromkeys(tokenize(text))
        ]
        if not terms:
            return {}
        scored = index.score(terms)
        if not scored:
            return {}
        top_score = max(item.score for item in scored) or 1.0
        result: dict[str, tuple[float, tuple[str, ...]]] = {}
        for item in scored:
            if item.doc_id == seed.paper_id:
                continue
            normalised = item.score / top_score
            terms_hit = tuple(dict.fromkeys(record.token for record in item.matches[:6]))
            result[item.doc_id] = (normalised * LEXICAL_CAP, terms_hit)
        return result


__all__ = [
    "LEXICAL_CAP",
    "RelatedWork",
    "RelatedWorkFinder",
    "WEIGHT_AUTHOR",
    "WEIGHT_DOMAIN",
    "WEIGHT_JOURNAL",
    "WEIGHT_KEYWORD",
    "WEIGHT_LEXICAL",
    "WEIGHT_SUBTOPIC",
]
