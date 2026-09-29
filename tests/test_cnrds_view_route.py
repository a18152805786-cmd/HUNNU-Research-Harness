"""The CNRDS table-page route: one path for every table the school account holds.

Each test name says what breaking it would mean.  The live facts these tests
encode were read on 2026-09-28 through the HUNNU library route; see
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

from hunnu_harness.databases.cnrds import CNRDSAdapter, CNRDSGate
from hunnu_harness.databases.cnrds_catalog import (
    SCHOOL_ACCOUNT_BASE_DATABASES,
    CNRDSQueueError,
    CNRDSRequestError,
    QueuedTask,
    claim_new_task,
    classify_gate,
    find_task,
    summary_confirms,
    cnrds_view_url,
    parse_subscribed_databases,
    queued_tasks,
    validate_view_request,
    view_from_url,
    view_heading_matches,
)
from hunnu_harness.downloads.manager import DownloadManager
from hunnu_harness.models import BrowserState, DownloadRequest
from hunnu_harness.workflows import run_cnrds_download, run_cnrds_view_download

MODAL_LISTING = """已订阅数据库
筛选：
全库
公司特色库
经济特色库
基础库
数据库名
系列
截止时间
股价研究 - CNSP
上市公司股票基础数据
2027-09-28
宏观经济(季度) - MACROQ
经济研究基础数据
2027-09-28
区域经济研究 - CRED
经济研究基础数据
2027-09-28
关闭"""

PERSONAL_CENTRE_LISTING = "已订阅数据库\n数据库名\t截止时间\n股价研究 - CNSP2027-09-28\n财务报表 - CNFS2027-09-28\n宏观经济(年度) - MACRO2027-09-28\n"

HIDDEN_LISTING = "已订阅数据库筛选：全库基础库股价研究 - CNSP上市公司股票基础数据2027-09-28股权研究 - CERD上市公司治理基础数据2027-09-28关闭"

SIGNED = "https://cnrds-file.oss-cn-shanghai.aliyuncs.com/T2?Expires=1&OSSAccessKeyId=K&Signature=S"


class CatalogTests(unittest.TestCase):
    def test_route_round_trips_so_a_page_left_elsewhere_is_never_taken_for_the_table(self) -> None:
        url = cnrds_view_url("cnsp", "个股年回报率")
        self.assertIn("#/BaseDatabase/DB/CNSP/ViewName/", url)
        self.assertEqual(view_from_url(url), ("CNSP", "个股年回报率"))
        self.assertIsNone(view_from_url("https://www.cnrds.com/Home/Index#/"))

    def test_heading_names_the_table_or_the_download_would_be_of_another_table(self) -> None:
        body = "首页 / 基础库 / 股价研究 - CNSP\n股价研究 - 个股回报率 - 个股年回报率\n温馨提示"
        self.assertTrue(view_heading_matches(body, "个股年回报率", "股价研究"))
        self.assertFalse(view_heading_matches(body, "个股月回报率", "股价研究"))
        self.assertFalse(view_heading_matches(body, "个股年回报率", "股权研究"))
        self.assertFalse(view_heading_matches("股价研究- CNSP\n个股回报率", "个股年回报率"))

    def test_subscription_list_is_read_in_all_three_shapes_the_site_renders(self) -> None:
        self.assertEqual(
            parse_subscribed_databases(MODAL_LISTING),
            {"CNSP": "2027-09-28", "MACROQ": "2027-09-28", "CRED": "2027-09-28"},
        )
        self.assertEqual(
            parse_subscribed_databases(PERSONAL_CENTRE_LISTING),
            {"CNSP": "2027-09-28", "CNFS": "2027-09-28", "MACRO": "2027-09-28"},
        )
        self.assertEqual(
            parse_subscribed_databases(HIDDEN_LISTING),
            {"CNSP": "2027-09-28", "CERD": "2027-09-28"},
        )

    def test_school_account_catalog_is_the_verified_base_library(self) -> None:
        self.assertEqual(len(SCHOOL_ACCOUNT_BASE_DATABASES), 29)
        for featured in ("SCRD", "CESG", "NQPD", "CIRD", "EIQD"):
            self.assertNotIn(featured, SCHOOL_ACCOUNT_BASE_DATABASES)

    def test_featured_database_is_refused_before_a_browser_is_touched(self) -> None:
        with self.assertRaises(CNRDSRequestError) as caught:
            validate_view_request(DownloadRequest(database="CNRDS", module="SCRD", table="供应商总采购信息"))
        self.assertEqual(caught.exception.code, "CNRDS_NOT_IN_SCHOOL_SUBSCRIPTION")

    def test_a_subset_request_fails_closed_instead_of_downloading_the_whole_table_as_if_filtered(self) -> None:
        for extra in ({"stocks": ("000001",)}, {"fields": ("年总市值",)}, {"date_start": "2010"}):
            with self.subTest(extra=extra), self.assertRaises(CNRDSRequestError) as caught:
                validate_view_request(DownloadRequest(database="CNRDS", module="CNSP", table="个股年回报率", **extra))
            self.assertEqual(caught.exception.code, "CNRDS_SUBSET_UNSUPPORTED")

    def test_unknown_format_is_refused(self) -> None:
        with self.assertRaises(CNRDSRequestError):
            validate_view_request(DownloadRequest(database="CNRDS", module="CNSP", table="个股年回报率", output_format="sas"))

    def test_valid_request_normalizes_code_and_format(self) -> None:
        self.assertEqual(
            validate_view_request(DownloadRequest(database="cnrds", module=" cnsp ", table="个股年回报率", output_format="DTA")),
            ("CNSP", "个股年回报率", "dta"),
        )

    def test_the_summary_must_confirm_table_period_codes_and_format_before_anything_is_queued(self) -> None:
        summary = "× 下载概要 下载表名 个股年回报率 数据期间 时间不限 代码选择 全部代码 选择字段 股票代码 年总市值 筛选条件 未选择任何条件 输出类型 Stata格式（*.dta） 添加到下载队列"
        self.assertEqual(summary_confirms(summary, "个股年回报率", "dta"), (True, ""))
        self.assertEqual(summary_confirms(summary, "个股年回报率", "csv"), (False, "format"))
        self.assertEqual(summary_confirms(summary, "个股月回报率", "dta"), (False, "table"))
        self.assertEqual(summary_confirms(summary.replace("时间不限", "2010-2023"), "个股年回报率", "dta"), (False, "period"))

    def test_a_table_without_a_period_dimension_is_not_required_to_show_one(self) -> None:
        summary = "下载概要 下载表名 公司基本信息 代码选择 全部代码 选择字段 股票代码 输出类型 Stata格式（*.dta） 添加到下载队列"
        self.assertEqual(summary_confirms(summary, "公司基本信息", "dta"), (False, "period"))
        self.assertEqual(summary_confirms(summary, "公司基本信息", "dta", require_period=False), (True, ""))

    def test_trial_and_throttle_dialogs_hand_back_to_a_person_and_are_never_clicked_through(self) -> None:
        for dialog in ("trialWarningModal", "lockedWarningModal", "featureWarningModal", "openLimitWarning", "downloadAlertModal"):
            with self.subTest(dialog=dialog):
                spec = classify_gate(dialog)
                self.assertIsNotNone(spec)
                self.assertTrue(spec.human)
        self.assertFalse(classify_gate("waitingModal").human)
        self.assertIsNone(classify_gate("downloadSummaryModal"))
        self.assertIsNone(classify_gate("downList_modal"))


class SharedQueueTests(unittest.TestCase):
    def payload(self, *finished: tuple[str, str, str], pending: tuple[tuple[str, str, str], ...] = ()) -> dict:
        def row(task_id: str, title: str, when: str) -> dict:
            return {"DownloadTaskId": task_id, "Title": title, "DownloadTime": when, "FileType": "DTA", "URL": SIGNED}

        return {
            "downLoadResult": {
                "notFinishedTasks": [row(*item) for item in pending],
                "finishedDownloadRecords": [row(*item) for item in finished],
            }
        }

    def test_signed_storage_urls_are_dropped_on_first_read(self) -> None:
        tasks = queued_tasks(self.payload(("T1", "关联交易-关联交易等4表联表数据", "2026-07-29 16:08:33")))
        self.assertEqual(len(tasks), 1)
        text = json.dumps([task.as_dict() for task in tasks], ensure_ascii=False)
        self.assertNotIn("Signature", text)
        self.assertNotIn("aliyuncs", text)

    def test_another_users_newest_download_on_the_shared_account_is_never_claimed(self) -> None:
        before = {"T1"}
        after = queued_tasks(
            self.payload(
                ("T1", "个股年回报率", "2026-09-28 10:00:00"),
                ("T9", "关联交易-关联交易等4表联表数据", "2026-09-28 23:10:00"),
            )
        )
        self.assertIsNone(claim_new_task(before, after, "个股年回报率"))

    def test_the_run_claims_only_its_new_task_and_waits_while_it_is_pending(self) -> None:
        pending = queued_tasks(self.payload(pending=(("T2", "股价研究-个股年回报率", "2026-09-28 23:11:00"),)))
        task = claim_new_task({"T1"}, pending, "个股年回报率")
        self.assertEqual(task.task_id, "T2")
        self.assertFalse(task.finished)

    def gaosu(self, task_id: str, when: str, process: str = "Finish", system: str = "CNSP", view: str = "个股年回报率") -> dict:
        return {
            "downLoadResult": {
                "notFinishedTasks": [],
                "finishedDownloadRecords": [],
                "notFinishedDownloadRecords": [],
                "gaosuList": [
                    {
                        "DownloadTime": when,
                        "FileType": "Xlsx",
                        "Title": f"{system}-{view}",
                        "Process": process,
                        "DownloadTaskId": task_id,
                        "ViewName": view,
                        "System": system,
                        "URL": SIGNED,
                    }
                ],
            }
        }

    def test_single_table_tasks_listed_under_gaosuList_are_read_so_a_finished_task_is_not_missed(self) -> None:
        # Found live on 2026-09-28: the run read only the joined-table endpoint
        # and timed out on a task that had finished minutes earlier.
        tasks = queued_tasks(self.payload(("T1", "关联交易-关联交易等4表联表数据", "2026-07-29 16:08:33")), self.gaosu("G1", "2026-09-28 23:19:48"))
        by_id = {task.task_id: task for task in tasks}
        self.assertTrue(by_id["G1"].finished)
        self.assertEqual((by_id["G1"].system, by_id["G1"].view_name), ("CNSP", "个股年回报率"))
        self.assertNotIn("aliyuncs", json.dumps([t.as_dict() for t in tasks], ensure_ascii=False))
        self.assertEqual(claim_new_task({"T1"}, tasks, "个股年回报率", "CNSP").task_id, "G1")

    def test_a_task_still_being_prepared_is_claimed_but_not_yet_finished(self) -> None:
        tasks = queued_tasks(self.gaosu("G1", "2026-09-28 23:19:48", process="Running"))
        self.assertFalse(claim_new_task(set(), tasks, "个股年回报率", "CNSP").finished)

    def test_system_and_view_name_decide_the_claim_when_the_record_carries_them(self) -> None:
        tasks = queued_tasks(self.gaosu("G2", "2026-09-28 23:20:00", system="CNFI", view="个股年回报率"))
        self.assertIsNone(claim_new_task(set(), tasks, "个股年回报率", "CNSP"))

    def test_collecting_a_named_task_checks_its_table_and_that_it_is_finished(self) -> None:
        tasks = queued_tasks(self.gaosu("G1", "2026-09-28 23:19:48"), self.gaosu("G3", "2026-09-28 23:30:00", process="Running"))
        self.assertEqual(find_task(tasks, "G1", "CNSP", "个股年回报率").task_id, "G1")
        for task_id, table, code in (("G1", "个股月回报率", "CNRDS_TASK_TABLE_MISMATCH"), ("G3", "个股年回报率", "CNRDS_TASK_NOT_FINISHED"), ("NOPE", "个股年回报率", "CNRDS_TASK_NOT_FOUND")):
            with self.subTest(task_id=task_id), self.assertRaises(CNRDSQueueError) as caught:
                find_task(tasks, task_id, "CNSP", table)
            self.assertEqual(caught.exception.code, code)

    def test_two_new_candidates_are_ambiguous_and_fail_closed(self) -> None:
        after = queued_tasks(
            self.payload(("T2", "股价研究-个股年回报率", "2026-09-28 23:11:00"), ("T3", "股价研究-个股年回报率", "2026-09-28 23:11:05"))
        )
        with self.assertRaises(CNRDSQueueError):
            claim_new_task({"T1"}, after, "个股年回报率")


class FakePage:
    def __init__(
        self,
        *,
        url: str,
        body: str,
        subscription: str,
        dialogs: list[str] | None = None,
        redirect_to: str | None = None,
    ) -> None:
        self.url = url
        self.body = body
        self.subscription = subscription
        self.dialogs = dialogs or []
        self.redirect_to = redirect_to
        self.visited: list[str] = []

    async def goto(self, url: str, **_kwargs) -> None:
        self.visited.append(url)
        self.url = self.redirect_to or url

    async def reload(self, **_kwargs) -> None:
        return None

    async def evaluate(self, script: str, arg=None):
        if "已订阅数据库" in script:
            return self.subscription
        if "classList.contains('in')" in script:
            return list(self.dialogs)
        raise AssertionError(f"unexpected script: {script[:60]}")


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def state(self, *, database=None, module=None, table=None) -> BrowserState:
        return BrowserState(url=self.page.url, title="中国研究数据服务平台", body_text=self.page.body, database=database)


TABLE_BODY = "学校账号\n首页 / 基础库 / 股价研究 - CNSP\n股价研究 - 个股回报率 - 个股年回报率\n*　本表数据开始时间： 1990"
PERSONAL_CENTRE = "https://www.cnrds.com/Home/Index#/personalCenter"


def adapter_for(page: FakePage) -> CNRDSAdapter:
    adapter = CNRDSAdapter(FakeBrowser(page))
    adapter.page_ready_seconds = 0.05

    async def no_pause(_seconds: float) -> None:
        return None

    adapter._pause = no_pause  # type: ignore[method-assign]
    return adapter


class AdapterGuardTests(unittest.TestCase):
    def test_signed_out_session_is_redirected_to_login_and_handed_back_to_the_user(self) -> None:
        page = FakePage(
            url="about:blank",
            body="密码登录 个人登录 学校登录 CNRDS",
            subscription="",
            redirect_to="https://www.cnrds.com/Home/Login",
        )
        with self.assertRaises(CNRDSGate) as caught:
            asyncio.run(adapter_for(page).open_view("CNSP", "个股年回报率"))
        self.assertEqual(caught.exception.code, "ACTION_REQUIRED_USER_LOGIN")
        self.assertTrue(caught.exception.human)
        self.assertEqual(page.visited, [PERSONAL_CENTRE])

    def test_table_page_is_confirmed_by_url_heading_and_subscription(self) -> None:
        page = FakePage(url="https://www.cnrds.com/Home/Index#/", body=TABLE_BODY, subscription=MODAL_LISTING)
        state = asyncio.run(adapter_for(page).open_view("CNSP", "个股年回报率"))
        self.assertEqual((state.module, state.table), ("CNSP", "个股年回报率"))
        self.assertEqual(page.visited, [PERSONAL_CENTRE, cnrds_view_url("CNSP", "个股年回报率")])

    def test_a_database_missing_from_the_accounts_own_list_is_refused_before_its_page_opens(self) -> None:
        page = FakePage(url="https://www.cnrds.com/Home/Index#/", body=TABLE_BODY, subscription=HIDDEN_LISTING.replace("CNSP", "CNFS"))
        with self.assertRaises(CNRDSGate) as caught:
            asyncio.run(adapter_for(page).open_view("CNSP", "个股年回报率"))
        self.assertEqual(caught.exception.code, "CNRDS_NOT_SUBSCRIBED")
        self.assertEqual(page.visited, [PERSONAL_CENTRE])

    def test_an_unreadable_subscription_list_refuses_rather_than_assumes(self) -> None:
        page = FakePage(url="https://www.cnrds.com/Home/Index#/", body=TABLE_BODY, subscription="已订阅数据库")
        with self.assertRaises(CNRDSGate) as caught:
            asyncio.run(adapter_for(page).open_view("CNSP", "个股年回报率"))
        self.assertEqual(caught.exception.code, "CNRDS_NOT_SUBSCRIBED")

    def test_a_page_that_never_shows_the_table_is_not_downloaded_from(self) -> None:
        body = "学校账号\n股价研究 - 个股回报率 - 个股月回报率\n本表数据开始时间： 1990"
        page = FakePage(url="https://www.cnrds.com/Home/Index#/", body=body, subscription=MODAL_LISTING)
        with self.assertRaises(CNRDSGate) as caught:
            asyncio.run(adapter_for(page).open_view("CNSP", "个股年回报率"))
        self.assertEqual(caught.exception.code, "CNRDS_TABLE_NOT_CONFIRMED")

    def test_a_visible_trial_dialog_stops_the_run(self) -> None:
        page = FakePage(url="x", body=TABLE_BODY, subscription="", dialogs=["downloadSummaryModal", "trialWarningModal"])
        with self.assertRaises(CNRDSGate) as caught:
            asyncio.run(adapter_for(page).raise_on_gate())
        self.assertEqual(caught.exception.code, "CNRDS_NOT_SUBSCRIBED_TRIAL_ONLY")


class FakeViewAdapter:
    """Stands in for the browser half; the workflow's ordering is what is tested."""

    def __init__(self, drop_dir: Path) -> None:
        self.drop_dir = drop_dir
        self.calls: list[str] = []

    async def open_view(self, db: str, table: str) -> None:
        self.calls.append(f"open_view:{db}/{table}")

    async def choose_whole_table(self, fmt: str) -> dict:
        self.calls.append(f"choose:{fmt}")
        return {"period": True, "codes": True}

    async def queue_download(self, table: str, fmt: str, applied=None):
        self.calls.append(f"queue:{fmt}")
        return {"T1"}, 0.0

    async def wait_for_task(self, before, table, *, timeout_seconds, db=""):
        self.calls.append(f"wait:{db}")
        return QueuedTask("T2", f"{db}-{table}", "2026-09-28 23:11:00", True, "DTA", db, table)

    async def find_finished_task(self, task_id: str, db: str, table: str):
        self.calls.append(f"find:{task_id}")
        return QueuedTask(task_id, f"{db}-{table}", "2026-09-28 23:19:48", True, "Xlsx", db, table)

    async def fetch_task_file(self, task: QueuedTask) -> None:
        self.calls.append(f"fetch:{task.task_id}")
        (self.drop_dir / "个股年回报率.zip").write_bytes(b"PK\x03\x04 fake")


