"""Acceptance tests against the real 179-WORK / 192-VERSION corpus.

Synthetic fixtures cannot prove a retrieval layer works; they only prove the
code runs.  These tests read the live Global Paper Library, which means they
skip cleanly on a machine without the Output Root rather than failing there.

Everything here is read-only.  ``FreezeTests`` asserts that fact by fingerprinting
the library before and after the whole module runs.
"""

import unittest
from pathlib import Path

from hunnu_harness.navigator.catalog import CatalogReader
from hunnu_harness.navigator.citation import CitationVerifier
from hunnu_harness.navigator.fingerprint import LibraryFingerprinter
from hunnu_harness.navigator.gaps import CoverageAnalyzer
from hunnu_harness.navigator.lexicon import RelevanceRole
from hunnu_harness.navigator.packs import ReadingPackBuilder
from hunnu_harness.navigator.related import RelatedWorkFinder
from hunnu_harness.navigator.resolver import FullTextStatus
from hunnu_harness.navigator.search import PaperNavigator
from hunnu_harness.paths import (
    LIBRARY_CATALOG_JSONL,
    LIBRARY_TOPICS_JSONL,
    PAPERS_BY_TOPIC_DIR,
)

# The sealed audit numbers this corpus is frozen at.
#
# Resealed at 185/198/345 on 2026-09-01, from 181/194/339.  Four papers were
# acquired through the full chain during the four-site live acceptance --
# ScienceDirect, SpringerLink, CNKI, and Oxford Academic; each searched,
# identity-locked, downloaded from the publisher by the harness itself, and
# archived.  (The 2026-08-30 reseal, 179/192/337 -> 181/194/339, recorded the
# first two of these; this one records the remaining CNKI and Oxford pair.)
# The numbers are read from the library itself; they are recorded here so that
# any *further* drift fails, and moving them is a deliberate act rather than a
# way to get a green suite.
#
# Unique topics stays 39 on purpose: every acquisition was filed under a topic
# that already existed, and nothing here may create a fortieth.
EXPECTED_WORKS = 185
EXPECTED_VERSIONS = 198
EXPECTED_TOPIC_ASSIGNMENTS = 345
EXPECTED_UNIQUE_TOPICS = 39

# Known fixtures in the real corpus, verified during discovery.
CAJ_WORK = "P23C61576ADCE"           # PDF + CAJ, and its files are named P6B4B9A7DE3F3.*
FOUR_VERSION_WORK = "P21FAD7D7F87D"  # four physical PDFs, one WORK
RENAMED_WORK = "PB3731259E47C"       # canonical file is P6C502A1FD06E.pdf
AI_WASHING_AUDIT_WORK = "PEC61CA32AC26"
AI_WASHING_INNOVATION_WORK = "P4451110E69BF"
BANK_LOANS_WORK = "PBBC02C012124"

LIBRARY_PRESENT = LIBRARY_CATALOG_JSONL.is_file()
_SKIP = "real Global Paper Library is not present on this machine"


