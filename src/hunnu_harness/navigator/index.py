"""The derived, rebuildable retrieval index.

Everything here is disposable.  The index stores no identity it did not read
from the catalog, has exactly one writer (``build``), and can be reconstructed
from the catalog plus the managed full texts at any time.  Deleting
``paper_retrieval/`` loses nothing but time.

Staleness is detected, never guessed: the manifest records the SHA-256 of both
catalog files at build time, and each work records the SHA-256 of the version
whose text was extracted.  A response built on a stale index says so.

Disposable does not mean replaceable by garbage: a build commits nothing until
extraction has finished, and it refuses to commit at all when the extraction
machinery failed systemically (every attempt, or more than
``max_failure_share`` of them).  A rebuild under an interpreter without
``pypdf`` therefore keeps the previous index instead of swapping in an empty
one that still says BUILT.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from ..paths import (
    PAPER_RETRIEVAL_FULLTEXT_DIR,
    PAPER_RETRIEVAL_FULLTEXT_MANIFEST,
    PAPER_RETRIEVAL_INDEX_DIR,
    PAPER_RETRIEVAL_INDEX_MANIFEST,
    PAPER_RETRIEVAL_ROOT,
    _logical_path,
    _transaction_token,
    _windows_io_path,
    require_output_path,
)
from .catalog import CatalogSnapshot, PaperWork
from .fulltext import (
    Chunk,
    EXTRACTION_DEPENDENCY_MISSING,
    EXTRACTION_FAILED,
    EXTRACTION_MISSING,
    EXTRACTION_OK,
    EXTRACTION_UNSUPPORTED,
    FullTextExtractor,
)
from .resolver import PreferredVersionResolver


INDEX_SCHEMA_VERSION = "navigator-index-0.1"

#: Statuses that mean the extraction machinery broke, as opposed to statuses
#: that describe the source document (missing file, scanned-only, non-PDF).
#: These are the failures that turn a rebuild into data loss when they cover
#: the corpus.
HARD_FAILURE_STATUSES = frozenset({EXTRACTION_FAILED, EXTRACTION_DEPENDENCY_MISSING})

#: Statuses that never exercised the extraction machinery at all; they do not
#: dilute the failure share.
_NOT_AN_EXTRACTION_ATTEMPT = frozenset({EXTRACTION_MISSING, EXTRACTION_UNSUPPORTED})

#: A build whose hard-failure share among real extraction attempts exceeds
#: this refuses to commit.  Total failure refuses whatever the value.
DEFAULT_MAX_FAILURE_SHARE = 0.5

#: Cap on per-work failure rows carried in a build/refusal payload.
_MAX_REPORTED_FAILURES = 50


class IndexBuildRefused(RuntimeError):
    """A build refused to commit because extraction failed systemically.

    Raised before anything is written or deleted, so the previous index --
    including the one a ``rebuild`` was about to replace -- is intact on
    disk.  ``payload`` carries the counts, the dominant cause, and the
    remedy, ready for the CLI to emit.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("reason", "index build refused")))
        self.payload = payload


