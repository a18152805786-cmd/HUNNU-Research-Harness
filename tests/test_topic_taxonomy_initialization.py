"""A reviewed frozen taxonomy is copied once, never inferred or replaced."""

from __future__ import annotations

import hashlib
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness import paths
from hunnu_harness.literature import topics


class TopicTaxonomyInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        paths.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="taxonomy-init-", dir=paths.TEMP_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "reviewed.json"
        self.raw = b'\xef\xbb\xbf' + json.dumps({"domains": {"Fixture domain": ["Fixture topic"]}}).encode("utf-8")
        self.source.write_bytes(self.raw)
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.destination = self.root / "output" / "topic_taxonomy.json"
        self.patch_destination = patch.object(topics, "TOPIC_TAXONOMY_JSON", self.destination)
        self.patch_destination.start()
        self.addCleanup(self.patch_destination.stop)

    def test_exact_reviewed_bytes_are_installed_without_touching_source_or_library(self) -> None:
        result = topics.initialize_topic_taxonomy(self.source, expected_sha256=self.digest)
        self.assertEqual(self.destination.read_bytes(), self.raw)
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertEqual(topics.TopicTaxonomy.load(self.destination).subtopics(), ("Fixture topic",))
        self.assertEqual(result["SHA256"], self.digest)
        self.assertEqual(result["Topics"], 1)
        self.assertFalse(result["ExistingTaxonomyReplaced"])
        self.assertFalse(result["TopicMetadataModified"])
        self.assertEqual([p.name for p in self.destination.parent.iterdir()], ["topic_taxonomy.json"])

    def test_wrong_or_missing_digest_refuses_before_any_destination_write(self) -> None:
        for digest in ("", "not-a-hash", "0" * 64):
            with self.subTest(digest=digest), self.assertRaises(topics.TopicTaxonomyError):
                topics.initialize_topic_taxonomy(self.source, expected_sha256=digest)
            self.assertFalse(self.destination.parent.exists())

    def test_invalid_schema_is_not_installed_even_with_correct_digest(self) -> None:
        for payload in ([], {"domains": {}}, {"domains": {"D": []}}, {"domains": {"D": [None]}}, {"domains": {"A": ["same"], "B": ["same"]}}):
            raw = json.dumps(payload).encode("utf-8")
            self.source.write_bytes(raw)
            with self.subTest(payload=payload), self.assertRaises(topics.TopicTaxonomyError):
                topics.initialize_topic_taxonomy(self.source, expected_sha256=hashlib.sha256(raw).hexdigest())
            self.assertFalse(self.destination.parent.exists())

    def test_ambiguous_storage_labels_and_invalid_directory_components_are_refused(self) -> None:
        invalid = (
            " topic", "topic ", "topic;another", "D\\topic", "D/topic", ".", "..",
            "topic.", "NUL", "nul.json", "COM1", "LPT9.txt", "com\u00b9", "a:b", "a?b",
            'a"b', "a|b", "a<b", "a>b", "a*b", "a\x00b", "a\nb", "a\tb", "a\x7fb",
            "x" * 256, "\ud800",
        )
        for value in invalid:
            for domains in ({"Good domain": [value]}, {value: ["Good topic"]}):
                raw = json.dumps({"domains": domains}).encode("utf-8")
                self.source.write_bytes(raw)
                with self.subTest(value=repr(value), domains=domains):
                    with self.assertRaises(topics.TopicTaxonomyError):
                        topics.initialize_topic_taxonomy(
                            self.source, expected_sha256=hashlib.sha256(raw).hexdigest()
                        )
                    self.assertEqual(self.source.read_bytes(), raw, "reviewed bytes must never be normalized")
                    self.assertFalse(self.destination.parent.exists())

    def test_valid_labels_round_trip_through_the_stored_field_without_normalization(self) -> None:
        taxonomy = topics.TopicTaxonomy.from_mapping({
            "domains": {"01_治理与金融": ["公司治理", "Financing & allocation"], "02_审计": ["审计意见"]}
        })
        labels = taxonomy.labels()
        parsed = topics.parse_topics_field(topics.format_topics_field(labels))
        self.assertEqual(parsed, labels)
        self.assertEqual(taxonomy.require_known(parsed), labels)

    def test_existing_taxonomy_is_never_replaced_even_by_identical_bytes(self) -> None:
        self.destination.parent.mkdir()
        self.destination.write_bytes(self.raw)
        with self.assertRaisesRegex(topics.TopicTaxonomyError, "will not be replaced"):
            topics.initialize_topic_taxonomy(self.source, expected_sha256=self.digest)
        self.assertEqual(self.destination.read_bytes(), self.raw)

    def test_concurrent_destination_creation_cannot_be_overwritten(self) -> None:
        concurrent = b"another approved configuration"

        def race(_source, destination):
            Path(destination).write_bytes(concurrent)
            raise FileExistsError("another initializer won")

        with patch.object(topics.os, "link", side_effect=race):
            with self.assertRaisesRegex(topics.TopicTaxonomyError, "nothing was replaced"):
                topics.initialize_topic_taxonomy(self.source, expected_sha256=self.digest)
        self.assertEqual(self.destination.read_bytes(), concurrent)
        self.assertEqual([p.name for p in self.destination.parent.iterdir()], ["topic_taxonomy.json"])

    def test_parent_creation_failure_is_a_controlled_taxonomy_error(self) -> None:
        with patch.object(type(self.destination), "mkdir", side_effect=PermissionError("mkdir denied")):
            with self.assertRaisesRegex(topics.TopicTaxonomyError, "mkdir denied"):
                topics.initialize_topic_taxonomy(self.source, expected_sha256=self.digest)
        self.assertFalse(self.destination.exists())

    def test_cli_reports_a_directory_creation_failure_as_one_json_refusal(self) -> None:
        from hunnu_harness.cli import main

        stdout = io.StringIO()
        with patch.object(type(self.destination), "mkdir", side_effect=PermissionError("mkdir denied")):
            with contextlib.redirect_stdout(stdout):
                code = main([
                    "library-init-taxonomy", "--source", str(self.source),
                    "--sha256", self.digest, "--json",
                ])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["Status"], "REFUSED")
        self.assertIn("mkdir denied", payload["Reason"])
        self.assertFalse(self.destination.exists())

    def test_cleanup_failure_does_not_turn_a_committed_configuration_into_a_failure(self) -> None:
        original_unlink = type(self.destination).unlink

        def fail_pending(path, *args, **kwargs):
            if path.name.endswith(".pending"):
                raise PermissionError("cleanup denied")
            return original_unlink(path, *args, **kwargs)

        with patch.object(type(self.destination), "unlink", new=fail_pending):
            result = topics.initialize_topic_taxonomy(self.source, expected_sha256=self.digest)
        self.assertEqual(result["Status"], "INITIALIZED")
        self.assertEqual(self.destination.read_bytes(), self.raw)
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertFalse(result["TemporaryFileCleanupComplete"])
        self.assertIn("temporary hardlink", result["Warnings"][0])

    def test_cleanup_failure_does_not_hide_the_original_commit_error(self) -> None:
        original_unlink = type(self.destination).unlink

        def fail_pending(path, *args, **kwargs):
            if path.name.endswith(".pending"):
                raise PermissionError("cleanup denied")
            return original_unlink(path, *args, **kwargs)

        with patch.object(topics.os, "link", side_effect=OSError("commit denied")):
            with patch.object(type(self.destination), "unlink", new=fail_pending):
                with self.assertRaisesRegex(topics.TopicTaxonomyError, "commit denied") as error:
                    topics.initialize_topic_taxonomy(self.source, expected_sha256=self.digest)
        self.assertNotIn("cleanup denied", str(error.exception))
        self.assertFalse(self.destination.exists())

    def test_core_root_cannot_be_a_runtime_configuration_destination(self) -> None:
        forbidden = paths.CORE_ROOT / "topic_taxonomy.json"
        with patch.object(topics, "TOPIC_TAXONOMY_JSON", forbidden):
            with self.assertRaises(ValueError):
                topics.initialize_topic_taxonomy(self.source, expected_sha256=self.digest)
        self.assertFalse(forbidden.exists())


if __name__ == "__main__":
    unittest.main()
