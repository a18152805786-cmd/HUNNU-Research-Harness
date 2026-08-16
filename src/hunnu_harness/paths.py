"""Canonical Core Root and Output Root separation for Harness paths.

``PROJECT_ROOT`` remains a compatibility alias for the source repository.  No
runtime writer should derive a destination from it.  Runtime paths are derived
only from ``OUTPUT_ROOT``.
"""

from __future__ import annotations

import os
from pathlib import Path


CORE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = CORE_ROOT

OUTPUT_ROOT_ENV = "HUNNU_HARNESS_OUTPUT_ROOT"
DEFAULT_OUTPUT_ROOT = CORE_ROOT.with_name(f"{CORE_ROOT.name}-Output")
OUTPUT_ROOT = Path(os.environ.get(OUTPUT_ROOT_ENV, DEFAULT_OUTPUT_ROOT)).expanduser().resolve()
if OUTPUT_ROOT == CORE_ROOT or OUTPUT_ROOT.is_relative_to(CORE_ROOT):
    raise RuntimeError(f"Harness Output Root must be outside Core Root: {OUTPUT_ROOT}")

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

V023_RUN_ROOT = RUNS_ROOT / "Harness_V023_Output_Root_Separation"
V024_RUN_ROOT = RUNS_ROOT / "Harness_V024_CNKI_Challenge_Detection_Hardening"
V025_RUN_ROOT = RUNS_ROOT / "Harness_V025_Agent_Integration_Global_Routing"
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

    resolved_path = Path(path).resolve()
    resolved_root = Path(root).resolve()
    return resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root)


def require_output_path(path: Path, *, label: str = "Harness runtime path") -> Path:
    """Resolve a runtime path and reject any destination outside Output Root."""

    resolved = Path(path).resolve()
    if not is_within(resolved, OUTPUT_ROOT):
        raise ValueError(f"{label} must remain inside Harness Output Root: {OUTPUT_ROOT}")
    return resolved


def resolve_relocated_path(path: Path) -> Path:
    """Map a historical Core-root runtime path to its relocated Output path.

    The historical artifact is never rewritten.  This helper only resolves the
    physical location for readers that receive a pre-v0.2.3 path.
    """

    candidate = Path(path).expanduser().resolve()
    if not is_within(candidate, CORE_ROOT):
        return candidate
    relative = candidate.relative_to(CORE_ROOT.resolve())
    if relative.parts:
        mapped_root = LEGACY_RUNTIME_TOP_LEVEL_MAP.get(relative.parts[0].casefold())
        if mapped_root is not None:
            return (OUTPUT_ROOT / mapped_root / Path(*relative.parts[1:])).resolve()
    if len(relative.parts) == 1:
        name = relative.name
        if name in LEGACY_FORMAL_ROOT_FILES:
            return (MIGRATION_DIR / "legacy-core-root" / name).resolve()
        if relative.suffix.casefold() == ".log":
            return (LOGS_DIR / "legacy-core-root" / name).resolve()
        if name.casefold().startswith("v21post_") or name == "v13_path_validation_20260814_post_cleanup.md":
            return (MIGRATION_DIR / "legacy-core-root" / "generated" / name).resolve()
    return candidate
