from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hunnu_harness.browser.manual_download_handoff import ManualDownloadCandidateAmbiguous, ManualDownloadHandoffTimeout
from hunnu_harness.literature.adapters.base import SourceUnavailable, SourceUserDownloadRequired
from hunnu_harness.literature.adapters.oxfordacademic import OxfordAcademicAdapter
from hunnu_harness.literature.manual_resume import ManualResumeError, get_handoff_receipt, resume_oxford_manual_handoff, save_oxford_manual_handoff
from hunnu_harness.literature.models import AccessDecision, AccessType, LiteratureRecord, LiteratureSearchRequest, RunStatus
from hunnu_harness.literature.normalization import sha256_file, stable_paper_id
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow
from hunnu_harness.paths import TEMP_DIR
from literature_test_support import write_minimal_pdf_with_text


TITLE = "Identity locked empirical research with machine learning methods"
DOI = "10.1234/manual-resume"


class OxfordManualResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.watch = self.root / "explicit-native-downloads"
        self.watch.mkdir()
        self.staging = self.run / "downloads" / "staging"
        self.staging.mkdir(parents=True)
        self.browser = SimpleNamespace(downloads_dir=self.staging)
        self.adapter = OxfordAcademicAdapter(self.browser, manual_download_watch_directory=self.watch)
        self.record = LiteratureRecord(paper_id=stable_paper_id(doi=DOI), title=TITLE, doi=DOI, authors=("Ada Researcher",), year="2020", journal="Example Journal", source_database="OxfordAcademic", source_page="https://academic.oup.com/ectj/article/21/1/C1/5056401", stable_identifier="5056401", search_query=DOI, target_identity_confirmed=True)
        self.access = AccessDecision(True, AccessType.OPEN_ACCESS, True, RunStatus.SUCCESS, "OpenAccess: official main article PDF", download_url="https://academic.oup.com/ectj/article-pdf/21/1/C1/12345/example.pdf?signature=NEVER_PERSIST", download_locator="a[href*='/article-pdf/']")
        self.request = LiteratureSearchRequest(original_research_request=f"Acquire {DOI}", dois=(DOI,), max_search_results=1, max_results_per_source=1, max_downloads=1, max_downloads_per_run=1)
        self.state = self.adapter.arm_manual_download_handoff(self.record, self.access, pdf_viewer_opened=True)

    def save(self):
        return save_oxford_manual_handoff(request=self.request, record=self.record, access=self.access, state=self.state, run_root=self.run)

    def pdf(self, filename="paper.pdf", text=None):
        return write_minimal_pdf_with_text(self.watch / filename, text or f"{TITLE} Ada Researcher DOI: {DOI}")

    async def resume(self, receipt, **kwargs):
        return await resume_oxford_manual_handoff(state_path=receipt.state_path, expected_sha256=receipt.state_sha256, timeout_seconds=.15, poll_interval_seconds=.01, **kwargs)

    def test_explicit_watch_and_controlled_staging_are_distinct(self):
        self.assertEqual(self.state.watch_directory, self.watch)
        self.assertEqual(self.state.staging_directory, self.staging / "manual-handoff-staging")
        self.assertNotEqual(self.state.watch_directory, self.browser.downloads_dir)

    def test_missing_native_watch_is_not_guessed_from_run_staging(self):
        with patch.dict(os.environ, {}, clear=True):
            adapter = OxfordAcademicAdapter(self.browser)
        with self.assertRaisesRegex(SourceUnavailable, "WatchDirectoryUnconfigured"):
            adapter.arm_manual_download_handoff(self.record, self.access, pdf_viewer_opened=True)

    def test_state_is_public_hash_bound_and_never_overwritten(self):
        receipt = self.save()
        payload = json.loads(receipt.state_path.read_text(encoding="utf-8"))
        text = receipt.state_path.read_text(encoding="utf-8")
        self.assertNotIn("NEVER_PERSIST", text)
        self.assertNotIn("article-pdf", text)
        self.assertNotIn("download_locator", text)
        self.assertNotIn("navigation_url", text)
        self.assertEqual(payload["Handoff"]["ArmedAtNS"], self.state.armed_at_ns)
        self.assertEqual(receipt.state_sha256, sha256_file(receipt.state_path))
        self.assertEqual(get_handoff_receipt(self.run), receipt)
        with self.assertRaises(FileExistsError):
            self.save()

    def test_exact_title_only_and_combined_requests_must_bind_the_record_title(self):
        for dois in ((), (DOI,)):
            self.request = replace(self.request, dois=dois, exact_titles=("An unrelated exact requested title",))
            with self.subTest(dois=dois), self.assertRaisesRegex(ManualResumeError, "requested exact title"):
                self.save()
        self.assertFalse((self.run / "manual-download-handoff").exists())

    async def test_local_resume_archives_manifests_library_and_preserves_original(self):
        receipt = self.save()
        original = self.pdf()
        original_hash = sha256_file(original)
        with patch.object(OxfordAcademicAdapter, "__init__", side_effect=AssertionError("No browser or new adapter on resume")):
            result = await self.resume(receipt)
        self.assertEqual(result.status, RunStatus.SUCCESS)
        self.assertEqual(len(result.downloads), 1)
        entry = result.downloads[0]
        self.assertEqual(entry.sha256, original_hash)
        self.assertEqual(sha256_file(original), original_hash)
        self.assertEqual(entry.acquisition_method, "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF")
        self.assertTrue(entry.manual_download_detected)
        self.assertTrue(entry.human_download_action)
        self.assertFalse(entry.oxford_unattended_download_ready)
        self.assertNotEqual(entry.library_managed_path, "unknown")
        self.assertTrue((self.run / "manual-resume" / "DOWNLOAD_MANIFEST.json").is_file())
        self.assertTrue((self.run / "manual-resume" / "SHA256SUMS.txt").read_text().strip())
        proof = json.loads((receipt.state_path.parent / "RESULT.json").read_text())
        self.assertEqual(proof["StateSHA256"], receipt.state_sha256)
        self.assertEqual(proof["SourceSHA256"], original_hash)
        self.assertEqual(proof["StagedSHA256"], original_hash)
        with self.assertRaisesRegex(ManualResumeError, "already finalized"):
            await self.resume(receipt)

    async def test_unchanged_preexisting_pdf_is_not_claimed(self):
        self.pdf()
        self.state = self.adapter.arm_manual_download_handoff(self.record, self.access, pdf_viewer_opened=True)
        receipt = self.save()
        with self.assertRaises(ManualDownloadHandoffTimeout):
            await self.resume(receipt)
        self.assertFalse((self.run / "manual-resume").exists())
        self.assertFalse((receipt.state_path.parent / "RESUME.lock").exists())

    async def test_wrong_identity_is_retained_and_cannot_be_retried_for_another_file(self):
        receipt = self.save()
        original = self.pdf(text="A different article DOI: 10.9999/other")
        with self.assertRaisesRegex(ManualResumeError, "local DOI and title"):
            await self.resume(receipt)
        self.assertTrue(original.is_file())
        self.assertTrue((self.state.staging_directory / f"{self.record.paper_id}.pdf").is_file())
        self.assertFalse((self.run / "manual-resume").exists())
        with self.assertRaisesRegex(ManualResumeError, "already finalized"):
            await self.resume(receipt)

    async def test_resume_rejects_reference_target_doi_despite_title_presence(self):
        receipt = self.save()
        original = self.pdf(text=f"{TITLE}\nAda Researcher\nDOI: 10.9999/other\nReferences\nCited target DOI: {DOI}")
        digest = sha256_file(original)
        with self.assertRaisesRegex(ManualResumeError, "local DOI and title"):
            await self.resume(receipt)
        self.assertEqual(sha256_file(original), digest)
        self.assertFalse((self.run / "manual-resume").exists())
        self.assertTrue((receipt.state_path.parent / "RESULT.json").is_file())

    async def test_two_new_pdfs_are_ambiguous(self):
        receipt = self.save()
        self.pdf("one.pdf")
        self.pdf("two.pdf")
        with self.assertRaises(ManualDownloadCandidateAmbiguous):
            await self.resume(receipt)
        self.assertFalse((self.run / "manual-resume").exists())

    async def test_temporary_file_blocks_finalization_and_timeout_can_resume_without_new_snapshot(self):
        receipt = self.save()
        self.pdf()
        temporary = self.watch / "paper.pdf.crdownload"
        temporary.write_bytes(b"partial")
        with self.assertRaises(ManualDownloadHandoffTimeout):
            await self.resume(receipt)
        self.assertFalse((receipt.state_path.parent / "RESULT.json").exists())
        temporary.unlink()
        result = await self.resume(receipt)
        self.assertEqual(result.status, RunStatus.SUCCESS)

    async def test_one_valid_and_one_invalid_pdf_are_still_ambiguous(self):
        receipt = self.save()
        self.pdf()
        (self.watch / "invalid.pdf").write_bytes(b"not a PDF")
        with self.assertRaisesRegex(ManualResumeError, "Exactly one"):
            await self.resume(receipt)

    async def test_state_tampering_and_copied_state_cannot_select_another_run(self):
        receipt = self.save()
        self.pdf()
        with self.assertRaisesRegex(ManualResumeError, "SHA256 mismatch"):
            await resume_oxford_manual_handoff(state_path=receipt.state_path, expected_sha256="0" * 64)
        copied = self.root / "copied.json"
        copied.write_bytes(receipt.state_path.read_bytes())
        with self.assertRaisesRegex(ManualResumeError, "not bound"):
            await resume_oxford_manual_handoff(state_path=copied, expected_sha256=receipt.state_sha256)

    async def test_changed_authorization_or_cross_run_staging_is_rejected_even_with_new_hash(self):
        receipt = self.save()
        data = json.loads(receipt.state_path.read_text())
        for mutation in ("authorization", "staging", "snapshot-time", "wrong-doi", "wrong-stable-id"):
            payload = json.loads(json.dumps(data))
            if mutation == "authorization":
                payload["Authorization"]["AuthorizedAccess"] = False
            elif mutation == "staging":
                payload["Handoff"]["ControlledStagingDirectory"] = str(self.root / "another-run")
            elif mutation == "snapshot-time":
                payload["Handoff"]["DownloadDirectorySnapshotBefore"]["CapturedAtNS"] = payload["Handoff"]["ArmedAtNS"] + 1
            elif mutation == "wrong-doi":
                payload["Record"]["doi"] = "10.9999/other"
            else:
                payload["Record"]["stable_identifier"] = "9"
            raw = json.dumps(payload).encode()
            receipt.state_path.write_bytes(raw)
            with self.subTest(mutation=mutation), self.assertRaises(ManualResumeError):
                await resume_oxford_manual_handoff(state_path=receipt.state_path, expected_sha256=hashlib.sha256(raw).hexdigest())

    async def test_existing_lock_and_partial_output_refuse_reentry(self):
        receipt = self.save()
        lock = receipt.state_path.parent / "RESUME.lock"
        lock.write_bytes(b"existing")
        with self.assertRaisesRegex(ManualResumeError, "active or interrupted"):
            await self.resume(receipt)
        self.assertEqual(lock.read_bytes(), b"existing")
        lock.unlink()
        (self.run / "manual-resume").mkdir()
        with self.assertRaisesRegex(ManualResumeError, "retained partial"):
            await self.resume(receipt)

    async def test_state_link_is_refused_before_reading(self):
        receipt = self.save()
        link = self.root / "state-link.json"
        try:
            link.symlink_to(receipt.state_path)
        except OSError as exc:
            self.skipTest(f"Local symlink creation unavailable: {exc}")
        with self.assertRaisesRegex(ManualResumeError, "links or junctions"):
            await resume_oxford_manual_handoff(state_path=link, expected_sha256=receipt.state_sha256)

    def test_symbolic_watch_directory_is_not_resolved_into_a_silent_alternative(self):
        link = self.root / "watch-link"
        try:
            link.symlink_to(self.watch, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Local symlink creation unavailable: {exc}")
        adapter = OxfordAcademicAdapter(self.browser, manual_download_watch_directory=link)
        with self.assertRaisesRegex(Exception, "links or junctions"):
            adapter.arm_manual_download_handoff(self.record, self.access, pdf_viewer_opened=True)

    async def test_workflow_persists_real_snapshot_before_returning_user_gate(self):
        with patch.object(self.adapter, "search", AsyncMock(return_value=[self.record])), patch.object(self.adapter, "open_result", AsyncMock()), patch.object(self.adapter, "extract_metadata", AsyncMock(return_value=self.record)), patch.object(self.adapter, "current_target_matches", AsyncMock(return_value=True)), patch.object(self.adapter, "check_fulltext_access", AsyncMock(return_value=self.access)), patch.object(self.adapter, "download_fulltext", AsyncMock(side_effect=SourceUserDownloadRequired("Download manually after receipt", handoff_state=self.state))):
            workflow = LiteratureAcquisitionWorkflow(self.adapter, run_root=self.run, human_like_delay_seconds=0, search_pacer=SimpleNamespace(pace=lambda **kwargs: None))
            result = await workflow.run(self.request)
        self.assertEqual(result.status, RunStatus.ACTION_REQUIRED_USER_DOWNLOAD)
        receipt = get_handoff_receipt(self.run)
        self.assertIsNotNone(receipt)
        self.assertIn(receipt.state_sha256, result.action_required_reason)
        self.assertEqual(result.manual_download_receipt, receipt.as_dict())
        self.assertFalse((self.run / "manual-resume").exists())

    async def test_workflow_persistence_failure_does_not_claim_armed_or_invite_click(self):
        with patch.object(self.adapter, "search", AsyncMock(return_value=[self.record])), patch.object(self.adapter, "open_result", AsyncMock()), patch.object(self.adapter, "extract_metadata", AsyncMock(return_value=self.record)), patch.object(self.adapter, "current_target_matches", AsyncMock(return_value=True)), patch.object(self.adapter, "check_fulltext_access", AsyncMock(return_value=self.access)), patch.object(self.adapter, "download_fulltext", AsyncMock(side_effect=SourceUserDownloadRequired("Viewer requires manual download", handoff_state=self.state))), patch("hunnu_harness.literature.manual_resume.save_oxford_manual_handoff", side_effect=PermissionError("test output refused")):
            workflow = LiteratureAcquisitionWorkflow(self.adapter, run_root=self.run, human_like_delay_seconds=0, search_pacer=SimpleNamespace(pace=lambda **kwargs: None))
            result = await workflow.run(self.request)
        self.assertEqual(result.status, RunStatus.ACTION_REQUIRED_USER_DOWNLOAD)
        self.assertFalse(self.record.manual_download_handoff_armed)
        self.assertIn("do not click Download", result.action_required_reason)
        self.assertIn("ManualDownloadHandoffPersisted=false", result.action_required_reason)
        self.assertIsNone(get_handoff_receipt(self.run))
        self.assertIsNone(result.manual_download_receipt)

    async def test_old_durable_receipt_is_not_returned_as_this_failed_persistence(self):
        old_receipt = self.save()
        original_state = old_receipt.state_path.read_bytes()
        with patch.object(self.adapter, "search", AsyncMock(return_value=[self.record])), patch.object(self.adapter, "open_result", AsyncMock()), patch.object(self.adapter, "extract_metadata", AsyncMock(return_value=self.record)), patch.object(self.adapter, "current_target_matches", AsyncMock(return_value=True)), patch.object(self.adapter, "check_fulltext_access", AsyncMock(return_value=self.access)), patch.object(self.adapter, "download_fulltext", AsyncMock(side_effect=SourceUserDownloadRequired("Viewer needs a new handoff", handoff_state=self.state))):
            workflow = LiteratureAcquisitionWorkflow(self.adapter, run_root=self.run, human_like_delay_seconds=0, search_pacer=SimpleNamespace(pace=lambda **kwargs: None))
            result = await workflow.run(self.request)
        self.assertEqual(result.status, RunStatus.ACTION_REQUIRED_USER_DOWNLOAD)
        self.assertIsNone(result.manual_download_receipt)
        self.assertFalse(self.record.manual_download_handoff_armed)
        self.assertEqual(old_receipt.state_path.read_bytes(), original_state)
        self.assertEqual(get_handoff_receipt(self.run), old_receipt)

    async def test_unconfigured_capture_fallback_records_fetch_and_reports_not_armed(self):
        with patch.dict(os.environ, {}, clear=True):
            adapter = OxfordAcademicAdapter(SimpleNamespace(downloads_dir=self.staging))
        adapter.browser.execute = AsyncMock(side_effect=[RuntimeError("No file emitted"), SimpleNamespace(url="https://watermark02.silverchair.com/served.pdf", page_inventory=())])
        ticket = SimpleNamespace(record_outcome=unittest.mock.Mock())
        with patch.object(adapter, "authorize_publisher_fetch", return_value=ticket):
            with self.assertRaises(SourceUserDownloadRequired) as caught:
                await adapter.download_fulltext(self.record, self.access)
        self.assertIsNone(caught.exception.handoff_state)
        self.assertIn("ManualDownloadHandoffArmed=false", str(caught.exception))
        ticket.record_outcome.assert_called_once()


if __name__ == "__main__":
    unittest.main()
