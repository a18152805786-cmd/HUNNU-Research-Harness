import unittest

from hunnu_harness.literature.dedupe import LiteratureDeduplicator
from hunnu_harness.literature.models import UNKNOWN, LiteratureRecord, PublicationStatus
from hunnu_harness.literature.normalization import (
    normalize_doi,
    normalize_title,
    normalized_pdf_filename,
    stable_paper_id,
)


class LiteratureNormalizationTests(unittest.TestCase):
    def test_doi_normalization_removes_resolver_and_prefix(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1016/J.FRL.2026.109884"), "10.1016/j.frl.2026.109884")
        self.assertEqual(normalize_doi("doi: 10.1000/ABC."), "10.1000/abc")

    def test_invalid_doi_is_unknown_not_guessed(self):
        self.assertEqual(normalize_doi("not-a-doi"), "unknown")

    def test_title_normalization_uses_unicode_case_and_punctuation(self):
        left = normalize_title("ＡＩ Washing： Audit—Monitoring")
        right = normalize_title("ai washing audit monitoring")
        self.assertEqual(left, right)

    def test_stable_paper_id_prefers_doi(self):
        left = stable_paper_id(doi="https://doi.org/10.1000/ABC", title="One")
        right = stable_paper_id(doi="10.1000/abc", title="Different")
        self.assertEqual(left, right)

    def test_normalized_filename_is_windows_safe(self):
        record = LiteratureRecord(
            paper_id="P1",
            title='AI washing: effects / audit? "evidence"',
            authors=("Weiqi Liu",),
            year="2026",
        )
        filename = normalized_pdf_filename(record)
        self.assertTrue(filename.startswith("2026_Liu_"))
        self.assertTrue(filename.endswith(".pdf"))
        self.assertNotRegex(filename, r'[<>:"/\\|?*]')


class LiteratureDeduplicationTests(unittest.TestCase):
    def test_doi_exact_match_is_primary_duplicate_reason(self):
        records = [
            LiteratureRecord(paper_id="P1", title="Title A", doi="10.1000/ABC", year="2025"),
            LiteratureRecord(paper_id="P2", title="Title B", doi="https://doi.org/10.1000/abc", year="2026"),
        ]
        LiteratureDeduplicator().deduplicate(records)
        duplicate = next(record for record in records if record.duplicate_detected)
        self.assertEqual(duplicate.duplicate_reason, "DOI exact match")

    def test_normalized_title_and_year_detect_duplicate(self):
        records = [
            LiteratureRecord(paper_id="P1", title="AI Washing: Evidence", year="2025"),
            LiteratureRecord(paper_id="P2", title="ai washing — evidence", year="2025"),
        ]
        LiteratureDeduplicator().deduplicate(records)
        self.assertEqual(sum(record.duplicate_detected for record in records), 1)
        duplicate = next(record for record in records if record.duplicate_detected)
        self.assertEqual(duplicate.duplicate_reason, "normalized title + year")

    def test_title_and_first_author_detect_duplicate_without_year(self):
        records = [
            LiteratureRecord(paper_id="P1", title="Same Work", authors=("A. Smith",)),
            LiteratureRecord(paper_id="P2", title="Same Work", authors=("A Smith",)),
        ]
        LiteratureDeduplicator().deduplicate(records)
        self.assertEqual(sum(record.duplicate_detected for record in records), 1)

    def test_sha256_detects_identical_file(self):
        records = [
            LiteratureRecord(paper_id="P1", title="One", sha256="a" * 64),
            LiteratureRecord(paper_id="P2", title="Two", sha256="a" * 64),
        ]
        LiteratureDeduplicator().deduplicate(records)
        duplicate = next(record for record in records if record.duplicate_detected)
        self.assertEqual(duplicate.duplicate_reason, "SHA256 identical file")

    def test_formal_version_becomes_canonical_and_alternate_is_retained(self):
        preprint = LiteratureRecord(
            paper_id="PRE",
            title="Same Work",
            authors=("A. Smith",),
            year="2025",
            publication_status=PublicationStatus.PREPRINT.value,
        )
        final = LiteratureRecord(
            paper_id="FINAL",
            title="Same Work",
            authors=("A. Smith",),
            year="2025",
            publication_status=PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value,
        )
        LiteratureDeduplicator().deduplicate([preprint, final])
        self.assertFalse(final.duplicate_detected)
        self.assertEqual(preprint.canonical_paper_id, "FINAL")
        self.assertTrue(preprint.same_work_different_version)
        self.assertTrue(preprint.archived_as_alternate_version)

    @staticmethod
    def _newspaper_article(paper_id: str, doi: str, **fields) -> LiteratureRecord:
        return LiteratureRecord(
            paper_id=paper_id,
            title="春耕生产正当时",
            year="2026",
            doi=doi,
            publication_status=PublicationStatus.OTHER.value,
            **fields,
        )

    def test_one_headline_under_two_dois_of_one_status_is_two_works(self):
        # If this fails, two newspaper articles printed under one headline on the same day
        # are merged again, and the second can never be downloaded: it is a "duplicate".
        daily = self._newspaper_article("DAILY", "10.99999/n.cnki.fixture.2026.000011")
        evening = self._newspaper_article("EVENING", "10.99999/n.cnki.fixture.2026.000012")
        LiteratureDeduplicator().deduplicate([daily, evening])
        self.assertEqual([record.duplicate_detected for record in (daily, evening)], [False, False])
        self.assertEqual((daily.canonical_paper_id, evening.canonical_paper_id), ("DAILY", "EVENING"))

    def test_a_shared_first_author_does_not_join_two_dois_of_one_status_either(self):
        # If this fails, the title + first author key merges what the DOIs keep apart.
        first = self._newspaper_article("FIRST", "10.1000/one", authors=("作者甲",))
        second = self._newspaper_article("SECOND", "10.1000/two", authors=("作者甲",))
        second.year = "2025"
        LiteratureDeduplicator().deduplicate([first, second])
        self.assertFalse(second.duplicate_detected)

    def test_a_record_without_a_doi_cannot_link_two_different_dois_into_one_work(self):
        # If this fails, whether two different works stay apart depends on the order in
        # which a third record with the same title but no DOI was found.
        orders = {
            "no DOI first": ("NONE", "ONE", "TWO"),
            "no DOI between": ("ONE", "NONE", "TWO"),
            "no DOI last": ("ONE", "TWO", "NONE"),
        }
        for label, order in orders.items():
            with self.subTest(order=label):
                records = {
                    "NONE": self._newspaper_article("NONE", UNKNOWN),
                    "ONE": self._newspaper_article("ONE", "10.1000/one"),
                    "TWO": self._newspaper_article("TWO", "10.1000/two"),
                }
                LiteratureDeduplicator().deduplicate([records[paper_id] for paper_id in order])
                self.assertNotEqual(records["ONE"].canonical_paper_id, records["TWO"].canonical_paper_id)
                self.assertEqual(
                    sum(record.duplicate_detected for record in records.values()),
                    1,
                    "the record without a DOI still joins one of them by title and year",
                )

    def test_a_preprint_and_its_journal_version_stay_one_work_under_different_dois(self):
        # If this fails, the DOI rule reached past one publication status and split a
        # preprint from its journal version, which routinely carry different DOIs.
        preprint = LiteratureRecord(
            paper_id="PRE",
            title="Same Work",
            authors=("A. Smith",),
            year="2025",
            doi="10.1000/preprint.1",
            publication_status=PublicationStatus.PREPRINT.value,
        )
        final = LiteratureRecord(
            paper_id="FINAL",
            title="Same Work",
            authors=("A. Smith",),
            year="2025",
            doi="10.1000/journal.1",
            publication_status=PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value,
        )
        LiteratureDeduplicator().deduplicate([preprint, final])
        self.assertFalse(final.duplicate_detected)
        self.assertEqual(preprint.canonical_paper_id, "FINAL")
        self.assertTrue(preprint.same_work_different_version)


if __name__ == "__main__":
    unittest.main()
