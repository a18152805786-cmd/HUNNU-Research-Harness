from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.agent_entrypoint import AgentRequestRouter
from hunnu_harness.browser.authorized_file_capture import (
    AcquisitionMethod,
    AuthorizedFileCaptureResult,
    BrowserAuthorizedFileCapture,
)
from hunnu_harness.browser.pdf_preferences import (
    PDF_DIRECT_DOWNLOAD_PREFERENCE,
    ResearchChromePdfPreference,
    ResearchChromePreferenceError,
    ResearchChromeProfileInUse,
)
from hunnu_harness.cli import build_parser
from hunnu_harness.literature.adapters.oxfordacademic import (
    OxfordAcademicAdapter,
    OxfordPDFIdentityResult,
)
from hunnu_harness.literature.models import (
    AccessDecision,
    AccessType,
    DownloadManifestEntry,
    LiteratureRecord,
    LiteratureRunResult,
    LiteratureSearchRequest,
    RunStatus,
)
from hunnu_harness.literature.institutional import (
    InstitutionalResolutionTrigger,
    InstitutionalRouteResult,
)
from hunnu_harness.literature.normalization import sha256_file
from hunnu_harness.literature.workflow import finalize_unattended_download_acceptance

from literature_test_support import isolated_fetch_ledger, write_minimal_pdf_with_text


TITLE = "Double/debiased machine learning for treatment and structural parameters"
DOI = "10.1111/ectj.12097"
PDF_URL = "https://academic.oup.com/ectj/article-pdf/21/1/C1/27684918/ectj00c1.pdf"
GATEWAY_PDF_URL = (
    "https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE-OXFORD-ROUTE"
    "/ectj/article-pdf/21/1/C1/27684918/ectj00c1.pdf"
)


def _profile(root: Path, payload: dict[str, object]) -> Path:
    profile = root / "ResearchHarness" / "chrome-profile"
    default = profile / "Default"
    default.mkdir(parents=True)
    (default / "Preferences").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return profile


def _manager(profile: Path, *, process_state: bool | None = False) -> ResearchChromePdfPreference:
    return ResearchChromePdfPreference(
        profile,
        expected_profile_dir=profile,
        allow_arbitrary_profile_for_tests=True,
        profile_process_check=lambda _: process_state,
    )


def _record() -> LiteratureRecord:
    return LiteratureRecord(
        paper_id="P210C53C008A6",
        title=TITLE,
        authors=("Victor Chernozhukov",),
        year="2018",
        journal="The Econometrics Journal",
        doi=DOI,
        source_database="OxfordAcademic",
        source_page="https://academic.oup.com/ectj/article/21/1/C1/5056401",
        stable_identifier="5056401",
        target_identity_confirmed=True,
    )


def _access() -> AccessDecision:
    return AccessDecision(
        full_text_accessible=True,
        access_type=AccessType.INSTITUTIONAL_AUTHENTICATED,
        authorized_access=True,
        status=RunStatus.SUCCESS,
        reason="Purchased: official article PDF is accessible",
        download_url=PDF_URL,
        download_locator=f'a[href="{PDF_URL}"]',
    )


def _gateway_access() -> AccessDecision:
    return replace(
        _access(),
        download_url=GATEWAY_PDF_URL,
        download_locator=f'a[href="{GATEWAY_PDF_URL}"]',
    )


class _Locator:
    def __init__(self) -> None:
        self.clicked = False

    def filter(self, **_: object) -> "_Locator":
        return self

    @property
    def first(self) -> "_Locator":
        return self

    async def click(self) -> None:
        self.clicked = True


class _Page:
    def __init__(self) -> None:
        self.action = _Locator()

    def locator(self, _: str) -> _Locator:
        return self.action


class _Browser:
    def __init__(self, downloads_dir: Path) -> None:
        self.page = _Page()
        self.downloads_dir = downloads_dir


