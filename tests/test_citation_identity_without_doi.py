"""A PDF that prints no DOI can be identified by title, first author, volume and first page.

Older scans (JSTOR, Blackwell, early Elsevier) carry no DOI anywhere, so the
DOI rule alone quarantined every one of them even when the right article was
fetched. These tests pin the replacement evidence and, above all, what it must
still refuse: a printed DOI that disagrees, a contradicting title, or any one of
the four facts missing or wrong.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.adapters.oxfordacademic import OxfordAcademicAdapter
from hunnu_harness.literature.adapters.springerlink import SpringerLinkAdapter
from hunnu_harness.literature.fulltext import (
    DOIContentVerification,
    ExternalIdentityDecision,
    verify_external_paper_identity,
)
from hunnu_harness.literature.models import LiteratureRecord

from literature_test_support import write_minimal_pdf_with_text


TITLE = "Contract Design for Staged Project Finance"
DOI = "10.9999/synthetic.00042"
CITATION_LINE = "Synthetic Review of Studies (1992) 59, 473-494 (c) 1992 Synthetic Press"
AUTHORS = "ALICE EXAMPLE and BOB SAMPLE"


def record(**changes) -> LiteratureRecord:
    values = dict(
        paper_id="PSYNTH",
        source_database="test",
        title=TITLE,
        doi=DOI,
        authors=("Alice Example", "Bob Sample"),
        volume="59",
        pages_or_article_number="473-494",
    )
    values.update(changes)
    return LiteratureRecord(**values)


class CitationIdentityWithoutDOITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def pdf(self, *lines: str, name: str = "article.pdf") -> Path:
        return write_minimal_pdf_with_text(self.root / name, "\n".join(lines))

    def test_title_author_volume_and_first_page_verify_a_pdf_that_prints_no_doi(self):
        path = self.pdf(CITATION_LINE, TITLE, AUTHORS)
        proof = verify_external_paper_identity(path, record())
        self.assertEqual(proof.decision, ExternalIdentityDecision.VERIFIED)
        self.assertEqual(proof.doi_verification, DOIContentVerification.NOT_AVAILABLE)
        self.assertEqual(proof.citation_verification, "matched")
        self.assertTrue(proof.doi_or_citation_matched)
        self.assertIn("prints no DOI", proof.reason)

    def test_common_citation_line_shapes_are_recognised(self):
        for line in (
            "Synthetic Review of Studies (1992) 59.473-494",
            "Journal of Synthetic Economics 59 (1992) 473}494",
            "Vol. 59, pp. 473-494",
        ):
            with self.subTest(line=line):
                path = self.pdf(line, TITLE, AUTHORS, name="shape.pdf")
                self.assertTrue(verify_external_paper_identity(path, record()).verified)

    def test_surname_first_metadata_is_read_as_the_surname(self):
        path = self.pdf(CITATION_LINE, TITLE, AUTHORS)
        proof = verify_external_paper_identity(path, record(authors=("Example, Alice",)))
        self.assertTrue(proof.verified)

    def test_any_wrong_or_missing_fact_leaves_the_pdf_unverified(self):
        cases = {
            "other volume": dict(volume="58"),
            "other first page": dict(pages_or_article_number="474-494"),
            "other first author": dict(authors=("Carol Other",)),
            "no volume": dict(volume=""),
            "no pages": dict(pages_or_article_number=""),
            "article number, not a page": dict(pages_or_article_number="e12"),
            "no authors": dict(authors=()),
        }
        path = self.pdf(CITATION_LINE, TITLE, AUTHORS)
        for label, changes in cases.items():
            with self.subTest(label):
                proof = verify_external_paper_identity(path, record(**changes))
                self.assertFalse(proof.verified)
                self.assertFalse(proof.doi_or_citation_matched)

    def test_volume_and_page_must_sit_together_not_anywhere_on_the_page(self):
        path = self.pdf(
            "Synthetic Review of Studies volume 59",
            TITLE,
            AUTHORS,
            "Received in revised form after 473 days of review",
        )
        self.assertFalse(verify_external_paper_identity(path, record()).verified)

    def test_digits_inside_longer_numbers_do_not_count(self):
        path = self.pdf("Synthetic Review (1992) 159, 4730-4940", TITLE, AUTHORS)
        self.assertFalse(verify_external_paper_identity(path, record()).verified)

    def test_a_printed_doi_that_disagrees_still_stops_the_pdf(self):
        path = self.pdf(CITATION_LINE, TITLE, AUTHORS, "doi:10.9999/synthetic.99999")
        proof = verify_external_paper_identity(path, record())
        self.assertEqual(proof.decision, ExternalIdentityDecision.CONFLICT)
        self.assertNotEqual(proof.citation_verification, "matched")

    def test_a_contradicting_title_still_stops_the_pdf(self):
        path = self.pdf(CITATION_LINE, "A Wholly Unrelated Survey of Bond Markets", AUTHORS)
        proof = verify_external_paper_identity(path, record())
        self.assertFalse(proof.verified)

    def test_without_a_claimed_doi_the_old_title_rule_is_unchanged(self):
        path = self.pdf(TITLE, AUTHORS)
        proof = verify_external_paper_identity(path, record(doi=""))
        self.assertEqual(proof.doi_verification, DOIContentVerification.NOT_SUPPLIED)
        self.assertEqual(proof.citation_verification, "not_attempted")

    def test_adapters_accept_the_citation_route_only_through_the_shared_proof(self):
        good = self.pdf(CITATION_LINE, TITLE, AUTHORS, name="good.pdf")
        bad = self.pdf("Synthetic Review (1992) 58, 473-494", TITLE, AUTHORS, name="bad.pdf")
        self.assertTrue(OxfordAcademicAdapter.validate_pdf_identity(good, record()).confirmed)
        self.assertFalse(OxfordAcademicAdapter.validate_pdf_identity(bad, record()).confirmed)
        self.assertTrue(SpringerLinkAdapter.validate_pdf_identity(good, record()).confirmed)
        self.assertFalse(SpringerLinkAdapter.validate_pdf_identity(bad, record()).confirmed)


if __name__ == "__main__":
    unittest.main()
