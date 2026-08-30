"""Fail-closed guard for the derived retrieval index build.

The defect this pins: ``paper-index rebuild`` under an interpreter without
``pypdf`` recorded an extraction failure for every work, still reported
BUILT, and replaced a healthy 8000+-chunk index with an empty one.  The
guard makes that impossible: systemic hard failure refuses to commit, keeps
the previous index byte-identical on disk, exits non-zero from the CLI, and
names the missing dependency.  Scattered per-document failure below the
threshold still commits and reports per-work statuses, because one damaged
PDF must not block indexing the other hundred and eighty.

Everything here runs on synthetic fixtures in temporary directories; nothing
reads the real corpus and nothing imports ``pypdf``.
"""

import argparse
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hunnu_harness.navigator import cli as navigator_cli
from hunnu_harness.navigator.catalog import CatalogReader, PaperVersion
from hunnu_harness.navigator.fulltext import (
    Chunk,
    EXTRACTION_DEPENDENCY_MISSING,
    EXTRACTION_FAILED,
    EXTRACTION_MISSING,
    EXTRACTION_OK,
    ExtractionResult,
    FullTextExtractor,
)
from hunnu_harness.navigator.index import (
    IndexBuildRefused,
    IndexStatus,
    NavigatorIndex,
)

from test_navigator_core import catalog_record, topic_record, write_catalog

PYPDF_DETAIL = "ModuleNotFoundError: No module named 'pypdf'"


def unlocked_output_paths():
    """The same Output-Root bypass every synthetic index test uses."""

    return patch(
        "hunnu_harness.navigator.index.require_output_path", side_effect=lambda p, **k: p
    )


class ScriptedExtractor(FullTextExtractor):
    """Deterministic per-work outcomes; touches no file and no pypdf."""

    def __init__(self, script=None, default=(EXTRACTION_OK, "")):
        super().__init__()
        self.script = dict(script or {})
        self.default = default

    def extract(self, paper_id, version):
        status, detail = self.script.get(paper_id, self.default)
        chunks = ()
        if status == EXTRACTION_OK:
            text = f"synthetic passage for {paper_id}"
            chunks = (
                Chunk(
                    chunk_id=f"{paper_id}:{version.sha256[:12]}:p1:c0",
                    paper_id=paper_id,
                    version_sha256=version.sha256,
                    managed_path=version.managed_path,
                    page=1,
                    ordinal=0,
                    text=text,
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                ),
            )
        return ExtractionResult(
            paper_id=paper_id,
            version_sha256=version.sha256,
            managed_path=version.managed_path,
            status=status,
            pages_read=1 if chunks else 0,
            chunks=chunks,
            detail=detail,
        )


def make_snapshot(directory: Path, count: int):
    letters = "ABCDEFGH"
    records, topic_rows = [], []
    for position in range(count):
        paper_id = "P" + letters[position] * 13
        records.append(
            catalog_record(
                paper_id=paper_id,
                doi=f"10.1000/nav-fail-{position}",
                canonical_sha=format(position, "x") * 64,
            )
        )
        topic_rows.append(topic_record(paper_id))
    catalog, topics = write_catalog(directory, records, topic_rows)
    return CatalogReader(catalog_path=catalog, topics_path=topics).load()


def paper_ids(snapshot):
    return [work.paper_id for work in snapshot.works]


