"""When a click fails but the browser says the file arrived anyway.

The click's only job is to operate the authorized control.  Whether a download
happened is answered by the browser: a matching ``downloadWillBegin``, a
matching GUID reaching ``completed``, and a readable PDF on disk.  Those three
together are stronger evidence than the click's own idea of whether it settled.

So a click that does not settle is recorded and judged afterwards rather than
ending the attempt.  The judgement is deliberately narrow -- without matching
download evidence the failure stands, and no exception is ever swallowed for its
own sake.  Every case below distinguishes "the file demonstrably arrived" from
"the click failed and nothing arrived", because collapsing those two is how a
real download gets thrown away or an imaginary one gets archived.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from hunnu_harness.browser.authorized_file_capture import AcquisitionMethod
from hunnu_harness.browser.commands import (
    BrowserTarget,
    DownloadCaptureSpec,
    DownloadCommand,
    DownloadFailure,
)
from hunnu_harness.browser.local_executor import LocalPlaywrightExecutor, _redacted_reason
from hunnu_harness.paths import TEMP_DIR

PII = "SLOCALPII123"
FILENAME = f"1-s2.0-{PII}-main.pdf"
EXPECTED_URL = f"https://www.sciencedirect.com/science/article/pii/{PII}/pdfft?md5=abc"
SOURCE_URL = f"https://pdf.sciencedirectassets.com/1/main.pdf?pii={PII}&X-Amz-Signature=s"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class _Session:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.sent: list = []

    def on(self, event, handler):  # noqa: ANN001
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):  # noqa: ANN001
        if handler in self.handlers.get(event, []):
            self.handlers[event].remove(handler)

    async def send(self, method, params=None):  # noqa: ANN001
        self.sent.append((method, params or {}))
        return {}

    def emit(self, event, payload):  # noqa: ANN001
        for handler in list(self.handlers.get(event, [])):
            handler(payload)


class _Backend:
    """An attached persistent browser, as the executor sees one."""

    attached = True
    page = object()
    context = object()

    def __init__(self, downloads_dir: Path, session: _Session) -> None:
        self.downloads_dir = downloads_dir
        self._cdp = session


class _Locator:
    """A control whose click either settles, or does not."""

    def __init__(self, session: _Session, directory: Path, *, behaviour: str) -> None:
        self.session = session
        self.directory = directory
        self.behaviour = behaviour
        self.clicked = 0

    async def click(self, **kwargs):  # noqa: ANN003
        self.clicked += 1
        if self.behaviour == "nothing":
            raise TimeoutError("Timeout 30000ms exceeded waiting for element")
        # The browser starts, and finishes, the download regardless of whether
        # the click itself ever settles.
        self.session.emit(
            "Browser.downloadWillBegin",
            {"guid": "g1", "url": SOURCE_URL, "suggestedFilename": FILENAME},
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.behaviour != "incomplete":
            (self.directory / FILENAME).write_bytes(
                PDF_BYTES if self.behaviour != "invalid-pdf" else b"<html>nope</html>"
            )
            self.session.emit(
                "Browser.downloadProgress", {"guid": "g1", "state": "completed"}
            )
        if self.behaviour in {"timeout-after-download", "incomplete", "invalid-pdf"}:
            raise TimeoutError(
                f"Timeout 30000ms exceeded waiting for {SOURCE_URL}"
            )


def command() -> DownloadCommand:
    return DownloadCommand(
        target=BrowserTarget(css="a#pdf"),
        suggested_filename="TARGET.pdf",
        capture=DownloadCaptureSpec(
            trusted_hosts=("pdf.sciencedirectassets.com",),
            expected_url=EXPECTED_URL,
            provenance_host="www.sciencedirect.com",
            source_route="offline-fixture",
        ),
    )


def attempt(behaviour: str, tmp: Path):
    session = _Session()
    directory = tmp / "staging"
    executor = LocalPlaywrightExecutor(_Backend(directory, session))
    locator = _Locator(session, directory, behaviour=behaviour)
    from hunnu_harness.browser import browser_directed_download as module

    original = module.DEFAULT_WILL_BEGIN_TIMEOUT_SECONDS, module.DEFAULT_COMPLETION_TIMEOUT_SECONDS
    module.DEFAULT_WILL_BEGIN_TIMEOUT_SECONDS = 0.5
    module.DEFAULT_COMPLETION_TIMEOUT_SECONDS = 0.8
    try:
        result = asyncio.run(executor._directed_capture(locator, command()))
    finally:
        module.DEFAULT_WILL_BEGIN_TIMEOUT_SECONDS, module.DEFAULT_COMPLETION_TIMEOUT_SECONDS = original
    return result, executor, locator


def temp_root(prefix: str):
    import tempfile

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=TEMP_DIR)


class AdjudicationTests(unittest.TestCase):
    def test_a_settled_click_with_a_completed_download_succeeds_quietly(self) -> None:
        with temp_root("adj-ok-") as tmp:
            result, executor, locator = attempt("settles", Path(tmp))
            self.assertEqual(
                result.acquisition_method, AcquisitionMethod.BROWSER_DIRECTED_DOWNLOAD
            )
            self.assertEqual(result.path.name, FILENAME)
            self.assertEqual(locator.clicked, 1)
            self.assertIsNone(executor.last_post_click_warning)

    def test_a_download_the_browser_proved_survives_a_click_that_did_not_settle(self) -> None:
        """The whole point: the PDF is on disk and the click says otherwise."""

        with temp_root("adj-post-") as tmp:
            result, executor, _ = attempt("timeout-after-download", Path(tmp))
            self.assertEqual(result.path.name, FILENAME)
            self.assertEqual(result.source_host, "pdf.sciencedirectassets.com")
            self.assertIsNotNone(executor.last_post_click_warning)
            self.assertIn(
                "TRIGGER_COMPLETED_WITH_POST_CLICK_TIMEOUT",
                executor.last_post_click_warning,
            )

    def test_a_click_that_starts_nothing_still_fails(self) -> None:
        """No download evidence means the click failure stands."""

        with temp_root("adj-none-") as tmp:
            with self.assertRaises(DownloadFailure) as caught:
                attempt("nothing", Path(tmp))
            self.assertIn("could not be operated", str(caught.exception))
            self.assertIn("Timeout", str(caught.exception))

    def test_a_download_that_never_completes_still_fails(self) -> None:
        with temp_root("adj-incomplete-") as tmp:
            with self.assertRaises(DownloadFailure):
                attempt("incomplete", Path(tmp))

    def test_a_completed_download_that_is_not_a_pdf_still_fails(self) -> None:
        """Browser-level completion is necessary, never sufficient."""

        with temp_root("adj-notpdf-") as tmp:
            with self.assertRaises(DownloadFailure) as caught:
                attempt("invalid-pdf", Path(tmp))
            self.assertIn("not a readable PDF", str(caught.exception))

    def test_nothing_is_swallowed_blindly(self) -> None:
        """Three failure shapes, three failures; only proven arrival passes."""

        outcomes = {}
        for behaviour in ("nothing", "incomplete", "invalid-pdf", "timeout-after-download"):
            with temp_root(f"adj-matrix-{behaviour}-") as tmp:
                try:
                    attempt(behaviour, Path(tmp))
                    outcomes[behaviour] = "accepted"
                except DownloadFailure:
                    outcomes[behaviour] = "refused"
        self.assertEqual(
            outcomes,
            {
                "nothing": "refused",
                "incomplete": "refused",
                "invalid-pdf": "refused",
                "timeout-after-download": "accepted",
            },
        )


class RedactionTests(unittest.TestCase):
    """Diagnosis is kept; the publisher's signed credentials are not."""

    def test_a_signed_url_loses_its_query_but_keeps_its_shape(self) -> None:
        message = _redacted_reason(
            TimeoutError(
                "Timeout waiting for https://pdf.sciencedirectassets.com/1/main.pdf"
                "?X-Amz-Security-Token=SECRET&X-Amz-Signature=SIG&pii=S1"
            )
        )
        self.assertNotIn("SECRET", message)
        self.assertNotIn("X-Amz-Signature", message)
        self.assertIn("pdf.sciencedirectassets.com", message)
        self.assertIn("TimeoutError", message)

    def test_an_ordinary_message_survives_intact(self) -> None:
        message = _redacted_reason(TimeoutError("element is not visible"))
        self.assertIn("element is not visible", message)

    def test_an_empty_message_falls_back_to_the_class(self) -> None:
        self.assertEqual(_redacted_reason(TimeoutError()), "TimeoutError")

    def test_a_very_long_message_is_bounded(self) -> None:
        message = _redacted_reason(TimeoutError("x" * 5000))
        self.assertLess(len(message), 700)


if __name__ == "__main__":
    unittest.main()
