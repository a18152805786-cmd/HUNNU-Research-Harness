from __future__ import annotations

from pathlib import Path
import unittest

from hunnu_harness.literature.adapters.base import SourceActionRequired
from hunnu_harness.literature.adapters.springerlink import SpringerLinkAdapter
from hunnu_harness.literature.models import AccessType, PublicationStatus, RunStatus


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
ARTICLE_URL = "https://link.springer.com/article/10.1007/s11573-023-01162-8"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class SpringerLinkFixtureTests(unittest.TestCase):
    def test_search_results_are_bounded_and_deduplicated(self) -> None:
        records = SpringerLinkAdapter.parse_search_results_html(
            _fixture("springerlink_search.html"),
            query='"Accounting for the middle"',
            max_results=5,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].doi, "10.1007/s11573-023-01162-8")
        self.assertEqual(records[0].source_page, ARTICLE_URL)

    def test_metadata_normalizes_publication_fields(self) -> None:
        record = SpringerLinkAdapter.parse_article_html(
            _fixture("springerlink_article_open_access.html"),
            source_url=ARTICLE_URL + "?tracking=discarded",
            search_query="earnings management",
        )
        self.assertTrue(record.title.startswith("Accounting for the middle"))
        self.assertEqual(record.authors, ("Sebastian Wagener",))
        self.assertEqual(record.year, "2024")
        self.assertEqual(record.journal, "Journal of Business Economics")
        self.assertEqual(record.pages_or_article_number, "225-277")
        self.assertEqual(record.doi, "10.1007/s11573-023-01162-8")
        self.assertEqual(record.publication_status, PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value)
        self.assertEqual(record.source_page, ARTICLE_URL)

    def test_open_access_requires_official_enabled_pdf_control(self) -> None:
        decision = SpringerLinkAdapter.check_fulltext_access_html(
            _fixture("springerlink_article_open_access.html"),
            source_url=ARTICLE_URL,
        )
        self.assertTrue(decision.full_text_accessible)
        self.assertTrue(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.OPEN_ACCESS)
        self.assertEqual(decision.status, RunStatus.SUCCESS)
        self.assertEqual(
            decision.download_url,
            "https://link.springer.com/content/pdf/10.1007/s11573-023-01162-8.pdf",
        )

    def test_metadata_only_when_pdf_control_is_missing(self) -> None:
        html = _fixture("springerlink_article_open_access.html").replace(
            '<a href="/content/pdf/10.1007/s11573-023-01162-8.pdf">Download PDF</a>',
            "",
        )
        decision = SpringerLinkAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
        self.assertFalse(decision.full_text_accessible)
        self.assertFalse(decision.authorized_access)
        self.assertEqual(decision.access_type, AccessType.METADATA_ONLY)
        self.assertEqual(decision.status, RunStatus.FULLTEXT_NOT_AUTHORIZED)

    def test_stops_at_human_verification(self) -> None:
        with self.assertRaisesRegex(SourceActionRequired, "ACTION_REQUIRED_USER_LOGIN=true"):
            SpringerLinkAdapter.detect_interruption(
                "<html><body>Security verification: verify you are human</body></html>",
                url="https://link.springer.com/article/10.1007/example",
            )


if __name__ == "__main__":
    unittest.main()
