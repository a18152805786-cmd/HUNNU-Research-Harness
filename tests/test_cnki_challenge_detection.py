from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from hunnu_harness.browser.commands import BrowserPageSummary
from hunnu_harness.literature.adapters.base import SourceActionRequired, SourceLayoutChanged
from hunnu_harness.literature.adapters.cnki import CNKIAdapter
from hunnu_harness.literature.cnki_challenge import (
    CNKIChallengeDetector,
    ChallengeBoundingBox,
    ChallengeFrameEvidence,
    ChallengeNodeEvidence,
    ChallengeState,
    PageIdentityEvidence,
    TargetPageIdentityError,
    classify_challenge,
    identify_cnki_target_page,
)
from hunnu_harness.literature.security import scan_text_for_sensitive_leaks
from hunnu_harness.paths import CORE_ROOT, OUTPUT_ROOT, V024_RUN_ROOT, is_within


FIXTURES = Path(__file__).parent / "fixtures" / "literature"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def node(
    *,
    marker: str = "安全验证",
    visible: bool | None = False,
    box: ChallengeBoundingBox | None = None,
    display: str | None = "block",
    visibility: str | None = "visible",
    opacity: str | None = "1",
    pointer_events: str | None = "auto",
    client_width: float | None = 100,
    client_height: float | None = 40,
    frame_index: int = 0,
    frame_visible: bool | None = True,
    complete: bool = True,
    blocking_overlay: bool = False,
) -> ChallengeNodeEvidence:
    return ChallengeNodeEvidence(
        marker=marker,
        frame_index=frame_index,
        frame_name="main" if frame_index == 0 else "challenge-frame",
        frame_url="https://kns.cnki.net/kns8s/search?temporary=removed",
        playwright_visible=visible,
        bounding_box=box,
        display=display,
        visibility=visibility,
        opacity=opacity,
        pointer_events=pointer_events,
        client_width=client_width,
        client_height=client_height,
        viewport_width=1280,
        viewport_height=720,
        frame_viewport_visible=frame_visible,
        inspection_complete=complete,
        blocking_overlay=blocking_overlay,
    )


def visible_node(**overrides) -> ChallengeNodeEvidence:
    values = {
        "visible": True,
        "box": ChallengeBoundingBox(100, 120, 400, 240),
        "client_width": 400,
        "client_height": 240,
    }
    values.update(overrides)
    return node(**values)


