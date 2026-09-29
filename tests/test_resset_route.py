"""RESSET through the HUNNU library: whole tables, a person at the CAPTCHA.

The live facts these tests encode were read on 2026-09-29; see
docs/INSTITUTIONAL_DATA_ACCESS.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.databases.resset_catalog import (
    RESSETQueueError,
    RESSETRequestError,
    RessetTask,
    TableHit,
    claim_resset_task,
    institutional_session,
    parse_permitted_databases,
    parse_table_hits,
    parse_task_rows,
    pick_table,
    validate_resset_request,
)
from hunnu_harness.downloads.manager import DownloadManager
from hunnu_harness.models import DownloadRequest
from hunnu_harness.workflows import run_resset_download

MAIN_PAGE_TEXT = (
    "用户信息\n登录名: hunnu 姓名: 湖南师范大学图书馆\n企业/学校: 湖南师范大学-图书馆 帐户性质: 正式用户\n"
    "帐户类别: 机构用户 过期日期: 2027-06-30\n"
    "权限数据库 (共16个库)\n股票 \t科创板 \t新三板 \n债券 \t基金 \t研究报告 \n融资融券 \t宏观统计 \t行业统计 \n"
    "金融统计 \t外汇 \t期货 \n黄金 \t常量 \tRESSET 沪深基础版分笔高频数据样例 \nRESSET 期权高频数据样例 \t\t\n\n"
    "→ 点击查看详细数据库权限\n"
)

SEARCH_HTML = """
<ul>
<li><a href="../download/dataSearch.jsp?dlm=113&tableName=EMPINFO&dbMsgId=4028818a2206ecbc012206edd0d10001">
 1.  RESSET 股票  -  员工构成信息 ( EMPINFO )</a></li>
<li><a href="../download/dataSearch.jsp?dlm=9&tableName=STIBSTAFFNUM&dbMsgId=ResSTIB2020">
 2.  RESSET 科创板  -  科创板员工构成 ( STIBSTAFFNUM )</a></li>
<li><a href="../download/dataSearch.jsp?dlm=7&tableName=LCSCSUP&dbMsgId=ResSC">
 3.  RESSET 上市公司供应链  -  主要供应商及采购额 ( LCSCSUP )</a></li>
