"""What the HUNNU school account can take from CNRDS, and how to read its pages.

Everything here is pure: no browser, no network.  The adapter in ``cnrds.py``
drives the page; these functions decide what the page it sees *means* -- which
database a URL names, whether the account holds a subscription to it, which
dialog just stopped the run, and which queued download is this run's own.

Verified live on 2026-09-28 through the HUNNU library route (library home ->
数据库导航 -> "CNRDS中国研究数据服务平台(商学院)" -> 网络地址 -> CNRDS login page ->
the user's 学校登录, bound to the campus IP).  The school account is the shared
``schooluser`` account: it subscribes to the 29 base-library (基础库) databases
below, and nothing in the company-featured (公司特色库) or economy-featured
(经济特色库) series.  Those need a personal CNRDS account, which only the user
can register.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import quote, unquote

from ..models import DownloadRequest

CNRDS_HOME_URL = "https://www.cnrds.com/Home/Index"
CNRDS_LOGIN_URL = "https://www.cnrds.com/Home/Login"
#: The page reads its download list from two endpoints.  Single-table downloads
#: land in the first one's ``gaosuList`` (with ``System``/``ViewName``/``Process``);
#: the second holds joined-table tasks.  Found live on 2026-09-28: reading only
#: the second, a finished single-table task was never seen and the run timed out.
CNRDS_DOWNLOAD_LIST_APIS = ("/Home/getDownloadResult", "/api/Home/getDownloadResult")

#: How a person reaches CNRDS under the institution's licence.  Documented, not
#: automated: the last step (学校登录) is the user's, like every sign-in.
HUNNU_LIBRARY_ROUTE = (
    "https://lib.hunnu.edu.cn/ -> 数据库导航 -> CNRDS中国研究数据服务平台(商学院) -> "
    "网络地址 -> https://www.cnrds.com/Home/Login -> 学校登录 (campus IP; no credentials)"
)

#: Base-library databases the HUNNU school account subscribes to, with their
#: series, as listed under 已订阅数据库 (valid until 2027-09-28 at verification).
SCHOOL_ACCOUNT_BASE_DATABASES: dict[str, tuple[str, str]] = {
    "CNSP": ("股价研究", "上市公司股票基础数据"),
    "CAST": ("股票异常交易", "上市公司股票基础数据"),
    "CSTS": ("特殊处理股票", "上市公司股票基础数据"),
    "CMTD": ("融资融券", "上市公司股票基础数据"),
    "CIPO": ("IPO综合", "上市公司股票基础数据"),
    "CSEO": ("增发与配股", "上市公司股票基础数据"),
    "CEPD": ("业绩预告", "上市公司财务基础数据"),
    "FRDT": ("财务报告披露时间", "上市公司财务基础数据"),
    "CNFS": ("财务报表", "上市公司财务基础数据"),
    "NFSD": ("财务报表附注", "上市公司财务基础数据"),
    "CNFI": ("财务指标", "上市公司财务基础数据"),
    "CEFD": ("盈利预测", "上市公司财务基础数据"),
    "CBID": ("公司基本信息", "上市公司治理基础数据"),
    "CCGD": ("公司治理", "上市公司治理基础数据"),
    "AUDIT": ("审计意见与费用", "上市公司治理基础数据"),
    "MTDB": ("管理层变更", "上市公司治理基础数据"),
    "VPCE": ("公司与高管违规", "上市公司治理基础数据"),
    "ECEI": ("高管薪酬与激励", "上市公司治理基础数据"),
    "CRTD": ("关联交易", "上市公司治理基础数据"),
    "CERD": ("股权研究", "上市公司治理基础数据"),
    "IORD": ("机构投资者持股", "上市公司治理基础数据"),
    "CCDD": ("股利分红", "上市公司治理基础数据"),
    "CLAD": ("诉讼仲裁", "上市公司治理基础数据"),
    "CITD": ("内部人交易", "上市公司治理基础数据"),
    "MACRO": ("宏观经济(年度)", "经济研究基础数据"),
    "MACROQ": ("宏观经济(季度)", "经济研究基础数据"),
    "MACROM": ("宏观经济(月度)", "经济研究基础数据"),
    "CRED": ("区域经济研究", "经济研究基础数据"),
    "BOND": ("债券研究", "经济研究基础数据"),
}

#: Output formats, keyed by the value a request may carry, mapped to the radio
#: button the table page renders for it.
FORMAT_RADIO_IDS: dict[str, str] = {
    "xlsx": "XlsxFile",
    "txt": "TxtFile",
    "csv": "CsvFile",
    "xml": "XmlFile",
    "xls": "XlsFile",
    "html": "HtmlFile",
    "dta": "DtaFile",
}


@dataclass(frozen=True)
class CNRDSGateSpec:
    """A dialog that stops the run.  ``human`` means a person must decide."""

    code: str
    reason: str
    human: bool = True


#: Dialogs the table page can raise.  Every one of them ends the run: several
#: offer a "继续下载" button, and pressing it would silently download *trial*
#: data, or keep hammering a shared account that was just told to slow down.
#: None of them is ever clicked through.
GATE_DIALOGS: dict[str, CNRDSGateSpec] = {
    "trialWarningModal": CNRDSGateSpec(
        "CNRDS_NOT_SUBSCRIBED_TRIAL_ONLY",
        "the institution has not subscribed to this database's formal data; continuing would download trial data",
    ),
    "lockedWarningModal": CNRDSGateSpec(
        "CNRDS_LOCKED_PERIOD_TRIAL_ONLY",
        "the database is in its lock period; downloads during it are trial data",
    ),
    "testWarningModal": CNRDSGateSpec(
        "CNRDS_TEST_DATABASE",
        "the database is still in test; its data are not formal releases",
    ),
    "featureWarningModal": CNRDSGateSpec(
        "CNRDS_PERSONAL_ACCOUNT_REQUIRED",
        "this featured database needs a personal CNRDS account; the shared school account cannot download it",
    ),
    "downloadAlertModal": CNRDSGateSpec(
        "CNRDS_FIELDS_NOT_DOWNLOADABLE",
        "some selected fields cannot be downloaded from the platform; the rest would arrive silently incomplete",
    ),
    "vpnErrorModal": CNRDSGateSpec(
        "CNRDS_VPN_FIELD_LIMIT",
        "access arrived through the school VPN, which cannot forward this many fields",
    ),
    "openLimitWarning": CNRDSGateSpec(
        "CNRDS_ACCOUNT_THROTTLED",
        "CNRDS warned the shared school account against frequent operations; stop and let a person decide",
    ),
    "waitingModal": CNRDSGateSpec(
        "CNRDS_DATABASE_UPGRADING",
        "the database or table is being upgraded and cannot be queried now",
        human=False,
    ),
    "errorInfoModal": CNRDSGateSpec(
        "CNRDS_ERROR_DIALOG",
        "the platform reported an error",
        human=False,
    ),
    "dtaWarningModal": CNRDSGateSpec(
        "CNRDS_DTA_CONDITIONS_IGNORED",
        "with the Stata format a conditional query only downloads the complete table",
    ),
}


class CNRDSRequestError(ValueError):
    """The request cannot be served by this adapter as asked."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def normalize_db_code(value: str) -> str:
    return re.sub(r"[^A-Za-z]", "", str(value or "")).upper()


