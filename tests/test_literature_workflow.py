import json
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.adapters.base import LiteratureSourceAdapter, SourceActionRequired
from hunnu_harness.literature.models import (
    AccessDecision,
    AccessType,
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
)
from hunnu_harness.literature.normalization import stable_paper_id
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow

from literature_test_support import write_minimal_pdf


class _MockAdapter(LiteratureSourceAdapter):
    name = "MockSource"
    human_like_delay_seconds = 0

    def __init__(self, root: Path, *, mode: str = "success"):
        super().__init__(browser=None)
        self.root = root
        self.mode = mode
        self.download_calls = 0

    async def search(self, query, request):
        if self.mode == "login":
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=manual login; BrowserReadyForManualAction=true"
            )
        if self.mode == "no_results":
            return []
        return [
            LiteratureRecord(
                paper_id="SEARCH1",
                title="AI washing and audit monitoring",
                source_database=self.name,
                source_page="https://example.test/article/1",
                stable_identifier="1",
                search_query=query,
            )
        ]

    async def open_result(self, record):
        return None

    async def extract_metadata(self, *, search_query):
        return LiteratureRecord(
            paper_id=stable_paper_id(doi="10.1000/test"),
            title="AI washing and audit monitoring",
            authors=("A. Author",),
            year="2026",
            journal="Journal of Tests",
            doi="10.1000/test",
            abstract="Audit monitoring constrains AI washing and earnings management.",
            source_database=self.name,
            source_page="https://example.test/article/1",
            stable_identifier="1",
            search_query=search_query,
            publication_status=PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value,
        )

    async def extract_abstract(self):
        return "Audit monitoring constrains AI washing."

    async def check_fulltext_access(self):
        if self.mode == "unauthorized":
            return AccessDecision(
                False,
                AccessType.METADATA_ONLY,
                False,
                RunStatus.FULLTEXT_NOT_AUTHORIZED,
                "No PDF control",
            )
        return AccessDecision(
            True,
            AccessType.OPEN_ACCESS,
            True,
            RunStatus.SUCCESS,
            "Official PDF control",
        )

    async def download_fulltext(self, record, access):
        self.download_calls += 1
        path = self.root / "source.pdf"
        if self.mode == "invalid_pdf":
            path.write_text("<html>blocked</html>", encoding="utf-8")
        else:
            write_minimal_pdf(path)
        return path

    async def get_citation(self):
        return {"Title": "AI washing and audit monitoring"}


def request(*, require_full_text=True, max_downloads=1):
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": "AI washing and audit monitoring",
            "KeywordsEN": ["AI washing", "audit monitoring"],
            "MaxSearchResults": 3,
            "MaxResultsPerSource": 3,
            "MaxDownloads": max_downloads,
            "MaxDownloadsPerRun": max_downloads,
            "RequireFullText": require_full_text,
        }
    )


class LiteratureWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_run_downloads_validates_hashes_and_writes_all_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = LiteratureAcquisitionWorkflow(
                _MockAdapter(root),
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            )
            result = await workflow.run(request())
            self.assertEqual(result.status, RunStatus.SUCCESS)
            self.assertEqual(len(result.records), 1)
            self.assertEqual(len(result.downloads), 1)
            self.assertTrue(result.records[0].pdf_validation_passed)
            self.assertEqual(len(result.records[0].sha256), 64)
            manifest = json.loads((root / "run" / "DOWNLOAD_MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["Downloads"]), 1)
            paper_id = result.records[0].paper_id
            self.assertTrue((root / "library" / "papers" / f"{paper_id}.pdf").exists())
            self.assertEqual(manifest["Downloads"][0]["LibraryDisposition"], "NEW_PAPER")
            self.assertTrue((root / "run" / "downloads" / "archive").exists())
            for name in (
                "SEARCH_REQUEST.json",
                "SEARCH_QUERY_LOG.csv",
                "SEARCH_RESULTS.csv",
                "SCREENING_DECISIONS.csv",
                "DOWNLOAD_MANIFEST.json",
                "SHA256SUMS.txt",
                "RUN_AUDIT.md",
                "OBSIDIAN_HANDOFF_MANIFEST.json",
            ):
                self.assertTrue((root / "run" / name).exists(), name)

    async def test_unauthorized_fulltext_is_metadata_only_and_never_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = _MockAdapter(root, mode="unauthorized")
            workflow = LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            )
            result = await workflow.run(request())
            self.assertEqual(result.status, RunStatus.FULLTEXT_NOT_AUTHORIZED)
            self.assertEqual(adapter.download_calls, 0)
            self.assertEqual(result.downloads, [])
            manifest = json.loads((root / "run" / "DOWNLOAD_MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["Downloads"], [])

    async def test_no_results_still_preserves_complete_audit_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = LiteratureAcquisitionWorkflow(
                _MockAdapter(root, mode="no_results"),
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            )
            result = await workflow.run(request(require_full_text=False, max_downloads=0))
            self.assertEqual(result.status, RunStatus.NO_RESULTS)
            self.assertTrue((root / "run" / "SEARCH_QUERY_LOG.csv").exists())
            self.assertTrue((root / "run" / "RUN_AUDIT.md").exists())

    async def test_invalid_pdf_is_download_failure_not_success_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = LiteratureAcquisitionWorkflow(
                _MockAdapter(root, mode="invalid_pdf"),
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            )
            result = await workflow.run(request())
            self.assertEqual(result.status, RunStatus.PARTIAL_SUCCESS)
            self.assertEqual(result.downloads, [])
            self.assertEqual(result.records[0].error_status, RunStatus.DOWNLOAD_FAILED.value)
            manifest = json.loads((root / "run" / "DOWNLOAD_MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["Downloads"], [])

    async def test_login_required_stops_and_keeps_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = LiteratureAcquisitionWorkflow(
                _MockAdapter(root, mode="login"),
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            )
            result = await workflow.run(request())
            self.assertEqual(result.status, RunStatus.ACTION_REQUIRED_USER_LOGIN)
            self.assertIn("ACTION_REQUIRED_USER_LOGIN=true", result.action_required_reason)
            self.assertTrue((root / "run" / "RUN_AUDIT.md").exists())
            self.assertEqual(result.downloads, [])


if __name__ == "__main__":
    unittest.main()