<li><a href="javascript:void(0)">表字段 ( 3 )</a></li>
</ul>
"""

TOKEN = "SECRET-TOKEN-abc"


def centre_html(*rows: tuple[str, str, str, str, bool]) -> str:
    body = []
    for index, (created, name, status, task_id, ready) in enumerate(rows, start=1):
        button = (
            f"<button class='btn btn-primary' onclick=\"downloadtask('{task_id}','{TOKEN}','/data/{task_id}.zip',this)\">下载</button>"
            if ready
            else "<span>--</span>"
        )
        body.append(
            f"<tr class='tableShow'><td>{index}</td><td>{created}</td><td>{name}</td><td><span>{status}</span></td>"
            f"<td>stata17</td><td>12.5</td><td>939736</td><td>{button}</td></tr>"
        )
    return "<table class='listTable'><tr class='tableTitle'><td>序号</td></tr>" + "".join(body) + "</table>"


class CatalogTests(unittest.TestCase):
    def test_the_institutional_session_is_recognised_from_the_user_block(self) -> None:
        self.assertTrue(institutional_session(MAIN_PAGE_TEXT))
        self.assertFalse(institutional_session("用户名 密码 登录"))

    def test_permitted_databases_are_read_although_the_subscribed_marks_are_images(self) -> None:
        names = parse_permitted_databases(MAIN_PAGE_TEXT)
        self.assertEqual(len(names), 16)
        self.assertIn("股票", names)
        self.assertIn("RESSET 期权高频数据样例", names)

    def test_a_permission_list_that_does_not_match_its_stated_count_is_not_trusted(self) -> None:
        self.assertEqual(parse_permitted_databases(MAIN_PAGE_TEXT.replace("共16个库", "共17个库")), [])

    def test_search_results_become_table_hits(self) -> None:
        hits = parse_table_hits(SEARCH_HTML)
        self.assertEqual([hit.code for hit in hits], ["EMPINFO", "STIBSTAFFNUM", "LCSCSUP"])
        self.assertEqual(hits[0].database, "RESSET 股票")
        self.assertEqual(hits[0].title, "员工构成信息")

    def test_a_table_is_picked_only_inside_a_permitted_database(self) -> None:
        hits = parse_table_hits(SEARCH_HTML)
        permitted = parse_permitted_databases(MAIN_PAGE_TEXT)
        self.assertEqual(pick_table(hits, "empinfo", permitted).code, "EMPINFO")
        with self.assertRaises(RESSETRequestError) as caught:
            pick_table(hits, "LCSCSUP", permitted)
        self.assertEqual(caught.exception.code, "RESSET_NOT_SUBSCRIBED")
        with self.assertRaises(RESSETRequestError) as caught:
            pick_table(hits, "NOPE", permitted)
        self.assertEqual(caught.exception.code, "RESSET_TABLE_NOT_FOUND")

    def test_requests_are_whole_tables_in_a_known_format(self) -> None:
        self.assertEqual(
            validate_resset_request(DownloadRequest(database="resset", module="", table=" empinfo ", output_format="DTA")),
            ("EMPINFO", "stata17"),
        )
        for extra in ({"stocks": ("000001",)}, {"fields": ("EmpNum",)}, {"date_start": "2010"}):
            with self.subTest(extra=extra), self.assertRaises(RESSETRequestError) as caught:
                validate_resset_request(DownloadRequest(database="RESSET", module="", table="EMPINFO", **extra))
            self.assertEqual(caught.exception.code, "RESSET_SUBSET_UNSUPPORTED")
        with self.assertRaises(RESSETRequestError):
            validate_resset_request(DownloadRequest(database="RESSET", module="", table="EMPINFO", output_format="sas"))


class DownloadCentreTests(unittest.TestCase):
    def test_download_tokens_and_paths_are_dropped_on_first_read(self) -> None:
        tasks = parse_task_rows(centre_html(("2026-09-29 07:05:01", "员工构成信息 EMPINFO", "已完成", "T7", True)))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task_id, "T7")
        self.assertTrue(tasks[0].finished)
        text = json.dumps([task.as_dict() for task in tasks], ensure_ascii=False)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(".zip", text)

    def test_a_row_still_being_generated_is_not_finished(self) -> None:
        tasks = parse_task_rows(centre_html(("2026-09-29 07:05:01", "员工构成信息 EMPINFO", "生成中", "", False)))
        self.assertFalse(tasks[0].finished)
        self.assertFalse(RessetTask.status_ready("未完成"))

    def test_only_the_new_row_for_this_table_is_claimed(self) -> None:
        before = parse_task_rows(centre_html(("2026-09-28 10:00:00", "员工构成信息 EMPINFO", "已完成", "T1", True)))
        after = parse_task_rows(
            centre_html(
                ("2026-09-29 07:05:01", "员工构成信息 EMPINFO", "已完成", "T2", True),
                ("2026-09-29 07:05:30", "采销情况 PURANDSALE", "已完成", "T3", True),
                ("2026-09-28 10:00:00", "员工构成信息 EMPINFO", "已完成", "T1", True),
            )
        )
        self.assertEqual(claim_resset_task(before, after, "EMPINFO", "员工构成信息").task_id, "T2")
        self.assertIsNone(claim_resset_task(after, after, "EMPINFO", "员工构成信息"))

    def test_two_new_rows_for_the_same_table_are_ambiguous(self) -> None:
        after = parse_task_rows(
            centre_html(
                ("2026-09-29 07:05:01", "员工构成信息 EMPINFO", "已完成", "T2", True),
                ("2026-09-29 07:06:01", "员工构成信息 EMPINFO", "已完成", "T4", True),
            )
        )
        with self.assertRaises(RESSETQueueError):
            claim_resset_task([], after, "EMPINFO", "员工构成信息")


class FakeResset:
    def __init__(self, drop_dir: Path) -> None:
        self.drop_dir = drop_dir
        self.calls: list[str] = []
        self.hit = TableHit("RESSET 股票", "员工构成信息", "EMPINFO", "../download/dataSearch.jsp?dlm=113&tableName=EMPINFO&dbMsgId=x")

    async def open(self) -> None:
        self.calls.append("open")

    async def resolve_table(self, code: str) -> TableHit:
        self.calls.append(f"resolve:{code}")
        return self.hit

    async def open_table(self, hit: TableHit) -> None:
        self.calls.append(f"open_table:{hit.code}")

    async def prepare_whole_table(self, output_type: str) -> dict:
        self.calls.append(f"prepare:{output_type}")
        return {}

    async def list_tasks(self) -> list:
        self.calls.append("list")
        return []

    async def wait_for_person_and_task(self, before, hit, *, timeout_seconds):
        self.calls.append("wait_person")
        return RessetTask("T2", "员工构成信息 EMPINFO", "2026-09-29 07:05:01", "已完成", "stata17", "12.5", "939736")

    async def find_task(self, task_id, hit, output_type=""):
        self.calls.append(f"find:{task_id}")
        return RessetTask(task_id, "员工构成信息 EMPINFO", "2026-09-29 07:05:01", "已完成", "stata17", "12.5", "939736")

    async def fetch_task_file(self, task) -> None:
        self.calls.append(f"fetch:{task.task_id}")
        (self.drop_dir / "EMPINFO.zip").write_bytes(b"PK\x03\x04 fake")


class WorkflowTests(unittest.TestCase):
    def run_flow(self, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        staging = root / "staging"
        staging.mkdir()
        adapter = FakeResset(staging)
        manager = DownloadManager([staging], root / "archive", root / "manifests")
        announced = []
        record = asyncio.run(
            run_resset_download(
                adapter,
                manager,
                DownloadRequest(database="RESSET", module="", table="EMPINFO", output_format="dta"),
                on_ready=lambda hit: announced.append(hit.code),
                **kwargs,
            )
        )
        return adapter, record, announced

    def test_the_person_is_told_to_act_only_after_the_form_is_prepared(self) -> None:
        adapter, record, announced = self.run_flow()
        self.assertEqual(
            adapter.calls,
            ["open", "resolve:EMPINFO", "open_table:EMPINFO", "prepare:stata17", "list", "wait_person", "fetch:T2"],
        )
        self.assertEqual(announced, ["EMPINFO"])
        self.assertEqual((record.database, record.module, record.table), ("RESSET", "RESSET 股票", "EMPINFO"))
        self.assertTrue(record.source_url.startswith("https://db.resset.com/db/download/dataSearch.jsp"))
        self.assertNotIn(TOKEN, record.source_url)

    def test_collecting_an_earlier_task_needs_no_captcha(self) -> None:
        adapter, _record, announced = self.run_flow(collect_task_id="T9")
        self.assertEqual(adapter.calls, ["open", "resolve:EMPINFO", "open_table:EMPINFO", "find:T9", "fetch:T9"])
        self.assertEqual(announced, [])


class CliTests(unittest.TestCase):
    def test_dry_run_validates_a_resset_table_without_a_browser(self) -> None:
        from hunnu_harness.cli import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["data-acquire", "--database", "RESSET", "--table", "EMPINFO", "--dry-run", "--json"])
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["OutputType"], "stata17")
        self.assertEqual(report["TableNameCN"], "员工构成信息")


if __name__ == "__main__":
    unittest.main()


class LiveFindingsTests(unittest.TestCase):
    """What the first live run on 2026-09-29 taught, pinned."""

    def test_a_row_reading_100_percent_is_ready(self) -> None:
        self.assertTrue(RessetTask.status_ready("100%"))
        self.assertFalse(RessetTask.status_ready("35%"))
        self.assertFalse(RessetTask.status_ready("生成失败"))

    def test_a_row_with_its_download_button_is_ready_whatever_its_status_says(self) -> None:
        self.assertTrue(RessetTask("9fb", "RESSET 股票_员工构成信息", "09-29 07:11:35", "", "stata17", "40", "939736").finished)
        self.assertFalse(RessetTask("row:1:x", "RESSET 股票_员工构成信息", "09-29 07:11:35", "35%", "", "", "").finished)

    def test_the_table_total_is_read_from_the_page(self) -> None:
        from hunnu_harness.databases.resset_catalog import parse_total_records

        self.assertEqual(parse_total_records("数据开始日期 1996-12-31 总记录数   939,736 更新频度 月更新"), 939736)
        self.assertIsNone(parse_total_records("no total here"))

    def test_a_row_submitted_from_a_tab_at_its_defaults_is_refused(self) -> None:
        from hunnu_harness.databases.resset_catalog import whole_table_problem

        stray = RessetTask("9fb", "RESSET 股票_员工构成信息", "09-29 07:11:35", "100%", "excel2007", "0.571", "3912")
        self.assertIn("format", whole_table_problem(stray, 939736, "stata17"))
        short = RessetTask("9fc", "RESSET 股票_员工构成信息", "09-29 07:30:00", "100%", "stata17", "0.6", "3912")
        self.assertIn("3,912", whole_table_problem(short, 939736, "stata17"))
        whole = RessetTask("9fd", "RESSET 股票_员工构成信息", "09-29 07:40:00", "100%", "stata17", "40", "939736")
        self.assertIsNone(whole_table_problem(whole, 939736, "stata17"))

    def test_the_download_button_is_marked_for_a_real_click_not_clicked_by_script(self) -> None:
        # 2026-09-29: a script click() on the PURANDSALE row at first draw started no download, twice;
        # a real click once the frame's downloadtask() was defined started it at once.
        from hunnu_harness.databases.resset import _MARK_ROW_BUTTON_JS

        self.assertIn("data-harness-row", _MARK_ROW_BUTTON_JS)
        self.assertNotIn(".click()", _MARK_ROW_BUTTON_JS)
