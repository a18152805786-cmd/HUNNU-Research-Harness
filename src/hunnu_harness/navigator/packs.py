"""Reading packs: references, never copies.

A pack is a curated pointer set.  Fifteen papers do not become fifteen more
PDFs -- the corpus already holds exactly one canonical copy of each work and a
second body copy would create a second identity to keep consistent.  Every entry
records the ``paper_id``, the resolved canonical path, and the SHA-256 of the
file it points at, so a pack written today can be re-validated against the
library tomorrow and say precisely what moved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import PAPER_RETRIEVAL_PACKS_DIR, require_output_path
from .lexicon import RelevanceRole
from .search import PaperNavigator, SearchResult


PACK_SCHEMA_VERSION = "navigator-pack-0.1"

#: Order roles appear in the generated README: the reading order a researcher
#: actually wants, not the score order.
ROLE_ORDER: tuple[RelevanceRole, ...] = (
    RelevanceRole.CORE,
    RelevanceRole.MECHANISM,
    RelevanceRole.OUTCOME,
    RelevanceRole.METHOD,
    RelevanceRole.BACKGROUND,
)

ROLE_GUIDANCE: dict[RelevanceRole, str] = {
    RelevanceRole.CORE: "Directly about the phenomenon in the question. Read first; these set the baseline claim.",
    RelevanceRole.MECHANISM: "Support or challenge the proposed channel. Read for the mediating construct and its measurement.",
    RelevanceRole.OUTCOME: "Establish how the dependent construct is measured and what is already known about it.",
    RelevanceRole.METHOD: "Identification strategy, variable construction, and robustness practice.",
    RelevanceRole.BACKGROUND: "Context and adjacent literature. Skim unless a specific point needs support.",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pack_id(query: str, *, now: datetime | None = None) -> str:
    moment = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:8].upper()
    return f"PACK_{moment}_{digest}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


@dataclass
class PackEntry:
    result: SearchResult
    canonical_sha256: str

    def as_dict(self) -> dict[str, Any]:
        preferred = self.result.resolution.preferred
        return {
            "paper_id": self.result.work.paper_id,
            "title": self.result.work.title,
            "authors": list(self.result.work.authors),
            "year": self.result.work.year,
            "journal": self.result.work.journal,
            "doi": self.result.work.doi,
            "topics": list(self.result.work.topic_labels),
            "relevance_role": self.result.role.value,
            "secondary_roles": [role.value for role in self.result.secondary_roles],
            "relevance_score": round(self.result.score, 4),
            "relevance_reason": self.result.reason,
            "matched_by": [record.as_dict() for record in self.result.matches[:8]],
            "fulltext_status": self.result.resolution.fulltext_status.value,
            "canonical_path": preferred.version.managed_path if preferred else None,
            "absolute_path": str(preferred.absolute_path) if preferred else None,
            "preferred_version": preferred.as_dict() if preferred else None,
            "verified_sha256_at_pack_time": self.canonical_sha256,
            "notes_path": self.result.work.notes_path,
        }


class ReadingPackBuilder:
    """Build a reading pack from a research question, copying nothing."""

    def __init__(self, navigator: PaperNavigator, *, packs_dir: Path | None = None) -> None:
        self.navigator = navigator
        self.packs_dir = Path(packs_dir or PAPER_RETRIEVAL_PACKS_DIR)

    def build(
        self,
        query: str,
        *,
        top: int = 15,
        use_fulltext: bool = True,
        write: bool = True,
        verify_hashes: bool = True,
    ) -> dict[str, Any]:
        search = self.navigator.search(query, top=top, use_fulltext=use_fulltext)
        results = self._results_from(search, top=top)

        entries: list[PackEntry] = []
        seen: set[str] = set()
        for result in results:
            if result.work.paper_id in seen:
                continue  # cannot happen with WORK-level results; asserted anyway
            seen.add(result.work.paper_id)
            preferred = result.resolution.preferred
            digest = ""
            if verify_hashes and preferred is not None and preferred.exists:
                digest = _sha256_file(preferred.absolute_path)
            entries.append(PackEntry(result=result, canonical_sha256=digest))

        pack_id = _pack_id(query)
        manifest = {
            "schema_version": PACK_SCHEMA_VERSION,
            "pack_id": pack_id,
            "created_at": _timestamp(),
            "query": query,
            "query_normalization": search["query_normalization"],
            "index_status": search["index_status"],
            "requested_top": top,
            "entry_count": len(entries),
            "unique_works": len(seen),
            "copies_made": 0,
            "entries": [entry.as_dict() for entry in entries],
        }
        ranking = {
            "pack_id": pack_id,
            "query": query,
            "ranking": [
                {
                    "rank": position,
                    "paper_id": entry.result.work.paper_id,
                    "relevance_score": round(entry.result.score, 4),
                    "relevance_role": entry.result.role.value,
                    "score_breakdown": {
                        "metadata": round(entry.result.stage1_score, 4),
                        "fulltext": round(entry.result.passage_score, 4),
                    },
                }
                for position, entry in enumerate(entries, start=1)
            ],
        }
        evidence_plan = self._evidence_plan(pack_id, query, entries)
        readme = self._readme(pack_id, query, entries, search)

        payload: dict[str, Any] = {
            **self.navigator.envelope(),
            "status": "BUILT",
            "pack_id": pack_id,
            "query": query,
            "entry_count": len(entries),
            "unique_works": len(seen),
            "copies_made": 0,
            "pack_dir": None,
            "manifest": manifest,
        }

        if write:
            pack_dir = self.packs_dir / pack_id
            require_output_path(pack_dir, label="Navigator reading pack")
            pack_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(pack_dir / "manifest.json", manifest)
            self._write_json(pack_dir / "ranking.json", ranking)
            self._write_json(pack_dir / "evidence_plan.json", evidence_plan)
            (pack_dir / "README.md").write_text(readme, encoding="utf-8", newline="\n")
            payload["pack_dir"] = str(pack_dir)
        return payload

    def _results_from(self, search: dict[str, Any], *, top: int) -> list[SearchResult]:
        """Re-resolve the serialised search rows back into result objects."""

        results: list[SearchResult] = []
        for row in search["results"][:top]:
            work = self.navigator.snapshot.by_id(row["paper_id"])
            if work is None:
                continue
            results.append(
                SearchResult(
                    work=work,
                    resolution=self.navigator.resolution_for(work),
                    score=float(row["relevance_score"]),
                    stage1_score=float(row["score_breakdown"]["metadata"]),
                    passage_score=float(row["score_breakdown"]["fulltext"]),
                    matches=(),
                    concept_evidence=(),
                    passages=(),
                    role=RelevanceRole(row["relevance_role"]),
                    reason=row["relevance_reason"],
                    secondary_roles=tuple(
                        RelevanceRole(value) for value in row.get("secondary_roles", [])
                    ),
                )
            )
        return results

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _evidence_plan(self, pack_id: str, query: str, entries: list[PackEntry]) -> dict[str, Any]:
        by_role: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            preferred = entry.result.resolution.preferred
            by_role.setdefault(entry.result.role.value, []).append(
                {
                    "paper_id": entry.result.work.paper_id,
                    "title": entry.result.work.title,
                    "what_to_extract": ROLE_GUIDANCE.get(entry.result.role, ""),
                    "fulltext_status": entry.result.resolution.fulltext_status.value,
                    "canonical_path": preferred.version.managed_path if preferred else None,
                    "readable_locally": bool(preferred and preferred.machine_readable),
                }
            )
        return {
            "schema_version": PACK_SCHEMA_VERSION,
            "pack_id": pack_id,
            "query": query,
            "reading_order": [role.value for role in ROLE_ORDER if role.value in by_role],
            "by_role": by_role,
            "unreadable_locally": [
                entry.result.work.paper_id
                for entry in entries
                if not (
                    entry.result.resolution.preferred
                    and entry.result.resolution.preferred.machine_readable
                )
            ],
        }

    def _readme(
        self,
        pack_id: str,
        query: str,
        entries: list[PackEntry],
        search: dict[str, Any],
    ) -> str:
        lines = [
            f"# Reading pack {pack_id}",
            "",
            f"- Research question: `{query}`",
            f"- Built: {_timestamp()}",
            f"- Works: {len(entries)} (unique WORKS; no duplicates, no PDF copies)",
            f"- Index status at build: {search['index_status']}",
            "",
            "This pack contains **references only**. Every paper stays at its single",
            "canonical location in the Global Paper Library; nothing was copied.",
            "",
        ]
        for role in ROLE_ORDER:
            group = [entry for entry in entries if entry.result.role is role]
            if not group:
                continue
            lines.append(f"## {role.value}")
            lines.append("")
            lines.append(ROLE_GUIDANCE.get(role, ""))
            lines.append("")
            for entry in group:
                work = entry.result.work
                preferred = entry.result.resolution.preferred
                path = preferred.version.managed_path if preferred else "(no managed version)"
                lines.append(f"- **{work.title}**")
                lines.append(
                    f"  - `{work.paper_id}` · {work.year} · {work.journal}"
                    + (f" · DOI {work.doi}" if work.doi and work.doi != "unknown" else "")
                )
                lines.append(f"  - full text: `{path}` ({entry.result.resolution.fulltext_status.value})")
                lines.append(f"  - why: {entry.result.reason}")
            lines.append("")
        lines.append("## Provenance")
        lines.append("")
        lines.append("`manifest.json` records the SHA-256 of each canonical file as it stood when")
        lines.append("this pack was built, so the pack can later be re-validated against the library.")
        lines.append("")
        return "\n".join(lines)


__all__ = ["PACK_SCHEMA_VERSION", "PackEntry", "ROLE_GUIDANCE", "ROLE_ORDER", "ReadingPackBuilder"]
