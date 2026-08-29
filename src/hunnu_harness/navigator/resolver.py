"""Derived preferred-version resolution.

The catalog records *which* files belong to a work but has no field that says
which one to read: ``version_role`` is 95/179 ``unknown`` and is mostly
provenance, not role.  So the Navigator derives an ordering -- and derives it
strictly, writing nothing and reasserting nothing.

The catalog does already pin one canonical file per work, through the top-level
``sha256`` / ``managed_fulltext_path`` pair.  That anchor dominates this
scoring function by an order of magnitude, so the resolver can never disagree
with the catalog about which file is canonical; role, format, and readability
only order the remaining variants.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ..literature.models import UNKNOWN
from .catalog import PaperVersion, PaperWork


class FullTextStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    AVAILABLE_NOT_MACHINE_READABLE = "AVAILABLE_NOT_MACHINE_READABLE"
    FILE_MISSING = "FILE_MISSING"
    UNREADABLE = "UNREADABLE"
    NO_VERSION = "NO_VERSION"


#: Formats the Harness can extract text from today.  CAJ is lawfully held and
#: listed, but no CAJ text extractor exists in this environment, so a CAJ-only
#: work is reported as present-but-not-machine-readable rather than as readable.
MACHINE_READABLE_FORMATS = frozenset({"PDF"})

#: Higher is preferred.  Values not listed fall back to 0.
VERSION_ROLE_RANK: dict[str, int] = {
    "CANONICAL_VERSION": 9,
    "PEERREVIEWEDJOURNALARTICLE": 8,
    "FINALPUBLICATION": 8,
    "PUBLISHERPDF": 7,
    "VERSIONOFRECORD": 7,
    "ACCEPTEDMANUSCRIPT": 5,
    "NETWORKFIRST": 4,
    "网络首发": 4,
    "HARNESS_DOWNLOAD": 3,
    "EXTERNAL_IMPORT": 2,
    "OTHER": 1,
    "FORMAT_VARIANT": 1,
    UNKNOWN.upper(): 0,
}

FORMAT_RANK: dict[str, int] = {"PDF": 3, "CAJ": 2}


@dataclass(frozen=True)
class ResolvedVersion:
    """One version with everything a caller needs to open or cite it."""

    version: PaperVersion
    score: float
    is_catalog_canonical: bool
    exists: bool
    machine_readable: bool
    reason: str

    @property
    def absolute_path(self) -> Path:
        return self.version.absolute_path

    def as_dict(self) -> dict[str, Any]:
        payload = self.version.as_dict()
        payload.update(
            {
                "is_catalog_canonical": self.is_catalog_canonical,
                "exists": self.exists,
                "machine_readable": self.machine_readable,
                "preference_reason": self.reason,
            }
        )
        return payload


@dataclass(frozen=True)
class VersionResolution:
    """The full version picture for one WORK."""

    paper_id: str
    preferred: ResolvedVersion | None
    versions: tuple[ResolvedVersion, ...]
    fulltext_status: FullTextStatus

    @property
    def available_count(self) -> int:
        return sum(1 for item in self.versions if item.exists)

    @property
    def readable(self) -> bool:
        return self.fulltext_status is FullTextStatus.AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "fulltext_status": self.fulltext_status.value,
            "readable": self.readable,
            "version_count": len(self.versions),
            "available_version_count": self.available_count,
            "preferred_version": self.preferred.as_dict() if self.preferred else None,
            "available_versions": [item.as_dict() for item in self.versions],
        }


def _role_rank(role: str) -> int:
    return VERSION_ROLE_RANK.get(str(role).strip().upper().replace(" ", ""), 0)


def _format_rank(full_text_format: str) -> int:
    return FORMAT_RANK.get(str(full_text_format).strip().upper(), 0)


def _machine_readable(version: PaperVersion, *, exists: bool) -> bool:
    return exists and version.full_text_format.upper() in MACHINE_READABLE_FORMATS


class PreferredVersionResolver:
    """Order a work's physical versions without changing any of them."""

    def resolve(self, work: PaperWork) -> VersionResolution:
        if not work.versions:
            return VersionResolution(
                paper_id=work.paper_id,
                preferred=None,
                versions=(),
                fulltext_status=FullTextStatus.NO_VERSION,
            )

        resolved: list[ResolvedVersion] = []
        for version in work.versions:
            exists = version.exists()
            is_canonical = bool(
                work.canonical_sha256
                and work.canonical_sha256 != UNKNOWN
                and version.sha256 == work.canonical_sha256
            )
            readable = _machine_readable(version, exists=exists)
            score = (
                1000.0 * float(is_canonical)
                + 100.0 * _role_rank(version.version_role)
                + 10.0 * _format_rank(version.full_text_format)
                + 1.0 * float(readable)
            )
            resolved.append(
                ResolvedVersion(
                    version=version,
                    score=score,
                    is_catalog_canonical=is_canonical,
                    exists=exists,
                    machine_readable=readable,
                    reason=self._reason(
                        is_canonical=is_canonical,
                        version=version,
                        exists=exists,
                        readable=readable,
                    ),
                )
            )

        # Deterministic: score, then sha256, so equal candidates never depend on
        # catalog ordering.
        resolved.sort(key=lambda item: (-item.score, item.version.sha256))
        preferred = resolved[0]
        return VersionResolution(
            paper_id=work.paper_id,
            preferred=preferred,
            versions=tuple(resolved),
            fulltext_status=self._status(preferred, resolved),
        )

    @staticmethod
    def _status(preferred: ResolvedVersion, resolved: list[ResolvedVersion]) -> FullTextStatus:
        if preferred.machine_readable:
            return FullTextStatus.AVAILABLE
        if any(item.machine_readable for item in resolved):
            # Should not happen -- readability is part of the score -- but if a
            # readable sibling exists the work is still readable.
            return FullTextStatus.AVAILABLE
        if preferred.exists or any(item.exists for item in resolved):
            return FullTextStatus.AVAILABLE_NOT_MACHINE_READABLE
        return FullTextStatus.FILE_MISSING

    @staticmethod
    def _reason(*, is_canonical: bool, version: PaperVersion, exists: bool, readable: bool) -> str:
        parts: list[str] = []
        if is_canonical:
            parts.append("catalog canonical sha256")
        role = version.version_role
        if role and role != UNKNOWN:
            parts.append(f"version_role={role}")
        parts.append(f"format={version.full_text_format}")
        if not exists:
            parts.append("file missing on disk")
        elif not readable:
            parts.append("format not machine-readable in this environment")
        return "; ".join(parts)


__all__ = [
    "FORMAT_RANK",
    "FullTextStatus",
    "MACHINE_READABLE_FORMATS",
    "PreferredVersionResolver",
    "ResolvedVersion",
    "VERSION_ROLE_RANK",
    "VersionResolution",
]
