"""Task 3.2: with --json, stdout is exactly one json.loads-able document.

An agent driving the CLI should never have to scrape Key=Value text.  The
contract is uniform: every subcommand accepts --json; commands that already
emit a single JSON document treat it as a no-op; the Key=Value emitters
switch to one document on stdout with human-directed sentences on stderr.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hunnu_harness.cli import _acquire_to_literature_argv, build_parser, main as harness_main
from hunnu_harness.literature import cli as literature_cli


def _subparsers(parser):
    for action in parser._actions:
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            return action.choices
    raise AssertionError("parser has no subcommands")


class UniformJsonFlagTests(unittest.TestCase):
    def test_every_main_cli_subcommand_accepts_json(self) -> None:
        for name, sub in _subparsers(build_parser()).items():
            with self.subTest(command=name):
                self.assertTrue(
                    any(action.dest == "json" for action in sub._actions),
                    f"{name} lacks --json",
                )

    def test_every_literature_subcommand_accepts_json(self) -> None:
        for name, sub in _subparsers(literature_cli.build_parser()).items():
            with self.subTest(command=name):
                self.assertTrue(
                    any(action.dest == "json" for action in sub._actions),
                    f"{name} lacks --json",
                )

    def test_acquire_forwards_json_to_the_literature_surface(self) -> None:
        args = build_parser().parse_args(
            ["acquire", "--source", "cnki", "--title", "x", "--json"]
        )
        self.assertIn("--json", _acquire_to_literature_argv(args))


class JsonStdoutTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = harness_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_env_json_is_one_parseable_document(self) -> None:
        code, out, _err = self._run(["env", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        for key in ("CoreRoot", "OutputRoot", "LibraryRoot", "Python"):
            self.assertIn(key, payload)

    def test_env_plain_output_is_unchanged(self) -> None:
        code, out, _err = self._run(["env"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("CoreRoot="))

    def test_browser_status_json_is_one_parseable_document(self) -> None:
        fake_status = SimpleNamespace(as_dict=lambda: {"Running": False, "Endpoint": ""})
        with patch("hunnu_harness.browser.persistent_browser.probe", return_value=fake_status):
            code, out, _err = self._run(["browser-status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["Running"], False)


class LiveJsonModeTests(unittest.TestCase):
    def test_live_failure_report_is_json_with_notes_on_stderr(self) -> None:
        class _Unavailable:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def start(self) -> None:
                raise literature_cli.PlaywrightUnavailable("browser extra is not installed")

            async def lifecycle(self) -> dict:
                return {}

            async def close(self) -> None:
                return None

        out, err = io.StringIO(), io.StringIO()
        with patch.object(literature_cli, "PlaywrightBrowser", _Unavailable):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = literature_cli.main(["live-cnki", "--title", "x", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 4)
        self.assertEqual(payload["Status"], "SOURCE_UNAVAILABLE")
        self.assertIn("HumanNotes", payload)

    def test_live_failure_plain_output_is_unchanged(self) -> None:
        class _Unavailable:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def start(self) -> None:
                raise literature_cli.PlaywrightUnavailable("browser extra is not installed")

            async def lifecycle(self) -> dict:
                return {}

            async def close(self) -> None:
                return None

        out = io.StringIO()
        with patch.object(literature_cli, "PlaywrightBrowser", _Unavailable):
            with contextlib.redirect_stdout(out):
                code = literature_cli.main(["live-cnki", "--title", "x"])
        self.assertEqual(code, 4)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "Status=SOURCE_UNAVAILABLE")
        self.assertTrue(lines[1].startswith("Reason="))


if __name__ == "__main__":
    unittest.main()
