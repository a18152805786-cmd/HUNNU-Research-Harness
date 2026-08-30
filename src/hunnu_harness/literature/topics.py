"""Canonical WORK-level topic metadata: taxonomy, assignments, and the view.

``paper_topics.jsonl`` is the source of truth for which taxonomy topics a WORK
carries.  ``papers_by_topic`` is a derived, rebuildable hardlink view of it, and
the Navigator's index is derived from both.  Nothing here reads the Navigator
index: AGENTS.md 66 keeps the retrieval layer downstream of this metadata, never
upstream of it.

Topic values are frozen.  Every assignment names a ``domain\\subtopic`` pair that
already exists in ``topic_taxonomy.json``; an unknown value is rejected rather
than silently added, so acquisition can never grow the taxonomy as a side
effect.
"""

from __future__ import annotations

import csv
import json
import os
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..paths import (
    LIBRARY_PAPERS_DIR,
    LIBRARY_TOPICS_CSV,
    LIBRARY_TOPICS_JSONL,
    PAPERS_BY_TOPIC_DIR,
    TOPIC_TAXONOMY_JSON,
    is_within,
)
from .models import UNKNOWN

# The stored ``topics`` field joins ``domain\subtopic`` pairs with ";".  The
# Navigator parses the same shape; both must agree on these two separators.
TOPIC_DOMAIN_SEPARATOR = chr(92)
TOPIC_ITEM_SEPARATOR = ";"

VARIANT_VIEW_DOMAIN = "00_版本变体"
LINK_TYPE_HARDLINK = "NTFS_HARDLINK"
LINK_TYPE_REFERENCE = "REFERENCE_ONLY"

TOPIC_ROW_FIELDS = (
    "paper_id",
    "title",
    "primary_domain",
    "secondary_domains",
    "topics",
    "keywords",
    "human_readable_name",
    "canonical_path",
    "canonical_sha256",
    "version_paths",
    "metadata_notes",
)

TOPIC_LINK_FIELDS = (
    "paper_id",
    "domain",
    "topic",
    "human_readable_name",
    "view_path",
    "canonical_path",
    "link_type",
    "canonical_sha256",
    "version_role",
)


class TopicTaxonomyError(RuntimeError):
    """Raised when the frozen taxonomy cannot be trusted."""


class UnknownTopicValue(ValueError):
    """Raised when an assignment names a topic outside the frozen taxonomy."""


class TopicStoreError(RuntimeError):
    """Raised when canonical topic metadata cannot be committed."""


@dataclass(frozen=True, order=True)
class TopicLabel:
    """One ``domain\\subtopic`` pair drawn from the frozen taxonomy."""

    domain: str
    subtopic: str

    @property
    def label(self) -> str:
        if not self.subtopic:
            return self.domain
        return f"{self.domain}{TOPIC_DOMAIN_SEPARATOR}{self.subtopic}"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.label


def parse_topic_label(raw: str) -> TopicLabel | None:
    """Parse one stored ``domain\\subtopic`` item, tolerating stray whitespace."""

    item = (raw or "").strip()
    if not item:
        return None
    if TOPIC_DOMAIN_SEPARATOR in item:
        domain, subtopic = item.split(TOPIC_DOMAIN_SEPARATOR, 1)
    else:
        domain, subtopic = item, ""
    return TopicLabel(domain=domain.strip(), subtopic=subtopic.strip())


def parse_topics_field(raw: Any) -> tuple[TopicLabel, ...]:
    """Parse the stored ``topics`` string into ordered, de-duplicated labels."""

    if not isinstance(raw, str):
        return ()
    labels: list[TopicLabel] = []
    seen: set[tuple[str, str]] = set()
    for item in raw.split(TOPIC_ITEM_SEPARATOR):
        label = parse_topic_label(item)
        if label is None:
            continue
        key = (label.domain, label.subtopic)
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return tuple(labels)


def format_topics_field(labels: Iterable[TopicLabel]) -> str:
    ordered: list[str] = []
    for label in labels:
        if label.label not in ordered:
            ordered.append(label.label)
    return TOPIC_ITEM_SEPARATOR.join(ordered)


