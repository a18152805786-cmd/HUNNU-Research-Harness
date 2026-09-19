import itertools
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.downloads.manager import DownloadManager, DownloadTimeout
from hunnu_harness.models import DownloadRequest


class DownloadManagerTests(unittest.TestCase):
    def test_hash_and_manifest_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watched = root / "watch"
            archive = root / "archive"
            manifests = root / "manifests"
            watched.mkdir()
            source = watched / "sample.csv"
            source.write_text("a,b\n1,2\n", encoding="utf-8")
            manager = DownloadManager([watched], archive, manifests)
            self.assertTrue(manager.verify_complete(source, stable_seconds=0))
            record = manager.archive_file(source, DownloadRequest(database="CNRDS", module="CNFS", table="利润表"), read_only=False)
            self.assertTrue(Path(record.archived_path).exists())
            self.assertTrue(record.sha256)
            primary_manifests = [path for path in manifests.rglob("*.json") if path.parent.name != "metadata"]
            metadata_manifests = list((manifests / "CNRDS").rglob("metadata/*.json"))
            self.assertEqual(len(primary_manifests), 1)
            self.assertEqual(len(metadata_manifests), 1)

    def test_partial_suffix_is_ignored(self):
        self.assertTrue(DownloadManager.is_partial(Path("data.csv.crdownload")))
        self.assertFalse(DownloadManager.is_partial(Path("data.csv")))

    def test_empty_baseline_is_not_replaced_by_a_fresh_snapshot(self):
        """This failing means an empty caller baseline is treated as missing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watched = root / "watch"
            watched.mkdir()
            archive = root / "archive"
            manifests = root / "manifests"
            candidate = watched / "new.csv"
            candidate.write_text("new\n", encoding="utf-8")
            manager = DownloadManager([watched], archive, manifests)

            with patch("hunnu_harness.downloads.manager.time.sleep", return_value=None):
                found = manager.wait_for_new_download(
                    set(),
                    timeout_seconds=0.05,
                    stable_seconds=0,
                )

            self.assertEqual(found, candidate.resolve())

    def test_wait_uses_snapshot_mtime_when_sorting_candidates(self):
        """This failing means candidate ordering performs an unsafe second stat."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watched = root / "watch"
            watched.mkdir()
            manager = DownloadManager([watched], root / "archive", root / "manifests")
            candidate = watched / "candidate.csv"
            baseline = {watched / "old.csv"}

            with (
                patch.object(manager, "_files", return_value={candidate: (3, 123)}),
                patch.object(manager, "verify_complete", return_value=True),
            ):
                found = manager.wait_for_new_download(
                    baseline,
                    timeout_seconds=1,
                    stable_seconds=0,
                )

            self.assertEqual(found, candidate)

    def test_verify_complete_treats_a_file_that_vanishes_before_first_stat_as_incomplete(self):
        """This failing means a disappearing file escapes the incomplete-file path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watched = root / "watch"
            watched.mkdir()
            target = watched / "vanishing.csv"
            target.write_text("data\n", encoding="utf-8")
            manager = DownloadManager([watched], root / "archive", root / "manifests")

            class _VanishingPath:
                def exists(self):
                    return True

                def is_file(self):
                    return True

                def stat(self):
                    raise OSError("file vanished")

            with patch(
                "hunnu_harness.downloads.manager._windows_io_path",
                return_value=_VanishingPath(),
            ):
                self.assertFalse(manager.verify_complete(target, stable_seconds=0))

    def test_wait_scans_once_before_timing_out_with_non_positive_timeout(self):
        """This failing means a non-positive timeout suppresses the only scan."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watched = root / "watch"
            watched.mkdir()
            manager = DownloadManager([watched], root / "archive", root / "manifests")

            with patch.object(manager, "_files", return_value={}) as files:
                with self.assertRaises(DownloadTimeout):
                    manager.wait_for_new_download(
                        {watched / "old.csv"},
                        timeout_seconds=0,
                        stable_seconds=0,
                    )

            files.assert_called_once_with()

    def test_wait_scans_again_after_the_last_sleep(self):
        """This failing means the final scan is skipped after a polling sleep."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watched = root / "watch"
            watched.mkdir()
            manager = DownloadManager([watched], root / "archive", root / "manifests")
            candidate = watched / "late.csv"

            with (
                patch.object(
                    manager,
                    "_files",
                    side_effect=[{}, {candidate: (3, 123)}],
                ),
                patch.object(manager, "verify_complete", return_value=True),
                patch(
                    "hunnu_harness.downloads.manager.time.monotonic",
                    side_effect=itertools.chain([100.0, 100.1], itertools.repeat(101.0)),
                ),
                patch("hunnu_harness.downloads.manager.time.sleep", return_value=None),
            ):
                found = manager.wait_for_new_download(
                    {watched / "old.csv"},
                    timeout_seconds=1,
                    stable_seconds=0,
                )

            self.assertEqual(found, candidate)


if __name__ == "__main__":
    unittest.main()
