import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from hunnu_harness.browser import DownloadCommand
from hunnu_harness.literature.adapters.base import SourceActionRequired, SourceLayoutChanged
from hunnu_harness.literature.adapters.sciencedirect import ScienceDirectAdapter
from hunnu_harness.literature.models import AccessType, LiteratureSearchRequest, PublicationStatus

from literature_test_support import isolated_fetch_ledger


FIXTURES = Path(__file__).parent / "fixtures" / "literature"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ScienceDirectFixtureTests(unittest.TestCase):
    def test_search_fixture_extracts_bounded_stable_results(self):
        records = ScienceDirectAdapter.parse_search_results_html(
            fixture("sciencedirect_search.html"),
            query='"AI washing"',
            max_results=1,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].stable_identifier, "S1544612326004149")
        self.assertEqual(records[0].source_database, "ScienceDirect")
        self.assertEqual(records[0].search_query, '"AI washing"')

    def test_article_fixture_extracts_required_metadata(self):
        record = ScienceDirectAdapter.parse_article_html(
            fixture("sciencedirect_article_authorized.html"),
            source_url="https://www.sciencedirect.com/science/article/pii/S1544612326004149?tracking=removed",
            search_query='"The impact of AI washing"',
        )
        self.assertTrue(record.title.startswith("The impact of AI washing"))
        self.assertEqual(record.authors, ("Weiqi Liu", "Meifang Li"))
        self.assertEqual(record.year, "2026")
        self.assertEqual(record.journal, "Finance Research Letters")
        self.assertEqual(record.volume, "98")
        self.assertEqual(record.pages_or_article_number, "109884")
        self.assertEqual(record.doi, "10.1016/j.frl.2026.109884")
        self.assertEqual(record.issn, "1544-6123")
        self.assertEqual(record.language, "en")
        self.assertEqual(record.publication_status, PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value)
        self.assertTrue(record.abstract_available)
        self.assertIn("Audit quality", record.keywords)
        self.assertNotIn("?", record.source_page)

    def test_authorized_fixture_requires_actual_pdf_control(self):
        access = ScienceDirectAdapter.check_fulltext_access_html(
            fixture("sciencedirect_article_authorized.html"),
            source_url="https://www.sciencedirect.com/science/article/pii/S1544612326004149",
        )
        self.assertTrue(access.full_text_accessible)
        self.assertTrue(access.authorized_access)
        self.assertEqual(access.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)
        self.assertIn("/pdfft", access.download_url)

    def test_abstract_visibility_does_not_imply_fulltext_access(self):
        access = ScienceDirectAdapter.check_fulltext_access_html(
            fixture("sciencedirect_article_metadata_only.html"),
            source_url="https://www.sciencedirect.com/science/article/pii/S0000000000000000",
        )
        self.assertFalse(access.full_text_accessible)
        self.assertFalse(access.authorized_access)
        self.assertEqual(access.access_type, AccessType.METADATA_ONLY)

    def test_login_fixture_stops_for_manual_action(self):
        with self.assertRaises(SourceActionRequired) as context:
            ScienceDirectAdapter.parse_article_html(
                fixture("sciencedirect_login.html"),
                source_url="https://www.sciencedirect.com/signin",
            )
        self.assertIn("ACTION_REQUIRED_USER_LOGIN=true", str(context.exception))
        self.assertIn("BrowserReadyForManualAction=true", str(context.exception))

    def test_captcha_fixture_stops_without_bypass(self):
        with self.assertRaises(SourceActionRequired) as context:
            ScienceDirectAdapter.parse_search_results_html(
                fixture("sciencedirect_captcha.html"),
                query="AI washing",
            )
        # Acquisition still stops, and nothing is bypassed.  Static HTML alone
        # cannot claim the user is looking at a rendered challenge, so the
        # verdict says the challenge text was not confirmed by an observation.
        error = context.exception
        self.assertFalse(error.challenge_visible)
        self.assertFalse(error.browser_ready_for_manual_action)
        self.assertEqual(error.reason.value, "CHALLENGE_TEXT_UNVERIFIED")

    def test_observed_visible_challenge_is_reported_as_visible(self):
        with self.assertRaises(SourceActionRequired) as context:
            ScienceDirectAdapter.detect_interruption(
                fixture("sciencedirect_captcha.html"),
                url="https://www.sciencedirect.com/search",
                observed=True,
                challenge_visible=True,
            )
        error = context.exception
        self.assertEqual(error.reason.value, "VISIBLE_CHALLENGE")
        self.assertTrue(error.challenge_observed)
        self.assertTrue(error.challenge_visible)
        self.assertTrue(error.challenge_blocking)

    def test_missing_fields_remain_unknown(self):
        record = ScienceDirectAdapter.parse_article_html(
            "<html><head><meta name='citation_title' content='Only a title'></head><body></body></html>",
            source_url="https://www.sciencedirect.com/science/article/pii/S0000000000000001",
        )
        self.assertEqual(record.doi, "unknown")
        self.assertEqual(record.year, "unknown")
        self.assertEqual(record.publication_status, PublicationStatus.UNKNOWN.value)

    def test_json_wrapped_browser_capture_is_supported(self):
        import json

        wrapped = json.dumps(fixture("sciencedirect_article_authorized.html"))
        record = ScienceDirectAdapter.parse_article_html(
            wrapped,
            source_url="https://www.sciencedirect.com/science/article/pii/S1544612326004149",
        )
        self.assertEqual(record.doi, "10.1016/j.frl.2026.109884")


