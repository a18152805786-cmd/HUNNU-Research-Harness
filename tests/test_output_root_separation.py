import shutil
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.cli import build_parser as build_harness_parser
from hunnu_harness.downloads.manager import DownloadManager
from hunnu_harness.literature.artifacts import (
    CNKI_UPGRADE_RUN_ROOT,
    INSTITUTIONAL_UPGRADE_RUN_ROOT,
    UPGRADE_RUN_ROOT,
    LiteratureArtifactWriter,
)
from hunnu_harness.literature.cli import build_parser as build_literature_parser
from hunnu_harness.literature.downloads import LiteratureDownloadManager
from hunnu_harness.models import DownloadRequest
from hunnu_harness.paths import (
    AUDIT_DIR,
    AUTHORIZED_DOWNLOADS_DIR,
    CORE_ROOT,
    DEFAULT_OUTPUT_ROOT,
    DOWNLOADS_ROOT,
    LOGS_DIR,
    LIBRARY_CATALOG_CSV,
    LIBRARY_CATALOG_JSONL,
    LIBRARY_CATALOG_DIR,
    LIBRARY_IMPORT_STAGING_DIR,
    LIBRARY_NOTES_DIR,
    LIBRARY_PAPERS_DIR,
    LIBRARY_ROOT,
    MANIFESTS_DIR,
    MIGRATION_DIR,
    OUTPUT_ROOT,
    PLAYWRIGHT_OUTPUT_DIR,
    QUARANTINE_DIR,
    REVIEW_DIR,
    RUNS_ROOT,
    SCREENSHOTS_DIR,
    STAGING_DIR,
    TEMP_DIR,
    V023_RUN_ROOT,
    is_within,
    resolve_relocated_path,
)
from hunnu_harness.relocation import (
    RelocationClassification,
    classify_core_path,
    record_verified_relocation,
    sha256_file,
    verify_relocated_file,
)


class OutputRootPathTests(unittest.TestCase):
    def test_core_output_isolation_and_default_derivation_contracts_remain_intact(self):
        self.assertNotEqual(CORE_ROOT, OUTPUT_ROOT)
        self.assertFalse(is_within(OUTPUT_ROOT, CORE_ROOT))
        self.assertFalse(is_within(CORE_ROOT, OUTPUT_ROOT))
        self.assertEqual(
            DEFAULT_OUTPUT_ROOT,
            CORE_ROOT.with_name(f"{CORE_ROOT.name}-Output"),
        )

    def test_every_runtime_root_is_derived_from_output_root(self):
        runtime_roots = (
            RUNS_ROOT,
            DOWNLOADS_ROOT,
            AUTHORIZED_DOWNLOADS_DIR,
            STAGING_DIR,
            PLAYWRIGHT_OUTPUT_DIR,
            AUDIT_DIR,
            MANIFESTS_DIR,
            LOGS_DIR,
            SCREENSHOTS_DIR,
            REVIEW_DIR,
            QUARANTINE_DIR,
            TEMP_DIR,
            MIGRATION_DIR,
            LIBRARY_ROOT,
            LIBRARY_PAPERS_DIR,
            LIBRARY_NOTES_DIR,
            LIBRARY_CATALOG_DIR,
            LIBRARY_CATALOG_JSONL,
            LIBRARY_CATALOG_CSV,
            LIBRARY_IMPORT_STAGING_DIR,
            V023_RUN_ROOT,
        )
        self.assertTrue(all(is_within(path, OUTPUT_ROOT) for path in runtime_roots))
        self.assertTrue(all(not is_within(path, CORE_ROOT) for path in runtime_roots))

    def test_historical_runtime_path_resolves_without_rewriting_evidence(self):
        historical = CORE_ROOT / "runs" / "Harness_V022" / "DOWNLOAD_MANIFEST.json"
        self.assertEqual(
            resolve_relocated_path(historical),
            OUTPUT_ROOT / "runs" / "Harness_V022" / "DOWNLOAD_MANIFEST.json",
        )
        self.assertEqual(
            resolve_relocated_path(CORE_ROOT / "V21_value_blind_preflight.log"),
            LOGS_DIR / "legacy-core-root" / "V21_value_blind_preflight.log",
        )
        self.assertEqual(
            resolve_relocated_path(CORE_ROOT / "PATH_MIGRATION_AUDIT.md"),
            MIGRATION_DIR / "legacy-core-root" / "PATH_MIGRATION_AUDIT.md",
        )

    def test_core_source_path_is_not_relocated(self):
        source = CORE_ROOT / "src" / "hunnu_harness" / "paths.py"
        self.assertEqual(resolve_relocated_path(source), source.resolve())

    def test_browser_cli_default_download_is_output_staging(self):
        args = build_harness_parser().parse_args(["browser-start"])
        self.assertEqual(args.downloads, STAGING_DIR)


