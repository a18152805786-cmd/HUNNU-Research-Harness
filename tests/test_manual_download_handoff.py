from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hunnu_harness.agent_entrypoint import AgentRequestRouter
from hunnu_harness.browser.manual_download_handoff import (
    ManualDownloadCandidateAmbiguous,
    ManualDownloadCandidateRejected,
    ManualDownloadHandoff,
    ManualDownloadHandoffTimeout,
    ManualDownloadScan,
)
from hunnu_harness.literature.adapters.base import SourceLayoutChanged, SourceUnavailable
from hunnu_harness.literature.adapters.oxfordacademic import OxfordAcademicAdapter
from hunnu_harness.literature.models import (
    AccessDecision,
    AccessType,
    LiteratureRecord,
    LiteratureRunResult,
    LiteratureSearchRequest,
    RunStatus,
)
from hunnu_harness.literature.normalization import sha256_file
from hunnu_harness.literature.workflow import finalize_manual_download_handoff_acceptance
from hunnu_harness.paths import OUTPUT_ROOT, TEMP_DIR

from literature_test_support import write_minimal_pdf, write_minimal_pdf_with_text


TITLE = "Double/debiased machine learning for treatment and structural parameters"
DOI = "10.1111/ectj.12097"
PDF_URL = "https://academic.oup.com/ectj/article-pdf/21/1/C1/27684918/ectj00c1.pdf"


def target_record() -> LiteratureRecord:
    return LiteratureRecord(
        paper_id="PDDML",
        title=TITLE,
        authors=("Victor Chernozhukov", "Denis Chetverikov"),
        year="2018",
        journal="The Econometrics Journal",
        volume="21",
        issue="1",
        pages_or_article_number="C1-C68",
        doi=DOI,
        source_database="OxfordAcademic",
        source_page="https://academic.oup.com/ectj/article/21/1/C1/5056401",
        stable_identifier="5056401",
        search_query=TITLE,
        target_identity_confirmed=True,
    )


def purchased_access() -> AccessDecision:
    return AccessDecision(
        full_text_accessible=True,
        access_type=AccessType.INSTITUTIONAL_AUTHENTICATED,
        authorized_access=True,
        status=RunStatus.SUCCESS,
        reason="Purchased: enabled official Oxford main-article PDF action confirmed",
        download_url=PDF_URL,
        download_locator="a[href*='/article-pdf/']",
    )


def request() -> LiteratureSearchRequest:
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": f"Download exact Oxford paper: {TITLE}",
            "ResearchQuestion": TITLE,
            "ExactTitles": [TITLE],
            "DOIs": [DOI],
            "MaxSearchResults": 1,
            "MaxResultsPerSource": 1,
            "MaxDownloads": 1,
            "MaxDownloadsPerRun": 1,
            "RequireFullText": True,
        }
    )


class _Browser:
    def __init__(self, downloads_dir: Path) -> None:
        self.downloads_dir = downloads_dir


class ManualDownloadSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def _handoff(self, root: Path) -> ManualDownloadHandoff:
        watch = root / "watch"
        watch.mkdir()
        return ManualDownloadHandoff(
            watch,
            root / "staging",
            allow_outside_output_for_tests=True,
        )

    def test_before_snapshot_records_filename_size_and_mtime(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            existing = write_minimal_pdf(handoff.watch_directory / "old.pdf")
            state = handoff.arm(hash_existing_pdfs=True)
            item = state.before.by_name["old.pdf"]
            self.assertEqual(item.size, existing.stat().st_size)
            self.assertEqual(item.mtime_ns, existing.stat().st_mtime_ns)
            self.assertEqual(item.sha256, sha256_file(existing))

    def test_old_pdf_is_not_a_new_candidate(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            write_minimal_pdf(handoff.watch_directory / "old.pdf")
            state = handoff.arm()
            self.assertEqual(handoff.scan(state).new_download_candidates, 0)

    def test_single_new_pdf_is_detected_after_handoff(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            state = handoff.arm()
            created = write_minimal_pdf(handoff.watch_directory / "new.pdf")
            scan = handoff.scan(state)
            self.assertEqual(scan.completed_candidates, (created,))
            self.assertEqual(scan.new_download_candidates, 1)

    def test_changed_existing_pdf_is_detected(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            existing = write_minimal_pdf(handoff.watch_directory / "same-name.pdf")
            state = handoff.arm()
            existing.write_bytes(existing.read_bytes() + b"\n")
            changed_ns = time.time_ns() + 1_000_000
            os.utime(existing, ns=(changed_ns, changed_ns))
            self.assertEqual(handoff.scan(state).completed_candidates, (existing,))

    def test_snapshot_skips_a_file_that_vanishes_before_stat(self) -> None:
        """This failing means a disappearing snapshot entry crashes the handoff."""
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            vanished = write_minimal_pdf(handoff.watch_directory / "vanishing.pdf")

            class _DirectoryIO:
                def iterdir(self):
                    return (vanished,)

            class _VanishingFileIO:
                def is_file(self):
                    return True

                def stat(self):
                    raise OSError("file vanished")

            def io_path(value):
                path = Path(value)
                if path == handoff.watch_directory:
                    return _DirectoryIO()
                if path == vanished:
                    return _VanishingFileIO()
                return path

            with patch(
                "hunnu_harness.browser.manual_download_handoff._windows_io_path",
                side_effect=io_path,
            ):
                snapshot = handoff.snapshot()

            self.assertEqual(snapshot.files, ())

    async def test_unreadable_watch_directory_oserror_surfaces_promptly(self) -> None:
        """This failing means an unreadable watch directory is misreported as a timeout."""
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            state = handoff.arm()

            class _UnreadableDirectory:
                def iterdir(self):
                    raise OSError("watch directory cannot be listed")

            with (
                patch(
                    "hunnu_harness.browser.manual_download_handoff._windows_io_path",
                    return_value=_UnreadableDirectory(),
                ),
                self.assertRaisesRegex(OSError, "watch directory cannot be listed"),
            ):
                await handoff.wait_for_completed_download(
                    state,
                    timeout_seconds=1,
                    poll_interval_seconds=1,
                )

    async def test_validation_stat_race_forgets_stability_and_keeps_polling(self) -> None:
        """This failing means a vanished validation candidate is rejected or accepted too soon."""
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            state = handoff.arm()
            candidate = handoff.watch_directory / "recovering.pdf"
            scan = ManualDownloadScan(
                after=state.before,
                completed_candidates=(candidate,),
                temporary_candidates=(),
            )

            class _CandidateIO:
                def __init__(self) -> None:
                    self.stat_calls = 0

                def stat(self):
                    self.stat_calls += 1
                    if self.stat_calls == 3:
                        raise OSError("file vanished")
                    return SimpleNamespace(st_size=5, st_mtime_ns=7)

            candidate_io = _CandidateIO()

            def has_header(_path: Path) -> bool:
                return candidate_io.stat_calls >= 6

            async def no_sleep(_delay: float) -> None:
                return None

            with (
                patch.object(handoff, "scan", return_value=scan),
                patch.object(handoff, "_has_pdf_header", side_effect=has_header),
                patch(
                    "hunnu_harness.browser.manual_download_handoff._windows_io_path",
                    return_value=candidate_io,
                ),
                patch("hunnu_harness.browser.manual_download_handoff.asyncio.sleep", new=no_sleep),
            ):
                detection = await handoff.wait_for_completed_download(
                    state,
                    timeout_seconds=0.2,
                    poll_interval_seconds=0.01,
                )

            self.assertEqual(detection.source_path, candidate)

    async def test_crdownload_is_not_accepted_as_completed(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            state = handoff.arm()
            (handoff.watch_directory / "paper.pdf.crdownload").write_bytes(b"%PDF-partial")
            with self.assertRaises(ManualDownloadHandoffTimeout):
                await handoff.wait_for_completed_download(
                    state,
                    timeout_seconds=0.04,
                    poll_interval_seconds=0.01,
                )

    async def test_file_size_must_be_stable_before_acceptance(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            state = handoff.arm()
            created = write_minimal_pdf(handoff.watch_directory / "stable.pdf")
            detection = await handoff.wait_for_completed_download(
                state,
                timeout_seconds=0.2,
                poll_interval_seconds=0.01,
                stable_observations=2,
            )
            self.assertEqual(detection.source_path, created)
            self.assertTrue(detection.file_size_stable)

    async def test_multiple_new_valid_pdfs_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            state = handoff.arm()
            write_minimal_pdf(handoff.watch_directory / "one.pdf")
            write_minimal_pdf(handoff.watch_directory / "two.pdf")
            with self.assertRaises(ManualDownloadCandidateAmbiguous):
                await handoff.wait_for_completed_download(
                    state,
                    timeout_seconds=0.2,
                    poll_interval_seconds=0.01,
                )

    async def test_invalid_pdf_header_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            state = handoff.arm()
            (handoff.watch_directory / "error.pdf").write_text("<html>not a pdf</html>")
            with self.assertRaises(ManualDownloadCandidateRejected):
                await handoff.wait_for_completed_download(
                    state,
                    timeout_seconds=0.2,
                    poll_interval_seconds=0.01,
                )

    def test_non_pdf_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            state = handoff.arm()
            (handoff.watch_directory / "unrelated.txt").write_text("unrelated")
            self.assertEqual(handoff.scan(state).new_download_candidates, 0)

    def test_state_round_trip_contains_no_browser_or_network_secrets(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            handoff = self._handoff(root)
            state = handoff.arm()
            state_path = handoff.save_state(state, root / "handoff.json")
            loaded = handoff.load_state(state_path)
            serialized = state_path.read_text(encoding="utf-8")
            payload = json.loads(serialized)
            self.assertEqual(loaded.watch_directory, handoff.watch_directory)
            self.assertNotIn("authorization", serialized.casefold())
            self.assertNotIn("cookie", serialized.casefold())
            self.assertFalse(payload["SignedURLReplay"])
            self.assertFalse(payload["AuthenticatedRequestReplay"])
            self.assertTrue(payload["ACTION_REQUIRED_USER_DOWNLOAD"])

    async def test_staging_copy_preserves_original_and_sha256(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            handoff = self._handoff(Path(temporary))
            state = handoff.arm()
            original = write_minimal_pdf(handoff.watch_directory / "manual.pdf")
            detection = await handoff.wait_for_completed_download(
                state,
                timeout_seconds=0.2,
                poll_interval_seconds=0.01,
            )
            result = handoff.stage(detection, controlled_filename="PDDML.pdf")
            self.assertTrue(original.exists())
            self.assertTrue(result.staged_path.exists())
            self.assertNotEqual(original, result.staged_path)
            self.assertEqual(sha256_file(original), sha256_file(result.staged_path))
            self.assertTrue(result.original_manual_download_preserved)

    def test_controlled_staging_defaults_to_output_root_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch = root / "watch"
            watch.mkdir()
            if root.resolve().is_relative_to(OUTPUT_ROOT):
                self.skipTest("Temporary directory unexpectedly resides inside OutputRoot")
            with self.assertRaises(ValueError):
                ManualDownloadHandoff(watch, root / "staging")


class OxfordManualDownloadHandoffTests(unittest.IsolatedAsyncioTestCase):
    def _adapter(self, root: Path) -> OxfordAcademicAdapter:
        watch = root / "watch"
        watch.mkdir()
        return OxfordAcademicAdapter(
            _Browser(watch),
            allow_capture_outside_output_for_tests=True,
            capture_timeout_ms=20,
        )

    def test_oxford_handoff_arm_sets_distinct_user_download_state(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            adapter = self._adapter(Path(temporary))
            record = target_record()
            state = adapter.arm_manual_download_handoff(
                record,
                purchased_access(),
                pdf_viewer_opened=True,
            )
            self.assertTrue(state.as_dict()["ACTION_REQUIRED_USER_DOWNLOAD"])
            self.assertFalse(state.as_dict()["ACTION_REQUIRED_USER_LOGIN"])
            self.assertTrue(record.manual_download_handoff_armed)
            self.assertEqual(record.source_access_status, "Purchased")
            self.assertTrue(record.official_pdf_action_confirmed)

    def test_oxford_handoff_requires_an_open_pdf_viewer(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            adapter = self._adapter(Path(temporary))
            with self.assertRaisesRegex(SourceUnavailable, "PDFViewerOpened=false"):
                adapter.arm_manual_download_handoff(
                    target_record(),
                    purchased_access(),
                    pdf_viewer_opened=False,
                )

    async def test_correct_title_and_doi_complete_oxford_handoff(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            adapter = self._adapter(Path(temporary))
            record = target_record()
            adapter.arm_manual_download_handoff(record, purchased_access(), pdf_viewer_opened=True)
            original = write_minimal_pdf_with_text(
                adapter.browser.downloads_dir / "ectj00c1.pdf",
                f"{TITLE} Victor Chernozhukov DOI: {DOI}",
            )
            staged = await adapter.complete_manual_download_handoff(
                record,
                timeout_seconds=0.2,
                poll_interval_seconds=0.01,
            )
            self.assertTrue(original.exists())
            self.assertTrue(staged.exists())
            self.assertTrue(record.target_identity_confirmed)
            self.assertTrue(record.target_title_matched)
            self.assertTrue(record.target_doi_matched)
            self.assertTrue(record.manual_download_detected)
            self.assertTrue(record.human_download_action)

    async def test_wrong_pdf_identity_is_rejected_before_archive(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            adapter = self._adapter(Path(temporary))
            record = target_record()
            adapter.arm_manual_download_handoff(record, purchased_access(), pdf_viewer_opened=True)
            original = write_minimal_pdf_with_text(
                adapter.browser.downloads_dir / "wrong.pdf",
                "A completely different paper DOI: 10.9999/wrong",
            )
            with patch.object(OxfordAcademicAdapter, "_quarantine", side_effect=lambda path: path):
                with self.assertRaisesRegex(SourceLayoutChanged, "target identity"):
                    await adapter.complete_manual_download_handoff(
                        record,
                        timeout_seconds=0.2,
                        poll_interval_seconds=0.01,
                    )
            self.assertTrue(original.exists())
            self.assertFalse(record.target_identity_confirmed)


class ManualDownloadFinalizerAndAgentTests(unittest.TestCase):
    def _completed_record(self) -> LiteratureRecord:
        record = target_record()
        record.acquisition_method = "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF"
        record.source_host = "academic.oup.com"
        record.source_route = "HUNNU_GATEWAY_TO_OXFORD"
        record.institutional_route_used = True
        record.official_pdf_action_confirmed = True
        record.source_access_status = "Purchased"
        record.manual_download_required = True
        record.manual_download_handoff_armed = True
        record.manual_download_detected = True
        record.human_download_action = True
        record.original_manual_download_preserved = True
        record.download_initiation_mode = "USER_MANUAL_NATIVE_VIEWER_CLICK"
        record.file_finalization_mode = "HARNESS_AUTOMATIC"
        record.target_title_matched = True
        record.target_doi_matched = True
        return record

    def test_existing_finalizer_writes_archive_sha_and_sanitized_manual_manifest(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            staged = write_minimal_pdf_with_text(
                root / "PDDML.pdf",
                f"{TITLE} Victor Chernozhukov DOI: {DOI}",
            )
            result = finalize_manual_download_handoff_acceptance(
                request=request(),
                record=self._completed_record(),
                access=purchased_access(),
                staged_pdf=staged,
                run_root=root / "run",
                allow_outside_project_for_tests=True,
            )
            self.assertEqual(result.status, RunStatus.SUCCESS)
            self.assertEqual(len(result.downloads), 1)
            entry = result.downloads[0]
            manifest_path = root / "run" / "DOWNLOAD_MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_text = manifest_path.read_text(encoding="utf-8").casefold()
            self.assertTrue(Path(entry.local_path).exists())
            self.assertEqual(entry.sha256, sha256_file(staged))
            self.assertTrue((root / "run" / "SHA256SUMS.txt").read_text().strip())
            self.assertEqual(
                manifest["Downloads"][0]["AcquisitionMethod"],
                "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF",
            )
            self.assertEqual(
                manifest["Downloads"][0]["DownloadInitiationMode"],
                "USER_MANUAL_NATIVE_VIEWER_CLICK",
            )
            self.assertEqual(manifest["Downloads"][0]["FileFinalizationMode"], "HARNESS_AUTOMATIC")
            self.assertTrue(manifest["Downloads"][0]["OriginalManualDownloadPreserved"])
            self.assertFalse(manifest["Downloads"][0]["SignedURLPersisted"])
            self.assertFalse(manifest["Downloads"][0]["AuthorizationHeaderPersisted"])
            self.assertFalse(manifest["Downloads"][0]["CookiePersisted"])
            self.assertNotIn("?token=", manifest_text)
            self.assertNotIn("bearer ", manifest_text)

    def test_finalizer_rejects_identity_mismatch_without_archive(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            staged = write_minimal_pdf(root / "wrong.pdf")
            record = self._completed_record()
            record.target_identity_confirmed = False
            result = finalize_manual_download_handoff_acceptance(
                request=request(),
                record=record,
                access=purchased_access(),
                staged_pdf=staged,
                run_root=root / "run",
                allow_outside_project_for_tests=True,
            )
            self.assertEqual(result.status, RunStatus.DOWNLOAD_FAILED)
            self.assertEqual(result.downloads, [])
            self.assertEqual(list((root / "run" / "downloads" / "archive").iterdir()), [])

    def test_agent_oxford_route_advertises_manual_download_handoff(self) -> None:
        decision = AgentRequestRouter().route(
            {
                "TaskType": "literature_search",
                "Query": TITLE,
                "PreferredSources": "OxfordAcademic",
                "MaxCandidates": 1,
                "MaxDownloads": 1,
            }
        )
        payload = decision.as_dict()
        self.assertTrue(payload["ManualDownloadHandoffSupported"])
        self.assertEqual(payload["ManualDownloadRequiredStatus"], "ACTION_REQUIRED_USER_DOWNLOAD")

    def test_agent_describes_user_download_without_login_gate(self) -> None:
        result = LiteratureRunResult(
            status=RunStatus.ACTION_REQUIRED_USER_DOWNLOAD,
            records=[target_record()],
            downloads=[],
            action_required_reason="native Viewer download click required",
        )
        payload = AgentRequestRouter.describe_literature_result(result)
        self.assertTrue(payload["ACTION_REQUIRED_USER_DOWNLOAD"])
        self.assertFalse(payload["ACTION_REQUIRED_USER_LOGIN"])
        self.assertTrue(payload["BrowserReadyForManualDownload"])
        self.assertFalse(payload["NativePDFViewerAutomation"])
        self.assertFalse(payload["SignedURLReplay"])


if __name__ == "__main__":
    unittest.main()
