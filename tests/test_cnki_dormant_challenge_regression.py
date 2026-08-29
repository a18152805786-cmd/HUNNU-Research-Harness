"""Cross-layer regression tests for CNKI dormant challenge classification.

CNKI preloads its verification component far outside the viewport on ordinary
pages, so the same challenge strings appear in the DOM and in accessibility
snapshots while nothing is rendered.  These tests pin the rule that runtime
evidence -- not markup text -- decides whether a human must act, and that no
layer downstream of the detector may re-escalate a dormant component.
"""

import tempfile
import unittest
from pathlib import Path

from hunnu_harness.browser.commands import (
    BrowserActionResult,
    BrowserObservation,
    BrowserPageSummary,
    BrowserTargetObservation,
    ClickCommand,
    DownloadCommand,
    NavigateCommand,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
from hunnu_harness.literature.adapters.base import SourceActionRequired
from hunnu_harness.literature.adapters.cnki import CNKIAdapter
from hunnu_harness.literature.cnki_challenge import (
    CNKIChallengeDetector,
    ChallengeState,
    classify_static_challenge,
    resolve_challenge_state,
)
from hunnu_harness.literature.institutional import HUNNUInstitutionalAccessResolver
from hunnu_harness.literature.models import LiteratureSearchRequest, RunStatus
from hunnu_harness.literature.workflow import LiteratureAcquisitionWorkflow


SEARCH_URL = "https://kns.cnki.net/kns8s/defaultresult/index?korder=TI&kw=test"
DETAIL_URL = "https://kns.cnki.net/kcms2/article/abstract?dbcode=CJFD&filename=DORMANT2026001"
TARGET_TITLE = "数字化转型与企业全要素生产率"

# The verification component CNKI parks outside the document: an ancestor with
# a large negative offset and zero opacity, holding the real challenge strings.
DORMANT_CHALLENGE_MARKUP = """
<div id="nc_1_wrapper" class="nc-container"
     style="position:absolute;top:-1000000px;left:-1000000px;opacity:0">
  <div class="nc_scale">
    <span class="nc-lang-cnt">拖动下方拼图完成验证</span>
    <div class="btn_slide">滑块</div>
  </div>
  <div class="nc_title">安全验证</div>
  <div class="nc_tip">验证码</div>
</div>
"""

DORMANT_SEARCH_HTML = f"""
<html><head><title>检索-中国知网</title></head><body>
  <main>
    <div class="pagerTitleCell">共找到 1 条结果</div>
    <section aria-label="检索结果">
      <a class="fz14" href="/kcms2/article/abstract?dbcode=CJFD&amp;filename=DORMANT2026001">{TARGET_TITLE}</a>
    </section>
  </main>
  {DORMANT_CHALLENGE_MARKUP}
</body></html>
"""

DORMANT_DETAIL_HTML = f"""
<html><head><title>中国知网</title>
  <meta name="citation_title" content="{TARGET_TITLE}">
  <meta name="citation_author" content="张三">
  <meta name="citation_journal_title" content="经济研究">
  <meta name="citation_publication_date" content="2023-05-01">
</head><body>
  <h1>{TARGET_TITLE}</h1>
  <div id="ChDivSummary">数字化转型对企业全要素生产率的影响……</div>
  <p class="keywords">关键词：数字化转型</p>
  <a href="/kcms2/download?filename=DORMANT2026001&dflag=pdfdown">PDF下载</a>
  {DORMANT_CHALLENGE_MARKUP}
</body></html>
"""

# The same challenge strings CNKI leaves in an accessibility tree.
DORMANT_SNAPSHOT = """- generic:
  - text "拖动下方拼图完成验证"
  - text "安全验证"
  - table:
    - columnheader "题名"
    - link "数字化转型与企业全要素生产率" [ref=e1]:
      - /url: /kcms2/article/abstract?dbcode=CJFD&filename=DORMANT2026001
  - contentinfo
"""


def _is_detail(url: str) -> bool:
    return "/kcms2/article/abstract" in url


def parked_probe(marker: str) -> BrowserTargetObservation:
    """Runtime evidence for a component parked far outside the viewport."""

    box = {"x": -1000000.0, "y": -1000000.0, "width": 300.0, "height": 60.0}
    return BrowserTargetObservation(
        marker=marker,
        frame_index=0,
        frame_url="https://kns.cnki.net/",
        playwright_visible=False,
        bounding_box=box,
        client_rect=box,
        display="block",
        visibility="visible",
        opacity="0",
        pointer_events="auto",
        client_width=300.0,
        client_height=60.0,
        viewport_width=1280.0,
        viewport_height=800.0,
        frame_viewport_visible=True,
        inspection_complete=True,
        blocking_overlay=False,
    )


def rendered_probe(marker: str, *, blocking: bool = False) -> BrowserTargetObservation:
    """Runtime evidence for a component actually rendered in the viewport."""

    box = {"x": 420.0, "y": 260.0, "width": 320.0, "height": 180.0}
    return BrowserTargetObservation(
        marker=marker,
        frame_index=0,
        frame_url="https://kns.cnki.net/",
        playwright_visible=True,
        bounding_box=box,
        client_rect=box,
        display="block",
        visibility="visible",
        opacity="1",
        pointer_events="auto",
        client_width=320.0,
        client_height=180.0,
        viewport_width=1280.0,
        viewport_height=800.0,
        frame_viewport_visible=True,
        inspection_complete=True,
        blocking_overlay=blocking,
    )


DORMANT_PROBES = (
    parked_probe("拖动下方拼图完成验证"),
    parked_probe("滑块"),
    parked_probe("安全验证"),
)


class _CNKIBrowser:
    """Fake CNKI command port that serves one page plus visibility probes."""

    navigation_provenance = ("HUNNU Official Portal", "HUNNU Library", "CNKI")

    def __init__(
        self,
        html: str,
        probes: tuple[BrowserTargetObservation, ...],
        *,
        detail_html: str | None = None,
    ) -> None:
        self.html = html
        self.detail_html = detail_html
        self.probes = probes
        self.commands: list[object] = []
        self.current_url = SEARCH_URL
        self.session = SessionHandle("cnki-dormant-regression")
        self.page_handle = PageHandle("main", session=self.session)

    @property
    def interaction_commands(self) -> list[object]:
        return [item for item in self.commands if isinstance(item, (ClickCommand, DownloadCommand))]

    @property
    def challenge_interaction_commands(self) -> list[object]:
        """Interactions aimed at a verification control rather than at content.

        Clicking a result title is ordinary business navigation; clicking a
        slider, puzzle, or CAPTCHA control never is.
        """

        markers = ("拖动", "拼图", "滑块", "验证", "captcha", "slide", "nc_")
        aimed: list[object] = []
        for command in self.interaction_commands:
            target = getattr(command, "target", None)
            haystack = " ".join(
                str(getattr(target, field, "") or "") for field in ("css", "text", "text_regex", "ref")
            ).casefold()
            if any(marker in haystack for marker in markers):
                aimed.append(command)
        return aimed

    async def execute(self, command):
        self.commands.append(command)
        if isinstance(command, NavigateCommand):
            self.current_url = command.url
            return self._observation()
        if isinstance(command, ObserveCommand):
            return self._observation()
        if isinstance(command, ClickCommand):
            # Following the result title reaches the detail page, which carries
            # the same parked verification component.
            self.current_url = DETAIL_URL
            return BrowserActionResult(
                session=self.session,
                page=self.page_handle,
                generation=0,
                action="click",
                url=self.current_url,
            )
        raise AssertionError(f"Unexpected command during a challenge check: {type(command).__name__}")

    @property
    def _current_html(self) -> str:
        if self.detail_html is not None and _is_detail(self.current_url):
            return self.detail_html
        return self.html

    def _observation(self) -> BrowserObservation:
        title = "中国知网" if _is_detail(self.current_url) else "检索-中国知网"
        return BrowserObservation(
            session=self.session,
            page=self.page_handle,
            generation=0,
            url=self.current_url,
            title=title,
            html=self._current_html,
            page_inventory=(BrowserPageSummary(0, title, self.current_url),),
            target_observations=self.probes,
            inspection_complete=True,
        )


def search_request() -> LiteratureSearchRequest:
    return LiteratureSearchRequest(
        original_research_request="CNKI dormant challenge regression",
        exact_titles=(TARGET_TITLE,),
        max_search_results=1,
        max_results_per_source=1,
        max_downloads=0,
        max_downloads_per_run=0,
        require_full_text=False,
    )


class DormantOffscreenChallengeTests(unittest.IsolatedAsyncioTestCase):
    """TEST A -- a parked component is evidence of markup, not of a CAPTCHA."""

    def test_runtime_diagnostic_reports_text_without_visibility(self) -> None:
        browser = _CNKIBrowser(DORMANT_SEARCH_HTML, DORMANT_PROBES)
        diagnostic = CNKIChallengeDetector.inspect_observation(browser._observation())
        payload = diagnostic.as_dict()

        self.assertTrue(payload["ChallengeNodeDetected"], "ChallengeTextPresent must stay true")
        self.assertFalse(payload["ChallengeVisible"])
        self.assertFalse(payload["ChallengeBlocking"])
        self.assertFalse(payload["ChallengeActionable"])
        self.assertEqual(payload["ChallengeState"], ChallengeState.DORMANT.value)
        self.assertFalse(payload["ACTION_REQUIRED_USER_LOGIN"])
        self.assertFalse(payload["CaptchaDetected"])
        self.assertFalse(payload["CaptchaInteractionPerformed"])
        self.assertFalse(payload["CaptchaBypassAttempted"])

    async def test_search_continues_through_the_real_adapter_chain(self) -> None:
        browser = _CNKIBrowser(DORMANT_SEARCH_HTML, DORMANT_PROBES)
        adapter = CNKIAdapter(browser)

        records = await adapter.search(f'"{TARGET_TITLE}"', search_request())

        self.assertEqual([record.title for record in records], [TARGET_TITLE])
        diagnostic = adapter.last_challenge_diagnostic
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic.state, ChallengeState.DORMANT)
        self.assertFalse(diagnostic.action_required_user_login)
        self.assertEqual(browser.interaction_commands, [])

    def test_parked_markup_alone_classifies_as_dormant(self) -> None:
        evidence = CNKIAdapter.static_challenge_evidence(DORMANT_SEARCH_HTML, url=SEARCH_URL)

        self.assertTrue(evidence.text_present)
        self.assertFalse(evidence.on_screen_text_present)
        self.assertFalse(evidence.blocking_overlay)
        self.assertEqual(classify_static_challenge(evidence), ChallengeState.DORMANT)


