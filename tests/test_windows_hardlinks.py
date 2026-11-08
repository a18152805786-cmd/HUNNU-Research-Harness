"""Windows alias disposal uses only synthetic files under Output/temp."""

from __future__ import annotations

import ctypes
import hashlib
import os
import tempfile
from pathlib import Path

import pytest

from hunnu_harness import windows_hardlinks as hardlinks
from hunnu_harness.paths import TEMP_DIR, _windows_io_path


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows FileDispositionInfoEx is required")


@pytest.fixture
def tmp_path() -> Path:
    # This module must remain isolated even when invoked with bare pytest.
    # Retain these tiny readonly fixtures as evidence; cleanup must not need
    # to clear an inode's readonly bit merely to make a test finish.
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="readonly-hardlink-test-", dir=TEMP_DIR))


def _fixture(tmp_path: Path) -> tuple[Path, Path, bytes, int]:
    assert tmp_path.resolve().is_relative_to(TEMP_DIR.resolve())
    managed = tmp_path / "managed.pdf"
    alias = tmp_path / "topic-alias.pdf"
    payload = b"%PDF-1.4\nSynthetic readonly hardlink fixture, never a library paper.\n"
    managed.write_bytes(payload)
    os.link(_windows_io_path(managed), _windows_io_path(alias))
    attributes = managed.stat().st_file_attributes | 0x1
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.SetFileAttributesW.restype = ctypes.c_int
    # This sets readonly only on a newly-created synthetic test inode. No
    # production helper clears or changes file attributes, even on failure.
    assert kernel.SetFileAttributesW(str(_windows_io_path(managed, force=True)), attributes)
    assert managed.stat().st_nlink == alias.stat().st_nlink == 2
    return managed, alias, payload, attributes


def _unchanged(managed: Path, alias: Path, payload: bytes, attributes: int) -> None:
    assert managed.read_bytes() == alias.read_bytes() == payload
    assert managed.stat().st_file_attributes == alias.stat().st_file_attributes == attributes
    assert managed.stat().st_nlink == alias.stat().st_nlink == 2


def test_native_disposition_removes_only_alias_preserving_bytes_readonly_and_file_id(tmp_path):
    managed, alias, payload, attributes = _fixture(tmp_path)
    before = managed.stat()
    before_md5 = hashlib.md5(payload).hexdigest()
    before_sha256 = hashlib.sha256(payload).hexdigest()
    # Reproduce the precise original blocker before exercising the narrow API.
    with pytest.raises(PermissionError):
        alias.unlink()
    _unchanged(managed, alias, payload, attributes)

    hardlinks.remove_readonly_hardlink_alias(alias, managed)

    after = managed.stat()
    assert not alias.exists()
    assert after.st_nlink == 1
    assert after.st_ino == before.st_ino
    assert after.st_file_attributes == attributes
    assert hashlib.md5(managed.read_bytes()).hexdigest() == before_md5
    assert hashlib.sha256(managed.read_bytes()).hexdigest() == before_sha256


def test_same_path_cannot_select_managed_original_for_removal(tmp_path):
    managed, alias, payload, attributes = _fixture(tmp_path)
    with pytest.raises(hardlinks.HardlinkAliasRemovalError, match="own alias"):
        hardlinks.remove_readonly_hardlink_alias(managed, managed)
    _unchanged(managed, alias, payload, attributes)


def test_other_topic_alias_remains_with_original_readonly_inode(tmp_path):
    managed, alias, payload, attributes = _fixture(tmp_path)
    other_alias = tmp_path / "other-topic-alias.pdf"
    os.link(_windows_io_path(managed), _windows_io_path(other_alias))
    assert managed.stat().st_nlink == 3
    hardlinks.remove_readonly_hardlink_alias(alias, managed)
    assert not alias.exists()
    assert managed.read_bytes() == other_alias.read_bytes() == payload
    assert managed.stat().st_file_attributes == other_alias.stat().st_file_attributes == attributes
    assert managed.stat().st_ino == other_alias.stat().st_ino
    assert managed.stat().st_nlink == other_alias.stat().st_nlink == 2


