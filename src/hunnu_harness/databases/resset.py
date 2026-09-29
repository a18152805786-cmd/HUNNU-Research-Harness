from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urljoin

from ..models import AuthStatus, BrowserState
from .base import DatabaseAdapter, UnexpectedPageState
from .resset_catalog import (
    HUNNU_LIBRARY_ROUTE,
    RESSET_DOWNLOAD_CENTRE_URL,
    RESSET_MAIN_URL,
    RESSET_TASK_LIST_ACTION,
    RESSETQueueError,
    RessetTask,
    TableHit,
    claim_resset_task,
    institutional_session,
    parse_permitted_databases,
    parse_table_hits,
    parse_task_rows,
    parse_total_records,
    pick_table,
    search_url,
    whole_table_problem,
)

RESSET_TABLE_BASE = "https://db.resset.com/db/table/"

# Find one download-centre row's own 下载 button, by the row id that is the
# first argument of its ``downloadtask(id, token, path, this)`` call (with any
# quoting), or by created time and name when the id is not known yet.  The
# button found is marked ``data-harness-row="press"`` so a real mouse click can
# be aimed at it.  Returns how many buttons matched.
_MARK_ROW_BUTTON_JS = r"""([id, created, name]) => {
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const firstArg = (b) => {
    const m = (b.getAttribute('onclick') || '').match(/downloadtask\(\s*['"]?([^,'"]+)['"]?\s*,/);
    return m ? m[1].trim() : null;
  };
  document.querySelectorAll('[data-harness-row]').forEach(b => b.removeAttribute('data-harness-row'));
  let buttons = [...document.querySelectorAll('[onclick*="downloadtask"]')];
  if (id && !id.startsWith('row:')) {
    buttons = buttons.filter((b) => firstArg(b) === id);
  } else {
    buttons = buttons.filter((b) => {
      const tr = b.closest('tr');
      const c = tr ? tr.querySelectorAll('td') : [];
      return c.length >= 8 && norm(c[1].innerText) === norm(created) && norm(c[2].innerText) === norm(name);
    });
  }
  if (buttons.length === 1) buttons[0].setAttribute('data-harness-row', 'press');
  return buttons.length;
}"""
HUNNU_LIBRARY_RESSET_DETAIL_URL = (
    "http://wisdom.chaoxing.com/newwisdom/doordatabase/databasedetail.html?wfwfid=125449&pageId=36761&id=27560"
)


class RESSETGate(RuntimeError):
    """A RESSET page stopped the run; ``human`` means a person acts next."""

    def __init__(self, code: str, message: str, *, human: bool = True):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.human = human


