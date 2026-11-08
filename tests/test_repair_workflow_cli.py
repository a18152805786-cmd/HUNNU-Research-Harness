"""Public repair commands retain read-only defaults and local-only refusal."""

import asyncio
import contextlib
import io
import json
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from hunnu_harness.cli import main


def invoke(args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main([*args, "--json"])
    return code, json.loads(output.getvalue())


def test_reclassification_cli_never_applies_without_explicit_apply_flag():
    result = SimpleNamespace(succeeded=True, as_dict=lambda: {"ReclassificationStatus": "DRY_RUN"})
    with patch("hunnu_harness.literature.topic_reclassification.TopicReclassifier") as service:
        service.return_value.reclassify.return_value = result
        code, payload = invoke(["library-reclassify", "--paper-id", "P-TEST", "--reason", "Repair a verified method-topic mismatch"])
    assert code == 0 and payload["ReclassificationStatus"] == "DRY_RUN"
    kwargs = service.return_value.reclassify.call_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["authorize_human_confirmed_fingerprint"] is None
    assert kwargs["expected_fingerprint"] is None


def test_reclassification_recovery_cli_is_explicit_and_defaults_to_preview(tmp_path):
    result = SimpleNamespace(succeeded=True, as_dict=lambda: {"RecoveryStatus": "DRY_RUN"})
    with patch("hunnu_harness.literature.topic_reclassification.TopicReclassifier") as service:
        service.return_value.recover.return_value = result
        code, payload = invoke([
            "library-reclassification-recover", "--recovery-path", str(tmp_path / "retained-package"),
            "--preimage-sha256", "a" * 64, "--reason", "Repair a verified extra hardlink alias",
        ])
    assert code == 0 and payload["RecoveryStatus"] == "DRY_RUN"
    kwargs = service.return_value.recover.call_args.kwargs
    assert kwargs["dry_run"] is True and kwargs["expected_current_fingerprint"] is None
    assert kwargs["expected_preimage_sha256"] == "a" * 64


def test_reclassification_apply_without_precondition_is_a_controlled_refusal(tmp_path):
    # The production service validates the optimistic precondition before any
    # library mutation. No unknown library WORK is created by this command.
    code, payload = invoke(["library-reclassify", "--paper-id", "P-UNHELD", "--reason", "Explicit repair", "--apply"])
    assert code == 1
    assert not payload["Applied"]
    assert payload["ReclassificationStatus"] in {"PRECONDITION_REQUIRED", "NOT_FOUND", "FAILED"}


def test_local_manual_resume_bad_receipt_does_not_construct_a_browser(tmp_path):
    with patch("hunnu_harness.browser.playwright_backend.PlaywrightBrowser", side_effect=AssertionError("No browser may be constructed")):
        code, payload = invoke(["manual-download-resume", "--state", str(tmp_path / "absent.json"), "--sha256", "0" * 64, "--timeout-seconds", "0.01"])
    assert code == 1 and payload["Status"] == "REFUSED"
    assert payload["NetworkActivity"] is False
    assert not list(tmp_path.iterdir())


def test_manual_watch_option_cannot_be_forwarded_to_another_source():
    code, payload = invoke(["acquire", "--source", "sciencedirect", "--doi", "10.1000/example", "--manual-watch-directory", str(Path("C:/fixture"))])
    assert code == 1 and payload["Status"] == "INVALID_REQUEST"


@pytest.mark.parametrize("pdf_count", [0, 2])
def test_manual_resume_cli_timeout_or_ambiguity_is_one_json_without_traceback(tmp_path, pdf_count):
    from hunnu_harness.literature.adapters.oxfordacademic import OxfordAcademicAdapter
    from hunnu_harness.literature.manual_resume import save_oxford_manual_handoff
    from hunnu_harness.literature.models import AccessDecision, AccessType, LiteratureRecord, LiteratureSearchRequest, RunStatus
    from literature_test_support import write_minimal_pdf_with_text
    from hunnu_harness.paths import TEMP_DIR

    # Child CLI processes enforce the configured Output boundary, while
    # pytest's independent --basetemp may be outside that isolated runtime.
    tmp_path = TEMP_DIR / ("manual-resume-cli-" + uuid.uuid4().hex)
    tmp_path.mkdir(parents=True)
    watch = tmp_path / "explicit-watch"
    staging = tmp_path / "run" / "downloads" / "staging"
    watch.mkdir()
    staging.mkdir(parents=True)
    record = LiteratureRecord(
        paper_id="P-MANUAL-CLI", title="A valid manual resume target paper", doi="10.1234/manual-cli",
        authors=("Ada Researcher",), source_database="OxfordAcademic", source_page="https://academic.oup.com/ectj/article/21/1/C1/5056401",
        stable_identifier="5056401", target_identity_confirmed=True,
    )
    access = AccessDecision(True, AccessType.OPEN_ACCESS, True, RunStatus.SUCCESS, "OpenAccess",
                            download_url="https://academic.oup.com/ectj/article-pdf/21/1/C1/12345/example.pdf")
    adapter = OxfordAcademicAdapter(SimpleNamespace(downloads_dir=staging), manual_download_watch_directory=watch)
    state = adapter.arm_manual_download_handoff(record, access, pdf_viewer_opened=True)
    receipt = save_oxford_manual_handoff(
        request=LiteratureSearchRequest(original_research_request=record.title, dois=(record.doi,)),
        record=record, access=access, state=state, run_root=tmp_path / "run",
    )
    originals = []
    for index in range(pdf_count):
        source = write_minimal_pdf_with_text(watch / f"candidate-{index}.pdf", record.title + " DOI: " + record.doi)
        originals.append((source, source.read_bytes()))
    process = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "hunnu_harness.cli", "manual-download-resume", "--state", str(receipt.state_path),
         "--sha256", receipt.state_sha256, "--timeout-seconds", "1.1" if pdf_count else ".01", "--json"],
        capture_output=True, text=True, encoding="utf-8", cwd=Path(__file__).parents[1], timeout=10,
    )
    payload = json.loads(process.stdout)
    assert process.returncode == 1 and payload["Status"] == "REFUSED"
    assert payload["NetworkActivity"] is False
    assert "Traceback" not in process.stderr
    assert not (tmp_path / "run" / "manual-resume").exists()
    assert not (receipt.state_path.parent / "RESUME.lock").exists()
    assert all(path.read_bytes() == raw for path, raw in originals)


