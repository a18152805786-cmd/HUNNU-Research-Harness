"""Read-only fingerprint of the frozen paper library.

This exists so that "the Navigator changed nothing" is a measurement rather than
an assurance.  It hashes the catalog files byte-for-byte, every managed physical
file, the logical work/version/topic shape, and the human-readable topic view,
then reduces all of it to one digest that any rename, re-hash, re-import, or
relink would move.

It opens nothing for writing inside the Library.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import (
    LIBRARY_CATALOG_DIR,
    LIBRARY_PAPERS_DIR,
    PAPERS_BY_TOPIC_DIR,
    _logical_path,
    _windows_io_path,
)
from .catalog import CatalogReader, CatalogSnapshot


FINGERPRINT_SCHEMA_VERSION = "navigator-fingerprint-0.1"


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with _windows_io_path(path).open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class LibraryFingerprint:
    payload: dict[str, Any]

    @property
    def digest(self) -> str:
        return str(self.payload["fingerprint_sha256"])

    def as_dict(self) -> dict[str, Any]:
        return self.payload

    def to_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def compare(self, other: "LibraryFingerprint | dict[str, Any]") -> dict[str, Any]:
        """Diff two fingerprints field by field, not just by digest."""

        previous = other.payload if isinstance(other, LibraryFingerprint) else dict(other)
        differences: list[dict[str, Any]] = []

        for key in ("catalog_records", "physical_versions", "nested_variants",
                    "papers_file_count", "topic_assignment_count", "unique_topic_values",
                    "papers_by_topic_entries"):
            before, after = previous.get(key), self.payload.get(key)
            if before != after:
                differences.append({"field": key, "before": before, "after": after})

        for section in ("catalog_files", "papers_files", "canonical_sha_map",
                        "managed_path_map", "topics_per_paper", "papers_by_topic"):
            before_map = previous.get(section) or {}
            after_map = self.payload.get(section) or {}
            for key in sorted(set(before_map) | set(after_map)):
                if before_map.get(key) != after_map.get(key):
                    differences.append(
                        {
                            "field": f"{section}.{key}",
                            "before": before_map.get(key),
                            "after": after_map.get(key),
                        }
                    )

        return {
            "identical": not differences and previous.get("fingerprint_sha256") == self.digest,
            "digest_before": previous.get("fingerprint_sha256"),
            "digest_after": self.digest,
            "difference_count": len(differences),
            "differences": differences[:200],
        }


class LibraryFingerprinter:
    """Compute a comparable fingerprint of the frozen library."""

    def __init__(
        self,
        *,
        catalog_dir: Path | None = None,
        papers_dir: Path | None = None,
        by_topic_dir: Path | None = None,
        reader: CatalogReader | None = None,
    ) -> None:
        self.catalog_dir = _logical_path(catalog_dir or LIBRARY_CATALOG_DIR)
        self.papers_dir = _logical_path(papers_dir or LIBRARY_PAPERS_DIR)
        self.by_topic_dir = _logical_path(by_topic_dir or PAPERS_BY_TOPIC_DIR)
        self.reader = reader or CatalogReader()

    def capture(self, *, snapshot: CatalogSnapshot | None = None) -> LibraryFingerprint:
        payload: dict[str, Any] = {"schema_version": FINGERPRINT_SCHEMA_VERSION}

        payload["catalog_files"] = {
            _logical_path(path).name: {
                "sha256": file_sha256(path),
                "size": _windows_io_path(path).stat().st_size,
            }
            for path in sorted(_windows_io_path(self.catalog_dir).glob("*"))
            if _windows_io_path(path).is_file()
        }

        papers: dict[str, dict[str, Any]] = {}
        if _windows_io_path(self.papers_dir).is_dir():
            for path in sorted(_windows_io_path(self.papers_dir).iterdir()):
                if _windows_io_path(path).is_file():
                    logical = _logical_path(path)
                    papers[logical.name] = {
                        "sha256": file_sha256(path),
                        "size": _windows_io_path(path).stat().st_size,
                    }
        payload["papers_files"] = papers
        payload["papers_file_count"] = len(papers)

        snapshot = snapshot if snapshot is not None else self.reader.load()
        payload["catalog_records"] = snapshot.work_count
        payload["physical_versions"] = snapshot.version_count
        payload["nested_variants"] = snapshot.version_count - snapshot.work_count
        payload["topic_assignment_count"] = snapshot.topic_assignment_count
        payload["paper_ids"] = sorted(work.paper_id for work in snapshot.works)
        payload["canonical_sha_map"] = {
            work.paper_id: work.canonical_sha256 for work in snapshot.works
        }
        payload["version_sha_map"] = {
            work.paper_id: sorted(version.sha256 for version in work.versions)
            for work in snapshot.works
        }
        payload["managed_path_map"] = {
            work.paper_id: sorted(version.managed_path for version in work.versions)
            for work in snapshot.works
        }
        payload["topics_per_paper"] = {
            work.paper_id: sorted(work.topic_labels) for work in snapshot.works
        }
        unique_topics: set[str] = set()
        for work in snapshot.works:
            unique_topics.update(work.topic_labels)
        payload["unique_topic_values"] = len(unique_topics)

        view: dict[str, int] = {}
        if _windows_io_path(self.by_topic_dir).is_dir():
            root = _logical_path(self.by_topic_dir)
            for path in sorted(_windows_io_path(self.by_topic_dir).rglob("*")):
                if _windows_io_path(path).is_file():
                    logical = _logical_path(path)
                    relative = logical.relative_to(root).as_posix()
                    view[relative] = _windows_io_path(path).stat().st_size
        payload["papers_by_topic"] = dict(sorted(view.items()))
        payload["papers_by_topic_entries"] = len(view)

        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        payload["fingerprint_sha256"] = hashlib.sha256(blob).hexdigest()
        return LibraryFingerprint(payload=payload)


def load_fingerprint(path: Path) -> LibraryFingerprint:
    return LibraryFingerprint(payload=json.loads(_windows_io_path(path).read_text(encoding="utf-8-sig")))


__all__ = [
    "FINGERPRINT_SCHEMA_VERSION",
    "LibraryFingerprint",
    "LibraryFingerprinter",
    "file_sha256",
    "load_fingerprint",
]
