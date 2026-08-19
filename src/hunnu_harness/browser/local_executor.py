"""Local command executor backed by an already-owned Python Playwright page.

This module is intentionally a backend implementation detail.  It does not
launch a browser, attach to the Research Chrome profile, or expose a page
object to source adapters.  Browser lifecycle and profile ownership remain
with the caller that supplied the existing local backend.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .authorized_file_capture import BrowserAuthorizedFileCapture
from .commands import (
    AuthenticatedFetchCommand,
    AuthenticatedFetchFailure,
    AuthenticatedFetchResult,
    BrowserActionResult,
    BrowserCommand,
    BrowserCommandError,
    BrowserCommandResult,
    BrowserObservation,
    BrowserPageSummary,
    BrowserTarget,
    BrowserTargetObservation,
    ClickCommand,
    DownloadArtifact,
    DownloadCaptureSpec,
    DownloadCommand,
    DownloadFailure,
    InvalidTarget,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
    PageHandle,
    SessionHandle,
    UnsupportedCommand,
)
from .transport import BrowserTransportError


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class LocalPlaywrightExecutor:
    """Translate typed commands to an existing local Playwright backend.

    ``legacy_browser`` is accepted only as a transition input for the
    v0.2.16 ``BrowserTransport``.  The executor never starts or closes that
    object; ownership and profile lifecycle stay outside this class.
    """

    def __init__(
        self,
        legacy_browser: Any | None = None,
        *,
        page: Any | None = None,
        context: Any | None = None,
        downloads_dir: Path | None = None,
        session: SessionHandle | None = None,
        page_handle: PageHandle | None = None,
    ) -> None:
        self._legacy_browser = legacy_browser
        self._page = page if page is not None else getattr(legacy_browser, "page", None)
        self._context = context if context is not None else getattr(legacy_browser, "context", None)
        if self._context is None and self._page is not None:
            self._context = getattr(self._page, "context", None)
        directory = downloads_dir if downloads_dir is not None else getattr(legacy_browser, "downloads_dir", None)
        if self._page is None and directory is None:
            raise BrowserTransportError(
                "LocalPlaywrightExecutor requires an existing Playwright page or downloads_dir"
            )
        self.downloads_dir = Path(directory) if directory is not None else None
        self.session = session or SessionHandle("local")
        self.page_handle = page_handle or PageHandle("main")
        self._generation = 0

    @property
    def navigation_provenance(self) -> tuple[str, ...]:
        value = getattr(self._legacy_browser, "navigation_provenance", ())
        if isinstance(value, str):
            return (value,)
        try:
            return tuple(str(item) for item in (value or ()))
        except TypeError:
            return ()

    def bound_to(self, value: Any) -> bool:
        """Return whether a compatibility wrapper is bound to ``value``."""

        return self is value or self._legacy_browser is value

    def supports(self, command_type: type[BrowserCommand]) -> bool:
        if command_type is AuthenticatedFetchCommand:
            context = self._context or getattr(self._page, "context", None)
            request = getattr(context, "request", None) if context is not None else None
            return callable(getattr(request, "get", None))
        return command_type in {NavigateCommand, ObserveCommand, ClickCommand, DownloadCommand}

    async def execute(self, command: BrowserCommand) -> BrowserCommandResult:
        if isinstance(command, NavigateCommand):
            return await self._navigate(command)
        if isinstance(command, ObserveCommand):
            return await self._observe(command)
        if isinstance(command, ClickCommand):
            return await self._click(command)
        if isinstance(command, DownloadCommand):
            return await self._download(command)
        if isinstance(command, AuthenticatedFetchCommand):
            return await self._authenticated_fetch(command)
        raise UnsupportedCommand(f"LocalPlaywrightExecutor does not support {type(command).__name__}")

    async def goto(self, url: str) -> None:
        """Compatibility-only alias for old non-adapter callers."""

        await self.execute(NavigateCommand(url))

    def __getattr__(self, name: str) -> Any:
        # A few v0.2.16 tests use a recording ``events`` list.  Delegating that
        # test-only value does not expose page/context/locator objects to the
        # migrated adapters.
        if name == "events" and self._legacy_browser is not None:
            return getattr(self._legacy_browser, name)
        raise AttributeError(name)

    def _require_page(self) -> Any:
        if self._page is None:
            raise ObservationUnavailable("Local Playwright page is unavailable")
        return self._page

    def _context_or_none(self) -> Any | None:
        return self._context or getattr(self._page, "context", None)

    @staticmethod
    def _page_url(page: Any) -> str:
        value = getattr(page, "url", "about:blank")
        return str(value() if callable(value) else value)

    @staticmethod
    async def _page_title(page: Any) -> str:
        value = getattr(page, "title", "")
        if callable(value):
            value = await _maybe_await(value())
        return str(value or "")

    @staticmethod
    def _frame_name(frame: Any) -> str:
        value = getattr(frame, "name", "")
        if callable(value):
            value = value()
        return str(value or "")[:80]

    async def _navigate(self, command: NavigateCommand) -> BrowserObservation:
        page = self._require_page()
        goto = getattr(page, "goto", None)
        if not callable(goto) and self._legacy_browser is not None:
            goto = getattr(self._legacy_browser, "goto", None)
        if not callable(goto):
            raise UnsupportedCommand("Local page does not expose navigation")
        try:
            await _maybe_await(goto(command.url, wait_until=command.wait_until))
        except TypeError:
            # Small deterministic test pages often expose only goto(url).
            await _maybe_await(goto(command.url))
        self._generation += 1
        return await self._observe(ObserveCommand(include_html=False, include_visible_text=False))

    async def _observe(self, command: ObserveCommand) -> BrowserObservation:
        page = self._require_page()
        url = self._page_url(page)
        title = await self._page_title(page)
        html: str | None = None
        inspection_complete = True
        if command.include_html:
            content = getattr(page, "content", None)
            if not callable(content):
                raise ObservationUnavailable("Local page does not expose HTML content")
            try:
                html = str(await _maybe_await(content()))
            except Exception as exc:
                raise ObservationUnavailable(
                    f"Local page HTML observation failed: {type(exc).__name__}"
                ) from exc

        visible_text: str | None = None
        if command.include_visible_text:
            try:
                body = page.locator("body")
                inner_text = getattr(body, "inner_text", None)
                if callable(inner_text):
                    visible_text = str(await _maybe_await(inner_text()))
            except Exception:
                visible_text = None

        inventory = await self._page_inventory(page)
        target_observations: tuple[BrowserTargetObservation, ...] = ()
        if command.text_probes:
            target_observations, probe_complete = await self._probe_text_targets(page, command)
            inspection_complete = inspection_complete and probe_complete

        return BrowserObservation(
            session=self.session,
            page=self.page_handle,
            generation=self._generation,
            url=url,
            title=title,
            html=html,
            visible_text=visible_text,
            page_inventory=inventory,
            target_observations=target_observations,
            inspection_complete=inspection_complete,
            metadata={"Backend": "LocalPlaywrightExecutor"},
        )

    async def _page_inventory(self, current_page: Any) -> tuple[BrowserPageSummary, ...]:
        context = self._context_or_none()
        candidates = getattr(context, "pages", None) if context is not None else None
        candidates = list(candidates or [current_page])
        summaries: list[BrowserPageSummary] = []
        for index, candidate in enumerate(candidates):
            try:
                summaries.append(
                    BrowserPageSummary(
                        index=index,
                        title=await self._page_title(candidate),
                        url=self._page_url(candidate),
                    )
                )
            except Exception:
                continue
        if not summaries:
            summaries.append(
                BrowserPageSummary(index=0, title=await self._page_title(current_page), url=self._page_url(current_page))
            )
        return tuple(summaries)

    async def _probe_text_targets(
        self,
        page: Any,
        command: ObserveCommand,
    ) -> tuple[tuple[BrowserTargetObservation, ...], bool]:
        frames_value = getattr(page, "frames", None)
        frames = list(frames_value or [page])
        viewport = await self._viewport(page)
        observations: list[BrowserTargetObservation] = []
        complete = True
        for frame_index, frame in enumerate(frames):
            frame_visible, frame_box, frame_complete = await self._frame_visibility(page, frame, viewport)
            complete = complete and frame_complete
            for pattern in command.text_probes:
                try:
                    get_by_text = getattr(frame, "get_by_text", None)
                    if not callable(get_by_text):
                        complete = False
                        continue
                    locator = get_by_text(re.compile(pattern, re.IGNORECASE), exact=False)
                    count_value = getattr(locator, "count", None)
                    if not callable(count_value):
                        complete = False
                        continue
                    count = int(await _maybe_await(count_value()))
                    if count > command.max_probe_matches:
                        complete = False
                    for index in range(min(count, command.max_probe_matches)):
                        candidate = locator.nth(index)
                        node_complete = True
                        visible: bool | None = None
                        box: Mapping[str, float] | None = None
                        render: Mapping[str, Any] = {}
                        try:
                            visible = bool(await _maybe_await(candidate.is_visible()))
                            box = await _maybe_await(candidate.bounding_box())
                            evaluate = getattr(candidate, "evaluate", None)
                            if callable(evaluate):
                                render_value = await _maybe_await(
                                    evaluate(
                                        """(el) => {
                                          const style = getComputedStyle(el);
                                          const rect = el.getBoundingClientRect();
                                          return {
                                            display: style.display,
                                            visibility: style.visibility,
                                            opacity: style.opacity,
                                            pointerEvents: style.pointerEvents,
                                            ariaHidden: el.getAttribute('aria-hidden'),
                                            clientWidth: el.clientWidth,
                                            clientHeight: el.clientHeight,
                                            rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height}
                                          };
                                        }"""
                                    )
                                )
                                if isinstance(render_value, Mapping):
                                    render = render_value
                        except Exception:
                            node_complete = False
                            complete = False
                        client_rect = render.get("rect") if isinstance(render, Mapping) else None
                        observations.append(
                            BrowserTargetObservation(
                                marker=pattern,
                                frame_index=frame_index,
                                frame_name=self._frame_name(frame),
                                frame_url=self._page_url(frame),
                                playwright_visible=visible,
                                bounding_box=box if isinstance(box, Mapping) else None,
                                client_rect=client_rect if isinstance(client_rect, Mapping) else None,
                                display=render.get("display"),
                                visibility=render.get("visibility"),
                                opacity=render.get("opacity"),
                                pointer_events=render.get("pointerEvents"),
                                aria_hidden=render.get("ariaHidden"),
                                client_width=render.get("clientWidth"),
                                client_height=render.get("clientHeight"),
                                viewport_width=viewport[0],
                                viewport_height=viewport[1],
                                frame_viewport_visible=frame_visible,
                                inspection_complete=node_complete,
                            )
                        )
                except Exception:
                    complete = False
        return tuple(observations), complete

    async def _viewport(self, page: Any) -> tuple[float, float]:
        value = getattr(page, "viewport_size", None)
        if callable(value):
            value = await _maybe_await(value())
        if isinstance(value, Mapping):
            return float(value.get("width", 0)), float(value.get("height", 0))
        evaluate = getattr(page, "evaluate", None)
        if callable(evaluate):
            try:
                value = await _maybe_await(evaluate("() => ({width: innerWidth, height: innerHeight})"))
                if isinstance(value, Mapping):
                    return float(value.get("width", 0)), float(value.get("height", 0))
            except Exception:
                pass
        return 0.0, 0.0

    async def _frame_visibility(
        self,
        page: Any,
        frame: Any,
        viewport: tuple[float, float],
    ) -> tuple[bool | None, Mapping[str, float] | None, bool]:
        if frame is page:
            return True, None, True
        try:
            frame_element = await _maybe_await(getattr(frame, "frame_element")())
            visible = bool(await _maybe_await(frame_element.is_visible()))
            box = await _maybe_await(frame_element.bounding_box())
            if not isinstance(box, Mapping):
                return None, None, False
            intersects = (
                float(box.get("width", 0)) > 0
                and float(box.get("height", 0)) > 0
                and float(box.get("x", 0)) + float(box.get("width", 0)) > 0
                and float(box.get("y", 0)) + float(box.get("height", 0)) > 0
                and float(box.get("x", 0)) < viewport[0]
                and float(box.get("y", 0)) < viewport[1]
            )
            return bool(visible and intersects), box, True
        except Exception:
            return None, None, False

    def _resolve_locator(self, target: BrowserTarget) -> Any:
        page = self._require_page()
        if target.css:
            locator_factory = getattr(page, "locator", None)
            if not callable(locator_factory):
                raise UnsupportedCommand("Local page does not expose CSS locator resolution")
            locator = locator_factory(target.css)
            if target.text is not None or target.text_regex is not None:
                filter_method = getattr(locator, "filter", None)
                if not callable(filter_method):
                    raise UnsupportedCommand("Local locator does not expose bounded text filtering")
                text_value: Any = target.text
                if target.text_regex is not None:
                    text_value = re.compile(target.text_regex, re.IGNORECASE)
                locator = filter_method(has_text=text_value)
        else:
            get_by_text = getattr(page, "get_by_text", None)
            if not callable(get_by_text):
                raise UnsupportedCommand("Local page does not expose text target resolution")
            text_value: Any = target.text
            if target.text_regex is not None:
                text_value = re.compile(target.text_regex, re.IGNORECASE)
            locator = get_by_text(text_value, exact=target.exact_text)
        if target.occurrence:
            nth = getattr(locator, "nth", None)
            if not callable(nth):
                raise InvalidTarget("Local locator cannot select a requested occurrence")
            return nth(target.occurrence)
        first = getattr(locator, "first", None)
        return first if first is not None else locator

    async def _click(self, command: ClickCommand) -> BrowserActionResult:
        locator = self._resolve_locator(command.target)
        click = getattr(locator, "click", None)
        if not callable(click):
            raise UnsupportedCommand("Local locator does not expose click")
        try:
            await _maybe_await(click())
        except Exception as exc:
            raise BrowserCommandError(f"Local browser click failed: {type(exc).__name__}") from exc
        self._generation += 1
        return BrowserActionResult(
            session=self.session,
            page=self.page_handle,
            generation=self._generation,
            action="click",
            url=self._page_url(self._require_page()),
        )

    @staticmethod
    def _safe_filename(value: str) -> str:
        name = Path(value).name
        name = re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("._")
        return name or "download.bin"

    def _target_path(self, suggested_filename: str) -> Path:
        if self.downloads_dir is None:
            raise DownloadFailure("Local browser downloads_dir is unavailable")
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        base = self.downloads_dir / self._safe_filename(suggested_filename)
        target = base
        counter = 1
        while target.exists():
            target = base.with_name(f"{base.stem}_{counter}{base.suffix}")
            counter += 1
        return target

    @staticmethod
    def _expected_url_predicate(expected_url: str | None):
        if not expected_url:
            return lambda _value: True
        expected = urlsplit(expected_url)
        expected_host = (expected.hostname or "").casefold().rstrip(".")
        expected_path = expected.path.casefold()
        expected_name = Path(expected_path).name

        def matches(value: str) -> bool:
            candidate = urlsplit(value)
            candidate_host = (candidate.hostname or "").casefold().rstrip(".")
            candidate_path = candidate.path.casefold()
            if expected_host and candidate_host != expected_host:
                return False
            return bool(
                candidate_path == expected_path
                or (expected_path and expected_path in candidate_path)
                or (expected_name and candidate_path.endswith(f"/{expected_name}"))
            )

        return matches

    async def _download(self, command: DownloadCommand) -> DownloadArtifact:
        page = self._require_page()
        locator = self._resolve_locator(command.target)
        click = getattr(locator, "click", None)
        if not callable(click):
            raise UnsupportedCommand("Local download target does not expose click")
        try:
            if command.capture is not None:
                result = await self._capture_authorized_pdf(page, locator, command)
                artifact = DownloadArtifact.from_path(
                    result.path,
                    suggested_filename=command.suggested_filename,
                    source_url=command.capture.expected_url or self._page_url(page),
                    page=self.page_handle,
                    mime_type="application/pdf",
                    metadata={
                        "AcquisitionMethod": result.acquisition_method.value,
                        "SourceHost": result.source_host,
                        "SourceRoute": result.source_route,
                        "DownloadEventEmitted": result.download_event_emitted,
                        "AuthorizedPDFResponseCaptured": result.authorized_pdf_response_captured,
                    },
                )
            else:
                async with page.expect_download(timeout=command.timeout_ms) as download_info:
                    await _maybe_await(click())
                download = await _maybe_await(download_info.value)
                filename = str(getattr(download, "suggested_filename", "") or command.suggested_filename)
                target = self._target_path(filename)
                save_as = getattr(download, "save_as", None)
                if not callable(save_as):
                    raise UnsupportedCommand("Local download object does not expose save_as")
                await _maybe_await(save_as(str(target)))
                if not target.is_file():
                    raise DownloadFailure("Playwright download completed without a local file")
                artifact = DownloadArtifact.from_path(
                    target,
                    suggested_filename=filename,
                    source_url=self._page_url(page),
                    page=self.page_handle,
                )
        except (BrowserCommandError, UnsupportedCommand, InvalidTarget, DownloadFailure):
            raise
        except Exception as exc:
            raise DownloadFailure(f"Local browser download failed: {type(exc).__name__}") from exc
        self._generation += 1
        return artifact

    async def _capture_authorized_pdf(self, page: Any, locator: Any, command: DownloadCommand):
        spec = command.capture
        if spec is None:  # pragma: no cover - guarded by caller
            raise DownloadFailure("Authorized PDF capture specification is missing")
        if self.downloads_dir is None:
            raise DownloadFailure("Local browser downloads_dir is unavailable")
        capture = BrowserAuthorizedFileCapture(
            self.downloads_dir,
            allow_outside_output_for_tests=spec.allow_outside_output_for_tests,
        )
        return await capture.capture_pdf(
            page=page,
            official_action=locator.click,
            trusted_hosts=spec.trusted_hosts,
            response_url_is_expected=self._expected_url_predicate(spec.expected_url),
            controlled_filename=command.suggested_filename,
            provenance_host=spec.provenance_host,
            source_route=spec.source_route,
            timeout_ms=command.timeout_ms,
            require_download_event=spec.require_download_event or not spec.allow_response_capture,
        )

    async def _authenticated_fetch(self, command: AuthenticatedFetchCommand) -> AuthenticatedFetchResult:
        context = self._context_or_none()
        request = getattr(context, "request", None) if context is not None else None
        get = getattr(request, "get", None) if request is not None else None
        if not callable(get):
            raise UnsupportedCommand(
                "Local browser context does not expose authenticated request.get"
            )
        try:
            response = await _maybe_await(get(command.url, timeout=command.timeout_ms))
            status_value = getattr(response, "status", 0)
            status = int(await _maybe_await(status_value() if callable(status_value) else status_value))
            ok_value = getattr(response, "ok", 200 <= status < 400)
            ok = bool(await _maybe_await(ok_value() if callable(ok_value) else ok_value))
            headers_value = getattr(response, "headers", {})
            headers_value = await _maybe_await(headers_value() if callable(headers_value) else headers_value)
            headers: Mapping[str, str] = (
                {str(key): str(value) for key, value in headers_value.items()}
                if isinstance(headers_value, Mapping)
                else {}
            )
            artifact: DownloadArtifact | None = None
            if ok:
                body_method = getattr(response, "body", None)
                if not callable(body_method):
                    raise AuthenticatedFetchFailure("Authenticated response does not expose body()")
                payload = bytes(await _maybe_await(body_method()))
                target = self._target_path(command.suggested_filename)
                target.write_bytes(payload)
                artifact = DownloadArtifact.from_path(
                    target,
                    suggested_filename=command.suggested_filename,
                    source_url=command.url,
                    page=self.page_handle,
                    metadata={"AuthenticatedResponse": True, "HTTPStatus": status},
                )
            self._generation += 1
            return AuthenticatedFetchResult(
                url=command.url,
                status=status,
                ok=ok,
                headers=headers,
                artifact=artifact,
            )
        except (BrowserCommandError, UnsupportedCommand, AuthenticatedFetchFailure):
            raise
        except Exception as exc:
            raise AuthenticatedFetchFailure(
                f"Authenticated browser request failed: {type(exc).__name__}"
            ) from exc


__all__ = ["LocalPlaywrightExecutor"]
