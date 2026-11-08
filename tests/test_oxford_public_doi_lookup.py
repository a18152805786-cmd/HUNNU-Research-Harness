"""Exact DOI lookup uses public metadata and still verifies Oxford itself.

The port explicitly distinguishes the JSON metadata surface from the publisher
HTML. A generic Navigate cannot manufacture the expected article fixture.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import quote, urlsplit

from hunnu_harness.browser.commands import (
    BrowserObservation, NavigateCommand, ObserveCommand, PageHandle, SessionHandle,
)
from hunnu_harness.literature.adapters.base import SourceActionRequired, SourceLayoutChanged
from hunnu_harness.literature.adapters.oxfordacademic import OxfordAcademicAdapter
from hunnu_harness.literature.institutional import InstitutionalResolutionTrigger, InstitutionalRouteResult
from hunnu_harness.literature.models import LiteratureSearchRequest
from hunnu_harness.paths import TEMP_DIR


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
DOI = "10.1111/ectj.12097"
TITLE = "Double/debiased machine learning for treatment and structural parameters"
ARTICLE = "https://academic.oup.com/ectj/article/21/1/C1/5056401"


def payload(doi=DOI, title=TITLE, landing=ARTICLE):
    return {
        "status": "ok", "message-type": "work", "message": {
            "DOI": doi, "title": [title],
            "URL": "https://doi.org/" + doi,
            "resource": {"primary": {"URL": landing}},
            "link": [{"URL": "https://academic.oup.com/ectj/article-pdf/21/1/C1/27684918/ectj00c1.pdf"}],
        },
    }


def request(doi=DOI):
    return LiteratureSearchRequest.from_mapping({
        "OriginalResearchRequest": "Exact DOI metadata-only acceptance",
        "DOIs": [doi], "MaxSearchResults": 3, "MaxResultsPerSource": 3,
        "MaxDownloads": 0, "MaxDownloadsPerRun": 0,
    })


class MetadataPort:
    def __init__(self, *, metadata=None, article_html=None, article_url=ARTICLE):
        self.downloads_dir = TEMP_DIR
        self.session = SessionHandle("oxford-doi-metadata")
        self.page_handle = PageHandle("main", session=self.session)
        self.commands = []
        self.metadata_text = json.dumps(payload() if metadata is None else metadata)
        self.api_final_url = None
        self.article_url = article_url
        self.article_final_url = None
        self.article_html = article_html or (FIXTURES / "oxfordacademic_article_purchased.html").read_text(encoding="utf-8")
        self.mode = "blank"
        self.url = "about:blank"

    async def execute(self, command):
        self.commands.append(command)
        if isinstance(command, NavigateCommand):
            if urlsplit(command.url).hostname == "api.crossref.org":
                self.mode, self.url = "metadata", self.api_final_url or command.url
            elif command.url == self.article_url or (
                command.url.startswith("https://yclib.hunnu.edu.cn/vpn/")
                and command.url.split("?", 1)[0].endswith(urlsplit(self.article_url).path)
            ):
                self.mode, self.url = "article", self.article_final_url or command.url
            elif urlsplit(command.url).path.endswith("/search-results"):
                self.mode, self.url = "search", command.url
            else:
                raise AssertionError("Unexpected navigation outside configured fixture surfaces")
        elif not isinstance(command, ObserveCommand):
            raise AssertionError("Metadata lookup must never click, fetch or download")
        if self.mode == "metadata":
            if isinstance(command, ObserveCommand):
                assert not command.include_html and command.include_visible_text
            return BrowserObservation(
                self.session, self.page_handle, 0, self.url,
                title="Public Crossref work metadata", visible_text=self.metadata_text,
            )
        html = (
            self.article_html if self.mode == "article"
            else (FIXTURES / "oxfordacademic_search.html").read_text(encoding="utf-8")
        )
        return BrowserObservation(self.session, self.page_handle, 0, self.url, html=html)

    @property
    def navigations(self):
        return [command.url for command in self.commands if isinstance(command, NavigateCommand)]


class OxfordPublicDOILookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_doi_resolves_only_metadata_then_verifies_oxford_article(self):
        port = MetadataPort()
        records = await OxfordAcademicAdapter(port).search(DOI, request())
        self.assertEqual(port.navigations, ["https://api.crossref.org/works/" + quote(DOI, safe=""), ARTICLE])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].doi, DOI)
        self.assertEqual(records[0].title, TITLE)
        self.assertTrue(records[0].target_identity_confirmed)

    async def test_lookup_is_not_hardcoded_to_one_acceptance_paper(self):
        doi = "10.1093/rfs/hhab001"
        title = "A generic corporate finance article"
        landing = "https://academic.oup.com/rfs/article/35/1/123/5000001"
        html = (FIXTURES / "oxfordacademic_article_purchased.html").read_text(encoding="utf-8")
        html = html.replace(DOI, doi).replace(TITLE, title)
        port = MetadataPort(metadata=payload(doi, title, landing), article_html=html, article_url=landing)
        records = await OxfordAcademicAdapter(port).search(doi, request(doi))
        self.assertEqual(records[0].doi, doi)
        self.assertEqual(port.navigations, ["https://api.crossref.org/works/" + quote(doi, safe=""), landing])

    async def test_normal_topic_query_does_not_use_the_metadata_api(self):
        port = MetadataPort()
        records = await OxfordAcademicAdapter(port).search(TITLE, request())
        self.assertEqual(len(records), 1)
        self.assertEqual(len(port.navigations), 1)
        self.assertIn("/search-results?q=", port.navigations[0])

    async def test_wrong_or_unconfirmed_work_metadata_cannot_navigate_to_oxford(self):
        cases = [[], {"status": "error"}, {"status": "ok", "message": "not found"}]
        wrong = payload("10.1111/ectj.other")
        wrong["message"]["reference"] = [{"DOI": DOI}]
        cases.append(wrong)
        malformed = payload()
        malformed["message"]["DOI"] = "a cited identifier " + DOI
        cases.append(malformed)
        for malformed_doi in ([DOI], DOI + "?other=value", DOI + "#reference"):
            malformed = payload()
            malformed["message"]["DOI"] = malformed_doi
            cases.append(malformed)
        for field, value in (("title", []), ("title", [TITLE, "Another title"]), ("resource", {})):
            item = payload()
            item["message"][field] = value
            cases.append(item)
        for item in cases:
            with self.subTest(item=item):
                port = MetadataPort(metadata=item)
                with self.assertRaises(SourceLayoutChanged):
                    await OxfordAcademicAdapter(port).search(DOI, request())
                self.assertEqual(len(port.navigations), 1)

    async def test_primary_url_rejects_pdf_signed_nonpublisher_and_browser_normalization_escapes(self):
        bad = (
            "https://doi.org/" + DOI,
            ARTICLE.replace("academic.oup.com", "academic.oup.com.evil.example"),
            ARTICLE.replace("https://", "http://"),
            ARTICLE.replace("academic.oup.com", "name:password@academic.oup.com"),
            ARTICLE + "?token=secret", ARTICLE + "#fragment",
            "https://academic.oup.com/ectj/article-pdf/21/1/C1/27684918/ectj00c1.pdf",
            "https://academic.oup.com/ectj/article/21/../article-pdf/5056401",
            "https://academic.oup.com/ectj/article/21/%2e%2e/C1/5056401",
            "https://academic.oup.com/ectj/article/21/1%2f..%2farticle-pdf/C1/5056401",
            "https://academic.oup.com/ectj/article/21/1\\..\\article-pdf/C1/5056401",
            "https://academic.oup.com/ectj/article/21/1/C1.pdf/5056401",
            ARTICLE + "\n", ARTICLE.replace("/21/", "/.//"),
        )
        for landing in bad:
            with self.subTest(landing=landing):
                port = MetadataPort(metadata=payload(landing=landing))
                with self.assertRaises(SourceLayoutChanged):
                    await OxfordAcademicAdapter(port).search(DOI, request())
                self.assertEqual(len(port.navigations), 1, "bad primary URL must be rejected before browser navigation")

    async def test_api_redirect_or_wrong_doi_endpoint_has_no_fallback(self):
        for url in (
            "https://evil.example/works/" + DOI,
            "https://api.crossref.org/works/10.1111/other",
            "https://api.crossref.org/works/" + DOI + "?token=secret",
        ):
            with self.subTest(url=url):
                port = MetadataPort()
                port.api_final_url = url
                with self.assertRaises(SourceLayoutChanged):
                    await OxfordAcademicAdapter(port).search(DOI, request())
                self.assertEqual(len(port.navigations), 1)

    async def test_unreadable_or_script_wrapped_json_is_not_executed_or_replaced_by_a_provider(self):
        for text in (None, "", "not JSON", "<script>" + json.dumps(payload()) + "</script>"):
            with self.subTest(text=text):
                port = MetadataPort()
                port.metadata_text = text
                with self.assertRaises(SourceLayoutChanged):
                    await OxfordAcademicAdapter(port).search(DOI, request())
                self.assertEqual(len(port.navigations), 1)

    async def test_metadata_api_human_gate_stops_without_retry(self):
        port = MetadataPort()
        port.metadata_text = "<html><title>Security verification</title>Verify you are human</html>"
        with self.assertRaises(SourceActionRequired):
            await OxfordAcademicAdapter(port).search(DOI, request())
        self.assertEqual(len(port.navigations), 1)

    async def test_oxford_own_doi_title_and_stable_id_must_all_match(self):
        original = (FIXTURES / "oxfordacademic_article_purchased.html").read_text(encoding="utf-8")
        wrong_doi = original.replace(DOI, "10.1111/ectj.other") + f'<div>Reference: {DOI}</div>'
        cases = (
            (wrong_doi, None), (original.replace(TITLE, "A different article"), None),
            (original, ARTICLE.replace("5056401", "5056402")),
            (original, "https://academic.oup.com/ectj"),
        )
        for html, final_url in cases:
            with self.subTest(final_url=final_url, html=html[:80]):
                port = MetadataPort(article_html=html)
                port.article_final_url = final_url
                with self.assertRaises(SourceLayoutChanged):
                    await OxfordAcademicAdapter(port).search(DOI, request())
                self.assertEqual(len(port.navigations), 2)

    async def test_fresh_identity_proof_reuses_the_just_verified_navigation_once(self):
        port = MetadataPort()
        adapter = OxfordAcademicAdapter(port)
        record = (await adapter.search(DOI, request()))[0]
        observations_before = sum(isinstance(command, ObserveCommand) for command in port.commands)
        await adapter.open_result(record)
        self.assertEqual(len(port.navigations), 2)
        self.assertGreater(sum(isinstance(command, ObserveCommand) for command in port.commands), observations_before)
        metadata = await adapter.extract_metadata(search_query=DOI)
        self.assertTrue(metadata.target_identity_confirmed)

    async def test_reuse_cannot_hide_a_changed_page_or_human_gate(self):
        for html in (
            "<html><title>Security verification</title>Verify you are human</html>",
            (FIXTURES / "oxfordacademic_article_purchased.html").read_text(encoding="utf-8").replace(DOI, "10.1111/ectj.other"),
        ):
            port = MetadataPort()
            adapter = OxfordAcademicAdapter(port)
            record = (await adapter.search(DOI, request()))[0]
            port.article_html = html
            with self.assertRaises((SourceActionRequired, SourceLayoutChanged)):
                await adapter.open_result(record)
            self.assertEqual(len(port.navigations), 2)

    async def test_verified_institutional_gateway_still_uses_existing_article_rewrite(self):
        port = MetadataPort()
        route = InstitutionalRouteResult(
            requested_source="OxfordAcademic", resolution_trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
            institutional_route_resolved=True, institutional_target_database_match=True,
            publisher_navigation_url="https://yclib.hunnu.edu.cn/vpn/983/https/OXFORD/",
        )
        records = await OxfordAcademicAdapter(port, institutional_route=route).search(DOI, request())
        self.assertTrue(records[0].target_identity_confirmed)
        self.assertTrue(port.navigations[1].startswith("https://yclib.hunnu.edu.cn/vpn/"))


if __name__ == "__main__":
    unittest.main()
