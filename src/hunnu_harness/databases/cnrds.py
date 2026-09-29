from __future__ import annotations

import asyncio
import time
from typing import Any

from ..auth.state import classify_auth_state
from ..models import AuthStatus, BrowserState, DownloadRequest
from .base import DatabaseAdapter, UnexpectedPageState
from .cnrds_catalog import (
    CNRDS_DOWNLOAD_LIST_APIS,
    CNRDS_HOME_URL,
    FORMAT_RADIO_IDS,
    HUNNU_LIBRARY_ROUTE,
    SCHOOL_ACCOUNT_BASE_DATABASES,
    CNRDSQueueError,
    QueuedTask,
    claim_new_task,
    find_task,
    summary_confirms,
    classify_gate,
    cnrds_view_url,
    parse_subscribed_databases,
    queued_tasks,
    view_from_url,
    view_heading_matches,
)


class CNRDSGate(RuntimeError):
    """A CNRDS dialog or page state stopped the run.

    ``human`` is True when a person has to decide what happens next (sign in,
    subscribe, register a personal account, wait out a throttle warning); the
    run never clicks past such a dialog.
    """

    def __init__(self, code: str, message: str, *, human: bool = True):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.human = human


class CNRDSAdapter(DatabaseAdapter):
    name = "CNRDS"
    # v0.1 menu path and tables, kept for the original CNFS acceptance route.
    cnfs_path = ("基础库", "上市公司财务基础数据", "财务报表（CNFS）")
    tables = ("资产负债表", "利润表", "现金流量表")

    # How long the table page, a dialog, and the shared download queue are
    # given before the run reports what it saw instead of waiting on.
    page_ready_seconds = 20.0
    dialog_seconds = 15.0
    queue_poll_seconds = 5.0

    async def detect(self) -> BrowserState:
        state = await self.browser.state(database=self.name)
        detected_auth = classify_auth_state(state)
        return BrowserState(**{**state.__dict__, "database": self.name, "auth_status": detected_auth})

    async def _require_cnrds(self, *, module: str | None = None, table: str | None = None) -> BrowserState:
        state = await self.detect()
        haystack = f"{state.url} {state.title} {state.body_text}".lower()
        if "cnrds" not in haystack and "中国研究数据服务平台" not in haystack:
            raise UnexpectedPageState("UnexpectedPageState: current page is not identified as CNRDS.")
        if state.auth_status in {AuthStatus.AUTH_REQUIRED, AuthStatus.AUTH_IN_PROGRESS, AuthStatus.SESSION_EXPIRED}:
            raise UnexpectedPageState("ManualLoginRequired=true; CNRDS authentication is not complete.")
        return BrowserState(**{**state.__dict__, "module": module or state.module, "table": table or state.table})

    async def open(self) -> BrowserState:
        await self._require_cnrds()
        for label in self.cnfs_path:
            await self.browser.click_text(label)
        return await self._require_cnrds(module="CNFS")

    async def open_table(self, table: str) -> BrowserState:
        if table not in self.tables:
            raise ValueError(f"Unsupported CNRDS CNFS table: {table}")
        await self._require_cnrds(module="CNFS")
        await self.browser.click_text(table)
        return await self._require_cnrds(module="CNFS", table=table)

    async def open_balance_sheet(self) -> BrowserState:
        return await self.open_table("资产负债表")

    async def open_income_statement(self) -> BrowserState:
        return await self.open_table("利润表")

    async def open_cashflow_statement(self) -> BrowserState:
        return await self.open_table("现金流量表")

    async def select_stocks(self, stocks: list[str]) -> BrowserState:
        state = await self._require_cnrds(module="CNFS")
        try:
            await self.browser.select_options_by_label("股票代码", stocks)
        except Exception as exc:
            if len(stocks) != 1:
                raise UnexpectedPageState("CNRDS stock selector is not a native multi-select; selector validation is required before selecting multiple stocks.") from exc
            await self.browser.fill_by_label("股票代码", stocks[0])
        return state

    async def select_date_range(self, start: str, end: str) -> BrowserState:
        state = await self._require_cnrds(module="CNFS")
        await self.browser.fill_by_label("开始日期", start)
        await self.browser.fill_by_label("结束日期", end)
        return state

    async def select_fields(self, fields: list[str]) -> BrowserState:
        state = await self._require_cnrds(module="CNFS")
        for field in fields:
            locator = self.browser.page.get_by_label(field, exact=False).first
            await locator.check()
        return state

    async def preview(self) -> BrowserState:
        state = await self._require_cnrds(module="CNFS")
        await self.browser.click_text("预览")
        return await self._require_cnrds(module="CNFS", table=state.table)

    async def download(self, request: DownloadRequest) -> Any:
        state = await self._require_cnrds(module="CNFS", table=request.table)
        if state.table != request.table:
            raise UnexpectedPageState(f"UnexpectedPageState: expected table={request.table}, observed={state.table}")
        await self.browser.click_text("下载")
        return await self.browser.download_by_text("确认下载")

    # ------------------------------------------------------------------
    # Table-page route (any subscribed base-library table)
    # ------------------------------------------------------------------
    #
    # The site routes every table the same way, ``#/BaseDatabase/DB/<code>/
    # ViewName/<table>``, and every table page carries the same controls with
    # stable ids.  Navigating by that route, then checking the URL, the
    # table heading and the account's own subscription list, replaces the v0.1
    # menu clicks -- which only ever reached CNFS -- with one path for all of
    # them.

    @property
    def page(self) -> Any:
        page = getattr(self.browser, "page", None)
        if page is None:
            raise RuntimeError("Browser is not started")
        return page

    async def _pause(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _require_signed_in(self) -> BrowserState:
        state = await self.detect()
        haystack = f"{state.url} {state.title} {state.body_text}"
        if "cnrds" not in haystack.lower() and "中国研究数据服务平台" not in haystack:
            raise CNRDSGate("CNRDS_NOT_ON_PLATFORM", "the Research Chrome is not showing CNRDS", human=False)
        if "/Home/Login" in state.url or state.auth_status in {
            AuthStatus.AUTH_REQUIRED,
            AuthStatus.AUTH_IN_PROGRESS,
            AuthStatus.SESSION_EXPIRED,
        }:
            raise CNRDSGate(
                "ACTION_REQUIRED_USER_LOGIN",
                "CNRDS is not signed in. In the Research Chrome, go " + HUNNU_LIBRARY_ROUTE
                + "; the 学校登录 click is yours, then run again.",
            )
        return state

    async def subscribed_databases(self) -> dict[str, str]:
        """The account's own list of subscribed databases, read from its personal centre.

        The personal centre renders the list inline.  Elsewhere it lives in a
        dialog that is only filled once opened, so reading it from a table page
        returned an empty list -- and every table was refused as unsubscribed.
        """

        await self.page.goto(f"{CNRDS_HOME_URL}#/personalCenter", wait_until="domcontentloaded")
        deadline = time.monotonic() + self.page_ready_seconds
        while True:
            await self._require_signed_in()
            # The smallest element holding both the heading and expiry dates is
            # the list itself; innerText falls back to textContent when hidden.
            text = await self.page.evaluate(
                """() => {
                     const nodes = [...document.querySelectorAll('.modal, .modal-body, table, div')]
                       .filter(e => (e.textContent || '').includes('已订阅数据库')
                                 && /\\d{4}-\\d{2}-\\d{2}/.test(e.textContent || ''));
                     nodes.sort((a, b) => a.textContent.length - b.textContent.length);
                     return nodes.length ? (nodes[0].innerText || nodes[0].textContent || '') : '';
                   }"""
            )
            subscribed = parse_subscribed_databases(text)
            if subscribed or time.monotonic() >= deadline:
                return subscribed
            await self._pause(1.0)

    async def open_view(self, db: str, view_name: str) -> BrowserState:
        # The account's own subscription list first: a table the account does
        # not hold is refused before its page is even opened.  Both pages are
        # CNRDS's own, so a signed-out session is sent to /Home/Login by CNRDS
        # itself and handed back to the user from there.
        subscribed = await self.subscribed_databases()
        if db not in subscribed:
            raise CNRDSGate(
                "CNRDS_NOT_SUBSCRIBED",
                f"{db} is not in this account's 已订阅数据库 list ({len(subscribed)} databases read); "
                "the school account holds only the base library",
            )
        url = cnrds_view_url(db, view_name)
        await self.page.goto(url, wait_until="domcontentloaded")
        # CNRDS is a single-page app: moving between its pages only changes the
        # hash, so a dialog and its backdrop left open by an earlier run (the
        # download list, say) stay over the page and swallow every click.  A
        # reload gives each run a clean table page.  Found live on 2026-09-28.
        await self.page.reload(wait_until="domcontentloaded")
        deadline = time.monotonic() + self.page_ready_seconds
        db_name_cn = SCHOOL_ACCOUNT_BASE_DATABASES.get(db, (None, None))[0]
        while True:
            state = await self._require_signed_in()
            observed = view_from_url(state.url)
            if (
                observed == (db, view_name)
                and "本表数据开始时间" in state.body_text
                and view_heading_matches(state.body_text, view_name, db_name_cn)
            ):
                break
            if time.monotonic() >= deadline:
                raise CNRDSGate(
                    "CNRDS_TABLE_NOT_CONFIRMED",
                    f"the page never showed table {db}/{view_name} (observed {observed})",
                    human=False,
                )
            await self._pause(1.0)
        return BrowserState(**{**state.__dict__, "database": self.name, "module": db, "table": view_name})

    async def visible_dialogs(self) -> list[str]:
        return list(
            await self.page.evaluate(
                """() => [...document.querySelectorAll('.modal')]
                     .filter(m => m.id && (m.classList.contains('in') || getComputedStyle(m).display === 'block'))
                     .map(m => m.id)"""
            )
            or ()
        )

    async def raise_on_gate(self) -> None:
        for dialog_id in await self.visible_dialogs():
            spec = classify_gate(dialog_id)
            if spec is not None:
                raise CNRDSGate(spec.code, spec.reason, human=spec.human)

    async def _click_first_visible(self, selector: str) -> None:
        locator = self.page.locator(selector)
        count = await locator.count()
        for index in range(count):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                await candidate.click()
                return
        raise UnexpectedPageState(f"UnexpectedPageState: no visible control for {selector}")

    async def _radio_state(self, radio_id: str) -> str:
        """``checked``, ``unchecked``, ``disabled`` or ``missing`` for one of the page's radios."""

        return await self.page.evaluate(
            """(id) => {
                 const e = document.getElementById(id);
                 if (!e) return 'missing';
                 if (e.disabled) return 'disabled';
                 return e.checked ? 'checked' : 'unchecked';
               }""",
            radio_id,
        )

    async def _ensure_radio(self, radio_id: str) -> str:
        """Select a radio only if the page has not already; report its state.

        The radios are opacity-0 inputs behind styled spans, so clicking the
        input itself can wait out the whole action timeout.  The page defaults
        to 时间不限 and 全部代码; pressing only what is not already so, and then
        reading the summary back, is both gentler and checked.
        """

        state = await self._radio_state(radio_id)
        if state == "unchecked":
            await self.page.locator(f"label[for='{radio_id}']").first.click()
            state = await self._radio_state(radio_id)
            if state != "checked":
                raise UnexpectedPageState(f"UnexpectedPageState: could not select {radio_id}")
        return state

    async def choose_whole_table(self, output_format: str) -> dict[str, bool]:
        """All periods, all codes, all fields, in ``output_format``.

        A table without a time or code dimension (公司基本信息 has no date
        field) renders that radio disabled; it is left alone and reported, so
        the summary check knows not to expect it.
        """

        applied: dict[str, bool] = {}
        for radio_id, dimension in (("nolimit", "period"), ("allStockCode", "codes")):
            applied[dimension] = (await self._ensure_radio(radio_id)) == "checked"
        await self.page.get_by_role("button", name="全选").first.click()
        if (await self._ensure_radio(FORMAT_RADIO_IDS[output_format])) != "checked":
            raise UnexpectedPageState(f"UnexpectedPageState: output format {output_format} cannot be selected")
        await self.raise_on_gate()
        return applied

    async def list_queue(self) -> list[QueuedTask]:
        payloads = await self.page.evaluate(
            """async (apis) => Promise.all(apis.map(async (api) => {
                 const response = await fetch(api + '?' + Date.now(), {credentials: 'same-origin'});
                 try { return await response.json(); } catch (e) { return null; }
               }))""",
            list(CNRDS_DOWNLOAD_LIST_APIS),
        )
        # queued_tasks drops the signed URLs on this first read.
        return queued_tasks(*(payloads or ()))

    async def queue_download(
        self, view_name: str, output_format: str = "dta", applied: dict[str, bool] | None = None
    ) -> tuple[set[str], float]:
        """Press 下载, check the summary, add the task to the shared queue.

        The summary is read back first: it must name this table, the whole
        period, all codes and the requested format, or nothing is queued.
        Returns the task ids that existed before the click, and when it was.
        """

        await self._click_first_visible("button.downloadButton")
        deadline = time.monotonic() + self.dialog_seconds
        while True:
            await self.raise_on_gate()
            if "downloadSummaryModal" in await self.visible_dialogs():
                break
            if time.monotonic() >= deadline:
                raise CNRDSGate("CNRDS_SUMMARY_NOT_SHOWN", "the download summary never appeared", human=False)
            await self._pause(0.5)
        summary = await self.page.locator("#downloadSummaryModal").inner_text()
        applied = applied or {}
        confirmed, what = summary_confirms(
            summary,
            view_name,
            output_format,
            require_period=applied.get("period", True),
            require_codes=applied.get("codes", True),
        )
        if not confirmed:
            raise CNRDSGate(
                "CNRDS_SUMMARY_MISMATCH",
                f"the download summary does not confirm the {what} for {view_name!r} ({output_format}); nothing was queued",
                human=False,
            )
        before = {task.task_id for task in await self.list_queue()}
        clicked_at = time.time()
        await self._click_first_visible("#downloadSummaryModal button.download_button")
        await self._pause(1.0)
        await self.raise_on_gate()
        return before, clicked_at

    async def wait_for_task(
        self, before: set[str], view_name: str, *, timeout_seconds: float, db: str = ""
    ) -> QueuedTask:
        deadline = time.monotonic() + timeout_seconds
        while True:
            await self.raise_on_gate()
            task = claim_new_task(before, await self.list_queue(), view_name, db)
            if task is not None and task.finished:
                return task
            if time.monotonic() >= deadline:
                state = "queued but not finished" if task is not None else "never appeared"
                raise CNRDSQueueError(
                    "CNRDS_QUEUE_TIMEOUT",
                    f"the download task for {view_name!r} {state} within {int(timeout_seconds)} s; "
                    "it stays in this session's 下载列表; collect it later with --collect-task "
                    + (task.task_id if task is not None else "<DownloadTaskId>"),
                )
            await self._pause(self.queue_poll_seconds)

    async def find_finished_task(self, task_id: str, db: str, view_name: str) -> QueuedTask:
        """A task queued earlier (by a run that timed out), to collect without queueing again."""

        return find_task(await self.list_queue(), task_id, db, view_name)

    async def fetch_task_file(self, task: QueuedTask) -> None:
        """Open 下载列表 and press this task's own download icon."""

        # The page may already show the list (it can open itself after a task
        # is queued); pressing its header button then would hit the backdrop.
        if "downList_modal" not in await self.visible_dialogs():
            await self._click_first_visible("button[data-target='#downList_modal']")
        deadline = time.monotonic() + self.dialog_seconds
        while "downList_modal" not in await self.visible_dialogs():
            if time.monotonic() >= deadline:
                raise CNRDSGate("CNRDS_DOWNLOAD_LIST_NOT_SHOWN", "the download list never opened", human=False)
            await self._pause(0.5)
        clicked = await self.page.evaluate(
            """([when, title]) => {
                 const rows = [...document.querySelectorAll('#downList_modal .down_table tbody tr')];
                 const mine = rows.filter(r => {
                   const cells = r.children;
                   if (cells.length < 6) return false;
                   const t = (cells[2].getAttribute('title') || cells[2].innerText || '').trim();
                   return (cells[1].innerText || '').trim() === when && t === title;
                 });
                 if (mine.length !== 1) return mine.length;
                 const icon = mine[0].children[mine[0].children.length - 1].querySelector('i, a, button');
                 if (!icon) return -1;
                 icon.click();
                 return 1;
               }""",
            [task.download_time, task.title],
        )
        if clicked != 1:
            raise CNRDSQueueError(
                "CNRDS_DOWNLOAD_ROW_NOT_UNIQUE",
                f"expected exactly one 下载列表 row for {task.title!r} at {task.download_time}, found {clicked}",
            )
