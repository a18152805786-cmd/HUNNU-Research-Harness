from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..audit.logger import AuditLogger
from ..models import DownloadRecord, DownloadRequest
from ..paths import MANIFESTS_DIR, RAW_DIR, STAGING_DIR, _logical_path, _windows_io_path


PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp", ".download")


class DownloadTimeout(TimeoutError):
    pass


class DownloadManager:
    def __init__(
        self,
        watch_dirs: Iterable[Path] | None = None,
        archive_root: Path | None = None,
        manifest_root: Path | None = None,
        logger: AuditLogger | None = None,
    ):
        resolved_watch_dirs = watch_dirs if watch_dirs is not None else (STAGING_DIR,)
        self.watch_dirs = tuple(_logical_path(d) for d in resolved_watch_dirs)
        self.archive_root = _logical_path(archive_root) if archive_root is not None else _logical_path(RAW_DIR)
        self.manifest_root = _logical_path(manifest_root) if manifest_root is not None else _logical_path(MANIFESTS_DIR)
        self.logger = logger
        _windows_io_path(self.archive_root).mkdir(parents=True, exist_ok=True)
        _windows_io_path(self.manifest_root).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def is_partial(path: Path) -> bool:
        return path.name.lower().endswith(PARTIAL_SUFFIXES)

    def _files(self) -> dict[Path, tuple[int, int]]:
        result: dict[Path, tuple[int, int]] = {}
        for directory in self.watch_dirs:
            directory_io = _windows_io_path(directory)
            if not directory_io.exists():
                continue
            for raw_path in directory_io.iterdir():
                path = _logical_path(raw_path)
                path_io = _windows_io_path(path)
                if path_io.is_file() and not self.is_partial(path):
                    try:
                        stat_result = path_io.stat()
                    except OSError:
                        continue
                    result[path] = (stat_result.st_size, stat_result.st_mtime_ns)
        return result

    def snapshot(self) -> set[Path]:
        """Return the current complete-file baseline for a later download wait."""
        return set(self._files())

    @staticmethod
    def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
        digest = hashlib.sha256()
        with _windows_io_path(path).open("rb") as handle:
            while chunk := handle.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()

    def verify_complete(self, path: Path, *, stable_seconds: float = 2, poll_seconds: float = 0.5) -> bool:
        path_io = _windows_io_path(path)
        if not path_io.exists() or not path_io.is_file() or self.is_partial(path):
            return False
        try:
            first_size = path_io.stat().st_size
        except OSError:
            return False
        if first_size <= 0:
            return False
        time.sleep(stable_seconds)
        try:
            return path_io.exists() and path_io.stat().st_size == first_size and os.access(path_io, os.R_OK)
        except OSError:
            return False

    def wait_for_new_download(self, before: set[Path] | None = None, *, timeout_seconds: float = 180, stable_seconds: float = 2) -> Path:
        baseline = before if before is not None else set(self._files())
        deadline = time.monotonic() + timeout_seconds
        while True:
            current = self._files()
            candidates = [p for p in current if p not in baseline]
            candidates.sort(key=lambda p: current[p][1], reverse=True)
            for candidate in candidates:
                if self.verify_complete(candidate, stable_seconds=stable_seconds):
                    if self.logger:
                        try:
                            size = _windows_io_path(candidate).stat().st_size
                        except OSError:
                            size = current[candidate][0]
                        self.logger.log(
                            "wait_for_new_download",
                            status="success",
                            path=str(candidate),
                            size=size,
                        )
                    return candidate
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if self.logger:
            self.logger.log("wait_for_new_download", status="timeout", watched_dirs=[str(p) for p in self.watch_dirs])
        raise DownloadTimeout("No new complete download was detected before timeout.")

    def archive_file(self, source: Path, request: DownloadRequest, *, source_url: str | None = None, read_only: bool = True) -> DownloadRecord:
        source = _logical_path(source)
        if not self.verify_complete(source, stable_seconds=0):
            raise ValueError(f"Source is not a readable complete file: {source}")
        day = datetime.now().strftime("%Y-%m-%d")
        raw_dir = self.archive_root / request.database / day / "raw"
        metadata_dir = self.manifest_root / request.database / day / "metadata"
        _windows_io_path(raw_dir).mkdir(parents=True, exist_ok=True)
        _windows_io_path(metadata_dir).mkdir(parents=True, exist_ok=True)
        destination = raw_dir / source.name
        if _windows_io_path(destination).exists():
            if self.sha256(destination) == self.sha256(source):
                pass
            else:
                destination = raw_dir / f"{source.stem}_{datetime.now().strftime('%H%M%S')}{source.suffix}"
                shutil.copy2(_windows_io_path(source), _windows_io_path(destination))
        else:
            shutil.copy2(_windows_io_path(source), _windows_io_path(destination))
        digest = self.sha256(destination)
        if read_only:
            destination_io = _windows_io_path(destination)
            destination_io.chmod(destination_io.stat().st_mode & ~stat.S_IWRITE)
        record = DownloadRecord(
            database=request.database,
            institution="湖南师范大学",
            access_type="school_account",
            download_time=datetime.now().astimezone().isoformat(),
            module=request.module,
            table=request.table,
            query={
                "stocks": list(request.stocks),
                "date_start": request.date_start,
                "date_end": request.date_end,
                "fields": list(request.fields),
                "format": request.output_format,
            },
            original_filename=source.name,
            original_path=str(source),
            archived_path=str(destination),
            sha256=digest,
            source_url=source_url or request.source_url,
        )
        manifest_name = f"{destination.stem}_{digest[:12]}.json"
        manifest_path = self.manifest_root / request.database / day / manifest_name
        metadata_manifest_path = metadata_dir / manifest_name
        import json

        manifest_payload = json.dumps(record.as_dict(), ensure_ascii=False, indent=2) + "\n"
        _windows_io_path(manifest_path.parent).mkdir(parents=True, exist_ok=True)
        _windows_io_path(manifest_path).write_text(manifest_payload, encoding="utf-8")
        _windows_io_path(metadata_manifest_path).write_text(manifest_payload, encoding="utf-8")
        if self.logger:
            self.logger.log("archive_download", status="success", original_path=str(source), archived_path=str(destination), sha256=digest, manifest_path=str(manifest_path), metadata_manifest_path=str(metadata_manifest_path), database=request.database, table=request.table)
        return record