class RESSETAdapter(DatabaseAdapter):
    """RESSET 金融研究数据库 through the HUNNU library's institutional sign-in.

    Whole-table downloads only.  The form is prepared by the harness; the
    four-character CAPTCHA and the 下载数据 press are the person's.  While the
    person acts, the adapter only reads the download-centre list from the same
    page (no navigation, no clicks), and afterwards presses the new row's own
    download button.
    """

    name = "RESSET"
    page_ready_seconds = 20.0
    poll_seconds = 5.0

    def __init__(self, browser: Any):
        super().__init__(browser)
        self.permitted: list[str] = []
        self.total_records: int | None = None
        self._prepared_output_type = ""

    @property
    def page(self) -> Any:
        page = getattr(self.browser, "page", None)
        if page is None:
            raise RuntimeError("Browser is not started")
        return page

    async def _pause(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _frames_text(self) -> str:
        """Text of the page and every frame in it.

        RESSET's main page draws its user block (帐户类别 机构用户, 湖南师范大学)
        inside nested frames; reading only the top document found nothing and
        reported a signed-in session as signed out (found live on 2026-09-29).
        """

        texts: list[str] = []
        for frame in list(getattr(self.page, "frames", []) or []):
            try:
                texts.append(await frame.locator("body").inner_text(timeout=3000))
            except Exception:
                continue
        return "\n".join(texts)

    async def detect(self) -> BrowserState:
        state = await self.browser.state(database=self.name)
        body = state.body_text
        framed = await self._frames_text()
        if framed:
            body = f"{body}\n{framed}"[:60000]
        auth = AuthStatus.AUTH_SUCCESS if institutional_session(body) else AuthStatus.AUTH_UNKNOWN
        return BrowserState(**{**state.__dict__, "body_text": body, "database": self.name, "auth_status": auth})

    async def _signed_in_within(self, seconds: float) -> BrowserState | None:
        deadline = time.monotonic() + seconds
        while True:
            state = await self.detect()
            if state.auth_status == AuthStatus.AUTH_SUCCESS:
                return state
            if time.monotonic() >= deadline:
                return None
            await self._pause(1.0)

    async def _enter_through_library(self) -> None:
        """The HUNNU library's own way in: its RESSET page's 网络地址 link.

        That link opens db.resset.com in a new page already signed in as the
        library's institutional account -- nothing is typed and no sign-in
        form is involved.  The run carries on in that new page.
        """

        await self.page.goto(HUNNU_LIBRARY_RESSET_DETAIL_URL, wait_until="domcontentloaded")
        link = self.page.get_by_text("网络地址", exact=True).first
        await link.wait_for(timeout=int(self.page_ready_seconds * 1000))
        context = self.page.context
        async with context.expect_page(timeout=int(self.page_ready_seconds * 1000)) as opened:
            await link.click()
        new_page = await opened.value
        await new_page.wait_for_load_state("domcontentloaded")
        self.browser.page = new_page

    async def open(self) -> BrowserState:
        """The main page, signed in as the library's institutional account.

        The run works in a tab of its own: the Research Chrome restores earlier
        tabs, and a restored copy of the same table at its defaults is exactly
        where a person once typed the CAPTCHA by mistake.
        """

        self.browser.page = await self.page.context.new_page()
        await self.page.goto(RESSET_MAIN_URL, wait_until="domcontentloaded")
        state = await self._signed_in_within(5.0)
        if state is None:
            await self._enter_through_library()
            state = await self._signed_in_within(self.page_ready_seconds)
        if state is None:
            raise RESSETGate(
                "ACTION_REQUIRED_USER_LOGIN",
                "RESSET did not show the library's institutional account, even through the library's own link. "
                "In the Research Chrome, go " + HUNNU_LIBRARY_ROUTE + " by hand and run again. "
                "Agents never type the shared password the library page publishes.",
            )
        self.permitted = parse_permitted_databases(state.body_text)
        if not self.permitted:
            raise RESSETGate("RESSET_PERMISSIONS_UNREADABLE", "the 权限数据库 list could not be read", human=False)
        return state

    async def download(self, request: Any) -> Any:
        """Not a one-step action here: the CAPTCHA puts a person in the middle.

        Use ``workflows.run_resset_download``, which prepares the form, waits
        for the person, and collects the file.
        """

        raise UnexpectedPageState(
            "UnexpectedPageState: RESSET downloads go through workflows.run_resset_download (a person types the CAPTCHA)"
        )

    async def resolve_table(self, code: str) -> TableHit:
        html = await self.page.evaluate(
            """async (url) => (await fetch(url, {credentials: 'same-origin'})).text()""",
            search_url(code),
        )
        return pick_table(parse_table_hits(html), code, self.permitted)

    async def open_table(self, hit: TableHit) -> BrowserState:
        await self.page.goto(urljoin(RESSET_TABLE_BASE, hit.href), wait_until="domcontentloaded")
        deadline = time.monotonic() + self.page_ready_seconds
        while True:
            state = await self.detect()
            title = state.title or ""
            if hit.code in title.upper() and "数据开始日期" in state.body_text:
                has_form = await self.page.evaluate("() => !!document.myform && !!document.getElementById('verifyCode')")
                if has_form:
                    self.total_records = parse_total_records(state.body_text)
                    return BrowserState(**{**state.__dict__, "module": hit.database, "table": hit.code})
            if time.monotonic() >= deadline:
                raise RESSETGate(
                    "RESSET_TABLE_NOT_CONFIRMED", f"the page never showed table {hit.code} with its download form", human=False
                )
            await self._pause(1.0)

    async def prepare_whole_table(self, output_type: str) -> dict[str, Any]:
        """All periods, all companies, every field, in ``output_type``; read back to confirm."""

        await self.page.check("input[name='timeRange'][value='1']")
        await self.page.check("input[name='cgRadio'][value='c']")
        await self.page.fill("input[name='cValue']", "")
        links = self.page.locator("a", has_text="全选")
        for index in range(await links.count()):
            await links.nth(index).click()
        # 更新时间 and 观测序号 sit outside every 全选 group (found live on
        # EMPINFO: 19 of 21 fields after the group links); tick what is left.
        boxes = self.page.locator("input[name='varCheck']")
        for index in range(await boxes.count()):
            box = boxes.nth(index)
            if not await box.is_checked():
                await box.check()
        await self.page.check(f"input[name='outputType'][value='{output_type}']")
        self._prepared_output_type = output_type
        zip_radio = self.page.locator("input[name='zipType'][value='zip']")
        if await zip_radio.count():
            await zip_radio.first.check()
        state = await self.page.evaluate(
            """() => {
                 const f = document.myform;
                 const pick = (name) => { const el = [...f.querySelectorAll(`input[name='${name}']`)].find(e => e.checked); return el ? el.value : null; };
                 const vars = [...f.querySelectorAll("input[name='varCheck']")];
                 return {
                   timeRange: pick('timeRange'), cgRadio: pick('cgRadio'), cValue: (f.cValue ? f.cValue.value : ''),
                   outputType: pick('outputType'), zipType: pick('zipType'),
                   fieldsChecked: vars.filter(e => e.checked).length, fieldsTotal: vars.length,
                 };
               }"""
        )
        problems = []
        if state.get("timeRange") != "1":
            problems.append("period is not 时间不限")
        if state.get("cgRadio") != "c" or (state.get("cValue") or "").strip():
            problems.append("a company filter is set")
        if state.get("outputType") != output_type:
            problems.append(f"format is {state.get('outputType')}, not {output_type}")
        if not state.get("fieldsTotal") or state.get("fieldsChecked") != state.get("fieldsTotal"):
            problems.append(f"{state.get('fieldsChecked')}/{state.get('fieldsTotal')} fields selected")
        if problems:
            raise UnexpectedPageState("UnexpectedPageState: the RESSET form is not a whole-table request: " + "; ".join(problems))
        return state

    async def present_for_person(self) -> str:
        """Bring the prepared page to the front and scroll to its CAPTCHA.

        The Research Chrome restores earlier tabs, and one of them may be the
        same table at its defaults; the person must type the CAPTCHA in the
        page that was prepared, so that page is put in front of them.
        """

        await self.page.bring_to_front()
        try:
            await self.page.locator("#verifyCode").scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        return await self.page.title()

    async def list_tasks(self) -> list[RessetTask]:
        """This browser's download-centre rows, read without leaving the page."""

        html = await self.page.evaluate(
            """async (action) => {
                 let id = null;
                 try { id = localStorage.getItem('browserId'); } catch (e) {}
                 const body = new URLSearchParams({orderStr: 'createTime', orderType: 'desc', dbMsgId: '', tableName: '',
                                                   parentId: '', id: '', browserId: id || '', ts: String(Date.now())});
                 const r = await fetch(action, {method: 'POST', credentials: 'same-origin',
                                               headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}, body});
                 return await r.text();
               }""",
            RESSET_TASK_LIST_ACTION,
        )
        # parse_task_rows drops download tokens and paths on this first read.
        return parse_task_rows(html)

    async def wait_for_person_and_task(
        self, before: list[RessetTask], hit: TableHit, *, timeout_seconds: float
    ) -> RessetTask:
        """Wait while a person types the CAPTCHA and presses 下载数据; claim the new row."""

        deadline = time.monotonic() + timeout_seconds
        task: RessetTask | None = None
        while True:
            task = claim_resset_task(before, await self.list_tasks(), hit.code, hit.title)
            if task is not None and task.finished:
                problem = whole_table_problem(task, self.total_records, self._prepared_output_type)
                if problem:
                    raise RESSETQueueError(
                        "RESSET_NOT_WHOLE_TABLE",
                        f"the submitted {hit.code} task is not the prepared whole-table request: {problem}. "
                        "It was probably submitted from another tab; it is not archived. Run again and type the "
                        "验证码 in the page the harness brings to the front",
                    )
                return task
            if time.monotonic() >= deadline:
                if task is None:
                    raise RESSETGate(
                        "ACTION_REQUIRED_USER_CAPTCHA",
                        f"nobody submitted the {hit.code} form within {int(timeout_seconds)} s. In the Research Chrome's "
                        f"{hit.title} page, type the 4-character 验证码 and press 下载数据, then run again",
                    )
                raise RESSETQueueError(
                    "RESSET_TASK_NOT_FINISHED",
                    f"the {hit.code} task was created at {task.created} but its file is not ready yet ({task.status}); "
                    "collect it later with --collect-task latest once the download centre shows it ready",
                )
            await self._pause(self.poll_seconds)

    async def find_task(self, task_id: str, hit: TableHit, output_type: str = "") -> RessetTask:
        """A row to collect: by its id, or ``latest`` for this table's newest ready row.

        ``latest`` is safe here because the download centre lists only this
        browser's own tasks.  The row must still be the whole table in the
        requested format (checked against the table page's 总记录数, which
        ``open_table`` reads), exactly as a freshly claimed row is.
        """

        wanted = str(task_id).strip()
        tasks = await self.list_tasks()
        chosen: RessetTask | None = None
        if wanted.casefold() == "latest":
            mine = [t for t in tasks if t.finished and (hit.code in t.name.upper() or hit.title in t.name)]
            if not mine:
                raise RESSETQueueError("RESSET_TASK_NOT_FOUND", f"no ready {hit.code} row in this browser's download centre")
            chosen = max(mine, key=lambda t: t.created)
        else:
            for task in tasks:
                if task.task_id == wanted:
                    if hit.code not in task.name.upper() and hit.title not in task.name:
                        raise RESSETQueueError("RESSET_TASK_TABLE_MISMATCH", f"task {wanted} is {task.name!r}, not {hit.code}")
                    if not task.finished:
                        raise RESSETQueueError("RESSET_TASK_NOT_FINISHED", f"task {wanted} is not ready ({task.status})")
                    chosen = task
                    break
        if chosen is None:
            raise RESSETQueueError("RESSET_TASK_NOT_FOUND", f"task {wanted} is not in this browser's download centre")
        problem = whole_table_problem(chosen, self.total_records, output_type or chosen.fmt)
        if problem:
            raise RESSETQueueError("RESSET_NOT_WHOLE_TABLE", f"task {chosen.task_id} is not the whole table: {problem}")
        return chosen

    async def fetch_task_file(self, task: RessetTask, *, start_seconds: float = 60.0) -> None:
        """Open the download centre, press this row's own 下载 button, and see a download begin.

        The page fills its list by polling every three seconds, and its cell
        text can differ from the list endpoint's in spacing, so the button is
        found by the row id its own ``downloadtask(id, ...)`` call carries.

        The button starts the download from a hidden iframe
        (``/verifyDownload?token=...``).  It is pressed with a real mouse
        click, as a person presses it: on 2026-09-29 the PURANDSALE row, the
        second RESSET file of the day, was pressed by a script ``click()`` and
        no download began.  If none begins within ``start_seconds`` the run
        says so at once instead of waiting out the download timeout.
        """

        dialogs: list[str] = []

        def on_dialog(dialog: Any) -> None:
            # The button reports a missing path or a failed record with alert().
            dialogs.append(f"{getattr(dialog, 'type', '')}: {getattr(dialog, 'message', '')}"[:200])
            asyncio.ensure_future(dialog.dismiss())

        self.page.on("dialog", on_dialog)
        await self.page.goto(RESSET_DOWNLOAD_CENTRE_URL, wait_until="domcontentloaded")
        events_before = len(getattr(self.browser, "download_events", None) or [])
        deadline = time.monotonic() + max(self.page_ready_seconds, 30.0)
        pressed = False
        while not pressed:
            # The list is drawn in a frame (downloadTaskUser.action?browserId=...)
            # inside the download-centre page; the top document holds no rows
            # (found live on 2026-09-29).  Every frame is searched.
            found = 0
            for frame in list(self.page.frames):
                try:
                    # A row's button can be drawn before the frame's own script
                    # defines downloadtask(); pressed then, it does nothing.  The
                    # PURANDSALE collection of 2026-09-29 pressed that early twice
                    # and no download began; pressed later, the same row worked.
                    if not await frame.evaluate(
                        "() => typeof downloadtask === 'function' && typeof jQuery !== 'undefined'"
                    ):
                        continue
                    count = await frame.evaluate(_MARK_ROW_BUTTON_JS, [task.task_id, task.created, task.name])
                except Exception:
                    continue
                found += count if isinstance(count, int) else 0
                if count == 1:
                    try:
                        await self._pause(1.0)
                        await frame.locator("[data-harness-row='press']").first.click(timeout=5000)
                        pressed = True
                    except Exception:
                        pass  # the list redrew under the click; find the row again
                    break
            if pressed:
                break
            if time.monotonic() >= deadline:
                raise RESSETQueueError(
                    "RESSET_DOWNLOAD_ROW_NOT_UNIQUE",
                    f"expected one ready download-centre row for {task.name!r} at {task.created}, found {found}",
                )
            await self._pause(1.0)
        if getattr(self.browser, "download_events", None) is None:
            return  # a browser that reports no download events: the file wait decides
        started = time.monotonic() + start_seconds
        while time.monotonic() < started:
            new = self.browser.download_events[events_before:]
            if any(event.get("state") == "begin" for event in new):
                return
            await self._pause(1.0)
        said = f" The page said: {'; '.join(dialogs)}." if dialogs else ""
        raise RESSETQueueError(
            "RESSET_DOWNLOAD_NOT_STARTED",
            f"the 下载 button of {task.name!r} ({task.created}) was pressed but the browser began no download in "
            f"{int(start_seconds)} s.{said} Look at the Research Chrome's download-centre tab, then collect it with "
            f"--collect-task {task.task_id}",
        )