class _FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.html = ""

    async def content(self):
        return self.html


class _FakeBrowser:
    def __init__(self):
        self.page = _FakePage()
        self.downloads_dir = FIXTURES

    async def goto(self, url: str):
        self.page.url = url
        if "/search" in url:
            self.page.html = fixture("sciencedirect_search.html")
        else:
            self.page.html = fixture("sciencedirect_article_authorized.html")


class ScienceDirectAdapterAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_contract_search_open_extract_and_access(self):
        adapter = ScienceDirectAdapter(_FakeBrowser())
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "exact AI washing article",
                "ExactTitles": ["The impact of AI washing on enterprises' access to bank loans"],
                "MaxSearchResults": 2,
                "MaxResultsPerSource": 2,
                "MaxDownloads": 0,
                "MaxDownloadsPerRun": 0,
            }
        )
        records = await adapter.search('"AI washing"', request)
        self.assertEqual(len(records), 2)
        await adapter.open_result(records[0])
        metadata = await adapter.extract_metadata(search_query='"AI washing"')
        access = await adapter.check_fulltext_access()
        self.assertEqual(metadata.doi, "10.1016/j.frl.2026.109884")
        self.assertTrue(access.authorized_access)

    async def test_authorized_pdf_download_uses_mcp_representable_css_target(self):
        class _DownloadBrowser:
            def __init__(self):
                self.command = None

            async def execute(self, command):
                self.command = command
                return SimpleNamespace(local_path=FIXTURES / "sciencedirect_article_authorized.html")

        browser = _DownloadBrowser()
        adapter = ScienceDirectAdapter(browser)
        record = ScienceDirectAdapter.parse_article_html(
            fixture("sciencedirect_article_authorized.html"),
            source_url="https://www.sciencedirect.com/science/article/pii/S1544612326004149",
        )
        access = ScienceDirectAdapter.check_fulltext_access_html(
            fixture("sciencedirect_article_authorized.html"),
            source_url=record.source_page,
        )

        with isolated_fetch_ledger() as ledger:
            adapter.fetch_ledger = ledger
            await adapter.download_fulltext(record, access)

        self.assertIsInstance(browser.command, DownloadCommand)
        self.assertIn("S1544612326004149", browser.command.target.css)
        self.assertIn("/pdfft", browser.command.target.css)
        self.assertNotEqual(browser.command.target.css, access.download_locator)
        self.assertIsNone(browser.command.target.text_regex)

    async def test_authorized_pdf_download_rejects_control_for_another_article(self):
        class _DownloadBrowser:
            async def execute(self, command):
                raise AssertionError("mismatched PDF control must not reach the browser")

        adapter = ScienceDirectAdapter(_DownloadBrowser())
        record = ScienceDirectAdapter.parse_article_html(
            fixture("sciencedirect_article_authorized.html"),
            source_url="https://www.sciencedirect.com/science/article/pii/S1544612326004149",
        )
        access = ScienceDirectAdapter.check_fulltext_access_html(
            fixture("sciencedirect_article_authorized.html"),
            source_url=record.source_page,
        )
        access = replace(
            access,
            download_url="https://www.sciencedirect.com/science/article/pii/S0000000000000000/pdfft",
        )

        with self.assertRaisesRegex(SourceLayoutChanged, "not bound"):
            await adapter.download_fulltext(record, access)


if __name__ == "__main__":
    unittest.main()
