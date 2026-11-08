"""The official PDF action can render a PDF instead of emitting a download.

These are same-action browser responses, never a second request or click.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hunnu_harness.browser.commands import BrowserTarget, DownloadCaptureSpec, DownloadCommand, DownloadFailure
from hunnu_harness.browser.local_executor import LocalPlaywrightExecutor
from hunnu_harness.paths import TEMP_DIR

from literature_test_support import minimal_pdf_bytes


PII = "S0165410109000469"
ARTICLE = f"https://www.sciencedirect.com/science/article/pii/{PII}"
PDF_URL = ARTICLE + "/pdfft"


class _Emitter:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        callbacks = self.listeners.get(event, [])
        if callback in callbacks:
            callbacks.remove(callback)

    def emit(self, event, value):
        for callback in tuple(self.listeners.get(event, ())):
            callback(value)


class _Session(_Emitter):
    async def send(self, method, params):
        if method == "Browser.setDownloadBehavior":
            self.download_dir = params.get("downloadPath")


class _Response:
    def __init__(self, url=PDF_URL, *, body=None, content_type="application/pdf"):
        self.url = url
        self.headers = {"content-type": content_type}
        self.payload = minimal_pdf_bytes() if body is None else body
        self.body_reads = 0

    async def body(self):
        self.body_reads += 1
        return self.payload


class _Download:
    def __init__(self, url=PDF_URL):
        self.url = url

    async def save_as(self, path):
        Path(path).write_bytes(minimal_pdf_bytes())


class _Page(_Emitter):
    def __init__(self, context, action):
        super().__init__()
        self.context = context
        self.url = ARTICLE
        self.action = action
        self.clicks = 0

    def locator(self, _selector):
        return self

    @property
    def first(self):
        return self

    async def click(self, **_kwargs):
        self.clicks += 1
        await self.action(self)


def _command():
    return DownloadCommand(
        target=BrowserTarget(css=f'a[href="/science/article/pii/{PII}/pdfft"]'),
        suggested_filename="target.pdf",
        timeout_ms=40,
        capture=DownloadCaptureSpec(
            trusted_hosts=("www.sciencedirect.com", "pdf.sciencedirectassets.com"),
            provenance_host="www.sciencedirect.com",
            source_route="SCIENCEDIRECT_OFFICIAL_PDF",
            expected_url=PDF_URL,
            download_identity_required=True,
        ),
    )


class ScienceDirectResponseCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def _run_action(
        self, action, *, attached=True, expect_failure=False,
        expected_payload=None, expected_invalid_payload=None, expected_retained_payload=None,
    ):
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sd-response-", dir=TEMP_DIR) as temp:
            context = _Emitter()
            context.pages = []
            page = _Page(context, action)
            context.pages.append(page)
            session = _Session()
            page.session = session
            backend = SimpleNamespace(
                page=page, context=context, attached=attached,
                downloads_dir=Path(temp), _cdp=session,
            )
            executor = LocalPlaywrightExecutor(backend)
            with (
                patch("hunnu_harness.browser.browser_directed_download.DEFAULT_WILL_BEGIN_TIMEOUT_SECONDS", .04),
                patch("hunnu_harness.browser.browser_directed_download.DEFAULT_COMPLETION_TIMEOUT_SECONDS", .04),
            ):
                if expect_failure:
                    with self.assertRaises(DownloadFailure):
                        await executor.execute(_command())
                    files = [path for path in Path(temp).rglob("*") if path.is_file()]
                    if expected_retained_payload is not None:
                        self.assertEqual(len(files), 1)
                        self.assertEqual(files[0].read_bytes(), expected_retained_payload)
                    elif expected_invalid_payload is None:
                        self.assertEqual(files, [])
                    else:
                        self.assertEqual(len(files), 1)
                        self.assertEqual(files[0].read_bytes(), expected_invalid_payload)
                        self.assertEqual(files[0].suffix, ".bin")
                    result = None
                else:
                    result = await executor.execute(_command())
                    self.assertTrue(result.local_path.read_bytes().startswith(b"%PDF-"))
                    if expected_payload is not None:
                        self.assertEqual(result.local_path.read_bytes(), expected_payload)
            self.assertEqual(page.clicks, 1)
            self.assertTrue(all(not callbacks for callbacks in context.listeners.values()))
            self.assertTrue(all(not callbacks for callbacks in session.listeners.values()))
            return result, executor.last_post_click_warning

    async def test_attached_pdf_response_without_a_download_event_is_captured_from_the_one_click(self):
        async def action(page):
            page.context.emit("response", _Response())
        result, _ = await self._run_action(action)
        self.assertEqual(result.metadata["AcquisitionMethod"], "AUTHORIZED_PDF_RESPONSE")
        self.assertFalse(result.metadata["DownloadEventEmitted"])

    async def test_launched_browser_uses_the_same_authorized_response_path(self):
        async def action(page):
            page.context.emit("response", _Response())
        result, _ = await self._run_action(action, attached=False)
        self.assertEqual(result.metadata["AcquisitionMethod"], "AUTHORIZED_PDF_RESPONSE")

    async def test_other_pii_response_is_not_claimed_on_the_trusted_host(self):
        async def action(page):
            page.context.emit("response", _Response(PDF_URL.replace(PII, "S9999999999999999")))
        await self._run_action(action, expect_failure=True)

    async def test_other_paper_download_in_the_same_context_is_not_claimed(self):
        async def action(page):
            page.emit("download", _Download(PDF_URL.replace(PII, "S9999999999999999")))
        await self._run_action(action, attached=False, expect_failure=True)

    async def test_untrusted_pdf_response_is_not_claimed(self):
        async def action(page):
            page.context.emit("response", _Response(f"https://evil.example/pii/{PII}/pdfft"))
        await self._run_action(action, expect_failure=True)

    async def test_matching_cdp_download_is_preferred_to_its_pdf_response(self):
        async def action(page):
            name = PII + ".pdf"
            page.context.emit("response", _Response())
            Path(page.session.download_dir, name).write_bytes(minimal_pdf_bytes())
            page.session.emit("Browser.downloadWillBegin", {
                "guid": "target", "url": PDF_URL, "suggestedFilename": name,
            })
            page.session.emit("Browser.downloadProgress", {"guid": "target", "state": "completed"})
        result, _ = await self._run_action(action)
        self.assertEqual(result.metadata["AcquisitionMethod"], "BROWSER_DIRECTED_DOWNLOAD")

    async def test_canceled_cdp_download_cannot_be_overridden_by_a_pdf_response(self):
        async def action(page):
            page.context.emit("response", _Response())
            await asyncio.sleep(.001)
            page.session.emit("Browser.downloadWillBegin", {
                "guid": "target", "url": PDF_URL, "suggestedFilename": PII + ".pdf",
            })
            page.session.emit("Browser.downloadProgress", {"guid": "target", "state": "canceled"})
        await self._run_action(action, expect_failure=True, expected_retained_payload=minimal_pdf_bytes())

    async def test_cdp_suggested_name_cannot_collide_with_the_response_capture_copy(self):
        original = minimal_pdf_bytes() + b"\n% original browser bytes"

        async def action(page):
            # This exact name used to be preselected for response.write_bytes
            # in Chrome's own root staging directory, overwriting this file.
            name = "target_response.pdf"
            Path(page.session.download_dir, name).write_bytes(original)
            page.session.emit("Browser.downloadWillBegin", {
                "guid": "target", "url": PDF_URL, "suggestedFilename": name,
            })
            page.session.emit("Browser.downloadProgress", {"guid": "target", "state": "completed"})
            page.context.emit("response", _Response(body=minimal_pdf_bytes() + b"\n% response copy"))

        result, _ = await self._run_action(action, expected_payload=original)
        self.assertEqual(result.metadata["AcquisitionMethod"], "BROWSER_DIRECTED_DOWNLOAD")

    async def test_pdf_mime_html_or_empty_body_is_preserved_and_never_ingested(self):
        for attached in (True, False):
            for payload in (b"<html><title>Client Challenge</title></html>", b""):
                with self.subTest(attached=attached, payload=payload):
                    async def action(page):
                        page.context.emit("response", _Response(body=payload))
                    await self._run_action(
                        action, attached=attached, expect_failure=True,
                        expected_invalid_payload=payload,
                    )

    async def test_cdp_cancellation_still_wins_when_an_invalid_pdf_mime_body_was_received(self):
        payload = b"<html>received diagnostic bytes</html>"

        async def action(page):
            page.context.emit("response", _Response(body=payload))
            await asyncio.sleep(.001)
            page.session.emit("Browser.downloadWillBegin", {
                "guid": "target", "url": PDF_URL, "suggestedFilename": PII + ".pdf",
            })
            page.session.emit("Browser.downloadProgress", {"guid": "target", "state": "canceled"})

        await self._run_action(
            action, expect_failure=True, expected_invalid_payload=payload,
        )

    def test_identity_requirement_without_an_expected_url_is_refused(self):
        with self.assertRaisesRegex(ValueError, "stable paper identifier"):
            DownloadCaptureSpec(
                trusted_hosts=("www.sciencedirect.com",),
                provenance_host="www.sciencedirect.com", source_route="test",
                download_identity_required=True,
            )

    def test_identity_requirement_cannot_degrade_to_any_pdf_on_the_host(self):
        with self.assertRaisesRegex(ValueError, "stable paper identifier"):
            DownloadCaptureSpec(
                trusted_hosts=("www.sciencedirect.com",),
                provenance_host="www.sciencedirect.com", source_route="test",
                expected_url="https://www.sciencedirect.com/any.pdf",
                download_identity_required=True,
            )

    async def test_html_response_and_no_file_fail_without_retrying_the_action(self):
        async def action(page):
            page.context.emit("response", _Response(body=b"<html>gate</html>", content_type="text/html"))
        await self._run_action(action, expect_failure=True)

    async def test_response_then_click_timeout_preserves_the_file_without_another_click(self):
        async def action(page):
            page.context.emit("response", _Response())
            raise TimeoutError("click did not settle")
        result, warning = await self._run_action(action)
        self.assertEqual(result.metadata["AcquisitionMethod"], "AUTHORIZED_PDF_RESPONSE")
        self.assertIn("POST_CLICK_TIMEOUT", warning)

    async def test_launched_response_then_click_timeout_does_not_retry_the_action(self):
        async def action(page):
            page.context.emit("response", _Response())
            raise TimeoutError("click did not settle")
        result, warning = await self._run_action(action, attached=False)
        self.assertEqual(result.metadata["AcquisitionMethod"], "AUTHORIZED_PDF_RESPONSE")
        self.assertIn("POST_CLICK_TIMEOUT", warning)

    async def test_post_click_typeerror_never_triggers_a_second_action(self):
        async def action(page):
            page.context.emit("response", _Response())
            raise TypeError("action completed but page callback failed")
        result, _ = await self._run_action(action)
        self.assertEqual(result.metadata["AcquisitionMethod"], "AUTHORIZED_PDF_RESPONSE")