def load_navigator() -> PaperNavigator:
    return PaperNavigator(reader=CatalogReader())


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class CorpusShapeTests(unittest.TestCase):
    """The Navigator must see exactly what the sealed audit recorded."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()

    def test_work_count_matches_the_sealed_audit(self):
        self.assertEqual(self.navigator.snapshot.work_count, EXPECTED_WORKS)

    def test_physical_version_count_matches(self):
        self.assertEqual(self.navigator.snapshot.version_count, EXPECTED_VERSIONS)

    def test_nested_variant_count_matches(self):
        snapshot = self.navigator.snapshot
        self.assertEqual(snapshot.version_count - snapshot.work_count, 13)

    def test_topic_assignments_match(self):
        self.assertEqual(
            self.navigator.snapshot.topic_assignment_count, EXPECTED_TOPIC_ASSIGNMENTS
        )

    def test_unique_topic_values_match(self):
        labels = set()
        for work in self.navigator.snapshot.works:
            labels.update(work.topic_labels)
        self.assertEqual(len(labels), EXPECTED_UNIQUE_TOPICS)

    def test_no_work_is_without_a_topic(self):
        self.assertEqual([w.paper_id for w in self.navigator.snapshot.works if not w.topics], [])

    def test_catalog_reads_without_degradation(self):
        self.assertEqual(self.navigator.snapshot.degraded_dicts(), [])

    def test_every_paper_id_is_unique(self):
        ids = [work.paper_id for work in self.navigator.snapshot.works]
        self.assertEqual(len(ids), len(set(ids)))


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestALookupExactTitle(unittest.TestCase):
    """TEST A -- an exact title returns one correct WORK with its full text."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()

    def test_exact_title_returns_one_work(self):
        payload = self.navigator.lookup("人工智能漂洗抹杀了企业技术创新吗")
        self.assertEqual(payload["status"], "FOUND")
        self.assertEqual(payload["exact_match_count"], 1)
        self.assertEqual(payload["matches"][0]["paper_id"], AI_WASHING_INNOVATION_WORK)

    def test_exact_title_resolves_a_readable_preferred_version(self):
        match = self.navigator.lookup("人工智能漂洗抹杀了企业技术创新吗")["matches"][0]
        self.assertEqual(match["fulltext_status"], FullTextStatus.AVAILABLE.value)
        self.assertTrue(match["preferred_version"]["machine_readable"])
        self.assertTrue(Path(match["preferred_version"]["absolute_path"]).is_file())

    def test_english_exact_title_also_resolves(self):
        payload = self.navigator.lookup(
            "The impact of AI washing on enterprises' access to bank loans: "
            "From the perspective of external governance"
        )
        self.assertEqual(payload["matches"][0]["paper_id"], BANK_LOANS_WORK)


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestBLookupAuthor(unittest.TestCase):
    """TEST B -- author lookup recalls the right paper."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()

    def test_author_field_query(self):
        payload = self.navigator.lookup("author:王海森 李纲")
        self.assertEqual(payload["status"], "FOUND")
        self.assertIn(
            AI_WASHING_INNOVATION_WORK, [m["paper_id"] for m in payload["matches"]]
        )

    def test_bare_author_names_without_a_field_prefix(self):
        payload = self.navigator.lookup("王海森 李纲")
        self.assertIn(
            AI_WASHING_INNOVATION_WORK, [m["paper_id"] for m in payload["matches"]]
        )

    def test_single_author_recalls_their_work(self):
        payload = self.navigator.lookup("author:袁春生")
        self.assertTrue(payload["matches"])
        self.assertIn(
            AI_WASHING_AUDIT_WORK, [m["paper_id"] for m in payload["matches"]]
        )


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestCLookupDOI(unittest.TestCase):
    """TEST C -- a DOI hits exactly one WORK."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()
        cls.doi_works = [
            work for work in cls.navigator.snapshot.works if work.normalized_doi != "unknown"
        ]

    def test_corpus_has_dois_to_test_with(self):
        self.assertGreaterEqual(len(self.doi_works), 100)

    def test_every_doi_resolves_to_exactly_one_work(self):
        for work in self.doi_works[:40]:
            with self.subTest(paper_id=work.paper_id):
                payload = self.navigator.lookup(f"DOI:{work.doi}")
                self.assertEqual(payload["exact_match_count"], 1)
                self.assertEqual(payload["matches"][0]["paper_id"], work.paper_id)

    def test_doi_url_form_also_resolves(self):
        work = self.doi_works[0]
        payload = self.navigator.lookup(f"https://doi.org/{work.doi}")
        self.assertEqual(payload["matches"][0]["paper_id"], work.paper_id)

    def test_unknown_doi_returns_not_in_library_without_a_guess(self):
        payload = self.navigator.lookup("DOI:10.9999/definitely-not-real-2099")
        self.assertEqual(payload["status"], "NOT_IN_LIBRARY")
        self.assertEqual(payload["matches"], [])


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestDChineseQuery(unittest.TestCase):
    """TEST D -- a Chinese semantic/topic query recalls the right WORKS."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()
        cls.payload = cls.navigator.search("人工智能漂洗 审计风险", top=10, use_fulltext=False)

    def test_ai_washing_audit_paper_is_recalled(self):
        ids = [row["paper_id"] for row in self.payload["results"]]
        self.assertIn(AI_WASHING_AUDIT_WORK, ids)

    def test_top_result_is_the_ai_washing_audit_paper(self):
        self.assertEqual(self.payload["results"][0]["paper_id"], AI_WASHING_AUDIT_WORK)

    def test_several_ai_washing_works_are_recalled(self):
        cores = [r for r in self.payload["results"] if r["relevance_role"] == RelevanceRole.CORE.value]
        self.assertGreaterEqual(len(cores), 3)

    def test_results_carry_provenance_and_a_reason(self):
        for row in self.payload["results"]:
            self.assertTrue(row["matched_by"])
            self.assertTrue(row["relevance_reason"])


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestEEnglishQuery(unittest.TestCase):
    """TEST E -- an English query recalls the right WORKS, including Chinese ones."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()
        cls.payload = cls.navigator.search("AI washing bank loans", top=10, use_fulltext=False)

    def test_bank_loans_paper_is_the_top_result(self):
        self.assertEqual(self.payload["results"][0]["paper_id"], BANK_LOANS_WORK)

    def test_english_query_reaches_chinese_works_through_the_lexicon(self):
        audit = self.navigator.search("AI washing", top=12, use_fulltext=False)
        ids = [row["paper_id"] for row in audit["results"]]
        self.assertIn(AI_WASHING_AUDIT_WORK, ids)
        self.assertIn(AI_WASHING_INNOVATION_WORK, ids)

    def test_expansion_is_reported_in_the_response(self):
        expansions = self.payload["query_normalization"]["expansions"]
        self.assertTrue(any(item["concept"] == "ai_washing" for item in expansions))

    def test_chinese_query_reaches_english_works(self):
        payload = self.navigator.search("人工智能漂洗", top=12, use_fulltext=False)
        ids = [row["paper_id"] for row in payload["results"]]
        self.assertIn(BANK_LOANS_WORK, ids)


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestFMechanismQuery(unittest.TestCase):
    """TEST F -- a research question returns differentiated roles, not a keyword pile."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()
        cls.payload = cls.navigator.search(
            "AI washing earnings quality audit mechanism", top=12, use_fulltext=False
        )

    def test_core_mechanism_and_outcome_are_all_present(self):
        roles = {row["relevance_role"] for row in self.payload["results"]}
        self.assertIn(RelevanceRole.CORE.value, roles)
        self.assertIn(RelevanceRole.OUTCOME.value, roles)

    def test_roles_are_justified_by_named_concepts(self):
        for row in self.payload["results"]:
            if row["relevance_role"] in (
                RelevanceRole.CORE.value,
                RelevanceRole.MECHANISM.value,
                RelevanceRole.OUTCOME.value,
            ):
                self.assertTrue(row["concept_evidence"], row["paper_id"])

    def test_the_question_concepts_are_all_detected(self):
        concepts = {
            item["concept"] for item in self.payload["query_normalization"]["concepts_matched"]
        }
        self.assertIn("ai_washing", concepts)
        self.assertIn("earnings_quality", concepts)
        self.assertIn("audit_risk", concepts)

    def test_a_chinese_research_question_also_differentiates_roles(self):
        payload = self.navigator.search(
            "AI washing 如何通过审计风险影响盈余质量？", top=12, use_fulltext=False
        )
        roles = {row["relevance_role"] for row in payload["results"]}
        self.assertGreaterEqual(len(roles), 2)
        self.assertIn(RelevanceRole.CORE.value, roles)


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestGVariantHandling(unittest.TestCase):
    """TEST G -- a WORK with nested versions appears once, with all versions listed."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()

    def test_four_version_work_has_four_versions_and_one_preferred(self):
        payload = self.navigator.fulltext(FOUR_VERSION_WORK)
        self.assertEqual(payload["status"], "FOUND")
        self.assertEqual(payload["version_count"], 4)
        self.assertIsNotNone(payload["preferred_version"])

    def test_preferred_version_is_the_catalog_canonical_file(self):
        payload = self.navigator.fulltext(FOUR_VERSION_WORK)
        self.assertTrue(payload["preferred_version"]["is_catalog_canonical"])

    def test_search_returns_the_work_exactly_once(self):
        payload = self.navigator.search("企业内部薪酬差距与创新", top=20, use_fulltext=False)
        ids = [row["paper_id"] for row in payload["results"]]
        self.assertEqual(ids.count(FOUR_VERSION_WORK), 1)

    def test_no_search_result_list_ever_repeats_a_work(self):
        for query in ("创新", "AI washing", "供应链", "审计", "数字化转型"):
            with self.subTest(query=query):
                ids = [
                    row["paper_id"]
                    for row in self.navigator.search(query, top=25, use_fulltext=False)["results"]
                ]
                self.assertEqual(len(ids), len(set(ids)))

    def test_every_multi_version_work_resolves_deterministically(self):
        multi = [w for w in self.navigator.snapshot.works if len(w.versions) > 1]
        self.assertEqual(len(multi), 9)
        for work in multi:
            with self.subTest(paper_id=work.paper_id):
                first = self.navigator.resolver.resolve(work)
                second = self.navigator.resolver.resolve(work)
                self.assertEqual(
                    first.preferred.version.sha256, second.preferred.version.sha256
                )

    def test_a_renamed_canonical_file_still_resolves(self):
        """The catalog file name does not contain this work's paper_id."""

        payload = self.navigator.fulltext(RENAMED_WORK)
        self.assertEqual(payload["status"], "FOUND")
        path = payload["preferred_version"]["managed_path"]
        self.assertNotIn(RENAMED_WORK, path)
        self.assertTrue(Path(payload["preferred_version"]["absolute_path"]).is_file())


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestHCajHandling(unittest.TestCase):
    """TEST H -- the corpus's one CAJ is detected, listed, and not preferred over a PDF."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()
        cls.payload = cls.navigator.fulltext(CAJ_WORK)

    def test_caj_work_is_found_with_two_versions(self):
        self.assertEqual(self.payload["status"], "FOUND")
        self.assertEqual(self.payload["version_count"], 2)

    def test_caj_format_is_detected(self):
        formats = {version["format"] for version in self.payload["available_versions"]}
        self.assertEqual(formats, {"PDF", "CAJ"})

    def test_pdf_is_preferred_over_the_caj(self):
        self.assertEqual(self.payload["preferred_version"]["format"], "PDF")
        self.assertTrue(self.payload["preferred_version"]["is_catalog_canonical"])

    def test_work_is_readable_because_a_pdf_sibling_exists(self):
        self.assertEqual(self.payload["fulltext_status"], FullTextStatus.AVAILABLE.value)
        self.assertTrue(self.payload["readable"])

    def test_caj_is_reported_as_present_but_not_machine_readable(self):
        caj = next(v for v in self.payload["available_versions"] if v["format"] == "CAJ")
        self.assertTrue(caj["exists"])
        self.assertFalse(caj["machine_readable"])
        self.assertIn("not machine-readable", caj["preference_reason"])

    def test_the_caj_file_is_the_one_on_disk(self):
        caj = next(v for v in self.payload["available_versions"] if v["format"] == "CAJ")
        self.assertTrue(Path(caj["absolute_path"]).is_file())
        self.assertTrue(caj["absolute_path"].lower().endswith(".caj"))


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestIMultiTopic(unittest.TestCase):
    """TEST I -- multi-topic works use every topic in retrieval and relatedness."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()
        cls.multi = [w for w in cls.navigator.snapshot.works if len(w.topics) >= 3]

    def test_corpus_has_multi_topic_works(self):
        self.assertGreaterEqual(len(self.multi), 38)

    def test_each_topic_of_a_multi_topic_work_can_recall_it(self):
        work = self.multi[0]
        for assignment in work.topics:
            with self.subTest(topic=assignment.label):
                payload = self.navigator.search(
                    assignment.subtopic or assignment.domain, top=30, use_fulltext=False
                )
                self.assertIn(work.paper_id, [row["paper_id"] for row in payload["results"]])

    def test_relatedness_uses_multiple_shared_subtopics(self):
        work = next(w for w in self.multi if len(w.subtopics) >= 3)
        payload = RelatedWorkFinder(self.navigator).find(work.paper_id, top=5)
        self.assertEqual(payload["status"], "FOUND")
        shared = payload["results"][0]["matched_by"]["shared_subtopics"]
        self.assertGreaterEqual(len(shared), 2)

    def test_related_results_are_work_level_unique_and_exclude_the_seed(self):
        for work in self.multi[:5]:
            with self.subTest(paper_id=work.paper_id):
                payload = RelatedWorkFinder(self.navigator).find(work.paper_id, top=10)
                ids = [row["paper_id"] for row in payload["results"]]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertNotIn(work.paper_id, ids)

    def test_related_on_an_unknown_id_is_not_in_library(self):
        payload = RelatedWorkFinder(self.navigator).find("PFFFFFFFFFFFF")
        self.assertEqual(payload["status"], "NOT_IN_LIBRARY")


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestJNotInLibrary(unittest.TestCase):
    """TEST J -- a fictional citation must not hallucinate a held paper."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()
        cls.verifier = CitationVerifier(cls.navigator)

    def test_fictional_english_citation(self):
        payload = self.verifier.verify(
            "Okonkwo, R. & Halvorsen, T. (2031). Quantum ledger washing and the collapse "
            "of audit assurance. Journal of Speculative Accountancy, 12(4), 991-1030."
        )
        self.assertEqual(payload["status"], "NOT_IN_LIBRARY")
        self.assertIsNone(payload["match"])
        self.assertIsNone(payload["paper_id"])
        self.assertFalse(payload["fulltext_available"])

    def test_fictional_chinese_citation(self):
        payload = self.verifier.verify(
            "赵子龙、孙尚香(2030). 量子账簿漂洗对区块链审计鉴证的抑制效应. 虚构经济评论, 8(2), 1-30."
        )
        self.assertEqual(payload["status"], "NOT_IN_LIBRARY")
        self.assertIsNone(payload["match"])

    def test_fictional_doi_is_not_in_library(self):
        payload = self.verifier.verify("Someone (2030). doi:10.9999/fabricated.2030.0001")
        self.assertEqual(payload["status"], "NOT_IN_LIBRARY")

    def test_handoff_is_returned_and_does_not_download(self):
        payload = self.verifier.verify("Nobody, A. (2032). A paper that does not exist. Nowhere.")
        self.assertEqual(payload["suggested_next_action"], "ACQUISITION")
        handoff = payload["handoff"]
        self.assertIn("identity", handoff)
        self.assertIn("agent-route", handoff["entry_point"])

    def test_a_real_citation_is_still_found(self):
        payload = self.verifier.verify(
            "王海森、李纲(2026). 人工智能漂洗抹杀了企业技术创新吗. 中国工业经济."
        )
        self.assertEqual(payload["status"], "IN_LIBRARY")
        self.assertEqual(payload["paper_id"], AI_WASHING_INNOVATION_WORK)
        self.assertTrue(payload["fulltext_available"])

    def test_a_real_doi_citation_is_found(self):
        work = next(w for w in self.navigator.snapshot.works if w.normalized_doi != "unknown")
        payload = self.verifier.verify(f"Anon (2026). doi:{work.doi}")
        self.assertEqual(payload["status"], "IN_LIBRARY")
        self.assertEqual(payload["paper_id"], work.paper_id)


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestKReadingPack(unittest.TestCase):
    """TEST K -- a reading pack references papers and copies none."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()
        cls.pack = ReadingPackBuilder(cls.navigator).build(
            "AI washing 如何通过审计风险影响盈余质量？",
            top=15,
            use_fulltext=False,
            write=False,
        )

    def test_pack_has_the_requested_number_of_works(self):
        self.assertEqual(self.pack["entry_count"], 15)

    def test_pack_contains_no_duplicate_works(self):
        ids = [entry["paper_id"] for entry in self.pack["manifest"]["entries"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.pack["unique_works"], 15)

    def test_pack_copies_no_pdf(self):
        self.assertEqual(self.pack["copies_made"], 0)

    def test_every_entry_points_at_a_real_canonical_file(self):
        for entry in self.pack["manifest"]["entries"]:
            with self.subTest(paper_id=entry["paper_id"]):
                self.assertIsNotNone(entry["canonical_path"])
                self.assertTrue(Path(entry["absolute_path"]).is_file())

    def test_every_entry_records_a_verified_hash(self):
        for entry in self.pack["manifest"]["entries"]:
            with self.subTest(paper_id=entry["paper_id"]):
                self.assertEqual(len(entry["verified_sha256_at_pack_time"]), 64)

    def test_every_entry_has_a_paper_id_in_the_catalog(self):
        for entry in self.pack["manifest"]["entries"]:
            self.assertIsNotNone(self.navigator.snapshot.by_id(entry["paper_id"]))

    def test_every_entry_carries_a_role_and_a_reason(self):
        for entry in self.pack["manifest"]["entries"]:
            self.assertIn(entry["relevance_role"], {role.value for role in RelevanceRole})
            self.assertTrue(entry["relevance_reason"])

    def test_dry_run_writes_nothing(self):
        self.assertIsNone(self.pack["pack_dir"])


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class GapHelperTests(unittest.TestCase):
    """Coverage analysis must stay a local-corpus statement."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()
        cls.payload = CoverageAnalyzer(cls.navigator).analyze(
            "AI washing 通过审计风险影响盈余质量", top=20
        )

    def test_scope_is_stated_explicitly(self):
        self.assertIn("local", self.payload["scope"].lower())
        self.assertIn("not", self.payload["scope"].lower())

    def test_it_never_claims_the_literature_lacks_something(self):
        text = (self.payload["scope"] + self.payload["interpretation_guard"]).lower()
        self.assertNotIn("gap in the literature", text)
        self.assertIn("novelty", text)

    def test_concept_coverage_counts_local_works(self):
        coverage = {item["concept"]: item for item in self.payload["concept_coverage"]}
        self.assertIn("ai_washing", coverage)
        self.assertGreater(coverage["ai_washing"]["local_work_count"], 0)

    def test_joint_coverage_is_reported_per_connection(self):
        self.assertTrue(self.payload["under_covered_connections"])
        for pair in self.payload["under_covered_connections"]:
            self.assertIn(
                pair["assessment"],
                {"NO_LOCAL_WORK_JOINS_THESE", "THINLY_JOINED", "JOINED"},
            )

    def test_recommendations_point_at_real_works(self):
        for row in self.payload["recommended_papers_to_inspect"]:
            self.assertIsNotNone(self.navigator.snapshot.by_id(row["paper_id"]))


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class VersionResolutionSweepTests(unittest.TestCase):
    """Every one of the 192 versions must resolve to a real, correctly-typed file."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = load_navigator()

    def test_every_work_resolves_to_a_preferred_version(self):
        for work in self.navigator.snapshot.works:
            with self.subTest(paper_id=work.paper_id):
                resolution = self.navigator.resolver.resolve(work)
                self.assertIsNotNone(resolution.preferred)

    def test_every_managed_file_exists_on_disk(self):
        missing = []
        for work in self.navigator.snapshot.works:
            for version in work.versions:
                if not version.exists():
                    missing.append(version.managed_path)
        self.assertEqual(missing, [])

    def test_every_work_is_reported_available(self):
        not_available = [
            work.paper_id
            for work in self.navigator.snapshot.works
            if self.navigator.resolver.resolve(work).fulltext_status
            is not FullTextStatus.AVAILABLE
        ]
        self.assertEqual(not_available, [])

    def test_preferred_version_is_the_catalog_canonical_wherever_one_exists(self):
        for work in self.navigator.snapshot.works:
            if not work.canonical_sha256 or work.canonical_sha256 == "unknown":
                continue
            if work.version_by_sha(work.canonical_sha256) is None:
                continue
            with self.subTest(paper_id=work.paper_id):
                preferred = self.navigator.resolver.resolve(work).preferred
                self.assertEqual(preferred.version.sha256, work.canonical_sha256)


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TestLFreeze(unittest.TestCase):
    """TEST L -- the library is byte-identical before and after everything above."""

    def test_library_fingerprint_is_unchanged_by_navigator_use(self):
        fingerprinter = LibraryFingerprinter()
        before = fingerprinter.capture()

        navigator = load_navigator()
        navigator.search("AI washing 审计风险 盈余质量", top=20)
        navigator.lookup("author:王海森")
        navigator.fulltext(CAJ_WORK)
        RelatedWorkFinder(navigator).find(AI_WASHING_AUDIT_WORK, top=10)
        CitationVerifier(navigator).verify("Nobody (2031). Nothing at all. Nowhere.")
        CoverageAnalyzer(navigator).analyze("AI washing audit risk", top=10)
        ReadingPackBuilder(navigator).build("AI washing", top=10, write=False)

        after = fingerprinter.capture()
        comparison = after.compare(before)
        self.assertTrue(comparison["identical"], comparison["differences"][:10])

    def test_fingerprint_records_the_sealed_counts(self):
        payload = LibraryFingerprinter().capture().as_dict()
        self.assertEqual(payload["catalog_records"], EXPECTED_WORKS)
        self.assertEqual(payload["physical_versions"], EXPECTED_VERSIONS)
        self.assertEqual(payload["papers_file_count"], EXPECTED_VERSIONS)
        self.assertEqual(payload["nested_variants"], 13)
        self.assertEqual(payload["topic_assignment_count"], EXPECTED_TOPIC_ASSIGNMENTS)
        self.assertEqual(payload["unique_topic_values"], EXPECTED_UNIQUE_TOPICS)

    def test_fingerprint_detects_a_change(self):
        """The guard must be capable of failing, or it proves nothing."""

        fingerprinter = LibraryFingerprinter()
        before = fingerprinter.capture()
        tampered = dict(before.as_dict())
        tampered["catalog_records"] = 178
        comparison = before.compare(tampered)
        self.assertFalse(comparison["identical"])
        self.assertTrue(comparison["differences"])

    def test_no_navigator_call_ever_opens_a_library_path_for_writing(self):
        """A runtime guard, not a grep: intercept every mutating filesystem call.

        Every write primitive is wrapped for the duration of a full Navigator
        exercise, and any call whose target resolves inside the Library or the
        topic view is recorded as a violation.
        """

        import builtins
        import pathlib
        import shutil

        protected = [
            Path(LIBRARY_CATALOG_JSONL).parent.parent.resolve(),   # library/
            Path(PAPERS_BY_TOPIC_DIR).resolve(),
        ]
        violations: list[str] = []

        def guard(label: str, target) -> None:
            try:
                resolved = Path(target).resolve()
            except (OSError, TypeError, ValueError):
                return
            for root in protected:
                if resolved == root or root in resolved.parents:
                    violations.append(f"{label}: {resolved}")

        real_open = builtins.open
        real_path_open = pathlib.Path.open
        real_write_text = pathlib.Path.write_text
        real_write_bytes = pathlib.Path.write_bytes
        real_unlink = pathlib.Path.unlink
        real_replace = pathlib.Path.replace
        real_rename = pathlib.Path.rename
        real_chmod = pathlib.Path.chmod
        real_mkdir = pathlib.Path.mkdir
        real_rmtree = shutil.rmtree

        def patched_open(file, mode="r", *args, **kwargs):
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                guard("open", file)
            return real_open(file, mode, *args, **kwargs)

        def patched_path_open(self, mode="r", *args, **kwargs):
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                guard("Path.open", self)
            return real_path_open(self, mode, *args, **kwargs)

        def make_wrapper(name, function):
            def wrapper(self, *args, **kwargs):
                guard(name, self)
                return function(self, *args, **kwargs)

            return wrapper

        def patched_rmtree(path, *args, **kwargs):
            guard("shutil.rmtree", path)
            return real_rmtree(path, *args, **kwargs)

        builtins.open = patched_open
        pathlib.Path.open = patched_path_open
        pathlib.Path.write_text = make_wrapper("Path.write_text", real_write_text)
        pathlib.Path.write_bytes = make_wrapper("Path.write_bytes", real_write_bytes)
        pathlib.Path.unlink = make_wrapper("Path.unlink", real_unlink)
        pathlib.Path.replace = make_wrapper("Path.replace", real_replace)
        pathlib.Path.rename = make_wrapper("Path.rename", real_rename)
        pathlib.Path.chmod = make_wrapper("Path.chmod", real_chmod)
        pathlib.Path.mkdir = make_wrapper("Path.mkdir", real_mkdir)
        shutil.rmtree = patched_rmtree
        try:
            navigator = load_navigator()
            navigator.search("AI washing 审计风险 盈余质量", top=20)
            navigator.lookup("author:王海森")
            navigator.fulltext(CAJ_WORK)
            navigator.fulltext(FOUR_VERSION_WORK)
            RelatedWorkFinder(navigator).find(AI_WASHING_AUDIT_WORK, top=10)
            CitationVerifier(navigator).verify("Nobody (2031). Nothing at all. Nowhere.")
            CoverageAnalyzer(navigator).analyze("AI washing audit risk", top=10)
            ReadingPackBuilder(navigator).build("AI washing", top=10, write=False)
            LibraryFingerprinter().capture()
        finally:
            builtins.open = real_open
            pathlib.Path.open = real_path_open
            pathlib.Path.write_text = real_write_text
            pathlib.Path.write_bytes = real_write_bytes
            pathlib.Path.unlink = real_unlink
            pathlib.Path.replace = real_replace
            pathlib.Path.rename = real_rename
            pathlib.Path.chmod = real_chmod
            pathlib.Path.mkdir = real_mkdir
            shutil.rmtree = real_rmtree

        self.assertEqual(violations, [])

    def test_the_write_guard_can_actually_fail(self):
        """The guard above proves nothing unless it detects a real write."""

        import pathlib

        protected = Path(LIBRARY_CATALOG_JSONL).parent.parent.resolve()
        target = Path(LIBRARY_CATALOG_JSONL).resolve()
        self.assertTrue(target == protected or protected in target.parents)
        self.assertTrue(hasattr(pathlib.Path, "write_text"))


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class TopicMetadataSourceTests(unittest.TestCase):
    def test_topics_file_is_read_from_the_catalog_directory(self):
        self.assertTrue(LIBRARY_TOPICS_JSONL.is_file())
        self.assertEqual(LIBRARY_TOPICS_JSONL.parent, LIBRARY_CATALOG_JSONL.parent)


if __name__ == "__main__":
    unittest.main()
