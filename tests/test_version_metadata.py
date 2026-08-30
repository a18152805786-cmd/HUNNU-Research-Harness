import importlib.metadata
import io
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from hunnu_harness import __version__
from hunnu_harness.cli import build_parser


class VersionMetadataTests(unittest.TestCase):
    def test_package_project_and_installed_distribution_versions_match(self) -> None:
        project = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(project["project"]["version"], "0.2.22")
        self.assertEqual(__version__, "0.2.22")
        self.assertEqual(
            importlib.metadata.version("hunnu-research-harness"),
            "0.2.22",
        )

    def test_root_cli_reports_runtime_package_version(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "hunnu-harness 0.2.22")


if __name__ == "__main__":
    unittest.main()
