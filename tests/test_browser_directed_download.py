"""Accepting a download the attached browser performed, and only that one.

The bytes were never the problem: a complete, valid 3.7 MB PDF arrived and was
staged while the run reported that no download had happened, because the
``download`` event never reached the page being awaited.  Waiting on the
browser's own account of the download removes that dependency.

What these pin is that removing it costs nothing in rigour.  The paper's
identifier has to match the recognised URL identity, the exact stem of a
suggested ``.pdf``/``.caj`` filename, or a sufficiently long bibliographic label
the adapter explicitly declared; the delivery host still has to be one this
Harness recognises; only the GUID of *this* download counts, and only after the
browser says it completed.  A file that merely turns up in the directory proves
nothing and is never enough.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from hunnu_harness.browser.authorized_file_capture import AcquisitionMethod
from hunnu_harness.browser.browser_directed_download import (
    DIRECTED_DOWNLOAD_DEFAULT_HOSTS,
    ELSEVIER_PDF_HOSTS,
    BrowserDirectedDownload,
    DirectedDownloadFailure,
    _host_is_allowed,
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
OUP_PDF_URL = (
    "https://academic.oup.com/rfs/article-pdf/36/9/3603/51141974/hhad021.pdf"
)
OUP_PII = "hhad021"
OUP_CDN_HOST = "oup.silverchair-cdn.com"
OUP_CDN_URL = (
    f"https://{OUP_CDN_HOST}/oup/backfile/Content_public/Journal/rfs/"
    "36/9/10.1093_rfs_hhad021/1/download.pdf?Expires=1788211200"
)
OUP_FILENAME = f"{OUP_PII}.pdf"
OUP_WATERMARK_HOST = "watermark02.silverchair.com"
OUP_WATERMARK_URL = f"https://{OUP_WATERMARK_HOST}/watermarked/{OUP_FILENAME}"
CNKI_ORDER_ID = "CNKI_ORDER_20260901_ABC123"
CNKI_ORDER_URL = (
    f"https://bar.cnki.net/bar/download/order?id={CNKI_ORDER_ID}&filename=paper.pdf"
)
CNKI_TITLE = "数字化转型与企业分工：专业化还是纵向一体化"
CNKI_AUTHOR = "袁淳"
CNKI_DECLARED_FILENAME = f"{CNKI_TITLE}_{CNKI_AUTHOR}.pdf"
CNKI_OPAQUE_DELIVERY_URL = "https://download.cnki.net/final/paper?token=opaque"
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
    def __init__(
        self,
        root: Path,
        *,
        locked: str = PII,
        locked_labels: tuple[str, ...] = (),
        allowed_hosts: frozenset[str] = DIRECTED_DOWNLOAD_DEFAULT_HOSTS,
    ) -> None:
        self.session = _Session()
        self.dir = root / "staging"
        self.directed = BrowserDirectedDownload(
            session=self.session,
            download_dir=self.dir,
            locked_pii=locked,
            locked_labels=locked_labels,
            allowed_hosts=allowed_hosts,
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

    def test_oup_article_pdf_path_yields_the_main_article_stem(self) -> None:
        self.assertEqual(pii_from_url(OUP_PDF_URL), "hhad021")

    def test_cnki_order_url_yields_its_single_paper_order_id(self) -> None:
        self.assertEqual(pii_from_url(CNKI_ORDER_URL), CNKI_ORDER_ID)

    def test_pii_query_keeps_priority_over_the_existing_path_form(self) -> None:
        url = f"https://www.sciencedirect.com/science/article/pii/{OTHER_PII}/pdfft?pii={PII}"
        self.assertEqual(pii_from_url(url), PII)

    def test_a_url_naming_no_paper_yields_nothing(self) -> None:
        for url in ("https://example.invalid/a.pdf", "", "not a url"):
            self.assertEqual(pii_from_url(url), "")

    def test_the_publisher_delivery_host_is_recognised(self) -> None:
        """The bytes do not come from the article host, and that is normal."""

        self.assertIn("pdf.sciencedirectassets.com", ELSEVIER_PDF_HOSTS)
        self.assertIn("www.sciencedirect.com", ELSEVIER_PDF_HOSTS)
        self.assertNotIn("example.invalid", ELSEVIER_PDF_HOSTS)
        self.assertEqual(host_of(REAL_URL), "pdf.sciencedirectassets.com")

    def test_bare_attached_download_defaults_explicitly_include_cnki_delivery(self) -> None:
        # Intentional Task H decision: a bare attached DownloadCommand has no
        # capture-spec channel through which CNKI can declare its delivery host.
        self.assertTrue(ELSEVIER_PDF_HOSTS.issubset(DIRECTED_DOWNLOAD_DEFAULT_HOSTS))
        self.assertIn("bar.cnki.net", DIRECTED_DOWNLOAD_DEFAULT_HOSTS)
        self.assertIn("download.cnki.net", DIRECTED_DOWNLOAD_DEFAULT_HOSTS)
        self.assertIn("docdown.cnki.net", DIRECTED_DOWNLOAD_DEFAULT_HOSTS)
        self.assertNotIn("example.invalid", DIRECTED_DOWNLOAD_DEFAULT_HOSTS)

    def test_declared_suffix_allows_a_silverchair_watermark_delivery_host(self) -> None:
        self.assertTrue(
            _host_is_allowed("watermark02.silverchair.com", frozenset({".silverchair.com"}))
        )

    def test_declared_suffix_does_not_allow_the_bare_domain(self) -> None:
        self.assertFalse(_host_is_allowed("silverchair.com", frozenset({".silverchair.com"})))

    def test_default_hosts_remain_exact_entries_only(self) -> None:
        self.assertFalse(any(host.startswith(".") for host in DIRECTED_DOWNLOAD_DEFAULT_HOSTS))
        self.assertFalse(_host_is_allowed("watermark02.silverchair.com", DIRECTED_DOWNLOAD_DEFAULT_HOSTS))


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

    def test_a_cdn_url_can_be_claimed_by_the_locked_safe_suggested_filename(self) -> None:
        self.assertEqual(pii_from_url(OUP_CDN_URL), "")
        with temp_root("bdd-cdn-filename-") as tmp:
            fixture = _Fixture(
                Path(tmp),
                locked=OUP_PII,
                allowed_hosts=frozenset({OUP_CDN_HOST}),
            )

            async def script(f):
                f.session.emit(
                    "Browser.downloadWillBegin",
                    begin(url=OUP_CDN_URL, filename=OUP_FILENAME),
                )
                f.land(OUP_FILENAME)
                f.session.emit("Browser.downloadProgress", progress())

            result = run(fixture, script)

            self.assertEqual(result.source_pii, OUP_PII)
            self.assertEqual(result.as_dict()["DownloadSourcePII"], OUP_PII)
            self.assertEqual(result.source_url_host, OUP_CDN_HOST)
            self.assertEqual(result.path.name, OUP_FILENAME)

    def test_a_declared_suffix_allows_a_watermarked_silverchair_delivery(self) -> None:
        with temp_root("bdd-watermark-suffix-") as tmp:
            fixture = _Fixture(
                Path(tmp),
                locked=OUP_PII,
                allowed_hosts=frozenset({".silverchair.com"}),
            )

            async def script(f):
                f.session.emit(
                    "Browser.downloadWillBegin",
                    begin(url=OUP_WATERMARK_URL, filename=OUP_FILENAME),
                )
                f.land(OUP_FILENAME)
                f.session.emit("Browser.downloadProgress", progress())

            result = run(fixture, script)

            self.assertEqual(result.source_url_host, OUP_WATERMARK_HOST)
            self.assertEqual(result.path.name, OUP_FILENAME)

    def test_declared_title_label_claims_filename_with_author_suffix(self) -> None:
        self.assertEqual(pii_from_url(CNKI_OPAQUE_DELIVERY_URL), "")
        with temp_root("bdd-declared-title-") as tmp:
            fixture = _Fixture(
                Path(tmp),
                locked=CNKI_ORDER_ID,
                locked_labels=(CNKI_TITLE,),
            )

            async def script(f):
                f.session.emit(
                    "Browser.downloadWillBegin",
                    begin(
                        url=CNKI_OPAQUE_DELIVERY_URL,
                        filename=CNKI_DECLARED_FILENAME,
                    ),
                )
                f.land(CNKI_DECLARED_FILENAME)
                f.session.emit("Browser.downloadProgress", progress())

            result = run(fixture, script)

            self.assertEqual(result.source_pii, CNKI_TITLE)
            self.assertEqual(result.source_url_host, "download.cnki.net")
            self.assertEqual(result.path.name, CNKI_DECLARED_FILENAME)

    def test_declared_label_matching_normalizes_nfkc_case_and_whitespace(self) -> None:
        label = "数字化转型:Professional Evidence"
        filename = "数字化 转型：professional evidence_袁淳.caj"
        with temp_root("bdd-declared-normalized-") as tmp:
            fixture = _Fixture(
                Path(tmp),
                locked=CNKI_ORDER_ID,
                locked_labels=(label,),
            )

            async def script(f):
                f.session.emit(
                    "Browser.downloadWillBegin",
                    begin(url=CNKI_OPAQUE_DELIVERY_URL, filename=filename),
                )
                f.land(filename)
                f.session.emit("Browser.downloadProgress", progress())

            result = run(fixture, script)

            self.assertEqual(result.source_pii, label)
            self.assertEqual(result.path.name, filename)

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
    def _expect_failure(
        self,
        prefix: str,
        script,
        *,
        locked: str = PII,
        locked_labels: tuple[str, ...] = (),
        allowed_hosts: frozenset[str] = DIRECTED_DOWNLOAD_DEFAULT_HOSTS,
    ) -> str:
        with temp_root("bdd-reject-") as tmp:
            fixture = _Fixture(
                Path(tmp),
                locked=locked,
                locked_labels=locked_labels,
                allowed_hosts=allowed_hosts,
            )
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

    def test_a_filename_that_only_contains_the_locked_pii_is_not_an_exact_match(self) -> None:
        """The second source is an exact safe-extension stem, not a substring."""

        foreign = "https://pdf.sciencedirectassets.com/x/main.pdf?pii=S9999999999999999"

        async def script(f):
            f.session.emit(
                "Browser.downloadWillBegin", begin(url=foreign, filename=FILENAME)
            )
            f.land(FILENAME)
            f.session.emit("Browser.downloadProgress", progress())

        self._expect_failure("DOWNLOAD_EVENT_TIMEOUT", script)

    def test_a_cdn_url_with_another_suggested_filename_is_not_claimed(self) -> None:
        async def script(f):
            f.session.emit(
                "Browser.downloadWillBegin",
                begin(url=OUP_CDN_URL, filename="other.pdf"),
            )
            f.land("other.pdf")
            f.session.emit("Browser.downloadProgress", progress())

        self._expect_failure(
            "DOWNLOAD_EVENT_TIMEOUT",
            script,
            locked=OUP_PII,
            allowed_hosts=frozenset({OUP_CDN_HOST}),
        )

    def test_title_filename_is_not_claimed_without_declared_labels(self) -> None:
        async def script(f):
            f.session.emit(
                "Browser.downloadWillBegin",
                begin(
                    url=CNKI_OPAQUE_DELIVERY_URL,
                    filename=CNKI_DECLARED_FILENAME,
                ),
            )
            f.land(CNKI_DECLARED_FILENAME)
            f.session.emit("Browser.downloadProgress", progress())

        message = self._expect_failure(
            "DOWNLOAD_EVENT_TIMEOUT",
            script,
            locked=CNKI_ORDER_ID,
        )
        self.assertIn("declared labels: 0", message)

    def test_declared_title_label_does_not_bypass_the_host_allowlist(self) -> None:
        untrusted = "https://evil.example/cdn/download?token=opaque"

        async def script(f):
            f.session.emit(
                "Browser.downloadWillBegin",
                begin(url=untrusted, filename=CNKI_DECLARED_FILENAME),
            )
            f.land(CNKI_DECLARED_FILENAME)
            f.session.emit("Browser.downloadProgress", progress())

        self._expect_failure(
            "DOWNLOAD_SOURCE_HOST_REJECTED",
            script,
            locked=CNKI_ORDER_ID,
            locked_labels=(CNKI_TITLE,),
        )

    def test_declared_label_shorter_than_six_normalized_characters_is_not_claimed(self) -> None:
        short_label = "短标题"
        filename = f"{short_label}_作者.pdf"

        async def script(f):
            f.session.emit(
                "Browser.downloadWillBegin",
                begin(url=CNKI_OPAQUE_DELIVERY_URL, filename=filename),
            )
            f.land(filename)
            f.session.emit("Browser.downloadProgress", progress())

        message = self._expect_failure(
            "DOWNLOAD_EVENT_TIMEOUT",
            script,
            locked=CNKI_ORDER_ID,
            locked_labels=(short_label,),
        )
        self.assertIn("declared labels: 1", message)

    def test_a_locked_suggested_filename_does_not_bypass_the_host_allowlist(self) -> None:
        untrusted = "https://evil.example/cdn/download?token=opaque"

        async def script(f):
            f.session.emit(
                "Browser.downloadWillBegin",
                begin(url=untrusted, filename=OUP_FILENAME),
            )
            f.land(OUP_FILENAME)
            f.session.emit("Browser.downloadProgress", progress())

        self._expect_failure(
            "DOWNLOAD_SOURCE_HOST_REJECTED", script, locked=OUP_PII
        )

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
