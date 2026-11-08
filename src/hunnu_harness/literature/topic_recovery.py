"""Conservative recovery of a retained reclassification's hardlink view.

Canonical topic/provenance bytes must already equal the saved preimages. This
entry point restores the old view only; it never overwrites a later assignment,
edits a frozen configuration, copies a PDF, or discards the recovery package.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..paths import _transaction_token, _windows_io_path, is_within, require_output_path
from .auto_classification import PostAcquisitionClassifier
from .topics import TopicTaxonomy


@dataclass(frozen=True)
class TopicRecoveryResult:
    status: str
    recovery_path: str
    reason: str
    dry_run: bool = True
    current_fingerprint: str = ""
    paper_id: str = ""
    old_topics: tuple[str, ...] = ()
    remove_aliases: tuple[str, ...] = ()
    restore_aliases: tuple[str, ...] = ()
    recovered: bool = False
    audit_path: str = ""
    audit_error: str = ""
    canonical_sha256: str = ""
    metadata_sha256: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status in {"DRY_RUN", "RECOVERED", "ALREADY_RECOVERED"}

    def as_dict(self) -> dict[str, Any]:
        return {"RecoveryStatus": self.status, "RecoveryPath": self.recovery_path,
                "Reason": self.reason, "DryRun": self.dry_run,
                "CurrentFingerprint": self.current_fingerprint, "PaperID": self.paper_id,
                "OriginalTopics": list(self.old_topics), "RemoveAliases": list(self.remove_aliases),
                "RestoreAliases": list(self.restore_aliases), "Recovered": self.recovered,
                "AuditPath": self.audit_path, "AuditError": self.audit_error,
                "CanonicalSHA256": self.canonical_sha256, "MetadataSHA256": self.metadata_sha256,
                "PDFCopiesCreated": 0, "MetadataOverwritten": False, "RecoveryPackagePreserved": True}


class _RecoveryRefusal(ValueError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_digest(data: Any) -> str:
    return _digest(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _checked(path: Path) -> Path:
    if not path.is_absolute():
        raise _RecoveryRefusal("RECOVERY_REFUSED", "Recovery paths must be absolute")
    for candidate in (path, *path.parents):
        io = _windows_io_path(candidate)
        if io.is_symlink() or getattr(io, "is_junction", lambda: False)():
            raise _RecoveryRefusal("RECOVERY_REFUSED", "Recovery paths cannot contain links or junctions")
    return require_output_path(path, label="Topic recovery path")


def _optional_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _plan(service: PostAcquisitionClassifier, recovery: Path, expected_preimage_sha256: str) -> dict[str, Any]:
    root = service.output_root
    recovery = _checked(recovery)
    if recovery.parent != root / "temp/topic-reclassification" or not re.fullmatch(r"[a-f0-9]{8}", recovery.name):
        raise _RecoveryRefusal("RECOVERY_REFUSED", "Select one retained standard reclassification package")
    journal_path = _checked(recovery / "PREIMAGE.json")
    raw = journal_path.read_bytes()
    if _digest(raw) != expected_preimage_sha256.casefold():
        raise _RecoveryRefusal("PREIMAGE_CONFLICT", "Recovery PREIMAGE SHA256 differs from the reviewed receipt")
    journal = json.loads(raw)
    if not isinstance(journal, dict) or set(journal) != {"paper_id", "old_links", "canonical_path", "files"}:
        raise _RecoveryRefusal("RECOVERY_REFUSED", "Unsupported preimage schema")
    expected_paths = (service.store.jsonl_path, service.store.csv_path, service.view.links_path, service.provenance.path)
    entries = journal["files"]
    if not isinstance(entries, list) or len(entries) != len(expected_paths):
        raise _RecoveryRefusal("RECOVERY_REFUSED", "Incomplete preimage file list")
    metadata: dict[str, str] = {}
    for number, (entry, expected) in enumerate(zip(entries, expected_paths)):
        if not isinstance(entry, dict) or set(entry) != {"path", "backup"} or not isinstance(entry["path"], str) or _checked(Path(entry["path"])) != expected:
            raise _RecoveryRefusal("RECOVERY_REFUSED", "Preimage file targets differ from this service's canonical layout")
        backup_name = entry["backup"]
        if backup_name is not None and backup_name != f"{number}.before":
            raise _RecoveryRefusal("RECOVERY_REFUSED", "Unexpected backup filename")
        before = None if backup_name is None else _checked(recovery / backup_name).read_bytes()
        current = _optional_bytes(expected)
        if current != before:
            raise _RecoveryRefusal("METADATA_CHANGED", "Current metadata differs from the preimage; this view-only recovery cannot overwrite it")
        metadata[str(expected)] = _digest(current) if current is not None else "ABSENT"
    paper_id = journal["paper_id"]
    if not isinstance(paper_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", paper_id):
        raise _RecoveryRefusal("RECOVERY_REFUSED", "Invalid preimage WORK identity")
    row = service.store.load().get(paper_id)
    catalog = service._catalog_entry(paper_id)
    if row is None or catalog is None or not row.topics:
        raise _RecoveryRefusal("RECOVERY_REFUSED", "The original filed WORK is not present")
    TopicTaxonomy.load(root / "topic_taxonomy.json").require_known(row.topics)
    canonical = _checked(Path(journal["canonical_path"]))
    if canonical != _checked(Path(row.canonical_path)) or not is_within(canonical, root / "library/papers"):
        raise _RecoveryRefusal("RECOVERY_REFUSED", "Preimage canonical file differs from the filed WORK")
    canonical_hash = _digest(canonical.read_bytes())
    if canonical_hash != row.canonical_sha256 or canonical_hash != catalog.get("sha256"):
        raise _RecoveryRefusal("CANONICAL_CHANGED", "The managed PDF identity no longer matches the original metadata")
    managed_value = str(catalog.get("managed_pdf_path") or catalog.get("managed_fulltext_path") or "")
    managed_path = Path(managed_value)
    if not managed_path.is_absolute():
        managed_path = root / managed_path
    if not managed_value or _checked(managed_path) != canonical:
        raise _RecoveryRefusal("CANONICAL_CHANGED", "The catalog no longer names the same managed canonical file")
    name = row.human_readable_name or f"{paper_id}.pdf"
    if Path(name).name != name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((".", " ")):
        raise _RecoveryRefusal("RECOVERY_REFUSED", "Unsafe original view filename")
    expected_links = {service.view._safe_view_path(service.view.view_root / label.domain / label.subtopic / name) for label in row.topics}
    if not isinstance(journal["old_links"], list) or any(not isinstance(p, str) for p in journal["old_links"]):
        raise _RecoveryRefusal("RECOVERY_REFUSED", "Invalid original alias list")
    old_links = {_checked(Path(p)) for p in journal["old_links"]}
    if old_links != expected_links or len(old_links) != len(journal["old_links"]):
        raise _RecoveryRefusal("RECOVERY_REFUSED", "Original aliases must exactly represent the restored topic metadata")
    current_links = set(service.view._existing_links_for(paper_id, name))
    stats = {}
    for alias in current_links:
        alias = _checked(alias)
        if not is_within(alias, service.view.view_root) or not os.path.samefile(alias, canonical):
            raise _RecoveryRefusal("VIEW_CONFLICT", "A current view entry belongs to another file")
        st = alias.stat()
        stats[str(alias)] = (st.st_dev, st.st_ino, st.st_size, getattr(st, "st_file_attributes", 0))
    st = canonical.stat()
    original_stat = (st.st_dev, st.st_ino, st.st_size, getattr(st, "st_file_attributes", 0))
    fingerprint = _json_digest({"schema": "topic-recovery-view-only-0.1", "preimage_sha256": _digest(raw),
        "metadata_sha256": metadata, "catalog_sha256": _digest(service.catalog_path.read_bytes()),
        "taxonomy_sha256": _digest((root / "topic_taxonomy.json").read_bytes()), "canonical_sha256": canonical_hash,
        "canonical_stat": original_stat, "canonical_link_count": st.st_nlink, "aliases": stats})
    return {"recovery": recovery, "paper_id": paper_id, "canonical": canonical,
            "canonical_hash": canonical_hash, "canonical_stat": original_stat,
            "metadata": metadata, "old_links": old_links, "current_links": current_links,
            "fingerprint": fingerprint, "old_topics": tuple(t.label for t in row.topics)}


def recover_topic_view(service: PostAcquisitionClassifier, recovery_path: Path, *, reason: str,
                       expected_preimage_sha256: str, expected_current_fingerprint: str | None = None,
                       dry_run: bool = True) -> TopicRecoveryResult:
    base = {"recovery_path": str(recovery_path), "reason": reason, "dry_run": dry_run}
    try:
        if not isinstance(reason, str) or not reason.strip():
            raise _RecoveryRefusal("REASON_REQUIRED", "Recovery requires an explicit non-empty reason")
        if not isinstance(expected_preimage_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_preimage_sha256):
            raise _RecoveryRefusal("PREIMAGE_REQUIRED", "An explicit reviewed PREIMAGE SHA256 is required")
        if not dry_run and expected_current_fingerprint is None:
            raise _RecoveryRefusal("PRECONDITION_REQUIRED", "Apply requires the current recovery preview fingerprint")
        if dry_run:
            return _recover(service, recovery_path, expected_preimage_sha256, expected_current_fingerprint, base)
        with service.store.mutation_lock():
            return _recover(service, recovery_path, expected_preimage_sha256, expected_current_fingerprint, base)
    except _RecoveryRefusal as exc:
        return TopicRecoveryResult(**{**base, "reason": str(exc)}, status=exc.status)
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        return TopicRecoveryResult(**{**base, "reason": f"{type(exc).__name__}: {exc}"}, status="RECOVERY_REQUIRED")


def _recover(service, recovery_path, preimage_hash, expected_fingerprint, base):
    plan = _plan(service, recovery_path, preimage_hash)
    base.update(current_fingerprint=plan["fingerprint"], paper_id=plan["paper_id"],
                old_topics=plan["old_topics"], canonical_sha256=plan["canonical_hash"], metadata_sha256=plan["metadata"],
                remove_aliases=tuple(sorted(map(str, plan["current_links"] - plan["old_links"]))),
                restore_aliases=tuple(sorted(map(str, plan["old_links"] - plan["current_links"]))))
    if expected_fingerprint is not None and expected_fingerprint != plan["fingerprint"]:
        raise _RecoveryRefusal("PRECONDITION_CONFLICT", "The current recovery state differs from the reviewed preview")
    if base["dry_run"]:
        return TopicRecoveryResult(**base, status="DRY_RUN")
    if not base["remove_aliases"] and not base["restore_aliases"]:
        return TopicRecoveryResult(**base, status="ALREADY_RECOVERED", recovered=True)
    from ..windows_hardlinks import remove_readonly_hardlink_alias

    # Restore required original aliases before removing redundant ones. A
    # failure leaves at least the canonical original, never a deleted PDF.
    for raw in base["restore_aliases"]:
        alias = _checked(Path(raw))
        alias.parent.mkdir(parents=True, exist_ok=True)
        os.link(_windows_io_path(plan["canonical"]), _windows_io_path(alias))
    for raw in base["remove_aliases"]:
        remove_readonly_hardlink_alias(Path(raw), plan["canonical"])
    checked = _plan(service, recovery_path, preimage_hash)
    if checked["current_links"] != checked["old_links"] or checked["canonical_hash"] != plan["canonical_hash"] or checked["canonical_stat"] != plan["canonical_stat"]:
        raise _RecoveryRefusal("RECOVERY_REQUIRED", "The view or canonical identity did not finish consistently")
    result = TopicRecoveryResult(**base, status="RECOVERED", recovered=True)
    receipt = plan["recovery"] / f"RECOVERY_RESULT_{_transaction_token()}.json"
    try:
        with receipt.open("xb") as handle:
            handle.write((json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        return TopicRecoveryResult(**base, status="RECOVERED", recovered=True, audit_error=str(exc))
    return TopicRecoveryResult(**base, status="RECOVERED", recovered=True, audit_path=str(receipt))


__all__ = ["TopicRecoveryResult", "recover_topic_view"]