class CNKIChallengeStateTests(unittest.TestCase):
    def test_no_challenge_evidence_is_none(self) -> None:
        self.assertEqual(classify_challenge([]).state, ChallengeState.NONE)

    def test_accessibility_node_exists_but_playwright_hidden_is_dormant(self) -> None:
        diagnostic = classify_challenge([node(visible=False, box=None)])
        self.assertEqual(diagnostic.state, ChallengeState.DORMANT)
        self.assertTrue(diagnostic.node_detected)
        self.assertFalse(diagnostic.visible)

    def test_display_none_component_is_dormant(self) -> None:
        diagnostic = classify_challenge([node(visible=True, display="none", box=None)])
        self.assertEqual(diagnostic.state, ChallengeState.DORMANT)

    def test_visibility_hidden_component_is_dormant(self) -> None:
        diagnostic = classify_challenge([node(visible=True, visibility="hidden", box=None)])
        self.assertEqual(diagnostic.state, ChallengeState.DORMANT)

    def test_zero_size_component_is_dormant(self) -> None:
        diagnostic = classify_challenge(
            [node(visible=False, box=ChallengeBoundingBox(0, 0, 0, 0), client_width=0, client_height=0)]
        )
        self.assertEqual(diagnostic.state, ChallengeState.DORMANT)

    def test_playwright_visible_but_far_offscreen_is_dormant(self) -> None:
        evidence = node(visible=True, box=ChallengeBoundingBox(15, -999985, 150, 18))
        diagnostic = classify_challenge([evidence])
        self.assertTrue(evidence.playwright_visible)
        self.assertFalse(evidence.viewport_visible)
        self.assertEqual(diagnostic.state, ChallengeState.DORMANT)

    def test_challenge_inside_hidden_iframe_is_dormant(self) -> None:
        diagnostic = classify_challenge([visible_node(frame_index=1, frame_visible=False)])
        self.assertEqual(diagnostic.state, ChallengeState.DORMANT)

    def test_challenge_inside_visible_iframe_is_visible(self) -> None:
        diagnostic = classify_challenge([visible_node(frame_index=1, frame_visible=True)])
        self.assertEqual(diagnostic.state, ChallengeState.VISIBLE)

    def test_challenge_visible_in_main_frame_is_visible(self) -> None:
        diagnostic = classify_challenge([visible_node()])
        self.assertEqual(diagnostic.state, ChallengeState.VISIBLE)
        self.assertTrue(diagnostic.captcha_detected)

    def test_visible_blocking_overlay_is_blocking(self) -> None:
        diagnostic = classify_challenge([visible_node(blocking_overlay=True)])
        self.assertEqual(diagnostic.state, ChallengeState.BLOCKING)
        self.assertTrue(diagnostic.blocking)

    def test_dormant_component_does_not_trigger_manual_authentication(self) -> None:
        diagnostic = classify_challenge([node(visible=False)])
        CNKIAdapter.enforce_challenge_diagnostic(diagnostic)
        self.assertFalse(diagnostic.action_required_user_login)

    def test_visible_challenge_triggers_manual_authentication(self) -> None:
        diagnostic = classify_challenge([visible_node()])
        with self.assertRaisesRegex(SourceActionRequired, "ACTION_REQUIRED_USER_LOGIN=true"):
            CNKIAdapter.enforce_challenge_diagnostic(diagnostic)

    def test_blocking_challenge_triggers_manual_authentication(self) -> None:
        diagnostic = classify_challenge([node(visible=False)], blocking_evidence=True)
        with self.assertRaisesRegex(SourceActionRequired, "ChallengeState|challenge state"):
            CNKIAdapter.enforce_challenge_diagnostic(diagnostic)

    def test_uncertain_challenge_stops_without_bypass(self) -> None:
        diagnostic = classify_challenge([node(visible=None, complete=False)], inspection_complete=False)
        self.assertEqual(diagnostic.state, ChallengeState.UNCERTAIN)
        self.assertFalse(diagnostic.bypass_attempted)
        with self.assertRaisesRegex(SourceActionRequired, "CaptchaBypassAttempted=false"):
            CNKIAdapter.enforce_challenge_diagnostic(diagnostic)

    def test_uncertain_target_page_stops_before_business_action(self) -> None:
        diagnostic = classify_challenge([], target_page_confirmed=False)
        with self.assertRaisesRegex(SourceLayoutChanged, "TargetPageIdentity=uncertain"):
            CNKIAdapter.enforce_challenge_diagnostic(diagnostic)