class VisibleBlockingChallengeTests(unittest.IsolatedAsyncioTestCase):
    """TEST B -- a rendered challenge still stops the run, without interaction."""

    def test_rendered_component_is_visible_and_requires_manual_action(self) -> None:
        diagnostic = CNKIChallengeDetector.inspect_observation(
            _CNKIBrowser(DORMANT_SEARCH_HTML, (rendered_probe("拖动下方拼图完成验证"),))._observation()
        )
        payload = diagnostic.as_dict()

        self.assertTrue(payload["ChallengeVisible"])
        self.assertEqual(payload["ChallengeState"], ChallengeState.VISIBLE.value)
        self.assertTrue(payload["ACTION_REQUIRED_USER_LOGIN"])
        self.assertTrue(payload["CaptchaDetected"])
        self.assertFalse(payload["CaptchaBypassAttempted"])

    def test_blocking_overlay_is_classified_as_blocking(self) -> None:
        diagnostic = CNKIChallengeDetector.inspect_observation(
            _CNKIBrowser(
                DORMANT_SEARCH_HTML, (rendered_probe("安全验证", blocking=True),)
            )._observation()
        )

        self.assertEqual(diagnostic.state, ChallengeState.BLOCKING)
        self.assertTrue(diagnostic.action_required_user_login)

    async def test_adapter_stops_and_performs_no_challenge_interaction(self) -> None:
        browser = _CNKIBrowser(DORMANT_SEARCH_HTML, (rendered_probe("拖动下方拼图完成验证"),))
        adapter = CNKIAdapter(browser)

        with self.assertRaises(SourceActionRequired) as context:
            await adapter.search(f'"{TARGET_TITLE}"', search_request())

        message = str(context.exception)
        self.assertIn("ACTION_REQUIRED_USER_LOGIN=true", message)
        self.assertIn("BrowserReadyForManualAction=true", message)
        self.assertIn("CaptchaBypassAttempted=false", message)
        self.assertEqual(
            browser.interaction_commands,
            [],
            "The harness must never click or drag a challenge control",
        )

    def test_static_blocking_overlay_stops_even_with_business_content(self) -> None:
        html = f"""
        <html><head><title>中国知网</title></head><body>
          <main>学术期刊 检索结果 共找到 12 条结果</main>
          <div class="nc-mask-overlay"
               style="position:fixed;top:0;left:0;z-index:9999;width:1280px;height:800px">
            <div>安全验证</div><div>拖动下方拼图完成验证</div>
          </div>
        </body></html>
        """
        with self.assertRaises(SourceActionRequired):
            CNKIAdapter.detect_interruption(html, url=SEARCH_URL)

    def test_interstitial_challenge_page_stops(self) -> None:
        html = (
            "<html><head><title>安全验证</title></head><body>"
            "<h1>请完成验证</h1><p>拖动下方拼图完成验证</p></body></html>"
        )
        with self.assertRaises(SourceActionRequired):
            CNKIAdapter.detect_interruption(html, url=SEARCH_URL)


