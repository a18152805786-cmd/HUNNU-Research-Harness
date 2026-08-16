from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.adapters.base import SourceActionRequired, SourceLayoutChanged
from hunnu_harness.literature.adapters.oxfordacademic import OxfordAcademicAdapter
from hunnu_harness.literature.institutional import (
    HUNNUInstitutionalAccessResolver,
    InstitutionalResolutionTrigger,
    InstitutionalRouteResult,
)
from hunnu_harness.literature.models import AccessType, LiteratureSearchRequest, PublicationStatus
from hunnu_harness.paths import TEMP_DIR

from literature_test_support import write_minimal_pdf_with_text


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
ARTICLE_URL = "https://academic.oup.com/ectj/article/21/1/C1/5056401"
PDF_URL = "https://academic.oup.com/ectj/article-pdf/21/1/C1/27684918/ectj00c1.pdf"
TITLE = "Double/debiased machine learning for treatment and structural parameters"
DOI = "10.1111/ectj.12097"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class OxfordAcademicFixtureTests(unittest.TestCase):
    def test_all_required_source_aliases_resolve(self) -> None:
        for alias in (
            "Oxford Academic",
            "OxfordAcademic",
            "Oxford Journals",
            "Oxford Journals Collection",
            "OUP",
            "Oxford University Press",
        ):
            self.assertEqual(
                HUNNUInstitutionalAccessResolver.canonical_source(alias),
                "OxfordAcademic",
            )

    def test_search_results_are_bounded_and_deduplicated(self) -> None:
        records = OxfordAcademicAdapter.parse_search_results_html(
            fixture("oxfordacademic_search.html"),
            query=TITLE,
            max_results=3,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].stable_identifier, "5056401")
        self.assertEqual(records[0].source_page, ARTICLE_URL)
        self.assertEqual(records[0].navigation_url, ARTICLE_URL)

    def test_gateway_results_require_a_verified_institutional_route(self) -> None:
        gateway = "https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE/ectj/article/21/1/C1/5056401"
        html = f'<a href="{gateway}">{TITLE}</a>'
        self.assertEqual(
            OxfordAcademicAdapter.parse_search_results_html(
                html,
                query=TITLE,
                source_url="https://yclib.hunnu.edu.cn/vpn/",
            ),
            [],
        )
        records = OxfordAcademicAdapter.parse_search_results_html(
            html,
            query=TITLE,
            source_url="https://yclib.hunnu.edu.cn/vpn/",
            institutional_route_verified=True,
        )
        self.assertEqual(records[0].source_page, ARTICLE_URL)
        self.assertEqual(records[0].navigation_url, gateway)

    def test_verified_gateway_rewrites_relative_article_and_pdf_links_in_memory_only(self) -> None:
        search_source = (
            "https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE-OXFORD-ROUTE/search-results"
            "?opaque=REDACTED_TEST_VALUE"
        )
        records = OxfordAcademicAdapter.parse_search_results_html(
            fixture("oxfordacademic_search.html"),
            query=TITLE,
            source_url=search_source,
            institutional_route_verified=True,
        )
        self.assertEqual(records[0].source_page, ARTICLE_URL)
        self.assertTrue(records[0].navigation_url.startswith("https://yclib.hunnu.edu.cn/vpn/"))
        gateway_article = (
            "https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE-OXFORD-ROUTE"
            "/ectj/article/21/1/C1/5056401?opaque=REDACTED_TEST_VALUE"
        )
        access = OxfordAcademicAdapter.check_fulltext_access_html(
            fixture("oxfordacademic_article_purchased.html"),
            source_url=gateway_article,
            institutional_route_verified=True,
        )
        self.assertTrue(access.authorized_access)
        self.assertTrue(access.download_url.startswith("https://yclib.hunnu.edu.cn/vpn/"))

    def test_metadata_extracts_stable_article_fields(self) -> None:
        record = OxfordAcademicAdapter.parse_article_html(
            fixture("oxfordacademic_article_purchased.html"),
            source_url=ARTICLE_URL + "?opaque=REDACTED_TEST_VALUE",
            search_query=TITLE,
        )
        self.assertEqual(record.title, TITLE)
        self.assertEqual(record.authors[0], "Victor Chernozhukov")
        self.assertEqual(record.year, "2018")
        self.assertEqual(record.journal, "The Econometrics Journal")
        self.assertEqual(record.volume, "21")
        self.assertEqual(record.issue, "1")
        self.assertEqual(record.pages_or_article_number, "C1-C68")
        self.assertEqual(record.doi, DOI)
        self.assertEqual(record.stable_identifier, "5056401")
        self.assertEqual(record.source_page, ARTICLE_URL)
        self.assertEqual(record.publication_status, PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value)

    def test_purchased_is_authorized_institutional_access(self) -> None:
        access = OxfordAcademicAdapter.check_fulltext_access_html(
            fixture("oxfordacademic_article_purchased.html"),
            source_url=ARTICLE_URL,
        )
        self.assertTrue(access.full_text_accessible)
        self.assertTrue(access.authorized_access)
        self.assertEqual(access.access_type, AccessType.INSTITUTIONAL_AUTHENTICATED)
        self.assertIn("Purchased", access.reason)
        self.assertEqual(access.download_url, PDF_URL)

    def test_open_access_is_distinguished(self) -> None:
        html = fixture("oxfordacademic_article_purchased.html").replace("Purchased", "Open Access")
        access = OxfordAcademicAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertEqual(access.access_type, AccessType.OPEN_ACCESS)
        self.assertIn("OpenAccess", access.reason)

    def test_not_accessible_is_distinguished_from_unknown(self) -> None:
        html = fixture("oxfordacademic_article_purchased.html")
        html = html.replace('<a class="article-pdfLink" href="/ectj/article-pdf/21/1/C1/27684918/ectj00c1.pdf">PDF</a>', "")
        denied = OxfordAcademicAdapter.check_fulltext_access_html(
            html.replace("Purchased", "You do not have access. Get access"),
            source_url=ARTICLE_URL,
        )
        unknown = OxfordAcademicAdapter.check_fulltext_access_html(
            html.replace("Purchased", "Article metadata"),
            source_url=ARTICLE_URL,
        )
        self.assertEqual(denied.access_type, AccessType.METADATA_ONLY)
        self.assertIn("NotAccessible", denied.reason)
        self.assertEqual(unknown.access_type, AccessType.UNKNOWN)
        self.assertIn("Unknown", unknown.reason)

    def test_supplement_is_not_accepted_as_main_article_pdf(self) -> None:
        html = fixture("oxfordacademic_article_purchased.html")
        html = html.replace('<a class="article-pdfLink" href="/ectj/article-pdf/21/1/C1/27684918/ectj00c1.pdf">PDF</a>', "")
        access = OxfordAcademicAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertFalse(access.authorized_access)

    def test_pdf_action_must_match_article_level_citation_pdf_metadata(self) -> None:
        html = fixture("oxfordacademic_article_purchased.html").replace(
            "/ectj/article-pdf/21/1/C1/27684918/ectj00c1.pdf",
            "/ectj/article-pdf/21/1/C1/27684918/wrong-paper.pdf",
            1,
        )
        access = OxfordAcademicAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertFalse(access.authorized_access)

    def test_identity_lock_rejects_wrong_doi_title_or_stable_identifier(self) -> None:
        expected = OxfordAcademicAdapter.parse_search_results_html(
            fixture("oxfordacademic_search.html"), query=TITLE
        )[0]
        actual = OxfordAcademicAdapter.parse_article_html(
            fixture("oxfordacademic_article_purchased.html"), source_url=ARTICLE_URL
        )
        self.assertTrue(OxfordAcademicAdapter.identity_matches(expected, actual))
        actual.doi = "10.0000/wrong"
        expected.doi = DOI
        self.assertFalse(OxfordAcademicAdapter.identity_matches(expected, actual))

    def test_local_pdf_identity_requires_doi_or_title_plus_author(self) -> None:
        record = OxfordAcademicAdapter.parse_article_html(
            fixture("oxfordacademic_article_purchased.html"), source_url=ARTICLE_URL
        )
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="v026-oxford-id-", dir=TEMP_DIR) as temporary:
            matched_path = write_minimal_pdf_with_text(
                Path(temporary) / "matched.pdf",
                f"{TITLE} Victor Chernozhukov DOI: {DOI}",
            )
            wrong_path = write_minimal_pdf_with_text(
                Path(temporary) / "wrong.pdf",
                "An unrelated paper by Nobody DOI: 10.0000/wrong",
            )
            matched = OxfordAcademicAdapter.validate_pdf_identity(matched_path, record)
            wrong = OxfordAcademicAdapter.validate_pdf_identity(wrong_path, record)
        self.assertTrue(matched.confirmed)
        self.assertTrue(matched.title_matched)
        self.assertTrue(matched.doi_matched)
        self.assertFalse(wrong.confirmed)

    def test_human_verification_stops_for_manual_action(self) -> None:
        with self.assertRaisesRegex(SourceActionRequired, "ACTION_REQUIRED_USER_LOGIN=true"):
            OxfordAcademicAdapter.detect_interruption(
                "<html><title>Security verification</title><body>Verify you are human</body></html>",
                url=ARTICLE_URL,
            )


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.html = ""

    async def content(self) -> str:
        return self.html