class CNKIPageIdentityTests(unittest.TestCase):
    def test_target_page_identity_does_not_depend_on_tab_number(self) -> None:
        pages = (
            PageIdentityEvidence(0, "湖南师范大学图书馆", "https://lib.hunnu.edu.cn/"),
            PageIdentityEvidence(7, "检索-中国知网", "https://kns.cnki.net/kns8s/search?kw=safe"),
        )
        selected, basis = identify_cnki_target_page(
            pages,
            current_url="https://kns.cnki.net/kns8s/search?kw=different-runtime-value",
            current_title="检索-中国知网",
            route_provenance=("HUNNU Official Portal", "HUNNU Library", "CNKI"),
        )
        self.assertEqual(selected.page_index, 7)
        self.assertIn("current-stable-url", basis)
        self.assertIn("navigation-provenance", basis)

    def test_multiple_indistinguishable_cnki_pages_are_rejected(self) -> None:
        pages = (
            PageIdentityEvidence(1, "检索-中国知网", "https://kns.cnki.net/kns8s/search?kw=one"),
            PageIdentityEvidence(2, "检索-中国知网", "https://kns.cnki.net/kns8s/search?kw=two"),
        )
        with self.assertRaises(TargetPageIdentityError):
            identify_cnki_target_page(
                pages,
                current_url="https://kns.cnki.net/kns8s/search?kw=current",
                current_title="检索-中国知网",
            )

    def test_broker_runtime_index_disambiguates_same_sanitized_cnki_identity(self) -> None:
        pages = (
            PageIdentityEvidence(1, "检索-中国知网", "https://kns.cnki.net/kns8s/search?kw=one"),
            PageIdentityEvidence(2, "检索-中国知网", "https://kns.cnki.net/kns8s/search?kw=two"),
        )

        selected, basis = identify_cnki_target_page(
            pages,
            current_url="https://kns.cnki.net/kns8s/search?kw=current",
            current_title="检索-中国知网",
            current_index=2,
        )

        self.assertEqual(selected.page_index, 2)
        self.assertIn("broker-runtime-index-correlated", basis)

    def test_broker_runtime_index_cannot_override_cnki_url_title_identity(self) -> None:
        pages = (
            PageIdentityEvidence(1, "检索-中国知网", "https://kns.cnki.net/kns8s/search?kw=one"),
            PageIdentityEvidence(2, "Springer", "https://link.springer.com/search?query=test"),
        )

        with self.assertRaises(TargetPageIdentityError):
            identify_cnki_target_page(
                pages,
                current_url="https://kns.cnki.net/kns8s/search?kw=current",
                current_title="检索-中国知网",
                current_index=2,
            )

    def test_structured_observation_uses_broker_index_for_duplicate_cnki_tabs(self) -> None:
        observation = SimpleNamespace(
            url="https://kns.cnki.net/kns8s/search?kw=two",
            title="检索-中国知网",
            page_inventory=(
                BrowserPageSummary(1, "检索-中国知网", "https://kns.cnki.net/kns8s/search?kw=one"),
                BrowserPageSummary(2, "检索-中国知网", "https://kns.cnki.net/kns8s/search?kw=two"),
            ),
            target_observations=(),
            inspection_complete=True,
            metadata={"RuntimeTabIndex": 2},
        )

        diagnostic = CNKIChallengeDetector.inspect_observation(observation)

        self.assertTrue(diagnostic.target_page_confirmed)
        self.assertEqual(diagnostic.target_page_index, 2)
        self.assertIn("broker-runtime-index-correlated", diagnostic.target_page_identity_basis)

    def test_route_provenance_is_preserved_in_diagnostic(self) -> None:
        route = ("HUNNU Official Portal", "Library / Database Navigation", "CNKI")
        diagnostic = classify_challenge([node(visible=False)], route_provenance=route)
        self.assertEqual(diagnostic.route_provenance, route)


class CNKIHTMLAndSecurityRegressionTests(unittest.TestCase):
    def test_dormant_component_in_normal_cnki_html_does_not_false_positive(self) -> None:
        records = CNKIAdapter.parse_search_results_html(
            fixture("cnki_dormant_challenge.html"),
            query="人工智能",
            source_url="https://kns.cnki.net/kns8s/search?kw=temporary",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "人工智能治理研究")

    def test_dedicated_security_challenge_page_still_enters_manual_gate(self) -> None:
        with self.assertRaisesRegex(SourceActionRequired, "ACTION_REQUIRED_USER_LOGIN=true"):
            CNKIAdapter.parse_search_results_html(fixture("cnki_security_challenge.html"), query="测试")

    def test_diagnostic_serialization_contains_no_credential_material(self) -> None:
        diagnostic = classify_challenge(
            [node(visible=False)],
            route_provenance=("HUNNU Official Portal", "HUNNU Library", "CNKI"),
        )
        payload = json.dumps(diagnostic.as_dict(), ensure_ascii=False, sort_keys=True)
        self.assertFalse(scan_text_for_sensitive_leaks(payload))

    def test_v024_runtime_root_is_outside_core_and_inside_output(self) -> None:
        self.assertTrue(is_within(V024_RUN_ROOT, OUTPUT_ROOT))
        self.assertFalse(is_within(V024_RUN_ROOT, CORE_ROOT))

    def test_frame_counts_distinguish_detected_and_visible_frames(self) -> None:
        diagnostic = classify_challenge(
            [node(visible=False, frame_index=0), visible_node(frame_index=2)],
            frames=(
                ChallengeFrameEvidence(0, "", "https://kns.cnki.net/", True, True, False, 1),
                ChallengeFrameEvidence(2, "challenge", "about:blank", True, True, True, 1),
            ),
        )
        payload = diagnostic.as_dict()
        self.assertEqual(payload["ChallengeFrameCount"], 2)
        self.assertEqual(payload["ChallengeVisibleFrameCount"], 1)