def cnrds_view_url(db: str, view_name: str, *, section: str = "BaseDatabase") -> str:
    """The table page for ``db``/``view_name``, as the site's own router writes it."""

    code = normalize_db_code(db)
    if not code:
        raise CNRDSRequestError("CNRDS_UNKNOWN_DATABASE", "a database code is required")
    name = str(view_name or "").strip()
    if not name:
        raise CNRDSRequestError("CNRDS_UNKNOWN_TABLE", "a table (ViewName) is required")
    return f"{CNRDS_HOME_URL}#/{section}/DB/{code}/ViewName/{quote(name, safe='')}"


def view_from_url(url: str) -> tuple[str, str] | None:
    """Read ``(database, table)`` back out of a table-page URL, or ``None``."""

    match = re.search(r"#/[A-Za-z]+/DB/([A-Za-z]+)/ViewName/([^/?#]+)", str(url or ""), flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).upper(), unquote(match.group(2))


def view_heading_matches(body_text: str, view_name: str, db_name_cn: str | None = None) -> bool:
    """True when the page's table heading names this table.

    The heading reads ``股价研究 - 个股回报率 - 个股年回报率``: database, group,
    table.  A page left on a different table, or the database landing page,
    does not carry it.
    """

    for line in str(body_text or "").splitlines():
        text = line.strip()
        if not text.endswith(f"- {view_name}"):
            continue
        if db_name_cn is None or text.startswith(db_name_cn):
            return True
    return False


