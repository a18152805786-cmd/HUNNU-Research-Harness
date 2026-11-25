"""An authorized download whose browser-suggested name has no extension.

Found live on 2026-11-25: CNKI served a valid 19-page PDF (赵璨等 2020, 中国工业经济)
under the suggested name ``pdf``.  It landed as ``staging/pdf``; the validator
requires ``.pdf`` for a declared PDF, so the run reported DOWNLOAD_FAILED with
reason ``unknown`` although the file was the right paper.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hunnu_harness.browser.local_executor import LocalPlaywrightExecutor
from hunnu_harness.literature.fulltext import AuthorizedFullTextValidator
from hunnu_harness.literature.models import FullTextFormat
from hunnu_harness.paths import TEMP_DIR

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 10 10]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n159\n%%EOF\n"
)


class ExtensionlessDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="extless-", dir=TEMP_DIR)
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_pdf_named_without_extension_gets_the_requested_suffix(self) -> None:
        # If this fails, a valid PDF saved under a bare browser name is again
        # rejected as DOWNLOAD_FAILED.
        path = self.dir / "pdf"
        path.write_bytes(MINIMAL_PDF)
        restored = LocalPlaywrightExecutor._restore_requested_suffix(path, "P5FF75069F44D.pdf")
        self.assertEqual(restored.suffix, ".pdf")
        self.assertTrue(restored.is_file())
        self.assertFalse(path.exists())

    def test_bytes_that_do_not_prove_the_format_keep_their_name(self) -> None:
        # If this fails, an HTML error page (or anything else) could be renamed
        # to .pdf and slip past the extension check.
        path = self.dir / "pdf"
        path.write_bytes(b"<html><body>error</body></html>")
        restored = LocalPlaywrightExecutor._restore_requested_suffix(path, "P5FF75069F44D.pdf")
        self.assertEqual(restored, path)
        self.assertTrue(path.exists())

    def test_a_file_that_already_has_an_extension_is_left_alone(self) -> None:
        path = self.dir / "paper.caj"
        path.write_bytes(MINIMAL_PDF)
        self.assertEqual(LocalPlaywrightExecutor._restore_requested_suffix(path, "x.pdf"), path)

    def test_a_caj_named_without_extension_gets_caj(self) -> None:
        path = self.dir / "caj"
        path.write_bytes(b"HNLC" + b"\x00" * 64)
        restored = LocalPlaywrightExecutor._restore_requested_suffix(path, "P1.caj")
        self.assertEqual(restored.suffix, ".caj")

    def test_the_validator_names_the_extension_problem_instead_of_unknown(self) -> None:
        # If this fails, an extension mismatch is reported as reason "unknown" again.
        path = self.dir / "pdf"
        path.write_bytes(MINIMAL_PDF)
        result = AuthorizedFullTextValidator.validate(path, FullTextFormat.PDF)
        self.assertFalse(result.passed)
        self.assertIn("extension", result.error)


if __name__ == "__main__":
    unittest.main()
