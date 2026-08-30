"""Accepting a download the attached browser performed, and only that one.

The bytes were never the problem: a complete, valid 3.7 MB PDF arrived and was
staged while the run reported that no download had happened, because the
``download`` event never reached the page being awaited.  Waiting on the
browser's own account of the download removes that dependency.

What these pin is that removing it costs nothing in rigour.  The paper's
identifier has to appear in the URL the bytes actually came from; the delivery
host has to be one this Harness recognises; only the GUID of *this* download
counts, and only after the browser says it completed.  A file that merely turns
up in the directory proves nothing and is never enough.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from hunnu_harness.browser.authorized_file_capture import AcquisitionMethod
from hunnu_harness.browser.browser_directed_download import (
    ELSEVIER_PDF_HOSTS,
    BrowserDirectedDownload,
    DirectedDownloadFailure,
    host_of,
    lease_for,
    pii_from_url,
)
from hunnu_harness.paths import TEMP_DIR

PII = "S1059056026008695"
OTHER_PII = "S1544612326002151"
FILENAME = f"1-s2.0-{PII}-main.pdf"
REAL_URL = (
    "https://pdf.sciencedirectassets.com/272089/1-s2.0-S1059056026X20056/"
    f"1-s2.0-{PII}/main.pdf?X-Amz-Date=20260830T121037Z&pii={PII}&tid=spdf-7373"
)
PDF_BYTES = b"%PDF-1.7\n" + b"x" * 4096 + b"\n%%EOF\n"


class _Session:
    """Stands in for a browser-level CDP session."""

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.sent: list[tuple[str, dict]] = []

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

    @property
    def download_path(self) -> str:
        for method, params in self.sent:
            if method == "Browser.setDownloadBehavior" and "downloadPath" in params:
                return params["downloadPath"]
        return ""

    @property
    def behaviours(self) -> list[str]:
        return [
            params.get("behavior", "")
            for method, params in self.sent
            if method == "Browser.setDownloadBehavior"
        ]


def begin(guid: str = "g1", url: str = REAL_URL, filename: str = FILENAME) -> dict:
    return {"guid": guid, "url": url, "suggestedFilename": filename}


def progress(guid: str = "g1", state: str = "completed") -> dict:
    return {"guid": guid, "state": state}


class _Fixture:
    def __init__(self, root: Path, *, locked: str = PII) -> None:
        self.session = _Session()
        self.dir = root / "staging"
        self.directed = BrowserDirectedDownload(
            session=self.session,
            download_dir=self.dir,
            locked_pii=locked,
            will_begin_timeout=0.6,
            completion_timeout=1.2,
            settle_seconds=0.0,
        )

    def land(self, name: str = FILENAME, payload: bytes = PDF_BYTES) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / name
        path.write_bytes(payload)
        return path


def run(fixture: _Fixture, script) -> object:
    async def main():
        await fixture.directed.arm()
        try:
            await script(fixture)
            return await fixture.directed.await_download()
        finally:
            await fixture.directed.release()

    return asyncio.run(main())


def temp_root(prefix: str):
    import tempfile

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=TEMP_DIR)


class IdentityTests(unittest.TestCase):
    def test_the_real_delivery_url_yields_the_paper_identifier(self) -> None:
        self.assertEqual(pii_from_url(REAL_URL), PII)

    def test_the_article_path_form_also_yields_it(self) -> None:
        self.assertEqual(
            pii_from_url(f"/science/article/pii/{OTHER_PII}/pdfft?md5=a"), OTHER_PII
        )

    def test_a_url_naming_no_paper_yields_nothing(self) -> None:
        for url in ("https://example.invalid/a.pdf", "", "not a url"):
            self.assertEqual(pii_from_url(url), "")

    def test_the_publisher_delivery_host_is_recognised(self) -> None:
        """The bytes do not come from the article host, and that is normal."""

        self.assertIn("pdf.sciencedirectassets.com", ELSEVIER_PDF_HOSTS)
        self.assertIn("www.sciencedirect.com", ELSEVIER_PDF_HOSTS)
        self.assertNotIn("example.invalid", ELSEVIER_PDF_HOSTS)
        self.assertEqual(host_of(REAL_URL), "pdf.sciencedirectassets.com")


class AcceptanceTests(unittest.TestCase):
    def test_a_completed_download_for_the_locked_paper_is_accepted(self) -> None:
        with temp_root("bdd-ok-") as tmp:
            fixture = _Fixture(Path(tmp))

            async def script(f):
                f.session.emit("Browser.downloadWillBegin", begin())
                f.land()
                f.session.emit("Browser.downloadProgress", progress())

            result = run(fixture, script)

            self.assertEqual(result.guid, "g1")
            self.assertEqual(result.source_pii, PII)
            self.assertEqual(result.source_url_host, "pdf.sciencedirectassets.com")
            self.assertEqual(result.path.name, FILENAME)
            self.assertTrue(result.path.is_file())

    def test_the_browser_is_pointed_at_this_run(self) -> None:
        with temp_root("bdd-dir-") as tmp:
            fixture = _Fixture(Path(tmp))

            async def script(f):
                f.session.emit("Browser.downloadWillBegin", begin())
                f.land()
                f.session.emit("Browser.downloadProgress", progress())

            run(fixture, script)
            self.assertEqual(fixture.session.download_path, str(fixture.dir))

    def test_the_policy_is_released_when_the_capture_ends(self) -> None:
        """A finished run must not keep catching somebody else's downloads."""

        with temp_root("bdd-release-") as tmp:
            fixture = _Fixture(Path(tmp))

            async def script(f):
                f.session.emit("Browser.downloadWillBegin", begin())
                f.land()
                f.session.emit("Browser.downloadProgress", progress())

            run(fixture, script)
            self.assertEqual(fixture.session.behaviours, ["allow", "default"])
            self.assertFalse(fixture.directed.configured)


