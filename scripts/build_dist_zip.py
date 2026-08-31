"""Build the distributable zip of the HUNNU Research Harness.

The zip is built from ``git ls-files`` -- the tracked tree is the only source
of truth -- and then an explicit deny-list is applied on top.  ``.gitignore``
does not travel with a zip, so everything the repository merely ignores
(.venv, caches, output trees) must be excluded here explicitly, and everything
tracked-but-private (secrets/) must be refused by name.  Every excluded entry
is printed; exclusion is a decision, not a silent side effect.

Stdlib only, deliberately: this script must run before any dependency is
installed, with nothing but Python and git.

Usage (from anywhere; the repo root is derived from this file's location):

    python scripts/build_dist_zip.py [--output DIR] [--allow-dirty]

The zip lands in ``<Output Root>/dist/`` by default, never inside the repo.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Stable top-level folder inside the zip, independent of the local checkout
# directory name.
ARCHIVE_PREFIX = "HUNNU-Research-Harness"

# Explicit exclusions, per the distribution plan.  Directory entries match the
# path itself and everything below it.  Most of these are never tracked; they
# are listed anyway so that accidentally tracking one refuses loudly here
# instead of shipping silently.
DENY_DIRS = (
    ".venv",
    ".git",
    ".claude",
    "secrets",
    # Output-Root shaped trees must never ride along inside the code zip.
    "runs",
    "downloads",
    "logs",
    "manifests",
    "screenshots",
    "staging",
    "audit",
    "temp",
    "library",
    "quarantine",
    "review",
    "migration",
)
DENY_PARTS = ("__pycache__",)
DENY_SUFFIXES = (".pyc", ".pyo")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _project_version() -> str:
    for line in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return "unknown"


def _default_output_dir() -> Path:
    configured = os.environ.get("HUNNU_HARNESS_OUTPUT_ROOT")
    output_root = (
        Path(configured).expanduser()
        if configured
        else REPO_ROOT.with_name(f"{REPO_ROOT.name}-Output")
    )
    return output_root / "dist"


def _is_denied(relative: str) -> bool:
    parts = relative.split("/")
    if parts[0] in DENY_DIRS:
        return True
    if any(part in DENY_PARTS for part in parts):
        return True
    return relative.endswith(DENY_SUFFIXES)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Directory to write the zip into (default: <Output Root>/dist)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Build even with uncommitted changes (tests only; a release zip must match a commit)",
    )
    args = parser.parse_args()

    dirty = _git("status", "--porcelain").strip()
    if dirty and not args.allow_dirty:
        print(
            "REFUSED: the working tree has uncommitted changes; a release zip "
            "must correspond to one commit. Commit first, or pass --allow-dirty "
            "for a throwaway build.",
            file=sys.stderr,
        )
        print(dirty, file=sys.stderr)
        return 2

    tracked = [name for name in _git("ls-files", "-z").split("\0") if name]
    shipped: list[str] = []
    excluded: list[str] = []
    for relative in sorted(tracked):
        (excluded if _is_denied(relative) else shipped).append(relative)

    if not shipped:
        print("REFUSED: nothing to ship.", file=sys.stderr)
        return 2

    output_dir = args.output if args.output is not None else _default_output_dir()
    output_dir = output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / f"hunnu-research-harness-{_project_version()}-dist.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in shipped:
            archive.write(REPO_ROOT / relative, f"{ARCHIVE_PREFIX}/{relative}")

    for relative in excluded:
        print(f"excluded: {relative}")
    commit = _git("rev-parse", "--short", "HEAD").strip()
    print(f"commit: {commit}{' (dirty build)' if dirty else ''}")
    print(f"entries: {len(shipped)}")
    print(f"zip: {zip_path}")
    print(f"bytes: {zip_path.stat().st_size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
