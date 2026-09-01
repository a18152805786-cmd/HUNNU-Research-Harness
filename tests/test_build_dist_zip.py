"""The distribution zip ships the tracked tree and nothing private.

A zip has no .gitignore semantics: whatever lands in it is what classmates
receive.  These tests pin the packaging script's two duties -- ship the code
(including the packaged vocabulary data), and refuse the private/dev trees by
explicit name.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from hunnu_harness.paths import TEMP_DIR, _windows_io_path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_dist_zip.py"


class DistZipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        toplevel = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if (
            toplevel.returncode != 0
            or Path(toplevel.stdout.strip()).resolve() != REPO_ROOT
        ):
            raise unittest.SkipTest(
                "no git metadata of its own here: an extracted distribution "
                "cannot rebuild its zip; this gate runs in the repository"
            )
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        cls._holder = tempfile.TemporaryDirectory(prefix="dist-zip-", dir=TEMP_DIR)
        cls.output_dir = Path(cls._holder.name)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--output", str(cls.output_dir), "--allow-dirty"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, f"build failed:\n{result.stdout}\n{result.stderr}"
        cls.stdout = result.stdout
        zips = sorted(cls.output_dir.glob("*.zip"))
        assert len(zips) == 1, f"expected exactly one zip, found {zips}"
        # zipfile opens via io.open, below the pathlib long-path shims, so a
        # deep Output Root needs the extended-length form here.
        with zipfile.ZipFile(_windows_io_path(zips[0])) as archive:
            cls.names = archive.namelist()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._holder.cleanup()

    def test_the_zip_carries_code_docs_and_packaged_vocabulary_data(self) -> None:
        expected = (
            "HUNNU-Research-Harness/README.md",
            "HUNNU-Research-Harness/AGENTS.md",
            "HUNNU-Research-Harness/pyproject.toml",
            "HUNNU-Research-Harness/src/hunnu_harness/paths.py",
            "HUNNU-Research-Harness/src/hunnu_harness/navigator/lexicon.default.json",
            "HUNNU-Research-Harness/src/hunnu_harness/literature/taxonomy_aliases.default.json",
            "HUNNU-Research-Harness/tests/conftest.py",
        )
        for name in expected:
            self.assertIn(name, self.names)

    def test_private_and_dev_trees_are_excluded_by_name(self) -> None:
        """This failing means the zip started shipping private or dev state."""

        for name in self.names:
            relative = name.split("/", 1)[1] if "/" in name else name
            top = relative.split("/")[0]
            self.assertNotIn(
                top,
                {".venv", ".git", ".claude", "secrets", "runs", "downloads", "logs", "temp"},
                name,
            )
            self.assertNotIn("__pycache__", name)
            self.assertFalse(name.endswith((".pyc", ".pyo")), name)

    def test_every_entry_lives_under_the_stable_prefix(self) -> None:
        self.assertTrue(all(name.startswith("HUNNU-Research-Harness/") for name in self.names))

    def test_exclusions_are_printed_not_silent(self) -> None:
        self.assertIn("excluded: secrets/.gitkeep", self.stdout)
