from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hunnu_harness.browser.authorized_file_capture import (
    AcquisitionMethod,
    AuthorizedFileCaptureUnavailable,
    BrowserAuthorizedFileCapture,
)
from hunnu_harness.paths import TEMP_DIR

from literature_test_support import minimal_pdf_bytes


class _Emitter:
    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}

    def on(self, event: str, callback: Any) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        callbacks = self.listeners.get(event, [])
        if callback in callbacks:
            callbacks.remove(callback)

    def emit(self, event: str, value: Any) -> None:
        for callback in tuple(self.listeners.get(event, ())):
            callback(value)


class _FakeContext(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.pages: list[_FakePage] = []


class _FakePage(_Emitter):
    def __init__(self, context: _FakeContext) -> None:
        super().__init__()
        self.context = context
        context.pages.append(self)


class _FakeDownload:
    async def save_as(self, path: str) -> None:
        Path(path).write_bytes(minimal_pdf_bytes())


class _ZeroByteDownload:
    async def save_as(self, path: str) -> None:
        Path(path).write_bytes(b"")


class _FakeResponse:
    def __init__(self, url: str, *, content_type: str = "application/pdf") -> None:
        self.url = url
        self.headers = {
            "content-type": content_type,
            "authorization": "Bearer <REDACTED>",
            "cookie": "<REDACTED>",
        }

    async def body(self) -> bytes:
        return minimal_pdf_bytes()


class BrowserAuthorizedFileCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    async def _capture(
        self,
        action: Any,
        *,
        timeout_ms: int = 100,
        trusted_hosts: tuple[str, ...] = ("academic.oup.com", "yclib.hunnu.edu.cn"),
        response_url_is_expected: Any = None,
    ):
        context = _FakeContext()
        page = _FakePage(context)
        with tempfile.TemporaryDirectory(prefix="v026-capture-", dir=TEMP_DIR) as temporary:
            capture = BrowserAuthorizedFileCapture(Path(temporary))
            result = await capture.capture_pdf(
                page=page,
                official_action=lambda: action(context, page),
                trusted_hosts=trusted_hosts,
                response_url_is_expected=(
                    response_url_is_expected
                    or (lambda value: "/article-pdf/" in value)
                ),
                controlled_filename="PTEST.pdf",
                provenance_host="academic.oup.com",
                source_route="OXFORD_DIRECT",
                timeout_ms=timeout_ms,
                download_priority_grace_ms=5,
            )
            payload = result.path.read_bytes()
            safe = asdict(result)
            safe["path"] = str(safe["path"])
            safe["acquisition_method"] = result.acquisition_method.value
            return result, payload, json.dumps(safe, sort_keys=True)

    async def test_download_event_is_preferred_and_saved_to_staging(self) -> None:
        async def action(context: _FakeContext, page: _FakePage) -> None:
            page.emit("download", _FakeDownload())

        result, payload, _ = await self._capture(action)
        self.assertEqual(result.acquisition_method, AcquisitionMethod.PLAYWRIGHT_DOWNLOAD_EVENT)
        self.assertTrue(result.download_event_emitted)
        self.assertFalse(result.authorized_pdf_response_captured)
        self.assertTrue(payload.startswith(b"%PDF-"))

    async def test_zero_byte_download_event_is_rejected(self) -> None:
        context = _FakeContext()
        page = _FakePage(context)
        with tempfile.TemporaryDirectory(prefix="v026-zero-byte-download-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            capture = BrowserAuthorizedFileCapture(root)

            async def action() -> None:
                page.emit("download", _ZeroByteDownload())

            with self.assertRaisesRegex(
                AuthorizedFileCaptureUnavailable,
                "empty or invalid PDF",
            ):
                await capture.capture_pdf(
                    page=page,
                    official_action=action,
                    trusted_hosts=("academic.oup.com",),
                    response_url_is_expected=lambda value: True,
                    controlled_filename="target.pdf",
                    provenance_host="academic.oup.com",
                    source_route="OXFORD_DIRECT",
                    timeout_ms=100,
                    require_download_event=True,
                )
            self.assertEqual(list(root.iterdir()), [])

    async def test_authorized_pdf_response_is_captured_without_replay(self) -> None:
        async def action(context: _FakeContext, page: _FakePage) -> None:
            context.emit(
                "response",
                _FakeResponse(
                    "https://example.invalid/file.pdf"
                    "?signature=REDACTED_TEST_VALUE"
                ),
            )

        result, payload, serialized = await self._capture(
            action,
            trusted_hosts=("example.invalid",),
            response_url_is_expected=lambda value: value.startswith(
                "https://example.invalid/file.pdf?"
            ),
        )
        self.assertEqual(result.acquisition_method, AcquisitionMethod.AUTHORIZED_PDF_RESPONSE)
        self.assertTrue(result.authorized_pdf_response_captured)
        self.assertTrue(payload.startswith(b"%PDF-"))
        self.assertNotIn("REDACTED_TEST_VALUE", serialized)
        self.assertNotIn("authorization", serialized.casefold())
        self.assertNotIn("cookie", serialized.casefold())

    async def test_unexpected_pdf_on_trusted_host_is_rejected(self) -> None:
        context = _FakeContext()
        page = _FakePage(context)
        with tempfile.TemporaryDirectory(prefix="v026-wrong-pdf-", dir=TEMP_DIR) as temporary:
            capture = BrowserAuthorizedFileCapture(Path(temporary))

            async def action() -> None:
                context.emit(
                    "response",
                    _FakeResponse("https://academic.oup.com/unrelated/article-pdf/wrong.pdf"),
                )

            with self.assertRaises(AuthorizedFileCaptureUnavailable):
                await capture.capture_pdf(
                    page=page,
                    official_action=action,
                    trusted_hosts=("academic.oup.com",),
                    response_url_is_expected=lambda value: "ectj00c1.pdf" in value,
                    controlled_filename="target.pdf",
                    provenance_host="academic.oup.com",
                    source_route="OXFORD_DIRECT",
                    timeout_ms=20,
                )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    async def test_pdf_from_untrusted_host_is_rejected(self) -> None:
        context = _FakeContext()
        page = _FakePage(context)
        with tempfile.TemporaryDirectory(prefix="v026-untrusted-", dir=TEMP_DIR) as temporary:
            capture = BrowserAuthorizedFileCapture(Path(temporary))

            async def action() -> None:
                context.emit(
                    "response",
                    _FakeResponse("https://untrusted.example/ectj/article-pdf/ectj00c1.pdf"),
                )

            with self.assertRaises(AuthorizedFileCaptureUnavailable):
                await capture.capture_pdf(
                    page=page,
                    official_action=action,
                    trusted_hosts=("academic.oup.com",),
                    response_url_is_expected=lambda value: True,
                    controlled_filename="target.pdf",
                    provenance_host="academic.oup.com",
                    source_route="OXFORD_DIRECT",
                    timeout_ms=20,
                )

    async def test_response_before_action_window_is_not_captured(self) -> None:
        context = _FakeContext()
        page = _FakePage(context)
        context.emit(
            "response",
            _FakeResponse("https://academic.oup.com/ectj/article-pdf/21/1/C1/ectj00c1.pdf"),
        )
        with tempfile.TemporaryDirectory(prefix="v026-outside-window-", dir=TEMP_DIR) as temporary:
            capture = BrowserAuthorizedFileCapture(Path(temporary))

            async def no_action() -> None:
                return None

            with self.assertRaises(AuthorizedFileCaptureUnavailable):
                await capture.capture_pdf(
                    page=page,
                    official_action=no_action,
                    trusted_hosts=("academic.oup.com",),
                    response_url_is_expected=lambda value: True,
                    controlled_filename="target.pdf",
                    provenance_host="academic.oup.com",
                    source_route="OXFORD_DIRECT",
                    timeout_ms=20,
                )


if __name__ == "__main__":
    unittest.main()
