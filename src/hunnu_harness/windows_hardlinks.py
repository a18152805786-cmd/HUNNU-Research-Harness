"""Remove a Windows topic-view hardlink without changing its read-only inode.

This is not a general delete primitive. It accepts an explicit topic alias and
an explicit managed original, proves their shared file identity, and deletes
only the alias handle's name. Unsupported systems fail closed: no chmod,
attribute reset, ordinary unlink fallback, or original-file deletion exists.

Microsoft's FileDispositionInfoEx / FILE_DISPOSITION_INFORMATION_EX contract:
https://learn.microsoft.com/windows/win32/api/minwinbase/ne-minwinbase-file_info_by_handle_class
https://learn.microsoft.com/windows-hardware/drivers/ddi/ntddk/ns-ntddk-_file_disposition_information_ex
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
from typing import Any

from .paths import (
    LIBRARY_PAPERS_DIR,
    PAPERS_BY_TOPIC_DIR,
    TEMP_DIR,
    _logical_path,
    _windows_io_path,
    require_output_path,
)


class HardlinkAliasRemovalError(OSError):
    """No safe alias-only deletion could be proved or completed."""


_DIRECTORY = 0x00000010
_REPARSE_POINT = 0x00000400
_DELETE = 0x00010000
_READ_ATTRIBUTES = 0x00000080
_GENERIC_READ = 0x80000000
_SHARE_READ = 0x00000001
_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_OPEN_REPARSE_POINT = 0x00200000
_FILE_DISPOSITION_INFO_EX = 21
_DISPOSITION_DELETE_POSIX_IGNORE_READONLY = 0x00000001 | 0x00000002 | 0x00000010


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32), ("creation", _FileTime),
        ("access", _FileTime), ("write", _FileTime),
        ("volume", ctypes.c_uint32), ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32), ("links", ctypes.c_uint32),
        ("index_high", ctypes.c_uint32), ("index_low", ctypes.c_uint32),
    ]

    @property
    def identity(self) -> tuple[int, int, int]:
        return self.volume, self.index_high, self.index_low


class _Disposition(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint32)]


def _checked_path(value: Path, *, alias: bool) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise HardlinkAliasRemovalError("Hardlink alias removal requires explicit absolute paths")
    for ancestor in (candidate, *candidate.parents):
        io = _windows_io_path(ancestor)
        try:
            attributes = getattr(io.lstat(), "st_file_attributes", 0)
        except OSError as exc:
            raise HardlinkAliasRemovalError("Cannot verify a hardlink removal path") from exc
        if attributes & _REPARSE_POINT or io.is_symlink() or getattr(io, "is_junction", lambda: False)():
            raise HardlinkAliasRemovalError("Hardlink removal paths cannot contain reparse points or links")
    try:
        path = require_output_path(_logical_path(candidate), label="Read-only hardlink alias removal")
    except (OSError, ValueError) as exc:
        raise HardlinkAliasRemovalError("Hardlink removal paths must remain inside Harness Output Root") from exc
    role_root = PAPERS_BY_TOPIC_DIR if alias else LIBRARY_PAPERS_DIR
    # Isolated tests use only Output/temp; production paths remain restricted
    # to the canonical view and managed-paper roles.
    if not path.is_relative_to(role_root) and not path.is_relative_to(TEMP_DIR):
        raise HardlinkAliasRemovalError("Path is outside the permitted topic-alias or managed-paper role")
    if alias and path.is_relative_to(LIBRARY_PAPERS_DIR):
        raise HardlinkAliasRemovalError("A managed paper cannot be selected as the removable alias")
    if any(":" in part for part in path.parts[1:]):
        raise HardlinkAliasRemovalError("Alternate data streams are not hardlink aliases")
    return path


def _api() -> Any:
    if os.name != "nt":
        raise HardlinkAliasRemovalError("Read-only alias deletion requires supported Windows FileDispositionInfoEx")
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.POINTER(_FileInformation)]
        kernel.GetFileInformationByHandle.restype = ctypes.c_int
        kernel.SetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        kernel.SetFileInformationByHandle.restype = ctypes.c_int
        kernel.SetFilePointerEx.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_uint32]
        kernel.SetFilePointerEx.restype = ctypes.c_int
        kernel.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        kernel.ReadFile.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        return kernel
    except (AttributeError, OSError) as exc:
        raise HardlinkAliasRemovalError("Windows FileDispositionInfoEx API is unavailable; no attribute-changing fallback is allowed") from exc


def _raise_api_error(operation: str) -> None:
    error = ctypes.get_last_error()
    failure = HardlinkAliasRemovalError(
        f"{operation} failed (WinError={error}); no chmod or ordinary-delete fallback is allowed"
    )
    failure.winerror = error
    raise failure


def _open(kernel: Any, path: Path, access: int, share: int) -> int:
    handle = kernel.CreateFileW(str(_windows_io_path(path, force=True)), access, share, None, _OPEN_EXISTING, _OPEN_REPARSE_POINT, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        _raise_api_error("Opening the explicitly selected hardlink")
    return handle


def _information(kernel: Any, handle: int) -> _FileInformation:
    info = _FileInformation()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        _raise_api_error("Reading hardlink file identity")
    if info.attributes & (_DIRECTORY | _REPARSE_POINT):
        raise HardlinkAliasRemovalError("Only regular non-reparse hardlink files are supported")
    return info


def _digest(kernel: Any, handle: int) -> str:
    if not kernel.SetFilePointerEx(handle, 0, None, 0):
        _raise_api_error("Seeking the protected original")
    digest = hashlib.sha256()
    buffer = ctypes.create_string_buffer(65536)
    length = ctypes.c_uint32()
    while True:
        if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(length), None):
            _raise_api_error("Reading the protected original")
        if not length.value:
            break
        digest.update(buffer.raw[:length.value])
    return digest.hexdigest()


def remove_readonly_hardlink_alias(alias_path: Path, managed_path: Path) -> None:
    """Delete only an explicit topic alias while preserving its managed inode.

    Requires Windows/filesystem support for FileDispositionInfoEx's
    IGNORE_READONLY_ATTRIBUTE. Access/API/identity failures propagate without
    changing file attributes or attempting any weaker deletion strategy.
    """

    alias = _checked_path(alias_path, alias=True)
    managed = _checked_path(managed_path, alias=False)
    if alias == managed:
        raise HardlinkAliasRemovalError("The managed original cannot be deleted as its own alias")
    kernel = _api()
    original_handle = _open(kernel, managed, _GENERIC_READ | _READ_ATTRIBUTES, _SHARE_READ | _SHARE_DELETE)
    alias_handle = None
    try:
        original = _information(kernel, original_handle)
        before_digest = _digest(kernel, original_handle)
        # This handle grants no write/delete sharing while it is held. The
        # alias name selected for disposition cannot be replaced underneath it.
        alias_handle = _open(kernel, alias, _DELETE | _READ_ATTRIBUTES, _SHARE_READ)
        linked = _information(kernel, alias_handle)
        if original.identity != linked.identity or original.links < 2 or linked.links < 2:
            raise HardlinkAliasRemovalError("The selected alias is not a distinct hardlink to the managed original")
        flags = _Disposition(_DISPOSITION_DELETE_POSIX_IGNORE_READONLY)
        if not kernel.SetFileInformationByHandle(alias_handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags), ctypes.sizeof(flags)):
            _raise_api_error("Alias-only FileDispositionInfoEx")
        if not kernel.CloseHandle(alias_handle):
            _raise_api_error("Closing the deleted alias handle")
        alias_handle = None
        after = _information(kernel, original_handle)
        if (after.identity != original.identity or after.attributes != original.attributes
            or after.size_high != original.size_high or after.size_low != original.size_low
            or _digest(kernel, original_handle) != before_digest):
            raise HardlinkAliasRemovalError("Managed original verification changed after alias removal; recovery review required")
        if _windows_io_path(alias).exists() or not _windows_io_path(managed).is_file():
            raise HardlinkAliasRemovalError("Alias removal did not preserve the expected path roles")
        verify = _open(kernel, managed, _READ_ATTRIBUTES, _SHARE_READ | _SHARE_DELETE)
        try:
            if _information(kernel, verify).identity != original.identity:
                raise HardlinkAliasRemovalError("The managed path no longer names the protected original")
        finally:
            kernel.CloseHandle(verify)
    finally:
        if alias_handle is not None:
            kernel.CloseHandle(alias_handle)
        kernel.CloseHandle(original_handle)


__all__ = ["HardlinkAliasRemovalError", "remove_readonly_hardlink_alias"]
