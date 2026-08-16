from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..paths import require_output_path


_TEMPORARY_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp")
_SAFE_FILENAME = re.compile(r"[^0-9A-Za-z._-]+")


class ManualDownloadHandoffError(RuntimeError):
    pass


class ManualDownloadHandoffTimeout(ManualDownloadHandoffError):
    pass


class ManualDownloadCandidateAmbiguous(ManualDownloadHandoffError):
    pass


class ManualDownloadCandidateRejected(ManualDownloadHandoffError):
    pass


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class DownloadFileSnapshot:
    filename: str
    size: int
    mtime_ns: int
    sha256: str = "not_recorded"

    def as_dict(self) -> dict[str, Any]:
        return {
            "Filename": self.filename,
            "Size": self.size,
            "MTimeNS": self.mtime_ns,
            "SHA256": self.sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DownloadFileSnapshot":
        return cls(
            filename=str(value["Filename"]),
            size=int(value["Size"]),
            mtime_ns=int(value["MTimeNS"]),
            sha256=str(value.get("SHA256", "not_recorded")),
        )


@dataclass(frozen=True)
class DownloadDirectorySnapshot:
    directory: Path
    captured_at_ns: int
    files: tuple[DownloadFileSnapshot, ...]

    @property
    def by_name(self) -> dict[str, DownloadFileSnapshot]:
        return {item.filename.casefold(): item for item in self.files}

    def as_dict(self) -> dict[str, Any]:
        return {
            "Directory": str(self.directory),
            "CapturedAtNS": self.captured_at_ns,
            "Files": [item.as_dict() for item in self.files],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DownloadDirectorySnapshot":
        return cls(
            directory=Path(str(value["Directory"])),
            captured_at_ns=int(value["CapturedAtNS"]),
            files=tuple(
                DownloadFileSnapshot.from_mapping(item)
                for item in value.get("Files", ())
            ),
        )


@dataclass(frozen=True)
class ManualDownloadHandoffState:
    watch_directory: Path
    staging_directory: Path
    armed_at_ns: int
    before: DownloadDirectorySnapshot

    def as_dict(self) -> dict[str, Any]:
        return {
            "SchemaVersion": "0.2.6",
            "ManualDownloadHandoffArmed": True,
            "ACTION_REQUIRED_USER_LOGIN": False,
            "ACTION_REQUIRED_USER_DOWNLOAD": True,
            "BrowserReadyForManualDownload": True,
            "ManualDownloadWatchDirectory": str(self.watch_directory),
            "ControlledStagingDirectory": str(self.staging_directory),
            "ArmedAtNS": self.armed_at_ns,
            "DownloadDirectorySnapshotBefore": self.before.as_dict(),
            "WindowsGUIFallback": False,
            "NativePDFViewerAutomation": False,
            "SignedURLReplay": False,
            "AuthenticatedRequestReplay": False,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ManualDownloadHandoffState":
        return cls(
            watch_directory=Path(str(value["ManualDownloadWatchDirectory"])),
            staging_directory=Path(str(value["ControlledStagingDirectory"])),
            armed_at_ns=int(value["ArmedAtNS"]),
            before=DownloadDirectorySnapshot.from_mapping(
                value["DownloadDirectorySnapshotBefore"]
            ),
        )


@dataclass(frozen=True)
class ManualDownloadScan:
    after: DownloadDirectorySnapshot
    completed_candidates: tuple[Path, ...]
    temporary_candidates: tuple[Path, ...]

    @property
    def new_download_candidates(self) -> int:
        return len(self.completed_candidates)


@dataclass(frozen=True)
class ManualDownloadDetection:
    source_path: Path
    after: DownloadDirectorySnapshot
    new_download_candidates: int
    created_or_changed_after_handoff: bool = True
    temporary_download_file: bool = False
    file_size_stable: bool = True
    non_zero_size: bool = True
    pdf_header_valid: bool = True


@dataclass(frozen=True)
class ManualDownloadHandoffResult:
    source_path: Path
    staged_path: Path
    after: DownloadDirectorySnapshot
    new_download_candidates: int
    original_manual_download_preserved: bool
    acquisition_method: str = "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF"
    download_initiation_mode: str = "USER_MANUAL_NATIVE_VIEWER_CLICK"
    file_finalization_mode: str = "HARNESS_AUTOMATIC"
    manual_download_detected: bool = True
    download_completed: bool = True
    file_size_stable: bool = True
    temporary_download_file: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "AcquisitionMethod": self.acquisition_method,
            "DownloadInitiationMode": self.download_initiation_mode,
            "FileFinalizationMode": self.file_finalization_mode,
            "ManualDownloadDetected": self.manual_download_detected,
            "NewDownloadCandidates": self.new_download_candidates,
            "DownloadCompleted": self.download_completed,
            "FileSizeStable": self.file_size_stable,
            "TemporaryDownloadFile": self.temporary_download_file,
            "OriginalManualDownloadPreserved": self.original_manual_download_preserved,
            "DetectedFilename": self.source_path.name,
            "StagedFilename": self.staged_path.name,
            "DownloadDirectorySnapshotAfter": self.after.as_dict(),
            "SignedURLPersisted": False,
            "QueryStringPersisted": False,
            "AuthorizationHeaderPersisted": False,
            "CookiePersisted": False,
        }


class ManualDownloadHandoff:
    """Observe one user-initiated browser download and copy it into staging.

    This primitive never interacts with browser UI or network requests. It only
    snapshots a configured download directory, accepts a completed PDF created
    or changed after arming, and preserves the user's original file while
    copying it into Harness-controlled staging.
    """

    def __init__(
        self,
        watch_directory: Path,
        staging_directory: Path,
        *,
        allow_outside_output_for_tests: bool = False,
    ) -> None:
        self.watch_directory = Path(watch_directory).resolve()
        self.staging_directory = Path(staging_directory).resolve()
        if not self.watch_directory.is_dir():
            raise FileNotFoundError(f"Manual download watch directory does not exist: {self.watch_directory}")
        if not allow_outside_output_for_tests:
            self.staging_directory = require_output_path(
                self.staging_directory,
                label="Manual download controlled staging",
            )
        self.staging_directory.mkdir(parents=True, exist_ok=True)
        self.allow_outside_output_for_tests = allow_outside_output_for_tests

    @staticmethod
    def _is_temporary(path: Path) -> bool:
        return path.name.casefold().endswith(_TEMPORARY_SUFFIXES)

    @staticmethod
    def _is_pdf(path: Path) -> bool:
        return path.suffix.casefold() == ".pdf"

    @staticmethod
    def _has_pdf_header(path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return handle.read(5) == b"%PDF-"
        except OSError:
            return False

    def snapshot(self, *, hash_existing_pdfs: bool = False) -> DownloadDirectorySnapshot:
        files: list[DownloadFileSnapshot] = []
        for path in sorted(self.watch_directory.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file():
                continue
            stat = path.stat()
            digest = _sha256(path) if hash_existing_pdfs and self._is_pdf(path) else "not_recorded"
            files.append(
                DownloadFileSnapshot(
                    filename=path.name,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    sha256=digest,
                )
            )
        return DownloadDirectorySnapshot(
            directory=self.watch_directory,
            captured_at_ns=time.time_ns(),
            files=tuple(files),
        )

    def arm(self, *, hash_existing_pdfs: bool = False) -> ManualDownloadHandoffState:
        before = self.snapshot(hash_existing_pdfs=hash_existing_pdfs)
        return ManualDownloadHandoffState(
            watch_directory=self.watch_directory,
            staging_directory=self.staging_directory,
            armed_at_ns=time.time_ns(),
            before=before,
        )

    def scan(self, state: ManualDownloadHandoffState) -> ManualDownloadScan:
        self._validate_state(state)
        after = self.snapshot(hash_existing_pdfs=False)
        before = state.before.by_name
        complete: list[Path] = []
        temporary: list[Path] = []
        for item in after.files:
            previous = before.get(item.filename.casefold())
            changed = previous is None or (
                previous.size != item.size or previous.mtime_ns != item.mtime_ns
            )
            if not changed:
                continue
            path = self.watch_directory / item.filename
            if self._is_temporary(path):
                temporary.append(path)
            elif self._is_pdf(path):
                complete.append(path)
        return ManualDownloadScan(
            after=after,
            completed_candidates=tuple(complete),
            temporary_candidates=tuple(temporary),
        )

    async def wait_for_completed_download(
        self,
        state: ManualDownloadHandoffState,
        *,
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 0.5,
        stable_observations: int = 2,
    ) -> ManualDownloadDetection:
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("Manual download wait values must be positive")
        if stable_observations < 2:
            raise ValueError("At least two stable file-size observations are required")
        self._validate_state(state)
        deadline = time.monotonic() + timeout_seconds
        signatures: dict[Path, tuple[int, int]] = {}
        counts: dict[Path, int] = {}
        last_scan = self.scan(state)
        while time.monotonic() < deadline:
            scan = self.scan(state)
            last_scan = scan
            current_paths = set(scan.completed_candidates)
            for path in tuple(signatures):
                if path not in current_paths:
                    signatures.pop(path, None)
                    counts.pop(path, None)
            for path in scan.completed_candidates:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                signature = (stat.st_size, stat.st_mtime_ns)
                counts[path] = counts.get(path, 0) + 1 if signatures.get(path) == signature else 1
                signatures[path] = signature

            all_stable = bool(scan.completed_candidates) and all(
                counts.get(path, 0) >= stable_observations
                for path in scan.completed_candidates
            )
            if all_stable and not scan.temporary_candidates:
                valid = tuple(
                    path
                    for path in scan.completed_candidates
                    if path.stat().st_size > 0 and self._has_pdf_header(path)
                )
                if len(valid) > 1:
                    raise ManualDownloadCandidateAmbiguous(
                        f"ManualDownloadCandidateAmbiguous=true; NewDownloadCandidates={len(valid)}"
                    )
                if len(valid) == 1:
                    return ManualDownloadDetection(
                        source_path=valid[0],
                        after=scan.after,
                        new_download_candidates=len(scan.completed_candidates),
                    )
                raise ManualDownloadCandidateRejected(
                    "Manual download candidate failed non-zero PDF header validation"
                )
            await asyncio.sleep(poll_interval_seconds)

        detail = (
            "temporary download file still present"
            if last_scan.temporary_candidates
            else "no stable new PDF appeared"
        )
        raise ManualDownloadHandoffTimeout(f"Manual download handoff timed out: {detail}")

    def stage(
        self,
        detection: ManualDownloadDetection,
        *,
        controlled_filename: str,
    ) -> ManualDownloadHandoffResult:
        source = detection.source_path.resolve()
        if source.parent != self.watch_directory:
            raise ManualDownloadCandidateRejected("Detected candidate is outside the armed watch directory")
        if not source.is_file() or source.stat().st_size <= 0 or not self._has_pdf_header(source):
            raise ManualDownloadCandidateRejected("Detected candidate is not a completed PDF")
        safe_name = _SAFE_FILENAME.sub("_", Path(controlled_filename).name).strip("._")
        if not safe_name.casefold().endswith(".pdf"):
            safe_name = f"{safe_name or 'manual-download'}.pdf"
        destination = self.staging_directory / safe_name
        digest = _sha256(source)
        if destination.exists() and _sha256(destination) != digest:
            destination = destination.with_name(f"{destination.stem}_{digest[:12]}.pdf")
        if source != destination and not destination.exists():
            shutil.copy2(source, destination)
        if not destination.is_file() or _sha256(destination) != digest:
            raise ManualDownloadHandoffError("Controlled staging copy failed SHA256 verification")
        return ManualDownloadHandoffResult(
            source_path=source,
            staged_path=destination,
            after=detection.after,
            new_download_candidates=detection.new_download_candidates,
            original_manual_download_preserved=source.is_file(),
        )

    def save_state(self, state: ManualDownloadHandoffState, path: Path) -> Path:
        destination = Path(path).resolve()
        if not self.allow_outside_output_for_tests:
            destination = require_output_path(destination, label="Manual download handoff state")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(state.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(destination)
        return destination

    def save_result(self, result: ManualDownloadHandoffResult, path: Path) -> Path:
        destination = Path(path).resolve()
        if not self.allow_outside_output_for_tests:
            destination = require_output_path(destination, label="Manual download handoff result")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(destination)
        return destination

    @staticmethod
    def load_state(path: Path) -> ManualDownloadHandoffState:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return ManualDownloadHandoffState.from_mapping(payload)

    def _validate_state(self, state: ManualDownloadHandoffState) -> None:
        if state.watch_directory.resolve() != self.watch_directory:
            raise ManualDownloadHandoffError("Handoff state watch directory does not match")
        if state.staging_directory.resolve() != self.staging_directory:
            raise ManualDownloadHandoffError("Handoff state staging directory does not match")


__all__ = [
    "DownloadDirectorySnapshot",
    "DownloadFileSnapshot",
    "ManualDownloadCandidateAmbiguous",
    "ManualDownloadCandidateRejected",
    "ManualDownloadDetection",
    "ManualDownloadHandoff",
    "ManualDownloadHandoffError",
    "ManualDownloadHandoffResult",
    "ManualDownloadHandoffState",
    "ManualDownloadHandoffTimeout",
    "ManualDownloadScan",
]