def parse_subscribed_databases(text: str) -> dict[str, str]:
    """Database codes, with their expiry date, from the 已订阅数据库 listing.

    Rows read ``股价研究 - CNSP`` followed by an expiry date -- on the next line
    after the series name in the dialog, glued straight on in the personal
    centre (``股价研究 - CNSP2027-09-28``), and with no line breaks at all when
    the list is read from a hidden element.  Anything that is not a
    code-then-date pair is ignored.
    """

    subscribed: dict[str, str] = {}
    pattern = re.compile(r"-\s*([A-Z]{2,8})(?=[^A-Za-z])[^\d]{0,60}?(\d{4}-\d{2}-\d{2})")
    for match in pattern.finditer(str(text or "")):
        subscribed.setdefault(match.group(1), match.group(2))
    return subscribed


def classify_gate(dialog_id: str) -> CNRDSGateSpec | None:
    return GATE_DIALOGS.get(str(dialog_id or ""))


def validate_view_request(request: DownloadRequest) -> tuple[str, str, str]:
    """Check a CNRDS request against what this adapter can do, before any browser.

    Returns ``(database, table, format)``.  The first version downloads whole
    tables only -- all periods, all codes, all fields -- because the table
    page's date inputs are read-only pickers and its code and field selectors
    have not been verified end to end.  Asking for a subset fails closed rather
    than downloading more (or less) than was asked; narrow the table after
    download instead.
    """

    if str(request.database or "").strip().upper() != "CNRDS":
        raise CNRDSRequestError("CNRDS_WRONG_SOURCE", f"not a CNRDS request: {request.database!r}")
    db = normalize_db_code(request.module)
    table = str(request.table or "").strip()
    if not db:
        raise CNRDSRequestError("CNRDS_UNKNOWN_DATABASE", "Module must be a CNRDS database code such as CNSP")
    if not table:
        raise CNRDSRequestError("CNRDS_UNKNOWN_TABLE", "Table must name a CNRDS table (ViewName)")
    if db not in SCHOOL_ACCOUNT_BASE_DATABASES:
        raise CNRDSRequestError(
            "CNRDS_NOT_IN_SCHOOL_SUBSCRIPTION",
            f"{db} is not one of the base-library databases the HUNNU school account subscribes to; "
            "featured databases need a personal CNRDS account, which only the user can register",
        )
    if request.stocks:
        raise CNRDSRequestError("CNRDS_SUBSET_UNSUPPORTED", "selecting individual codes is not supported yet; download the table and filter it")
    if request.fields:
        raise CNRDSRequestError("CNRDS_SUBSET_UNSUPPORTED", "selecting individual fields is not supported yet; the whole table is downloaded")
    if request.date_start or request.date_end:
        raise CNRDSRequestError("CNRDS_SUBSET_UNSUPPORTED", "restricting the period is not supported yet; download the table and filter it")
    fmt = str(request.output_format or "dta").strip().lower()
    if fmt not in FORMAT_RADIO_IDS:
        raise CNRDSRequestError("CNRDS_UNKNOWN_FORMAT", f"output format must be one of {sorted(FORMAT_RADIO_IDS)}")
    return db, table, fmt


#: What the download summary says under 输出类型 for each format.  The summary
#: is read back before queueing, so a format click the page did not register is
#: caught before a file in the wrong format is produced.
FORMAT_SUMMARY_LABELS: dict[str, str] = {
    "xlsx": "Excel2007格式",
    "txt": "TXT文本格式",
    "csv": "逗号分隔文本",
    "xml": "Xml文件",
    "xls": "Excel2003格式",
    "html": "HTML格式",
    "dta": "Stata格式",
}


def summary_confirms(
    summary_text: str,
    table: str,
    fmt: str,
    *,
    require_period: bool = True,
    require_codes: bool = True,
) -> tuple[bool, str]:
    """Does the download summary describe exactly this whole-table request?

    ``require_period`` / ``require_codes`` are False only for a table whose
    page has no such dimension (its radio is disabled).
    """

    text = " ".join(str(summary_text or "").split())
    checks = [(f"下载表名 {table}", "table")]
    if require_period:
        checks.append(("数据期间 时间不限", "period"))
    if require_codes:
        checks.append(("代码选择 全部代码", "codes"))
    checks.append((f"输出类型 {FORMAT_SUMMARY_LABELS[fmt]}", "format"))
    for needle, what in checks:
        if needle not in text:
            return False, what
    return True, ""