class _FakeCandidate:
    def __init__(self, evidence: ChallengeNodeEvidence):
        self.evidence = evidence

    async def is_visible(self):
        return self.evidence.playwright_visible

    async def bounding_box(self):
        if not self.evidence.bounding_box:
            return None
        box = self.evidence.bounding_box
        return {"x": box.x, "y": box.y, "width": box.width, "height": box.height}

    async def evaluate(self, _expression):
        box = self.evidence.bounding_box or ChallengeBoundingBox(0, 0, 0, 0)
        return {
            "display": self.evidence.display,
            "visibility": self.evidence.visibility,
            "opacity": self.evidence.opacity,
            "pointerEvents": self.evidence.pointer_events,
            "ariaHidden": self.evidence.aria_hidden,
            "clientWidth": self.evidence.client_width,
            "clientHeight": self.evidence.client_height,
            "rect": {"x": box.x, "y": box.y, "width": box.width, "height": box.height},
        }


class _FakeLocator:
    def __init__(self, candidates):
        self.candidates = list(candidates)

    async def count(self):
        return len(self.candidates)

    def nth(self, index):
        return self.candidates[index]


class _FakeFrameElement:
    def __init__(self, visible, box):
        self.visible = visible
        self.box = box

    async def is_visible(self):
        return self.visible

    async def bounding_box(self):
        if not self.box:
            return None
        return {"x": self.box.x, "y": self.box.y, "width": self.box.width, "height": self.box.height}


class _FakeFrame:
    def __init__(self, *, url, name="", nodes=(), frame_visible=True, frame_box=None):
        self.url = url
        self.name = name
        self.nodes = list(nodes)
        self._frame_visible = frame_visible
        self._frame_box = frame_box

    def get_by_text(self, pattern, exact=False):
        del exact
        return _FakeLocator(
            _FakeCandidate(item) for item in self.nodes if re.search(pattern, item.marker)
        )

    async def frame_element(self):
        return _FakeFrameElement(self._frame_visible, self._frame_box)


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages


class _FakePage(_FakeFrame):
    def __init__(self, *, title, url, nodes=(), child_frames=()):
        super().__init__(url=url, nodes=nodes)
        self._title = title
        self.viewport_size = {"width": 1280, "height": 720}
        self.main_frame = self
        self.frames = [self, *child_frames]
        self.context = _FakeContext([self])

    async def title(self):
        return self._title


class CNKIChallengeCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_collector_traverses_frames_and_keeps_offscreen_component_dormant(self) -> None:
        offscreen = node(
            marker="拖动下方拼图完成验证",
            visible=True,
            box=ChallengeBoundingBox(15, -999985, 150, 18),
        )
        hidden_frame = _FakeFrame(
            url="about:blank",
            name="preloaded-challenge-frame",
            frame_visible=False,
            frame_box=None,
        )
        page = _FakePage(
            title="个性化首页-中国知网",
            url="https://kns.cnki.net/kns8s/?classid=safe",
            nodes=(offscreen,),
            child_frames=(hidden_frame,),
        )
        diagnostic = await CNKIChallengeDetector.inspect_page(
            page,
            route_provenance=("HUNNU Official Portal", "HUNNU Library", "CNKI"),
        )
        self.assertEqual(diagnostic.state, ChallengeState.DORMANT)
        self.assertTrue(diagnostic.target_page_confirmed)
        self.assertEqual(len(diagnostic.frames), 2)
        self.assertFalse(diagnostic.visible)


if __name__ == "__main__":
    unittest.main()