@pytest.mark.parametrize("current_kind,armed,ready", [("absent", True, False), ("same", False, False), ("different", True, False), ("same", True, True)])
def test_live_manual_gate_never_advertises_an_old_or_unarmed_receipt(tmp_path, current_kind, armed, ready):
    from hunnu_harness.cli_output import CliReport
    from hunnu_harness.literature import cli as literature_cli
    from hunnu_harness.literature.manual_resume import ManualHandoffReceipt
    from hunnu_harness.literature.models import LiteratureRunResult, RunStatus

    receipt = ManualHandoffReceipt(tmp_path / "run/manual-download-handoff/STATE.json", "a" * 64)
    current = None if current_kind == "absent" else receipt.as_dict()
    if current_kind == "different":
        current = {**current, "StateSHA256": "b" * 64}
    record = SimpleNamespace(source_database="OxfordAcademic", manual_download_handoff_armed=armed)
    result = LiteratureRunResult(RunStatus.ACTION_REQUIRED_USER_DOWNLOAD, [record], [],
                                 action_required_reason="Manual gate", manual_download_receipt=current)
    browser = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), lifecycle=AsyncMock(return_value={}))
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=None))
    workflow = SimpleNamespace(run=AsyncMock(return_value=result))
    args = literature_cli.build_parser().parse_args([
        "live-oxfordacademic", "--doi", "10.1234/manual-cli", "--run-root", str(tmp_path / "run"),
        "--profile", str(tmp_path / "fixture-profile"), "--json",
    ])
    report = CliReport(True)
    with patch.object(literature_cli, "PlaywrightBrowser", return_value=browser), patch.object(
        literature_cli, "OxfordAcademicAdapter", return_value=Mock()
    ), patch.object(literature_cli, "HUNNUInstitutionalAccessResolver", return_value=resolver), patch.object(
        literature_cli, "LiteratureAcquisitionWorkflow", return_value=workflow
    ), patch.object(literature_cli, "assess_pdf_download_readiness", return_value={"ResearchChromeDirectPdfDownloadConfigured": False}), patch(
        "hunnu_harness.literature.manual_resume.get_handoff_receipt", return_value=receipt
    ) as load:
        code, returned = asyncio.run(literature_cli._run_live_into(args, report))
    payload = report.payload()
    assert payload["BrowserReadyForManualDownload"] is ready
    assert payload["ManualDownloadHandoffPersisted"] is ready
    if current_kind == "absent" or not armed:
        load.assert_not_called()
    if not ready:
        assert "StatePath" not in payload and "StateSHA256" not in payload
