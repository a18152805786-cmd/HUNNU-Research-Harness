from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..paths import require_output_path


class AcquisitionMethod(str, Enum):
    PLAYWRIGHT_DOWNLOAD_EVENT = "PLAYWRIGHT_DOWNLOAD_EVENT"
    AUTHORIZED_PDF_RESPONSE = "AUTHORIZED_PDF_RESPONSE"
    MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF = "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF"


class AuthorizedFileCaptureUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorizedFileCaptureResult:
    path: Path
    acquisition_method: AcquisitionMethod
    source_host: str
    source_route: str
    download_event_emitted: bool
    authorized_pdf_response_captured: bool


class BrowserAuthorizedFileCapture:
    """Capture one file produced by a visible, already-authorized browser action.

    The primitive does not decide source access or target identity. Callers must
    establish both before arming it. It listens only during one bounded action,
    prefers a normal Playwright download event, and otherwise accepts the body
    of the browser's own trusted PDF response. It never replays a request and
    never returns or persists response URLs, headers, cookies, or tokens.
    """

    _SAFE_FILENAME = re.compile(r"[^0-9A-Za-z._-]+")

    def __init__(
        self,
        staging_dir: Path,
        *,
        allow_outside_output_for_tests: bool = False,
    ) -> None:
        resolved = Path(staging_dir).resolve()
        if not allow_outside_output_for_tests:
            resolved = require_output_path(resolved, label="Authorized browser capture staging")
        resolved.mkdir(parents=True, exist_ok=True)
        self.staging_dir = resolved

    @classmethod
    def _safe_filename(cls, value: str) -> str:
        name = Path(value).name
        stem = cls._SAFE_FILENAME.sub("_", Path(name).stem).strip("._") or "authorized-file"
        return f"{stem}.pdf"

    def _unique_target(self, filename: str) -> Path:
        base = self.staging_dir / self._safe_filename(filename)
        target = base
        counter = 1
        while target.exists():
            target = base.with_name(f"{base.stem}_{counter}{base.suffix}")
            counter += 1
        return target

    @staticmethod
    def _response_headers(response: Any) -> Mapping[str, str]:
        headers = getattr(response, "headers", {})
        if callable(headers):
            headers = headers()
        return headers if isinstance(headers, Mapping) else {}

    @staticmethod
    def _host(value: str) -> str:
        try:
            return (urlsplit(value).hostname or "").casefold().rstrip(".")
        except ValueError:
            return ""

    @staticmethod
    def _remove_listener(emitter: Any, event: str, callback: Callable[..., Any]) -> None:
        remove = getattr(emitter, "remove_listener", None)
        if callable(remove):
            remove(event, callback)

    @staticmethod
    def _is_valid_pdf_file(path: Path) -> bool:
        try:
            if path.stat().st_size < 5:
                return False
            with path.open("rb") as stream:
                return stream.read(5) == b"%PDF-"
        except OSError:
            return False

    async def capture_pdf(
        self,
        *,
        page: Any,
        official_action: Callable[[], Awaitable[None]],
        trusted_hosts: tuple[str, ...],
        response_url_is_expected: Callable[[str], bool],
        controlled_filename: str,
        provenance_host: str,
        source_route: str,
        timeout_ms: int = 45_000,
        download_priority_grace_ms: int = 250,
        require_download_event: bool = False,
    ) -> AuthorizedFileCaptureResult:
        context = getattr(page, "context", None)
        if context is None:
            raise AuthorizedFileCaptureUnavailable("BrowserContext is unavailable")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")

        trusted = {host.casefold().rstrip(".") for host in trusted_hosts if host}
        if not trusted:
            raise ValueError("At least one exact trusted host is required")

        loop = asyncio.get_running_loop()
        download_future: asyncio.Future[AuthorizedFileCaptureResult] = loop.create_future()
        response_future: asyncio.Future[tuple[bytes, str]] = loop.create_future()
        tasks: set[asyncio.Task[Any]] = set()
        attached_pages: list[Any] = []
        armed = True
        download_event_emitted = False
        response_candidate_seen = False
        target = self._unique_target(controlled_filename)

        def track(coro: Awaitable[Any]) -> None:
            task = asyncio.create_task(coro)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        async def save_download(download: Any) -> None:
            nonlocal download_event_emitted
            download_event_emitted = True
            if not armed or download_future.done():
                return
            try:
                await download.save_as(str(target))
                if not armed or download_future.done():
                    return
                if not self._is_valid_pdf_file(target):
                    try:
                        target.unlink()
                    except OSError:
                        pass
                    raise AuthorizedFileCaptureUnavailable(
                        "Playwright download event produced an empty or invalid PDF"
                    )
                download_future.set_result(
                    AuthorizedFileCaptureResult(
                        path=target,
                        acquisition_method=AcquisitionMethod.PLAYWRIGHT_DOWNLOAD_EVENT,
                        source_host=provenance_host,
                        source_route=source_route,
                        download_event_emitted=True,
                        authorized_pdf_response_captured=False,
                    )
                )
            except Exception as exc:
                if not download_future.done():
                    if isinstance(exc, AuthorizedFileCaptureUnavailable):
                        download_future.set_exception(exc)
                    else:
                        download_future.set_exception(
                            AuthorizedFileCaptureUnavailable(
                                f"Playwright download event could not be saved: {type(exc).__name__}"
                            )
                        )

        def on_download(download: Any) -> None:
            if armed:
                track(save_download(download))

        def attach_page(candidate: Any) -> None:
            if candidate in attached_pages:
                return
            on = getattr(candidate, "on", None)
            if callable(on):
                on("download", on_download)
                attached_pages.append(candidate)

        async def read_pdf_response(response: Any) -> None:
            nonlocal response_candidate_seen
            if not armed or response_future.done():
                return
            url = str(getattr(response, "url", ""))
            host = self._host(url)
            headers = self._response_headers(response)
            content_type = next(
                (
                    str(value)
                    for key, value in headers.items()
                    if str(key).casefold() == "content-type"
                ),
                "",
            ).split(";", 1)[0].strip().casefold()
            if host not in trusted or content_type != "application/pdf":
                return
            try:
                expected = response_url_is_expected(url)
            except Exception:
                expected = False
            if not expected:
                return
            response_candidate_seen = True
            try:
                payload = await response.body()
            except Exception:
                return
            if not armed or response_future.done() or not payload or not payload.startswith(b"%PDF-"):
                return
            response_future.set_result((bytes(payload), host))

        def on_response(response: Any) -> None:
            if armed:
                track(read_pdf_response(response))

        def on_page(candidate: Any) -> None:
            if armed:
                attach_page(candidate)

        attach_page(page)
        for candidate in tuple(getattr(context, "pages", ()) or ()):
            attach_page(candidate)
        context_on = getattr(context, "on", None)
        if not callable(context_on):
            raise AuthorizedFileCaptureUnavailable("BrowserContext event API is unavailable")
        context_on("page", on_page)
        if not require_download_event:
            context_on("response", on_response)

        try:
            await official_action()
            deadline = timeout_ms / 1000
            waitables = (
                (download_future,)
                if require_download_event
                else (download_future, response_future)
            )
            done, _ = await asyncio.wait(
                waitables,
                timeout=deadline,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                detail = (
                    "Official action produced no Playwright download event"
                    if require_download_event
                    else (
                        "Trusted PDF response body was unavailable"
                        if response_candidate_seen
                        else "Official action produced neither a download event nor an expected PDF response"
                    )
                )
                raise AuthorizedFileCaptureUnavailable(detail)

            if download_future.done() and not download_future.cancelled():
                return download_future.result()

            if (
                not require_download_event
                and response_future.done()
                and not response_future.cancelled()
            ):
                if download_priority_grace_ms > 0:
                    try:
                        return await asyncio.wait_for(
                            asyncio.shield(download_future),
                            timeout=download_priority_grace_ms / 1000,
                        )
                    except TimeoutError:
                        pass
                payload, host = response_future.result()
                target.write_bytes(payload)
                return AuthorizedFileCaptureResult(
                    path=target,
                    acquisition_method=AcquisitionMethod.AUTHORIZED_PDF_RESPONSE,
                    source_host=host,
                    source_route=source_route,
                    download_event_emitted=download_event_emitted,
                    authorized_pdf_response_captured=True,
                )

            raise AuthorizedFileCaptureUnavailable("Authorized file capture ended without a usable file")
        finally:
            armed = False
            self._remove_listener(context, "page", on_page)
            if not require_download_event:
                self._remove_listener(context, "response", on_response)
            for candidate in attached_pages:
                self._remove_listener(candidate, "download", on_download)
            for task in tuple(tasks):
                if not task.done():
                    task.cancel()


__all__ = [
    "AcquisitionMethod",
    "AuthorizedFileCaptureResult",
    "AuthorizedFileCaptureUnavailable",
    "BrowserAuthorizedFileCapture",
]
