from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from hunnu_harness.literature.adapters.oxfordacademic import OxfordAcademicAdapter
from hunnu_harness.literature.models import LiteratureSearchRequest, RunStatus
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow
from hunnu_harness.paths import TEMP_DIR

from literature_test_support import minimal_pdf_with_text_bytes


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
TITLE = "Double/debiased machine learning for treatment and structural parameters"
DOI = "10.1111/ectj.12097"
ARTICLE_URL = "https://academic.oup.com/ectj/article/21/1/C1/5056401"
PDF_URL = "https://academic.oup.com/ectj/article-pdf/21/1/C1/27684918/ectj00c1.pdf"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _Emitter:
    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}

    def on(self, event: str, callback: Any) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        callbacks = self.listeners.get(event, [])
        if callback in callbacks:
            callbacks.remove(callback)

    def emit(self, event: str, value: Any) -> None:
        for callback in tuple(self.listeners.get(event, ())):
            callback(value)


class _Response:
    url = PDF_URL + "?opaque=REDACTED_TEST_VALUE"
    headers = {"content-type": "application/pdf", "cookie": "<REDACTED>"}

    async def body(self) -> bytes:
        return minimal_pdf_with_text_bytes(f"{TITLE} Victor Chernozhukov DOI: {DOI}")


class _Locator:
    def __init__(self, page: "_Page") -> None:
        self.page = page

    def filter(self, **_: Any) -> "_Locator":
        return self

    @property
    def first(self) -> "_Locator":
        return self

    async def click(self) -> None:
        self.page.context.emit("response", _Response())


class _Context(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.pages: list[_Page] = []


class _Page(_Emitter):
    def __init__(self, context: _Context) -> None:
        super().__init__()
        self.context = context
        self.url = "about:blank"
        self.html = ""
        context.pages.append(self)

    async def content(self) -> str:
        return self.html

    def locator(self, _: str) -> _Locator:
        return _Locator(self)


class _Browser:
    def __init__(self, downloads_dir: Path) -> None:
        self.context = _Context()
        self.page = _Page(self.context)
        self.downloads_dir = downloads_dir

    async def goto(self, url: str) -> None:
        if "search-results" in url:
            self.page.url = "https://academic.oup.com/search-results"
            self.page.html = fixture("oxfordacademic_search.html")
        else:
            self.page.url = ARTICLE_URL
            self.page.html = fixture("oxfordacademic_article_purchased.html")


class OxfordWorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_pdf_response_enters_existing_archive_pipeline(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="v026-oxford-workflow-", dir=TEMP_DIR) as temporary:
            run_root = Path(temporary) / "run"
            browser = _Browser(run_root / "downloads" / "staging")
            adapter = OxfordAcademicAdapter(browser)
            request = LiteratureSearchRequest.from_mapping(
                {
                    "OriginalResearchRequest": f"Find exact Oxford paper: {TITLE}",
                    "ResearchQuestion": TITLE,
                    "ExactTitles": [TITLE],
                    "DOIs": [DOI],
                    "KeywordsEN": ["double machine learning"],
                    "MaxSearchResults": 1,
                    "MaxResultsPerSource": 1,
                    "MaxDownloads": 1,
                    "MaxDownloadsPerRun": 1,
                    "RequireFullText": True,
                }
            )
            result = await LiteratureAcquisitionWorkflow(
                adapter,
                run_root=run_root,
                human_like_delay_seconds=0,
            ).run(request)
            manifest_text = (run_root / "DOWNLOAD_MANIFEST.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)

            self.assertEqual(result.status, RunStatus.SUCCESS)
            self.assertEqual(len(result.downloads), 1)
            entry = result.downloads[0]
            self.assertTrue(entry.file_validation_passed)
            self.assertTrue(entry.pdf_validation_passed)
            self.assertTrue(entry.target_identity_confirmed)
            self.assertTrue(entry.target_title_matched)
            self.assertTrue(entry.target_doi_matched)
            self.assertEqual(entry.acquisition_method, "AUTHORIZED_PDF_RESPONSE")
            self.assertTrue(entry.authorized_pdf_response_captured)
            self.assertTrue(Path(entry.local_path).exists())
            self.assertEqual(len(entry.sha256), 64)
            self.assertEqual(manifest["Downloads"][0]["SignedURLPersisted"], False)
            self.assertEqual(manifest["Downloads"][0]["QueryStringPersisted"], False)
            self.assertEqual(manifest["Downloads"][0]["AuthorizationHeaderPersisted"], False)
            self.assertEqual(manifest["Downloads"][0]["CookiePersisted"], False)
            self.assertNotIn("REDACTED_TEST_VALUE", manifest_text)
            self.assertNotIn("<REDACTED>", manifest_text)
            self.assertTrue((run_root / "SHA256SUMS.txt").read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