class WorkflowTests(unittest.TestCase):
    def test_whole_table_download_is_archived_with_the_table_page_as_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, archive, manifests = root / "staging", root / "archive", root / "manifests"
            staging.mkdir()
            adapter = FakeViewAdapter(staging)
            manager = DownloadManager([staging], archive, manifests)
            record = asyncio.run(
                run_cnrds_download(adapter, manager, DownloadRequest(database="CNRDS", module="cnsp", table="个股年回报率", output_format="dta"))
            )
            self.assertEqual(adapter.calls, ["open_view:CNSP/个股年回报率", "choose:dta", "queue:dta", "wait:CNSP", "fetch:T2"])
            self.assertEqual((record.module, record.table), ("CNSP", "个股年回报率"))
            self.assertEqual(record.source_url, cnrds_view_url("CNSP", "个股年回报率"))
            self.assertNotIn("aliyuncs", record.source_url)
            self.assertTrue(Path(record.archived_path).exists())
            self.assertEqual(len(record.sha256), 64)

    def test_collecting_an_earlier_task_does_not_queue_the_table_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            adapter = FakeViewAdapter(staging)
            manager = DownloadManager([staging], root / "archive", root / "manifests")
            record = asyncio.run(
                run_cnrds_view_download(
                    adapter,
                    manager,
                    DownloadRequest(database="CNRDS", module="CNSP", table="个股年回报率"),
                    collect_task_id="G1",
                )
            )
            self.assertEqual(adapter.calls, ["open_view:CNSP/个股年回报率", "find:G1", "fetch:G1"])
            self.assertTrue(Path(record.archived_path).exists())

    def test_an_invalid_request_never_reaches_the_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeViewAdapter(Path(tmp))
            manager = DownloadManager([Path(tmp)], Path(tmp) / "a", Path(tmp) / "m")
            with self.assertRaises(CNRDSRequestError):
                asyncio.run(
                    run_cnrds_view_download(
                        adapter, manager, DownloadRequest(database="CNRDS", module="CNSP", table="个股年回报率", stocks=("000001",))
                    )
                )
            self.assertEqual(adapter.calls, [])


class DataAcquireCliTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, dict]:
        from hunnu_harness.cli import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["data-acquire", *argv, "--json"])
        return code, json.loads(buffer.getvalue())

    def test_dry_run_validates_a_subscribed_table_without_a_browser(self) -> None:
        code, report = self.run_cli("--module", "CNSP", "--table", "个股年回报率", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(report["Status"], "VALIDATED")
        self.assertEqual(report["TablePage"], cnrds_view_url("CNSP", "个股年回报率"))

    def test_featured_database_reports_the_missing_capability(self) -> None:
        code, report = self.run_cli("--module", "SCRD", "--table", "供应商总采购信息", "--dry-run")
        self.assertEqual(code, 1)
        self.assertFalse(report["HarnessCapabilityAvailable"])
        self.assertEqual(report["MissingCapability"], "CNRDS_NOT_IN_SCHOOL_SUBSCRIPTION")


if __name__ == "__main__":
    unittest.main()