class ResearchChromePdfPreferenceTests(unittest.TestCase):
    def test_absent_preference_is_added_surgically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profile = _profile(Path(td), {"homepage": "https://example.invalid"})
            preferences = profile / "Default" / "Preferences"
            before = preferences.read_text(encoding="utf-8")
            audit = _manager(profile).configure_direct_download()
            after = preferences.read_text(encoding="utf-8")
            self.assertIsNone(audit.previous_value)
            self.assertTrue(audit.new_value)
            self.assertIn(before[1:], after)
            self.assertTrue(json.loads(after)["plugins"]["always_open_pdf_externally"])

    def test_existing_false_preference_is_changed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profile = _profile(
                Path(td),
                {"plugins": {"always_open_pdf_externally": False, "other": {"nested": True}}},
            )
            audit = _manager(profile).configure_direct_download()
            payload = json.loads((profile / "Default" / "Preferences").read_text())
            self.assertFalse(audit.previous_value)
            self.assertTrue(payload["plugins"]["always_open_pdf_externally"])
            self.assertEqual(payload["plugins"]["other"], {"nested": True})

    def test_existing_true_preference_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profile = _profile(Path(td), {"plugins": {"always_open_pdf_externally": True}})
            preferences = profile / "Default" / "Preferences"
            before = preferences.read_bytes()
            audit = _manager(profile).configure_direct_download()
            self.assertTrue(audit.previous_value)
            self.assertEqual(preferences.read_bytes(), before)

    def test_unrelated_profile_state_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            payload = {
                "profile": {"exit_type": "Normal", "name": "Research"},
                "download": {"prompt_for_download": False},
            }
            profile = _profile(Path(td), payload)
            _manager(profile).configure_direct_download()
            after = json.loads((profile / "Default" / "Preferences").read_text())
            self.assertEqual(after["profile"], payload["profile"])
            self.assertEqual(after["download"], payload["download"])

    def test_profile_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profile = _profile(Path(td), {})
            (profile / "SingletonLock").write_text("test")
            with self.assertRaises(ResearchChromeProfileInUse):
                _manager(profile).configure_direct_download()

    def test_running_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profile = _profile(Path(td), {})
            with self.assertRaises(ResearchChromeProfileInUse):
                _manager(profile, process_state=True).configure_direct_download()

    def test_unavailable_running_check_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profile = _profile(Path(td), {})
            with self.assertRaises(ResearchChromeProfileInUse):
                _manager(profile, process_state=None).configure_direct_download()

    def test_wrong_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = _profile(root, {})
            with self.assertRaises(ResearchChromePreferenceError):
                ResearchChromePdfPreference(
                    profile,
                    expected_profile_dir=root / "different-profile",
                )

    def test_daily_chrome_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td)
            daily = local / "Google" / "Chrome" / "User Data" / "Default"
            daily.mkdir(parents=True)
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
                with self.assertRaises(ResearchChromePreferenceError):
                    ResearchChromePdfPreference(daily, expected_profile_dir=daily)

    def test_audit_contains_no_profile_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profile = _profile(Path(td), {})
            payload = _manager(profile).configure_direct_download().as_dict()
            self.assertEqual(payload["PreferenceName"], PDF_DIRECT_DOWNLOAD_PREFERENCE)
            self.assertEqual(payload["Scope"], "ResearchChromeOnly")
            self.assertFalse(payload["NormalUserChromeModified"])
            self.assertFalse(payload["SystemWideChromePolicyModified"])
            self.assertFalse(payload["FullPreferencesSnapshotCreated"])

    def test_cli_exposes_explicit_offline_configuration_command(self) -> None:
        args = build_parser().parse_args(
            ["browser-configure-pdf-download", "--profile", "C:/Research/profile"]
        )
        self.assertEqual(args.command, "browser-configure-pdf-download")


class OxfordUnattendedStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_event_marks_fully_unattended_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            browser = _Browser(root)
            captured = root / "captured.pdf"
            captured.write_bytes(b"%PDF-1.4\n%%EOF\n")
            adapter = OxfordAcademicAdapter(
                browser,
                allow_capture_outside_output_for_tests=True,
                research_chrome_direct_pdf_download_configured=True,
            )
            ledger_scope = isolated_fetch_ledger()
            adapter.fetch_ledger = ledger_scope.__enter__()
            self.addCleanup(ledger_scope.__exit__, None, None, None)

            trusted_hosts: tuple[str, ...] = ()

            async def capture(*_: object, **kwargs: object) -> AuthorizedFileCaptureResult:
                nonlocal trusted_hosts
                trusted_hosts = kwargs["trusted_hosts"]
                await kwargs["official_action"]()
                return AuthorizedFileCaptureResult(
                    path=captured,
                    acquisition_method=AcquisitionMethod.PLAYWRIGHT_DOWNLOAD_EVENT,
                    source_host="academic.oup.com",
                    source_route="OXFORD_DIRECT",
                    download_event_emitted=True,
                    authorized_pdf_response_captured=False,
                )

            record = _record()
            with patch.object(BrowserAuthorizedFileCapture, "capture_pdf", new=capture), patch.object(
                OxfordAcademicAdapter,
                "validate_pdf_identity",
                return_value=OxfordPDFIdentityResult(True, True, True, True),
            ):
                result = await adapter.download_fulltext(record, _access())
            self.assertEqual(result, captured)
            self.assertTrue(browser.page.action.clicked)
            self.assertTrue(record.unattended_download_attempted)
            self.assertTrue(record.automatic_download_initiation)
            self.assertTrue(record.automatic_download_detection)
            self.assertTrue(record.download_event_emitted)
            self.assertFalse(record.user_native_viewer_click_required)
            self.assertFalse(record.manual_download_handoff_used)
            self.assertFalse(record.human_download_action)
            self.assertTrue(record.oxford_unattended_download_ready)
            self.assertEqual(
                trusted_hosts,
                ("academic.oup.com", "oup.silverchair-cdn.com"),
            )

    async def test_gateway_capture_also_declares_silverchair_delivery_host(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            browser = _Browser(root)
            captured = root / "captured.pdf"
            captured.write_bytes(b"%PDF-1.4\n%%EOF\n")
            adapter = OxfordAcademicAdapter(
                browser,
                allow_capture_outside_output_for_tests=True,
            )
            adapter.bind_institutional_route(
                InstitutionalRouteResult(
                    requested_source="OxfordAcademic",
                    resolution_trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
                    institutional_route_resolved=True,
                    institutional_target_database_match=True,
                    publisher_entry_url="https://yclib.hunnu.edu.cn/vpn/",
                    publisher_navigation_url="https://yclib.hunnu.edu.cn/vpn/",
                )
            )
            ledger_scope = isolated_fetch_ledger()
            adapter.fetch_ledger = ledger_scope.__enter__()
            self.addCleanup(ledger_scope.__exit__, None, None, None)
            trusted_hosts: tuple[str, ...] = ()

            async def capture(*_: object, **kwargs: object) -> AuthorizedFileCaptureResult:
                nonlocal trusted_hosts
                trusted_hosts = kwargs["trusted_hosts"]
                await kwargs["official_action"]()
                return AuthorizedFileCaptureResult(
                    path=captured,
                    acquisition_method=AcquisitionMethod.PLAYWRIGHT_DOWNLOAD_EVENT,
                    source_host="yclib.hunnu.edu.cn",
                    source_route="HUNNU_GATEWAY_TO_OXFORD",
                    download_event_emitted=True,
                    authorized_pdf_response_captured=False,
                )

            with patch.object(BrowserAuthorizedFileCapture, "capture_pdf", new=capture), patch.object(
                OxfordAcademicAdapter,
                "validate_pdf_identity",
                return_value=OxfordPDFIdentityResult(True, True, True, True),
            ):
                await adapter.download_fulltext(_record(), _gateway_access())
            self.assertEqual(
                trusted_hosts,
                (
                    "academic.oup.com",
                    "yclib.hunnu.edu.cn",
                    "oup.silverchair-cdn.com",
                ),
            )

    async def test_manual_handoff_is_not_unattended_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adapter = OxfordAcademicAdapter(
                _Browser(root / "watch"),
                allow_capture_outside_output_for_tests=True,
            )
            (root / "watch").mkdir()
            state = adapter.arm_manual_download_handoff(
                _record(),
                _access(),
                pdf_viewer_opened=True,
                watch_directory=root / "watch",
                staging_directory=root / "staging",
            )
            record = _record()
            adapter.arm_manual_download_handoff(
                record,
                _access(),
                pdf_viewer_opened=True,
                watch_directory=root / "watch",
                staging_directory=root / "staging-2",
            )
            self.assertTrue(state.armed_at_ns > 0)
            self.assertTrue(record.user_native_viewer_click_required)
            self.assertTrue(record.manual_download_handoff_used)
            self.assertFalse(record.oxford_unattended_download_ready)

    def test_agent_routes_unattended_before_manual_fallback(self) -> None:
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
        self.assertTrue(payload["TryUnattendedDownload"])
        self.assertTrue(payload["ManualDownloadHandoffIsFallback"])

    def test_agent_reports_unattended_success_without_user_click(self) -> None:
        entry = DownloadManifestEntry(
            paper_id="P210C53C008A6",
            source="OxfordAcademic",
            title=TITLE,
            doi=DOI,
            access_type=AccessType.INSTITUTIONAL_AUTHENTICATED.value,
            authorized_access=True,
            original_url_or_stable_identifier="5056401",
            original_filename="ectj00c1.pdf",
            normalized_filename="paper.pdf",
            download_timestamp="2026-08-15T00:00:00+08:00",
            file_size_bytes=1,
            sha256="a" * 64,
            local_path="C:/output/paper.pdf",
            pdf_validation_passed=True,
            download_event_emitted=True,
            automatic_download_initiation=True,
            automatic_download_detection=True,
            user_native_viewer_click_required=False,
            manual_download_handoff_used=False,
            human_download_action=False,
            oxford_unattended_download_ready=True,
        )
        result = LiteratureRunResult(
            status=RunStatus.SUCCESS,
            records=[_record()],
            downloads=[entry],
        )
        payload = AgentRequestRouter.describe_literature_result(result)
        self.assertTrue(payload["OxfordUnattendedDownloadReady"])
        self.assertFalse(payload["ACTION_REQUIRED_USER_DOWNLOAD"])
        self.assertFalse(payload["UserNativeViewerClickRequired"])

    def test_unattended_finalizer_reuses_validator_manifest_sha_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = write_minimal_pdf_with_text(
                root / "ectj00c1.pdf",
                f"{TITLE} Victor Chernozhukov DOI: {DOI}",
            )
            record = _record()
            record.search_query = TITLE
            record.acquisition_method = "PLAYWRIGHT_DOWNLOAD_EVENT"
            record.download_event_emitted = True
            record.official_pdf_action_confirmed = True
            record.source_access_status = "Purchased"
            record.unattended_download_attempted = True
            record.automatic_download_initiation = True
            record.automatic_download_detection = True
            record.user_native_viewer_click_required = False
            record.manual_download_handoff_used = False
            record.human_download_action = False
            record.oxford_unattended_download_ready = True
            record.research_chrome_direct_pdf_download_configured = True
            record.download_initiation_mode = "HARNESS_OFFICIAL_PDF_ACTION_CLICK"
            record.file_finalization_mode = "HARNESS_AUTOMATIC"
            result = finalize_unattended_download_acceptance(
                request=LiteratureSearchRequest.from_mapping(
                    {
                        "OriginalResearchRequest": f"Download exact Oxford paper: {TITLE}",
                        "ResearchQuestion": TITLE,
                        "ExactTitles": [TITLE],
                        "DOIs": [DOI],
                        "MaxSearchResults": 1,
                        "MaxResultsPerSource": 1,
                        "MaxDownloads": 1,
                    }
                ),
                record=record,
                access=_access(),
                staged_pdf=staged,
                run_root=root / "run",
                allow_outside_project_for_tests=True,
            )
            self.assertEqual(result.status, RunStatus.SUCCESS)
            self.assertEqual(result.downloads[0].sha256, sha256_file(staged))
            manifest = json.loads((root / "run" / "DOWNLOAD_MANIFEST.json").read_text())
            item = manifest["Downloads"][0]
            self.assertEqual(item["AcquisitionMethod"], "PLAYWRIGHT_DOWNLOAD_EVENT")
            self.assertTrue(item["AutomaticDownloadInitiation"])
            self.assertTrue(item["AutomaticDownloadDetection"])
            self.assertTrue(item["OxfordUnattendedDownloadReady"])
            self.assertFalse(item["UserNativeViewerClickRequired"])
            self.assertFalse(item["ManualDownloadHandoffUsed"])
            self.assertFalse(item["SignedURLPersisted"])
            self.assertTrue(Path(item["LocalPath"]).exists())

    def test_manual_state_is_rejected_by_unattended_finalizer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = write_minimal_pdf_with_text(root / "paper.pdf", TITLE)
            record = _record()
            record.acquisition_method = "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF"
            record.manual_download_handoff_used = True
            record.human_download_action = True
            result = finalize_unattended_download_acceptance(
                request=LiteratureSearchRequest.from_mapping(
                    {
                        "OriginalResearchRequest": TITLE,
                        "ResearchQuestion": TITLE,
                        "MaxDownloads": 1,
                    }
                ),
                record=record,
                access=_access(),
                staged_pdf=staged,
                run_root=root / "run",
                allow_outside_project_for_tests=True,
            )
            self.assertEqual(result.status, RunStatus.DOWNLOAD_FAILED)
            self.assertEqual(result.downloads, [])

    def test_manual_gate_never_reports_unattended_ready(self) -> None:
        result = LiteratureRunResult(
            status=RunStatus.ACTION_REQUIRED_USER_DOWNLOAD,
            records=[_record()],
            downloads=[],
        )
        payload = AgentRequestRouter.describe_literature_result(result)
        self.assertFalse(payload["OxfordUnattendedDownloadReady"])
        self.assertTrue(payload["UserNativeViewerClickRequired"])


if __name__ == "__main__":
    unittest.main()
