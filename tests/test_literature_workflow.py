import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from hunnu_harness.literature.adapters.base import LiteratureSourceAdapter, SourceActionRequired
from hunnu_harness.literature.models import (
    AccessDecision,
    AccessType,
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
    ScreeningDecision,
)
from hunnu_harness.literature.normalization import stable_paper_id
from hunnu_harness.literature.workflow import (
    LiteratureAcquisitionWorkflow,
    _is_fulltext_acquisition_candidate,
)

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


class _IdentityGateAdapter(LiteratureSourceAdapter):
    name = "IdentityGateFixture"
    human_like_delay_seconds = 0

    def __init__(
        self,
        root: Path,
        records: list[LiteratureRecord],
        *,
        accessible_ids: set[str] | None = None,
        authorized_ids: set[str] | None = None,
    ) -> None:
        super().__init__(browser=None)
        self.root = root
        self.records = records
        self.accessible_ids = accessible_ids or set()
        self.authorized_ids = authorized_ids if authorized_ids is not None else set(self.accessible_ids)
        self.active: LiteratureRecord | None = None
        self.open_calls: list[str] = []
        self.access_calls: list[str] = []
        self.download_calls: list[str] = []

    @property
    def fulltext_entry_calls(self) -> int:
        opened = Counter(self.open_calls)
        return sum(max(0, count - 1) for count in opened.values())

    async def search(self, query, request):
        return self.records

    async def open_result(self, record):
        self.active = record
        self.open_calls.append(record.paper_id)

    async def extract_metadata(self, *, search_query):
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

    async def extract_abstract(self):
        assert self.active is not None
        return self.active.abstract

    async def check_fulltext_access(self):
        assert self.active is not None
        paper_id = self.active.paper_id
        self.access_calls.append(paper_id)
        accessible = paper_id in self.accessible_ids
        authorized = paper_id in self.authorized_ids
        return AccessDecision(
            accessible,
            AccessType.OPEN_ACCESS if authorized else AccessType.METADATA_ONLY,
            authorized,
            RunStatus.SUCCESS if authorized else RunStatus.FULLTEXT_NOT_AUTHORIZED,
            "Fixture full-text access" if authorized else "Fixture has no authorized full text",
        )

    async def download_fulltext(self, record, access):
        self.download_calls.append(record.paper_id)
        return write_minimal_pdf(self.root / f"{record.paper_id}.pdf")

    async def get_citation(self):
        return {"Title": self.active.title if self.active is not None else "unknown"}


def identity_candidate(
    paper_id: str,
    title: str,
    *,
    authors: tuple[str, ...] = ("Unrelated Author",),
    year: str = "2025",
) -> LiteratureRecord:
    return LiteratureRecord(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        journal="Fixture Journal",
        doi=f"10.1000/{paper_id.casefold()}",
        abstract="Unrelated subject matter.",
        source_database=_IdentityGateAdapter.name,
        source_page=f"https://example.test/article/{paper_id}",
        stable_identifier=paper_id,
        publication_status=PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value,
    )