class IndexStatus(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    ABSENT = "ABSENT"
    UNREADABLE = "UNREADABLE"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with _windows_io_path(path).open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def _safe_digest(path: Path) -> str:
    try:
        return file_sha256(path)
    except OSError:
        return ""


def _atomic_write_text(path: Path, content: str) -> None:
    logical = require_output_path(path, label="Navigator index path")
    _windows_io_path(logical.parent).mkdir(parents=True, exist_ok=True)
    temporary = logical.with_name(f"{logical.name}.{_transaction_token()}.tmp")
    temporary_io = _windows_io_path(temporary)
    try:
        temporary_io.write_text(content, encoding="utf-8", newline="\n")
        temporary_io.replace(_windows_io_path(logical))
    finally:
        temporary_io.unlink(missing_ok=True)


@dataclass
class IndexManifest:
    schema_version: str
    built_at: str
    catalog_sha256: str
    topics_sha256: str
    work_count: int
    version_count: int
    topic_assignment_count: int
    fulltext_works: int
    fulltext_chunks: int
    page_limit: int
    chunk_chars: int

    def as_dict(self) -> dict[str, Any]:
        return dict(sorted(self.__dict__.items()))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IndexManifest":
        return cls(
            schema_version=str(payload.get("schema_version", "")),
            built_at=str(payload.get("built_at", "")),
            catalog_sha256=str(payload.get("catalog_sha256", "")),
            topics_sha256=str(payload.get("topics_sha256", "")),
            work_count=int(payload.get("work_count", 0)),
            version_count=int(payload.get("version_count", 0)),
            topic_assignment_count=int(payload.get("topic_assignment_count", 0)),
            fulltext_works=int(payload.get("fulltext_works", 0)),
            fulltext_chunks=int(payload.get("fulltext_chunks", 0)),
            page_limit=int(payload.get("page_limit", 0)),
            chunk_chars=int(payload.get("chunk_chars", 0)),
        )


@dataclass
class FullTextIndex:
    """Loaded chunks plus the staleness verdict for each work."""

    chunks_by_work: dict[str, tuple[Chunk, ...]] = field(default_factory=dict)
    stale_works: frozenset[str] = frozenset()
    manifest_entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def chunk_count(self) -> int:
        return sum(len(items) for items in self.chunks_by_work.values())

    def for_work(self, paper_id: str) -> tuple[Chunk, ...]:
        if paper_id in self.stale_works:
            return ()
        return self.chunks_by_work.get(paper_id, ())

    def covers(self, paper_id: str) -> bool:
        return paper_id in self.chunks_by_work and paper_id not in self.stale_works


class NavigatorIndex:
    """Build, validate, and load the derived retrieval index."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        extractor: FullTextExtractor | None = None,
        resolver: PreferredVersionResolver | None = None,
        max_failure_share: float = DEFAULT_MAX_FAILURE_SHARE,
    ) -> None:
        self.root = _logical_path(root or PAPER_RETRIEVAL_ROOT)
        self.index_dir = self.root / "index" if root else _logical_path(PAPER_RETRIEVAL_INDEX_DIR)
        self.manifest_path = self.index_dir / "manifest.json" if root else _logical_path(PAPER_RETRIEVAL_INDEX_MANIFEST)
        self.fulltext_dir = self.index_dir / "fulltext" if root else _logical_path(PAPER_RETRIEVAL_FULLTEXT_DIR)
        self.fulltext_manifest_path = (
            self.index_dir / "fulltext_manifest.json"
            if root
            else _logical_path(PAPER_RETRIEVAL_FULLTEXT_MANIFEST)
        )
        self.extractor = extractor or FullTextExtractor()
        self.resolver = resolver or PreferredVersionResolver()
        self.max_failure_share = float(max_failure_share)

    # -- status ------------------------------------------------------------

    def read_manifest(self) -> IndexManifest | None:
        manifest_io = _windows_io_path(self.manifest_path)
        if not manifest_io.is_file():
            return None
        try:
            payload = json.loads(manifest_io.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return IndexManifest.from_dict(payload)

    def status(self, snapshot: CatalogSnapshot) -> tuple[IndexStatus, dict[str, Any]]:
        manifest = self.read_manifest()
        if manifest is None:
            detail = {
                "reason": "index manifest absent or unreadable",
                "index_dir": str(self.index_dir),
            }
            return (
                IndexStatus.ABSENT if not _windows_io_path(self.manifest_path).exists() else IndexStatus.UNREADABLE,
                detail,
            )
        current_catalog = _safe_digest(snapshot.catalog_path)
        current_topics = _safe_digest(snapshot.topics_path) if _windows_io_path(snapshot.topics_path).is_file() else ""
        fresh = manifest.catalog_sha256 == current_catalog and manifest.topics_sha256 == current_topics
        detail = {
            "built_at": manifest.built_at,
            "schema_version": manifest.schema_version,
            "work_count": manifest.work_count,
            "fulltext_works": manifest.fulltext_works,
            "fulltext_chunks": manifest.fulltext_chunks,
            "catalog_sha256_at_build": manifest.catalog_sha256,
            "catalog_sha256_now": current_catalog,
            "topics_sha256_at_build": manifest.topics_sha256,
            "topics_sha256_now": current_topics,
        }
        return (IndexStatus.FRESH if fresh else IndexStatus.STALE), detail

    def resolved_status(self, snapshot: CatalogSnapshot) -> tuple[IndexStatus, dict[str, Any]]:
        """The authoritative index state, and the only one callers should report.

        ``status()`` compares the catalog digests recorded at build time.  That
        is necessary but not sufficient: a work whose preferred version changed
        has stale chunks even when the catalog file is byte-identical, and
        ``load_fulltext()`` used to be the only place that noticed.  A command
        that never loaded chunks therefore reported FRESH while a command that
        did reported STALE, for the same index.

        This computes the complete verdict from the manifests alone -- no chunk
        file is read -- so every command can afford to ask for it.
        """

        status, detail = self.status(snapshot)
        if status is not IndexStatus.FRESH:
            return status, detail
        stale = self.stale_works(snapshot)
        if stale:
            return IndexStatus.STALE, {**detail, "stale_works": sorted(stale)}
        return status, detail

    def stale_works(self, snapshot: CatalogSnapshot) -> set[str]:
        """Works whose indexed text came from a version that is no longer preferred."""

        entries = self._read_fulltext_manifest()
        if not entries:
            return set()
        stale: set[str] = set()
        for work in snapshot.works:
            entry = entries.get(work.paper_id)
            if entry is None or entry.get("status") != EXTRACTION_OK:
                continue
            resolution = self.resolver.resolve(work)
            expected = resolution.preferred.version.sha256 if resolution.preferred else ""
            if str(entry.get("source_sha256")) != expected:
                stale.add(work.paper_id)
        return stale

    # -- build -------------------------------------------------------------

    def build(
        self,
        snapshot: CatalogSnapshot,
        *,
        progress: Callable[[str, int, int], None] | None = None,
        only: Iterable[str] | None = None,
        fresh: bool = False,
        allow_degraded: bool = False,
    ) -> dict[str, Any]:
        """Extract text for every readable work, then commit the index.

        Extraction runs first and writes nothing; every filesystem effect
        belongs to the commit phase at the end.  A work whose text cannot be
        extracted is recorded with its failure status and skipped -- but when
        hard failures (the extraction machinery breaking, not a document
        being missing or scanned-only) cover every extraction attempt or
        exceed ``max_failure_share`` of them, the build raises
        :class:`IndexBuildRefused` instead of committing, because replacing a
        working index with an empty one is data loss, not a build.
        ``allow_degraded=True`` commits anyway.  ``fresh=True`` deletes the
        previous index directory at commit time; it is what :meth:`rebuild`
        passes.
        """

        require_output_path(self.root, label="Navigator index root")

        selected = set(only) if only is not None else None
        entries: dict[str, dict[str, Any]] = {}
        if selected is not None and not fresh:
            existing = self._read_fulltext_manifest()
            entries.update(existing)

        works = [work for work in snapshot.works if selected is None or work.paper_id in selected]
        total = len(works)

        # -- extraction phase: reads only, so a refusal below loses nothing -
        chunks_by_work: dict[str, tuple[Chunk, ...]] = {}
        extraction_attempts = 0
        hard_failures: list[dict[str, Any]] = []
        for position, work in enumerate(works, start=1):
            if progress is not None:
                progress(work.paper_id, position, total)
            resolution = self.resolver.resolve(work)
            preferred = resolution.preferred
            if preferred is None:
                entries[work.paper_id] = {
                    "paper_id": work.paper_id,
                    "source_sha256": "",
                    "managed_path": "",
                    "status": "NO_VERSION",
                    "pages_read": 0,
                    "chunk_count": 0,
                    "detail": "work has no managed version",
                }
                continue
            result = self.extractor.extract(work.paper_id, preferred.version)
            entries[work.paper_id] = result.as_manifest_entry()
            if result.status not in _NOT_AN_EXTRACTION_ATTEMPT:
                extraction_attempts += 1
            if result.status == EXTRACTION_OK and result.chunks:
                chunks_by_work[work.paper_id] = result.chunks
            elif result.status in HARD_FAILURE_STATUSES:
                hard_failures.append(
                    {"paper_id": work.paper_id, "status": result.status, "detail": result.detail}
                )

        refusal = self._systemic_failure(extraction_attempts, hard_failures)
        if refusal is not None and not allow_degraded:
            raise IndexBuildRefused(refusal)

        # -- commit phase ----------------------------------------------------
        if fresh:
            self.drop()
        _windows_io_path(self.index_dir).mkdir(parents=True, exist_ok=True)
        _windows_io_path(self.fulltext_dir).mkdir(parents=True, exist_ok=True)

        chunk_total = 0
        ok_works = 0
        for work in works:
            chunks = chunks_by_work.get(work.paper_id)
            if chunks:
                self._write_chunks(work.paper_id, chunks)
                chunk_total += len(chunks)
                ok_works += 1
            else:
                self._remove_chunk_file(work.paper_id)

        if selected is not None:
            # Recount from the manifest so a partial rebuild reports the whole index.
            chunk_total = sum(int(item.get("chunk_count", 0)) for item in entries.values())
            ok_works = sum(1 for item in entries.values() if item.get("status") == EXTRACTION_OK)

        _atomic_write_text(
            self.fulltext_manifest_path,
            json.dumps(
                {"entries": [entries[key] for key in sorted(entries)]},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        manifest = IndexManifest(
            schema_version=INDEX_SCHEMA_VERSION,
            built_at=_timestamp(),
            catalog_sha256=_safe_digest(snapshot.catalog_path),
            topics_sha256=_safe_digest(snapshot.topics_path) if _windows_io_path(snapshot.topics_path).is_file() else "",
            work_count=snapshot.work_count,
            version_count=snapshot.version_count,
            topic_assignment_count=snapshot.topic_assignment_count,
            fulltext_works=ok_works,
            fulltext_chunks=chunk_total,
            page_limit=self.extractor.page_limit,
            chunk_chars=self.extractor.chunk_chars,
        )
        _atomic_write_text(
            self.manifest_path,
            json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

        statuses: dict[str, int] = {}
        for item in entries.values():
            statuses[str(item.get("status"))] = statuses.get(str(item.get("status")), 0) + 1
        return {
            "status": "BUILT",
            "index_dir": str(self.index_dir),
            "manifest": manifest.as_dict(),
            "extraction_status_counts": dict(sorted(statuses.items())),
            "extraction_attempts": extraction_attempts,
            "hard_failure_count": len(hard_failures),
            "hard_failures": sorted(hard_failures, key=lambda item: str(item["paper_id"]))[
                :_MAX_REPORTED_FAILURES
            ],
            "degraded": refusal is not None,
        }

    def _systemic_failure(
        self, extraction_attempts: int, hard_failures: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """The refusal payload when this run's hard failures look environmental.

        The denominator is the attempts that exercised the extraction
        machinery: a missing file or an unsupported format says nothing about
        whether ``pypdf`` works, so those outcomes must not dilute the share.
        """

        failed = len(hard_failures)
        if extraction_attempts <= 0 or failed == 0:
            return None
        share = failed / extraction_attempts
        if failed < extraction_attempts and share <= self.max_failure_share:
            return None

        causes: dict[str, int] = {}
        for item in hard_failures:
            cause = str(item["status"])
            if item.get("detail"):
                cause = f"{cause}: {item['detail']}"
            causes[cause] = causes.get(cause, 0) + 1
        dominant_cause, dominant_count = max(causes.items(), key=lambda kv: (kv[1], kv[0]))

        if failed == extraction_attempts:
            scale = f"every extraction attempt ({failed} of {failed}) failed hard"
        else:
            scale = (
                f"{failed} of {extraction_attempts} extraction attempts failed hard"
                f" ({share:.0%} > {self.max_failure_share:.0%} allowed)"
            )
        reason = (
            f"Refusing to commit the index: {scale}."
            f" Dominant cause ({dominant_count} of {failed} failures): {dominant_cause}."
            " The previous index, if any, was left untouched."
        )
        payload: dict[str, Any] = {
            "status": "REFUSED",
            "reason": reason,
            "extraction_attempts": extraction_attempts,
            "hard_failure_count": failed,
            "hard_failure_share": round(share, 4),
            "max_failure_share": self.max_failure_share,
            "failure_causes": dict(sorted(causes.items())),
            "hard_failures": sorted(hard_failures, key=lambda item: str(item["paper_id"]))[
                :_MAX_REPORTED_FAILURES
            ],
            "previous_index_kept": True,
            "index_dir": str(self.index_dir),
            "override": "pass allow_degraded=True (CLI: --allow-degraded) to commit anyway",
        }
        if any(item["status"] == EXTRACTION_DEPENDENCY_MISSING for item in hard_failures):
            payload["missing_dependency_hint"] = (
                "the PDF extraction dependency is not importable in this"
                " interpreter (see failure_causes, e.g. pypdf); install it or"
                " rerun under the project virtualenv instead of rebuilding"
            )
        return payload

    def rebuild(self, snapshot: CatalogSnapshot, **kwargs: Any) -> dict[str, Any]:
        """Build a replacement index, then swap it in.

        The previous index directory is deleted only inside the commit phase,
        after extraction has finished and the fail-closed guard has passed.
        A rebuild under a broken environment -- an interpreter without
        ``pypdf``, say -- raises :class:`IndexBuildRefused` and keeps the
        existing index instead of replacing it with an empty one.
        """

        return self.build(snapshot, fresh=True, **kwargs)

    def drop(self) -> None:
        require_output_path(self.index_dir, label="Navigator index dir")
        if _windows_io_path(self.index_dir).exists():
            shutil.rmtree(_windows_io_path(self.index_dir))

    # -- validate ----------------------------------------------------------

    def validate(self, snapshot: CatalogSnapshot) -> dict[str, Any]:
        """Check the index against the catalog without repairing anything."""

        status, detail = self.status(snapshot)
        problems: list[dict[str, str]] = []
        entries = self._read_fulltext_manifest()
        catalog_ids = {work.paper_id for work in snapshot.works}

        for paper_id in sorted(set(entries) - catalog_ids):
            problems.append({"paper_id": paper_id, "problem": "indexed work is not in the catalog"})

        checked = 0
        for work in snapshot.works:
            entry = entries.get(work.paper_id)
            if entry is None:
                continue
            checked += 1
            resolution = self.resolver.resolve(work)
            expected = resolution.preferred.version.sha256 if resolution.preferred else ""
            if entry.get("status") == EXTRACTION_OK and str(entry.get("source_sha256")) != expected:
                problems.append(
                    {
                        "paper_id": work.paper_id,
                        "problem": "indexed text was extracted from a different version sha256",
                    }
                )
            if int(entry.get("chunk_count", 0)) > 0:
                path = self._chunk_path(work.paper_id)
                if not _windows_io_path(path).is_file():
                    problems.append({"paper_id": work.paper_id, "problem": "chunk file missing"})

        return {
            "index_status": status.value,
            "detail": detail,
            "works_in_catalog": len(catalog_ids),
            "works_in_index": len(entries),
            "works_checked": checked,
            "problems": problems,
            "valid": status is IndexStatus.FRESH and not problems,
        }

    # -- load --------------------------------------------------------------

    def load_fulltext(self, snapshot: CatalogSnapshot) -> tuple[FullTextIndex, IndexStatus, dict[str, Any]]:
        """Load chunks, reporting the same status every other command reports."""

        status, detail = self.resolved_status(snapshot)
        if status in (IndexStatus.ABSENT, IndexStatus.UNREADABLE):
            return FullTextIndex(), status, detail

        entries = self._read_fulltext_manifest()
        stale = self.stale_works(snapshot)

        chunks_by_work: dict[str, tuple[Chunk, ...]] = {}
        for paper_id, entry in entries.items():
            if entry.get("status") != EXTRACTION_OK:
                continue
            loaded = self._read_chunks(paper_id)
            if not loaded:
                continue
            chunks_by_work[paper_id] = loaded

        index = FullTextIndex(
            chunks_by_work=chunks_by_work,
            stale_works=frozenset(stale),
            manifest_entries=entries,
        )
        return index, status, detail

    # -- internals ---------------------------------------------------------

    def _chunk_path(self, paper_id: str) -> Path:
        return self.fulltext_dir / f"{paper_id}.jsonl"

    def _write_chunks(self, paper_id: str, chunks: tuple[Chunk, ...]) -> None:
        content = "".join(
            json.dumps(chunk.as_dict(), ensure_ascii=False, sort_keys=True) + "\n" for chunk in chunks
        )
        _atomic_write_text(self._chunk_path(paper_id), content)

    def _remove_chunk_file(self, paper_id: str) -> None:
        path = self._chunk_path(paper_id)
        if _windows_io_path(path).exists():
            require_output_path(path, label="Navigator chunk file")
            _windows_io_path(path).unlink()

    def _read_chunks(self, paper_id: str) -> tuple[Chunk, ...]:
        path = self._chunk_path(paper_id)
        path_io = _windows_io_path(path)
        if not path_io.is_file():
            return ()
        chunks: list[Chunk] = []
        try:
            for line in path_io.read_text(encoding="utf-8-sig").splitlines():
                if not line.strip():
                    continue
                chunks.append(Chunk.from_dict(json.loads(line)))
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return ()
        return tuple(chunks)

    def _read_fulltext_manifest(self) -> dict[str, dict[str, Any]]:
        manifest_io = _windows_io_path(self.fulltext_manifest_path)
        if not manifest_io.is_file():
            return {}
        try:
            payload = json.loads(manifest_io.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if isinstance(entry, dict) and entry.get("paper_id"):
                result[str(entry["paper_id"])] = entry
        return result


__all__ = [
    "DEFAULT_MAX_FAILURE_SHARE",
    "HARD_FAILURE_STATUSES",
    "INDEX_SCHEMA_VERSION",
    "FullTextIndex",
    "IndexBuildRefused",
    "IndexManifest",
    "IndexStatus",
    "NavigatorIndex",
    "file_sha256",
]