class HiddenChallengeTextOnlyTests(unittest.TestCase):
    """TEST C -- a challenge string on its own never triggers manual auth."""

    def test_declared_hidden_challenge_text_does_not_stop_the_run(self) -> None:
        html = f"""
        <html><head><title>检索-中国知网</title></head><body>
          <main><section aria-label="检索结果">学术期刊 检索结果</section></main>
          <div style="display:none">拖动下方拼图完成验证</div>
          <div aria-hidden="true">安全验证</div>
          <p style="visibility:hidden">验证码</p>
        </body></html>
        """
        CNKIAdapter.detect_interruption(html, url=SEARCH_URL)

    def test_parked_challenge_text_on_a_bare_page_does_not_stop_the_run(self) -> None:
        """A page with no business content yet must not be read as a CAPTCHA."""

        html = f"""
        <html><head><title>检索-中国知网</title></head><body>
          <div id="app"></div>{DORMANT_CHALLENGE_MARKUP}
        </body></html>
        """
        CNKIAdapter.detect_interruption(html, url=SEARCH_URL)

    def test_hidden_personal_login_box_does_not_stop_a_working_page(self) -> None:
        """The same rule on the login axis: hidden markup is not a live prompt."""

        html = """
        <html><head><title>检索-中国知网</title></head><body>
          <main><div>共找到 1 条结果</div>
            <a href="/kcms2/article/abstract?dbcode=CJFD&filename=X1">某论文标题</a></main>
          <div class="ecp_personalLoginBox" style="display:none">请登录个人账号使用</div>
        </body></html>
        """
        CNKIAdapter.detect_interruption(html, url=SEARCH_URL)

    def test_rendered_login_prompt_still_stops_the_run(self) -> None:
        html = (
            "<html><head><title>CNKI 登录</title></head><body>"
            "<main>请登录后下载全文</main></body></html>"
        )
        with self.assertRaisesRegex(SourceActionRequired, "manual authentication required"):
            CNKIAdapter.detect_interruption(html, url=SEARCH_URL)

    def test_accessibility_snapshot_text_alone_does_not_stop_the_run(self) -> None:
        records = CNKIAdapter.parse_search_results_snapshot(
            DORMANT_SNAPSHOT,
            query=TARGET_TITLE,
            source_url=SEARCH_URL,
        )
        self.assertEqual([record.title for record in records], [TARGET_TITLE])

    def test_article_title_about_captcha_research_is_not_a_challenge(self) -> None:
        """``og:title``/``citation_title`` carry the article, not the page."""

        html = """
        <html><head><title>中国知网</title>
          <meta name="og:title" content="基于深度学习的验证码识别方法研究">
          <meta name="citation_title" content="基于深度学习的验证码识别方法研究">
          <meta name="citation_author" content="李四">
        </head><body>
          <h1>基于深度学习的验证码识别方法研究</h1>
          <div id="ChDivSummary">本文研究滑块验证码的识别方法。</div>
        </body></html>
        """
        record = CNKIAdapter.parse_article_html(html, source_url=DETAIL_URL)
        self.assertEqual(record.title, "基于深度学习的验证码识别方法研究")

    def test_nested_children_do_not_unpark_their_parked_ancestor(self) -> None:
        """The historic nesting bug: an inner ``</div>`` re-exposed the wrapper."""

        evidence = CNKIAdapter.static_challenge_evidence(
            f"<html><body><main>学术期刊</main>{DORMANT_CHALLENGE_MARKUP}</body></html>",
            url=SEARCH_URL,
        )
        self.assertTrue(evidence.text_present)
        self.assertFalse(evidence.on_screen_text_present)


