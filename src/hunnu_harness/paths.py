"""Canonical Core Root and Output Root separation for Harness paths.

``PROJECT_ROOT`` remains a compatibility alias for the source repository.  No
runtime writer should derive a destination from it.  Runtime paths are derived
only from ``OUTPUT_ROOT``.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path


_WINDOWS_EXTENDED_PATH_PREFIX = "\\\\?\\"
_WINDOWS_DEVICE_PATH_PREFIX = "\\\\.\\"
_WINDOWS_EXTENDED_PATH_LIMIT = 32767
_WINDOWS_MAX_COMPONENT_LENGTH = 255
_TRANSACTION_TOKEN_LENGTH = 8


def _windows_io_path(path: Path | str, *, force: bool = False) -> Path:
    """Return a filesystem-I/O path that supports long Windows paths.

    The extended-length prefix belongs only at the Windows I/O boundary.  It
    must not be used for logical paths, persisted metadata, or caller-visible
    results, because those values are part of the Harness path contract.
    """

    candidate = Path(path)
    if os.name != "nt":
        return candidate

    raw = os.fspath(candidate)
    if raw.startswith((_WINDOWS_EXTENDED_PATH_PREFIX, _WINDOWS_DEVICE_PATH_PREFIX)):
        return candidate
    if not os.path.isabs(raw):
        raw = os.path.abspath(raw)
    if raw.startswith((_WINDOWS_EXTENDED_PATH_PREFIX, _WINDOWS_DEVICE_PATH_PREFIX)):
        return Path(raw)
    if raw.startswith("\\\\"):
        if not force and _windows_path_units(raw) < 248:
            return Path(raw)
        return Path(f"{_WINDOWS_EXTENDED_PATH_PREFIX}UNC\\{raw[2:]}")
    if not force and _windows_path_units(raw) < 248:
        return Path(raw)
    return Path(f"{_WINDOWS_EXTENDED_PATH_PREFIX}{raw}")


def _logical_path(path: Path | str) -> Path:
    """Resolve a path while keeping the public form free of ``\\\\?\\``."""

    candidate = Path(path)
    try:
        resolved = candidate.resolve()
    except OSError:
        if os.name != "nt":
            raise
        try:
            resolved = _windows_io_path(candidate).resolve()
        except OSError:
            # ``abspath`` is sufficient for generated paths when Windows
            # cannot perform a realpath lookup at the legacy path boundary.
            return Path(os.path.abspath(os.fspath(candidate)))

    raw = os.fspath(resolved)
    extended_unc_prefix = f"{_WINDOWS_EXTENDED_PATH_PREFIX}UNC\\"
    if raw.startswith(extended_unc_prefix):
        return Path(f"\\\\{raw[len(extended_unc_prefix):]}")
    if raw.startswith(_WINDOWS_EXTENDED_PATH_PREFIX):
        return Path(raw[len(_WINDOWS_EXTENDED_PATH_PREFIX) :])
    return Path(raw)


def _windows_path_units(path: Path | str) -> int:
    """Count Windows path length in UTF-16 code units."""

    return len(os.fspath(path).encode("utf-16-le")) // 2


def _transaction_token() -> str:
    """Return a short collision-resistant token for atomic temporary names."""

    return uuid.uuid4().hex[:_TRANSACTION_TOKEN_LENGTH]


def _install_long_path_temporary_cleanup() -> None:
    """Keep Python's temporary-directory lifecycle on the same I/O boundary.

    ``tempfile.TemporaryDirectory`` is used by Harness callers and by the
    isolated runtime tests.  Its creation and cleanup callbacks otherwise use
    legacy Windows paths, which can fail at a deep Output Root or leave a deep
    temporary tree behind after Harness code successfully wrote it through an
    extended path.  The wrappers change only those I/O boundaries; the name
    returned to callers remains the logical path.
    """

    if os.name != "nt":
        return
    import tempfile

    original_mkdtemp = getattr(tempfile, "mkdtemp", None)
    if original_mkdtemp is not None and not getattr(original_mkdtemp, "_hunnu_long_path", False):

        def long_path_mkdtemp(
            suffix: str | None = None,
            prefix: str | None = None,
            dir: str | os.PathLike[str] | None = None,
        ) -> str:
            if dir is None:
                return original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
            # Preserve tempfile's bytes-path API for callers outside the
            # Harness; the runtime paths used here are text paths.
            if any(isinstance(value, bytes) for value in (suffix, prefix, dir)):
                return original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
            logical_dir = _logical_path(dir)
            created = original_mkdtemp(
                suffix=suffix,
                prefix=prefix,
                dir=os.fspath(_windows_io_path(logical_dir, force=True)),
            )
            return os.fspath(_logical_path(created))

        setattr(long_path_mkdtemp, "_hunnu_long_path", True)
        tempfile.mkdtemp = long_path_mkdtemp

    original_rmtree = getattr(tempfile.TemporaryDirectory, "_rmtree", None)
    if original_rmtree is None or getattr(original_rmtree, "_hunnu_long_path", False):
        return

    def long_path_rmtree(name: Path | str, *args: object, **kwargs: object) -> object:
        return original_rmtree(_windows_io_path(name, force=True), *args, **kwargs)

    setattr(long_path_rmtree, "_hunnu_long_path", True)
    tempfile.TemporaryDirectory._rmtree = staticmethod(long_path_rmtree)


def _install_long_path_path_io() -> None:
    """Make direct ``pathlib`` I/O long-path aware without changing path text.

    Harness APIs intentionally expose ordinary logical ``Path`` values.  A
    caller may still use one of those values directly (for example
    ``path.read_bytes()``), so the compatibility boundary must cover that
    common operation as well as Harness-owned call sites.  Only paths close to
    the legacy Windows limit are redirected; the returned path and all values
    derived from it remain unprefixed.
    """

    if os.name != "nt":
        return
    path_type = type(Path())
    if getattr(path_type, "_hunnu_long_path_io", False):
        return

    def needs_extended(value: Path | str) -> bool:
        raw = os.fspath(value)
        return (
            not raw.startswith((_WINDOWS_EXTENDED_PATH_PREFIX, _WINDOWS_DEVICE_PATH_PREFIX))
            and _windows_path_units(raw) >= 248
        )

    methods = (
        "open",
        "stat",
        "lstat",
        "mkdir",
        "unlink",
        "rmdir",
        "replace",
        "rename",
        "chmod",
        "touch",
    )
    for name in methods:
        original = getattr(path_type, name)

        def wrapped(self: Path, *args: object, _original=original, **kwargs: object) -> object:
            if needs_extended(self):
                return _original(_windows_io_path(self), *args, **kwargs)
            return _original(self, *args, **kwargs)

        setattr(path_type, name, wrapped)

    for name in ("iterdir", "glob", "rglob"):
        original = getattr(path_type, name)

        def iter_wrapped(self: Path, *args: object, _original=original, **kwargs: object):
            # A recursive glob can start below MAX_PATH and cross the limit
            # only in a descendant directory.  Redirect the whole absolute
            # traversal so that pathlib does not fall back to legacy paths at
            # that point.
            if not self.is_absolute():
                return _original(self, *args, **kwargs)
            return (
                _logical_path(item)
                for item in _original(_windows_io_path(self, force=True), *args, **kwargs)
            )

        setattr(path_type, name, iter_wrapped)

    setattr(path_type, "_hunnu_long_path_io", True)


CORE_ROOT = _logical_path(Path(__file__).resolve().parents[2])
PROJECT_ROOT = CORE_ROOT

OUTPUT_ROOT_ENV = "HUNNU_HARNESS_OUTPUT_ROOT"
DEFAULT_OUTPUT_ROOT = CORE_ROOT.with_name(f"{CORE_ROOT.name}-Output")
OUTPUT_ROOT = _logical_path(Path(os.environ.get(OUTPUT_ROOT_ENV, DEFAULT_OUTPUT_ROOT)).expanduser())
if OUTPUT_ROOT == CORE_ROOT or OUTPUT_ROOT.is_relative_to(CORE_ROOT):
    raise RuntimeError(f"Harness Output Root must be outside Core Root: {OUTPUT_ROOT}")

_install_long_path_temporary_cleanup()
_install_long_path_path_io()

RUNS_ROOT = OUTPUT_ROOT / "runs"
DOWNLOADS_ROOT = OUTPUT_ROOT / "downloads"
AUTHORIZED_DOWNLOADS_DIR = DOWNLOADS_ROOT / "authorized"
STAGING_DIR = OUTPUT_ROOT / "staging"
RAW_DIR = AUTHORIZED_DOWNLOADS_DIR
PLAYWRIGHT_OUTPUT_DIR = STAGING_DIR / "playwright-output"
AUDIT_DIR = OUTPUT_ROOT / "audit"
MANIFESTS_DIR = OUTPUT_ROOT / "manifests"
LOGS_DIR = OUTPUT_ROOT / "logs"
SCREENSHOTS_DIR = OUTPUT_ROOT / "screenshots"
REVIEW_DIR = OUTPUT_ROOT / "review"
QUARANTINE_DIR = OUTPUT_ROOT / "quarantine"
TEMP_DIR = OUTPUT_ROOT / "temp"
MIGRATION_DIR = OUTPUT_ROOT / "migration"

# Long-lived personal literature corpus.  These paths are deliberately
# independent from both per-run acquisition evidence and generic/data
# downloads.
LIBRARY_ROOT = OUTPUT_ROOT / "library"
LIBRARY_PAPERS_DIR = LIBRARY_ROOT / "papers"
LIBRARY_NOTES_DIR = LIBRARY_ROOT / "notes"
LIBRARY_CATALOG_DIR = LIBRARY_ROOT / "catalog"
LIBRARY_CATALOG_JSONL = LIBRARY_CATALOG_DIR / "papers.jsonl"
LIBRARY_CATALOG_CSV = LIBRARY_CATALOG_DIR / "papers.csv"
LIBRARY_IMPORT_STAGING_DIR = LIBRARY_ROOT / "import_staging"

# Additive topic metadata produced alongside the catalog.  These are read-only
# navigation inputs; the Library writer does not own them.
LIBRARY_TOPICS_JSONL = LIBRARY_CATALOG_DIR / "paper_topics.jsonl"
LIBRARY_TOPICS_CSV = LIBRARY_CATALOG_DIR / "paper_topics.csv"
PAPERS_BY_TOPIC_DIR = OUTPUT_ROOT / "papers_by_topic"
TOPIC_TAXONOMY_JSON = OUTPUT_ROOT / "topic_taxonomy.json"
NAVIGATOR_LEXICON_JSON = OUTPUT_ROOT / "navigator_lexicon.json"
TAXONOMY_ALIASES_JSON = OUTPUT_ROOT / "taxonomy_aliases.json"

# Paper Research Navigator derived data.  Everything below this root is
# rebuildable from the catalog and the managed full texts; deleting it must
# never lose a paper, a version, or a topic assignment.
PAPER_RETRIEVAL_ROOT = OUTPUT_ROOT / "paper_retrieval"
PAPER_RETRIEVAL_INDEX_DIR = PAPER_RETRIEVAL_ROOT / "index"
PAPER_RETRIEVAL_INDEX_MANIFEST = PAPER_RETRIEVAL_INDEX_DIR / "manifest.json"
PAPER_RETRIEVAL_FULLTEXT_DIR = PAPER_RETRIEVAL_INDEX_DIR / "fulltext"
PAPER_RETRIEVAL_FULLTEXT_MANIFEST = PAPER_RETRIEVAL_INDEX_DIR / "fulltext_manifest.json"
PAPER_RETRIEVAL_PACKS_DIR = PAPER_RETRIEVAL_ROOT / "reading_packs"

V023_RUN_ROOT = RUNS_ROOT / "Harness_V023_Output_Root_Separation"
V024_RUN_ROOT = RUNS_ROOT / "Harness_V024_CNKI_Challenge_Detection_Hardening"
# Retained as a historical artifact location.
V025_RUN_ROOT = RUNS_ROOT / "Harness_V025_Agent_Integration_Global_Routing"
V0216_RUN_ROOT = RUNS_ROOT / "Harness_V0216_Agent_Adapter_First_Enforcement"
V0217_RUN_ROOT = RUNS_ROOT / "Harness_V0217_Browser_Command_Layer"
V026_RUN_ROOT = RUNS_ROOT / "Harness_V026_Oxford_Academic_Adapter"
V027_RUN_ROOT = RUNS_ROOT / "Harness_V027_Oxford_Unattended_PDF_Download"
V028_RUN_ROOT = RUNS_ROOT / "Harness_V028_MultiSource_Preflight_Coordinator"

LEGACY_RUNTIME_TOP_LEVEL_MAP = {
    "runs": Path("runs"),
    "downloads": Path("downloads"),
    "logs": Path("logs"),
    "manifests": Path("manifests"),
    "screenshots": Path("screenshots"),
    "temp": Path("temp"),
    "tmp": Path("temp") / "legacy-tmp",
    ".pytest_cache": Path("temp") / "core-cache" / "pytest_cache",
}
LEGACY_FORMAL_ROOT_FILES = frozenset(
    {
        "ARTIFACT_MIGRATION_AUDIT.md",
        "ARTIFACT_MIGRATION_HASH_CHECK.csv",
        "ARTIFACT_PATH_MIGRATION_MAP.csv",
        "PATH_MIGRATION_AUDIT.md",
    }
)


def is_within(path: Path, root: Path) -> bool:
    """Return whether *path* resolves inside *root* (including the root)."""

    resolved_path = _logical_path(path)
    resolved_root = _logical_path(root)
    return resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root)


def require_output_path(path: Path, *, label: str = "Harness runtime path") -> Path:
    """Resolve a runtime path and reject any destination outside Output Root."""

    resolved = _logical_path(path)
    if not is_within(resolved, OUTPUT_ROOT):
        raise ValueError(f"{label} must remain inside Harness Output Root: {OUTPUT_ROOT}")
    return resolved


def resolve_relocated_path(path: Path) -> Path:
    """Map a historical Core-root runtime path to its relocated Output path.

    The historical artifact is never rewritten.  This helper only resolves the
    physical location for readers that receive a pre-v0.2.3 path.
    """

    candidate = _logical_path(Path(path).expanduser())
    if not is_within(candidate, CORE_ROOT):
        return candidate
    relative = candidate.relative_to(_logical_path(CORE_ROOT))
    if relative.parts:
        mapped_root = LEGACY_RUNTIME_TOP_LEVEL_MAP.get(relative.parts[0].casefold())
        if mapped_root is not None:
            return _logical_path(OUTPUT_ROOT / mapped_root / Path(*relative.parts[1:]))
    if len(relative.parts) == 1:
        name = relative.name
        if name in LEGACY_FORMAL_ROOT_FILES:
            return _logical_path(MIGRATION_DIR / "legacy-core-root" / name)
        if relative.suffix.casefold() == ".log":
            return _logical_path(LOGS_DIR / "legacy-core-root" / name)
        if name.casefold().startswith("v21post_") or name == "v13_path_validation_20260814_post_cleanup.md":
            return _logical_path(MIGRATION_DIR / "legacy-core-root" / "generated" / name)
    return candidate