class LiteratureOutputRoutingTests(unittest.TestCase):
    def test_all_literature_source_defaults_use_output_runs(self):
        parser = build_literature_parser()
        cases = (
            (["live-sciencedirect", "--title", "x"], UPGRADE_RUN_ROOT),
            (["live-springerlink", "--title", "x"], UPGRADE_RUN_ROOT),
            (["live-cnki", "--title", "x"], CNKI_UPGRADE_RUN_ROOT),
        )
        for argv, expected in cases:
            with self.subTest(command=argv[0]):
                args = parser.parse_args(argv)
                self.assertEqual(args.run_root, expected)
                self.assertTrue(is_within(args.run_root, OUTPUT_ROOT))

    def test_institutional_resolver_run_default_uses_output_root(self):
        self.assertTrue(is_within(INSTITUTIONAL_UPGRADE_RUN_ROOT, RUNS_ROOT))
        self.assertFalse(is_within(INSTITUTIONAL_UPGRADE_RUN_ROOT, CORE_ROOT))

    def test_literature_writer_rejects_core_runtime_destination(self):
        with self.assertRaises(ValueError):
            LiteratureArtifactWriter(CORE_ROOT / "runs" / "forbidden-core-run")

    def test_literature_download_manager_rejects_core_destination(self):
        with self.assertRaises(ValueError):
            LiteratureDownloadManager(CORE_ROOT / "downloads" / "forbidden")

    def test_literature_writer_emits_only_under_output_root(self):
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="v023-lit-", dir=TEMP_DIR) as tmp:
            writer = LiteratureArtifactWriter(Path(tmp) / "run")
            self.assertTrue(is_within(writer.run_root, OUTPUT_ROOT))
            self.assertTrue(is_within(writer.download_manifest_path, OUTPUT_ROOT))
            self.assertTrue(is_within(writer.institutional_route_provenance_path, OUTPUT_ROOT))


class DataAcquisitionOutputRoutingTests(unittest.TestCase):
    def test_v01_download_manager_defaults_use_output_root(self):
        manager = DownloadManager()
        self.assertTrue(all(is_within(path, OUTPUT_ROOT) for path in manager.watch_dirs))
        self.assertTrue(is_within(manager.archive_root, OUTPUT_ROOT))
        self.assertTrue(is_within(manager.manifest_root, OUTPUT_ROOT))

    def test_authorized_mock_download_archives_under_output_root(self):
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="v023-v01-", dir=TEMP_DIR) as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            source = staging / "authorized.csv"
            source.write_text("firm,year\n1,2026\n", encoding="utf-8")
            manager = DownloadManager(
                [staging],
                root / "downloads" / "authorized",
                root / "manifests",
            )
            record = manager.archive_file(
                source,
                DownloadRequest(database="CNRDS", module="CNFS", table="fixture"),
                read_only=False,
            )
            self.assertTrue(is_within(Path(record.archived_path), OUTPUT_ROOT))
            self.assertTrue(is_within(manager.manifest_root, OUTPUT_ROOT))


class RelocationProtectionTests(unittest.TestCase):
    def test_core_assets_are_never_classified_as_runtime_output(self):
        protected = (
            CORE_ROOT / "AGENTS.md",
            CORE_ROOT / "src" / "hunnu_harness" / "paths.py",
            CORE_ROOT / "tests" / "test_output_root_separation.py",
            CORE_ROOT / "tests" / "fixtures" / "literature" / "cnki_search.html",
            CORE_ROOT / "config" / "config.example.yaml",
        )
        self.assertTrue(
            all(classify_core_path(path) == RelocationClassification.CORE for path in protected)
        )

    def test_runtime_and_historical_outputs_have_explicit_classification(self):
        self.assertEqual(
            classify_core_path(CORE_ROOT / "runs" / "Harness_V022" / "RUN_AUDIT.md"),
            RelocationClassification.HISTORICAL_FORMAL_ARTIFACT,
        )
        self.assertEqual(
            classify_core_path(CORE_ROOT / "downloads" / "staging" / "sample.bin"),
            RelocationClassification.GENERATED_OUTPUT,
        )
        self.assertEqual(
            classify_core_path(CORE_ROOT / "V21_value_blind_preflight.log"),
            RelocationClassification.GENERATED_OUTPUT,
        )

    def test_unknown_core_item_stays_uncertain(self):
        self.assertEqual(
            classify_core_path(CORE_ROOT / "user-created-unknown.bin"),
            RelocationClassification.UNCERTAIN,
        )

    def test_relocation_integrity_requires_identical_sha256(self):
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="v023-hash-", dir=TEMP_DIR) as tmp:
            root = Path(tmp)
            before = root / "before.bin"
            after = root / "after.bin"
            before.write_bytes(b"historical artifact bytes\n")
            digest = sha256_file(before)
            shutil.copy2(before, after)
            self.assertTrue(verify_relocated_file(digest, after))
            record = record_verified_relocation(
                before,
                after,
                classification=RelocationClassification.HISTORICAL_FORMAL_ARTIFACT,
                sha256_before=digest,
                size=before.stat().st_size,
            )
            self.assertFalse(record.content_changed)
            self.assertEqual(record.move_status, "MOVED_VERIFIED")


if __name__ == "__main__":
    unittest.main()
