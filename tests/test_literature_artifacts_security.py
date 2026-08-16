import csv
import json
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.literature.artifacts import (
    QUERY_LOG_FIELDS,
    SEARCH_RESULT_FIELDS,
    LiteratureArtifactWriter,
)
from hunnu_harness.literature.models import (
    DownloadManifestEntry,
    LiteratureRecord,
    LiteratureSearchRequest,
    QueryLogEntry,
)
from hunnu_harness.literature.security import (
    LiteratureAuditLogger,
    sanitize_text,
    sanitize_url,
    sanitize_value,
    scan_files_for_sensitive_leaks,
    scan_text_for_sensitive_leaks,
)


class LiteratureArtifactTests(unittest.TestCase):
    def _request(self):
        return LiteratureSearchRequest.from_mapping(
            {
                "OriginalResearchRequest": "AI washing and audit",
                "KeywordsEN": ["AI washing", "audit"],
                "MaxDownloads": 1,
                "MaxDownloadsPerRun": 1,
            }
        )

    def test_writer_generates_all_required_run_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = LiteratureArtifactWriter(Path(tmp), allow_outside_project_for_tests=True)
            request = self._request()
            record = LiteratureRecord(
                paper_id="P1",
                title="AI washing and audit",
                authors=("A. Author",),
                source_database="ScienceDirect",
                screening_decision="KEEP",
                screening_reason="Direct match",
            )
            writer.write_search_request(request)
            writer.append_query_log(
                QueryLogEntry("2026-08-15T00:00:00Z", "ScienceDirect", request.original_research_request, '"AI washing" AND audit', "{}", 1, 1)
            )
            writer.write_search_results([record])
            writer.write_screening_decisions([record])
            writer.write_download_manifest([])
            writer.write_sha256s([])
            writer.write_run_audit(
                status="SUCCESS", source="ScienceDirect", query_count=1, result_count=1, download_count=0
            )
            writer.write_obsidian_handoff([record])
            required = {
                "SEARCH_REQUEST.json",
                "SEARCH_QUERY_LOG.csv",
                "SEARCH_RESULTS.csv",
                "SCREENING_DECISIONS.csv",
                "DOWNLOAD_MANIFEST.json",
                "SHA256SUMS.txt",
                "RUN_AUDIT.md",
                "OBSIDIAN_HANDOFF_MANIFEST.json",
            }
            self.assertTrue(required.issubset({path.name for path in Path(tmp).iterdir()}))

    def test_csv_headers_match_required_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = LiteratureArtifactWriter(Path(tmp), allow_outside_project_for_tests=True)
            request = self._request()
            writer.append_query_log(QueryLogEntry("now", "source", "request", "query", "{}", 0, 0))
            writer.write_search_results([LiteratureRecord("P1")])
            with writer.query_log_path.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(tuple(next(csv.reader(handle))), QUERY_LOG_FIELDS)
            with writer.search_results_path.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(tuple(next(csv.reader(handle))), SEARCH_RESULT_FIELDS)

    def test_download_manifest_contains_authorization_hash_and_no_auth_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = LiteratureArtifactWriter(Path(tmp), allow_outside_project_for_tests=True)
            entry = DownloadManifestEntry(
                paper_id="P1",
                source="ScienceDirect",
                title="Title",
                doi="10.1000/test",
                access_type="InstitutionalAuthenticated",
                authorized_access=True,
                original_url_or_stable_identifier="S123",
                original_filename="original.pdf",
                normalized_filename="2026_A_Title.pdf",
                download_timestamp="now",
                file_size_bytes=100,
                sha256="a" * 64,
                local_path=str(Path(tmp) / "2026_A_Title.pdf"),
                pdf_validation_passed=True,
            )
            writer.write_download_manifest([entry])
            payload = json.loads(writer.download_manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["Downloads"][0]["AuthorizedAccess"])
            self.assertEqual(payload["Downloads"][0]["SHA256"], "a" * 64)
            serialized = json.dumps(payload).casefold()
            self.assertNotIn("authorization\"", serialized)
            self.assertNotIn("cookie\"", serialized)

    def test_obsidian_handoff_is_manifest_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = LiteratureArtifactWriter(Path(tmp), allow_outside_project_for_tests=True)
            record = LiteratureRecord(
                paper_id="P1",
                title="Title",
                full_text_downloaded=True,
                local_path=str(Path(tmp) / "paper.pdf"),
                sha256="b" * 64,
            )
            writer.write_obsidian_handoff([record])
            payload = json.loads(writer.handoff_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["AutoWriteToObsidian"])
            self.assertEqual(len(payload["Papers"]), 1)
            self.assertEqual({path.name for path in Path(tmp).iterdir()}, {"downloads", "OBSIDIAN_HANDOFF_MANIFEST.json"})


class LiteratureSecurityTests(unittest.TestCase):
    def test_sensitive_keys_and_labeled_values_are_redacted(self):
        payload = sanitize_value({"password": "secret-value", "nested": {"sessionid": "abc123"}, "safe": "ok"})
        self.assertEqual(payload["password"], "<REDACTED>")
        self.assertEqual(payload["nested"]["sessionid"], "<REDACTED>")
        self.assertEqual(payload["safe"], "ok")
        self.assertIn("password=<REDACTED>", sanitize_text("password=secret-value"))

    def test_stable_url_removes_userinfo_query_and_fragment(self):
        value = sanitize_url("https://user:pass@example.test/article/1?token=abc&x=1#fragment")
        self.assertEqual(value, "https://example.test/article/1")

    def test_scanner_detects_required_credential_shapes(self):
        text = "password=hunter2\nAuthorization: Bearer abcdefghijk\ncookie=session-value\n"
        findings = scan_text_for_sensitive_leaks(text)
        terms = {finding.term for finding in findings}
        self.assertTrue({"password", "bearer", "authorization", "cookie"}.intersection(terms))
        self.assertNotIn("hunter2", " ".join(finding.redacted_evidence for finding in findings))

    def test_scanner_detects_temporary_signed_download_parameters(self):
        text = "https://publisher.test/file.pdf?X-Amz-Credential=temporary&X-Amz-Signature=abcdef123456"
        terms = {finding.term for finding in scan_text_for_sensitive_leaks(text)}
        self.assertIn("credential", terms)
        self.assertIn("signature", terms)

    def test_scanner_ignores_boolean_security_policy_statements(self):
        text = "CredentialsStored=false\nTokensExported=false\nCookiesExported=false\n"
        self.assertEqual(scan_text_for_sensitive_leaks(text), [])

    def test_audit_logger_redacts_before_write_and_scan_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            logger = LiteratureAuditLogger(path)
            logger.log(
                "test",
                status="SUCCESS",
                password="never-write-me",
                message="Authorization: Bearer abcdefghijk",
                source_url="https://example.test/path?token=abc",
            )
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("never-write-me", content)
            self.assertNotIn("abcdefghijk", content)
            self.assertNotIn("?token=", content)
            self.assertEqual(scan_files_for_sensitive_leaks([path]), [])


if __name__ == "__main__":
    unittest.main()
