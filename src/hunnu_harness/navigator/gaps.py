"""Local coverage analysis -- deliberately not a novelty claim.

The distinction this module refuses to blur: "the local library holds nothing on
this" and "the literature contains nothing on this" are different statements,
and only the first is knowable from 179 works on one disk.  Every response
therefore carries an explicit scope statement, and the vocabulary avoids
"gap in the literature" entirely in favour of "under-covered locally".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lexicon import CONCEPTS_BY_KEY, Facet, RelevanceRole
from .query import parse_query
from .search import PaperNavigator


SCOPE_STATEMENT = (
    "This is a coverage analysis of the local Global Paper Library only. "
    "Absence here means the corpus does not hold such a paper; it is NOT evidence "
    "that the research literature lacks one, and it must never be reported as novelty."
)

#: A concept with fewer local works than this is reported as thinly covered.
THIN_COVERAGE = 3

#: Below this many works, coverage analysis is refused outright: on a
#: five-work library "everything is a gap" is a fact about the library's size,
#: not about anything a researcher should act on, and a technically-correct
#: but misleading answer is worse than a refusal.
MIN_WORKS_FOR_COVERAGE = 30

#: Between the floor and this ceiling the analysis runs, but carries an
#: explicit small-library warning.
SMALL_LIBRARY_CEILING = 100


@dataclass
class ConceptCoverage:
    concept: str
    facet: Facet
    matched_term: str
    work_count: int
    example_paper_ids: tuple[str, ...]
    taxonomy_topics: tuple[str, ...]

    @property
    def level(self) -> str:
        if self.work_count == 0:
            return "ABSENT"
        if self.work_count < THIN_COVERAGE:
            return "THIN"
        return "COVERED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept,
            "facet": self.facet.value,
            "matched_term": self.matched_term,
            "local_work_count": self.work_count,
            "coverage": self.level,
            "example_paper_ids": list(self.example_paper_ids),
            "taxonomy_topics": list(self.taxonomy_topics),
        }


class CoverageAnalyzer:
    """Report what the local corpus does and does not hold for a question."""

    def __init__(self, navigator: PaperNavigator) -> None:
        self.navigator = navigator

    def analyze(self, query: str, *, top: int = 25) -> dict[str, Any]:
        works_in_library = self.navigator.snapshot.work_count
        if works_in_library < MIN_WORKS_FOR_COVERAGE:
            payload = self.navigator.envelope()
            payload.update(
                {
                    "status": "REFUSED_LIBRARY_TOO_SMALL",
                    "scope": SCOPE_STATEMENT,
                    "query": query,
                    "works_in_library": works_in_library,
                    "minimum_works_for_coverage": MIN_WORKS_FOR_COVERAGE,
                    "why_refused": (
                        f"This library holds {works_in_library} works. On a corpus this "
                        "small, coverage analysis reports 'under-covered' almost "
                        "everywhere, and that pattern reflects the size of the library, "
                        "not the state of any research area. A technically correct but "
                        "misleading answer is worse than this refusal."
                    ),
                    "how_to_proceed": (
                        "Accumulate the corpus first: acquire validated full texts with "
                        "the acquisition pipeline (hunnu-harness acquire / the live-* "
                        "commands) or import individual PDFs via library-stage + "
                        f"library-import. Coverage analysis answers from "
                        f"{MIN_WORKS_FOR_COVERAGE} works, and stops warning about "
                        f"library size above {SMALL_LIBRARY_CEILING}."
                    ),
                }
            )
            return payload

        parsed = parse_query(query)
        search = self.navigator.search(query, top=top, use_fulltext=False)
        results = search["results"]

        coverage = [self._concept_coverage(key) for key in parsed.concept_keys()]
        role_counts: dict[str, int] = {}
        for row in results:
            role_counts[row["relevance_role"]] = role_counts.get(row["relevance_role"], 0) + 1

        pairs = self._pair_coverage(parsed, coverage)
        topic_counts = self._topic_counts()

        recommended = [
            {
                "paper_id": row["paper_id"],
                "title": row["title"],
                "relevance_role": row["relevance_role"],
                "why": row["relevance_reason"],
                "fulltext_status": row["fulltext_status"],
            }
            for row in results[:10]
        ]

        payload = self.navigator.envelope()
        payload.update(
            {
                "status": "ANALYZED",
                "scope": SCOPE_STATEMENT,
                "query": query,
                "query_normalization": parsed.audit(),
                "library_coverage": {
                    "works_in_library": self.navigator.snapshot.work_count,
                    "works_recalled": len(results),
                    "roles_present": dict(sorted(role_counts.items())),
                    "missing_roles": [
                        role.value
                        for role in (
                            RelevanceRole.CORE,
                            RelevanceRole.MECHANISM,
                            RelevanceRole.OUTCOME,
                            RelevanceRole.METHOD,
                        )
                        if role.value not in role_counts
                    ],
                },
                "concept_coverage": [item.as_dict() for item in coverage],
                "under_covered_connections": pairs,
                "topic_coverage": topic_counts,
                "recommended_papers_to_inspect": recommended,
                "interpretation_guard": (
                    "Report these as local corpus coverage facts. To make any claim about "
                    "the state of the literature, run the acquisition/search pipeline "
                    "against external databases first."
                ),
            }
        )
        if works_in_library <= SMALL_LIBRARY_CEILING:
            payload["small_library_warning"] = (
                f"This library holds {works_in_library} works "
                f"(warning band: {MIN_WORKS_FOR_COVERAGE}-{SMALL_LIBRARY_CEILING}). "
                "Coverage judgments over a corpus this small are dominated by what "
                "happens to have been acquired; treat every 'under-covered' line as "
                "a fact about this collection, and weigh it accordingly."
            )
        return payload

    def _concept_coverage(self, key: str) -> ConceptCoverage:
        concept = CONCEPTS_BY_KEY[key]
        terms = concept.folded_terms()
        topic_names = {name for name in concept.topics}
        hits: list[str] = []
        for work in self.navigator.snapshot.works:
            haystack = " ".join(
                [
                    work.normalized_title,
                    " ".join(work.topic_labels),
                    " ".join(work.keywords),
                ]
            ).casefold()
            if topic_names and any(name in haystack for name in topic_names):
                hits.append(work.paper_id)
                continue
            if any(term and term in haystack for term in terms):
                hits.append(work.paper_id)
        return ConceptCoverage(
            concept=key,
            facet=concept.facet,
            matched_term=terms[0] if terms else key,
            work_count=len(hits),
            example_paper_ids=tuple(sorted(hits)[:5]),
            taxonomy_topics=concept.topics,
        )

    def _pair_coverage(self, parsed: Any, coverage: list[ConceptCoverage]) -> list[dict[str, Any]]:
        """Which concept *combinations* the local corpus does not co-cover.

        A question is usually a link between two constructs; a library can hold
        both ends and still hold nothing that joins them.  That joint absence is
        the only kind of "gap" this module is willing to name.
        """

        by_key = {item.concept: item for item in coverage}
        phenomena = [item for item in coverage if item.facet is Facet.PHENOMENON]
        others = [item for item in coverage if item.facet in (Facet.MECHANISM, Facet.OUTCOME)]
        pairs: list[dict[str, Any]] = []
        for left in phenomena:
            for right in others:
                joint = self._joint_works(left, right)
                pairs.append(
                    {
                        "connection": f"{left.concept} × {right.concept}",
                        "left_local_works": left.work_count,
                        "right_local_works": right.work_count,
                        "joint_local_works": len(joint),
                        "joint_paper_ids": sorted(joint)[:8],
                        "assessment": (
                            "NO_LOCAL_WORK_JOINS_THESE"
                            if not joint
                            else ("THINLY_JOINED" if len(joint) < THIN_COVERAGE else "JOINED")
                        ),
                    }
                )
        pairs.sort(key=lambda item: (item["joint_local_works"], item["connection"]))
        _ = by_key
        return pairs

    def _joint_works(self, left: ConceptCoverage, right: ConceptCoverage) -> set[str]:
        return set(self._works_for(left.concept)) & set(self._works_for(right.concept))

    def _works_for(self, key: str) -> list[str]:
        concept = CONCEPTS_BY_KEY[key]
        terms = concept.folded_terms()
        topic_names = set(concept.topics)
        hits: list[str] = []
        for work in self.navigator.snapshot.works:
            haystack = " ".join(
                [work.normalized_title, " ".join(work.topic_labels), " ".join(work.keywords)]
            ).casefold()
            if (topic_names and any(name in haystack for name in topic_names)) or any(
                term and term in haystack for term in terms
            ):
                hits.append(work.paper_id)
        return hits

    def _topic_counts(self) -> dict[str, Any]:
        domains: dict[str, int] = {}
        subtopics: dict[str, int] = {}
        for work in self.navigator.snapshot.works:
            for assignment in work.topics:
                if assignment.domain:
                    domains[assignment.domain] = domains.get(assignment.domain, 0) + 1
                if assignment.subtopic:
                    subtopics[assignment.subtopic] = subtopics.get(assignment.subtopic, 0) + 1
        return {
            "domains": dict(sorted(domains.items(), key=lambda item: (-item[1], item[0]))),
            "subtopics": dict(sorted(subtopics.items(), key=lambda item: (-item[1], item[0]))),
        }


__all__ = [
    "ConceptCoverage",
    "CoverageAnalyzer",
    "MIN_WORKS_FOR_COVERAGE",
    "SCOPE_STATEMENT",
    "SMALL_LIBRARY_CEILING",
    "THIN_COVERAGE",
]
