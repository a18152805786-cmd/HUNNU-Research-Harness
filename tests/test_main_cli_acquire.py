"""Task 3.1: acquisition is a first-class citizen of the main CLI.

Live acquisition used to live only in ``python -m hunnu_harness.literature``,
invisible to ``hunnu-harness --help``.  These tests pin the promotion: the
entry is visible, and the translation onto the literature surface forwards
only what the caller set, so the literature parser remains the single owner
of per-source defaults.
"""

from __future__ import annotations

import unittest

from hunnu_harness.cli import _acquire_to_literature_argv, build_parser
from hunnu_harness.literature.artifacts import CNKI_UPGRADE_RUN_ROOT, OXFORD_UPGRADE_RUN_ROOT
from hunnu_harness.literature.cli import build_parser as build_literature_parser


class AcquireVisibilityTests(unittest.TestCase):
    def test_main_help_shows_the_acquisition_entry(self) -> None:
        self.assertIn("acquire", build_parser().format_help())

    def test_acquire_requires_a_source_and_a_selector(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["acquire", "--title", "x"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["acquire", "--source", "cnki"])


class AcquireTranslationTests(unittest.TestCase):
    def _translate(self, argv: list[str]) -> list[str]:
        return _acquire_to_literature_argv(build_parser().parse_args(argv))

    def test_minimal_invocation_forwards_only_what_was_set(self) -> None:
        self.assertEqual(
            self._translate(["acquire", "--source", "cnki", "--title", "x"]),
            ["live-cnki", "--title", "x"],
        )

    def test_flags_forward_and_switches_append(self) -> None:
        argv = self._translate(
            [
                "acquire",
                "--source",
                "sciencedirect",
                "--doi",
                "10.1/x",
                "--max-results",
                "2",
                "--daily-limit",
                "7",
                "--headless",
                "--allow-refetch",
            ]
        )
        self.assertEqual(argv[0], "live-sciencedirect")
        self.assertIn("--doi", argv)
        self.assertEqual(argv[argv.index("--max-results") + 1], "2")
        self.assertEqual(argv[argv.index("--daily-limit") + 1], "7")
        self.assertIn("--headless", argv)
        self.assertIn("--allow-refetch", argv)

    def test_per_source_defaults_stay_owned_by_the_literature_parser(self) -> None:
        for source, expected_run_root in (
            ("cnki", CNKI_UPGRADE_RUN_ROOT),
            ("oxfordacademic", OXFORD_UPGRADE_RUN_ROOT),
        ):
            with self.subTest(source=source):
                translated = self._translate(["acquire", "--source", source, "--title", "x"])
                live_args = build_literature_parser().parse_args(translated)
                self.assertEqual(live_args.run_root, expected_run_root)
                self.assertFalse(live_args.allow_refetch)
                self.assertIsNone(live_args.daily_limit)


if __name__ == "__main__":
    unittest.main()
