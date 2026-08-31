"""Task 3.5: the audit logger redacts what actually leaks, not just known keys.

The old redaction matched eight exact key names.  ``Set-Cookie`` is not
``cookie``, and a signed URL under a ``detail`` key is not a key at all --
both went to disk verbatim.  The logger now shares the literature pipeline's
sanitizer; these tests pin the two named holes and the benign cases.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.audit.logger import AuditLogger
from hunnu_harness.paths import TEMP_DIR


class AuditLoggerRedactionTests(unittest.TestCase):
    def _log_one(self, **details) -> dict:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="audit-log-", dir=TEMP_DIR) as tmp:
            path = Path(tmp) / "audit.jsonl"
            AuditLogger(path).log("download", status="ok", **details)
            return json.loads(path.read_text(encoding="utf-8").splitlines()[0])

    def test_set_cookie_key_is_redacted_despite_not_matching_exactly(self) -> None:
        event = self._log_one(headers={"Set-Cookie": "JSESSIONID=deadbeef; Path=/"})
        self.assertEqual(event["headers"]["Set-Cookie"], "<REDACTED>")

    def test_a_signed_url_inside_an_ordinary_value_loses_its_query(self) -> None:
        event = self._log_one(
            detail=(
                "fetched https://cdn.example.org/paper.pdf"
                "?Expires=1700000000&Signature=AbCdEf123&Key-Pair-Id=K2JCJM"
            )
        )
        self.assertIn("https://cdn.example.org/paper.pdf", event["detail"])
        self.assertNotIn("Signature", event["detail"])
        self.assertNotIn("AbCdEf123", event["detail"])

    def test_a_bearer_credential_inside_text_is_scrubbed(self) -> None:
        event = self._log_one(note="sent Authorization: Bearer sk-live-123456 upstream")
        self.assertNotIn("sk-live-123456", event["note"])
        self.assertIn("<REDACTED>", event["note"])

    def test_benign_details_survive_untouched(self) -> None:
        event = self._log_one(
            database="CNRDS",
            table="cash_flow",
            size_bytes=1024,
            read_only=True,
            sha256="a" * 64,
        )
        self.assertEqual(event["database"], "CNRDS")
        self.assertEqual(event["size_bytes"], 1024)
        self.assertTrue(event["read_only"])
        self.assertEqual(event["sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