class _FrameElement:
    """A frame element that is not rendered: Playwright reports no box for it."""

    def __init__(self, *, visible: bool, box: dict | None) -> None:
        self._visible = visible
        self._box = box

    async def is_visible(self) -> bool:
        return self._visible

    async def bounding_box(self):
        return self._box


class _Frame:
    def __init__(self, url: str, element: _FrameElement | None) -> None:
        self.url = url
        self.name = ""
        self._element = element

    async def frame_element(self):
        if self._element is None:
            raise ValueError("Frame.frame_element: Frame has been detached or is the main frame")
        return self._element


class _PageWithFrames:
    """A page whose ``frames[0]`` is a Frame object, as Playwright reports it."""

    def __init__(self, main: _Frame, *others: _Frame) -> None:
        self.main_frame = main
        self.frames = [main, *others]
        self.url = main.url


class UnrenderedFrameCompletenessTests(unittest.IsolatedAsyncioTestCase):
    """A hidden utility iframe is assessed evidence, not a failed inspection.

    Real CNKI pages always carry an ``about:blank`` frame whose bounding box is
    ``None``.  Treating that as an incomplete inspection marked every live
    observation ``UNCERTAIN`` and demanded manual action with no challenge
    evidence at all.
    """

    async def test_main_frame_is_not_probed_as_an_embedded_frame(self) -> None:
        """``frames[0]`` is a Frame, not the Page: identity must use main_frame."""

        from hunnu_harness.browser.local_executor import LocalPlaywrightExecutor

        executor = LocalPlaywrightExecutor.__new__(LocalPlaywrightExecutor)
        main = _Frame("https://kns.cnki.net/kns8s/defaultresult/index", None)
        page = _PageWithFrames(main, _Frame("about:blank", _FrameElement(visible=False, box=None)))

        visible, box, complete = await executor._frame_visibility(page, main, (1280.0, 800.0))

        self.assertIs(visible, True)
        self.assertTrue(complete, "the main frame is always fully assessed")

    async def test_hidden_about_blank_frame_keeps_the_observation_complete(self) -> None:
        from hunnu_harness.browser.local_executor import LocalPlaywrightExecutor

        executor = LocalPlaywrightExecutor.__new__(LocalPlaywrightExecutor)
        page = _Frame("https://kns.cnki.net/kns8s/defaultresult/index", None)
        hidden = _Frame("about:blank", _FrameElement(visible=False, box=None))

        visible, box, complete = await executor._frame_visibility(page, hidden, (1280.0, 800.0))

        self.assertIs(visible, False)
        self.assertIsNone(box)
        self.assertTrue(complete, "a frame proven not rendered is fully assessed")

    async def test_a_frame_claiming_visibility_without_a_box_stays_unresolved(self) -> None:
        from hunnu_harness.browser.local_executor import LocalPlaywrightExecutor

        executor = LocalPlaywrightExecutor.__new__(LocalPlaywrightExecutor)
        page = _Frame("https://kns.cnki.net/kns8s/defaultresult/index", None)
        contradictory = _Frame("https://verify.example/nc", _FrameElement(visible=True, box=None))

        visible, box, complete = await executor._frame_visibility(page, contradictory, (1280.0, 800.0))

        self.assertIsNone(visible)
        self.assertIsNone(box)
        self.assertFalse(complete, "contradictory frame evidence must stay uncertain")

    async def test_a_rendered_challenge_frame_is_still_reported_visible(self) -> None:
        from hunnu_harness.browser.local_executor import LocalPlaywrightExecutor

        executor = LocalPlaywrightExecutor.__new__(LocalPlaywrightExecutor)
        page = _Frame("https://kns.cnki.net/kns8s/defaultresult/index", None)
        rendered = _Frame(
            "https://verify.example/nc",
            _FrameElement(visible=True, box={"x": 300.0, "y": 200.0, "width": 400.0, "height": 260.0}),
        )

        visible, box, complete = await executor._frame_visibility(page, rendered, (1280.0, 800.0))

        self.assertIs(visible, True)
        self.assertIsNotNone(box)
        self.assertTrue(complete)


