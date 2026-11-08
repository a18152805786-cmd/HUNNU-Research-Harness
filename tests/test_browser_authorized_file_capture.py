from __future__ import annotations

import asyncio
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

    async def test_an_explicit_dot_suffix_accepts_only_its_real_subdomains(self) -> None:
        async def action(context, page):
            context.emit("response", _FakeResponse(
                "https://cdn.silverchair.com/ectj/article-pdf/21/1/C1/ectj00c1.pdf"
            ))
        result, _, _ = await self._capture(
            action, trusted_hosts=(".silverchair.com",),
            response_url_is_expected=lambda value: "ectj00c1.pdf" in value,
        )
        self.assertEqual(result.source_host, "cdn.silverchair.com")

    async def test_suffix_declarations_do_not_authorize_apex_or_lookalike_hosts(self) -> None:
        for declared, host in (
            ((".silverchair.com",), "silverchair.com"),
            ((".silverchair.com",), "evil-silverchair.com"),
            ((".silverchair.com",), "cdn.silverchair.com.evil.example"),
            (("silverchair.com",), "cdn.silverchair.com"),
            (("academic.oup.com",), "cdn.silverchair.com"),
        ):
            with self.subTest(declared=declared, host=host):
                async def action(context, page):
                    context.emit("response", _FakeResponse(f"https://{host}/article-pdf/ectj00c1.pdf"))
                with self.assertRaises(AuthorizedFileCaptureUnavailable):
                    await self._capture(
                        action, trusted_hosts=declared, timeout_ms=10,
                        response_url_is_expected=lambda _value: True,
                    )

    async def test_identity_locked_download_event_respects_the_declared_suffix_boundary(self) -> None:
        context = _FakeContext()
        page = _FakePage(context)
        download = _FakeDownload()
        download.url = "https://cdn.silverchair.com/article-pdf/ectj00c1.pdf"
        with tempfile.TemporaryDirectory(prefix="capture-declared-suffix-", dir=TEMP_DIR) as temporary:
            async def action():
                page.emit("download", download)
            result = await BrowserAuthorizedFileCapture(Path(temporary)).capture_pdf(
                page=page, official_action=action,
                trusted_hosts=(".silverchair.com",),
                response_url_is_expected=lambda value: "ectj00c1.pdf" in value,
                download_url_is_expected=lambda value: "ectj00c1.pdf" in value,
                controlled_filename="target.pdf", provenance_host="academic.oup.com",
                source_route="test", timeout_ms=50,
            )
            self.assertEqual(result.acquisition_method, AcquisitionMethod.PLAYWRIGHT_DOWNLOAD_EVENT)

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
            files = [path for path in root.rglob("*") if path.is_file()]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b"")

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
            self.assertEqual([path for path in Path(temporary).rglob("*") if path.is_file()], [])

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

    async def test_claimed_slow_download_finishes_before_the_response_can_be_selected(self) -> None:
        context = _FakeContext()
        page = _FakePage(context)
        identity = "S0165410109000469"
        url = f"https://www.sciencedirect.com/science/article/pii/{identity}/pdfft"

        class SlowDownload:
            def __init__(self):
                self.url = url
                self.save_calls = 0
                self.finished = False
                self.path = None

            async def save_as(self, path):
                self.save_calls += 1
                self.path = Path(path)
                self.path.write_bytes(b"first partial bytes")
                try:
                    await asyncio.sleep(.02)
                except asyncio.CancelledError:
                    # A native save already under way may finish while its
                    # caller is being canceled.  Cleanup must await it, and
                    # it must never be writing to the winning response path.
                    await asyncio.sleep(.01)
                self.path.write_bytes(minimal_pdf_bytes() + b"\n% download evidence")
                self.finished = True

        first, duplicate = SlowDownload(), SlowDownload()
        with tempfile.TemporaryDirectory(prefix="capture-race-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)
            capture = BrowserAuthorizedFileCapture(root)

            async def action():
                page.emit("download", first)
                page.emit("download", duplicate)
                context.emit("response", _FakeResponse(url))

            result = await capture.capture_pdf(
                page=page, official_action=action,
                trusted_hosts=("www.sciencedirect.com",),
                response_url_is_expected=lambda value: identity in value,
                download_url_is_expected=lambda value: identity in value,
                controlled_filename="target.pdf",
                provenance_host="www.sciencedirect.com", source_route="test",
                timeout_ms=100, download_priority_grace_ms=5,
            )
            self.assertEqual(result.acquisition_method, AcquisitionMethod.PLAYWRIGHT_DOWNLOAD_EVENT)
            self.assertTrue(first.finished, "the claimed event must complete before success")
            self.assertEqual(first.save_calls, 1)
            self.assertEqual(duplicate.save_calls, 0)
            self.assertEqual(result.path, first.path)
            self.assertFalse(result.authorized_pdf_response_captured)
            self.assertTrue(result.download_event_emitted)
            self.assertEqual(first.path.read_bytes(), minimal_pdf_bytes() + b"\n% download evidence")
            response_copies = [p for p in root.rglob("*") if p.is_file() and p != first.path]
            self.assertEqual(len(response_copies), 1)
            self.assertNotEqual(response_copies[0], first.path)
            self.assertEqual(response_copies[0].read_bytes(), minimal_pdf_bytes())
            snapshot = result.path.read_bytes()
            await asyncio.sleep(.02)
            self.assertEqual(result.path.read_bytes(), snapshot)
            self.assertEqual(len([path for path in root.rglob("*") if path.is_file()]), 2)

    async def test_claimed_download_save_failure_is_not_washed_away_by_a_valid_response(self) -> None:
        context = _FakeContext()
        page = _FakePage(context)

        class FailedDownload:
            async def save_as(self, path):
                Path(path).write_bytes(b"partial first bytes")
                await asyncio.sleep(.01)
                raise RuntimeError("browser canceled the claimed download")

        with tempfile.TemporaryDirectory(prefix="capture-claimed-failure-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)

            async def action():
                page.emit("download", FailedDownload())
                context.emit("response", _FakeResponse("https://academic.oup.com/article-pdf/ectj00c1.pdf"))

            with self.assertRaisesRegex(AuthorizedFileCaptureUnavailable, "could not be saved"):
                await BrowserAuthorizedFileCapture(root).capture_pdf(
                    page=page, official_action=action,
                    trusted_hosts=("academic.oup.com",), response_url_is_expected=lambda _value: True,
                    controlled_filename="target.pdf", provenance_host="academic.oup.com",
                    source_route="test", timeout_ms=100, download_priority_grace_ms=5,
                )
            payloads = [p.read_bytes() for p in root.rglob("*") if p.is_file()]
            self.assertEqual(len(payloads), 2)
            self.assertIn(b"partial first bytes", payloads)
            self.assertIn(minimal_pdf_bytes(), payloads)

    async def test_claimed_download_wait_is_bounded_and_cleanup_waits_for_late_save(self) -> None:
        context = _FakeContext()
        page = _FakePage(context)
        finished = False

        class SlowDownload:
            async def save_as(self, path):
                nonlocal finished
                Path(path).write_bytes(b"partial first bytes")
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    await asyncio.sleep(.01)
                Path(path).write_bytes(b"retained late first download")
                finished = True

        with tempfile.TemporaryDirectory(prefix="capture-claimed-timeout-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)

            async def action():
                page.emit("download", SlowDownload())
                context.emit("response", _FakeResponse("https://academic.oup.com/article-pdf/ectj00c1.pdf"))

            with self.assertRaisesRegex(AuthorizedFileCaptureUnavailable, "Claimed Playwright download"):
                await BrowserAuthorizedFileCapture(root).capture_pdf(
                    page=page, official_action=action,
                    trusted_hosts=("academic.oup.com",), response_url_is_expected=lambda _value: True,
                    controlled_filename="target.pdf", provenance_host="academic.oup.com",
                    source_route="test", timeout_ms=20, download_priority_grace_ms=5,
                )
            self.assertTrue(finished)
            snapshot = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(len(snapshot), 2)
            self.assertIn(b"retained late first download", snapshot.values())
            self.assertIn(minimal_pdf_bytes(), snapshot.values())
            await asyncio.sleep(.02)
            self.assertEqual({p: p.read_bytes() for p in snapshot}, snapshot)

    async def test_invalid_download_bytes_are_retained_without_becoming_an_artifact(self) -> None:
        context = _FakeContext()
        page = _FakePage(context)

        class HtmlDownload:
            async def save_as(self, path):
                Path(path).write_bytes(b"<html><title>Client Challenge</title></html>")

        with tempfile.TemporaryDirectory(prefix="capture-invalid-evidence-", dir=TEMP_DIR) as temporary:
            root = Path(temporary)

            async def action():
                page.emit("download", HtmlDownload())

            with self.assertRaises(AuthorizedFileCaptureUnavailable):
                await BrowserAuthorizedFileCapture(root).capture_pdf(
                    page=page, official_action=action,
                    trusted_hosts=("academic.oup.com",),
                    response_url_is_expected=lambda _value: True,
                    controlled_filename="target.pdf",
                    provenance_host="academic.oup.com", source_route="test",
                    timeout_ms=100,
                )
            files = [path for path in root.rglob("*") if path.is_file()]
            self.assertEqual(len(files), 1)
            self.assertIn(b"Client Challenge", files[0].read_bytes())


if __name__ == "__main__":
    unittest.main()
