"""Read-only WORK/VERSION view over the frozen Global Paper Library catalog.

``GlobalPaperLibrary._load_catalog`` is the *writer's* loader: it raises on any
anomaly because a writer must never commit on top of a catalog it does not
fully understand.  A navigator has the opposite duty.  A single unreadable line
must not make 178 other works unfindable, so this reader degrades: it skips what
it cannot parse, records exactly what it skipped, and reports the degradation in
every response built from it.

Nothing here writes.  Every path returned is resolved from the catalog's own
``managed_path`` values -- never constructed from a ``paper_id``, because three
of the 192 managed files carry a historical name that does not match the
``paper_id`` of the work that owns them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..paths import (
    LIBRARY_CATALOG_JSONL,
    LIBRARY_TOPICS_JSONL,
    OUTPUT_ROOT,
    _logical_path,
    _windows_io_path,
)
from ..literature.models import UNKNOWN
from ..literature.normalization import normalize_doi, normalize_person, normalize_title


CATALOG_READER_VERSION = "navigator-catalog-0.1"

TOPIC_ASSIGNMENT_SEPARATOR = ";"
TOPIC_DOMAIN_SEPARATOR = chr(92)  # a literal backslash inside "domain\subtopic"


class LibraryUnavailable(RuntimeError):
    """The catalog itself could not be opened; no partial answer is possible."""


@dataclass(frozen=True)
class Degradation:
    """One thing the reader could not use, preserved for the response."""

    source: str
    detail: str
    locator: str = UNKNOWN

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "detail": self.detail, "locator": self.locator}


@dataclass(frozen=True)
class TopicAssignment:
    """One ``domain\\subtopic`` pair attached to a work."""

    domain: str
    subtopic: str

    @property
    def label(self) -> str:
        if not self.subtopic:
            return self.domain
        return f"{self.domain}{TOPIC_DOMAIN_SEPARATOR}{self.subtopic}"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.label


@dataclass(frozen=True)
class PaperVersion:
    """One concrete physical file belonging to a WORK."""

    sha256: str
    managed_path: str
    full_text_format: str
    version_role: str
    source_type: str
    source_locator: str
    status: str
    acquired_at: str
    imported_at: str

    @property
    def absolute_path(self) -> Path:
        """Resolve the Output-Root-relative managed path recorded in the catalog."""

        candidate = Path(self.managed_path)
        if candidate.is_absolute():
            return _logical_path(candidate)
        return _logical_path(OUTPUT_ROOT / candidate)

    def exists(self) -> bool:
        try:
            return _windows_io_path(self.absolute_path).is_file()
        except OSError:
            return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "managed_path": self.managed_path,
            "absolute_path": str(self.absolute_path),
            "format": self.full_text_format,
            "version_role": self.version_role,
            "source_type": self.source_type,
            "status": self.status,
        }


@dataclass(frozen=True)
class PaperWork:
    """One scholarly WORK: the unit of retrieval.

    A WORK owns one or more :class:`PaperVersion` objects.  Search, ranking,
    relatedness, and reading packs all operate on this type, never on versions,
    which is what keeps 192 physical files from becoming 192 candidates.
    """

    paper_id: str
    title: str
    authors: tuple[str, ...]
    first_author: str
    year: str
    journal: str
    doi: str
    canonical_sha256: str
    canonical_managed_path: str
    notes_path: str
    status: str
    source_type: str
    schema_version: str
    same_work_different_version: bool
    versions: tuple[PaperVersion, ...]
    topics: tuple[TopicAssignment, ...] = ()
    keywords: tuple[str, ...] = ()
    human_readable_name: str = UNKNOWN
    primary_domain: str = UNKNOWN
    secondary_domains: tuple[str, ...] = ()

    # -- normalised identity, computed once at load ------------------------
    normalized_title: str = field(default="", compare=False)
    normalized_doi: str = field(default="", compare=False)
    normalized_authors: tuple[str, ...] = field(default=(), compare=False)

    @property
    def topic_labels(self) -> tuple[str, ...]:
        return tuple(assignment.label for assignment in self.topics)

    @property
    def domains(self) -> tuple[str, ...]:
        seen: list[str] = []
        for assignment in self.topics:
            if assignment.domain and assignment.domain not in seen:
                seen.append(assignment.domain)
        return tuple(seen)

    @property
    def subtopics(self) -> tuple[str, ...]:
        seen: list[str] = []
        for assignment in self.topics:
            if assignment.subtopic and assignment.subtopic not in seen:
                seen.append(assignment.subtopic)
        return tuple(seen)

    def version_by_sha(self, sha256: str) -> PaperVersion | None:
        for version in self.versions:
            if version.sha256 == sha256:
                return version
        return None

    def identity(self) -> dict[str, Any]:
        """The bibliographic identity, for handoff and pack manifests."""

        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "journal": self.journal,
            "doi": self.doi,
        }


def _text(value: Any, *, default: str = UNKNOWN) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if value is None:
        return ()
    text = str(value).strip()
    return (text,) if text else ()


def parse_topic_field(raw: Any) -> tuple[TopicAssignment, ...]:
    """Parse the ``topics`` string into structured assignments.

    The field is ``domain\\subtopic`` pairs joined by ``;``.  Splitting on the
    backslash first -- the tempting reading -- turns 337 real assignments into
    516 fragments and 146 topic values that do not exist in the taxonomy, so the
    separator order here is load-bearing.
    """

    if not raw:
        return ()
    assignments: list[TopicAssignment] = []
    seen: set[tuple[str, str]] = set()
    for item in str(raw).split(TOPIC_ASSIGNMENT_SEPARATOR):
        item = item.strip()
        if not item:
            continue
        if TOPIC_DOMAIN_SEPARATOR in item:
            domain, subtopic = item.split(TOPIC_DOMAIN_SEPARATOR, 1)
        else:
            domain, subtopic = item, ""
        key = (domain.strip(), subtopic.strip())
        if key in seen:
            continue
        seen.add(key)
        assignments.append(TopicAssignment(domain=key[0], subtopic=key[1]))
    return tuple(assignments)


def _parse_keywords(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    parts = [part.strip() for part in str(raw).replace("，", ";").split(";")]
    return tuple(part for part in parts if part)


def _iter_jsonl(path: Path, source: str, degraded: list[Degradation]) -> Iterator[Mapping[str, Any]]:
    """Yield objects from a JSONL file, recording rather than raising on damage."""

    try:
        raw = _windows_io_path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        degraded.append(Degradation(source=source, detail=f"unreadable: {exc}", locator=str(path)))
        return
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            degraded.append(
                Degradation(source=source, detail=f"unparseable JSON: {exc.msg}", locator=f"line {line_number}")
            )
            continue
        if not isinstance(item, dict):
            degraded.append(
                Degradation(source=source, detail="record is not an object", locator=f"line {line_number}")
            )
            continue
        yield item


@dataclass
class CatalogSnapshot:
    """An immutable in-memory view of the library at one point in time."""

    works: tuple[PaperWork, ...]
    degraded: tuple[Degradation, ...]
    catalog_path: Path
    topics_path: Path
    topics_present: bool

    @property
    def work_count(self) -> int:
        return len(self.works)

    @property
    def version_count(self) -> int:
        return sum(len(work.versions) for work in self.works)

    @property
    def topic_assignment_count(self) -> int:
        return sum(len(work.topics) for work in self.works)

    def by_id(self, paper_id: str) -> PaperWork | None:
        return self._id_map.get(str(paper_id).strip().upper())

    def by_doi(self, doi: str) -> PaperWork | None:
        normalized = normalize_doi(doi)
        if normalized == UNKNOWN:
            return None
        return self._doi_map.get(normalized)

    def by_normalized_title(self, title: str) -> tuple[PaperWork, ...]:
        normalized = normalize_title(title)
        if normalized == UNKNOWN:
            return ()
        return tuple(self._title_map.get(normalized, ()))

    def degraded_dicts(self) -> list[dict[str, str]]:
        return [item.as_dict() for item in self.degraded]

    def __post_init__(self) -> None:
        self._id_map: dict[str, PaperWork] = {}
        self._doi_map: dict[str, PaperWork] = {}
        self._title_map: dict[str, list[PaperWork]] = {}
        for work in self.works:
            self._id_map[work.paper_id.upper()] = work
            if work.normalized_doi != UNKNOWN:
                self._doi_map.setdefault(work.normalized_doi, work)
            if work.normalized_title != UNKNOWN:
                self._title_map.setdefault(work.normalized_title, []).append(work)


class CatalogReader:
    """Load the frozen catalog into WORK objects without ever writing."""

    def __init__(
        self,
        *,
        catalog_path: Path | None = None,
        topics_path: Path | None = None,
    ) -> None:
        self.catalog_path = _logical_path(catalog_path or LIBRARY_CATALOG_JSONL)
        self.topics_path = _logical_path(topics_path or LIBRARY_TOPICS_JSONL)

    def load(self) -> CatalogSnapshot:
        if not _windows_io_path(self.catalog_path).is_file():
            raise LibraryUnavailable(
                f"Paper catalog not found: {self.catalog_path}. "
                "The Navigator reads the Global Paper Library; it never creates one."
            )

        degraded: list[Degradation] = []
        topic_rows = self._load_topics(degraded)

        works: list[PaperWork] = []
        seen: set[str] = set()
        for item in _iter_jsonl(self.catalog_path, "papers.jsonl", degraded):
            paper_id = _text(item.get("paper_id"), default="")
            if not paper_id:
                degraded.append(Degradation(source="papers.jsonl", detail="record has no paper_id"))
                continue
            if paper_id in seen:
                degraded.append(
                    Degradation(
                        source="papers.jsonl",
                        detail="duplicate paper_id; first record kept",
                        locator=paper_id,
                    )
                )
                continue
            seen.add(paper_id)
            try:
                works.append(self._build_work(item, topic_rows.get(paper_id, {})))
            except Exception as exc:  # defensive: one bad record must not stop the load
                degraded.append(
                    Degradation(
                        source="papers.jsonl",
                        detail=f"record could not be interpreted: {type(exc).__name__}: {exc}",
                        locator=paper_id,
                    )
                )

        return CatalogSnapshot(
            works=tuple(works),
            degraded=tuple(degraded),
            catalog_path=self.catalog_path,
            topics_path=self.topics_path,
            topics_present=bool(topic_rows),
        )

    # -- internals ---------------------------------------------------------

    def _load_topics(self, degraded: list[Degradation]) -> dict[str, Mapping[str, Any]]:
        if not _windows_io_path(self.topics_path).is_file():
            degraded.append(
                Degradation(
                    source="paper_topics.jsonl",
                    detail="topic metadata not found; retrieval continues without the topic signal",
                    locator=str(self.topics_path),
                )
            )
            return {}
        rows: dict[str, Mapping[str, Any]] = {}
        for item in _iter_jsonl(self.topics_path, "paper_topics.jsonl", degraded):
            paper_id = _text(item.get("paper_id"), default="")
            if paper_id:
                rows.setdefault(paper_id, item)
        return rows

    def _build_work(self, item: Mapping[str, Any], topics: Mapping[str, Any]) -> PaperWork:
        versions = self._build_versions(item)
        title = _text(item.get("title"))
        authors = _string_list(item.get("authors"))
        doi = _text(item.get("doi"))
        return PaperWork(
            paper_id=_text(item.get("paper_id")),
            title=title,
            authors=authors,
            first_author=_text(item.get("first_author")),
            year=_text(item.get("year")),
            journal=_text(item.get("journal")),
            doi=doi,
            canonical_sha256=_text(item.get("sha256")),
            canonical_managed_path=_text(item.get("managed_fulltext_path")),
            notes_path=_text(item.get("notes_path")),
            status=_text(item.get("status")),
            source_type=_text(item.get("source_type")),
            schema_version=_text(item.get("schema_version")),
            same_work_different_version=bool(item.get("same_work_different_version")),
            versions=versions,
            topics=parse_topic_field(topics.get("topics")),
            keywords=_parse_keywords(topics.get("keywords")),
            human_readable_name=_text(topics.get("human_readable_name")),
            primary_domain=_text(topics.get("primary_domain")),
            secondary_domains=tuple(
                part.strip()
                for part in _text(topics.get("secondary_domains"), default="").split(";")
                if part.strip()
            ),
            normalized_title=normalize_title(title),
            normalized_doi=normalize_doi(doi),
            normalized_authors=tuple(normalize_person(author) for author in authors),
        )

    @staticmethod
    def _build_versions(item: Mapping[str, Any]) -> tuple[PaperVersion, ...]:
        raw_versions = item.get("versions")
        entries: Sequence[Any]
        if isinstance(raw_versions, list) and raw_versions:
            entries = raw_versions
        else:
            # A record without a versions list still has a canonical file.
            entries = [
                {
                    "sha256": item.get("sha256"),
                    "managed_path": item.get("managed_fulltext_path"),
                    "full_text_format": "PDF"
                    if str(item.get("managed_pdf_path", UNKNOWN)) != UNKNOWN
                    else UNKNOWN,
                    "version_role": item.get("version_role"),
                    "source_type": item.get("source_type"),
                    "source_locator": item.get("source_locator"),
                    "status": item.get("status"),
                    "acquired_at": item.get("acquired_at"),
                    "imported_at": item.get("imported_at"),
                }
            ]
        versions: list[PaperVersion] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            managed_path = _text(entry.get("managed_path"), default="")
            if not managed_path:
                continue
            versions.append(
                PaperVersion(
                    sha256=_text(entry.get("sha256")),
                    managed_path=managed_path,
                    full_text_format=_text(entry.get("full_text_format")).upper(),
                    version_role=_text(entry.get("version_role")),
                    source_type=_text(entry.get("source_type")),
                    source_locator=_text(entry.get("source_locator")),
                    status=_text(entry.get("status")),
                    acquired_at=_text(entry.get("acquired_at")),
                    imported_at=_text(entry.get("imported_at")),
                )
            )
        return tuple(versions)


__all__ = [
    "CATALOG_READER_VERSION",
    "CatalogReader",
    "CatalogSnapshot",
    "Degradation",
    "LibraryUnavailable",
    "PaperVersion",
    "PaperWork",
    "TopicAssignment",
    "parse_topic_field",
]
