"""Multi-signal CNKI challenge detection without challenge interaction.

The CNKI page can preload verification components far outside the viewport.
Accessibility or DOM text is therefore evidence that a component exists, not
evidence that an active CAPTCHA is visible or blocking the user.  This module
keeps those facts separate and never performs authentication or challenge
actions.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .security import sanitize_text, sanitize_url


class ChallengeState(str, Enum):
    NONE = "NONE"
    DORMANT = "DORMANT"
    VISIBLE = "VISIBLE"
    BLOCKING = "BLOCKING"
    UNCERTAIN = "UNCERTAIN"


#: States that require a human to act before automation may continue.  Every
#: CNKI caller must consult this one set instead of re-deriving its own rule.
MANUAL_ACTION_CHALLENGE_STATES = frozenset(
    {ChallengeState.VISIBLE, ChallengeState.BLOCKING, ChallengeState.UNCERTAIN}
)

#: States that positively identify an active CAPTCHA rather than an unresolved
#: observation.  ``UNCERTAIN`` stops safely but is not a CAPTCHA claim.
CAPTCHA_CHALLENGE_STATES = frozenset({ChallengeState.VISIBLE, ChallengeState.BLOCKING})


def challenge_state_requires_manual_action(state: ChallengeState) -> bool:
    return state in MANUAL_ACTION_CHALLENGE_STATES


class TargetPageIdentityError(RuntimeError):
    """The current browser page cannot be locked to one CNKI target."""


@dataclass(frozen=True)
class ChallengeBoundingBox:
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> ChallengeBoundingBox | None:
        if not value:
            return None
        try:
            return cls(
                x=float(value["x"]),
                y=float(value["y"]),
                width=float(value["width"]),
                height=float(value["height"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def has_area(self) -> bool:
        return self.width > 0 and self.height > 0

    def intersects_viewport(self, width: float, height: float) -> bool:
        return (
            self.has_area
            and width > 0
            and height > 0
            and self.x + self.width > 0
            and self.y + self.height > 0
            and self.x < width
            and self.y < height
        )

    def as_dict(self) -> dict[str, float]:
        return {"X": self.x, "Y": self.y, "Width": self.width, "Height": self.height}


@dataclass(frozen=True)
class ChallengeNodeEvidence:
    marker: str
    frame_index: int = 0
    frame_name: str = ""
    frame_url: str = "unknown"
    playwright_visible: bool | None = None
    bounding_box: ChallengeBoundingBox | None = None
    client_rect: ChallengeBoundingBox | None = None
    display: str | None = None
    visibility: str | None = None
    opacity: str | None = None
    pointer_events: str | None = None
    aria_hidden: str | None = None
    client_width: float | None = None
    client_height: float | None = None
    viewport_width: float = 0
    viewport_height: float = 0
    frame_viewport_visible: bool | None = True
    inspection_complete: bool = True
    blocking_overlay: bool = False

    @property
    def render_visible(self) -> bool | None:
        if self.playwright_visible is False:
            return False
        if self.aria_hidden and self.aria_hidden.casefold() == "true":
            return False
        if self.display and self.display.casefold() == "none":
            return False
        if self.visibility and self.visibility.casefold() in {"hidden", "collapse"}:
            return False
        if self.client_width == 0 or self.client_height == 0:
            return False
        if self.opacity is not None:
            try:
                if float(self.opacity) <= 0:
                    return False
            except ValueError:
                return None
        if self.playwright_visible is None:
            return None
        return True

    @property
    def viewport_visible(self) -> bool | None:
        if self.frame_viewport_visible is False:
            return False
        if self.frame_viewport_visible is None:
            return None
        if self.bounding_box is None:
            return False if self.playwright_visible is False else None
        return self.bounding_box.intersects_viewport(self.viewport_width, self.viewport_height)

    @property
    def effective_visible(self) -> bool | None:
        render_visible = self.render_visible
        viewport_visible = self.viewport_visible
        if render_visible is False or viewport_visible is False:
            return False
        if render_visible is None or viewport_visible is None:
            return None
        return bool(self.playwright_visible and render_visible and viewport_visible)

    @property
    def actionable(self) -> bool:
        return self.effective_visible is True and (self.pointer_events or "").casefold() != "none"

    @property
    def visibility_assessed(self) -> bool:
        return self.inspection_complete and self.effective_visible is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "Marker": self.marker,
            "FrameIndex": self.frame_index,
            "FrameName": self.frame_name,
            "FrameURL": _stable_runtime_url(self.frame_url),
            "PlaywrightIsVisible": self.playwright_visible,
            "BoundingBoxPresent": self.bounding_box is not None,
            "BoundingBox": self.bounding_box.as_dict() if self.bounding_box else None,
            "ClientRect": self.client_rect.as_dict() if self.client_rect else None,
            "ViewportVisible": self.viewport_visible,
            "EffectiveVisible": self.effective_visible,
            "Display": self.display,
            "Visibility": self.visibility,
            "Opacity": self.opacity,
            "PointerEvents": self.pointer_events,
            "AriaHidden": self.aria_hidden,
            "ClientWidth": self.client_width,
            "ClientHeight": self.client_height,
            "InspectionComplete": self.inspection_complete,
        }


@dataclass(frozen=True)
class ChallengeFrameEvidence:
    frame_index: int
    frame_name: str
    frame_url: str
    playwright_visible: bool | None
    viewport_visible: bool | None
    bounding_box_present: bool
    challenge_node_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "FrameIndex": self.frame_index,
            "FrameName": self.frame_name,
            "FrameURL": _stable_runtime_url(self.frame_url),
            "PlaywrightIsVisible": self.playwright_visible,
            "ViewportVisible": self.viewport_visible,
            "BoundingBoxPresent": self.bounding_box_present,
            "ChallengeNodeCount": self.challenge_node_count,
        }


@dataclass(frozen=True)
class PageIdentityEvidence:
    page_index: int
    title: str
    url: str

    @property
    def stable_url(self) -> str:
        return _stable_runtime_url(self.url)

    def as_dict(self) -> dict[str, Any]:
        return {"PageIndex": self.page_index, "PageTitle": self.title, "PageURL": self.stable_url}


@dataclass(frozen=True)
class ChallengeDiagnostic:
    state: ChallengeState
    nodes: tuple[ChallengeNodeEvidence, ...] = ()
    frames: tuple[ChallengeFrameEvidence, ...] = ()
    page_inventory: tuple[PageIdentityEvidence, ...] = ()
    target_page_confirmed: bool = True
    target_page_index: int | None = None
    target_page_identity_basis: tuple[str, ...] = ()
    route_provenance: tuple[str, ...] = ()
    blocking: bool = False
    inspection_complete: bool = True
    errors: tuple[str, ...] = ()
    bypass_attempted: bool = False

    @property
    def node_detected(self) -> bool:
        return bool(self.nodes)

    @property
    def visible(self) -> bool:
        return any(node.effective_visible is True for node in self.nodes)

    @property
    def actionable(self) -> bool:
        return any(node.actionable for node in self.nodes)

    @property
    def captcha_detected(self) -> bool:
        return self.state in CAPTCHA_CHALLENGE_STATES

    @property
    def action_required_user_login(self) -> bool:
        return challenge_state_requires_manual_action(self.state)

    def as_dict(self) -> dict[str, Any]:
        visible_frames = {node.frame_index for node in self.nodes if node.effective_visible is True}
        challenge_frames = {node.frame_index for node in self.nodes}
        return {
            "ChallengeState": self.state.value,
            "ChallengeNodeDetected": self.node_detected,
            "ChallengeTextMatches": sorted({node.marker for node in self.nodes}),
            "ChallengeVisible": self.visible,
            "ChallengeBlocking": self.blocking,
            "ChallengeActionable": self.actionable,
            "ChallengeBoundingBoxPresent": any(node.bounding_box is not None for node in self.nodes),
            "ChallengeFrameCount": len(challenge_frames),
            "ChallengeVisibleFrameCount": len(visible_frames),
            "PlaywrightIsVisibleAny": any(node.playwright_visible is True for node in self.nodes),
            "CaptchaDetected": self.captcha_detected,
            "ACTION_REQUIRED_USER_LOGIN": self.action_required_user_login,
            "BrowserReadyForManualAction": self.action_required_user_login,
            "TargetPageConfirmed": self.target_page_confirmed,
            "TargetPageIndex": self.target_page_index,
            "TargetPageIdentityBasis": list(self.target_page_identity_basis),
            "RouteProvenance": list(self.route_provenance),
            "InspectionComplete": self.inspection_complete,
            "Errors": list(self.errors),
            "Frames": [frame.as_dict() for frame in self.frames],
            "Nodes": [node.as_dict() for node in self.nodes],
            "CaptchaInteractionPerformed": False,
            "CaptchaBypassAttempted": self.bypass_attempted,
        }


def _stable_runtime_url(value: str) -> str:
    if value in {"about:blank", "unknown", ""}:
        return value or "unknown"
    try:
        parsed = urlsplit(value)
        if (parsed.hostname or "").casefold() == "yclib.hunnu.edu.cn" and parsed.path.casefold().startswith("/vpn/"):
            return f"{parsed.scheme}://{parsed.hostname}/vpn/"
    except ValueError:
        pass
    return sanitize_url(value)


def _hostname(value: str) -> str:
    try:
        return (urlsplit(value).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _is_cnki_target_page(page: PageIdentityEvidence) -> bool:
    host = _hostname(page.url)
    if host == "cnki.net" or host.endswith(".cnki.net"):
        return True
    return host == "yclib.hunnu.edu.cn" and any(
        marker in page.title.casefold() for marker in ("cnki", "中国知网", "知网")
    )


def identify_cnki_target_page(
    pages: Sequence[PageIdentityEvidence],
    *,
    current_url: str,
    current_title: str,
    current_index: int | None = None,
    route_provenance: Sequence[str] = (),
) -> tuple[PageIdentityEvidence, tuple[str, ...]]:
    """Lock the active CNKI page by identity, never by tab number alone.

    A broker-provided runtime index may disambiguate otherwise identical
    sanitized URL/title evidence only within the same fresh observation.  It
    is never sufficient without the existing official-host, URL, and title
    checks.
    """

    candidates = [page for page in pages if _is_cnki_target_page(page)]
    current_stable_url = _stable_runtime_url(current_url)
    current_title_normalized = re.sub(r"\s+", " ", current_title).strip().casefold()
    exact_current = [
        page
        for page in candidates
        if page.stable_url == current_stable_url
        and re.sub(r"\s+", " ", page.title).strip().casefold() == current_title_normalized
    ]
    runtime_index_used = False
    if current_index is not None:
        indexed_current = [page for page in exact_current if page.page_index == current_index]
        if len(indexed_current) != 1:
            raise TargetPageIdentityError(
                "TargetPageIdentity=uncertain; broker runtime index conflicts with current CNKI URL/title"
            )
        exact_current = indexed_current
        runtime_index_used = True
    if len(exact_current) != 1:
        raise TargetPageIdentityError(
            "TargetPageIdentity=uncertain; expected one current CNKI page locked by stable URL and title"
        )
    if (
        not runtime_index_used
        and len(
            [
                page
                for page in candidates
                if page.stable_url == current_stable_url and page.title == exact_current[0].title
            ]
        )
        > 1
    ):
        raise TargetPageIdentityError(
            "TargetPageIdentity=uncertain; duplicate CNKI pages share the same stable URL and title"
        )
    basis = ["official-cnki-domain-or-HUNNU-gateway", "current-stable-url", "current-page-title"]
    if runtime_index_used:
        basis.append("broker-runtime-index-correlated")
    if route_provenance:
        basis.append("navigation-provenance")
    return exact_current[0], tuple(basis)


def classify_challenge(
    nodes: Iterable[ChallengeNodeEvidence],
    *,
    frames: Iterable[ChallengeFrameEvidence] = (),
    page_inventory: Iterable[PageIdentityEvidence] = (),
    target_page_confirmed: bool = True,
    target_page_index: int | None = None,
    target_page_identity_basis: Sequence[str] = (),
    route_provenance: Sequence[str] = (),
    blocking_evidence: bool = False,
    inspection_complete: bool = True,
    errors: Sequence[str] = (),
) -> ChallengeDiagnostic:
    evidence = tuple(nodes)
    visible = any(node.effective_visible is True for node in evidence)
    unresolved = any(not node.visibility_assessed for node in evidence)
    blocking = blocking_evidence or any(node.blocking_overlay and node.effective_visible is not False for node in evidence)
    if blocking:
        state = ChallengeState.BLOCKING
    elif visible:
        state = ChallengeState.VISIBLE
    elif not target_page_confirmed or not inspection_complete or unresolved:
        state = ChallengeState.UNCERTAIN
    elif evidence:
        state = ChallengeState.DORMANT
    else:
        state = ChallengeState.NONE
    return ChallengeDiagnostic(
        state=state,
        nodes=evidence,
        frames=tuple(frames),
        page_inventory=tuple(page_inventory),
        target_page_confirmed=target_page_confirmed,
        target_page_index=target_page_index,
        target_page_identity_basis=tuple(target_page_identity_basis),
        route_provenance=tuple(sanitize_text(item) for item in route_provenance),
        blocking=blocking,
        inspection_complete=inspection_complete,
        errors=tuple(sanitize_text(item) for item in errors),
        bypass_attempted=False,
    )


@dataclass(frozen=True)
class StaticChallengeEvidence:
    """Challenge evidence obtainable without a live visibility probe.

    Static HTML and accessibility snapshots can prove that a challenge
    component *exists*, and can sometimes prove that it is inert (declared
    hidden, moved far outside the document, or made fully transparent).  They
    cannot prove that a component is currently rendered on the user's screen,
    so this evidence is deliberately weaker than a runtime diagnostic.
    """

    text_present: bool = False
    on_screen_text_present: bool = False
    blocking_overlay: bool = False
    business_evidence: bool = False
    page_identity_is_challenge: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ChallengeTextPresent": self.text_present,
            "ChallengeOnScreenTextPresent": self.on_screen_text_present,
            "ChallengeStaticBlockingOverlay": self.blocking_overlay,
            "NormalBusinessEvidence": self.business_evidence,
            "PageIdentityIsChallenge": self.page_identity_is_challenge,
        }


def classify_static_challenge(evidence: StaticChallengeEvidence) -> ChallengeState:
    """Classify static challenge evidence without ever assuming activity.

    The ordering encodes the rule that markup text is not a CAPTCHA:

    * no marker text at all -> ``NONE``;
    * a statically provable blocking overlay -> ``BLOCKING``;
    * marker text that is provably rendered on screen while the page shows no
      working business content, or a document whose own identity is a
      challenge interstitial -> ``VISIBLE``;
    * anything else, including text that is only present in the DOM or in an
      accessibility tree -> ``DORMANT``.

    Static evidence never yields ``UNCERTAIN``: an unproven hidden string is
    not a reason to stop the run.
    """

    if not evidence.text_present:
        return ChallengeState.NONE
    if evidence.blocking_overlay:
        return ChallengeState.BLOCKING
    if evidence.page_identity_is_challenge:
        return ChallengeState.VISIBLE
    if evidence.on_screen_text_present and not evidence.business_evidence:
        return ChallengeState.VISIBLE
    return ChallengeState.DORMANT


def resolve_challenge_state(
    *,
    static_state: ChallengeState = ChallengeState.NONE,
    runtime_state: ChallengeState | None = None,
) -> ChallengeState:
    """Return the single authoritative CNKI challenge state.

    A runtime diagnostic is produced from viewport geometry, effective
    visibility, ancestor opacity, frame visibility, and overlay evidence.  It
    therefore outranks static text unconditionally: once the detector has
    ruled on an observation, no later keyword scan of the same page may
    re-escalate it.
    """

    if runtime_state is not None:
        return runtime_state
    return static_state


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _page_title(page: Any) -> str:
    title = getattr(page, "title", "")
    return str(await _maybe_await(title() if callable(title) else title))


def _page_url(page: Any) -> str:
    url = getattr(page, "url", "unknown")
    return str(url() if callable(url) else url)


def _frame_name(frame: Any) -> str:
    value = getattr(frame, "name", "")
    name = str(value() if callable(value) else value)
    if len(name) > 80 or re.fullmatch(r"[A-Za-z0-9._~-]{32,}", name):
        return "[redacted-dynamic]"
    return sanitize_text(name)


class CNKIChallengeDetector:
    """Read-only Playwright inspection for CNKI challenge state."""

    marker_patterns = (
        ("拖动下方拼图完成验证", re.compile(r"拖动下方拼图完成验证", re.I)),
        ("安全验证", re.compile(r"安全验证", re.I)),
        ("验证码", re.compile(r"验证码", re.I)),
        ("滑块", re.compile(r"滑块", re.I)),
        ("人机验证", re.compile(r"人机验证", re.I)),
        ("captcha", re.compile(r"captcha", re.I)),
    )
    max_matches_per_marker = 10

    @classmethod
    async def inspect_page(
        cls,
        page: Any,
        *,
        route_provenance: Sequence[str] = (),
        business_flow_blocked: bool = False,
    ) -> ChallengeDiagnostic:
        """Compatibility-only direct-page inspection for legacy callers.

        Migrated source-adapter execution uses ``inspect_observation`` below;
        this method remains for existing detector tests and local callers
        that still own a Python Playwright page.
        """

        errors: list[str] = []
        inspection_complete = True
        context = getattr(page, "context", None)
        context = await _maybe_await(context() if callable(context) else context)
        pages_value = getattr(context, "pages", None) if context is not None else None
        pages = await _maybe_await(pages_value() if callable(pages_value) else pages_value)
        pages = list(pages or [page])
        inventory: list[PageIdentityEvidence] = []
        for index, candidate in enumerate(pages):
            try:
                inventory.append(PageIdentityEvidence(index, await _page_title(candidate), _page_url(candidate)))
            except Exception as exc:  # pragma: no cover - defensive against detached pages
                inspection_complete = False
                errors.append(f"page identity inspection failed: {type(exc).__name__}")
        current_title = await _page_title(page)
        current_url = _page_url(page)
        target: PageIdentityEvidence | None = None
        basis: tuple[str, ...] = ()
        target_confirmed = False
        try:
            target, basis = identify_cnki_target_page(
                inventory,
                current_url=current_url,
                current_title=current_title,
                route_provenance=route_provenance,
            )
            target_confirmed = True
        except TargetPageIdentityError as exc:
            errors.append(str(exc))

        viewport = getattr(page, "viewport_size", None)
        viewport = await _maybe_await(viewport() if callable(viewport) else viewport)
        if not viewport:
            try:
                viewport = await page.evaluate("() => ({width: innerWidth, height: innerHeight})")
            except Exception as exc:
                viewport = {"width": 0, "height": 0}
                inspection_complete = False
                errors.append(f"viewport inspection failed: {type(exc).__name__}")
        viewport_width = float(viewport.get("width", 0))
        viewport_height = float(viewport.get("height", 0))

        frames_value = getattr(page, "frames", None)
        runtime_frames = await _maybe_await(frames_value() if callable(frames_value) else frames_value)
        runtime_frames = list(runtime_frames or [page])
        node_evidence: list[ChallengeNodeEvidence] = []
        frame_evidence: list[ChallengeFrameEvidence] = []
        main_frame = getattr(page, "main_frame", page)
        main_frame = await _maybe_await(main_frame() if callable(main_frame) else main_frame)
        for frame_index, frame in enumerate(runtime_frames):
            frame_box: ChallengeBoundingBox | None = None
            frame_playwright_visible: bool | None = True
            frame_viewport_visible: bool | None = True
            if frame is not main_frame:
                try:
                    frame_element_method = getattr(frame, "frame_element")
                    frame_element = await _maybe_await(frame_element_method())
                    frame_playwright_visible = bool(await _maybe_await(frame_element.is_visible()))
                    frame_box = ChallengeBoundingBox.from_mapping(await _maybe_await(frame_element.bounding_box()))
                    frame_viewport_visible = bool(
                        frame_box
                        and frame_box.intersects_viewport(viewport_width, viewport_height)
                        and frame_playwright_visible
                    )
                except Exception as exc:
                    frame_playwright_visible = None
                    frame_viewport_visible = None
                    inspection_complete = False
                    errors.append(f"frame visibility inspection failed: {type(exc).__name__}")
            frame_node_start = len(node_evidence)
            for marker, pattern in cls.marker_patterns:
                try:
                    locator = frame.get_by_text(pattern, exact=False)
                    count = int(await _maybe_await(locator.count()))
                    if count > cls.max_matches_per_marker:
                        inspection_complete = False
                        errors.append(f"challenge marker match limit exceeded: {marker}")
                    for locator_index in range(min(count, cls.max_matches_per_marker)):
                        candidate = locator.nth(locator_index)
                        node_complete = True
                        playwright_visible: bool | None = None
                        box: ChallengeBoundingBox | None = None
                        render: Mapping[str, Any] = {}
                        try:
                            playwright_visible = bool(await _maybe_await(candidate.is_visible()))
                            box = ChallengeBoundingBox.from_mapping(await _maybe_await(candidate.bounding_box()))
                            render = await _maybe_await(
                                candidate.evaluate(
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
                        except Exception as exc:
                            node_complete = False
                            inspection_complete = False
                            errors.append(f"challenge node inspection failed: {type(exc).__name__}")
                        client_rect = ChallengeBoundingBox.from_mapping(render.get("rect"))
                        node_evidence.append(
                            ChallengeNodeEvidence(
                                marker=marker,
                                frame_index=frame_index,
                                frame_name=_frame_name(frame),
                                frame_url=_page_url(frame),
                                playwright_visible=playwright_visible,
                                bounding_box=box,
                                client_rect=client_rect,
                                display=render.get("display"),
                                visibility=render.get("visibility"),
                                opacity=render.get("opacity"),
                                pointer_events=render.get("pointerEvents"),
                                aria_hidden=render.get("ariaHidden"),
                                client_width=render.get("clientWidth"),
                                client_height=render.get("clientHeight"),
                                viewport_width=viewport_width,
                                viewport_height=viewport_height,
                                frame_viewport_visible=frame_viewport_visible,
                                inspection_complete=node_complete,
                            )
                        )
                except Exception as exc:
                    inspection_complete = False
                    errors.append(f"challenge marker inspection failed: {type(exc).__name__}")
            frame_evidence.append(
                ChallengeFrameEvidence(
                    frame_index=frame_index,
                    frame_name=_frame_name(frame),
                    frame_url=_page_url(frame),
                    playwright_visible=frame_playwright_visible,
                    viewport_visible=frame_viewport_visible,
                    bounding_box_present=frame_box is not None,
                    challenge_node_count=len(node_evidence) - frame_node_start,
                )
            )
        return classify_challenge(
            node_evidence,
            frames=frame_evidence,
            page_inventory=inventory,
            target_page_confirmed=target_confirmed,
            target_page_index=target.page_index if target else None,
            target_page_identity_basis=basis,
            route_provenance=route_provenance,
            blocking_evidence=business_flow_blocked,
            inspection_complete=inspection_complete,
            errors=errors,
        )

    @classmethod
    def inspect_observation(
        cls,
        observation: Any,
        *,
        route_provenance: Sequence[str] = (),
        business_flow_blocked: bool = False,
    ) -> ChallengeDiagnostic:
        """Classify a structured command-layer observation.

        This is the command-boundary equivalent of ``inspect_page``.  It
        consumes only sanitized page summaries and bounded visibility probes;
        it never receives a Playwright page, locator, context, or callback.
        Missing probe evidence is deliberately classified as uncertain so the
        adapter pauses for manual action instead of attempting a bypass.
        """

        current_url = str(getattr(observation, "url", "unknown"))
        current_title = str(getattr(observation, "title", ""))
        raw_inventory = tuple(getattr(observation, "page_inventory", ()) or ())
        inventory = tuple(
            PageIdentityEvidence(
                int(getattr(item, "index", index)),
                str(getattr(item, "title", "")),
                str(getattr(item, "url", "unknown")),
            )
            for index, item in enumerate(raw_inventory)
        )
        if not inventory:
            inventory = (PageIdentityEvidence(0, current_title, current_url),)

        errors: list[str] = []
        target: PageIdentityEvidence | None = None
        basis: tuple[str, ...] = ()
        target_confirmed = False
        metadata = getattr(observation, "metadata", {})
        runtime_index = metadata.get("RuntimeTabIndex") if isinstance(metadata, Mapping) else None
        if isinstance(runtime_index, bool) or not isinstance(runtime_index, int) or runtime_index < 0:
            runtime_index = None
        try:
            target, basis = identify_cnki_target_page(
                inventory,
                current_url=current_url,
                current_title=current_title,
                current_index=runtime_index,
                route_provenance=route_provenance,
            )
            target_confirmed = True
        except TargetPageIdentityError as exc:
            errors.append(str(exc))

        raw_nodes = tuple(getattr(observation, "target_observations", ()) or ())
        nodes: list[ChallengeNodeEvidence] = []
        for item in raw_nodes:
            render_complete = bool(getattr(item, "inspection_complete", True))
            nodes.append(
                ChallengeNodeEvidence(
                    marker=str(getattr(item, "marker", "unknown")),
                    frame_index=int(getattr(item, "frame_index", 0)),
                    frame_name=str(getattr(item, "frame_name", "")),
                    frame_url=str(getattr(item, "frame_url", "unknown")),
                    playwright_visible=getattr(item, "playwright_visible", None),
                    bounding_box=ChallengeBoundingBox.from_mapping(getattr(item, "bounding_box", None)),
                    client_rect=ChallengeBoundingBox.from_mapping(getattr(item, "client_rect", None)),
                    display=getattr(item, "display", None),
                    visibility=getattr(item, "visibility", None),
                    opacity=getattr(item, "opacity", None),
                    pointer_events=getattr(item, "pointer_events", None),
                    aria_hidden=getattr(item, "aria_hidden", None),
                    client_width=getattr(item, "client_width", None),
                    client_height=getattr(item, "client_height", None),
                    viewport_width=float(getattr(item, "viewport_width", 0)),
                    viewport_height=float(getattr(item, "viewport_height", 0)),
                    frame_viewport_visible=getattr(item, "frame_viewport_visible", None),
                    inspection_complete=render_complete,
                    blocking_overlay=bool(getattr(item, "blocking_overlay", False)),
                )
            )
            if not render_complete:
                errors.append("structured challenge visibility probe was incomplete")

        frames: list[ChallengeFrameEvidence] = []
        frame_indexes = sorted({node.frame_index for node in nodes} or {0})
        for frame_index in frame_indexes:
            frame_nodes = [node for node in nodes if node.frame_index == frame_index]
            frame = frame_nodes[0] if frame_nodes else None
            frames.append(
                ChallengeFrameEvidence(
                    frame_index=frame_index,
                    frame_name=frame.frame_name if frame else "",
                    frame_url=frame.frame_url if frame else current_url,
                    playwright_visible=(
                        any(node.playwright_visible is True for node in frame_nodes)
                        if frame_nodes
                        else None
                    ),
                    viewport_visible=(
                        any(node.frame_viewport_visible is True for node in frame_nodes)
                        if frame_nodes
                        else None
                    ),
                    bounding_box_present=any(node.bounding_box is not None for node in frame_nodes),
                    challenge_node_count=len(frame_nodes),
                )
            )

        inspection_complete = bool(getattr(observation, "inspection_complete", True))
        if not inspection_complete:
            errors.append("browser observation was incomplete")
        return classify_challenge(
            nodes,
            frames=frames,
            page_inventory=inventory,
            target_page_confirmed=target_confirmed,
            target_page_index=target.page_index if target else None,
            target_page_identity_basis=basis,
            route_provenance=route_provenance,
            blocking_evidence=business_flow_blocked,
            inspection_complete=inspection_complete,
            errors=errors,
        )
