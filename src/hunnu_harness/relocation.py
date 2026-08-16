"""Safe classification and integrity helpers for output relocation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .paths import CORE_ROOT, OUTPUT_ROOT, is_within


class RelocationClassification(StrEnum):
    CORE = "CORE"
    GENERATED_OUTPUT = "GENERATED_OUTPUT"
    HISTORICAL_FORMAL_ARTIFACT = "HISTORICAL_FORMAL_ARTIFACT"
    UNCERTAIN = "UNCERTAIN"


CORE_DIRECTORIES = frozenset(
    {"src", "tests", "config", "docs", "scripts", "secrets", ".git", ".venv"}
)
CORE_FILES = frozenset(
    {
        "AGENTS.md",
        "README.md",
        ".gitignore",
        ".gitattributes",
        "pyproject.toml",
        "BROWSER_USE_EVALUATION.md",
        "ENVIRONMENT_AUDIT.md",
    }
)
FORMAL_DIRECTORIES = frozenset({"runs", "manifests"})
GENERATED_DIRECTORIES = frozenset(
    {"downloads", "logs", "screenshots", "temp", "tmp", ".pytest_cache"}
)
FORMAL_ROOT_FILES = frozenset(
    {
        "ARTIFACT_MIGRATION_AUDIT.md",
        "ARTIFACT_MIGRATION_HASH_CHECK.csv",
        "ARTIFACT_PATH_MIGRATION_MAP.csv",
        "PATH_MIGRATION_AUDIT.md",
    }
)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def classify_core_path(path: Path, *, core_root: Path = CORE_ROOT) -> RelocationClassification:
    candidate = Path(path).resolve()
    root = Path(core_root).resolve()
    if not is_within(candidate, root):
        return RelocationClassification.UNCERTAIN
    relative = candidate.relative_to(root)
    if not relative.parts:
        return RelocationClassification.CORE
    first = relative.parts[0]
    if first in CORE_DIRECTORIES or first in CORE_FILES:
        return RelocationClassification.CORE
    if first in FORMAL_DIRECTORIES or first in FORMAL_ROOT_FILES:
        return RelocationClassification.HISTORICAL_FORMAL_ARTIFACT
    if first in GENERATED_DIRECTORIES:
        return RelocationClassification.GENERATED_OUTPUT
    if len(relative.parts) == 1 and (
        candidate.suffix.casefold() == ".log"
        or candidate.name.casefold().startswith("v21post_")
        or candidate.name == "v13_path_validation_20260814_post_cleanup.md"
    ):
        return RelocationClassification.GENERATED_OUTPUT
    return RelocationClassification.UNCERTAIN


@dataclass(frozen=True)
class RelocationRecord:
    old_path: str
    new_path: str
    classification: str
    size: int
    sha256_before: str
    sha256_after: str
    content_changed: bool
    move_status: str

    def as_manifest_entry(self) -> dict[str, Any]:
        return {
            "OldPath": self.old_path,
            "NewPath": self.new_path,
            "Classification": self.classification,
            "Size": self.size,
            "SHA256Before": self.sha256_before,
            "SHA256After": self.sha256_after,
            "ContentChanged": self.content_changed,
            "MoveStatus": self.move_status,
        }


def verify_relocated_file(expected_sha256: str, destination: Path) -> bool:
    candidate = Path(destination)
    return candidate.is_file() and sha256_file(candidate) == expected_sha256


def record_verified_relocation(
    source: Path,
    destination: Path,
    *,
    classification: RelocationClassification,
    sha256_before: str,
    size: int,
) -> RelocationRecord:
    after = sha256_file(destination)
    changed = after != sha256_before
    return RelocationRecord(
        old_path=str(Path(source)),
        new_path=str(Path(destination)),
        classification=classification.value,
        size=size,
        sha256_before=sha256_before,
        sha256_after=after,
        content_changed=changed,
        move_status="MOVED_VERIFIED" if not changed else "HASH_MISMATCH",
    )
