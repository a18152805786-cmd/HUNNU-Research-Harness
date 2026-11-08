"""The official visible PDF control is clicked once; no context HTTP replay."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from hunnu_harness.browser.commands import DownloadArtifact, DownloadCommand, PageHandle, SessionHandle
from hunnu_harness.literature.adapters.base import SourceLayoutChanged, SourceUnavailable
from hunnu_harness.literature.adapters.springerlink import SpringerLinkAdapter
from hunnu_harness.literature.fetch_ledger import FulltextFetchLedger
from literature_test_support import write_minimal_pdf_with_text


DOI = "10.1007/s11573-023-01162-8"
ARTICLE_URL = f"https://link.springer.com/article/{DOI}"
PDF_URL = f"https://link.springer.com/content/pdf/{DOI}.pdf"
FIXTURE = Path(__file__).parent / "fixtures/literature/springerlink_article_open_access.html"


class CapturePort:
    session = SessionHandle("springer-public-test")
    page_handle = PageHandle("main")

    def __init__(self, source, metadata):
        self.source = source
        self.metadata = metadata
        self.downloads_dir = source.parent
        self.calls = []

    async def execute(self, command):
        # An HTTP request, navigation retry or second click cannot silently
        # satisfy the test, even if it returned the desired bytes.
        assert isinstance(command, DownloadCommand)
        self.calls.append(command)
        assert len(self.calls) == 1
        assert command.capture.expected_url == PDF_URL
        assert command.capture.trusted_hosts == ("link.springer.com",)
        assert command.capture.download_identity_required
        assert command.target.css == f'a[href="/content/pdf/{DOI}.pdf"]'
        return DownloadArtifact.from_path(self.source, metadata=self.metadata)


def setup_capture(tmp_path, *, method="BROWSER_DIRECTED_DOWNLOAD", event=True, response=False, host="link.springer.com", route="SPRINGER_DIRECT", pdf_doi=DOI):
    html = FIXTURE.read_text(encoding="utf-8")
    record = SpringerLinkAdapter.parse_article_html(html, source_url=ARTICLE_URL)
    record.target_identity_confirmed = True
    source = write_minimal_pdf_with_text(tmp_path / "first-response.pdf", record.title.replace("\u2019", "'") + " DOI: " + pdf_doi)
    port = CapturePort(source, {
        "AcquisitionMethod": method, "DownloadEventEmitted": event,
        "AuthorizedPDFResponseCaptured": response, "SourceHost": host, "SourceRoute": route,
    })
    adapter = SpringerLinkAdapter(port)
    return adapter, port, record, adapter.check_fulltext_access_html(html, source_url=ARTICLE_URL), source


@pytest.mark.parametrize("method,event,response", [
    ("BROWSER_DIRECTED_DOWNLOAD", True, False),
    ("PLAYWRIGHT_DOWNLOAD_EVENT", True, False),
    ("AUTHORIZED_PDF_RESPONSE", False, True),
])
def test_public_official_control_uses_one_browser_action_and_local_identity(tmp_path, method, event, response):
    adapter, port, record, access, source = setup_capture(tmp_path, method=method, event=event, response=response)
    assert asyncio.run(adapter.download_fulltext(record, access)) == source
    assert len(port.calls) == 1
    assert record.target_identity_confirmed
    assert record.target_doi_matched
    assert record.acquisition_method == method
    assert record.download_event_emitted == event
    assert record.authorized_pdf_response_captured == response
    assert not record.institutional_route_used
    assert not record.human_download_action
    assert "ok=True" in FulltextFetchLedger().usage_today()["Identifiers"][0]["last_outcome"]


@pytest.mark.parametrize("overrides", [
    {"event": False}, {"response": True},
    {"method": "AUTHORIZED_PDF_RESPONSE", "event": True, "response": True},
    {"host": "untrusted.test"}, {"route": "HUNNU_GATEWAY_TO_SPRINGER"},
    {"method": "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF"},
])
def test_public_capture_rejects_inconsistent_or_manual_provenance(tmp_path, overrides):
    adapter, port, record, access, source = setup_capture(tmp_path, **overrides)
    before = source.read_bytes()
    with pytest.raises(SourceUnavailable, match="provenance"):
        asyncio.run(adapter.download_fulltext(record, access))
    assert len(port.calls) == 1 and source.read_bytes() == before
    assert "ok=False" in FulltextFetchLedger().usage_today()["Identifiers"][0]["last_outcome"]


def test_different_doi_control_is_refused_before_any_publisher_fetch(tmp_path):
    adapter, port, record, access, source = setup_capture(tmp_path)
    access = replace(access, download_url=PDF_URL.replace(DOI, "10.1007/different-paper"))
    with pytest.raises(SourceLayoutChanged, match="locked article DOI"):
        asyncio.run(adapter.download_fulltext(record, access))
    assert not port.calls
    assert FulltextFetchLedger().usage_today()["AttemptsToday"] == 0


def test_wrong_pdf_is_retained_and_never_claimed_as_confirmed(tmp_path):
    adapter, port, record, access, source = setup_capture(tmp_path, pdf_doi="10.1007/different-paper")
    before = source.read_bytes()
    with pytest.raises(SourceLayoutChanged, match="local target identity"):
        asyncio.run(adapter.download_fulltext(record, access))
    assert not record.target_identity_confirmed
    assert source.read_bytes() == before and len(port.calls) == 1
    assert "ok=False" in FulltextFetchLedger().usage_today()["Identifiers"][0]["last_outcome"]


@pytest.mark.parametrize("same_title", [False, True])
def test_a_references_doi_does_not_override_the_main_article_identity(tmp_path, same_title):
    adapter, port, record, access, source = setup_capture(tmp_path)
    title = record.title.replace("\u2019", "'") if same_title else "An entirely different empirical research article"
    write_minimal_pdf_with_text(source, title + "\nDOI: 10.9999/different-paper\nReferences\nCited study DOI: " + DOI)
    before = source.read_bytes()
    with pytest.raises(SourceLayoutChanged, match="local target identity"):
        asyncio.run(adapter.download_fulltext(record, access))
    assert not record.target_identity_confirmed
    assert not record.target_doi_matched
    assert source.read_bytes() == before and len(port.calls) == 1


def test_absolute_control_href_gets_its_actual_exact_locator():
    html = FIXTURE.read_text(encoding="utf-8").replace(f'href="/content/pdf/{DOI}.pdf"', f'href="{PDF_URL}"')
    access = SpringerLinkAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
    assert access.download_locator == f'a[href="{PDF_URL}"]'