def index_state(root: Path) -> dict[str, bytes]:
    """Every file under the index root, byte for byte."""

    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class FailClosedGuardTests(unittest.TestCase):
    def test_rebuild_without_pypdf_refuses_and_keeps_the_previous_index(self):
        """The reported defect, end to end at the API layer."""

        with tempfile.TemporaryDirectory() as raw, unlocked_output_paths():
            directory = Path(raw)
            snapshot = make_snapshot(directory, 3)
            root = directory / "paper_retrieval"
            healthy = NavigatorIndex(root=root, extractor=ScriptedExtractor())
            report = healthy.build(snapshot)
            self.assertEqual(report["status"], "BUILT")
            self.assertEqual(report["manifest"]["fulltext_chunks"], 3)
            before = index_state(root)
            self.assertTrue(before)

            broken = NavigatorIndex(
                root=root,
                extractor=ScriptedExtractor(
                    default=(EXTRACTION_DEPENDENCY_MISSING, PYPDF_DETAIL)
                ),
            )
            with self.assertRaises(IndexBuildRefused) as caught:
                broken.rebuild(snapshot)

            self.assertEqual(index_state(root), before)
            self.assertIs(broken.status(snapshot)[0], IndexStatus.FRESH)
            self.assertEqual(broken.read_manifest().fulltext_chunks, 3)

            payload = caught.exception.payload
            self.assertEqual(payload["status"], "REFUSED")
            self.assertEqual(payload["extraction_attempts"], 3)
            self.assertEqual(payload["hard_failure_count"], 3)
            self.assertTrue(payload["previous_index_kept"])
            self.assertIn("pypdf", payload["reason"])
            self.assertIn("missing_dependency_hint", payload)
            self.assertIn("pypdf", str(caught.exception))

    def test_total_generic_extraction_failure_also_refuses(self):
        """The guard is about systemic failure, not only missing imports."""

        with tempfile.TemporaryDirectory() as raw, unlocked_output_paths():
            directory = Path(raw)
            snapshot = make_snapshot(directory, 3)
            index = NavigatorIndex(
                root=directory / "paper_retrieval",
                extractor=ScriptedExtractor(
                    default=(EXTRACTION_FAILED, "ValueError: broken xref table")
                ),
            )
            with self.assertRaises(IndexBuildRefused) as caught:
                index.build(snapshot)
            # A refused first-ever build writes nothing at all.
            self.assertEqual(index_state(directory / "paper_retrieval"), {})
            self.assertIs(index.status(snapshot)[0], IndexStatus.ABSENT)
            payload = caught.exception.payload
            self.assertIn("broken xref table", payload["reason"])
            self.assertNotIn("missing_dependency_hint", payload)

    def test_partial_failure_below_threshold_commits_and_reports_statuses(self):
        with tempfile.TemporaryDirectory() as raw, unlocked_output_paths():
            directory = Path(raw)
            snapshot = make_snapshot(directory, 4)
            failing = paper_ids(snapshot)[0]
            index = NavigatorIndex(
                root=directory / "paper_retrieval",
                extractor=ScriptedExtractor(
                    {failing: (EXTRACTION_FAILED, "EOFError: truncated PDF")}
                ),
            )
            report = index.rebuild(snapshot)

            self.assertEqual(report["status"], "BUILT")
            self.assertFalse(report["degraded"])
            self.assertEqual(
                report["extraction_status_counts"], {"EXTRACTION_FAILED": 1, "OK": 3}
            )
            self.assertEqual(report["hard_failure_count"], 1)
            self.assertEqual(report["hard_failures"][0]["paper_id"], failing)
            self.assertIn("EOFError", report["hard_failures"][0]["detail"])
            self.assertEqual(report["manifest"]["fulltext_works"], 3)
            self.assertEqual(report["manifest"]["fulltext_chunks"], 3)

            # Per-work statuses are committed to the fulltext manifest on disk.
            entries = index._read_fulltext_manifest()
            self.assertEqual(entries[failing]["status"], EXTRACTION_FAILED)
            self.assertFalse((index.fulltext_dir / f"{failing}.jsonl").exists())
            for pid in paper_ids(snapshot):
                if pid == failing:
                    continue
                self.assertEqual(entries[pid]["status"], EXTRACTION_OK)
                self.assertTrue((index.fulltext_dir / f"{pid}.jsonl").is_file())

    def test_failure_share_at_the_threshold_still_commits(self):
        """The bar is strictly 'more than', so exactly half commits."""

        with tempfile.TemporaryDirectory() as raw, unlocked_output_paths():
            directory = Path(raw)
            snapshot = make_snapshot(directory, 4)
            failing = paper_ids(snapshot)[:2]
            index = NavigatorIndex(
                root=directory / "paper_retrieval",
                extractor=ScriptedExtractor(
                    {pid: (EXTRACTION_FAILED, "EOFError: truncated PDF") for pid in failing}
                ),
            )
            report = index.build(snapshot)
            self.assertEqual(report["status"], "BUILT")
            self.assertFalse(report["degraded"])
            self.assertEqual(report["hard_failure_count"], 2)

    def test_failure_share_above_the_threshold_refuses(self):
        with tempfile.TemporaryDirectory() as raw, unlocked_output_paths():
            directory = Path(raw)
            snapshot = make_snapshot(directory, 4)
            root = directory / "paper_retrieval"
            healthy = NavigatorIndex(root=root, extractor=ScriptedExtractor())
            healthy.build(snapshot)
            before = index_state(root)

            failing = paper_ids(snapshot)[:3]
            index = NavigatorIndex(
                root=root,
                extractor=ScriptedExtractor(
                    {pid: (EXTRACTION_FAILED, "EOFError: truncated PDF") for pid in failing}
                ),
            )
            with self.assertRaises(IndexBuildRefused) as caught:
                index.build(snapshot)
            self.assertEqual(index_state(root), before)
            self.assertEqual(caught.exception.payload["hard_failure_share"], 0.75)

    def test_missing_files_do_not_dilute_the_failure_share(self):
        """Two absent PDFs plus one dependency failure is total failure, not 1/3."""

        with tempfile.TemporaryDirectory() as raw, unlocked_output_paths():
            directory = Path(raw)
            snapshot = make_snapshot(directory, 3)
            ids = paper_ids(snapshot)
            index = NavigatorIndex(
                root=directory / "paper_retrieval",
                extractor=ScriptedExtractor(
                    {
                        ids[0]: (EXTRACTION_MISSING, ""),
                        ids[1]: (EXTRACTION_MISSING, ""),
                        ids[2]: (EXTRACTION_DEPENDENCY_MISSING, PYPDF_DETAIL),
                    }
                ),
            )
            with self.assertRaises(IndexBuildRefused) as caught:
                index.build(snapshot)
            self.assertEqual(caught.exception.payload["extraction_attempts"], 1)
            self.assertEqual(caught.exception.payload["hard_failure_count"], 1)

    def test_missing_files_alone_never_trip_the_guard(self):
        """The synthetic-fixture status quo: an all-FILE_MISSING corpus commits."""

        with tempfile.TemporaryDirectory() as raw, unlocked_output_paths():
            directory = Path(raw)
            snapshot = make_snapshot(directory, 3)
            index = NavigatorIndex(root=directory / "paper_retrieval")
            report = index.build(snapshot)
            self.assertEqual(report["status"], "BUILT")
            self.assertEqual(report["extraction_status_counts"], {"FILE_MISSING": 3})
            self.assertEqual(report["extraction_attempts"], 0)
            self.assertFalse(report["degraded"])

    def test_allow_degraded_commits_the_empty_index_explicitly(self):
        with tempfile.TemporaryDirectory() as raw, unlocked_output_paths():
            directory = Path(raw)
            snapshot = make_snapshot(directory, 3)
            root = directory / "paper_retrieval"
            healthy = NavigatorIndex(root=root, extractor=ScriptedExtractor())
            healthy.build(snapshot)

            broken = NavigatorIndex(
                root=root,
                extractor=ScriptedExtractor(
                    default=(EXTRACTION_DEPENDENCY_MISSING, PYPDF_DETAIL)
                ),
            )
            report = broken.rebuild(snapshot, allow_degraded=True)
            self.assertEqual(report["status"], "BUILT")
            self.assertTrue(report["degraded"])
            self.assertEqual(report["manifest"]["fulltext_chunks"], 0)
            self.assertEqual(broken.read_manifest().fulltext_chunks, 0)
            self.assertIs(broken.status(snapshot)[0], IndexStatus.FRESH)