# ---------------------------------------------------------------------------
# The shared download queue
# ---------------------------------------------------------------------------
#
# The school account is one account for the whole university.  Its download
# list can show downloads other people queued, so "the newest finished item" is
# not necessarily ours.  A run claims its task by difference: the task ids
# present before it queued are recorded, and only a task that appears after the
# click, for this database and table, can be claimed.  Two such tasks (someone
# else queued the same table in the same minutes) is ambiguous, and ambiguity
# fails closed.
#
# The records carry signed OSS download URLs.  They are dropped here, at the
# first read, so no report, log or manifest can ever contain one.

_FINISHED_PROCESS = {"finish", "finished", "done", "complete", "completed", "压缩完成", "完成"}


@dataclass(frozen=True)
class QueuedTask:
    task_id: str
    title: str
    download_time: str
    finished: bool
    file_type: str = ""
    system: str = ""
    view_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "DownloadTaskId": self.task_id,
            "Title": self.title,
            "DownloadTime": self.download_time,
            "Finished": self.finished,
            "FileType": self.file_type,
            "System": self.system,
            "ViewName": self.view_name,
        }

    def is_for(self, db: str, table: str) -> bool:
        """Names this table: by System/ViewName when the record carries them."""

        if self.system and self.view_name:
            return self.system.upper() == db.upper() and self.view_name == table
        return table in self.title


def queued_tasks(*payloads: Mapping[str, Any] | None) -> list[QueuedTask]:
    """Every task in the download-list payloads, without their signed URLs."""

    tasks: dict[str, QueuedTask] = {}
    for payload in payloads:
        result = (payload or {}).get("downLoadResult") or {}
        for key in ("notFinishedTasks", "notFinishedDownloadRecords", "finishedDownloadRecords", "gaosuList"):
            for record in result.get(key) or ():
                if not isinstance(record, Mapping):
                    continue
                task_id = str(record.get("DownloadTaskId") or record.get("TaskId") or "").strip()
                if not task_id:
                    continue
                if key == "gaosuList":
                    finished = str(record.get("Process") or "").strip().casefold() in _FINISHED_PROCESS
                else:
                    finished = key == "finishedDownloadRecords"
                task = QueuedTask(
                    task_id=task_id,
                    title=str(record.get("Title") or "").strip(),
                    download_time=str(record.get("DownloadTime") or "").strip(),
                    finished=finished,
                    file_type=str(record.get("FileType") or "").strip(),
                    system=str(record.get("System") or "").strip(),
                    view_name=str(record.get("ViewName") or "").strip(),
                )
                previous = tasks.get(task_id)
                if previous is None or (task.finished and not previous.finished):
                    tasks[task_id] = task
    return list(tasks.values())


class CNRDSQueueError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def claim_new_task(before_ids: Iterable[str], after: Iterable[QueuedTask], table: str, db: str = "") -> QueuedTask | None:
    """This run's task: new since ``before_ids`` and for this table.

    ``None`` while it has not appeared yet.  Raises when more than one new task
    could be ours.
    """

    seen = set(before_ids)
    candidates = [
        task
        for task in after
        if task.task_id not in seen and (task.is_for(db, table) if db else table in task.title)
    ]
    unique = {task.task_id: task for task in candidates}
    if len(unique) > 1:
        raise CNRDSQueueError(
            "CNRDS_QUEUE_AMBIGUOUS",
            f"{len(unique)} new download tasks for {table!r} appeared on the shared school account; "
            "this run cannot tell which one is its own",
        )
    return next(iter(unique.values()), None)


def find_task(tasks: Iterable[QueuedTask], task_id: str, db: str, table: str) -> QueuedTask:
    """A named task to collect: it must exist, be for this table, and be finished."""

    wanted = str(task_id or "").strip()
    for task in tasks:
        if task.task_id != wanted:
            continue
        if not task.is_for(db, table):
            raise CNRDSQueueError(
                "CNRDS_TASK_TABLE_MISMATCH",
                f"task {wanted} is for {task.system or '?'}/{task.view_name or task.title}, not {db}/{table}",
            )
        if not task.finished:
            raise CNRDSQueueError("CNRDS_TASK_NOT_FINISHED", f"task {wanted} is still being prepared; run again later")
        return task
    raise CNRDSQueueError("CNRDS_TASK_NOT_FOUND", f"task {wanted} is not in this session's download list")
