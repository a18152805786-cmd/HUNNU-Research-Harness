"""A completed CDP download is an event, never an invented response or handoff."""

import pytest

from hunnu_harness.literature.models import (
    AccessDecision, AccessType, FullTextFormat, LiteratureRecord,
    LiteratureSearchRequest, RunStatus,
)
from hunnu_harness.literature.workflow import finalize_unattended_download_acceptance
from literature_test_support import write_minimal_pdf_with_text


@pytest.mark.parametrize("event,response,human,accepted", [
    (True, False, False, True),
    (False, False, False, False),
    (True, True, False, False),
    (True, False, True, False),
])
def test_cdp_unattended_finalization_checks_provenance_before_archiving(tmp_path, event, response, human, accepted):
    title = "Double/debiased machine learning for treatment and structural parameters"
    doi = "10.1111/ectj.12097"
    source = write_minimal_pdf_with_text(tmp_path / "ectj00c1.pdf", title + " DOI: " + doi)
    record = LiteratureRecord(
        paper_id="P-CDP-ACCEPTANCE", title=title, doi=doi,
        source_database="OxfordAcademic", source_page="https://academic.oup.com/ectj/article/21/1/C1/5056401",
        stable_identifier="5056401", target_identity_confirmed=True,
        acquisition_method="BROWSER_DIRECTED_DOWNLOAD", download_event_emitted=event,
        authorized_pdf_response_captured=response, unattended_download_attempted=True,
        automatic_download_initiation=True, automatic_download_detection=True,
        oxford_unattended_download_ready=True, human_download_action=human,
    )
    request = LiteratureSearchRequest.from_mapping({
        "OriginalResearchRequest": title, "ExactTitles": [title], "DOIs": [doi], "MaxDownloads": 1,
    })
    access = AccessDecision(True, AccessType.OPEN_ACCESS, True, RunStatus.SUCCESS,
                            "Official PDF control", full_text_format=FullTextFormat.PDF)
    result = finalize_unattended_download_acceptance(
        request=request, record=record, access=access, staged_pdf=source,
        run_root=tmp_path / "run", allow_outside_project_for_tests=True,
    )
    assert (result.status == RunStatus.SUCCESS) == accepted
    assert bool(result.downloads) == accepted
    assert source.exists()