class DependencyClassificationTests(unittest.TestCase):
    def test_import_error_is_classified_as_dependency_missing(self):
        """The real extractor, with pypdf genuinely unimportable."""

        with tempfile.TemporaryDirectory() as raw:
            pdf = Path(raw) / "work.pdf"
            pdf.write_bytes(b"%PDF-1.4\n% not really a pdf\n")
            version = PaperVersion(
                sha256="b" * 64,
                managed_path=str(pdf),
                full_text_format="PDF",
                version_role="CANONICAL_VERSION",
                source_type="EXTERNAL_IMPORT",
                source_locator="TEST",
                status="MANAGED",
                acquired_at="unknown",
                imported_at="2026-01-01T00:00:00+00:00",
            )
            with patch.dict(sys.modules, {"pypdf": None}):
                result = FullTextExtractor().extract("PAAAAAAAAAAAA", version)
            self.assertEqual(result.status, EXTRACTION_DEPENDENCY_MISSING)
            self.assertIn("pypdf", result.detail)
            self.assertEqual(result.chunks, ())


class CliFailClosedTests(unittest.TestCase):
    def _args(self, **overrides):
        base = dict(
            command="paper-index",
            action="rebuild",
            page_limit=None,
            quiet=True,
            allow_degraded=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_parser_accepts_allow_degraded(self):
        parser = argparse.ArgumentParser()
        navigator_cli.add_navigator_subcommands(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["paper-index", "rebuild", "--allow-degraded"])
        self.assertTrue(args.allow_degraded)
        args = parser.parse_args(["paper-index", "rebuild"])
        self.assertFalse(args.allow_degraded)

    def _run_cli(self, snapshot, root, extractor, args):
        emitted = []

        def fake_reader(**kwargs):
            return SimpleNamespace(load=lambda: snapshot)

        def fake_index(**kwargs):
            return NavigatorIndex(root=root, extractor=extractor)

        with patch.object(navigator_cli, "_emit", emitted.append), patch(
            "hunnu_harness.navigator.catalog.CatalogReader", fake_reader
        ), patch("hunnu_harness.navigator.index.NavigatorIndex", fake_index):
            code = navigator_cli.run_navigator_command(args)
        return code, emitted

    def test_cli_rebuild_exits_nonzero_and_keeps_the_index(self):
        with tempfile.TemporaryDirectory() as raw, unlocked_output_paths():
            directory = Path(raw)
            snapshot = make_snapshot(directory, 3)
            root = directory / "paper_retrieval"
            healthy = NavigatorIndex(root=root, extractor=ScriptedExtractor())
            healthy.build(snapshot)
            before = index_state(root)

            code, emitted = self._run_cli(
                snapshot,
                root,
                ScriptedExtractor(default=(EXTRACTION_DEPENDENCY_MISSING, PYPDF_DETAIL)),
                self._args(),
            )

            self.assertEqual(code, navigator_cli.EXIT_INDEX_FAILED)
            self.assertNotEqual(code, navigator_cli.EXIT_OK)
            self.assertEqual(index_state(root), before)
            payload = emitted[-1]
            self.assertEqual(payload["status"], "REFUSED")
            self.assertIn("pypdf", payload["reason"])
            self.assertIn("missing_dependency_hint", payload)

    def test_cli_allow_degraded_still_commits_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as raw, unlocked_output_paths():
            directory = Path(raw)
            snapshot = make_snapshot(directory, 3)
            root = directory / "paper_retrieval"
            healthy = NavigatorIndex(root=root, extractor=ScriptedExtractor())
            healthy.build(snapshot)

            code, emitted = self._run_cli(
                snapshot,
                root,
                ScriptedExtractor(default=(EXTRACTION_DEPENDENCY_MISSING, PYPDF_DETAIL)),
                self._args(allow_degraded=True),
            )

            self.assertEqual(code, navigator_cli.EXIT_OK)
            payload = emitted[-1]
            self.assertEqual(payload["status"], "BUILT")
            self.assertTrue(payload["degraded"])
            self.assertEqual(
                NavigatorIndex(root=root).read_manifest().fulltext_chunks, 0
            )


if __name__ == "__main__":
    unittest.main()
