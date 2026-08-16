import unittest

from hunnu_harness.literature.models import (
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    ScreeningDecision,
)
from hunnu_harness.literature.planning import LiteratureSearchPlanner
from hunnu_harness.literature.screening import LiteratureScreener


class LiteratureSearchRequestTests(unittest.TestCase):
    def test_mapping_accepts_required_pascal_case_fields(self):
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "AI washing and audit",
                "ResearchQuestion": "Does audit monitoring constrain AI washing?",
                "KeywordsCN": ["人工智能漂洗", "审计监督"],
                "KeywordsEN": ["AI washing", "audit monitoring"],
                "ExactTitles": ["An exact title"],
                "Authors": ["A. Author"],
                "DOIs": ["https://doi.org/10.1000/ABC"],
                "YearStart": 2023,
                "YearEnd": 2026,
                "PreferredLanguages": ["zh", "en"],
                "MaxSearchResults": 30,
                "MaxDownloads": 10,
                "MaxResultsPerSource": 20,
                "MaxDownloadsPerRun": 10,
                "RequireFullText": True,
            }
        )
        self.assertEqual(request.keywords_cn, ("人工智能漂洗", "审计监督"))
        self.assertEqual(request.year_start, 2023)
        self.assertEqual(request.max_downloads, 10)
        self.assertTrue(request.require_full_text)

    def test_request_requires_original_text_for_audit(self):
        with self.assertRaises(ValueError):
            LiteratureSearchRequest.from_mapping({"KeywordsEN": ["AI washing"]})

    def test_download_limit_is_clamped_to_per_run_guard(self):
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "bounded",
                "MaxDownloads": 10,
                "MaxDownloadsPerRun": 3,
            }
        )
        self.assertEqual(request.max_downloads, 3)

    def test_unbounded_result_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            LiteratureSearchRequest.from_mapping(
                {"OriginalResearchRequest": "too many", "MaxSearchResults": 1000}
            )

    def test_natural_language_parses_topics_recent_years_and_limits(self):
        request = LiteratureSearchRequest.from_natural_language(
            "中英文都找 AI washing、audit monitoring 和 earnings management，近 5 年，最多保留 30 篇，最多下载 10 篇全文。",
            current_year=2026,
        )
        self.assertEqual(request.year_start, 2022)
        self.assertEqual(request.year_end, 2026)
        self.assertEqual(request.max_search_results, 30)
        self.assertEqual(request.max_downloads, 10)
        self.assertEqual(request.preferred_languages, ("zh", "en"))
        self.assertIn("ai washing", request.keywords_en)

    def test_natural_language_no_download_is_enforced(self):
        request = LiteratureSearchRequest.from_natural_language(
            "围绕 AI washing 找候选清单，先不要下载。"
        )
        self.assertEqual(request.max_downloads, 0)
        self.assertFalse(request.require_full_text)


class LiteraturePlanningAndScreeningTests(unittest.TestCase):
    def test_query_planner_creates_bounded_requested_pairs(self):
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "AI washing audit earnings",
                "KeywordsEN": ["AI washing", "audit", "earnings management", "discretionary accruals"],
                "KeywordsCN": ["人工智能漂洗", "审计", "盈余管理", "应计"],
            }
        )
        plans = LiteratureSearchPlanner(max_queries=5).plan(request)
        self.assertLessEqual(len(plans), 5)
        self.assertEqual(plans[0].query, '"AI washing" AND "audit"')
        self.assertIn("人工智能漂洗 审计", [plan.query for plan in plans])

    def test_exact_doi_precedes_keyword_queries(self):
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "exact DOI",
                "DOIs": ["doi:10.1000/ABC"],
                "KeywordsEN": ["AI washing", "audit"],
            }
        )
        plans = LiteratureSearchPlanner().plan(request)
        self.assertEqual(plans[0].query, "10.1000/abc")

    def test_transparent_screening_keeps_direct_recent_journal_match(self):
        request = LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "AI washing audit",
                "KeywordsEN": ["AI washing", "audit"],
                "YearStart": 2023,
                "YearEnd": 2026,
            }
        )
        record = LiteratureRecord(
            paper_id="P1",
            title="AI washing and audit monitoring",
            abstract="Audit quality constrains AI washing.",
            year="2026",
            doi="10.1000/test",
            publication_status=PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value,
            full_text_accessible=True,
        )
        LiteratureScreener().screen(record, request)
        self.assertEqual(record.screening_decision, ScreeningDecision.KEEP.value)
        self.assertGreaterEqual(record.relevance_score, 60)
        self.assertFalse(record.ai_assisted)
        self.assertIn("(+", record.screening_reason)

    def test_screening_rejects_unrelated_result_but_preserves_record(self):
        request = LiteratureSearchRequest.from_mapping(
            {"OriginalResearchRequest": "AI washing", "KeywordsEN": ["AI washing"]}
        )
        record = LiteratureRecord(paper_id="P2", title="Marine biology field observations", year="1990")
        returned = LiteratureScreener().screen(record, request)
        self.assertIs(returned, record)
        self.assertEqual(record.screening_decision, ScreeningDecision.REJECT.value)
        self.assertIn("No requested topic term", record.screening_reason)


if __name__ == "__main__":
    unittest.main()
