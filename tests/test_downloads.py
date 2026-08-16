import tempfile
import time
import unittest
from pathlib import Path

from hunnu_harness.downloads.manager import DownloadManager
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


if __name__ == "__main__":
    unittest.main()
