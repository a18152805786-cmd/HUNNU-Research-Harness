"""Focused tests for replaceable Navigator and taxonomy vocabulary data."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import importlib.resources as package_resources

from hunnu_harness import paths
from hunnu_harness.literature import taxonomy_aliases
from hunnu_harness.literature.models import LiteratureSearchRequest
from hunnu_harness.navigator import lexicon


class ConfigurableVocabularyTests(unittest.TestCase):
    def test_packaged_defaults_are_loaded_and_keep_the_canary_match(self) -> None:
        self.assertEqual(len(lexicon.CONCEPTS), 20)
        self.assertEqual(lexicon.CONCEPTS_BY_KEY["ai_washing"].terms[0], "ai washing")
        self.assertTrue(any(match.key == "ai_washing" for match in lexicon.match_concepts("AI washing")))
        self.assertIn("AI漂洗", taxonomy_aliases.ENGLISH_SUBTOPIC_ALIASES)

    def test_a_single_user_lexicon_override_replaces_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            override = root / "navigator_lexicon.json"
            override.write_text(
                json.dumps(
                    {
                        "concepts": [
                            {
                                "key": "only_concept",
                                "facet": "CONTEXT",
                                "terms": ["opaque signal"],
                                "topics": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(paths, "NAVIGATOR_LEXICON_JSON", override):
                loaded = lexicon.load_concepts(default_resource=None)
            self.assertEqual(tuple(concept.key for concept in loaded), ("only_concept",))

    def test_missing_override_and_default_are_a_valid_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            missing_override = Path(raw) / "missing-override.json"
            missing_default = Path(raw) / "missing-default.json"
            missing_root = Path(raw) / "missing-package"
            with (
                patch.object(paths, "NAVIGATOR_LEXICON_JSON", missing_override),
                patch.object(paths, "TAXONOMY_ALIASES_JSON", missing_override),
                patch.object(package_resources, "files", return_value=missing_root),
            ):
                empty_concepts = lexicon.load_concepts()
                empty_aliases = taxonomy_aliases.load_taxonomy_aliases()
            with (
                patch.object(paths, "NAVIGATOR_LEXICON_JSON", missing_override),
                patch.object(paths, "TAXONOMY_ALIASES_JSON", missing_override),
                patch.object(lexicon, "CONCEPTS", empty_concepts),
                patch.object(lexicon, "CONCEPTS_BY_KEY", {}),
                patch.object(lexicon, "_TERM_LOOKUP", {}),
                patch.object(lexicon, "_LONGEST_TERM", 1),
                patch.object(taxonomy_aliases, "ENGLISH_SUBTOPIC_ALIASES", empty_aliases),
                patch.object(taxonomy_aliases, "_FOLDED_ALIASES", {}),
            ):
                self.assertEqual(lexicon.CONCEPTS, ())
                self.assertEqual(lexicon.match_concepts("AI washing"), ())
                self.assertEqual(lexicon.expansion_terms(()), {})
                self.assertEqual(lexicon.topics_for_concepts(()), ())
                self.assertEqual(dict(taxonomy_aliases.ENGLISH_SUBTOPIC_ALIASES), {})

                request = LiteratureSearchRequest.from_natural_language(
                    "找 AI washing 的文献，2024-2025", current_year=2026
                )
                self.assertEqual(request.keywords_en, ())
                self.assertEqual(request.keywords_cn, ())
                self.assertEqual(request.year_start, 2024)
                self.assertEqual(request.year_end, 2025)

    def test_invalid_json_fails_closed_with_path_and_repair_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bad_lexicon = root / "bad-lexicon.json"
            bad_aliases = root / "bad-aliases.json"
            bad_lexicon.write_text("{", encoding="utf-8")
            bad_aliases.write_text("{", encoding="utf-8")

            with self.assertRaises(ValueError) as lexicon_error:
                lexicon.load_concepts(override_path=bad_lexicon, default_resource=None)
            self.assertIn(str(bad_lexicon), str(lexicon_error.exception))
            self.assertIn("Repair", str(lexicon_error.exception))

            with self.assertRaises(ValueError) as aliases_error:
                taxonomy_aliases.load_taxonomy_aliases(
                    override_path=bad_aliases, default_resource=None
                )
            self.assertIn(str(bad_aliases), str(aliases_error.exception))
            self.assertIn("Repair", str(aliases_error.exception))

    def test_invalid_lexicon_shape_and_facet_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "invalid-lexicon.json"
            path.write_text(
                json.dumps(
                    {
                        "concepts": [
                            {
                                "key": "broken",
                                "facet": "NOT_A_FACET",
                                "terms": ["signal"],
                                "topics": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as error:
                lexicon.load_concepts(override_path=path, default_resource=None)
            self.assertIn(str(path), str(error.exception))
            self.assertIn("facet", str(error.exception))

    def test_natural_language_uses_folded_default_terms(self) -> None:
        request = LiteratureSearchRequest.from_natural_language("找 AI washing 的文献")
        self.assertIn("ai washing", request.keywords_en)


if __name__ == "__main__":
    unittest.main()