class RejectionTests(unittest.TestCase):
    def _expect_failure(self, prefix: str, script, *, locked: str = PII) -> str:
        with temp_root("bdd-reject-") as tmp:
            fixture = _Fixture(Path(tmp), locked=locked)
            with self.assertRaises(DirectedDownloadFailure) as caught:
                run(fixture, script)
            message = str(caught.exception)
            self.assertIn(prefix, message)
            return message

    def test_a_download_for_another_paper_is_refused(self) -> None:
        """Even though the file downloads perfectly and is a valid PDF."""

        foreign = REAL_URL.replace(PII, "S9999999999999999")

        async def script(f):
            f.session.emit(
                "Browser.downloadWillBegin", begin(url=foreign, filename="other.pdf")
            )
            f.land("other.pdf")
            f.session.emit("Browser.downloadProgress", progress())

        self._expect_failure("DOWNLOAD_EVENT_TIMEOUT", script)

    def test_a_correct_filename_cannot_rescue_a_foreign_url(self) -> None:
        """The filename is corroboration, never the authority."""

        foreign = "https://pdf.sciencedirectassets.com/x/main.pdf?pii=S9999999999999999"

        async def script(f):
            f.session.emit(
                "Browser.downloadWillBegin", begin(url=foreign, filename=FILENAME)
            )
            f.land(FILENAME)
            f.session.emit("Browser.downloadProgress", progress())

        self._expect_failure("DOWNLOAD_EVENT_TIMEOUT", script)

    def test_an_unrecognised_delivery_host_is_refused(self) -> None:
        evil = f"https://evil.example/main.pdf?pii={PII}"

        async def script(f):
            f.session.emit("Browser.downloadWillBegin", begin(url=evil))
            f.land()
            f.session.emit("Browser.downloadProgress", progress())

        self._expect_failure("DOWNLOAD_SOURCE_HOST_REJECTED", script)

    def test_an_unrelated_concurrent_download_is_not_adopted(self) -> None:
        """Another download starting in the same browser is not this paper."""

        other = f"https://pdf.sciencedirectassets.com/y/main.pdf?pii={OTHER_PII}"

        async def script(f):
            f.session.emit(
                "Browser.downloadWillBegin",
                begin(guid="unrelated", url=other, filename="unrelated.pdf"),
            )
            f.land("unrelated.pdf")
            f.session.emit("Browser.downloadProgress", progress(guid="unrelated"))

        message = self._expect_failure("DOWNLOAD_EVENT_TIMEOUT", script)
        self.assertIn("other downloads seen: 1", message)

    def test_a_canceled_download_fails(self) -> None:
        async def script(f):
            f.session.emit("Browser.downloadWillBegin", begin())
            f.session.emit("Browser.downloadProgress", progress(state="canceled"))

        self._expect_failure("DOWNLOAD_CANCELED", script)

    def test_a_download_that_never_completes_fails(self) -> None:
        async def script(f):
            f.session.emit("Browser.downloadWillBegin", begin())
            f.land()
            f.session.emit("Browser.downloadProgress", progress(state="inProgress"))

        self._expect_failure("DOWNLOAD_COMPLETION_TIMEOUT", script)

    def test_a_click_that_starts_nothing_fails_distinctly(self) -> None:
        """Not the old vague "did not produce a browser download"."""

        async def script(f):
            return None

        message = self._expect_failure("DOWNLOAD_EVENT_TIMEOUT", script)
        self.assertIn(PII, message)

    def test_completion_without_the_file_fails(self) -> None:
        """The browser saying so is necessary; it is not sufficient."""

        async def script(f):
            f.session.emit("Browser.downloadWillBegin", begin())
            f.session.emit("Browser.downloadProgress", progress())

        self._expect_failure("DOWNLOAD_FILE_MISSING", script)


