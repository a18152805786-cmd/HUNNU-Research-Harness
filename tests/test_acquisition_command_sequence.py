"""Command-sequence invariants for the acquisition workflow.

The defect these pin down: after the screening phase locked a target and
confirmed authorized access, the download phase re-navigated to that same
article before resolving the PDF control.  A redundant navigation is not
harmless -- it discards the page state the identity lock was established
against, and an Agent driving the browser is right to refuse it.

What must stay true is narrower than "never navigate again":

  * exactly one search per bounded query plan, never a second one after lock
  * no navigation at all when the browser is still on the locked target
  * a real re-navigation when the browser has moved -- to the locked target,
    never back to search
  * the PDF control still bound to the locked article identity
"""

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from hunnu_harness.browser.commands import (
    BrowserObservation,
    DownloadArtifact,
    DownloadCommand,
    NavigateCommand,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from hunnu_harness.literature.adapters.sciencedirect import ScienceDirectAdapter
from hunnu_harness.literature.models import LiteratureRecord, LiteratureSearchRequest, RunStatus
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow
from hunnu_harness.paths import TEMP_DIR

from literature_test_support import minimal_pdf_bytes

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "literature"
SEARCH_HTML = (FIXTURES / "sciencedirect_search.html").read_text(encoding="utf-8")
ARTICLE_HTML = (FIXTURES / "sciencedirect_article_authorized.html").read_text(encoding="utf-8")

FIXTURE_PII = "S1544612326004149"
FIXTURE_TITLE = (
    "The impact of AI washing on enterprises' access to bank loans: "
    "From the perspective of external governance"
)
ARTICLE_URL = f"https://www.sciencedirect.com/science/article/pii/{FIXTURE_PII}"
_ARTICLE_PATH = re.compile(r"/science/article/(?:abs/)?pii/([A-Za-z0-9]+)")


class RecordingBrowser:
    """A BrowserCommandPort that records commands instead of driving a browser."""

    def __init__(self, downloads_dir: Path, *, start_url: str = "about:blank"):
        self.commands: list[tuple[str, str]] = []
        self.url = start_url
        self.html = ""
        self.session = SessionHandle(value="test")
        self.page_handle = PageHandle(value="page-1", session=self.session)
        self.downloads_dir = downloads_dir

    # -- convenience views over the recording ------------------------------

    @property
    def navigations(self) -> list[str]:
        return [target for kind, target in self.commands if kind == "NavigateCommand"]

    @property
    def search_navigations(self) -> list[str]:
        return [url for url in self.navigations if "/search?" in url]

    @property
    def article_navigations(self) -> list[str]:
        return [url for url in self.navigations if _ARTICLE_PATH.search(urlsplit(url).path)]

    @property
    def downloads(self) -> list[str]:
        return [target for kind, target in self.commands if kind == "DownloadCommand"]

    def kinds_after_last_article_observe(self) -> list[str]:
        """Command kinds issued after the target page was last observed."""

        kinds = [kind for kind, _ in self.commands]
        return kinds

    def _observation(self) -> BrowserObservation:
        return BrowserObservation(
            session=self.session,
            page=self.page_handle,
            generation=1,
            url=self.url,
            title="fixture",
            html=self.html,
        )

    async def execute(self, command):
        if isinstance(command, NavigateCommand):
            self.commands.append(("NavigateCommand", command.url))
            self.url = command.url
            self.html = SEARCH_HTML if "/search?" in command.url else ARTICLE_HTML
            return self._observation()
        if isinstance(command, ObserveCommand):
            self.commands.append(("ObserveCommand", self.url))
            return self._observation()
        if isinstance(command, DownloadCommand):
            target = getattr(command.target, "css", "") or ""
            self.commands.append(("DownloadCommand", target))
            path = self.downloads_dir / (command.suggested_filename or "download.pdf")
            path.write_bytes(minimal_pdf_bytes())
            return DownloadArtifact.from_path(path, source_url=self.url, page=self.page_handle)
        raise AssertionError(f"unexpected command: {type(command).__name__}")


class WanderingBrowser(RecordingBrowser):
    """Simulates losing the locked page between screening and download."""

    def __init__(self, downloads_dir: Path, *, wander_to: str):
        super().__init__(downloads_dir)
        self.wander_to = wander_to
        self.wandered = False

    async def execute(self, command):
        # Once access has been confirmed on the article, drift elsewhere, as a
        # tab switch or a redirect would.
        if (
            isinstance(command, ObserveCommand)
            and not self.wandered
            and _ARTICLE_PATH.search(urlsplit(self.url).path)
            and sum(1 for kind, _ in self.commands if kind == "ObserveCommand") >= 3
        ):
            self.wandered = True
            self.url = self.wander_to
            self.html = ""
        return await super().execute(command)


def acquisition_request(max_downloads: int) -> LiteratureSearchRequest:
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": FIXTURE_TITLE,
            "ExactTitles": [FIXTURE_TITLE],
            "DOIs": ["10.1016/j.frl.2026.109884"],
            "MaxSearchResults": 1,
            "MaxResultsPerSource": 1,
            "MaxDownloads": max_downloads,
            "MaxDownloadsPerRun": max_downloads,
            "RequireFullText": max_downloads > 0,
        }
    )


