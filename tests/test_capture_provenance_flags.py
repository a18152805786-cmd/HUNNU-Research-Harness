from __future__ import annotations

import pytest

from hunnu_harness.browser.commands import DownloadArtifact
from hunnu_harness.literature.adapters.base import SourceUnavailable, authorized_capture_result_from_artifact


def _artifact(tmp_path, flags):
    path = tmp_path / "first.pdf"
    path.write_bytes(b"%PDF-1.4\noriginal diagnostic bytes\n")
    return DownloadArtifact.from_path(path, metadata={
        "AcquisitionMethod": "BROWSER_DIRECTED_DOWNLOAD",
        "SourceHost": "link.springer.com", "SourceRoute": "SPRINGER_DIRECT",
        **flags,
    })


@pytest.mark.parametrize("event,response", ((True, False), (False, True), (False, False), (True, True)))
def test_real_boolean_capture_flags_keep_their_exact_values(tmp_path, event, response):
    artifact = _artifact(tmp_path, {
        "DownloadEventEmitted": event, "AuthorizedPDFResponseCaptured": response,
    })
    result = authorized_capture_result_from_artifact(artifact)
    assert result.download_event_emitted is event
    assert result.authorized_pdf_response_captured is response


def test_missing_flags_remain_false_without_inventing_provenance(tmp_path):
    result = authorized_capture_result_from_artifact(_artifact(tmp_path, {}))
    assert result.download_event_emitted is False
    assert result.authorized_pdf_response_captured is False


@pytest.mark.parametrize("key", ("DownloadEventEmitted", "AuthorizedPDFResponseCaptured"))
@pytest.mark.parametrize("value", ("false", "true", "False", 0, 1, None, [], {}))
def test_non_boolean_capture_flags_are_rejected_instead_of_becoming_truthy_evidence(tmp_path, key, value):
    artifact = _artifact(tmp_path, {key: value})
    before = artifact.local_path.read_bytes()
    with pytest.raises(SourceUnavailable, match="invalid boolean capture provenance") as failure:
        authorized_capture_result_from_artifact(artifact)
    assert key in str(failure.value)
    assert artifact.local_path.read_bytes() == before
