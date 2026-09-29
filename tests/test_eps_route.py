"""EPS on the campus network: one query per run, 确认提交 pressed by a person.

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
from datetime import datetime
from pathlib import Path

from hunnu_harness.databases.eps_catalog import (
    INSTANT_ROW_LIMIT,
    PROVINCES,
    EPSQuery,
    EPSRequestError,
    EPSTask,
    EPSTaskError,
    claim_eps_task,
    cube_access,
    cube_title_from_default_task_name,
    institutional_session,
    label_key,
    parse_dimension_counts,
    parse_estimate,
    parse_tasks,
    parse_year_range,
    unique_task_name,
    validate_eps_request,
)
from hunnu_harness.downloads.manager import DownloadManager
from hunnu_harness.models import DownloadRequest
from hunnu_harness.workflows import run_eps_download

GDP = "地区生产总值 （当年价）（亿元）"
GDP_PC = "人均 地区生产总值 （元）"

# What localStorage.userInfo held on the campus network (trimmed).
USER_INFO = {
    "groupName": "湖南师范大学",
    "realname": "湖南师范大学",
    "isValid": 1,
    "endDate": "2030-12-31 00:00:00",
    "cubes": [
        {"cubeId": 9, "cubeNameZh": "中国宏观经济数据库", "visitAuth": 1, "downloadAuth": 1},
        {"cubeId": 892, "cubeNameZh": "年度（分省级）", "visitAuth": 1, "downloadAuth": 1},
        {"cubeId": 777, "cubeNameZh": "年度（分区域县）", "visitAuth": 1, "downloadAuth": 0},
    ],
}

# The dialog as it read after 数据下载 on cube 892 (2 indicators x 31 provinces x 14 years).
DIALOG_TEXT = (
    "下载\n任务名\n预估数据 868 行\n文件格式\ncsv\ntxt\ndta\n数据预览（前10行）\n指标\t地区\t时间\t数值\n"
    "地区生产总值（当年价）（亿元）\t北京\t2023\t43760.70000\n以上为前 10 行预览，完整数据将以选定格式生成\n取 消\n确认提交"
)
HEADER_TEXT = "行维度\n联动\n指标\n\n已选择 2\n地区\n\n已选择 31\n列维度\n时间\n\n已选择 14\n固定维度"


def eps_request(**overrides) -> DownloadRequest:
    base = dict(
        database="EPS",
        module="",
        table="892",
        stocks=("provinces",),
        date_start="2010",
        date_end="2023",
        fields=(GDP, GDP_PC),
        output_format="csv",
    )
    base.update(overrides)
    return DownloadRequest(**base)


class RequestTests(unittest.TestCase):
    def test_a_query_names_a_cube_indicators_regions_years_and_a_format(self) -> None:
        query = validate_eps_request(eps_request())
        self.assertEqual(query.cube_id, 892)
        self.assertEqual(query.indicators, (GDP, GDP_PC))
        self.assertEqual(query.regions, PROVINCES)
        self.assertEqual(len(PROVINCES), 31)
        self.assertEqual(query.years, tuple(range(2010, 2024)))
        self.assertEqual(query.rows, 868)  # the live dialog's 预估数据

    def test_the_cube_is_the_number_in_the_page_address(self) -> None:
        with self.assertRaises(EPSRequestError) as caught:
            validate_eps_request(eps_request(table="中国宏观经济数据库"))
        self.assertEqual(caught.exception.code, "EPS_BAD_CUBE")

    def test_an_indicator_is_required(self) -> None:
        with self.assertRaises(EPSRequestError) as caught:
            validate_eps_request(eps_request(fields=()))
        self.assertEqual(caught.exception.code, "EPS_NO_INDICATOR")

    def test_only_the_formats_the_dialog_offers(self) -> None:
        with self.assertRaises(EPSRequestError) as caught:
            validate_eps_request(eps_request(output_format="xlsx"))
        self.assertEqual(caught.exception.code, "EPS_UNKNOWN_FORMAT")

    def test_a_request_the_page_would_queue_is_refused(self) -> None:
        many = tuple(f"指标{i}" for i in range(200))
        with self.assertRaises(EPSRequestError) as caught:
            validate_eps_request(eps_request(fields=many))
        self.assertEqual(caught.exception.code, "EPS_REQUEST_TOO_LARGE")
        self.assertGreaterEqual(200 * 31 * 14, INSTANT_ROW_LIMIT)

    def test_years_are_one_year_or_one_contiguous_range(self) -> None:
        self.assertEqual(parse_year_range("2010-2023"), ("2010", "2023"))
        self.assertEqual(parse_year_range("2010 – 2023"), ("2010", "2023"))
        self.assertEqual(parse_year_range("2015"), ("2015", "2015"))
        for bad in ("2023-2010", "2010,2012", "last ten years"):
            with self.assertRaises(EPSRequestError):
                parse_year_range(bad)

    def test_named_regions_are_kept_in_order_without_repeats(self) -> None:
        query = validate_eps_request(eps_request(stocks=("湖南", "湖北", "湖南")))
        self.assertEqual(query.regions, ("湖南", "湖北"))


class PageReadingTests(unittest.TestCase):
    def test_the_two_spellings_of_one_label_are_the_same_label(self) -> None:
        self.assertEqual(label_key(GDP_PC), label_key("人均地区生产总值（元）"))
        self.assertNotEqual(label_key(GDP), label_key("地区生产总值 （亿元）"))

    def test_the_campus_account_is_recognised_from_the_page_record(self) -> None:
        self.assertTrue(institutional_session(USER_INFO))
        self.assertFalse(institutional_session({**USER_INFO, "isValid": 0}))
        self.assertFalse(institutional_session({"groupName": "", "realname": ""}))

    def test_a_cube_must_be_open_for_download(self) -> None:
        self.assertEqual(cube_access(USER_INFO, 892), {"name": "年度（分省级）", "visit": True, "download": True})
        self.assertFalse(cube_access(USER_INFO, 777)["download"])
        self.assertIsNone(cube_access(USER_INFO, 123456))

    def test_the_dialog_estimate_and_the_header_counts_are_read(self) -> None:
        self.assertEqual(parse_estimate(DIALOG_TEXT), 868)
        self.assertEqual(parse_estimate("预估数据 12,345 行"), 12345)
        self.assertIsNone(parse_estimate("下载"))
        self.assertEqual(parse_dimension_counts(HEADER_TEXT), {"指标": 2, "地区": 31, "时间": 14})

    def test_the_cube_title_comes_from_the_proposed_task_name(self) -> None:
        self.assertEqual(
            cube_title_from_default_task_name("中国宏观经济数据库 - 年度（分省级）_2026-09-29_0800"),
            "中国宏观经济数据库 - 年度（分省级）",
        )

    def test_the_task_name_is_unique_to_the_run(self) -> None:
        self.assertEqual(unique_task_name(892, datetime(2026, 9, 29, 8, 30, 5)), "HUNNU-Harness-EPS-892-20260929-083005")


class DownloadListTests(unittest.TestCase):
    PAYLOAD = {
        "total": 3,
        "success": True,
        "list": [
            {"taskId": 88910, "taskName": "HUNNU-Harness-EPS-892-20260929-083005", "status": "COMPLETED",
             "statusReason": "", "dataCount": 868, "createTime": "2026-09-29T08:30:07", "downloadMode": "SYNC",
             "fileName": "1790640406104_宏观经济-中国宏观经济数据库.csv", "fileExpireTime": "2026-10-06"},
            {"taskId": 88905, "taskName": "中国宏观经济数据库 - 年度（分省级）_2026-09-29_0800", "status": "INVALID",
             "statusReason": "文件生成失败", "dataCount": 868, "createTime": "2026-09-29T08:00:47", "downloadMode": "SYNC"},
            {"taskId": 88801, "taskName": "湖南市县统计数据库 - 年度（市级）_2026-09-28_1450", "status": "COMPLETED",
             "statusReason": "", "dataCount": 32, "createTime": "2026-09-28T14:50:58", "downloadMode": "SYNC"},
        ],
    }

    def test_rows_keep_no_file_names_or_links(self) -> None:
        tasks = parse_tasks(self.PAYLOAD)
        self.assertEqual(len(tasks), 3)
        self.assertNotIn("1790640406104", json.dumps([t.as_dict() for t in tasks], ensure_ascii=False))
        self.assertTrue(tasks[0].completed)
        self.assertTrue(tasks[1].invalid)
        self.assertEqual(tasks[1].reason, "文件生成失败")

    def test_only_the_row_with_the_runs_own_name_is_claimed(self) -> None:
        tasks = parse_tasks(self.PAYLOAD)
        mine = claim_eps_task(tasks, "HUNNU-Harness-EPS-892-20260929-083005")
        self.assertEqual(mine.task_id, "88910")
        self.assertIsNone(claim_eps_task(tasks, "HUNNU-Harness-EPS-892-20260929-090000"))

    def test_two_rows_with_the_runs_name_are_ambiguous(self) -> None:
        row = EPSTask("1", "HUNNU-Harness-EPS-892-x", "COMPLETED", "", 868, "t", "SYNC")
        with self.assertRaises(EPSTaskError) as caught:
            claim_eps_task([row, row], "HUNNU-Harness-EPS-892-x")
        self.assertEqual(caught.exception.code, "EPS_TASK_AMBIGUOUS")


class FakeEPS:
    def __init__(self, drop_dir: Path, *, final_status: str = "COMPLETED") -> None:
        self.drop_dir = drop_dir
        self.final_status = final_status
        self.calls: list[str] = []

    async def open(self) -> None:
        self.calls.append("open")

    async def open_cube(self, query: EPSQuery) -> dict:
        self.calls.append(f"cube:{query.cube_id}")
        return {"指标": 0, "地区": 0, "时间": 0}

    async def select_query(self, query: EPSQuery, dimensions: dict) -> dict:
        self.calls.append(f"select:{len(query.indicators)}x{len(query.regions)}x{len(query.years)}")
        return {"指标": 2, "地区": 31, "时间": 14}

    async def open_download_dialog(self, query: EPSQuery, task_name: str) -> dict:
        self.calls.append(f"dialog:{task_name}")
        return {"estimate": 868, "cube_title": "中国宏观经济数据库 - 年度（分省级）", "task_name": task_name}

    async def present_for_person(self) -> None:
        self.calls.append("present")

    async def wait_for_person_and_task(self, task_name: str, *, timeout_seconds: float) -> EPSTask:
        self.calls.append("wait_person")
        if self.final_status == "INVALID":
            raise EPSTaskError("EPS_TASK_INVALID", "文件生成失败")
        (self.drop_dir / f"{task_name}.csv").write_text("指标,地区,时间,数值\n", encoding="utf-8")
        return EPSTask("88910", task_name, "COMPLETED", "", 868, "2026-09-29T08:30:07", "SYNC")


class WorkflowTests(unittest.TestCase):
    def run_flow(self, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        staging = root / "staging"
        staging.mkdir()
        adapter = FakeEPS(staging, **kwargs)
        manager = DownloadManager([staging], root / "archive", root / "manifests")
        announced = []
        record = asyncio.run(
            run_eps_download(
                adapter,
                manager,
                eps_request(),
                on_ready=lambda dialog: announced.append((list(adapter.calls), dialog["estimate"])),
                download_timeout_seconds=10,
                now=datetime(2026, 9, 29, 8, 30, 5),
            )
        )
        return adapter, record, announced

    def test_the_person_is_asked_only_once_the_dialog_is_filled(self) -> None:
        adapter, record, announced = self.run_flow()
        name = "HUNNU-Harness-EPS-892-20260929-083005"
        self.assertEqual(
            adapter.calls, ["open", "cube:892", "select:2x31x14", f"dialog:{name}", "present", "wait_person"]
        )
        self.assertEqual(announced, [(["open", "cube:892", "select:2x31x14", f"dialog:{name}", "present"], 868)])
        self.assertEqual((record.database, record.module, record.table), ("EPS", "中国宏观经济数据库 - 年度（分省级）", "892"))
        self.assertEqual(record.query["fields"], [GDP, GDP_PC])
        self.assertEqual(len(record.query["stocks"]), 31)
        self.assertEqual((record.query["date_start"], record.query["date_end"], record.query["format"]), ("2010", "2023", "csv"))
        self.assertEqual(record.source_url, "https://olap.epsnet.com.cn/#/datas_home?cubeId=892")
        self.assertNotIn("sid=", record.source_url)

    def test_an_invalid_task_archives_nothing(self) -> None:
        with self.assertRaises(EPSTaskError):
            self.run_flow(final_status="INVALID")

    def test_the_request_is_checked_before_the_browser_is_touched(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        adapter = FakeEPS(Path(tmp.name))
        manager = DownloadManager([Path(tmp.name)], Path(tmp.name) / "a", Path(tmp.name) / "m")
        with self.assertRaises(EPSRequestError):
            asyncio.run(run_eps_download(adapter, manager, eps_request(table="x")))
        self.assertEqual(adapter.calls, [])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, dict]:
        from hunnu_harness.cli import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["data-acquire", *argv, "--json"])
        return code, json.loads(buffer.getvalue())

    def test_dry_run_validates_an_eps_query_without_a_browser(self) -> None:
        code, report = self.run_cli(
            "--database", "EPS", "--cube", "892", "--indicator", GDP, "--indicator", GDP_PC,
            "--regions", "provinces", "--years", "2010-2023", "--format", "csv", "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["Status"], "VALIDATED")
        self.assertEqual(report["ExpectedRows"], 868)
        self.assertEqual(report["Regions"], 31)
        self.assertEqual(report["CubePage"], "https://olap.epsnet.com.cn/#/datas_home?cubeId=892")

    def test_a_table_database_still_needs_its_table(self) -> None:
        code, report = self.run_cli("--database", "RESSET", "--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(report["Status"], "INVALID_REQUEST")


class LiveFindingsTests(unittest.TestCase):
    """What the live runs on 2026-09-29 taught, pinned."""

    def test_the_search_popover_and_the_dimension_tree_are_told_apart_by_their_containers(self) -> None:
        # Runs 3 and 4 aimed at "the last visible tree" and hit the indicator
        # search popover, which stays open over the region tree.
        from hunnu_harness.databases.eps import _TREE_JS

        self.assertIn(".search-tree-list .ant-tree", _TREE_JS)
        self.assertIn(".select-view .selecting .ant-tree", _TREE_JS)

    def test_a_highlighted_search_label_is_the_plain_label(self) -> None:
        # The spaces are the highlight around the search word (run 1 searched with them and found nothing).
        self.assertEqual(label_key("人均 地区生产总值 （元）"), label_key("人均地区生产总值（元）"))
        self.assertEqual(label_key("支出法 地区生产总值 （亿元）"), label_key("支出法地区生产总值（亿元）"))

    def test_the_estimate_is_one_row_per_indicator_region_and_year(self) -> None:
        self.assertEqual(validate_eps_request(eps_request()).rows, parse_estimate(DIALOG_TEXT))


if __name__ == "__main__":
    unittest.main()
