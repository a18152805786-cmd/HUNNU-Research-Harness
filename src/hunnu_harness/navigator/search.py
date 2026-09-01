"""The two-stage WORK-first retrieval pipeline.

Stage 1 recalls WORKS from curated metadata; Stage 2 refines only the Stage-1
survivors with full-text passages.  Ranking never sees a physical file, which is
what keeps 192 versions from becoming 192 candidates -- deduplication is not a
post-processing step here, it is a property of the data model.

Every result carries the evidence that produced it.  A score with no explanation
is not a result this module is willing to emit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..literature.models import UNKNOWN
from ..literature.normalization import normalize_doi, normalize_person, normalize_title
from .catalog import CatalogReader, CatalogSnapshot, PaperWork
from .fulltext import Chunk
from .index import FullTextIndex, IndexStatus, NavigatorIndex
from .lexicon import CONCEPTS_BY_KEY, FACET_TO_ROLE, Facet, RelevanceRole
from .query import ParsedQuery, QueryTerm, parse_query
from .ranking import (
    BM25FIndex,
    DEFAULT_FIELD_WEIGHTS,
    MatchRecord,
    ScoredDocument,
    build_field_document,
)
from .resolver import FullTextStatus, PreferredVersionResolver, VersionResolution
from .tokenize import contains_cjk, display_terms, fold, normalize_person_query, tokenize


RESULT_SCHEMA_VERSION = "navigator-0.1"

#: How many Stage-1 candidates are handed to the full-text stage.
DEFAULT_RERANK_DEPTH = 40

#: Stage 2 can add at most this fraction of the top Stage-1 score, so a single
#: lucky page can refine an ordering but never overturn curated metadata.
PASSAGE_BOOST = 0.45

#: Maximum passages returned per work.
MAX_PASSAGES = 3

#: A title match contributing at least this share of a result's score counts as
#: a direct answer when the query names no phenomenon concept.
TITLE_DOMINANCE = 0.40

#: How much a concept hit in each field counts toward choosing a result's role.
#: A construct named in the title is what the paper is about; one that appears
#: only in a full-text passage is a mention.
FIELD_EVIDENCE_WEIGHT: dict[str, float] = {
    "title": 3.0,
    "topics": 2.0,
    "keywords": 2.0,
    "journal": 1.0,
    "fulltext": 1.0,
}

_PASSAGE_PREVIEW_CHARS = 320


@dataclass(frozen=True)
class ConceptEvidence:
    concept: str
    facet: Facet
    field_name: str
    term: str

    def as_dict(self) -> dict[str, str]:
        return {
            "concept": self.concept,
            "facet": self.facet.value,
            "field": self.field_name,
            "term": self.term,
        }


@dataclass(frozen=True)
class MatchedPassage:
    chunk: Chunk
    score: float
    terms: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        text = self.chunk.text
        preview = text if len(text) <= _PASSAGE_PREVIEW_CHARS else text[:_PASSAGE_PREVIEW_CHARS].rstrip() + "…"
        return {
            "chunk_id": self.chunk.chunk_id,
            "paper_id": self.chunk.paper_id,
            "version_sha256": self.chunk.version_sha256,
            "managed_path": self.chunk.managed_path,
            "page": self.chunk.page,
            "ordinal": self.chunk.ordinal,
            "matched_terms": list(self.terms),
            "text": preview,
            "text_sha256": self.chunk.text_sha256,
        }


@dataclass
class SearchResult:
    work: PaperWork
    resolution: VersionResolution
    score: float
    stage1_score: float
    passage_score: float
    matches: tuple[MatchRecord, ...]
    concept_evidence: tuple[ConceptEvidence, ...]
    passages: tuple[MatchedPassage, ...]
    role: RelevanceRole
    reason: str
    secondary_roles: tuple[RelevanceRole, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        preferred = self.resolution.preferred
        return {
            "paper_id": self.work.paper_id,
            "title": self.work.title,
            "authors": list(self.work.authors),
            "first_author": self.work.first_author,
            "year": self.work.year,
            "journal": self.work.journal,
            "doi": self.work.doi,
            "topics": list(self.work.topic_labels),
            "keywords": list(self.work.keywords),
            "relevance_score": round(self.score, 4),
            "relevance_role": self.role.value,
            "secondary_roles": [role.value for role in self.secondary_roles],
            "relevance_reason": self.reason,
            "matched_by": [record.as_dict() for record in self.matches[:12]],
            "concept_evidence": [item.as_dict() for item in self.concept_evidence],
            "fulltext_status": self.resolution.fulltext_status.value,
            "preferred_version": preferred.as_dict() if preferred else None,
            "available_versions": [item.as_dict() for item in self.resolution.versions],
            "matched_passages": [passage.as_dict() for passage in self.passages],
            "notes_path": self.work.notes_path,
            "score_breakdown": {
                "metadata": round(self.stage1_score, 4),
                "fulltext": round(self.passage_score, 4),
            },
        }


@dataclass
class LookupMatch:
    work: PaperWork
    resolution: VersionResolution
    matched_on: str
    confidence: str

    def as_dict(self) -> dict[str, Any]:
        preferred = self.resolution.preferred
        return {
            "paper_id": self.work.paper_id,
            "title": self.work.title,
            "authors": list(self.work.authors),
            "year": self.work.year,
            "journal": self.work.journal,
            "doi": self.work.doi,
            "topics": list(self.work.topic_labels),
            "matched_on": self.matched_on,
            "confidence": self.confidence,
            "fulltext_status": self.resolution.fulltext_status.value,
            "preferred_version": preferred.as_dict() if preferred else None,
            "available_versions": [item.as_dict() for item in self.resolution.versions],
        }


class PaperNavigator:
    """Read-only retrieval and navigation over the frozen paper library."""

    def __init__(
        self,
        *,
        snapshot: CatalogSnapshot | None = None,
        reader: CatalogReader | None = None,
        index: NavigatorIndex | None = None,
        resolver: PreferredVersionResolver | None = None,
        reranker: Callable[[ParsedQuery, Sequence[SearchResult]], Sequence[SearchResult]] | None = None,
    ) -> None:
        self._reader = reader or CatalogReader()
        self._snapshot = snapshot if snapshot is not None else self._reader.load()
        self._index = index or NavigatorIndex()
        self.resolver = resolver or PreferredVersionResolver()
        # Optional semantic stage.  Absent by default and absent-safe: nothing
        # in the pipeline calls out to a service, so no service can break it.
        self.reranker = reranker
        self._bm25: BM25FIndex | None = None
        self._resolutions: dict[str, VersionResolution] = {}
        self._fulltext: FullTextIndex | None = None
        self._index_status: IndexStatus | None = None
        self._index_detail: dict[str, Any] = {}
        # Chunk tokenisation is the dominant cost of Stage 2 and the chunks do
        # not change while the process lives, so tokenise each one once.
        self._chunk_documents: dict[str, Any] = {}

    # -- shared state ------------------------------------------------------

    @property
    def snapshot(self) -> CatalogSnapshot:
        return self._snapshot

    @property
    def index(self) -> NavigatorIndex:
        return self._index

    def resolution_for(self, work: PaperWork) -> VersionResolution:
        cached = self._resolutions.get(work.paper_id)
        if cached is None:
            cached = self.resolver.resolve(work)
            self._resolutions[work.paper_id] = cached
        return cached

    def _metadata_index(self) -> BM25FIndex:
        if self._bm25 is None:
            documents = [
                build_field_document(
                    work.paper_id,
                    {
                        "title": work.title,
                        "topics": " ".join(
                            f"{assignment.domain} {assignment.subtopic}" for assignment in work.topics
                        ),
                        "keywords": " ".join(work.keywords),
                        "authors": " ".join([*work.authors, work.first_author]),
                        "journal": work.journal,
                        "human_name": work.human_readable_name,
                    },
                )
                for work in self._snapshot.works
            ]
            self._bm25 = BM25FIndex(documents, field_weights=DEFAULT_FIELD_WEIGHTS)
        return self._bm25

    def index_status(self) -> tuple[IndexStatus, dict[str, Any]]:
        """The single authoritative index state for this process.

        Every Navigator command reports what this returns, whether or not it
        happens to need the full-text layer.  The bug this replaces: the status
        was read from a lazily-populated cache that only ``search()`` and
        ``fulltext()`` ever filled, and an unfilled cache was reported as
        ``ABSENT`` -- so ``paper-lookup`` claimed there was no index while
        ``paper-search`` on the same index in the same directory said FRESH.
        A not-yet-loaded cache is not evidence about the filesystem.
        """

        if self._index_status is None:
            self._index_status, self._index_detail = self._index.resolved_status(self._snapshot)
        return self._index_status, self._index_detail

    def _load_fulltext(self) -> tuple[FullTextIndex, IndexStatus, dict[str, Any]]:
        if self._fulltext is None:
            self._fulltext, self._index_status, self._index_detail = self._index.load_fulltext(self._snapshot)
        assert self._index_status is not None
        return self._fulltext, self._index_status, self._index_detail

    def envelope(self, *, index_status: IndexStatus | None = None) -> dict[str, Any]:
        """The header every machine response carries."""

        degraded = self._snapshot.degraded_dicts()
        status = index_status if index_status is not None else self.index_status()[0]
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "index_status": status.value,
            "works_indexed": self._snapshot.work_count,
            "physical_versions": self._snapshot.version_count,
            "topic_assignments": self._snapshot.topic_assignment_count,
            "catalog_status": "PARTIAL" if degraded else "COMPLETE",
            "degraded": degraded,
        }

    # -- capability B: structured lookup -----------------------------------

    def lookup(self, identity: str, *, limit: int = 10) -> dict[str, Any]:
        """Resolve an explicit identity: DOI, paper_id, title, or author names."""

        parsed = parse_query(identity, expand=False)
        matches: list[LookupMatch] = []
        seen: set[str] = set()

        def add(work: PaperWork, matched_on: str, confidence: str) -> None:
            if work.paper_id in seen:
                return
            seen.add(work.paper_id)
            matches.append(
                LookupMatch(
                    work=work,
                    resolution=self.resolution_for(work),
                    matched_on=matched_on,
                    confidence=confidence,
                )
            )

        if parsed.paper_id:
            work = self._snapshot.by_id(parsed.paper_id)
            if work is not None:
                add(work, "paper_id", "EXACT")

        if parsed.doi != UNKNOWN:
            work = self._snapshot.by_doi(parsed.doi)
            if work is not None:
                add(work, "doi", "EXACT")

        title_filter = parsed.field_filters.get("title")
        for candidate in self._snapshot.by_normalized_title(title_filter or identity):
            add(candidate, "title", "EXACT")

        if parsed.author_candidates or "author" in parsed.field_filters:
            for work in self._author_matches(parsed.author_candidates or [identity]):
                add(work, "author", "AUTHOR_SET")

        journal_filter = parsed.field_filters.get("journal")
        if journal_filter:
            folded = fold(journal_filter)
            for work in self._snapshot.works:
                if folded and folded in fold(work.journal):
                    add(work, "journal", "FIELD")

        exact_count = len(matches)
        requested_exact = self._requests_exact_identity(parsed)

        if not matches and not requested_exact:
            # A bare identity string may be a set of author names rather than a
            # title; try that before falling back to lexical similarity.
            for work in self._author_matches(normalize_person_query(identity)):
                add(work, "author", "AUTHOR_SET")

        if not matches and not requested_exact:
            # Last resort: an unexpanded lexical search, so a near-miss title
            # still returns the right work rather than nothing.  Deliberately
            # NOT reached when the caller asked for an exact identity -- an
            # unknown DOI must come back as NOT_IN_LIBRARY, never as the
            # closest-looking paper in the library.
            fallback = self.search(identity, top=limit, use_fulltext=False, expand=False)
            for result in fallback["results"][:limit]:
                work = self._snapshot.by_id(result["paper_id"])
                if work is not None:
                    add(work, "lexical", "APPROXIMATE")

        if not matches:
            status = "NOT_IN_LIBRARY"
        elif exact_count:
            status = "FOUND"
        else:
            status = "FOUND_APPROXIMATE"

        payload = self.envelope()
        payload.update(
            {
                "query": identity,
                "query_normalization": parsed.audit(),
                "match_count": len(matches),
                "exact_match_count": exact_count,
                "exact_identity_requested": requested_exact,
                "status": status,
                "matches": [item.as_dict() for item in matches[:limit]],
            }
        )
        return payload

    @staticmethod
    def _requests_exact_identity(parsed: ParsedQuery) -> bool:
        """Did the caller name a key that either resolves or does not exist?

        A DOI, a ``paper_id``, or an explicit ``title:``/``doi:`` filter is a
        precise claim.  When such a claim misses, the honest answer is
        ``NOT_IN_LIBRARY``; returning the nearest neighbour would invite an
        agent to cite a paper the library does not hold.
        """

        if parsed.doi != UNKNOWN or parsed.paper_id:
            return True
        return bool({"doi", "paper_id", "title"} & set(parsed.field_filters))

    def _author_matches(self, candidates: Iterable[str]) -> list[PaperWork]:
        wanted = [normalize_person(name) for name in candidates if fold(name)]
        wanted = [name for name in wanted if name and name != UNKNOWN]
        if not wanted:
            return []
        scored: list[tuple[int, PaperWork]] = []
        for work in self._snapshot.works:
            hits = 0
            for name in wanted:
                for author in work.normalized_authors:
                    if name == author or (len(name) >= 2 and name in author) or (len(author) >= 2 and author in name):
                        hits += 1
                        break
            if hits:
                scored.append((hits, work))
        scored.sort(key=lambda item: (-item[0], item[1].paper_id))
        return [work for _hits, work in scored]

    # -- capability C: full-text availability ------------------------------

    def fulltext(self, paper_id: str) -> dict[str, Any]:
        work = self._snapshot.by_id(paper_id)
        payload = self.envelope()
        if work is None:
            payload.update(
                {
                    "status": "NOT_IN_LIBRARY",
                    "paper_id": str(paper_id).upper(),
                    "reason": "no catalog record with this paper_id",
                }
            )
            return payload

        resolution = self.resolution_for(work)
        fulltext_index, index_status, _detail = self._load_fulltext()
        payload = self.envelope(index_status=index_status)
        payload.update(
            {
                "status": "FOUND",
                "work": work.identity(),
                "topics": list(work.topic_labels),
                "canonical": {
                    "sha256": work.canonical_sha256,
                    "managed_path": work.canonical_managed_path,
                },
                "notes_path": work.notes_path,
                "text_index": {
                    "indexed": fulltext_index.covers(work.paper_id),
                    "chunk_count": len(fulltext_index.for_work(work.paper_id)),
                    "stale": work.paper_id in fulltext_index.stale_works,
                    "extraction": fulltext_index.manifest_entries.get(work.paper_id, {}).get(
                        "status", "NOT_INDEXED"
                    ),
                },
            }
        )
        payload.update(resolution.as_dict())
        return payload

    # -- capability A/E: search --------------------------------------------

    def search(
        self,
        query: str,
        *,
        top: int = 10,
        use_fulltext: bool = True,
        expand: bool = True,
        rerank_depth: int = DEFAULT_RERANK_DEPTH,
    ) -> dict[str, Any]:
        parsed = parse_query(query, expand=expand)
        stage1 = self._stage1(parsed)

        fulltext_index = FullTextIndex()
        # The reported state is a fact about the index on disk, not about
        # whether this particular call chose to use it.  ``--no-fulltext`` skips
        # Stage 2; it does not make the index disappear.
        index_status, index_detail = self.index_status()
        passages: dict[str, tuple[MatchedPassage, ...]] = {}
        passage_scores: dict[str, float] = {}

        if use_fulltext and stage1:
            fulltext_index, index_status, index_detail = self._load_fulltext()
            candidates = [scored.doc_id for scored in stage1[:rerank_depth]]
            passages, passage_scores = self._stage2(parsed, candidates, fulltext_index)
        elif use_fulltext:
            fulltext_index, index_status, index_detail = self._load_fulltext()

        top_stage1 = stage1[0].score if stage1 else 0.0
        results: list[SearchResult] = []
        for scored in stage1:
            work = self._snapshot.by_id(scored.doc_id)
            if work is None:
                continue
            work_passages = passages.get(work.paper_id, ())
            boost = PASSAGE_BOOST * top_stage1 * passage_scores.get(work.paper_id, 0.0)
            results.append(
                self._build_result(
                    work=work,
                    scored=scored,
                    parsed=parsed,
                    passages=work_passages,
                    stage1_score=scored.score,
                    passage_score=boost,
                )
            )

        results.sort(key=lambda item: (-item.score, item.work.paper_id))
        results = self._assign_roles(results, parsed)

        if self.reranker is not None:
            try:
                results = list(self.reranker(parsed, results))
            except Exception as exc:  # a rerank service must never break retrieval
                self._snapshot.degraded_dicts()
                index_detail = {
                    **index_detail,
                    "reranker_error": f"{type(exc).__name__}: {exc}; lexical ranking retained",
                }

        payload = self.envelope(index_status=index_status)
        payload.update(
            {
                "query": query,
                "query_normalization": parsed.audit(),
                "index_detail": index_detail,
                "stage2_applied": bool(passages),
                "candidates_ranked": len(stage1),
                "result_count": min(len(results), top),
                "results": [result.as_dict() for result in results[:top]],
            }
        )
        if self._snapshot.work_count == 0:
            # Zero results from zero works is not a retrieval outcome, and an
            # agent left to guess reads it as "nothing relevant exists".  Say
            # what is actually true: nothing has been acquired yet.
            payload["status"] = "EMPTY_LIBRARY"
            payload["empty_library_note"] = (
                "The Global Paper Library holds no works yet, so no query can "
                "match anything. This is the expected state of a fresh install, "
                "not a defect. The library fills as the acquisition pipeline "
                "archives validated full texts (hunnu-harness acquire / the "
                "live-* commands), or as individual PDFs pass library-stage + "
                "library-import."
            )
        return payload

    def _stage1(self, parsed: ParsedQuery) -> list[ScoredDocument]:
        index = self._metadata_index()
        terms = list(parsed.terms)
        # Curated topic names implied by the query's concepts are added as
        # explicit terms so a work carrying the right taxonomy label is recalled
        # even when its title uses different words.
        for topic in parsed.concept_topics:
            for token in dict.fromkeys(tokenize(topic)):
                terms.append(QueryTerm(token=token, weight=0.8, source="expansion", concept="topic", surface=topic))
        scored = index.score(terms)
        return self._apply_filters(scored, parsed)

    def _apply_filters(self, scored: list[ScoredDocument], parsed: ParsedQuery) -> list[ScoredDocument]:
        year_filter = {year for year in parsed.years} if "year" in parsed.field_filters else set()
        journal_filter = fold(parsed.field_filters.get("journal", ""))
        topic_filter = fold(parsed.field_filters.get("topic", ""))
        if not (year_filter or journal_filter or topic_filter):
            return scored
        kept: list[ScoredDocument] = []
        for item in scored:
            work = self._snapshot.by_id(item.doc_id)
            if work is None:
                continue
            if year_filter and work.year not in year_filter:
                continue
            if journal_filter and journal_filter not in fold(work.journal):
                continue
            if topic_filter and not any(topic_filter in fold(label) for label in work.topic_labels):
                continue
            kept.append(item)
        return kept

    def _stage2(
        self,
        parsed: ParsedQuery,
        candidates: Sequence[str],
        fulltext_index: FullTextIndex,
    ) -> tuple[dict[str, tuple[MatchedPassage, ...]], dict[str, float]]:
        chunk_documents = []
        chunk_by_id: dict[str, Chunk] = {}
        for paper_id in candidates:
            for chunk in fulltext_index.for_work(paper_id):
                chunk_by_id[chunk.chunk_id] = chunk
                document = self._chunk_documents.get(chunk.chunk_id)
                if document is None:
                    document = build_field_document(chunk.chunk_id, {"text": chunk.text})
                    self._chunk_documents[chunk.chunk_id] = document
                chunk_documents.append(document)
        if not chunk_documents:
            return {}, {}

        passage_index = BM25FIndex(chunk_documents, field_weights={"text": 1.0})
        scored_chunks = passage_index.score(list(parsed.terms))
        if not scored_chunks:
            return {}, {}

        best_score = scored_chunks[0].score or 1.0
        grouped: dict[str, list[MatchedPassage]] = {}
        for scored in scored_chunks:
            chunk = chunk_by_id.get(scored.doc_id)
            if chunk is None:
                continue
            terms = tuple(dict.fromkeys(record.token for record in scored.matches))
            grouped.setdefault(chunk.paper_id, []).append(
                MatchedPassage(chunk=chunk, score=scored.score, terms=terms)
            )

        passages: dict[str, tuple[MatchedPassage, ...]] = {}
        normalised: dict[str, float] = {}
        for paper_id, items in grouped.items():
            items.sort(key=lambda item: (-item.score, item.chunk.page, item.chunk.ordinal))
            passages[paper_id] = tuple(items[:MAX_PASSAGES])
            normalised[paper_id] = min(1.0, items[0].score / best_score) if best_score else 0.0
        return passages, normalised

    # -- result construction ------------------------------------------------

    def _build_result(
        self,
        *,
        work: PaperWork,
        scored: ScoredDocument,
        parsed: ParsedQuery,
        passages: tuple[MatchedPassage, ...],
        stage1_score: float,
        passage_score: float,
    ) -> SearchResult:
        evidence = self._concept_evidence(work, parsed, passages)
        matches = list(scored.matches)
        for passage in passages:
            for term in passage.terms[:3]:
                matches.append(
                    MatchRecord(
                        field_name=f"fulltext:page{passage.chunk.page}",
                        token=term,
                        weight=PASSAGE_BOOST,
                        contribution=passage_score / max(1, len(passages)),
                        source="fulltext",
                    )
                )
        return SearchResult(
            work=work,
            resolution=self.resolution_for(work),
            score=stage1_score + passage_score,
            stage1_score=stage1_score,
            passage_score=passage_score,
            matches=tuple(matches),
            concept_evidence=evidence,
            passages=passages,
            role=RelevanceRole.BACKGROUND,
            reason="",
        )

    def _concept_evidence(
        self,
        work: PaperWork,
        parsed: ParsedQuery,
        passages: tuple[MatchedPassage, ...],
    ) -> tuple[ConceptEvidence, ...]:
        """Which of the query's concepts this work actually carries, and where."""

        if not parsed.concept_matches:
            return ()
        haystacks: list[tuple[str, str]] = [
            ("title", fold(work.title)),
            ("topics", fold(" ".join(work.topic_labels))),
            ("keywords", fold(" ".join(work.keywords))),
            ("journal", fold(work.journal)),
        ]
        for passage in passages:
            haystacks.append((f"fulltext:page{passage.chunk.page}", fold(passage.chunk.text)))

        evidence: list[ConceptEvidence] = []
        seen: set[tuple[str, str]] = set()
        for key in parsed.concept_keys():
            concept = CONCEPTS_BY_KEY.get(key)
            if concept is None:
                continue
            topic_names = {fold(name) for name in concept.topics}
            for field_name, haystack in haystacks:
                if not haystack:
                    continue
                if field_name == "topics" and topic_names:
                    hit = next((name for name in topic_names if name and name in haystack), None)
                    if hit and (key, field_name) not in seen:
                        seen.add((key, field_name))
                        evidence.append(
                            ConceptEvidence(concept=key, facet=concept.facet, field_name=field_name, term=hit)
                        )
                        continue
                for term in concept.folded_terms():
                    if not term or len(term) < 2:
                        continue
                    if term in haystack and (key, field_name) not in seen:
                        seen.add((key, field_name))
                        evidence.append(
                            ConceptEvidence(concept=key, facet=concept.facet, field_name=field_name, term=term)
                        )
                        break
        return tuple(evidence)

    def _assign_roles(self, results: list[SearchResult], parsed: ParsedQuery) -> list[SearchResult]:
        has_phenomenon = Facet.PHENOMENON in parsed.facets
        finished: list[SearchResult] = []
        for result in results:
            role, secondary = self._role_for(result, has_phenomenon=has_phenomenon)
            finished.append(
                SearchResult(
                    work=result.work,
                    resolution=result.resolution,
                    score=result.score,
                    stage1_score=result.stage1_score,
                    passage_score=result.passage_score,
                    matches=result.matches,
                    concept_evidence=result.concept_evidence,
                    passages=result.passages,
                    role=role,
                    reason=self._reason(result, role),
                    secondary_roles=secondary,
                )
            )
        return finished

    @classmethod
    def _role_for(
        cls,
        result: SearchResult,
        *,
        has_phenomenon: bool,
    ) -> tuple[RelevanceRole, tuple[RelevanceRole, ...]]:
        """Choose the primary role by evidence strength, not by precedence.

        A paper can genuinely serve two roles -- "Audit effort and earnings
        management" is both a mechanism and an outcome study -- so the primary
        role is the facet with the strongest evidence and the others are
        reported as secondary rather than discarded.  Being *about* the
        phenomenon still wins outright: a paper on the subject of the question
        is CORE regardless of what else it also covers.
        """

        facets = {item.facet for item in result.concept_evidence}
        secondary = tuple(
            FACET_TO_ROLE[facet]
            for facet in (Facet.MECHANISM, Facet.OUTCOME, Facet.METHOD)
            if facet in facets
        )

        if Facet.PHENOMENON in facets:
            return RelevanceRole.CORE, secondary
        if not has_phenomenon and cls._title_dominant(result):
            return RelevanceRole.CORE, secondary

        strength: dict[Facet, float] = {}
        for item in result.concept_evidence:
            if item.facet not in (Facet.MECHANISM, Facet.OUTCOME, Facet.METHOD):
                continue
            strength[item.facet] = strength.get(item.facet, 0.0) + FIELD_EVIDENCE_WEIGHT.get(
                item.field_name.split(":", 1)[0], 1.0
            )
        if not strength:
            return RelevanceRole.BACKGROUND, ()

        # Deterministic: strongest evidence, then a stable facet order.
        order = {Facet.MECHANISM: 0, Facet.OUTCOME: 1, Facet.METHOD: 2}
        best = min(strength.items(), key=lambda item: (-item[1], order[item[0]]))[0]
        return FACET_TO_ROLE[best], tuple(role for role in secondary if role is not FACET_TO_ROLE[best])

    @staticmethod
    def _title_dominant(result: SearchResult) -> bool:
        total = sum(record.contribution for record in result.matches) or 0.0
        if total <= 0:
            return False
        title = sum(record.contribution for record in result.matches if record.field_name == "title")
        return (title / total) >= TITLE_DOMINANCE

    @staticmethod
    def _reason(result: SearchResult, role: RelevanceRole) -> str:
        """A sentence built from this result's own provenance, not a template."""

        parts: list[str] = []
        for item in result.concept_evidence[:3]:
            label = {
                "title": "题名" if contains_cjk(result.work.title) else "title",
                "topics": "topic",
                "keywords": "keyword",
                "journal": "journal",
            }.get(item.field_name, item.field_name)
            parts.append(f"{label}「{item.term}」→ {item.concept} ({item.facet.value.lower()})")

        if not parts:
            by_field: dict[str, list[str]] = {}
            for record in result.matches[:20]:
                by_field.setdefault(record.field_name, []).append(record.token)
            fields = [
                f"{name}「" + "/".join(display_terms(tokens, limit=3)) + "」"
                for name, tokens in list(by_field.items())[:3]
                if display_terms(tokens, limit=3)
            ]
            if fields:
                parts.append("lexical match on " + ", ".join(fields))

        if result.passages:
            pages = sorted({passage.chunk.page for passage in result.passages})
            parts.append("full text p." + ", ".join(str(page) for page in pages))

        detail = "; ".join(parts) if parts else "topic overlap only"
        return f"{role.value}: {detail}"


__all__ = [
    "DEFAULT_RERANK_DEPTH",
    "LookupMatch",
    "MAX_PASSAGES",
    "MatchedPassage",
    "PASSAGE_BOOST",
    "PaperNavigator",
    "RESULT_SCHEMA_VERSION",
    "SearchResult",
]