def run_acquisition(browser_factory, *, max_downloads: int = 1):
    """Run the real workflow with the real adapter over the repository fixtures."""

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cmdseq-", dir=TEMP_DIR) as raw:
        tmp = Path(raw)
        browser = browser_factory(tmp)
        adapter = ScienceDirectAdapter(browser)
        # run_root under TEMP_DIR keeps the download manager on an isolated
        # library, so no test run can reach the frozen corpus or the taxonomy.
        workflow = LiteratureAcquisitionWorkflow(
            adapter,
            run_root=tmp / "run",
            human_like_delay_seconds=0.0,
            allow_outside_project_for_tests=True,
        )
        result = asyncio.run(workflow.run(acquisition_request(max_downloads)))
        return browser, result


class SearchIssuedOnceTests(unittest.TestCase):
    """A. one search per plan; never another one after the target is locked."""

    def test_exactly_one_search_navigation_for_one_bounded_plan(self):
        browser, result = run_acquisition(RecordingBrowser)
        self.assertEqual(len(browser.search_navigations), 1, browser.commands)
        self.assertEqual(len(result.records), 1)

    def test_no_search_navigation_after_the_target_page_is_reached(self):
        browser, _ = run_acquisition(RecordingBrowser)
        first_article = next(
            index
            for index, (kind, target) in enumerate(browser.commands)
            if kind == "NavigateCommand" and _ARTICLE_PATH.search(urlsplit(target).path)
        )
        later = browser.commands[first_article + 1 :]
        self.assertEqual(
            [target for kind, target in later if kind == "NavigateCommand" and "/search?" in target],
            [],
            later,
        )

    def test_the_same_search_url_is_never_issued_twice(self):
        browser, _ = run_acquisition(RecordingBrowser)
        self.assertEqual(len(browser.search_navigations), len(set(browser.search_navigations)))


