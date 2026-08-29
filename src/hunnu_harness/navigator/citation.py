"""Citation verification and the acquisition handoff.

The question this answers is narrow and useful: *does the Harness already hold
this cited paper, and where is its full text?*  It does not answer *does this
paper exist* -- that is the acquisition pipeline's job, and this module hands off
rather than guessing.

The failure mode worth engineering against is confident wrongness.  Returning
the nearest-looking paper for a citation the library does not hold would let an
agent cite a work it has never seen, so a match must clear an explicit evidence
bar: a DOI hit, an exact normalised title, or agreement on enough independent
fields (author, year, distinctive title terms) to be more than a coincidence.
Everything below that bar is reported as ``NOT_IN_LIBRARY`` with candidates
labelled as unverified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..literature.models import UNKNOWN
from ..literature.normalization import normalize_doi, normalize_person, normalize_title
from .catalog import PaperWork
from .search import PaperNavigator
from .tokenize import contains_cjk, fold, tokenize


class CitationStatus(str, Enum):
    """The three outcomes citation verification must distinguish.

    ``IN_LIBRARY`` is the FOUND state; the existing name is kept because the
    agent contract, the CLI, and the tests already speak it, and inventing a
    second vocabulary for the same state helps nobody.
    """

    IN_LIBRARY = "IN_LIBRARY"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_IN_LIBRARY = "NOT_IN_LIBRARY"


#: Minimum agreement, over the identity fields the citation actually supplies,
#: for a candidate to qualify as the cited work.
MATCH_THRESHOLD = 0.72

#: Below this, a candidate is not even worth showing as a near miss.
CANDIDATE_FLOOR = 0.30

#: How much each identity field is worth *when the citation supplies it*.
#: The score is normalised by the weight of the supplied fields only -- see
#: ``_score_candidates`` for why that normalisation is the whole fix.
WEIGHT_TITLE = 0.60
WEIGHT_AUTHORS = 0.25
WEIGHT_YEAR = 0.15

#: A field counts as an agreeing identity signal at or above this agreement.
SIGNAL_AGREEMENT = 0.5

#: One signal is not an identity.  "Biddle" alone names no particular paper; a
#: surname plus a year does.  Requiring two independent agreeing signals is what
#: keeps shorthand resolution from degenerating into similarity search.
MIN_IDENTITY_SIGNALS = 2

_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
_ET_AL_RE = re.compile(r"\bet\.?\s*al\.?", re.IGNORECASE)
#: CJK "and others".  Written attached to the last surname (``王海森等``) as well
#: as standing alone, so it is stripped in both positions.
_CJK_ET_AL_RE = re.compile(r"等人?")
_QUOTED_RE = re.compile(r"[\"“”'‘’《〈]([^\"“”'‘’》〉]{4,})[\"“”'‘’》〉]")


@dataclass
class ParsedCitation:
    """What could be read out of a free-form citation string."""

    raw: str
    doi: str = UNKNOWN
    year: str = ""
    authors: tuple[str, ...] = ()
    title_guess: str = ""
    tokens: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "doi": self.doi,
            "year": self.year,
            "authors": list(self.authors),
            "title_guess": self.title_guess,
        }

    def handoff_identity(self) -> dict[str, Any]:
        return {
            "title": self.title_guess or UNKNOWN,
            "authors": list(self.authors),
            "year": self.year or UNKNOWN,
            "doi": self.doi,
        }


def parse_citation(text: str) -> ParsedCitation:
    """Pull DOI, year, author names, and a title guess out of a citation string.

    Deliberately format-agnostic: the input may be APA, GB/T 7714, a Chinese
    author-title fragment, or something a person typed from memory.
    """

    raw = (text or "").strip()
    doi = normalize_doi(raw)
    year_match = _YEAR_RE.search(raw)
    year = year_match.group(0) if year_match else ""

    working = raw
    if doi != UNKNOWN:
        working = re.sub(r"(?i)(doi\s*[:：]?\s*)?(https?://(dx\.)?doi\.org/)?10\.\S+", " ", working)
    working = _ET_AL_RE.sub(" ", working)

    quoted = _QUOTED_RE.search(working)
    title_guess = quoted.group(1).strip() if quoted else ""

    # Everything before the year is usually the author block in both APA and
    # GB/T 7714; everything after is usually title + venue.
    authors: list[str] = []
    if year:
        head, _, tail = working.partition(year)
        authors = _author_tokens(head)
        if not title_guess:
            title_guess = _title_from_tail(tail)
    else:
        authors = _author_tokens(working)
        if not title_guess:
            title_guess = working.strip()

    # A shorthand citation -- "Biddle et al. 2009", "王海森等 2026" -- names
    # authors and a year and no title.  Falling back to "the whole string is the
    # title" would put the surname into the title field, where it scores 0
    # overlap against every real title and drags the match down.  An absent
    # title is recorded as absent.
    if not title_guess and not (authors and year):
        title_guess = re.sub(r"[\(\)（）\[\]]", " ", working).strip()

    return ParsedCitation(
        raw=raw,
        doi=doi,
        year=year,
        authors=tuple(authors),
        title_guess=title_guess,
        tokens=tuple(dict.fromkeys(tokenize(f"{title_guess} {' '.join(authors)}"))),
    )


def _author_tokens(head: str) -> list[str]:
    cleaned = re.sub(r"[\(\)（）\[\]&]", " ", head)
    cleaned = re.sub(r"\b(and|与|和)\b", " ", cleaned)
    parts = [part.strip(" .,;、") for part in re.split(r"[,;，；、]|\s{2,}", cleaned)]
    names: list[str] = []
    for part in parts:
        if not part:
            continue
        if contains_cjk(part):
            for name in part.split():
                # "王海森等" is one surname plus the CJK "et al." marker, not a
                # four-character name.  Strip the marker before length-testing,
                # or the author never matches anything in the catalog.
                name = _CJK_ET_AL_RE.sub("", name).strip()
                if 2 <= len(name) <= 5:
                    names.append(name)
        elif len(part.split()) <= 4 and any(char.isalpha() for char in part):
            # "Biddle, G., Hilary, G." splits into surnames and bare initials.
            # An initial is not a name: counting it as one makes a complete
            # author match look like 3-of-5 agreement and can push a correct
            # citation below the match threshold.
            if len(part.replace(".", "").strip()) > 1:
                names.append(part)
    return [name for name in dict.fromkeys(names) if name][:8]


def author_name_matches(wanted: str, actual: str) -> bool:
    """Does a cited author name identify a catalog author?

    Deliberately token-based rather than substring-based.  ``"biddle"`` should
    match ``"gary c biddle"`` because it is one of its name tokens, but a bare
    substring test would also let a two-character fragment match half the
    corpus, which is exactly the looseness citation verification must not have.
    """

    if not wanted or not actual or wanted == UNKNOWN or actual == UNKNOWN:
        return False
    if wanted == actual:
        return True
    actual_tokens = actual.split()
    wanted_tokens = wanted.split()
    if wanted_tokens and all(token in actual_tokens for token in wanted_tokens):
        return True
    # CJK personal names carry no internal whitespace, so token equality cannot
    # apply; require the whole cited name to appear, and never a single glyph.
    if len(wanted) >= 2 and contains_cjk(wanted) and wanted in actual:
        return True
    return False


def _title_from_tail(tail: str) -> str:
    cleaned = tail.strip(" .,;:)：）")
    for separator in (".", "。", "，", ",", "//"):
        candidate = cleaned.split(separator, 1)[0].strip()
        if len(candidate) >= 8:
            return candidate
    return cleaned


@dataclass
class CitationCandidate:
    work: PaperWork
    evidence_score: float
    signals: dict[str, Any]
    identity_signals: int = 0

    def as_dict(self, navigator: PaperNavigator) -> dict[str, Any]:
        resolution = navigator.resolution_for(self.work)
        preferred = resolution.preferred
        return {
            "paper_id": self.work.paper_id,
            "title": self.work.title,
            "authors": list(self.work.authors),
            "year": self.work.year,
            "journal": self.work.journal,
            "doi": self.work.doi,
            "evidence_score": round(self.evidence_score, 4),
            "evidence": self.signals,
            "fulltext_status": resolution.fulltext_status.value,
            "preferred_version": preferred.as_dict() if preferred else None,
            "available_versions": [item.as_dict() for item in resolution.versions],
        }


class CitationVerifier:
    """Decide whether a cited work is already in the library."""

    def __init__(self, navigator: PaperNavigator) -> None:
        self.navigator = navigator

    def verify(self, citation: str, *, max_candidates: int = 5) -> dict[str, Any]:
        parsed = parse_citation(citation)
        snapshot = self.navigator.snapshot

        # 1. DOI is decisive in both directions when the citation carries one.
        if parsed.doi != UNKNOWN:
            work = snapshot.by_doi(parsed.doi)
            if work is not None:
                return self._found(
                    parsed,
                    CitationCandidate(work=work, evidence_score=1.0, signals={"doi": parsed.doi}),
                    match_type="DOI",
                    candidates=(),
                )

        # 2. Exact normalised title.
        for source in (parsed.title_guess, parsed.raw):
            for work in snapshot.by_normalized_title(source):
                return self._found(
                    parsed,
                    CitationCandidate(
                        work=work,
                        evidence_score=1.0,
                        signals={"exact_normalized_title": normalize_title(source)},
                    ),
                    match_type="EXACT_TITLE",
                    candidates=(),
                )

        # 3. Agreement over the identity fields the citation actually supplies.
        candidates = self._score_candidates(parsed, max_candidates=max_candidates)
        qualified = [
            candidate
            for candidate in candidates
            if candidate.evidence_score >= MATCH_THRESHOLD
            and candidate.identity_signals >= MIN_IDENTITY_SIGNALS
        ]
        if len(qualified) == 1:
            return self._found(
                parsed,
                qualified[0],
                match_type="FIELD_AGREEMENT",
                candidates=tuple(item for item in candidates if item is not qualified[0]),
            )
        if len(qualified) > 1:
            return self._ambiguous(parsed, qualified)
        return self._not_in_library(parsed, candidates)

    # -- internals ---------------------------------------------------------

    def _candidate_pool(self, parsed: ParsedCitation, wanted_authors: set[str]) -> list[PaperWork]:
        """Every work the citation could plausibly denote.

        When the citation names authors, the pool is built by scanning the
        catalog for those authors rather than by taking a lexical shortlist.
        That matters for ambiguity: a shortlist ranked by text similarity can
        silently omit a second work by the same author in the same year, which
        would turn a genuinely AMBIGUOUS citation into a confident FOUND.
        """

        pool: dict[str, PaperWork] = {}
        if wanted_authors:
            for work in self.navigator.snapshot.works:
                if any(
                    author_name_matches(wanted, actual)
                    for wanted in wanted_authors
                    for actual in work.normalized_authors
                ):
                    pool[work.paper_id] = work
        if parsed.title_guess:
            probe = " ".join([parsed.title_guess, " ".join(parsed.authors)]).strip() or parsed.raw
            search = self.navigator.search(probe, top=25, use_fulltext=False, expand=False)
            for row in search["results"]:
                work = self.navigator.snapshot.by_id(row["paper_id"])
                if work is not None:
                    pool.setdefault(work.paper_id, work)
        return list(pool.values())

    def _score_candidates(self, parsed: ParsedCitation, *, max_candidates: int) -> list[CitationCandidate]:
        """Score each candidate on the fields the citation supplies, and only those.

        The defect this replaces: the score was a fixed-weight sum over title
        (0.60), authors (0.25) and year (0.15).  A shorthand citation supplies no
        title, so its maximum attainable score was 0.40 -- structurally below the
        0.72 threshold.  "Biddle et al. 2009" could not be verified no matter how
        perfectly the author and year agreed.

        Normalising by the weight of the *supplied* fields fixes that without
        loosening anything: a citation is now judged on the identity evidence it
        actually carries, and carrying less evidence is handled by the separate
        ``MIN_IDENTITY_SIGNALS`` requirement and by ambiguity detection rather
        than by an arithmetic accident.
        """

        wanted_authors = {normalize_person(name) for name in parsed.authors}
        wanted_authors.discard(UNKNOWN)
        wanted_authors.discard("")
        title_tokens = set(tokenize(parsed.title_guess))

        scored: list[CitationCandidate] = []
        for work in self._candidate_pool(parsed, wanted_authors):
            signals: dict[str, Any] = {}
            supplied: list[tuple[float, float]] = []
            agreeing = 0

            if title_tokens:
                work_tokens = set(tokenize(work.title))
                overlap = len(title_tokens & work_tokens) / len(title_tokens)
                signals["title_token_overlap"] = round(overlap, 3)
                supplied.append((WEIGHT_TITLE, overlap))
                if overlap >= SIGNAL_AGREEMENT:
                    agreeing += 1

            if wanted_authors:
                author_hits = [
                    wanted
                    for wanted in wanted_authors
                    if any(author_name_matches(wanted, actual) for actual in work.normalized_authors)
                ]
                ratio = len(author_hits) / len(wanted_authors)
                signals["author_agreement"] = round(ratio, 3)
                signals["authors_matched"] = sorted(author_hits)
                supplied.append((WEIGHT_AUTHORS, ratio))
                if author_hits and ratio >= SIGNAL_AGREEMENT:
                    agreeing += 1

            if parsed.year:
                same_year = parsed.year == work.year
                signals["year_match"] = same_year
                supplied.append((WEIGHT_YEAR, 1.0 if same_year else 0.0))
                if same_year:
                    agreeing += 1

            total_weight = sum(weight for weight, _ in supplied)
            if not total_weight:
                continue
            score = sum(weight * value for weight, value in supplied) / total_weight
            signals["fields_supplied"] = [
                name
                for name, present in (
                    ("title", bool(title_tokens)),
                    ("authors", bool(wanted_authors)),
                    ("year", bool(parsed.year)),
                )
                if present
            ]
            signals["identity_signals"] = agreeing

            if score >= CANDIDATE_FLOOR:
                scored.append(
                    CitationCandidate(
                        work=work,
                        evidence_score=score,
                        signals=signals,
                        identity_signals=agreeing,
                    )
                )

        scored.sort(key=lambda item: (-item.evidence_score, item.work.paper_id))
        return scored[:max_candidates]

    def _ambiguous(
        self,
        parsed: ParsedCitation,
        qualified: list[CitationCandidate],
    ) -> dict[str, Any]:
        """Several works satisfy the citation equally well.

        Picking the highest-scoring one would be a guess dressed as a
        verification.  The caller gets every qualifying work and decides.
        """

        payload = self.navigator.envelope()
        payload.update(
            {
                "status": CitationStatus.AMBIGUOUS.value,
                "citation": parsed.raw,
                "parsed_citation": parsed.as_dict(),
                "match": None,
                "paper_id": None,
                "fulltext_available": False,
                "match_threshold": MATCH_THRESHOLD,
                "candidate_count": len(qualified),
                "candidates": [item.as_dict(self.navigator) for item in qualified],
                "suggested_next_action": "DISAMBIGUATE",
                "reason": (
                    "The citation's identity evidence fits more than one WORK equally well. "
                    "Supply a title fragment or a DOI to resolve it; do not cite any of these "
                    "as the verified paper."
                ),
            }
        )
        return payload

    def _found(
        self,
        parsed: ParsedCitation,
        candidate: CitationCandidate,
        *,
        match_type: str,
        candidates: tuple[CitationCandidate, ...],
    ) -> dict[str, Any]:
        payload = self.navigator.envelope()
        resolution = self.navigator.resolution_for(candidate.work)
        payload.update(
            {
                "status": CitationStatus.IN_LIBRARY.value,
                "citation": parsed.raw,
                "parsed_citation": parsed.as_dict(),
                "match_type": match_type,
                "match": candidate.as_dict(self.navigator),
                "paper_id": candidate.work.paper_id,
                "fulltext_available": resolution.readable,
                "other_candidates": [item.as_dict(self.navigator) for item in candidates],
                "suggested_next_action": "READ_LOCAL_FULLTEXT"
                if resolution.readable
                else "INSPECT_VERSIONS",
            }
        )
        return payload

    def _not_in_library(
        self,
        parsed: ParsedCitation,
        candidates: list[CitationCandidate],
    ) -> dict[str, Any]:
        payload = self.navigator.envelope()
        payload.update(
            {
                "status": CitationStatus.NOT_IN_LIBRARY.value,
                "citation": parsed.raw,
                "parsed_citation": parsed.as_dict(),
                "match": None,
                "paper_id": None,
                "fulltext_available": False,
                "match_threshold": MATCH_THRESHOLD,
                "unverified_candidates": [
                    {**item.as_dict(self.navigator), "verified": False}
                    for item in candidates
                ],
                "suggested_next_action": "ACQUISITION",
                "handoff": acquisition_handoff(parsed),
            }
        )
        return payload


def acquisition_handoff(parsed: ParsedCitation) -> dict[str, Any]:
    """The object an agent passes to the existing Harness acquisition chain.

    The Navigator does not download.  It states the identity it could read and
    stops; ``agent-route`` / ``AgentRequestRouter`` remain the only acquisition
    entry point, with their authorization, identity-lock, validation, hash, and
    manifest guarantees intact.
    """

    identity = parsed.handoff_identity()
    query = parsed.title_guess or parsed.raw
    return {
        "status": CitationStatus.NOT_IN_LIBRARY.value,
        "suggested_next_action": "ACQUISITION",
        "identity": identity,
        "acquisition_request_hint": {
            "TaskType": "literature_search",
            "Query": query,
            "ExactTitles": [identity["title"]] if identity["title"] != UNKNOWN else [],
            "DOIs": [identity["doi"]] if identity["doi"] != UNKNOWN else [],
            "MaxCandidates": 5,
            "MaxDownloads": 1,
            "AuthorizedFullTextOnly": True,
            "FullTextReadingMode": "local",
        },
        "entry_point": "hunnu-harness agent-route --request-json <request.json>",
        "note": (
            "The Navigator never downloads. Route acquisition through the existing "
            "Harness adapter chain so authorization, target-identity lock, validation, "
            "SHA-256, and manifest guarantees are preserved."
        ),
    }


__all__ = [
    "CANDIDATE_FLOOR",
    "CitationCandidate",
    "CitationStatus",
    "CitationVerifier",
    "MATCH_THRESHOLD",
    "MIN_IDENTITY_SIGNALS",
    "ParsedCitation",
    "SIGNAL_AGREEMENT",
    "WEIGHT_AUTHORS",
    "WEIGHT_TITLE",
    "WEIGHT_YEAR",
    "acquisition_handoff",
    "author_name_matches",
    "parse_citation",
]
