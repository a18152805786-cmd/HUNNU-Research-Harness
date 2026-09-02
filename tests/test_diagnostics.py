"""Task 3.4: an agent can judge this machine before spending anything.

``capabilities`` answers "what does this build support" without touching the
environment; ``doctor`` answers "is this machine ready" without touching the
network, and grades its first blocking finding on the shared exit ladder.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness import diagnostics, paths
from hunnu_harness.cli import main as harness_main
from hunnu_harness.exit_codes import EXIT_CAPABILITY_MISSING, EXIT_ENV_NOT_READY, EXIT_OK
from hunnu_harness.paths import TEMP_DIR


def _run(argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = harness_main(argv)
    return code, json.loads(out.getvalue())


class CapabilitiesTests(unittest.TestCase):
    def test_capabilities_reports_sources_ladder_and_knobs(self) -> None:
        code, payload = _run(["capabilities"])
        self.assertEqual(code, 0)
        self.assertEqual(
            {source["cli_source"] for source in payload["Sources"]},
            {"sciencedirect", "springerlink", "cnki", "oxfordacademic"},
        )
        self.assertEqual(set(payload["ExitCodeLadder"]), {"0", "1", "2", "3", "4", "5"})
        self.assertIn("HUNNU_HARNESS_DAILY_FETCH_LIMIT", payload["Knobs"])
        self.assertIn("per_identifier_daily_limit", payload["NotKnobs"])
        self.assertFalse(payload["NetworkActivity"])

    def test_capabilities_accepts_json_flag_as_noop(self) -> None:
        code, payload = _run(["capabilities", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("Sources", payload)


class DoctorTests(unittest.TestCase):
    def test_doctor_on_this_machine_is_ready_and_exits_zero(self) -> None:
        code, payload = _run(["doctor"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["Verdict"], "READY")
        for name in (
            "python",
            "playwright",
            "pypdf",
            "chrome",
            "output_root",
            "fetch_budget",
            "vocabulary",
        ):
            self.assertIn(name, payload["Checks"])
        self.assertTrue(payload["Checks"]["output_root"]["ok"])

    def test_missing_playwright_grades_capability_missing(self) -> None:
        with patch("importlib.util.find_spec", return_value=None):
            payload, code = diagnostics.build_doctor()
        self.assertEqual(code, EXIT_CAPABILITY_MISSING)
        self.assertEqual(payload["Verdict"], "CAPABILITY_MISSING")
        self.assertIn("pip install", payload["Checks"]["playwright"]["detail"])

    def test_missing_pypdf_grades_capability_missing(self) -> None:
        """A foreign interpreter without pypdf is named before a rebuild is tried."""

        real_find_spec = importlib.util.find_spec

        def only_pypdf_missing(name, *args, **kwargs):
            return None if name == "pypdf" else real_find_spec(name, *args, **kwargs)

        with patch("importlib.util.find_spec", side_effect=only_pypdf_missing):
            payload, code = diagnostics.build_doctor()
        self.assertEqual(code, EXIT_CAPABILITY_MISSING)
        self.assertEqual(payload["Verdict"], "CAPABILITY_MISSING")
        self.assertTrue(payload["Checks"]["playwright"]["ok"])
        self.assertFalse(payload["Checks"]["pypdf"]["ok"])
        self.assertIn("paper-index", payload["Checks"]["pypdf"]["detail"])

    def test_unwritable_output_root_grades_env_not_ready(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="doctor-", dir=TEMP_DIR) as tmp:
            blocking_file = Path(tmp) / "not-a-directory"
            blocking_file.write_text("occupied", encoding="utf-8")
            with patch.object(paths, "OUTPUT_ROOT", blocking_file):
                payload, code = diagnostics.build_doctor()
        self.assertEqual(code, EXIT_ENV_NOT_READY)
        self.assertEqual(payload["Verdict"], "ENV_NOT_READY")
        self.assertFalse(payload["Checks"]["output_root"]["ok"])


if __name__ == "__main__":
    unittest.main()
