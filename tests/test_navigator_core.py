"""Unit tests for the Navigator's building blocks.

Everything here runs on synthetic fixtures in temporary directories.  Nothing
in this file reads the real corpus; the real-corpus acceptance suite lives in
``test_navigator_acceptance.py``.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.navigator.catalog import (
    CatalogReader,
    LibraryUnavailable,
    parse_topic_field,
)
from hunnu_harness.navigator.citation import (
    MATCH_THRESHOLD,
    CitationVerifier,
    acquisition_handoff,
    parse_citation,
)
from hunnu_harness.navigator.fulltext import split_page_text
from hunnu_harness.navigator.index import IndexStatus, NavigatorIndex
from hunnu_harness.navigator.lexicon import Facet, match_concepts
from hunnu_harness.navigator.query import EXPANSION_WEIGHT, PHENOMENON_WEIGHT, parse_query
from hunnu_harness.navigator.ranking import BM25FIndex, build_field_document
from hunnu_harness.navigator.resolver import FullTextStatus, PreferredVersionResolver
from hunnu_harness.navigator.search import PaperNavigator
from hunnu_harness.navigator.tokenize import display_terms, normalize_person_query, tokenize

BACKSLASH = chr(92)


def catalog_record(
    *,
    paper_id="PAAAAAAAAAAAA",
    title="Artificial intelligence washing and audit risk",
    authors=("Author A", "Author B"),
    year="2026",
    journal="Journal of Tests",
    doi="10.1000/nav-test",
    versions=None,
    canonical_sha="a" * 64,
):
    versions = versions or [
        {
            "sha256": canonical_sha,
            "managed_path": f"library/papers/{paper_id}.pdf",
            "full_text_format": "PDF",
            "version_role": "CANONICAL_VERSION",
            "source_type": "EXTERNAL_IMPORT",
            "source_locator": "TEST",
            "status": "MANAGED",
            "acquired_at": "unknown",
            "imported_at": "2026-01-01T00:00:00+00:00",
            "provenance": [],
        }
    ]
    return {
        "schema_version": "0.2.11",
        "paper_id": paper_id,
        "doi": doi,
        "title": title,
        "authors": list(authors),
        "first_author": authors[0] if authors else "unknown",
        "year": year,
        "journal": journal,
        "managed_pdf_path": versions[0]["managed_path"],
        "managed_fulltext_path": versions[0]["managed_path"],
        "sha256": canonical_sha,
        "source_type": "EXTERNAL_IMPORT",
        "source_locator": "TEST",
        "acquired_at": "unknown",
        "imported_at": "2026-01-01T00:00:00+00:00",
        "original_paths": [],
        "version_role": "CANONICAL_VERSION",
        "notes_path": f"library/notes/{paper_id}",
        "status": "MANAGED",
        "same_work_different_version": len(versions) > 1,
        "other_version_sha256s": [v["sha256"] for v in versions[1:]],
        "versions": versions,
    }


def topic_record(paper_id="PAAAAAAAAAAAA", topics=None, keywords="ai washing;audit"):
    topics = topics or [
        f"01_人工智能与数字经济{BACKSLASH}AI漂洗",
        f"04_会计审计与信息披露{BACKSLASH}审计与内部控制",
    ]
    return {
        "paper_id": paper_id,
        "title": "Artificial intelligence washing and audit risk",
        "human_readable_name": "Author A(2026)- AI washing.pdf",
        "canonical_path": "C:/x/library/papers/x.pdf",
        "canonical_sha256": "a" * 64,
        "version_paths": "C:/x/library/papers/x.pdf",
        "topics": ";".join(topics),
        "primary_domain": "01_人工智能与数字经济",
        "secondary_domains": "04_会计审计与信息披露",
        "keywords": keywords,
        "metadata_notes": "",
    }


def write_catalog(directory: Path, records, topic_records=None):
    catalog = directory / "papers.jsonl"
    catalog.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
        newline="\n",
    )
    topics = directory / "paper_topics.jsonl"
    if topic_records is not None:
        topics.write_text(
            "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in topic_records),
            encoding="utf-8",
            newline="\n",
        )
    return catalog, topics


class TokenizerTests(unittest.TestCase):
    def test_cjk_produces_unigrams_and_bigrams(self):
        tokens = tokenize("人工智能漂洗")
        self.assertIn("漂洗", tokens)
        self.assertIn("智能", tokens)
        self.assertIn("人", tokens)

    def test_bigram_never_spans_a_non_cjk_boundary(self):
        tokens = tokenize("智能 转型")
        self.assertIn("智能", tokens)
        self.assertNotIn("能转", tokens)

    def test_latin_is_lowercased_and_lightly_stemmed(self):
        tokens = tokenize("AI Washing Firms")
        self.assertIn("ai", tokens)
        self.assertIn("washing", tokens)
        self.assertIn("wash", tokens)
        self.assertIn("firm", tokens)

    def test_full_width_folds_to_half_width(self):
        self.assertEqual(tokenize("ＡＩ"), tokenize("AI"))

    def test_stop_words_are_dropped(self):
        self.assertNotIn("the", tokenize("the audit"))

    def test_empty_input_is_safe(self):
        self.assertEqual(tokenize(None), [])
        self.assertEqual(tokenize(""), [])

    def test_author_query_splits_cjk_names(self):
        self.assertEqual(normalize_person_query("王海森 李纲"), ["王海森", "李纲"])

    def test_latin_author_stays_one_name(self):
        self.assertEqual(normalize_person_query("Biddle Hilary"), ["biddle hilary"])

    def test_display_terms_prefers_multi_character_tokens(self):
        self.assertEqual(display_terms(["器", "机器", "人"], limit=2), ("机器",))

    def test_display_terms_falls_back_when_only_unigrams(self):
        self.assertEqual(display_terms(["器"]), ("器",))


class TopicParsingTests(unittest.TestCase):
    def test_assignments_split_on_semicolon_then_backslash(self):
        raw = f"01_A{BACKSLASH}sub1;02_B{BACKSLASH}sub2"
        parsed = parse_topic_field(raw)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].domain, "01_A")
        self.assertEqual(parsed[0].subtopic, "sub1")
        self.assertEqual(parsed[1].label, f"02_B{BACKSLASH}sub2")

    def test_domain_without_subtopic_is_kept(self):
        parsed = parse_topic_field("99_其他")
        self.assertEqual(parsed[0].domain, "99_其他")
        self.assertEqual(parsed[0].subtopic, "")

    def test_duplicate_assignments_collapse(self):
        raw = f"01_A{BACKSLASH}s;01_A{BACKSLASH}s"
        self.assertEqual(len(parse_topic_field(raw)), 1)

    def test_empty_field_is_empty(self):
        self.assertEqual(parse_topic_field(""), ())
        self.assertEqual(parse_topic_field(None), ())


class LexiconTests(unittest.TestCase):
    def test_chinese_and_english_reach_the_same_concept(self):
        english = {m.key for m in match_concepts("AI washing")}
        chinese = {m.key for m in match_concepts("人工智能漂洗")}
        self.assertIn("ai_washing", english)
        self.assertIn("ai_washing", chinese)

    def test_latin_term_requires_a_word_boundary(self):
        self.assertNotIn("artificial_intelligence", {m.key for m in match_concepts("domain said")})

    def test_facets_are_assigned(self):
        facets = {m.facet for m in match_concepts("AI washing audit risk earnings quality")}
        self.assertIn(Facet.PHENOMENON, facets)
        self.assertIn(Facet.MECHANISM, facets)
        self.assertIn(Facet.OUTCOME, facets)


class QueryTests(unittest.TestCase):
    def test_field_filters_are_extracted(self):
        parsed = parse_query("author:王海森 李纲 year:2026")
        self.assertEqual(parsed.field_filters["author"], "王海森 李纲")
        self.assertIn("2026", parsed.years)

    def test_doi_is_normalised(self):
        parsed = parse_query("DOI:https://doi.org/10.1000/ABC")
        self.assertEqual(parsed.doi, "10.1000/abc")

    def test_paper_id_is_detected(self):
        self.assertEqual(parse_query("PEC61CA32AC26").paper_id, "PEC61CA32AC26")

    def test_expansion_is_reported_and_discounted(self):
        parsed = parse_query("AI washing")
        audit = parsed.audit()
        self.assertTrue(any(item["concept"] == "ai_washing" for item in audit["expansions"]))
        expanded = [t for t in parsed.terms if t.source == "expansion" and t.concept == "ai_washing"]
        self.assertTrue(expanded)
        self.assertLess(expanded[0].weight, PHENOMENON_WEIGHT)

    def test_phenomenon_terms_outweigh_ordinary_terms(self):
        parsed = parse_query("AI washing earnings quality")
        weights = {t.token: t.weight for t in parsed.terms if t.source == "literal"}
        self.assertEqual(weights["washing"], PHENOMENON_WEIGHT)
        self.assertEqual(weights["earnings"], 1.0)

    def test_expansion_can_be_disabled(self):
        parsed = parse_query("AI washing", expand=False)
        self.assertEqual(parsed.expansions, {})
        self.assertTrue(all(t.source == "literal" for t in parsed.terms))

    def test_expansion_weight_constant_is_a_discount(self):
        self.assertLess(EXPANSION_WEIGHT, 1.0)


class RankingTests(unittest.TestCase):
    def _index(self):
        documents = [
            build_field_document("A", {"title": "ai washing and audit", "topics": "", "keywords": "",
                                       "authors": "", "journal": "", "human_name": ""}),
            build_field_document("B", {"title": "supply chain resilience", "topics": "", "keywords": "",
                                       "authors": "", "journal": "", "human_name": ""}),
        ]
        return BM25FIndex(documents)

    def test_matching_document_outranks_unrelated(self):
        parsed = parse_query("ai washing", expand=False)
        results = self._index().score(list(parsed.terms))
        self.assertEqual(results[0].doc_id, "A")

    def test_every_result_carries_provenance(self):
        parsed = parse_query("ai washing", expand=False)
        results = self._index().score(list(parsed.terms))
        self.assertTrue(results[0].matches)
        self.assertTrue(all(m.field_name and m.token for m in results[0].matches))

    def test_no_terms_scores_nothing(self):
        self.assertEqual(self._index().score([]), [])

    def test_ordering_is_deterministic_for_equal_scores(self):
        documents = [
            build_field_document(doc_id, {"title": "same text", "topics": "", "keywords": "",
                                          "authors": "", "journal": "", "human_name": ""})
            for doc_id in ("Z", "A", "M")
        ]
        parsed = parse_query("same text", expand=False)
        ordering = [r.doc_id for r in BM25FIndex(documents).score(list(parsed.terms))]
        self.assertEqual(ordering, sorted(ordering))


class ResolverTests(unittest.TestCase):
    def _work(self, tmp: Path, versions, canonical_sha):
        with tempfile.TemporaryDirectory() as _unused:
            pass
        record = catalog_record(versions=versions, canonical_sha=canonical_sha)
        directory = tmp / "catalog"
        directory.mkdir(parents=True, exist_ok=True)
        catalog, topics = write_catalog(directory, [record], [topic_record()])
        snapshot = CatalogReader(catalog_path=catalog, topics_path=topics).load()
        return snapshot.works[0]

    def _version(self, sha, path, fmt="PDF", role="EXTERNAL_IMPORT"):
        return {
            "sha256": sha,
            "managed_path": path,
            "full_text_format": fmt,
            "version_role": role,
            "source_type": "EXTERNAL_IMPORT",
            "source_locator": "TEST",
            "status": "MANAGED",
            "acquired_at": "unknown",
            "imported_at": "2026-01-01T00:00:00+00:00",
            "provenance": [],
        }

    def test_catalog_canonical_sha_wins_over_role_and_format(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            versions = [
                self._version("b" * 64, "library/papers/x__b.pdf", role="CANONICAL_VERSION"),
                self._version("a" * 64, "library/papers/x.pdf", role="EXTERNAL_IMPORT"),
            ]
            work = self._work(tmp, versions, canonical_sha="a" * 64)
            resolution = PreferredVersionResolver().resolve(work)
            self.assertEqual(resolution.preferred.version.sha256, "a" * 64)
            self.assertTrue(resolution.preferred.is_catalog_canonical)

    def test_pdf_preferred_over_caj_at_equal_rank(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            versions = [
                self._version("c" * 64, "library/papers/x.caj", fmt="CAJ", role="FORMAT_VARIANT"),
                self._version("a" * 64, "library/papers/x.pdf", role="FORMAT_VARIANT"),
            ]
            work = self._work(tmp, versions, canonical_sha="")
            resolution = PreferredVersionResolver().resolve(work)
            self.assertEqual(resolution.preferred.version.full_text_format, "PDF")

    def test_caj_only_work_is_present_but_not_machine_readable(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            papers = tmp / "papers"
            papers.mkdir()
            (papers / "x.caj").write_bytes(b"CAJ fake")
            versions = [self._version("c" * 64, str(papers / "x.caj"), fmt="CAJ")]
            work = self._work(tmp, versions, canonical_sha="c" * 64)
            resolution = PreferredVersionResolver().resolve(work)
            self.assertIs(resolution.fulltext_status, FullTextStatus.AVAILABLE_NOT_MACHINE_READABLE)
            self.assertFalse(resolution.readable)

    def test_missing_file_reports_file_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            versions = [self._version("a" * 64, "library/papers/does-not-exist.pdf")]
            work = self._work(tmp, versions, canonical_sha="a" * 64)
            resolution = PreferredVersionResolver().resolve(work)
            self.assertIs(resolution.fulltext_status, FullTextStatus.FILE_MISSING)

    def test_path_is_never_derived_from_paper_id(self):
        """The three real works whose file name differs from their paper_id."""

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            versions = [self._version("a" * 64, "library/papers/PDIFFERENT123.pdf")]
            work = self._work(tmp, versions, canonical_sha="a" * 64)
            resolution = PreferredVersionResolver().resolve(work)
            self.assertTrue(
                resolution.preferred.version.managed_path.endswith("PDIFFERENT123.pdf")
            )


class CatalogReaderTests(unittest.TestCase):
    def test_missing_catalog_raises_library_unavailable(self):
        with tempfile.TemporaryDirectory() as raw:
            reader = CatalogReader(
                catalog_path=Path(raw) / "absent.jsonl", topics_path=Path(raw) / "t.jsonl"
            )
            with self.assertRaises(LibraryUnavailable):
                reader.load()

    def test_corrupt_line_is_skipped_and_recorded(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            catalog = directory / "papers.jsonl"
            good = json.dumps(catalog_record(), ensure_ascii=False, sort_keys=True)
            catalog.write_text(good + "\n{ this is not json\n", encoding="utf-8", newline="\n")
            topics = directory / "paper_topics.jsonl"
            topics.write_text(json.dumps(topic_record()) + "\n", encoding="utf-8", newline="\n")
            snapshot = CatalogReader(catalog_path=catalog, topics_path=topics).load()
            self.assertEqual(snapshot.work_count, 1)
            self.assertEqual(len(snapshot.degraded), 1)
            self.assertIn("unparseable", snapshot.degraded[0].detail)

    def test_duplicate_paper_id_keeps_the_first(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            first = catalog_record(title="First")
            second = catalog_record(title="Second")
            catalog, topics = write_catalog(directory, [first, second], [topic_record()])
            snapshot = CatalogReader(catalog_path=catalog, topics_path=topics).load()
            self.assertEqual(snapshot.work_count, 1)
            self.assertEqual(snapshot.works[0].title, "First")
            self.assertTrue(any("duplicate" in d.detail for d in snapshot.degraded))

    def test_missing_topics_file_degrades_without_failing(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            catalog, _ = write_catalog(directory, [catalog_record()], None)
            snapshot = CatalogReader(
                catalog_path=catalog, topics_path=directory / "absent.jsonl"
            ).load()
            self.assertEqual(snapshot.work_count, 1)
            self.assertEqual(snapshot.works[0].topics, ())
            self.assertTrue(any("topic metadata not found" in d.detail for d in snapshot.degraded))

    def test_record_without_paper_id_is_recorded(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            broken = catalog_record()
            broken.pop("paper_id")
            catalog, topics = write_catalog(directory, [broken], [topic_record()])
            snapshot = CatalogReader(catalog_path=catalog, topics_path=topics).load()
            self.assertEqual(snapshot.work_count, 0)
            self.assertTrue(any("no paper_id" in d.detail for d in snapshot.degraded))

    def test_counts_are_work_first(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            versions = [
                {
                    "sha256": "a" * 64,
                    "managed_path": "library/papers/x.pdf",
                    "full_text_format": "PDF",
                    "version_role": "CANONICAL_VERSION",
                    "source_type": "EXTERNAL_IMPORT",
                    "source_locator": "TEST",
                    "status": "MANAGED",
                    "acquired_at": "unknown",
                    "imported_at": "2026-01-01T00:00:00+00:00",
                    "provenance": [],
                },
                {
                    "sha256": "b" * 64,
                    "managed_path": "library/papers/x__b.pdf",
                    "full_text_format": "PDF",
                    "version_role": "EXTERNAL_IMPORT",
                    "source_type": "EXTERNAL_IMPORT",
                    "source_locator": "TEST",
                    "status": "MANAGED",
                    "acquired_at": "unknown",
                    "imported_at": "2026-01-01T00:00:00+00:00",
                    "provenance": [],
                },
            ]
            catalog, topics = write_catalog(
                directory, [catalog_record(versions=versions)], [topic_record()]
            )
            snapshot = CatalogReader(catalog_path=catalog, topics_path=topics).load()
            self.assertEqual(snapshot.work_count, 1)
            self.assertEqual(snapshot.version_count, 2)


class ChunkingTests(unittest.TestCase):
    def test_short_page_is_one_chunk(self):
        self.assertEqual(split_page_text("hello world", chunk_chars=100, overlap=10), ["hello world"])

    def test_long_page_is_split_with_overlap(self):
        text = " ".join(["word"] * 800)
        chunks = split_page_text(text, chunk_chars=200, overlap=20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 220 for chunk in chunks))

    def test_empty_page_yields_nothing(self):
        self.assertEqual(split_page_text("   ", chunk_chars=100, overlap=10), [])


class NavigatorBehaviourTests(unittest.TestCase):
    def _navigator(self, directory: Path, records=None, topics=None):
        records = records or [catalog_record()]
        topics = topics if topics is not None else [topic_record()]
        catalog, topics_path = write_catalog(directory, records, topics)
        reader = CatalogReader(catalog_path=catalog, topics_path=topics_path)
        index = NavigatorIndex(root=directory / "paper_retrieval")
        return PaperNavigator(reader=reader, index=index)

    def test_search_works_with_no_index_at_all(self):
        with tempfile.TemporaryDirectory() as raw:
            navigator = self._navigator(Path(raw))
            payload = navigator.search("AI washing", top=5)
            self.assertEqual(payload["index_status"], IndexStatus.ABSENT.value)
            self.assertEqual(payload["results"][0]["paper_id"], "PAAAAAAAAAAAA")

    def test_unknown_doi_is_not_in_library_and_returns_no_guess(self):
        with tempfile.TemporaryDirectory() as raw:
            navigator = self._navigator(Path(raw))
            payload = navigator.lookup("DOI:10.9999/does-not-exist")
            self.assertEqual(payload["status"], "NOT_IN_LIBRARY")
            self.assertEqual(payload["matches"], [])

    def test_unknown_paper_id_is_not_in_library(self):
        with tempfile.TemporaryDirectory() as raw:
            navigator = self._navigator(Path(raw))
            self.assertEqual(navigator.fulltext("PFFFFFFFFFFFF")["status"], "NOT_IN_LIBRARY")

    def test_approximate_lookup_is_labelled_as_approximate(self):
        with tempfile.TemporaryDirectory() as raw:
            navigator = self._navigator(Path(raw))
            payload = navigator.lookup("artificial intelligence washing audit")
            self.assertIn(payload["status"], {"FOUND", "FOUND_APPROXIMATE"})
            if payload["status"] == "FOUND_APPROXIMATE":
                self.assertTrue(
                    all(m["confidence"] == "APPROXIMATE" for m in payload["matches"])
                )

    def test_every_result_explains_itself(self):
        with tempfile.TemporaryDirectory() as raw:
            navigator = self._navigator(Path(raw))
            for row in navigator.search("AI washing audit", top=5)["results"]:
                self.assertTrue(row["relevance_reason"])
                self.assertTrue(row["matched_by"])

    def test_degraded_catalog_is_reported_in_the_envelope(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            catalog = directory / "papers.jsonl"
            catalog.write_text(
                json.dumps(catalog_record(), ensure_ascii=False) + "\nbroken\n",
                encoding="utf-8",
                newline="\n",
            )
            topics = directory / "paper_topics.jsonl"
            topics.write_text(json.dumps(topic_record()) + "\n", encoding="utf-8", newline="\n")
            navigator = PaperNavigator(
                reader=CatalogReader(catalog_path=catalog, topics_path=topics),
                index=NavigatorIndex(root=directory / "paper_retrieval"),
            )
            payload = navigator.search("AI washing")
            self.assertEqual(payload["catalog_status"], "PARTIAL")
            self.assertTrue(payload["degraded"])

    def test_a_failing_reranker_cannot_break_retrieval(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            catalog, topics_path = write_catalog(directory, [catalog_record()], [topic_record()])

            def exploding(_parsed, _results):
                raise RuntimeError("embedding service unavailable")

            navigator = PaperNavigator(
                reader=CatalogReader(catalog_path=catalog, topics_path=topics_path),
                index=NavigatorIndex(root=directory / "paper_retrieval"),
                reranker=exploding,
            )
            payload = navigator.search("AI washing")
            self.assertTrue(payload["results"])
            self.assertIn("reranker_error", payload["index_detail"])

    def test_topic_signal_recalls_a_work_whose_title_does_not_match(self):
        with tempfile.TemporaryDirectory() as raw:
            record = catalog_record(title="A study of nothing in particular")
            navigator = self._navigator(Path(raw), records=[record])
            payload = navigator.search("AI washing", top=5)
            self.assertTrue(payload["results"])
            self.assertEqual(payload["results"][0]["paper_id"], "PAAAAAAAAAAAA")


class CitationTests(unittest.TestCase):
    def _navigator(self, directory: Path):
        catalog, topics = write_catalog(directory, [catalog_record()], [topic_record()])
        return PaperNavigator(
            reader=CatalogReader(catalog_path=catalog, topics_path=topics),
            index=NavigatorIndex(root=directory / "paper_retrieval"),
        )

    def test_apa_citation_is_parsed(self):
        parsed = parse_citation("Biddle, G., Hilary, G. (2009). Accounting quality. The Accounting Review.")
        self.assertEqual(parsed.year, "2009")
        self.assertIn("Biddle", parsed.authors)

    def test_chinese_citation_is_parsed(self):
        parsed = parse_citation("王海森、李纲(2026). 人工智能漂洗抹杀了企业技术创新吗. 中国工业经济.")
        self.assertEqual(parsed.year, "2026")
        self.assertTrue(parsed.title_guess)

    def test_initials_are_not_counted_as_authors(self):
        """"Biddle, G., Hilary, G." is two authors, not four."""

        parsed = parse_citation(
            "Biddle, G., Hilary, G., Verdi, R. (2009). Accounting quality. JAE."
        )
        self.assertEqual(list(parsed.authors), ["Biddle", "Hilary", "Verdi"])
        self.assertNotIn("G", parsed.authors)
        self.assertNotIn("R", parsed.authors)

    def test_doi_citation_matches_exactly(self):
        with tempfile.TemporaryDirectory() as raw:
            navigator = self._navigator(Path(raw))
            payload = CitationVerifier(navigator).verify("Someone (2026) doi:10.1000/nav-test")
            self.assertEqual(payload["status"], "IN_LIBRARY")
            self.assertEqual(payload["match_type"], "DOI")

    def test_fictional_citation_is_not_in_library(self):
        with tempfile.TemporaryDirectory() as raw:
            navigator = self._navigator(Path(raw))
            payload = CitationVerifier(navigator).verify(
                "Okonkwo, R. (2031). Quantum ledger washing. Journal of Speculative Accountancy."
            )
            self.assertEqual(payload["status"], "NOT_IN_LIBRARY")
            self.assertEqual(payload["suggested_next_action"], "ACQUISITION")
            self.assertIsNone(payload["match"])

    def test_unverified_candidates_are_flagged_as_unverified(self):
        with tempfile.TemporaryDirectory() as raw:
            navigator = self._navigator(Path(raw))
            payload = CitationVerifier(navigator).verify("Someone (2031). Audit washing something.")
            if payload["status"] == "NOT_IN_LIBRARY":
                for candidate in payload["unverified_candidates"]:
                    self.assertFalse(candidate["verified"])

    def test_handoff_carries_identity_and_no_download(self):
        handoff = acquisition_handoff(parse_citation("A. Author (2030). A missing paper. Somewhere."))
        self.assertEqual(handoff["status"], "NOT_IN_LIBRARY")
        self.assertEqual(handoff["suggested_next_action"], "ACQUISITION")
        self.assertIn("agent-route", handoff["entry_point"])
        self.assertEqual(handoff["acquisition_request_hint"]["TaskType"], "literature_search")

    def test_threshold_is_a_real_bar(self):
        self.assertGreater(MATCH_THRESHOLD, 0.5)


class IndexLifecycleTests(unittest.TestCase):
    def test_status_is_absent_before_any_build(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            catalog, topics = write_catalog(directory, [catalog_record()], [topic_record()])
            snapshot = CatalogReader(catalog_path=catalog, topics_path=topics).load()
            index = NavigatorIndex(root=directory / "paper_retrieval")
            status, _detail = index.status(snapshot)
            self.assertIs(status, IndexStatus.ABSENT)

    def test_build_then_status_is_fresh_and_rebuild_from_scratch_works(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            catalog, topics = write_catalog(directory, [catalog_record()], [topic_record()])
            snapshot = CatalogReader(catalog_path=catalog, topics_path=topics).load()
            root = directory / "paper_retrieval"
            with patch("hunnu_harness.navigator.index.require_output_path", side_effect=lambda p, **k: p):
                index = NavigatorIndex(root=root)
                index.build(snapshot)
                self.assertIs(index.status(snapshot)[0], IndexStatus.FRESH)
                index.drop()
                self.assertIs(index.status(snapshot)[0], IndexStatus.ABSENT)
                index.build(snapshot)
                self.assertIs(index.status(snapshot)[0], IndexStatus.FRESH)

    def test_catalog_change_makes_the_index_stale(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            catalog, topics = write_catalog(directory, [catalog_record()], [topic_record()])
            snapshot = CatalogReader(catalog_path=catalog, topics_path=topics).load()
            root = directory / "paper_retrieval"
            with patch("hunnu_harness.navigator.index.require_output_path", side_effect=lambda p, **k: p):
                index = NavigatorIndex(root=root)
                index.build(snapshot)
                write_catalog(directory, [catalog_record(title="Changed")], [topic_record()])
                changed = CatalogReader(catalog_path=catalog, topics_path=topics).load()
                self.assertIs(index.status(changed)[0], IndexStatus.STALE)

    def test_corrupt_manifest_is_treated_as_unreadable_not_fatal(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            catalog, topics = write_catalog(directory, [catalog_record()], [topic_record()])
            snapshot = CatalogReader(catalog_path=catalog, topics_path=topics).load()
            root = directory / "paper_retrieval"
            with patch("hunnu_harness.navigator.index.require_output_path", side_effect=lambda p, **k: p):
                index = NavigatorIndex(root=root)
                index.build(snapshot)
                index.manifest_path.write_text("not json", encoding="utf-8")
                status, _detail = index.status(snapshot)
                self.assertIs(status, IndexStatus.UNREADABLE)
                fulltext, status, _ = index.load_fulltext(snapshot)
                self.assertEqual(fulltext.chunk_count, 0)

    def test_index_is_never_the_source_of_truth(self):
        """Everything the index holds must be reconstructible from the catalog."""

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            catalog, topics = write_catalog(directory, [catalog_record()], [topic_record()])
            snapshot = CatalogReader(catalog_path=catalog, topics_path=topics).load()
            root = directory / "paper_retrieval"
            with patch("hunnu_harness.navigator.index.require_output_path", side_effect=lambda p, **k: p):
                index = NavigatorIndex(root=root)
                first = index.build(snapshot)
                index.drop()
                second = index.build(snapshot)
            self.assertEqual(
                first["extraction_status_counts"], second["extraction_status_counts"]
            )
            reloaded = CatalogReader(catalog_path=catalog, topics_path=topics).load()
            self.assertEqual(reloaded.work_count, snapshot.work_count)


if __name__ == "__main__":
    unittest.main()