class CrossLayerChallengeRegressionTests(unittest.IsolatedAsyncioTestCase):
    """TEST D -- no layer after the detector may re-escalate a dormant page."""

    async def test_workflow_run_traverses_search_detail_and_access(self) -> None:
        """search -> open_result -> extract_metadata -> check_fulltext_access."""

        browser = _CNKIBrowser(
            DORMANT_SEARCH_HTML, DORMANT_PROBES, detail_html=DORMANT_DETAIL_HTML
        )
        adapter = CNKIAdapter(browser)
        with tempfile.TemporaryDirectory() as directory:
            workflow = LiteratureAcquisitionWorkflow(
                adapter,
                run_root=Path(directory) / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            )
            result = await workflow.run(search_request())

        self.assertEqual(result.status, RunStatus.SUCCESS)
        self.assertEqual([record.title for record in result.records], [TARGET_TITLE])
        self.assertEqual(adapter.last_challenge_diagnostic.state, ChallengeState.DORMANT)
        self.assertFalse(adapter.last_challenge_diagnostic.action_required_user_login)
        # Every layer saw the detail page and its parked component.
        self.assertTrue(_is_detail(browser.current_url))
        self.assertTrue(any(isinstance(item, ObserveCommand) for item in browser.commands))
        # The run continued into ordinary business navigation and never aimed
        # an interaction at the parked verification control.
        self.assertEqual(browser.challenge_interaction_commands, [])
        self.assertTrue(browser.interaction_commands, "the dormant page must not halt the flow")

    async def test_workflow_still_stops_when_the_challenge_is_rendered(self) -> None:
        browser = _CNKIBrowser(
            DORMANT_SEARCH_HTML,
            (rendered_probe("拖动下方拼图完成验证"),),
            detail_html=DORMANT_DETAIL_HTML,
        )
        adapter = CNKIAdapter(browser)
        with tempfile.TemporaryDirectory() as directory:
            workflow = LiteratureAcquisitionWorkflow(
                adapter,
                run_root=Path(directory) / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
            )
            result = await workflow.run(search_request())

        self.assertEqual(result.status, RunStatus.ACTION_REQUIRED_USER_LOGIN)
        self.assertIn("CaptchaBypassAttempted=false", result.action_required_reason)
        self.assertEqual(browser.challenge_interaction_commands, [])

    def test_every_html_parse_entry_point_honours_a_dormant_verdict(self) -> None:
        """The historic bug: a later HTML re-scan flipped DORMANT back to CAPTCHA."""

        for label, call in (
            (
                "parse_search_results_html",
                lambda: CNKIAdapter.parse_search_results_html(
                    DORMANT_SEARCH_HTML,
                    query=TARGET_TITLE,
                    source_url=SEARCH_URL,
                    challenge_state=ChallengeState.DORMANT,
                ),
            ),
            (
                "parse_article_html",
                lambda: CNKIAdapter.parse_article_html(
                    DORMANT_DETAIL_HTML,
                    source_url=DETAIL_URL,
                    challenge_state=ChallengeState.DORMANT,
                ),
            ),
            (
                "check_fulltext_access_html",
                lambda: CNKIAdapter.check_fulltext_access_html(
                    DORMANT_DETAIL_HTML,
                    source_url=DETAIL_URL,
                    challenge_state=ChallengeState.DORMANT,
                ),
            ),
            (
                "detect_interruption",
                lambda: CNKIAdapter.detect_interruption(
                    DORMANT_DETAIL_HTML,
                    url=DETAIL_URL,
                    challenge_state=ChallengeState.DORMANT,
                ),
            ),
        ):
            with self.subTest(entry_point=label):
                call()

    def test_a_runtime_visible_verdict_outranks_innocent_looking_markup(self) -> None:
        """The single source of truth may not be downgraded either."""

        innocent = (
            "<html><head><title>中国知网</title></head><body>"
            "<main>学术期刊 检索结果</main></body></html>"
        )
        with self.assertRaises(SourceActionRequired):
            CNKIAdapter.detect_interruption(
                innocent, url=SEARCH_URL, challenge_state=ChallengeState.VISIBLE
            )
        self.assertEqual(
            resolve_challenge_state(
                static_state=ChallengeState.NONE, runtime_state=ChallengeState.BLOCKING
            ),
            ChallengeState.BLOCKING,
        )

    def test_institutional_resolver_does_not_re_escalate_a_cnki_page(self) -> None:
        """The resolver's generic keyword gate must not own CNKI challenges."""

        HUNNUInstitutionalAccessResolver.detect_manual_authentication(
            DORMANT_DETAIL_HTML, url=DETAIL_URL
        )
        HUNNUInstitutionalAccessResolver.detect_manual_authentication(
            DORMANT_SEARCH_HTML, url=SEARCH_URL
        )

    def test_resolver_still_gates_non_cnki_captcha_and_login_pages(self) -> None:
        """Compatibility: other sources keep their static authentication gate."""

        springer_captcha = (
            "<html><head><title>Verify</title></head><body>"
            "<p>Please complete the reCAPTCHA to continue.</p></body></html>"
        )
        with self.assertRaises(SourceActionRequired):
            HUNNUInstitutionalAccessResolver.detect_manual_authentication(
                springer_captcha, url="https://link.springer.com/article/10.1007/s11573-023-01162-8"
            )

        hunnu_login = (
            "<html><head><title>统一身份认证</title></head><body>"
            '<form action="/authserver/login"><input type="password" name="pwd"></form>'
            "</body></html>"
        )
        with self.assertRaises(SourceActionRequired):
            HUNNUInstitutionalAccessResolver.detect_manual_authentication(
                hunnu_login, url="https://authserver.hunnu.edu.cn/authserver/login"
            )

    def test_cnki_login_gate_survives_the_challenge_change(self) -> None:
        """A CNKI page that really does require sign-in still stops the run."""

        html = "<html><head><title>CNKI 登录</title></head><body><main>请登录后下载全文</main></body></html>"
        with self.assertRaisesRegex(SourceActionRequired, "manual authentication required"):
            CNKIAdapter.detect_interruption(
                html, url="https://kns.cnki.net/login", challenge_state=ChallengeState.DORMANT
            )


if __name__ == "__main__":
    unittest.main()
