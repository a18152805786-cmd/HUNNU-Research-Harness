"""Regressions for the three defects found in black-box acceptance.

P0  shorthand citations ("Biddle et al. 2009") were structurally unverifiable
P1  index_status differed between commands looking at the same index
P2  structured CLI output inherited the console codepage and emitted GBK bytes

The synthetic parts run anywhere; the parts that need the real 179-WORK corpus
skip cleanly without it.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.navigator.catalog import CatalogReader
from hunnu_harness.navigator.citation import (
    MIN_IDENTITY_SIGNALS,
    CitationStatus,
    CitationVerifier,
    author_name_matches,
    parse_citation,
)
from hunnu_harness.navigator.cli import EXIT_AMBIGUOUS, EXIT_NOT_FOUND, EXIT_OK
from hunnu_harness.navigator.gaps import CoverageAnalyzer
from hunnu_harness.navigator.index import IndexStatus, NavigatorIndex
from hunnu_harness.navigator.packs import ReadingPackBuilder
from hunnu_harness.navigator.related import RelatedWorkFinder
from hunnu_harness.navigator.search import PaperNavigator
from hunnu_harness.paths import (
    CORE_ROOT,
    LIBRARY_CATALOG_JSONL,
    LIBRARY_TOPICS_JSONL,
    OUTPUT_ROOT,
    PAPER_RETRIEVAL_INDEX_DIR,
    is_within,
)

from test_navigator_core import catalog_record, topic_record, write_catalog

LIBRARY_PRESENT = LIBRARY_CATALOG_JSONL.is_file()
_SKIP = "real Global Paper Library is not present on this machine"

BIDDLE_WORK = "P48F6ACEC90DC"
WANG_LI_WORK = "P4451110E69BF"
VENV_PYTHON = CORE_ROOT / ".venv" / "Scripts" / "python.exe"


# ---------------------------------------------------------------- P0 ----


class AuthorNameMatchingTests(unittest.TestCase):
    def test_surname_matches_a_full_catalog_name(self):
        self.assertTrue(author_name_matches("biddle", "gary c biddle"))

    def test_unrelated_surname_does_not_match(self):
        self.assertFalse(author_name_matches("hilary", "gary c biddle"))

    def test_a_fragment_is_not_a_surname(self):
        """Substring matching would let 'idd' match 'biddle'; token matching does not."""

        self.assertFalse(author_name_matches("idd", "gary c biddle"))

    def test_single_cjk_glyph_never_matches(self):
        self.assertFalse(author_name_matches("王", "王海森"))

    def test_full_cjk_name_matches(self):
        self.assertTrue(author_name_matches("王海森", "王海森"))

    def test_distinct_cjk_names_do_not_match(self):
        self.assertFalse(author_name_matches("李纲", "王海森"))

    def test_empty_and_unknown_never_match(self):
        self.assertFalse(author_name_matches("", "gary c biddle"))
        self.assertFalse(author_name_matches("biddle", ""))
        self.assertFalse(author_name_matches("unknown", "unknown"))


class ShorthandCitationParsingTests(unittest.TestCase):
    def test_latin_et_al_is_not_an_author(self):
        parsed = parse_citation("Biddle et al. 2009")
        self.assertEqual(list(parsed.authors), ["Biddle"])
        self.assertEqual(parsed.year, "2009")

    def test_cjk_deng_suffix_is_stripped_from_the_surname(self):
        """'王海森等' is one surname plus the CJK 'et al.', not a 4-glyph name."""

        parsed = parse_citation("王海森等 2026")
        self.assertEqual(list(parsed.authors), ["王海森"])
        self.assertEqual(parsed.year, "2026")

    def test_standalone_deng_is_not_an_author(self):
        parsed = parse_citation("王海森 等 2026")
        self.assertNotIn("等", parsed.authors)

    def test_multiple_cjk_authors(self):
        parsed = parse_citation("王海森 李纲 2026")
        self.assertEqual(list(parsed.authors), ["王海森", "李纲"])

    def test_ampersand_form(self):
        parsed = parse_citation("Liu & Li 2026")
        self.assertIn("Liu", parsed.authors)
        self.assertIn("Li", parsed.authors)

    def test_three_author_and_form(self):
        parsed = parse_citation("Biddle, Hilary and Verdi (2009)")
        for name in ("Biddle", "Hilary", "Verdi"):
            self.assertIn(name, parsed.authors)

    def test_shorthand_records_no_title(self):
        """The whole string is not a title guess when authors and a year parsed.

        Putting the surname in the title field is what made the score
        unreachable: it scored 0 overlap against every real title.
        """

        self.assertEqual(parse_citation("Biddle et al. 2009").title_guess, "")
        self.assertEqual(parse_citation("王海森等 2026").title_guess, "")

    def test_full_citation_still_extracts_a_title(self):
        parsed = parse_citation(
            "Biddle, G. (2009). How does financial reporting quality relate to "
            "investment efficiency? JAE."
        )
        self.assertIn("financial reporting quality", parsed.title_guess.lower())


class CitationResolutionSemanticsTests(unittest.TestCase):
    """FOUND / AMBIGUOUS / NOT_IN_LIBRARY on synthetic fixtures."""

    def _navigator(self, directory: Path, records, topics):
        catalog, topics_path = write_catalog(directory, records, topics)
        return PaperNavigator(
            reader=CatalogReader(catalog_path=catalog, topics_path=topics_path),
            index=NavigatorIndex(root=directory / "paper_retrieval"),
        )

    def _record(self, paper_id, title, authors, year, doi):
        record = catalog_record(
            paper_id=paper_id, title=title, authors=authors, year=year, doi=doi,
            canonical_sha=paper_id.lower().ljust(64, "0"),
        )
        record["versions"][0]["managed_path"] = f"library/papers/{paper_id}.pdf"
        record["versions"][0]["sha256"] = record["sha256"]
        record["managed_pdf_path"] = record["versions"][0]["managed_path"]
        record["managed_fulltext_path"] = record["versions"][0]["managed_path"]
        return record

    def test_unique_author_year_shorthand_is_found(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            records = [
                self._record("PAAAAAAAAAAAA", "Accounting quality and investment",
                             ("Gary C. Biddle", "Gilles Hilary"), "2009", "10.1000/a"),
                self._record("PBBBBBBBBBBBB", "Supply chain resilience",
                             ("Wei Chen",), "2021", "10.1000/b"),
            ]
            topics = [topic_record("PAAAAAAAAAAAA"), topic_record("PBBBBBBBBBBBB")]
            navigator = self._navigator(directory, records, topics)
            payload = CitationVerifier(navigator).verify("Biddle et al. 2009")
            self.assertEqual(payload["status"], CitationStatus.IN_LIBRARY.value)
            self.assertEqual(payload["paper_id"], "PAAAAAAAAAAAA")

    def test_two_works_by_the_same_author_and_year_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            records = [
                self._record("PAAAAAAAAAAAA", "Accounting quality and investment",
                             ("Gary C. Biddle",), "2009", "10.1000/a"),
                self._record("PBBBBBBBBBBBB", "Accounting conservatism and debt",
                             ("Gary C. Biddle",), "2009", "10.1000/b"),
            ]
            topics = [topic_record("PAAAAAAAAAAAA"), topic_record("PBBBBBBBBBBBB")]
            navigator = self._navigator(directory, records, topics)
            payload = CitationVerifier(navigator).verify("Biddle et al. 2009")
            self.assertEqual(payload["status"], CitationStatus.AMBIGUOUS.value)
            self.assertIsNone(payload["paper_id"])
            self.assertIsNone(payload["match"])
            self.assertEqual(payload["candidate_count"], 2)
            self.assertEqual(payload["suggested_next_action"], "DISAMBIGUATE")

    def test_ambiguity_is_resolved_by_adding_a_title_fragment(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            records = [
                self._record("PAAAAAAAAAAAA", "Accounting quality and investment",
                             ("Gary C. Biddle",), "2009", "10.1000/a"),
                self._record("PBBBBBBBBBBBB", "Accounting conservatism and debt",
                             ("Gary C. Biddle",), "2009", "10.1000/b"),
            ]
            topics = [topic_record("PAAAAAAAAAAAA"), topic_record("PBBBBBBBBBBBB")]
            navigator = self._navigator(directory, records, topics)
            payload = CitationVerifier(navigator).verify(
                "Biddle (2009). Accounting conservatism and debt."
            )
            self.assertEqual(payload["status"], CitationStatus.IN_LIBRARY.value)
            self.assertEqual(payload["paper_id"], "PBBBBBBBBBBBB")

    def test_right_author_wrong_year_is_not_in_library(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            records = [
                self._record("PAAAAAAAAAAAA", "Accounting quality and investment",
                             ("Gary C. Biddle",), "2009", "10.1000/a"),
            ]
            navigator = self._navigator(directory, records, [topic_record("PAAAAAAAAAAAA")])
            payload = CitationVerifier(navigator).verify("Biddle 1994")
            self.assertEqual(payload["status"], CitationStatus.NOT_IN_LIBRARY.value)
            self.assertIsNone(payload["paper_id"])

    def test_a_single_signal_is_never_enough(self):
        """An author with no year names no particular paper."""

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            records = [
                self._record("PAAAAAAAAAAAA", "Accounting quality and investment",
                             ("Gary C. Biddle",), "2009", "10.1000/a"),
            ]
            navigator = self._navigator(directory, records, [topic_record("PAAAAAAAAAAAA")])
            payload = CitationVerifier(navigator).verify("Biddle")
            self.assertEqual(payload["status"], CitationStatus.NOT_IN_LIBRARY.value)

    def test_unknown_author_is_not_in_library(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            records = [
                self._record("PAAAAAAAAAAAA", "Accounting quality and investment",
                             ("Gary C. Biddle",), "2009", "10.1000/a"),
            ]
            navigator = self._navigator(directory, records, [topic_record("PAAAAAAAAAAAA")])
            payload = CitationVerifier(navigator).verify("Nonexistentsurname et al. 2009")
            self.assertEqual(payload["status"], CitationStatus.NOT_IN_LIBRARY.value)
            self.assertEqual(payload["suggested_next_action"], "ACQUISITION")

    def test_minimum_identity_signals_is_at_least_two(self):
        self.assertGreaterEqual(MIN_IDENTITY_SIGNALS, 2)


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class BiddleShorthandAcceptanceTests(unittest.TestCase):
    """The reported defect, on the real corpus, without hard-coding the answer."""

    @classmethod
    def setUpClass(cls):
        cls.navigator = PaperNavigator(reader=CatalogReader())
        cls.verifier = CitationVerifier(cls.navigator)

    def _expected_work(self):
        """Find the target by its bibliographic identity, not by its paper_id.

        If this resolved by a hard-coded id the test would pass even with the
        resolver removed, which is the opposite of what it is for.
        """

        for work in self.navigator.snapshot.works:
            if work.year == "2009" and any(
                "biddle" in author for author in work.normalized_authors
            ):
                return work
        self.fail("no 2009 Biddle work in the corpus to test against")

    def test_the_corpus_really_contains_the_work(self):
        self.assertEqual(self._expected_work().paper_id, BIDDLE_WORK)

    def test_biddle_et_al_shorthand_is_found(self):
        payload = self.verifier.verify("Biddle et al. 2009")
        self.assertEqual(payload["status"], CitationStatus.IN_LIBRARY.value)
        self.assertEqual(payload["paper_id"], self._expected_work().paper_id)

    def test_biddle_year_shorthand_is_found(self):
        payload = self.verifier.verify("Biddle 2009")
        self.assertEqual(payload["paper_id"], self._expected_work().paper_id)

    def test_full_citation_still_found(self):
        payload = self.verifier.verify(
            "Biddle, G., Hilary, G., Verdi, R. (2009). How does financial reporting "
            "quality relate to investment efficiency? JAE."
        )
        self.assertEqual(payload["paper_id"], self._expected_work().paper_id)

    def test_doi_still_found(self):
        work = self._expected_work()
        payload = self.verifier.verify(f"Anon. doi:{work.doi}")
        self.assertEqual(payload["paper_id"], work.paper_id)

    def test_chinese_shorthand_with_deng_is_found(self):
        payload = self.verifier.verify("王海森等 2026")
        self.assertEqual(payload["status"], CitationStatus.IN_LIBRARY.value)
        self.assertEqual(payload["paper_id"], WANG_LI_WORK)

    def test_chinese_two_author_shorthand_is_found(self):
        payload = self.verifier.verify("王海森 李纲 2026")
        self.assertEqual(payload["paper_id"], WANG_LI_WORK)

    def test_fictional_shorthand_is_still_refused(self):
        for citation in (
            "Zhang, Testonly & Fakeauthor (2099), Artificial Intelligence Washing "
            "on Lunar Mining Firms",
            "Okonkwo et al. 2031",
            "赵子龙等 2030",
            "Nonexistentsurname 2020",
        ):
            with self.subTest(citation=citation):
                payload = self.verifier.verify(citation)
                self.assertEqual(payload["status"], CitationStatus.NOT_IN_LIBRARY.value)
                self.assertIsNone(payload["paper_id"])
                self.assertIsNone(payload["match"])

    def test_real_author_wrong_year_is_refused(self):
        payload = self.verifier.verify("Biddle 1899")
        self.assertEqual(payload["status"], CitationStatus.NOT_IN_LIBRARY.value)

    def test_bare_surname_is_refused(self):
        for citation in ("Biddle", "王海森"):
            with self.subTest(citation=citation):
                self.assertEqual(
                    self.verifier.verify(citation)["status"],
                    CitationStatus.NOT_IN_LIBRARY.value,
                )

    def test_a_found_result_never_omits_its_evidence(self):
        payload = self.verifier.verify("Biddle et al. 2009")
        evidence = payload["match"]["evidence"]
        self.assertIn("fields_supplied", evidence)
        self.assertIn("identity_signals", evidence)
        self.assertGreaterEqual(evidence["identity_signals"], MIN_IDENTITY_SIGNALS)


# ---------------------------------------------------------------- P1 ----


def observe_index_states(navigator: PaperNavigator) -> dict[str, str]:
    """index_status as reported by every agent-facing command, one process."""

    return {
        "paper-search": navigator.search("AI washing", top=3)["index_status"],
        "paper-search --no-fulltext": navigator.search(
            "AI washing", top=3, use_fulltext=False
        )["index_status"],
        "paper-lookup": navigator.lookup("AI washing")["index_status"],
        "paper-fulltext": navigator.fulltext(
            navigator.snapshot.works[0].paper_id
        )["index_status"],
        "paper-related": RelatedWorkFinder(navigator).find(
            navigator.snapshot.works[0].paper_id, top=3
        )["index_status"],
        "paper-pack": ReadingPackBuilder(navigator).build(
            "AI washing", top=3, write=False
        )["index_status"],
        "paper-gaps": CoverageAnalyzer(navigator).analyze("AI washing", top=5)["index_status"],
        "paper-verify-citation": CitationVerifier(navigator).verify(
            "Biddle et al. 2009"
        )["index_status"],
    }


class IndexStatusConsistencyTests(unittest.TestCase):
    """One index, one reported state -- whatever the command."""

    def _fixture(self, directory: Path):
        records = [catalog_record(), catalog_record(paper_id="PBBBBBBBBBBBB", doi="10.1000/b")]
        topics = [topic_record(), topic_record("PBBBBBBBBBBBB")]
        catalog, topics_path = write_catalog(directory, records, topics)
        return CatalogReader(catalog_path=catalog, topics_path=topics_path), catalog, topics_path

    def test_absent_index_is_absent_for_every_command(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            reader, _catalog, _topics = self._fixture(directory)
            navigator = PaperNavigator(
                reader=reader, index=NavigatorIndex(root=directory / "no_index")
            )
            states = observe_index_states(navigator)
            self.assertEqual(set(states.values()), {IndexStatus.ABSENT.value}, states)

    def test_fresh_index_is_fresh_for_every_command(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            reader, _catalog, _topics = self._fixture(directory)
            index = NavigatorIndex(root=directory / "paper_retrieval")
            with patch(
                "hunnu_harness.navigator.index.require_output_path",
                side_effect=lambda p, **k: p,
            ):
                index.build(reader.load())
            navigator = PaperNavigator(reader=reader, index=index)
            states = observe_index_states(navigator)
            self.assertEqual(set(states.values()), {IndexStatus.FRESH.value}, states)

    def test_stale_index_is_stale_for_every_command(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            reader, catalog, topics_path = self._fixture(directory)
            index = NavigatorIndex(root=directory / "paper_retrieval")
            with patch(
                "hunnu_harness.navigator.index.require_output_path",
                side_effect=lambda p, **k: p,
            ):
                index.build(reader.load())
            # Change the catalog fingerprint without changing any work.
            catalog.write_text(
                catalog.read_text(encoding="utf-8-sig") + "\n", encoding="utf-8", newline="\n"
            )
            navigator = PaperNavigator(
                reader=CatalogReader(catalog_path=catalog, topics_path=topics_path), index=index
            )
            states = observe_index_states(navigator)
            self.assertEqual(set(states.values()), {IndexStatus.STALE.value}, states)

    def test_corrupt_manifest_is_unreadable_for_every_command(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            reader, _catalog, _topics = self._fixture(directory)
            index = NavigatorIndex(root=directory / "paper_retrieval")
            index.index_dir.mkdir(parents=True, exist_ok=True)
            index.manifest_path.write_text("{{{ not json", encoding="utf-8")
            navigator = PaperNavigator(reader=reader, index=index)
            states = observe_index_states(navigator)
            self.assertEqual(set(states.values()), {IndexStatus.UNREADABLE.value}, states)

    def test_a_not_yet_loaded_cache_is_not_reported_as_absent(self):
        """The exact defect: lookup used to answer ABSENT on a FRESH index."""

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            reader, _catalog, _topics = self._fixture(directory)
            index = NavigatorIndex(root=directory / "paper_retrieval")
            with patch(
                "hunnu_harness.navigator.index.require_output_path",
                side_effect=lambda p, **k: p,
            ):
                index.build(reader.load())
            navigator = PaperNavigator(reader=reader, index=index)
            # Nothing has loaded the full-text layer yet.
            self.assertIsNone(navigator._fulltext)
            self.assertEqual(
                navigator.lookup("AI washing")["index_status"], IndexStatus.FRESH.value
            )

    def test_status_is_computed_without_reading_chunk_files(self):
        """Consistency must not cost every command a full chunk load."""

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            reader, _catalog, _topics = self._fixture(directory)
            index = NavigatorIndex(root=directory / "paper_retrieval")
            with patch(
                "hunnu_harness.navigator.index.require_output_path",
                side_effect=lambda p, **k: p,
            ):
                index.build(reader.load())
            navigator = PaperNavigator(reader=reader, index=index)
            with patch.object(
                NavigatorIndex, "_read_chunks", side_effect=AssertionError("chunks read")
            ):
                self.assertEqual(
                    navigator.index_status()[0].value, IndexStatus.FRESH.value
                )


@unittest.skipUnless(LIBRARY_PRESENT, _SKIP)
class RealIndexStatusConsistencyTests(unittest.TestCase):
    def test_all_commands_agree_on_the_real_index(self):
        navigator = PaperNavigator(reader=CatalogReader())
        states = observe_index_states(navigator)
        self.assertEqual(len(set(states.values())), 1, states)

    def test_derived_index_stays_in_the_output_root(self):
        self.assertTrue(is_within(PAPER_RETRIEVAL_INDEX_DIR, OUTPUT_ROOT))
        self.assertFalse(is_within(PAPER_RETRIEVAL_INDEX_DIR, CORE_ROOT))
        self.assertEqual(PAPER_RETRIEVAL_INDEX_DIR.name, "index")
        self.assertEqual(PAPER_RETRIEVAL_INDEX_DIR.parent.name, "paper_retrieval")


# ---------------------------------------------------------------- P2 ----


CHINESE_PROBES = ("人工智能漂洗", "盈余管理", "审计风险", "企业技术创新", "王海森", "李纲")


def run_cli(args, *, env_overrides=None):
    """Run the real CLI in a child process and capture raw bytes."""

    env = dict(os.environ)
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [str(VENV_PYTHON), "-m", "hunnu_harness.cli", *args],
        capture_output=True,
        cwd=str(CORE_ROOT),
        env=env,
    )


class UnicodeInternalTests(unittest.TestCase):
    def test_emit_writes_utf8_bytes_regardless_of_stream_encoding(self):
        from hunnu_harness.navigator import cli as navigator_cli

        class GbkStream:
            """A stdout whose text layer would happily encode CJK as GBK."""

            def __init__(self):
                self.buffer = _Buffer()

            def flush(self):
                pass

        class _Buffer:
            def __init__(self):
                self.written = b""

            def write(self, data):
                self.written += data

            def flush(self):
                pass

        stream = GbkStream()
        with patch.object(navigator_cli.sys, "stdout", stream):
            navigator_cli.emit_utf8("人工智能漂洗")
        self.assertEqual(stream.buffer.written, "人工智能漂洗\n".encode("utf-8"))
        self.assertNotEqual(stream.buffer.written, "人工智能漂洗\n".encode("gbk"))

    def test_emit_falls_back_to_print_without_a_byte_buffer(self):
        from hunnu_harness.navigator import cli as navigator_cli

        class TextOnly:
            """A capture stream that holds Unicode and exposes no byte buffer."""

            buffer = None

            def __init__(self):
                self.written = ""

            def write(self, data):
                self.written += data
                return len(data)

            def flush(self):
                pass

        stream = TextOnly()
        with patch.object(navigator_cli.sys, "stdout", stream):
            navigator_cli.emit_utf8("人工智能漂洗")
        self.assertIn("人工智能漂洗", stream.written)


@unittest.skipUnless(
    LIBRARY_PRESENT and VENV_PYTHON.is_file(),
    "needs the real library and the project venv",
)
class ChineseCliOutputTests(unittest.TestCase):
    """The CLI must hand an agent readable Chinese, whatever the console is."""

    def test_lookup_output_is_valid_utf8_and_roundtrips(self):
        result = run_cli(["paper-lookup", "--query", "人工智能漂洗抹杀了企业技术创新吗"])
        self.assertEqual(result.returncode, EXIT_OK)
        decoded = result.stdout.decode("utf-8")  # raises if not valid UTF-8
        payload = json.loads(decoded)
        match = payload["matches"][0]
        self.assertEqual(match["title"], "人工智能漂洗抹杀了企业技术创新吗")
        self.assertEqual(match["authors"], ["王海森", "李纲"])

    def test_no_replacement_characters_or_question_mark_runs(self):
        result = run_cli(["paper-search", "--query", "人工智能漂洗 审计风险", "--top", "5"])
        decoded = result.stdout.decode("utf-8")
        self.assertNotIn("�", decoded)
        self.assertNotIn("???", decoded)

    def test_every_chinese_probe_survives_the_argv_to_stdout_roundtrip(self):
        """Chinese in, Chinese out: argv -> Python -> JSON -> stdout bytes."""

        for probe in CHINESE_PROBES:
            with self.subTest(probe=probe):
                result = run_cli(["paper-search", "--query", probe, "--top", "3"])
                payload = json.loads(result.stdout.decode("utf-8"))
                self.assertEqual(payload["query"], probe)
                self.assertEqual(payload["query_normalization"]["original"], probe)

    def test_chinese_titles_authors_and_topics_all_survive(self):
        result = run_cli(["paper-search", "--query", "人工智能漂洗 审计风险", "--top", "8"])
        payload = json.loads(result.stdout.decode("utf-8"))
        titles = [row["title"] for row in payload["results"]]
        authors = [name for row in payload["results"] for name in row["authors"]]
        topics = [topic for row in payload["results"] for topic in row["topics"]]

        self.assertTrue(any("漂洗" in title for title in titles), titles[:4])
        self.assertTrue(any("漂洗" in topic for topic in topics), topics[:6])
        self.assertTrue(any(any("一" <= c <= "鿿" for c in a) for a in authors))
        for field in (*titles, *authors, *topics):
            self.assertNotIn("�", field)

    def test_output_is_utf8_under_a_hostile_console_codepage(self):
        """GBK can encode CJK, so it fails silently -- the case the old code missed."""

        for encoding in (None, "gbk", "cp936", "cp1252", "ascii"):
            with self.subTest(encoding=encoding):
                result = run_cli(
                    ["paper-lookup", "--query", "人工智能漂洗抹杀了企业技术创新吗"],
                    env_overrides={"PYTHONIOENCODING": encoding},
                )
                self.assertEqual(result.returncode, EXIT_OK)
                decoded = result.stdout.decode("utf-8")
                payload = json.loads(decoded)
                self.assertEqual(
                    payload["matches"][0]["title"], "人工智能漂洗抹杀了企业技术创新吗"
                )

    def test_english_output_is_unaffected(self):
        result = run_cli(["paper-lookup", "--query", "DOI:10.1016/j.jacceco.2009.09.001"])
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(
            payload["matches"][0]["title"],
            "How does financial reporting quality relate to investment efficiency?",
        )

    def test_ambiguous_citation_uses_its_own_exit_code(self):
        result = run_cli(["paper-verify-citation", "--citation", "Liu & Li 2026"])
        payload = json.loads(result.stdout.decode("utf-8"))
        if payload["status"] == CitationStatus.AMBIGUOUS.value:
            self.assertEqual(result.returncode, EXIT_AMBIGUOUS)
        else:  # corpus changed; the code path is still exercised by the unit tests
            self.assertIn(result.returncode, (EXIT_OK, EXIT_NOT_FOUND))

    def test_shorthand_citation_via_the_real_cli(self):
        result = run_cli(["paper-verify-citation", "--citation", "Biddle et al. 2009"])
        self.assertEqual(result.returncode, EXIT_OK)
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(payload["status"], CitationStatus.IN_LIBRARY.value)
        self.assertEqual(payload["paper_id"], BIDDLE_WORK)


if __name__ == "__main__":
    unittest.main()