@dataclass(frozen=True)
class TopicTaxonomy:
    """The frozen ``domain -> subtopics`` registry.

    Subtopic values are unique across domains in this corpus, so a bare subtopic
    resolves to exactly one label.  That uniqueness is verified on load rather
    than assumed, because a future taxonomy edit could break it.
    """

    domains: Mapping[str, tuple[str, ...]]
    generated_at: str = UNKNOWN

    @classmethod
    def load(cls, path: Path | None = None) -> "TopicTaxonomy":
        source = Path(path or TOPIC_TAXONOMY_JSON)
        if not source.exists():
            raise TopicTaxonomyError(f"Topic taxonomy is not present: {source}")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TopicTaxonomyError(f"Topic taxonomy is unreadable: {exc}") from exc
        raw_domains = payload.get("domains")
        if not isinstance(raw_domains, Mapping) or not raw_domains:
            raise TopicTaxonomyError("Topic taxonomy declares no domains")
        domains: dict[str, tuple[str, ...]] = {}
        for domain, subtopics in raw_domains.items():
            if not isinstance(subtopics, list):
                raise TopicTaxonomyError(f"Domain {domain!r} does not list subtopics")
            domains[str(domain)] = tuple(str(item) for item in subtopics)
        taxonomy = cls(domains=domains, generated_at=str(payload.get("generated_at", UNKNOWN)))
        taxonomy._require_unique_subtopics()
        return taxonomy

    def _require_unique_subtopics(self) -> None:
        seen: dict[str, str] = {}
        for domain, subtopics in self.domains.items():
            for subtopic in subtopics:
                if subtopic in seen:
                    raise TopicTaxonomyError(
                        "Subtopic is not unique across domains: "
                        f"{subtopic!r} in {seen[subtopic]!r} and {domain!r}"
                    )
                seen[subtopic] = domain

    def labels(self) -> tuple[TopicLabel, ...]:
        return tuple(
            TopicLabel(domain=domain, subtopic=subtopic)
            for domain, subtopics in self.domains.items()
            for subtopic in subtopics
        )

    def subtopics(self) -> tuple[str, ...]:
        return tuple(subtopic for subtopics in self.domains.values() for subtopic in subtopics)

    def contains(self, label: TopicLabel) -> bool:
        return label.subtopic in self.domains.get(label.domain, ())

    def resolve_subtopic(self, subtopic: str) -> TopicLabel | None:
        """Map a bare subtopic back to its unique ``domain\\subtopic`` label."""

        for domain, subtopics in self.domains.items():
            if subtopic in subtopics:
                return TopicLabel(domain=domain, subtopic=subtopic)
        return None

    def require_known(self, labels: Iterable[TopicLabel]) -> tuple[TopicLabel, ...]:
        checked = tuple(labels)
        unknown = [label.label for label in checked if not self.contains(label)]
        if unknown:
            raise UnknownTopicValue(
                "Topic values outside the frozen taxonomy: " + ", ".join(sorted(unknown))
            )
        return checked


