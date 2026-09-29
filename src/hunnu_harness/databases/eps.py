from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from ..models import AuthStatus, BrowserState
from .base import DatabaseAdapter, UnexpectedPageState
from .eps_catalog import (
    EPS_CUBE_URL,
    EPS_HOME_URL,
    EPS_RECORDS_URL,
    EPS_TASKS_API,
    HUNNU_LIBRARY_ROUTE,
    INSTANT_ROW_LIMIT,
    SUPPORTED_DIMENSIONS,
    EPSQuery,
    EPSRequestError,
    EPSTask,
    EPSTaskError,
    claim_eps_task,
    cube_access,
    cube_title_from_default_task_name,
    institutional_session,
    parse_dimension_counts,
    parse_estimate,
    parse_tasks,
    year_labels,
)

# The page's own account record, reduced to what the run needs (no session id).
_USER_INFO_JS = r"""() => {
  let u = {};
  try { u = JSON.parse(localStorage.getItem('userInfo') || '{}') || {}; } catch (e) { u = {}; }
  return {
    groupName: u.groupName || '', realname: u.realname || '', isValid: u.isValid, endDate: u.endDate || '',
    cubes: (u.metaCubeList || []).map(c => ({cubeId: c.cubeId, cubeNameZh: c.cubeNameZh,
                                             visitAuth: c.visitAuth, downloadAuth: c.downloadAuth})),
  };
}"""

# The query header (行维度 ... 已选择 N ... 固定维度) and the current tree's heading (选择指标).
_HEADER_JS = r"""() => {
  const el = [...document.querySelectorAll('div, section')].find(e => {
    const t = (e.innerText || '').trim();
    return /^行维度/.test(t) && /固定维度/.test(t) && t.length < 400;
  });
  const heading = [...document.querySelectorAll('h3')].map(h => h.innerText.trim()).find(t => /^选择/.test(t)) || '';
  return {header: el ? el.innerText : '', heading};
}"""

# One action on one tree.  scope 'dimension' is the current dimension's tree
# (``.select-view .selecting``); scope 'search' is the result list a search
# opens in a popover under the search box (``.search-tree-list``).  The
# 已选维度 tree sits in ``.select-view .selected`` and is never touched.
# Labels are compared after NFKC and without spaces, as ``label_key`` does.
_TREE_JS = r"""([action, labels, scope]) => {
  const key = (t) => (t || '').normalize('NFKC').replace(/\s+/g, '');
  const visible = (t) => t.getClientRects().length > 0;
  const selector = scope === 'search' ? '.search-tree-list .ant-tree' : '.select-view .selecting .ant-tree';
  const trees = [...document.querySelectorAll(selector)].filter(visible);
  const tree = trees[trees.length - 1];
  if (action === 'popover') return {open: [...document.querySelectorAll('.search-tree-list')].some(visible)};
  if (!tree) return {error: 'no tree', matches: 0, labels: []};
  const nodes = [...tree.querySelectorAll('.ant-tree-treenode')];
  const checkable = nodes.filter(n => n.querySelector('.ant-tree-checkbox'));
  if (action === 'mark') {
    // Name the current tree so a real mouse click can be aimed inside it.
    document.querySelectorAll('[data-harness-tree]').forEach(t => t.removeAttribute('data-harness-tree'));
    tree.setAttribute('data-harness-tree', 'current');
    const closed = nodes.filter(n => n.className.includes('switcher-close'));
    return {collapsed: closed.length, first: closed.length ? closed[0].innerText.trim() : ''};
  }
  if (action === 'opened') {
    // Has the branch labelled labels[0] opened and drawn its children?
    const node = nodes.find(n => key(n.innerText) === key((labels || [])[0]));
    if (!node) return {opened: false};
    const next = node.nextElementSibling;
    const deeper = next && next.querySelectorAll('.ant-tree-indent-unit').length > node.querySelectorAll('.ant-tree-indent-unit').length;
    return {opened: node.className.includes('switcher-open') && !!deeper};
  }
  if (action === 'present') {
    // labels: one list of spellings per wanted member; how many members show.
    const shown = new Set(checkable.map(n => key(n.innerText)));
    return {present: (labels || []).filter(group => group.some(l => shown.has(key(l)))).length};
  }
  const wanted = (labels || []).map(key);
  const hits = checkable.filter(n => wanted.includes(key(n.innerText)));
  if (action === 'tick') {
    if (hits.length !== 1) {
      const offered = (checkable.length ? checkable : nodes).map(n => n.innerText.trim()).filter(Boolean);
      return {matches: hits.length, labels: offered.slice(0, 40)};
    }
    const box = hits[0].querySelector('.ant-tree-checkbox');
    const was = box.classList.contains('ant-tree-checkbox-checked');
    if (!was) box.click();
    return {matches: 1, was};
  }
  if (action === 'checked') {
    return {matches: hits.length, checked: hits.length === 1 && !!hits[0].querySelector('.ant-tree-checkbox-checked')};
  }
  return {error: 'unknown action'};
}"""