def test_same_bytes_in_distinct_file_are_not_a_hardlink_identity(tmp_path):
    managed, alias, payload, attributes = _fixture(tmp_path)
    unrelated = tmp_path / "unrelated.pdf"
    unrelated.write_bytes(payload)
    with pytest.raises(hardlinks.HardlinkAliasRemovalError, match="distinct hardlink"):
        hardlinks.remove_readonly_hardlink_alias(unrelated, managed)
    assert unrelated.read_bytes() == payload
    _unchanged(managed, alias, payload, attributes)


def test_missing_api_fails_closed_without_attribute_or_delete_fallback(tmp_path, monkeypatch):
    managed, alias, payload, attributes = _fixture(tmp_path)

    def unavailable():
        raise hardlinks.HardlinkAliasRemovalError("FileDispositionInfoEx unavailable")

    monkeypatch.setattr(hardlinks, "_api", unavailable)
    with pytest.raises(hardlinks.HardlinkAliasRemovalError, match="unavailable"):
        hardlinks.remove_readonly_hardlink_alias(alias, managed)
    _unchanged(managed, alias, payload, attributes)


def test_unsupported_disposition_fails_closed_and_closes_handles(tmp_path, monkeypatch):
    managed, alias, payload, attributes = _fixture(tmp_path)
    real = hardlinks._api()
    calls = []

    class UnsupportedDisposition:
        def __getattr__(self, name):
            return getattr(real, name)

        def SetFileInformationByHandle(self, handle, information_class, information, size):
            calls.append((information_class, size))
            assert ctypes.cast(information, ctypes.POINTER(hardlinks._Disposition)).contents.flags == 0x13
            ctypes.set_last_error(87)  # ERROR_INVALID_PARAMETER on unsupported Windows/filesystems.
            return 0

    monkeypatch.setattr(hardlinks, "_api", lambda: UnsupportedDisposition())
    with pytest.raises(hardlinks.HardlinkAliasRemovalError, match="WinError=87"):
        hardlinks.remove_readonly_hardlink_alias(alias, managed)
    assert calls == [(21, 4)]
    _unchanged(managed, alias, payload, attributes)
    # Both handles were closed after failure: a subsequent supported call can
    # acquire the deliberately restrictive alias handle and finish normally.
    monkeypatch.setattr(hardlinks, "_api", lambda: real)
    hardlinks.remove_readonly_hardlink_alias(alias, managed)
    assert not alias.exists()
    assert managed.read_bytes() == payload
    assert managed.stat().st_file_attributes == attributes


def test_declared_path_roles_cannot_delete_a_managed_paper(tmp_path, monkeypatch):
    managed, alias, payload, attributes = _fixture(tmp_path)
    # Narrow canonical roles to an isolated subtree so this negative fixture
    # never creates, inspects, or mutates anything in the real managed library.
    monkeypatch.setattr(hardlinks, "LIBRARY_PAPERS_DIR", tmp_path)
    with pytest.raises(hardlinks.HardlinkAliasRemovalError, match="managed paper"):
        hardlinks.remove_readonly_hardlink_alias(alias, managed)
    _unchanged(managed, alias, payload, attributes)


def test_relative_or_outside_output_paths_fail_before_opening_handles(monkeypatch):
    def forbidden_api():
        raise AssertionError("Invalid path must never reach a delete-capable handle")

    monkeypatch.setattr(hardlinks, "_api", forbidden_api)
    with pytest.raises(hardlinks.HardlinkAliasRemovalError, match="absolute"):
        hardlinks.remove_readonly_hardlink_alias(Path("relative.pdf"), Path("original.pdf"))
    with pytest.raises(hardlinks.HardlinkAliasRemovalError, match="Output Root"):
        hardlinks.remove_readonly_hardlink_alias(Path(__file__).resolve(), Path(__file__).resolve())


def test_symlink_alias_rejected_without_touching_target(tmp_path):
    managed, alias, payload, attributes = _fixture(tmp_path)
    symbolic = tmp_path / "symbolic.pdf"
    try:
        symbolic.symlink_to(managed)
    except OSError as exc:
        pytest.skip(f"Windows symlink fixture unavailable: {exc.winerror}")
    with pytest.raises(hardlinks.HardlinkAliasRemovalError, match="reparse"):
        hardlinks.remove_readonly_hardlink_alias(symbolic, managed)
    assert symbolic.is_symlink()
    _unchanged(managed, alias, payload, attributes)
