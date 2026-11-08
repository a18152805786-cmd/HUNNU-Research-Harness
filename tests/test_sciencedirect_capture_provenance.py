"""ScienceDirect records the actual typed capture and verifies the local paper."""

import asyncio
import hashlib
from pathlib import Path

import pytest

from hunnu_harness.browser.commands import DownloadArtifact, DownloadCommand, PageHandle, SessionHandle
from hunnu_harness.literature.adapters.base import SourceActionRequired, SourceLayoutChanged, SourceUnavailable
from hunnu_harness.literature.adapters.sciencedirect import ScienceDirectAdapter
from hunnu_harness.literature.fetch_ledger import FulltextFetchLedger
from literature_test_support import write_minimal_pdf_with_text


FIXTURE = Path(__file__).parent / "fixtures/literature/sciencedirect_article_authorized.html"
ARTICLE_URL = "https://www.sciencedirect.com/science/article/pii/S1544612326004149"


class CapturePort:
    session = SessionHandle("sciencedirect-capture-test")
    page_handle = PageHandle("main")

    def __init__(self, source, metadata):
        self.source = source
        self.metadata = metadata
        self.downloads_dir = source.parent
        self.calls = []

    async def execute(self, command):
        assert isinstance(command, DownloadCommand)
        self.calls.append(command)
        assert len(self.calls) == 1
        return DownloadArtifact.from_path(self.source, metadata=self.metadata)


def setup_capture(tmp_path, **metadata_overrides):
    html = FIXTURE.read_text(encoding="utf-8")
    record = ScienceDirectAdapter.parse_article_html(html, source_url=ARTICLE_URL)
    record.target_identity_confirmed = True
    source = write_minimal_pdf_with_text(tmp_path / "received.pdf", record.title + " DOI: " + record.doi)
    metadata = {
        "AcquisitionMethod": "BROWSER_DIRECTED_DOWNLOAD",
        "DownloadEventEmitted": True, "AuthorizedPDFResponseCaptured": False,
        "SourceHost": "pdf.sciencedirectassets.com", "SourceRoute": "SCIENCEDIRECT_OFFICIAL_PDF",
    }
    metadata.update(metadata_overrides)
    port = CapturePort(source, metadata)
    adapter = ScienceDirectAdapter(port)
    access = adapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
    return adapter, port, record, access, source


@pytest.mark.parametrize("method,event,response", [
    ("BROWSER_DIRECTED_DOWNLOAD", True, False),
    ("PLAYWRIGHT_DOWNLOAD_EVENT", True, False),
    ("AUTHORIZED_PDF_RESPONSE", False, True),
])
def test_typed_capture_provenance_and_local_identity_reach_the_record(tmp_path, method, event, response):
    adapter, port, record, access, source = setup_capture(
        tmp_path, AcquisitionMethod=method, DownloadEventEmitted=event, AuthorizedPDFResponseCaptured=response,
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    assert asyncio.run(adapter.download_fulltext(record, access)) == source
    assert len(port.calls) == 1
    assert record.acquisition_method == method
    assert record.download_event_emitted is event
    assert record.authorized_pdf_response_captured is response
    assert record.source_host == "pdf.sciencedirectassets.com"
    assert record.source_route == "SCIENCEDIRECT_OFFICIAL_PDF"
    assert record.target_title_matched and record.target_doi_matched and record.target_identity_confirmed
    assert record.automatic_download_initiation and record.automatic_download_detection
    assert record.official_pdf_action_confirmed
    assert not record.human_download_action and not record.manual_download_handoff_used
    assert not record.user_native_viewer_click_required
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert "ok=True" in FulltextFetchLedger().usage_today()["Identifiers"][0]["last_outcome"]


@pytest.mark.parametrize("overrides", [
    {"DownloadEventEmitted": False},
    {"AuthorizedPDFResponseCaptured": True},
    {"AcquisitionMethod": "AUTHORIZED_PDF_RESPONSE", "DownloadEventEmitted": True, "AuthorizedPDFResponseCaptured": True},
    {"AcquisitionMethod": "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF"},
    {"DownloadEventEmitted": "false"},
    {"AuthorizedPDFResponseCaptured": 1},
    {"SourceHost": "pdf.sciencedirectassets.com.evil.invalid"},
    {"SourceRoute": "EXTERNAL_IMPORT"},
])
def test_inconsistent_or_untrusted_capture_is_retained_but_rejected(tmp_path, overrides):
    adapter, port, record, access, source = setup_capture(tmp_path, **overrides)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(SourceUnavailable, match="provenance"):
        asyncio.run(adapter.download_fulltext(record, access))
    assert len(port.calls) == 1
    assert not record.target_identity_confirmed
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert "ok=False" in FulltextFetchLedger().usage_today()["Identifiers"][0]["last_outcome"]


@pytest.mark.parametrize("same_title", [False, True])
def test_reference_doi_cannot_claim_a_different_article(tmp_path, same_title):
    adapter, port, record, access, source = setup_capture(tmp_path)
    title = record.title if same_title else "A different paper about empirical finance and management"
    write_minimal_pdf_with_text(source, title + "\nDOI: 10.9999/different-paper\nReferences\nCited paper DOI: " + record.doi)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(SourceLayoutChanged, match="local target identity"):
        asyncio.run(adapter.download_fulltext(record, access))
    assert not record.target_identity_confirmed and not record.target_doi_matched
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before and len(port.calls) == 1
    assert "ok=False" in FulltextFetchLedger().usage_today()["Identifiers"][0]["last_outcome"]


def test_correct_doi_does_not_override_a_different_title(tmp_path):
    adapter, port, record, access, source = setup_capture(tmp_path)
    write_minimal_pdf_with_text(source, "A different paper about empirical finance and management DOI: " + record.doi)
    before = source.read_bytes()
    with pytest.raises(SourceLayoutChanged, match="local target identity"):
        asyncio.run(adapter.download_fulltext(record, access))
    assert not record.target_identity_confirmed and not record.target_title_matched
    assert source.read_bytes() == before and len(port.calls) == 1


def test_downloaded_challenge_keeps_the_manual_gate_and_raw_bytes(tmp_path):
    adapter, port, record, access, source = setup_capture(tmp_path)
    source.write_bytes(b"<!doctype html><html><head><title>Just a moment...</title></head><body>Complete the CAPTCHA</body></html>")
    before = source.read_bytes()
    with pytest.raises(SourceActionRequired):
        asyncio.run(adapter.download_fulltext(record, access))
    assert source.read_bytes() == before and len(port.calls) == 1
    assert not record.target_identity_confirmed
    assert "ok=False" in FulltextFetchLedger().usage_today()["Identifiers"][0]["last_outcome"]