# This run's row in the group's download list, read from inside the page (the
# session id stays in the page).  Only the fields the run needs come back.
_TASKS_JS = r"""async ([api, name]) => {
  const m = document.cookie.match(/(?:^|;\s*)sid=([^;]+)/);
  const sid = m ? m[1] : '';
  const q = new URLSearchParams({currentPage: '1', pageSize: '10', taskName: name, cubeId: '', status: '', sid});
  const r = await fetch(api + '?' + q.toString(), {credentials: 'same-origin'});
  const j = await r.json();
  return {list: (j.list || []).map(t => ({taskId: t.taskId, taskName: t.taskName, status: t.status,
                                          statusReason: t.statusReason, dataCount: t.dataCount,
                                          createTime: t.createTime, downloadMode: t.downloadMode}))};
}"""


class EPSGate(RuntimeError):
    """An EPS page stopped the run; ``human`` means a person acts next."""

    def __init__(self, code: str, message: str, *, human: bool = True):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.human = human


class EPSAdapter(DatabaseAdapter):
    """EPS 数据平台 on the campus network: one query per run, 确认提交 by a person.

    The harness opens the cube, ticks the requested indicators, regions and
    years, reads the header's 已选择 counts back, and fills the download
    dialog (a unique task name, the format).  The person checks the dialog
    and presses 确认提交.  The run then only reads its own row in the
    download list and waits for the file the page hands to the browser.
    """

    name = "EPS"
    page_ready_seconds = 30.0
    poll_seconds = 3.0

    def __init__(self, browser: Any):
        super().__init__(browser)
        self.user_info: dict[str, Any] = {}
        self.cube_title = ""
        self.page_cleared = False

    @property
    def page(self) -> Any:
        page = getattr(self.browser, "page", None)
        if page is None:
            raise RuntimeError("Browser is not started")
        return page

    async def _pause(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _read_user_info(self) -> dict[str, Any]:
        try:
            return await self.page.evaluate(_USER_INFO_JS)
        except Exception:
            return {}

    async def detect(self) -> BrowserState:
        state = await self.browser.state(database=self.name)
        info = await self._read_user_info()
        auth = AuthStatus.AUTH_SUCCESS if institutional_session(info) else AuthStatus.AUTH_UNKNOWN
        return BrowserState(**{**state.__dict__, "database": self.name, "auth_status": auth})

    async def open(self) -> BrowserState:
        """The platform, in a tab of the run's own, recognised as 湖南师范大学."""

        self.browser.page = await self.page.context.new_page()
        await self.page.goto(EPS_HOME_URL, wait_until="domcontentloaded")
        deadline = time.monotonic() + self.page_ready_seconds
        while True:
            info = await self._read_user_info()
            if institutional_session(info):
                self.user_info = info
                return await self.detect()
            if time.monotonic() >= deadline:
                raise EPSGate(
                    "ACTION_REQUIRED_CAMPUS_NETWORK",
                    "EPS did not recognise 湖南师范大学. The account is granted by campus IP: connect this computer to "
                    "the campus network, open " + HUNNU_LIBRARY_ROUTE + " once in the Research Chrome, and run again",
                )
            await self._pause(1.0)

    async def download(self, request: Any) -> Any:
        """Not a one-step action: 确认提交 is a person's.  Use ``workflows.run_eps_download``."""

        raise UnexpectedPageState(
            "UnexpectedPageState: EPS downloads go through workflows.run_eps_download (a person presses 确认提交)"
        )

    async def _header(self) -> dict[str, Any]:
        return await self.page.evaluate(_HEADER_JS)

    async def open_cube(self, query: EPSQuery) -> dict[str, int]:
        """The cube's query page, with the account's right to download it and a clean selection."""

        access = cube_access(self.user_info, query.cube_id)
        if access is None or not (access["visit"] and access["download"]):
            raise EPSGate(
                "EPS_CUBE_NOT_PERMITTED",
                f"cube {query.cube_id} is not open for download to the 湖南师范大学 account ({access or 'not listed'})",
                human=False,
            )
        await self.page.goto(EPS_CUBE_URL.format(cube_id=query.cube_id), wait_until="domcontentloaded")
        # The platform is a single-page app; a reload drops any selection an
        # earlier visit left in memory.
        await self.page.reload(wait_until="domcontentloaded")
        deadline = time.monotonic() + self.page_ready_seconds
        while True:
            reading = await self._header()
            counts = parse_dimension_counts(reading.get("header", ""))
            if counts and reading.get("heading"):
                break
            if time.monotonic() >= deadline:
                raise UnexpectedPageState(f"UnexpectedPageState: cube {query.cube_id} never showed its query header")
            await self._pause(1.0)
        unsupported = [name for name in counts if name not in SUPPORTED_DIMENSIONS]
        if unsupported:
            raise EPSRequestError(
                "EPS_DIMENSION_UNSUPPORTED",
                f"cube {query.cube_id} has dimension(s) {', '.join(unsupported)}; the harness fills only "
                f"{', '.join(SUPPORTED_DIMENSIONS)}",
            )
        if "指标" not in counts or "时间" not in counts:
            raise EPSRequestError("EPS_DIMENSION_UNSUPPORTED", f"cube {query.cube_id} shows dimensions {list(counts)}")
        if ("地区" in counts) != bool(query.regions):
            raise EPSRequestError(
                "EPS_REGIONS_MISMATCH",
                f"cube {query.cube_id} {'has' if '地区' in counts else 'has no'} 地区 dimension; "
                f"{'pass --regions' if '地区' in counts else 'drop --regions'}",
            )
        if any(counts.values()):
            raise UnexpectedPageState(f"UnexpectedPageState: cube {query.cube_id} opened with members already ticked: {counts}")
        return counts

    async def _tree(self, action: str, labels: list[Any] | None = None, scope: str = "dimension") -> dict[str, Any]:
        return await self.page.evaluate(_TREE_JS, [action, labels or [], scope])

    async def _tick(self, labels: list[str], what: str, *, scope: str = "dimension", wait_seconds: float = 10.0) -> None:
        """Tick the one checkable node carrying one of ``labels``; confirm it is ticked.

        A search draws its result tree a moment after Enter (the first live
        run read the tree too early and found nothing), so an absent label is
        looked for again until ``wait_seconds`` pass; two matches fail at once.
        """

        deadline = time.monotonic() + wait_seconds
        while True:
            result = await self._tree("tick", labels, scope)
            matches = result.get("matches") or 0
            if matches == 1:
                break
            if matches > 1 or time.monotonic() >= deadline:
                code = "EPS_MEMBER_AMBIGUOUS" if matches > 1 else "EPS_MEMBER_NOT_FOUND"
                offered = "; ".join(result.get("labels") or [])[:600]
                raise EPSRequestError(code, f"{what} {labels[0]!r}: {matches} match(es). The tree offers: {offered}")
            await self._pause(0.5)
        deadline = time.monotonic() + 5.0
        while True:
            # The tree re-renders after each tick: read it again rather than
            # trust the node the click went to.
            state = await self._tree("checked", labels, scope)
            if state.get("checked"):
                return
            if time.monotonic() >= deadline:
                raise UnexpectedPageState(f"UnexpectedPageState: {what} {labels[0]!r} did not stay ticked")
            await self._pause(0.3)

    async def _expect_heading(self, dimension: str) -> None:
        deadline = time.monotonic() + 10.0
        while True:
            if (await self._header()).get("heading") == f"选择{dimension}":
                return
            if time.monotonic() >= deadline:
                raise UnexpectedPageState(f"UnexpectedPageState: the page never offered 选择{dimension}")
            await self._pause(0.5)

    async def _expand_until_shown(self, groups: list[list[str]], *, rounds: int = 12) -> None:
        """Open collapsed branches, one per round, until every wanted member shows.

        Regions sit two levels down (31个省 -> 地方合计 -> 北京 ...).  A real
        click on one caret at a time opens them (2026-09-29); the search
        popover must be closed first (see ``_close_search``).
        """

        for _ in range(rounds):
            if (await self._tree("present", groups)).get("present") == len(groups):
                return
            marked = await self._tree("mark")
            if not marked.get("collapsed"):
                return
            caret = self.page.locator(
                "[data-harness-tree='current'] .ant-tree-treenode-switcher-close .ant-tree-switcher"
            ).first
            await caret.click()
            # A branch loads its children from the server after the click; a
            # second click while it loads would close it again.  So wait for
            # this branch to open, and click nothing meanwhile.
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                await self._pause(0.5)
                if (await self._tree("opened", [marked.get("first", "")])).get("opened"):
                    break
            await self._pause(0.5)

    async def _count(self, dimension: str) -> int:
        return parse_dimension_counts((await self._header()).get("header", "")).get(dimension, 0)

    async def _search(self, text: str) -> None:
        """Search the indicator tree for one label.

        The spaces in a result label (人均 地区生产总值 （元）) are the page's
        highlight around the search word, not part of the name, so the name
        is searched without them.
        """

        box = self.page.locator("input[placeholder='请输入搜索关键词']").first
        await box.fill(re.sub(r"\s+", "", text))
        await box.press("Enter")
        await self._pause(1.5)

    async def _close_search(self) -> None:
        """Empty the search box and close its result popover.

        The popover outlives the dimension it was opened in and lies over the
        next dimension's tree: the third and fourth live runs clicked the
        region tree's caret and hit the stale indicator results instead.
        """

        box = self.page.locator("input[placeholder='请输入搜索关键词']").first
        await box.fill("")
        await box.press("Escape")
        await self.page.locator("h3", has_text="选择").first.click()
        deadline = time.monotonic() + 5.0
        while (await self._tree("popover")).get("open"):
            if time.monotonic() >= deadline:
                raise UnexpectedPageState("UnexpectedPageState: the indicator search popover did not close")
            await self._pause(0.3)

    async def select_query(self, query: EPSQuery, dimensions: dict[str, int]) -> dict[str, int]:
        """Tick every member, dimension by dimension in the page's order; read the counts back."""

        order = list(dimensions)
        for index, dimension in enumerate(order):
            await self._expect_heading(dimension)
            if dimension == "指标":
                for label in query.indicators:
                    await self._search(label)
                    await self._tick([label], "indicator", scope="search")
                await self._close_search()
                wanted = len(query.indicators)
            elif dimension == "地区":
                await self._expand_until_shown([[label] for label in query.regions])
                for label in query.regions:
                    await self._tick([label], "region")
                wanted = len(query.regions)
            else:
                for year in query.years:
                    await self._tick(list(year_labels(year)), "year")
                wanted = len(query.years)
            if await self._count(dimension) != wanted:
                raise UnexpectedPageState(
                    f"UnexpectedPageState: {dimension} shows {await self._count(dimension)} ticked, not {wanted}"
                )
            if index < len(order) - 1:
                await self.page.get_by_role("button", name="下一维度").click()
        final = parse_dimension_counts((await self._header()).get("header", ""))
        return final

    async def open_download_dialog(self, query: EPSQuery, task_name: str) -> dict[str, Any]:
        """数据下载 -> the dialog, with the run's task name and format; read back before the person sees it."""

        await self.page.get_by_role("button", name="数据下载").click()
        dialog = self.page.locator(".ant-modal").filter(has_text="预估数据").first
        try:
            await dialog.wait_for(state="visible", timeout=int(self.page_ready_seconds * 1000))
        except Exception as exc:
            text = ""
            try:
                text = " ".join((await self.page.locator(".ant-modal:visible, .ant-message").all_inner_texts()))
            except Exception:
                pass
            if "无数据" in text or "没有数据" in text or "暂无" in text:
                raise EPSRequestError("EPS_NO_DATA", f"EPS has no data for this query ({text[:120]})") from exc
            raise UnexpectedPageState(f"UnexpectedPageState: the download dialog did not open ({text[:120]})") from exc
        name_box = dialog.locator("input.ant-input").first
        self.cube_title = cube_title_from_default_task_name(await name_box.input_value())
        await name_box.fill(task_name)
        await dialog.locator(".ant-radio-wrapper", has_text=query.output_format).first.click()
        read_back = await dialog.evaluate(
            """(m) => ({
                 name: (m.querySelector('input.ant-input') || {}).value || '',
                 format: ([...m.querySelectorAll('input[type=radio]')].find(i => i.checked) || {}).value || '',
                 text: m.innerText,
               })"""
        )
        estimate = parse_estimate(read_back.get("text", ""))
        problems = []
        if read_back.get("name") != task_name:
            problems.append(f"task name reads {read_back.get('name')!r}")
        if read_back.get("format") != query.output_format:
            problems.append(f"format reads {read_back.get('format')!r}")
        if estimate is None:
            problems.append("no 预估数据 in the dialog")
        if problems:
            raise UnexpectedPageState("UnexpectedPageState: the download dialog is not as prepared: " + "; ".join(problems))
        if estimate >= INSTANT_ROW_LIMIT:
            raise EPSRequestError(
                "EPS_REQUEST_TOO_LARGE",
                f"the dialog estimates {estimate:,} rows; EPS queues {INSTANT_ROW_LIMIT:,} or more. Split the query",
            )
        if estimate != query.rows:
            # The estimate is one row per ticked indicator x region x year
            # (868 = 2 x 31 x 14 live); anything else means a tick went astray.
            raise UnexpectedPageState(
                f"UnexpectedPageState: the dialog estimates {estimate:,} rows but the query asks for {query.rows:,} "
                f"({len(query.indicators)} indicators x {max(1, len(query.regions))} regions x {len(query.years)} years)"
            )
        return {"estimate": estimate, "cube_title": self.cube_title, "task_name": task_name}

    async def present_for_person(self) -> str:
        await self.page.bring_to_front()
        return await self.page.title()

    async def list_tasks(self, task_name: str) -> list[EPSTask]:
        return parse_tasks(await self.page.evaluate(_TASKS_JS, [EPS_TASKS_API, task_name]))

    async def _page_is_ours(self) -> bool:
        url = str(getattr(self.page, "url", "") or "")
        return url.startswith("https://olap.epsnet.com.cn/")

    async def wait_for_person_and_task(self, task_name: str, *, timeout_seconds: float) -> EPSTask:
        """Wait while a person presses 确认提交; follow the run's own row to COMPLETED.

        Only the list is read; nothing on the person's page is clicked.  If
        the page leaves the platform (it was cleared to about:blank after
        automated presses), the row is read from a fresh records page.
        """

        deadline = time.monotonic() + timeout_seconds
        task: EPSTask | None = None
        while True:
            if not await self._page_is_ours():
                self.page_cleared = True
                self.browser.page = await self.page.context.new_page()
                await self.page.goto(EPS_RECORDS_URL, wait_until="domcontentloaded")
                await self._pause(2.0)
            try:
                task = claim_eps_task(await self.list_tasks(task_name), task_name) or task
            except EPSTaskError:
                raise
            except Exception:
                pass  # a list that could not be read this time is "not yet"
            if task is not None and task.completed:
                return task
            if task is not None and task.invalid:
                cleared = " and the page was cleared" if self.page_cleared else ""
                raise EPSTaskError(
                    "EPS_TASK_INVALID",
                    f"EPS ended {task_name!r} INVALID ({task.reason or 'no reason given'}){cleared}. The harness does "
                    "not retry or work around it. Download this query by hand in your own browser, or run again later",
                )
            if time.monotonic() >= deadline:
                if task is None:
                    raise EPSGate(
                        "ACTION_REQUIRED_USER_SUBMIT",
                        f"nobody pressed 确认提交 for {task_name!r} within {int(timeout_seconds)} s. In the Research "
                        "Chrome's EPS page, check the 下载 dialog and press 确认提交, then run again",
                    )
                raise EPSTaskError(
                    "EPS_TASK_NOT_FINISHED", f"{task_name!r} is {task.status} ({task.reason}) after {int(timeout_seconds)} s"
                )
            await self._pause(self.poll_seconds)