@dataclass(frozen=True)
class WorkTopicRow:
    """One WORK's row in ``paper_topics.jsonl``.

    The stored schema is preserved field for field.  Unknown keys encountered on
    load are carried through untouched so that writing a row never drops data
    this Harness version does not yet understand.
    """

    paper_id: str
    title: str = UNKNOWN
    primary_domain: str = ""
    secondary_domains: str = ""
    topics: tuple[TopicLabel, ...] = ()
    keywords: str = ""
    human_readable_name: str = ""
    canonical_path: str = ""
    canonical_sha256: str = ""
    version_paths: str = ""
    metadata_notes: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]) -> "WorkTopicRow":
        known = set(TOPIC_ROW_FIELDS)
        return cls(
            paper_id=str(item.get("paper_id", UNKNOWN)),
            title=str(item.get("title", UNKNOWN)),
            primary_domain=str(item.get("primary_domain", "")),
            secondary_domains=str(item.get("secondary_domains", "")),
            topics=parse_topics_field(item.get("topics")),
            keywords=str(item.get("keywords", "")),
            human_readable_name=str(item.get("human_readable_name", "")),
            canonical_path=str(item.get("canonical_path", "")),
            canonical_sha256=str(item.get("canonical_sha256", "")),
            version_paths=str(item.get("version_paths", "")),
            metadata_notes=str(item.get("metadata_notes", "")),
            extra={key: value for key, value in item.items() if key not in known},
        )

    def as_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = dict(self.extra)
        payload.update(
            {
                "paper_id": self.paper_id,
                "title": self.title,
                "primary_domain": self.primary_domain,
                "secondary_domains": self.secondary_domains,
                "topics": format_topics_field(self.topics),
                "keywords": self.keywords,
                "human_readable_name": self.human_readable_name,
                "canonical_path": self.canonical_path,
                "canonical_sha256": self.canonical_sha256,
                "version_paths": self.version_paths,
                "metadata_notes": self.metadata_notes,
            }
        )
        return payload

    @property
    def domains(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for label in self.topics:
            if label.domain and label.domain not in ordered:
                ordered.append(label.domain)
        return tuple(ordered)

    def with_topics(self, labels: Iterable[TopicLabel]) -> "WorkTopicRow":
        """Return a copy carrying *labels*, keeping primary/secondary in step."""

        ordered = tuple(dict.fromkeys(labels))
        domains: list[str] = []
        for label in ordered:
            if label.domain and label.domain not in domains:
                domains.append(label.domain)
        return replace(
            self,
            topics=ordered,
            primary_domain=domains[0] if domains else "",
            secondary_domains=TOPIC_ITEM_SEPARATOR.join(domains[1:]),
        )


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_csv(path: Path, fields: tuple[str, ...], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields))
            writer.writeheader()
            for row in rows:
                writer.writerow({name: row.get(name, "") for name in fields})
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class TopicStore:
    """Read and commit the canonical WORK-level topic assignments."""

    def __init__(
        self,
        *,
        jsonl_path: Path | None = None,
        csv_path: Path | None = None,
        taxonomy: TopicTaxonomy | None = None,
    ) -> None:
        self.jsonl_path = Path(jsonl_path or LIBRARY_TOPICS_JSONL)
        self.csv_path = Path(csv_path or LIBRARY_TOPICS_CSV)
        self._taxonomy = taxonomy

    @property
    def taxonomy(self) -> TopicTaxonomy:
        if self._taxonomy is None:
            self._taxonomy = TopicTaxonomy.load()
        return self._taxonomy

    def load(self) -> dict[str, WorkTopicRow]:
        if not self.jsonl_path.exists():
            return {}
        rows: dict[str, WorkTopicRow] = {}
        try:
            text = self.jsonl_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TopicStoreError(f"Topic metadata is unreadable: {exc}") from exc
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise TopicStoreError(
                    f"Topic metadata line {number} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(item, Mapping):
                raise TopicStoreError(f"Topic metadata line {number} is not an object")
            row = WorkTopicRow.from_mapping(item)
            if row.paper_id and row.paper_id != UNKNOWN:
                rows[row.paper_id] = row
        return rows

    def topics_for(self, paper_id: str) -> tuple[TopicLabel, ...]:
        row = self.load().get(paper_id)
        return row.topics if row is not None else ()

    def commit(self, rows: Mapping[str, WorkTopicRow]) -> None:
        """Write every row atomically, JSONL first then the CSV projection."""

        for row in rows.values():
            self.taxonomy.require_known(row.topics)
        ordered = sorted(rows.values(), key=lambda item: item.paper_id.casefold())
        payloads = [row.as_mapping() for row in ordered]
        content = "".join(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n" for payload in payloads
        )
        _atomic_text(self.jsonl_path, content)
        _atomic_csv(self.csv_path, TOPIC_ROW_FIELDS, payloads)

    def upsert(self, row: WorkTopicRow) -> dict[str, WorkTopicRow]:
        """Replace exactly one WORK's row, leaving every other row byte-identical."""

        self.taxonomy.require_known(row.topics)
        rows = self.load()
        rows[row.paper_id] = row
        self.commit(rows)
        return rows


class TopicViewBuilder:
    """Rebuild the ``papers_by_topic`` hardlink view from canonical assignments.

    A WORK carrying several topics appears under each of them, but every entry
    is a hardlink to the one managed file.  No full text is ever copied, so the
    view costs directory entries and nothing else.
    """

    def __init__(
        self,
        *,
        view_root: Path | None = None,
        papers_dir: Path | None = None,
    ) -> None:
        self.view_root = Path(view_root or PAPERS_BY_TOPIC_DIR)
        self.papers_dir = Path(papers_dir or LIBRARY_PAPERS_DIR)

    @property
    def links_path(self) -> Path:
        return self.view_root / "topic_links.csv"

    def link_rows(self, rows: Mapping[str, WorkTopicRow]) -> list[dict[str, Any]]:
        """Every link the view should contain, ordered for a stable file."""

        links: list[dict[str, Any]] = []
        for paper_id in sorted(rows, key=str.casefold):
            row = rows[paper_id]
            name = row.human_readable_name or f"{paper_id}.pdf"
            for label in row.topics:
                links.append(
                    {
                        "paper_id": paper_id,
                        "domain": label.domain,
                        "topic": label.subtopic,
                        "human_readable_name": name,
                        "view_path": str(self.view_root / label.domain / label.subtopic / name),
                        "canonical_path": row.canonical_path,
                        "link_type": LINK_TYPE_HARDLINK,
                        "canonical_sha256": row.canonical_sha256,
                        "version_role": row.extra.get("version_role", "PRIMARY_CANONICAL"),
                    }
                )
        return links

    def _safe_view_path(self, candidate: Path) -> Path:
        resolved_root = self.view_root.resolve()
        resolved = Path(os.path.normpath(str(candidate)))
        if not is_within(resolved, resolved_root):
            raise TopicStoreError(f"Topic view entry escaped the view root: {candidate}")
        return resolved

    def sync_work(self, row: WorkTopicRow) -> dict[str, Any]:
        """Make one WORK's view entries match its assignments.

        Links this WORK no longer needs are removed, links it gained are
        created, and links already correct are left alone -- so calling this
        twice creates nothing the first call did not.
        """

        canonical = Path(row.canonical_path) if row.canonical_path else None
        created: list[str] = []
        removed: list[str] = []
        referenced: list[str] = []
        wanted: dict[Path, TopicLabel] = {}
        name = row.human_readable_name or f"{row.paper_id}.pdf"
        for label in row.topics:
            target = self._safe_view_path(self.view_root / label.domain / label.subtopic / name)
            wanted[target] = label

        for existing in self._existing_links_for(row.paper_id, name):
            if existing not in wanted:
                try:
                    existing.unlink()
                    removed.append(str(existing))
                except OSError:
                    pass

        for target, _label in sorted(wanted.items(), key=lambda item: str(item[0])):
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if canonical is None or not canonical.exists():
                referenced.append(str(target))
                continue
            try:
                os.link(canonical, target)
                created.append(str(target))
            except OSError:
                # A view entry is a convenience, never the paper itself.  When
                # the filesystem refuses a hardlink the row still records the
                # canonical path, which is what the Navigator reads.
                referenced.append(str(target))
        return {
            "paper_id": row.paper_id,
            "links_created": tuple(created),
            "links_removed": tuple(removed),
            "links_unavailable": tuple(referenced),
            "pdf_copies_created": 0,
        }

    def _existing_links_for(self, paper_id: str, name: str) -> tuple[Path, ...]:
        if not self.view_root.exists():
            return ()
        found: list[Path] = []
        for domain_dir in self.view_root.iterdir():
            if not domain_dir.is_dir() or domain_dir.name == VARIANT_VIEW_DOMAIN:
                continue
            for subtopic_dir in domain_dir.iterdir():
                if not subtopic_dir.is_dir():
                    continue
                candidate = subtopic_dir / name
                if candidate.exists():
                    found.append(candidate)
        return tuple(found)

    def write_links_manifest(self, rows: Mapping[str, WorkTopicRow]) -> Path:
        """Regenerate ``topic_links.csv`` for the topic domains.

        The variant view (``00_版本变体``) is owned by the Library's version
        layout, not by classification, so its existing rows are preserved.
        """

        preserved: list[dict[str, Any]] = []
        if self.links_path.exists():
            with self.links_path.open("r", encoding="utf-8-sig", newline="") as handle:
                for item in csv.DictReader(handle):
                    if item.get("domain") == VARIANT_VIEW_DOMAIN:
                        preserved.append({name: item.get(name, "") for name in TOPIC_LINK_FIELDS})
        combined = self.link_rows(rows) + preserved
        combined.sort(key=lambda item: (str(item["paper_id"]).casefold(), str(item["domain"])))
        _atomic_csv(self.links_path, TOPIC_LINK_FIELDS, combined)
        return self.links_path


__all__ = [
    "LINK_TYPE_HARDLINK",
    "LINK_TYPE_REFERENCE",
    "TOPIC_DOMAIN_SEPARATOR",
    "TOPIC_ITEM_SEPARATOR",
    "TOPIC_LINK_FIELDS",
    "TOPIC_ROW_FIELDS",
    "TopicLabel",
    "TopicStore",
    "TopicStoreError",
    "TopicTaxonomy",
    "TopicTaxonomyError",
    "TopicViewBuilder",
    "UnknownTopicValue",
    "VARIANT_VIEW_DOMAIN",
    "WorkTopicRow",
    "format_topics_field",
    "parse_topic_label",
    "parse_topics_field",
]
