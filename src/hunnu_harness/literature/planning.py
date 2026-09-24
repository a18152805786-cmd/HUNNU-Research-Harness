from __future__ import annotations

from dataclasses import dataclass

from .models import LiteratureSearchRequest
from .normalization import normalize_doi


@dataclass(frozen=True)
class PlannedQuery:
    query: str
    filters: dict[str, object]
    rationale: str


def source_journal_query(journal: str) -> str:
    """The planned query for one named journal (a 文献来源 search).

    Only a source that declares ``supports_restricted_search`` ever receives
    one: the workflow refuses a restricted request everywhere else.
    """

    return f'source:"{journal}"'


class LiteratureSearchPlanner:
    """Create a small, deterministic query set with an explicit hard cap."""

    def __init__(self, *, max_queries: int = 8):
        if max_queries < 1 or max_queries > 20:
            raise ValueError("max_queries must be between 1 and 20")
        self.max_queries = max_queries

    def plan(self, request: LiteratureSearchRequest) -> list[PlannedQuery]:
        filters: dict[str, object] = {
            "YearStart": request.year_start or "unknown",
            "YearEnd": request.year_end or "unknown",
            "PreferredLanguages": list(request.preferred_languages),
            "PreferredPublicationTypes": list(request.preferred_publication_types),
            "MaxResultsPerSource": request.max_results_per_source,
        }
        if request.restricted_search:
            filters["ResourceType"] = request.resource_type
            filters["SourceJournals"] = list(request.source_journals)
        candidates: list[PlannedQuery] = []

        if request.source_journals:
            # A one-box search searches one field.  Named journals are searched
            # by source, one query each; topic terms cannot be joined to that
            # query, so they are left to screening instead of being dropped
            # into the source search where they would match nothing.
            return [
                PlannedQuery(
                    source_journal_query(journal),
                    filters,
                    "Named journal searched by source (文献来源); topic terms are screened, not searched",
                )
                for journal in request.source_journals[: self.max_queries]
            ]

        for doi in request.dois:
            normalized = normalize_doi(doi)
            if normalized != "unknown":
                candidates.append(PlannedQuery(normalized, filters, "Exact DOI supplied by user"))

        for title in request.exact_titles:
            candidates.append(PlannedQuery(f'"{title}"', filters, "Exact title supplied by user"))

        for author in request.authors:
            candidates.append(PlannedQuery(f'author:"{author}"', filters, "Author supplied by user"))

        if request.keywords_en:
            primary = request.keywords_en[0]
            if len(request.keywords_en) == 1:
                candidates.append(PlannedQuery(f'"{primary}"', filters, "Primary English concept"))
            else:
                for secondary in request.keywords_en[1:]:
                    candidates.append(
                        PlannedQuery(
                            f'"{primary}" AND "{secondary}"',
                            filters,
                            "Bounded English concept pairing",
                        )
                    )

        if request.keywords_cn:
            primary_cn = request.keywords_cn[0]
            if len(request.keywords_cn) == 1:
                candidates.append(PlannedQuery(primary_cn, filters, "Primary Chinese concept"))
            else:
                for secondary_cn in request.keywords_cn[1:]:
                    candidates.append(
                        PlannedQuery(
                            f"{primary_cn} {secondary_cn}",
                            filters,
                            "Bounded Chinese concept pairing",
                        )
                    )

        if not candidates and request.research_question:
            candidates.append(
                PlannedQuery(request.research_question.strip(), filters, "Verbatim research question fallback")
            )

        unique: list[PlannedQuery] = []
        seen: set[str] = set()
        for item in candidates:
            key = item.query.casefold().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= self.max_queries:
                break
        return unique

