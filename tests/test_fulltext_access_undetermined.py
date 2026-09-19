"""The workflow preserves adapter access decisions that are not refusals."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.browser.commands import BrowserObservation, ObserveCommand, PageHandle, SessionHandle
from hunnu_harness.literature.adapters.base import LiteratureSourceAdapter
from hunnu_harness.literature.adapters.cnki import CNKIAdapter
from hunnu_harness.literature.models import (
    AccessDecision,
    AccessType,
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
)
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow

from literature_test_support import write_minimal_pdf


ARTICLE_URL = "https://kns.cnki.net/kcms2/article/abstract?dbcode=CJFD&filename=TEST202601001"


class _WorkflowAccessAdapter(LiteratureSourceAdapter):
    name = "WorkflowAccessFixture"
    human_like_delay_seconds = 0

    def __init__(self, root: Path, records: list[LiteratureRecord], decisions: dict[str, AccessDecision]) -> None:
        super().__init__(browser=None)
        self.root = root
        self.records = records
        self.decisions = decisions
        self.active: LiteratureRecord | None = None
        self.download_calls: list[str] = []

    async def search(self, query: str, request: LiteratureSearchRequest) -> list[LiteratureRecord]:
        return self.records

    async def open_result(self, record: LiteratureRecord) -> None:
        self.active = record

    async def extract_metadata(self, *, search_query: str) -> LiteratureRecord:
        assert self.active is not None
        record = self.active
        return LiteratureRecord(
            paper_id=record.paper_id,
            title=record.title,
            authors=record.authors,
            year=record.year,
            journal=record.journal,
            doi=record.doi,
            abstract=record.abstract,
            source_database=self.name,
            source_page=record.source_page,
            stable_identifier=record.stable_identifier,
            search_query=search_query,
            publication_status=record.publication_status,
        )

    async def extract_abstract(self) -> str:
        assert self.active is not None
        return self.active.abstract

    async def check_fulltext_access(self) -> AccessDecision:
        assert self.active is not None
        return self.decisions[self.active.paper_id]

    async def download_fulltext(self, record: LiteratureRecord, access: AccessDecision) -> Path:
        self.download_calls.append(record.paper_id)
        return write_minimal_pdf(self.root / f"{record.paper_id}.pdf")

    async def get_citation(self) -> dict[str, str]:
        return {"Title": self.active.title if self.active is not None else "unknown"}


def _record(paper_id: str) -> LiteratureRecord:
    return LiteratureRecord(
        paper_id=paper_id,
        title=f"AI washing and audit monitoring {paper_id}",
        authors=("A. Author",),
        year="2026",
        journal="Journal of Tests",
        doi=f"10.1000/{paper_id.casefold()}",
        abstract="AI washing and audit monitoring affect earnings management.",
        source_database=_WorkflowAccessAdapter.name,
        source_page=f"https://example.test/article/{paper_id}",
        stable_identifier=paper_id,
        publication_status=PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value,
    )


def _request(*, candidate_count: int, max_downloads: int | None = None) -> LiteratureSearchRequest:
    limit = candidate_count if max_downloads is None else max_downloads
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": "AI washing and audit monitoring",
            "KeywordsEN": ["AI washing", "audit monitoring"],
            "MaxSearchResults": candidate_count,
            "MaxResultsPerSource": candidate_count,
            "MaxDownloads": limit,
            "MaxDownloadsPerRun": limit,
            "RequireFullText": True,
        }
    )


def _events(run_root: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (run_root / "audit" / "LITERATURE_EVENTS.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class LiteratureWorkflowAccessDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_undetermined_access_decision_is_carried_as_partial_failure(self) -> None:
        """This failing means the workflow turns an adapter layout result into a refusal."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = _record("UNKNOWN1")
            reason = "ARTICLE_READINESS_TIMEOUT: page never decided"
            adapter = _WorkflowAccessAdapter(
                root,
                [record],
                {
                    record.paper_id: AccessDecision(
                        False,
                        AccessType.UNKNOWN,
                        False,
                        RunStatus.SOURCE_LAYOUT_CHANGED,
                        reason,
                    )
                },
            )
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(_request(candidate_count=1))

            self.assertEqual(result.records[0].error_status, RunStatus.SOURCE_LAYOUT_CHANGED.value)
            self.assertEqual(result.records[0].error_reason, reason)
            self.assertEqual(result.status, RunStatus.PARTIAL_SUCCESS)
            self.assertNotEqual(result.status, RunStatus.FULLTEXT_NOT_AUTHORIZED)
            self.assertEqual(adapter.download_calls, [])
            manifest = json.loads((root / "run" / "DOWNLOAD_MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["Downloads"], [])
            events = _events(root / "run")
            undetermined = [event for event in events if event["action"] == "fulltext_access_undetermined"]
            self.assertEqual(len(undetermined), 1)
            self.assertEqual(undetermined[0]["status"], RunStatus.SOURCE_LAYOUT_CHANGED.value)
            self.assertEqual(undetermined[0]["paper_id"], record.paper_id)
            self.assertEqual(undetermined[0]["access_type"], AccessType.UNKNOWN.value)
            self.assertEqual(undetermined[0]["reason"], reason)
            self.assertFalse(any(event["action"] == "fulltext_not_authorized" for event in events))

    async def test_an_explicit_refusal_remains_fulltext_not_authorized(self) -> None:
        """This failing means the refusal branch no longer preserves its historical status and audit action."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = _record("REFUSED1")
            adapter = _WorkflowAccessAdapter(
                root,
                [record],
                {
                    record.paper_id: AccessDecision(
                        False,
                        AccessType.METADATA_ONLY,
                        False,
                        RunStatus.FULLTEXT_NOT_AUTHORIZED,
                        "Publisher explicitly refused full text",
                    )
                },
            )
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(_request(candidate_count=1))

            self.assertEqual(result.status, RunStatus.FULLTEXT_NOT_AUTHORIZED)
            self.assertEqual(result.records[0].error_status, RunStatus.FULLTEXT_NOT_AUTHORIZED.value)
            self.assertEqual(adapter.download_calls, [])
            events = _events(root / "run")
            refusal = [event for event in events if event["action"] == "fulltext_not_authorized"]
            self.assertEqual(len(refusal), 1)
            self.assertFalse(any(event["action"] == "fulltext_access_undetermined" for event in events))

    async def test_a_mixed_run_downloads_authorized_access_and_reports_undetermined_access(self) -> None:
        """This failing means one undetermined record prevents the authorized record from downloading."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unknown = _record("UNKNOWN2")
            authorized = _record("AUTHORIZED1")
            unknown_reason = "ARTICLE_READINESS_TIMEOUT: second page never decided"
            adapter = _WorkflowAccessAdapter(
                root,
                [unknown, authorized],
                {
                    unknown.paper_id: AccessDecision(
                        False,
                        AccessType.UNKNOWN,
                        False,
                        RunStatus.SOURCE_LAYOUT_CHANGED,
                        unknown_reason,
                    ),
                    authorized.paper_id: AccessDecision(
                        True,
                        AccessType.OPEN_ACCESS,
                        True,
                        RunStatus.SUCCESS,
                        "Official PDF control",
                    ),
                },
            )
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(_request(candidate_count=2, max_downloads=2))

            records = {item.paper_id: item for item in result.records}
            self.assertEqual(result.status, RunStatus.PARTIAL_SUCCESS)
            self.assertEqual(records[unknown.paper_id].error_status, RunStatus.SOURCE_LAYOUT_CHANGED.value)
            self.assertEqual(records[authorized.paper_id].error_status, "unknown")
            self.assertEqual(adapter.download_calls, [authorized.paper_id])
            self.assertEqual(len(result.downloads), 1)
            events = _events(root / "run")
            self.assertEqual(
                len([event for event in events if event["action"] == "fulltext_access_undetermined"]),
                1,
            )
            self.assertEqual(
                len([event for event in events if event["action"] == "authorized_fulltext_archived"]),
                1,
            )


class _CNKIHTMLBrowser:
    navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

    def __init__(self, observations: list[str]) -> None:
        self.observations = observations
        self.observe_count = 0
        self.commands: list[object] = []
        self.session = SessionHandle("cnki-access-decision-test")
        self.page_handle = PageHandle("main", session=self.session)

    async def execute(self, command):
        self.commands.append(command)
        if not isinstance(command, ObserveCommand):
            raise AssertionError(type(command).__name__)
        index = min(self.observe_count, len(self.observations) - 1)
        self.observe_count += 1
        return BrowserObservation(
            session=self.session,
            page=self.page_handle,
            generation=self.observe_count,
            url=ARTICLE_URL,
            title="CNKI article",
            html=self.observations[index],
        )


def _cnki_undetermined_html() -> str:
    return (Path(__file__).parent / "fixtures" / "literature" / "cnki_article_live_structure.html").read_text(
        encoding="utf-8"
    )


class CNKIAccessDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_undetermined_cnki_access_is_source_layout_changed(self) -> None:
        """This failing means bounded CNKI ambiguity is still reported as publisher refusal."""
        browser = _CNKIHTMLBrowser([_cnki_undetermined_html()])
        with patch("hunnu_harness.literature.adapters.cnki._ACCESS_SETTLE_DELAY_SECONDS", 0):
            decision = await CNKIAdapter(browser).check_fulltext_access()

        self.assertEqual(decision.status, RunStatus.SOURCE_LAYOUT_CHANGED)
        self.assertEqual(decision.access_type, AccessType.UNKNOWN)
        self.assertFalse(decision.full_text_accessible)
        self.assertFalse(decision.authorized_access)
        self.assertTrue(decision.reason.startswith("ACCESS_READINESS_TIMEOUT:"))
        self.assertIn("FULLTEXT_ACCESS_UNKNOWN", decision.reason)
        self.assertGreaterEqual(browser.observe_count, 1)

    async def test_an_explicit_cnki_authorization_block_remains_a_refusal(self) -> None:
        """This failing means the adapter rewrites an explicit authorization block as layout uncertainty."""
        explicit_block = _cnki_undetermined_html().replace(
            "<body>",
            "<body><p>full text not available</p>",
            1,
        )
        browser = _CNKIHTMLBrowser([explicit_block, _cnki_undetermined_html()])
        with patch("hunnu_harness.literature.adapters.cnki._ACCESS_SETTLE_DELAY_SECONDS", 0):
            decision = await CNKIAdapter(browser).check_fulltext_access()

        self.assertEqual(decision.status, RunStatus.FULLTEXT_NOT_AUTHORIZED)
        self.assertEqual(decision.access_type, AccessType.METADATA_ONLY)
        self.assertFalse(decision.full_text_accessible)
        self.assertFalse(decision.authorized_access)
        self.assertEqual(browser.observe_count, 1)


if __name__ == "__main__":
    unittest.main()
