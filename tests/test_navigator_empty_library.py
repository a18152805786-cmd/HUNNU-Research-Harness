"""A fresh, small, or absent library explains itself instead of shrugging.

Task 3.7 of the distribution plan: a classmate's first contact with the
Navigator is an empty Output Root.  Zero results with no explanation reads as
"nothing relevant exists" or "it is broken"; a coverage analysis over five
works reads as "gaps everywhere".  These tests pin the honest alternatives:
say the library is empty and how it fills, refuse coverage analysis below the
floor, and carry a warning through the small-library band.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.navigator import cli as navigator_cli
from hunnu_harness.navigator.catalog import CatalogReader, LibraryUnavailable
from hunnu_harness.navigator.cli import EXIT_LIBRARY_UNAVAILABLE
from hunnu_harness.navigator.gaps import (
    MIN_WORKS_FOR_COVERAGE,
    SMALL_LIBRARY_CEILING,
    CoverageAnalyzer,
)
from hunnu_harness.navigator.index import NavigatorIndex
from hunnu_harness.navigator.search import PaperNavigator

from test_navigator_core import catalog_record, topic_record, write_catalog


def _navigator_with_works(directory: Path, count: int) -> PaperNavigator:
    records = []
    topics = []
    for position in range(count):
        paper_id = f"P{position:012X}"
        records.append(
            catalog_record(
                paper_id=paper_id,
                doi=f"10.1000/nav-{position}",
                canonical_sha=f"{position:064x}",
            )
        )
        topics.append(topic_record(paper_id))
    catalog, topics_path = write_catalog(directory, records, topics)
    reader = CatalogReader(catalog_path=catalog, topics_path=topics_path)
    return PaperNavigator(reader=reader, index=NavigatorIndex(root=directory / "no_index"))


class EmptyLibrarySearchTests(unittest.TestCase):
    def test_search_on_zero_works_explains_instead_of_shrugging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            navigator = _navigator_with_works(Path(raw), 0)
            payload = navigator.search("AI washing", top=5)
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["status"], "EMPTY_LIBRARY")
        self.assertIn("acquisition", payload["empty_library_note"])
        self.assertIn("expected", payload["empty_library_note"])

    def test_search_on_a_populated_library_carries_no_empty_note(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            navigator = _navigator_with_works(Path(raw), 2)
            payload = navigator.search("AI washing", top=5)
        self.assertNotIn("empty_library_note", payload)


class LibraryUnavailableExplanationTests(unittest.TestCase):
    def test_missing_catalog_payload_says_the_state_is_expected(self) -> None:
        emitted: list[dict] = []

        def fake_navigator():
            raise LibraryUnavailable("Paper catalog not found: <path>")

        args = argparse.Namespace(command="paper-search", query="x", top=3, no_fulltext=True, no_expand=True)
        original_emit, original_navigator = navigator_cli._emit, navigator_cli._navigator
        navigator_cli._emit, navigator_cli._navigator = emitted.append, fake_navigator
        try:
            exit_code = navigator_cli.run_navigator_command(args)
        finally:
            navigator_cli._emit, navigator_cli._navigator = original_emit, original_navigator

        self.assertEqual(exit_code, EXIT_LIBRARY_UNAVAILABLE)
        self.assertEqual(len(emitted), 1)
        payload = emitted[0]
        self.assertEqual(payload["status"], "LIBRARY_UNAVAILABLE")
        self.assertIn("expected, not broken", payload["empty_library_note"])
        self.assertIn("acquisition", payload["empty_library_note"])


class CoverageThresholdTests(unittest.TestCase):
    def test_below_the_floor_coverage_analysis_refuses_with_guidance(self) -> None:
        """This failing means paper-gaps went back to answering from a tiny corpus."""

        with tempfile.TemporaryDirectory() as raw:
            navigator = _navigator_with_works(Path(raw), MIN_WORKS_FOR_COVERAGE - 1)
            payload = CoverageAnalyzer(navigator).analyze("AI washing audit risk")
        self.assertEqual(payload["status"], "REFUSED_LIBRARY_TOO_SMALL")
        self.assertEqual(payload["works_in_library"], MIN_WORKS_FOR_COVERAGE - 1)
        self.assertIn("misleading", payload["why_refused"])
        self.assertIn("acquire", payload["how_to_proceed"])
        # The machine envelope survives refusal so callers keep their keys.
        self.assertIn("index_status", payload)
        self.assertNotIn("concept_coverage", payload)

    def test_inside_the_warning_band_analysis_runs_with_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            navigator = _navigator_with_works(Path(raw), MIN_WORKS_FOR_COVERAGE)
            payload = CoverageAnalyzer(navigator).analyze("AI washing audit risk")
        self.assertEqual(payload["status"], "ANALYZED")
        self.assertIn("concept_coverage", payload)
        self.assertIn("small_library_warning", payload)
        self.assertIn(str(MIN_WORKS_FOR_COVERAGE), payload["small_library_warning"])

    def test_above_the_ceiling_the_warning_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            navigator = _navigator_with_works(Path(raw), SMALL_LIBRARY_CEILING + 1)
            payload = CoverageAnalyzer(navigator).analyze("AI washing audit risk")
        self.assertEqual(payload["status"], "ANALYZED")
        self.assertNotIn("small_library_warning", payload)


if __name__ == "__main__":
    unittest.main()
