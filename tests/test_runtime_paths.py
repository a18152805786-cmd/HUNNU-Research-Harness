import os
from pathlib import Path
import unittest

from hunnu_harness.downloads.manager import DownloadManager
from hunnu_harness.paths import (
    AUTHORIZED_DOWNLOADS_DIR,
    CORE_ROOT,
    LOGS_DIR,
    MANIFESTS_DIR,
    OUTPUT_ROOT,
    PLAYWRIGHT_OUTPUT_DIR,
    PROJECT_ROOT,
    RAW_DIR,
    RUNS_ROOT,
    SCREENSHOTS_DIR,
    STAGING_DIR,
    TEMP_DIR,
)


class RuntimePathTests(unittest.TestCase):
    def test_runtime_paths_follow_effective_output_root_and_environment_override(self):
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(PROJECT_ROOT, project_root)
        self.assertEqual(CORE_ROOT, project_root)
        configured_output_root = os.environ.get("HUNNU_HARNESS_OUTPUT_ROOT")
        if configured_output_root is not None:
            self.assertEqual(OUTPUT_ROOT, Path(configured_output_root).resolve())
        self.assertNotEqual(CORE_ROOT, OUTPUT_ROOT)
        self.assertEqual(RUNS_ROOT, OUTPUT_ROOT / "runs")
        self.assertEqual(STAGING_DIR, OUTPUT_ROOT / "staging")
        self.assertEqual(RAW_DIR, AUTHORIZED_DOWNLOADS_DIR)
        self.assertEqual(PLAYWRIGHT_OUTPUT_DIR, OUTPUT_ROOT / "staging" / "playwright-output")
        self.assertEqual(MANIFESTS_DIR, OUTPUT_ROOT / "manifests")
        self.assertEqual(LOGS_DIR, OUTPUT_ROOT / "logs")
        self.assertEqual(SCREENSHOTS_DIR, OUTPUT_ROOT / "screenshots")
        self.assertEqual(TEMP_DIR, OUTPUT_ROOT / "temp")

    def test_download_manager_defaults_to_output_runtime_paths(self):
        manager = DownloadManager()
        self.assertEqual(manager.watch_dirs, (STAGING_DIR,))
        self.assertEqual(manager.archive_root, RAW_DIR)
        self.assertEqual(manager.manifest_root, MANIFESTS_DIR)