def identity_request(
    candidate_count: int,
    *,
    exact_titles: tuple[str, ...] = (),
    max_downloads: int = 1,
) -> LiteratureSearchRequest:
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": "Sullivan Wamba 2022 artificial intelligence global value chain resilience",
            "Authors": ["Sullivan", "Wamba"],
            "ExactTitles": list(exact_titles),
            "KeywordsEN": ["artificial intelligence", "global value chain", "resilience"],
            "YearStart": 2022,
            "YearEnd": 2022,
            "MaxSearchResults": candidate_count,
            "MaxResultsPerSource": candidate_count,
            "MaxDownloads": max_downloads,
            "MaxDownloadsPerRun": max_downloads,
            "RequireFullText": True,
        }
    )


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

    async def test_identity_locked_keep_candidate_reaches_authorization_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            title = "Confirmed target paper"
            candidate = identity_candidate(
                "LOCKED1",
                title,
                authors=("Sullivan", "Wamba"),
                year="2022",
            )
            adapter = _IdentityGateAdapter(root, [candidate], accessible_ids={candidate.paper_id})
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(identity_request(1, exact_titles=(title,)))

            self.assertEqual(result.records[0].screening_decision, ScreeningDecision.KEEP.value)
            self.assertTrue(result.records[0].target_identity_confirmed)
            self.assertTrue(_is_fulltext_acquisition_candidate(result.records[0]))
            self.assertEqual(adapter.fulltext_entry_calls, 1)
            self.assertEqual(adapter.download_calls, [candidate.paper_id])
            self.assertEqual(len(result.downloads), 1)

    async def test_maybe_only_candidate_never_enters_fulltext_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = identity_candidate("MAYBE1", "Unrelated correction notice")
            adapter = _IdentityGateAdapter(root, [candidate], accessible_ids={candidate.paper_id})
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(identity_request(1))

            self.assertEqual(result.records[0].screening_decision, ScreeningDecision.MAYBE.value)
            self.assertFalse(_is_fulltext_acquisition_candidate(result.records[0]))
            self.assertEqual(adapter.fulltext_entry_calls, 0)
            self.assertEqual(adapter.download_calls, [])
            self.assertEqual(result.downloads, [])
            self.assertEqual(result.status, RunStatus.NO_RESULTS)

    async def test_li03_four_candidates_three_maybe_have_zero_fulltext_invocations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = [
                identity_candidate("LI03R", "Finiteness of Bowen-Margulis-Sullivan Measures"),
                identity_candidate("LI03M1", "Author Correction: Adaptive evolution of marine diatoms"),
                identity_candidate("LI03M2", "Author Correction: HER2-low breast cancer", year="2026"),
                identity_candidate("LI03M3", "ACNP Annual Meeting: Author Index", year="2026"),
            ]
            accessible = {record.paper_id for record in candidates[1:]}
            adapter = _IdentityGateAdapter(root, candidates, accessible_ids=accessible)
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(identity_request(4))

            self.assertEqual(
                [record.screening_decision for record in result.records],
                [ScreeningDecision.REJECT.value] + [ScreeningDecision.MAYBE.value] * 3,
            )
            self.assertFalse(any(_is_fulltext_acquisition_candidate(record) for record in result.records))
            self.assertEqual(adapter.fulltext_entry_calls, 0)
            self.assertEqual(adapter.download_calls, [])
            self.assertEqual(result.downloads, [])
            self.assertEqual(result.status, RunStatus.NO_RESULTS)

    async def test_rejected_candidate_never_enters_fulltext_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = identity_candidate("REJECT1", "Unrelated historical note", year="1990")
            adapter = _IdentityGateAdapter(root, [candidate])
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(identity_request(1))

            self.assertEqual(result.records[0].screening_decision, ScreeningDecision.REJECT.value)
            self.assertFalse(_is_fulltext_acquisition_candidate(result.records[0]))
            self.assertEqual(adapter.fulltext_entry_calls, 0)
            self.assertEqual(adapter.download_calls, [])

    async def test_mixed_candidates_only_keep_enters_fulltext_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_title = "Confirmed Sullivan and Wamba target"
            candidates = [
                identity_candidate("MIXR", "Unrelated historical note", year="1990"),
                identity_candidate("MIXM1", "Unrelated correction notice"),
                identity_candidate(
                    "MIXK",
                    target_title,
                    authors=("Sullivan", "Wamba"),
                    year="2022",
                ),
                identity_candidate("MIXM2", "Another unrelated correction notice"),
            ]
            accessible = {record.paper_id for record in candidates}
            adapter = _IdentityGateAdapter(root, candidates, accessible_ids=accessible)
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(identity_request(4, exact_titles=(target_title,)))

            self.assertEqual(
                [record.paper_id for record in result.records if _is_fulltext_acquisition_candidate(record)],
                ["MIXK"],
            )
            self.assertEqual(adapter.fulltext_entry_calls, 1)
            self.assertEqual(adapter.download_calls, ["MIXK"])
            self.assertEqual([entry.paper_id for entry in result.downloads], ["MIXK"])

    async def test_multiple_explicit_locked_targets_preserve_bounded_multi_target_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            titles = ("Explicit target one", "Explicit target two")
            candidates = [
                identity_candidate("LOCK1", titles[0], authors=("Sullivan",), year="2022"),
                identity_candidate("LOCK2", titles[1], authors=("Wamba",), year="2022"),
            ]
            accessible = {record.paper_id for record in candidates}
            adapter = _IdentityGateAdapter(root, candidates, accessible_ids=accessible)
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(identity_request(2, exact_titles=titles, max_downloads=2))

            self.assertEqual(adapter.fulltext_entry_calls, 2)
            self.assertEqual(adapter.download_calls, ["LOCK1", "LOCK2"])
            self.assertEqual(len(result.downloads), 2)

    async def test_identity_lock_does_not_bypass_article_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            title = "Locked but unauthorized target"
            candidate = identity_candidate("NOAUTH", title, authors=("Sullivan", "Wamba"), year="2022")
            adapter = _IdentityGateAdapter(
                root,
                [candidate],
                accessible_ids={candidate.paper_id},
                authorized_ids=set(),
            )
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(identity_request(1, exact_titles=(title,)))

            self.assertEqual(adapter.fulltext_entry_calls, 1)
            self.assertEqual(adapter.download_calls, [])
            self.assertEqual(result.downloads, [])
            self.assertEqual(result.status, RunStatus.FULLTEXT_NOT_AUTHORIZED)

    async def test_unconfirmed_search_detail_identity_cannot_enter_fulltext(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            title = "Apparently exact but detail identity changed"
            search_record = identity_candidate("SEARCH", title, authors=("Sullivan", "Wamba"), year="2022")
            changed_record = identity_candidate("DETAIL", title, authors=("Sullivan", "Wamba"), year="2022")
            adapter = _IdentityGateAdapter(root, [search_record], accessible_ids={search_record.paper_id})

            async def mismatched_metadata(*, search_query):
                changed_record.search_query = search_query
                return changed_record

            adapter.extract_metadata = mismatched_metadata
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            ).run(identity_request(1, exact_titles=(title,)))

            self.assertFalse(result.records[0].target_identity_confirmed)
            self.assertFalse(_is_fulltext_acquisition_candidate(result.records[0]))
            self.assertEqual(adapter.fulltext_entry_calls, 0)
            self.assertEqual(adapter.download_calls, [])
            self.assertEqual(result.downloads, [])


if __name__ == "__main__":
    unittest.main()