class NoRenavigationAfterLockTests(unittest.TestCase):
    """B/C. locked + authorized -> the next command is not a navigation."""

    def test_article_is_navigated_to_exactly_once(self):
        browser, _ = run_acquisition(RecordingBrowser)
        self.assertEqual(len(browser.article_navigations), 1, browser.commands)

    def test_download_phase_issues_no_navigation_at_all(self):
        browser, _ = run_acquisition(RecordingBrowser)
        download_index = next(
            index for index, (kind, _) in enumerate(browser.commands) if kind == "DownloadCommand"
        )
        article_nav_index = next(
            index
            for index, (kind, target) in enumerate(browser.commands)
            if kind == "NavigateCommand" and _ARTICLE_PATH.search(urlsplit(target).path)
        )
        between = browser.commands[article_nav_index + 1 : download_index]
        self.assertEqual([kind for kind, _ in between if kind == "NavigateCommand"], [], between)

    def test_commands_between_lock_and_download_are_read_only(self):
        browser, _ = run_acquisition(RecordingBrowser)
        download_index = next(
            index for index, (kind, _) in enumerate(browser.commands) if kind == "DownloadCommand"
        )
        article_nav_index = next(
            index
            for index, (kind, target) in enumerate(browser.commands)
            if kind == "NavigateCommand" and _ARTICLE_PATH.search(urlsplit(target).path)
        )
        between = browser.commands[article_nav_index + 1 : download_index]
        self.assertTrue(between)
        self.assertEqual({kind for kind, _ in between}, {"ObserveCommand"}, between)

    def test_download_still_happens(self):
        browser, result = run_acquisition(RecordingBrowser)
        self.assertEqual(len(browser.downloads), 1)
        self.assertEqual(result.status, RunStatus.SUCCESS)
        self.assertEqual(len(result.downloads), 1)


class DownloadControlIdentityTests(unittest.TestCase):
    """I. the PDF control stays bound to the locked article identity."""

    def test_download_target_carries_the_locked_pii(self):
        browser, _ = run_acquisition(RecordingBrowser)
        self.assertIn(FIXTURE_PII, browser.downloads[0])

    def test_download_target_is_a_pdf_control_for_that_article(self):
        browser, _ = run_acquisition(RecordingBrowser)
        target = browser.downloads[0]
        self.assertIn("/pdf", target.casefold())
        self.assertEqual(len(set(_ARTICLE_PATH.findall(target))), 1)

    def test_a_pdf_control_for_another_article_is_refused(self):
        """Skipping the re-navigation must not weaken the binding check."""

        from hunnu_harness.literature.models import AccessDecision, AccessType, FullTextFormat

        temporary = tempfile.TemporaryDirectory(prefix="bind-", dir=TEMP_DIR)
        self.addCleanup(temporary.cleanup)
        adapter = ScienceDirectAdapter(RecordingBrowser(Path(temporary.name)))
        record = LiteratureRecord(paper_id="PTEST", source_page=ARTICLE_URL)
        access = AccessDecision(
            full_text_accessible=True,
            access_type=AccessType.OPEN_ACCESS,
            authorized_access=True,
            status=RunStatus.SUCCESS,
            reason="authorized",
            full_text_format=FullTextFormat.PDF,
            download_url="https://www.sciencedirect.com/science/article/pii/S9999999999999/pdfft",
        )
        with self.assertRaises(Exception) as caught:
            asyncio.run(adapter.download_fulltext(record, access))
        self.assertIn("not bound to the locked", str(caught.exception))


class RecoveryNavigationTests(unittest.TestCase):
    """F. a browser that really moved is re-navigated -- to the locked target."""

    def test_lost_page_triggers_one_re_navigation_to_the_locked_article(self):
        browser, result = run_acquisition(
            lambda tmp: WanderingBrowser(tmp, wander_to="https://www.sciencedirect.com/journal/other")
        )
        self.assertTrue(browser.wandered)
        self.assertEqual(len(browser.article_navigations), 2, browser.commands)
        self.assertTrue(all(FIXTURE_PII in url for url in browser.article_navigations))
        self.assertEqual(len(result.downloads), 1)

    def test_recovery_never_goes_back_to_search(self):
        browser, _ = run_acquisition(
            lambda tmp: WanderingBrowser(tmp, wander_to="https://www.sciencedirect.com/journal/other")
        )
        self.assertEqual(len(browser.search_navigations), 1, browser.search_navigations)