class _Browser:
    """Weakly referenceable, like the real PlaywrightBrowser."""


class LeaseTests(unittest.TestCase):
    """The persistent browser is shared, and download behaviour is browser-wide."""

    def test_one_directed_capture_at_a_time_per_browser(self) -> None:
        browser = _Browser()
        lease = lease_for(browser)
        self.assertIs(lease_for(browser), lease)

        async def main():
            await lease.acquire("run-a")
            self.assertTrue(lease.held)
            second = asyncio.create_task(lease.acquire("run-b"))
            await asyncio.sleep(0.05)
            self.assertFalse(second.done(), "the second run must wait its turn")
            lease.release()
            await asyncio.wait_for(second, timeout=1)
            self.assertEqual(lease.holder, "run-b")
            lease.release()

        asyncio.run(main())

    def test_different_browsers_do_not_share_a_lease(self) -> None:
        first, second = _Browser(), _Browser()
        self.assertIsNot(lease_for(first), lease_for(second))

    def test_a_collected_browser_does_not_bequeath_its_lease(self) -> None:
        """Ids get reused; a lease must not outlive the browser it belonged to."""

        import gc

        one = _Browser()
        lease_for(one).holder = "gone"
        del one
        gc.collect()
        self.assertIsNone(lease_for(_Browser()).holder)

    def test_an_object_that_cannot_be_weakly_referenced_still_gets_a_lease(self) -> None:
        """Never crash a download over lease bookkeeping."""

        self.assertIsNotNone(lease_for(object()))


class BackendSelectionTests(unittest.TestCase):
    """Attached uses the directed path; a launched browser keeps its own."""

    def test_the_directed_method_exists_alongside_the_original(self) -> None:
        self.assertEqual(
            AcquisitionMethod.BROWSER_DIRECTED_DOWNLOAD.value,
            "BROWSER_DIRECTED_DOWNLOAD",
        )
        self.assertEqual(
            AcquisitionMethod.PLAYWRIGHT_DOWNLOAD_EVENT.value,
            "PLAYWRIGHT_DOWNLOAD_EVENT",
        )

    def test_a_launched_browser_is_not_diverted_to_the_directed_path(self) -> None:
        from hunnu_harness.browser.local_executor import LocalPlaywrightExecutor

        class _Launched:
            attached = False
            downloads_dir = TEMP_DIR
            page = object()
            context = object()

        executor = LocalPlaywrightExecutor(_Launched())
        result = asyncio.run(executor._directed_capture(object(), object()))
        self.assertIsNone(result, "a launched browser must keep the existing capture")


if __name__ == "__main__":
    unittest.main()
