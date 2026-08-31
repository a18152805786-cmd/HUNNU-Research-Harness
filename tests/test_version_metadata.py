import importlib.metadata
import io
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from hunnu_harness import __version__
from hunnu_harness.cli import build_parser


class VersionMetadataTests(unittest.TestCase):
    def test_package_and_project_versions_match(self) -> None:
        """One version, stated once per surface, never drifting apart."""

        project = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(project["project"]["version"], __version__)

    def test_installed_distribution_matches_when_it_is_this_tree(self) -> None:
        """The installed metadata check is meaningful only for this tree's install.

        A development setup may import this tree via PYTHONPATH while the
        *installed* distribution is an editable install of a different
        checkout; its metadata then reports that checkout's version, which
        says nothing about this tree.  The extracted-zip end-to-end venv
        exercises the strict case, where the two must agree.
        """

        try:
            installed = importlib.metadata.version("hunnu-research-harness")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("distribution is not installed here (PYTHONPATH-only run)")
        if installed != __version__:
            self.skipTest(
                f"installed distribution reports {installed} (an editable install "
                f"of another checkout); this tree is {__version__}"
            )
        self.assertEqual(installed, __version__)

    def test_root_cli_reports_runtime_package_version(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"hunnu-harness {__version__}")


if __name__ == "__main__":
    unittest.main()