class CurrentTargetProbeTests(unittest.IsolatedAsyncioTestCase):
    """The probe itself: read-only, identity-based, and conservative."""

    def _record(self, url: str = ARTICLE_URL) -> LiteratureRecord:
        return LiteratureRecord(paper_id="PTEST", source_page=url)

    async def _probe(self, current_url: str, record_url: str = ARTICLE_URL) -> tuple[bool, list]:
        with tempfile.TemporaryDirectory(prefix="probe-", dir=TEMP_DIR) as raw:
            browser = RecordingBrowser(Path(raw), start_url=current_url)
            adapter = ScienceDirectAdapter(browser)
            matched = await adapter.current_target_matches(self._record(record_url))
            return matched, browser.commands

    async def test_same_article_matches(self):
        matched, _ = await self._probe(ARTICLE_URL)
        self.assertTrue(matched)

    async def test_probe_issues_no_navigation(self):
        _, commands = await self._probe(ARTICLE_URL)
        self.assertEqual([kind for kind, _ in commands], ["ObserveCommand"])

    async def test_abs_pii_variant_is_the_same_article(self):
        matched, _ = await self._probe(
            f"https://www.sciencedirect.com/science/article/abs/pii/{FIXTURE_PII}"
        )
        self.assertTrue(matched)

    async def test_query_string_and_fragment_are_ignored(self):
        matched, _ = await self._probe(f"{ARTICLE_URL}?via%3Dihub#sec1")
        self.assertTrue(matched)

    async def test_a_different_article_does_not_match(self):
        matched, _ = await self._probe(
            "https://www.sciencedirect.com/science/article/pii/S9999999999999"
        )
        self.assertFalse(matched)

    async def test_search_page_does_not_match(self):
        matched, _ = await self._probe("https://www.sciencedirect.com/search?qs=ai+washing")
        self.assertFalse(matched)

    async def test_foreign_host_does_not_match(self):
        matched, _ = await self._probe(
            f"https://evil.example.com/science/article/pii/{FIXTURE_PII}"
        )
        self.assertFalse(matched)

    async def test_blank_page_does_not_match(self):
        matched, _ = await self._probe("about:blank")
        self.assertFalse(matched)

    async def test_observation_failure_is_conservative(self):
        class BrokenBrowser(RecordingBrowser):
            async def execute(self, command):
                raise RuntimeError("observation unavailable")

        with tempfile.TemporaryDirectory(prefix="probe-", dir=TEMP_DIR) as raw:
            adapter = ScienceDirectAdapter(BrokenBrowser(Path(raw)))
            self.assertFalse(await adapter.current_target_matches(self._record()))

    async def test_record_without_a_stable_article_url_does_not_match(self):
        matched, _ = await self._probe(ARTICLE_URL, record_url="https://www.sciencedirect.com/")
        self.assertFalse(matched)


class AdapterContractDefaultTests(unittest.IsolatedAsyncioTestCase):
    """G. adapters that have not opted in keep the old re-navigation."""

    async def test_base_default_is_false(self):
        from hunnu_harness.literature.adapters.base import LiteratureSourceAdapter

        class MinimalAdapter(LiteratureSourceAdapter):
            name = "Minimal"

            async def search(self, query, request):
                return []

            async def open_result(self, record):
                return None

            async def extract_metadata(self, *, search_query):
                raise NotImplementedError

            async def extract_abstract(self):
                raise NotImplementedError

            async def check_fulltext_access(self):
                raise NotImplementedError

            async def download_fulltext(self, record, access):
                raise NotImplementedError

            async def get_citation(self):
                raise NotImplementedError

        adapter = MinimalAdapter(None)
        self.assertFalse(
            await adapter.current_target_matches(LiteratureRecord(paper_id="PTEST"))
        )


class NoDownloadRequestedTests(unittest.TestCase):
    """A run that asks for no download must stop after screening."""

    def test_zero_max_downloads_issues_no_download_and_no_second_navigation(self):
        browser, result = run_acquisition(RecordingBrowser, max_downloads=0)
        self.assertEqual(browser.downloads, [])
        self.assertEqual(len(browser.article_navigations), 1)
        self.assertEqual(len(browser.search_navigations), 1)
        self.assertEqual(len(result.downloads), 0)


if __name__ == "__main__":
    unittest.main()