class _FakeBrowser:
    def __init__(self) -> None:
        self.page = _FakePage()

    async def goto(self, url: str) -> None:
        self.page.url = url
        self.page.html = (
            fixture("oxfordacademic_search.html")
            if "search-results" in url
            else fixture("oxfordacademic_article_purchased.html")
        )


class OxfordAcademicAsyncContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_detail_access_contract_locks_target_identity(self) -> None:
        adapter = OxfordAcademicAdapter(_FakeBrowser())
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": f"Find exact Oxford paper: {TITLE}",
                "ExactTitles": [TITLE],
                "DOIs": [DOI],
                "MaxSearchResults": 1,
                "MaxResultsPerSource": 1,
                "MaxDownloads": 0,
                "MaxDownloadsPerRun": 0,
            }
        )
        records = await adapter.search(TITLE, request)
        await adapter.open_result(records[0])
        metadata = await adapter.extract_metadata(search_query=TITLE)
        access = await adapter.check_fulltext_access()
        self.assertTrue(metadata.target_identity_confirmed)
        self.assertEqual(metadata.doi, DOI)
        self.assertTrue(access.authorized_access)

    async def test_access_check_before_identity_lock_is_rejected(self) -> None:
        adapter = OxfordAcademicAdapter(_FakeBrowser())
        with self.assertRaises(SourceLayoutChanged):
            await adapter.check_fulltext_access()


if __name__ == "__main__":
    unittest.main()
