"""EPS 数据平台 (olap.epsnet.com.cn) for the HUNNU campus-IP account: pure rules.

Verified live on 2026-09-29 (see docs/INSTITUTIONAL_DATA_ACCESS.md):

* On the campus network the platform recognises 湖南师范大学 by IP -- a group
  account (``userType`` 2) valid to 2030-12-31.  The page keeps it in
  ``localStorage.userInfo``, with every cube's ``visitAuth``/``downloadAuth``.
  Nothing is typed.
* A query is built by ticking members in one tree per dimension (指标, 地区,
  时间 ...), stepping with 下一维度; the header counts 已选择 N for each.  The
  dimension payloads come back encrypted, so the page is driven, not the API.
* 数据下载 opens a dialog: a task name, 预估数据 N 行, csv / txt / dta, a
  ten-row preview (long format: 指标, 地区, 时间, 数值) and 确认提交.  Below
  50,000 rows the file comes straight back to the page as a download;
  larger requests are queued instead.
* Every submission becomes a row in the group's shared download list
  (``/eps/v2/download/tasks``), next to other HUNNU users' rows: status
  COMPLETED, or INVALID with a ``statusReason``.
* Both submissions an automated browser pressed that day ended INVALID
  (文件生成失败) with the page cleared to about:blank.  A file the page built
  itself downloaded normally in the same browser, and the site's own code has
  no such redirect.  The platform loads Aliyun's device risk engine.  So
  确认提交 is the person's press, as the RESSET CAPTCHA is; the harness
  prepares the query and the dialog, and never works around the platform.

Everything here is pure; ``eps.py`` drives the page.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from ..models import DownloadRequest

EPS_HOME_URL = "https://olap.epsnet.com.cn/"
EPS_CUBE_URL = "https://olap.epsnet.com.cn/#/datas_home?cubeId={cube_id}"
EPS_RECORDS_URL = "https://olap.epsnet.com.cn/#/new_user/group?tab=3"
EPS_TASKS_API = "/eps/v2/download/tasks"

HUNNU_LIBRARY_ROUTE = (
    "https://lib.hunnu.edu.cn/ -> 数据库导航 -> EPS全球统计数据/分析平台 -> 网络地址 -> "
    "www.epsnet.com.cn (data at olap.epsnet.com.cn); the campus network signs the browser in as 湖南师范大学"
)
INSTITUTION = "湖南师范大学"

EPS_FORMATS = ("csv", "txt", "dta")

#: Below this many rows the page returns the file at once; the harness takes
#: only those (a queued task would need a second person-side step).
INSTANT_ROW_LIMIT = 50_000

#: ``--regions provinces``: the 31 members under 地方合计, as EPS names them.
ALL_PROVINCES = "provinces"
PROVINCES = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏", "浙江",
    "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "重庆",
    "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
)

#: Dimensions the harness can fill.  A cube with any other (行业, 国别 ...)
#: is refused rather than downloaded with that dimension left at a default.
SUPPORTED_DIMENSIONS = ("指标", "地区", "时间")

TASK_COMPLETED = "COMPLETED"
TASK_INVALID = "INVALID"


class EPSRequestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class EPSTaskError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class EPSQuery:
    cube_id: int
    indicators: tuple[str, ...]
    regions: tuple[str, ...]
    years: tuple[int, ...]
    output_format: str

    @property
    def rows(self) -> int:
        """One row per indicator x region x year: the dialog's long format."""

        return len(self.indicators) * max(1, len(self.regions)) * len(self.years)


def label_key(text: str) -> str:
    """One label, however EPS spaces it.

    The search tree writes 人均 地区生产总值 （元）; the selection tree writes
    人均地区生产总值（元）.  NFKC also folds full-width brackets.
    """

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def parse_year_range(text: str) -> tuple[str, str]:
    """``2010-2023`` or ``2015`` -> ("2010", "2023"); contiguous ranges only."""

    value = re.sub(r"\s*[-–~至]\s*", "-", (text or "").strip())
    match = re.fullmatch(r"(\d{4})(?:-(\d{4}))?", value)
    if not match:
        raise EPSRequestError("EPS_BAD_YEARS", f"{text!r} is not a year or a range like 2010-2023")
    first, last = match.group(1), match.group(2) or match.group(1)
    if int(first) > int(last):
        raise EPSRequestError("EPS_BAD_YEARS", f"{text!r}: the first year is after the last")
    return first, last


def validate_eps_request(request: DownloadRequest) -> EPSQuery:
    """One EPS query: a cube, named indicators, regions, a year range, a format.

    The cube is the ``cubeId`` in the page address; indicators are the
    tree's own labels (spacing aside); ``provinces`` stands for the 31
    provinces.  Requests the page would queue instead of returning at once
    are refused: split them by years or indicators.
    """

    if (request.database or "").strip().upper() != "EPS":
        raise EPSRequestError("EPS_WRONG_DATABASE", f"{request.database!r} is not EPS")
    cube = (request.table or "").strip()
    if not cube.isdigit():
        raise EPSRequestError(
            "EPS_BAD_CUBE", f"{cube!r}: --cube takes the number after cubeId= in the page address (e.g. 892)"
        )
    indicators = tuple(dict.fromkeys(i.strip() for i in request.fields if i and i.strip()))
    if not indicators:
        raise EPSRequestError("EPS_NO_INDICATOR", "name at least one indicator, as the EPS tree labels it (--indicator)")
    regions: list[str] = []
    for item in request.stocks:
        item = (item or "").strip()
        if not item:
            continue
        regions.extend(PROVINCES if item.casefold() == ALL_PROVINCES else [item])
    regions = list(dict.fromkeys(regions))
    if not request.date_start:
        raise EPSRequestError("EPS_BAD_YEARS", "name the years (--years 2010-2023)")
    first, last = parse_year_range(f"{request.date_start}-{request.date_end or request.date_start}")
    fmt = (request.output_format or "").strip().lower()
    if fmt not in EPS_FORMATS:
        raise EPSRequestError("EPS_UNKNOWN_FORMAT", f"format {fmt!r}; EPS offers {', '.join(EPS_FORMATS)}")
    query = EPSQuery(int(cube), indicators, tuple(regions), tuple(range(int(first), int(last) + 1)), fmt)
    if query.rows >= INSTANT_ROW_LIMIT:
        raise EPSRequestError(
            "EPS_REQUEST_TOO_LARGE",
            f"{query.rows:,} rows; EPS queues requests of {INSTANT_ROW_LIMIT:,} rows or more instead of returning "
            "them at once, and the harness only takes the ones it returns. Split the years or the indicators",
        )
    return query


def institutional_session(user_info: Mapping[str, Any]) -> bool:
    """The page's own account record says the campus account is signed in."""

    names = {str(user_info.get("groupName") or ""), str(user_info.get("realname") or "")}
    return INSTITUTION in names and str(user_info.get("isValid")) == "1"


def cube_access(user_info: Mapping[str, Any], cube_id: int) -> dict[str, Any] | None:
    for cube in user_info.get("cubes") or user_info.get("metaCubeList") or []:
        try:
            if int(cube.get("cubeId")) != int(cube_id):
                continue
        except (TypeError, ValueError):
            continue
        return {
            "name": str(cube.get("cubeNameZh") or ""),
            "visit": str(cube.get("visitAuth")) == "1",
            "download": str(cube.get("downloadAuth")) == "1",
        }
    return None


def parse_estimate(text: str) -> int | None:
    """``预估数据 868 行`` in the download dialog."""

    match = re.search(r"预估数据\s*([\d,]+)\s*行", text or "")
    return int(match.group(1).replace(",", "")) if match else None


def parse_dimension_counts(header_text: str) -> dict[str, int]:
    """The query header: 行维度 联动 指标 已选择 2 地区 已选择 31 列维度 时间 已选择 14 固定维度."""

    return {m.group(1): int(m.group(2)) for m in re.finditer(r"([^\s\d]+)\s*已选择\s*(\d+)", header_text or "")}


def cube_title_from_default_task_name(default_name: str) -> str:
    """The dialog proposes ``<cube title>_2026-09-29_0800``; the title is the cube's full name."""

    return re.sub(r"_\d{4}-\d{2}-\d{2}_\d{4}$", "", (default_name or "").strip())


def unique_task_name(cube_id: int, now: datetime) -> str:
    """The name the run's row is claimed by in the group's shared download list."""

    return f"HUNNU-Harness-EPS-{cube_id}-{now:%Y%m%d-%H%M%S}"


def year_labels(year: int) -> tuple[str, str]:
    """An annual cube's time member: ``2023`` (seen live) or ``2023年``."""

    return str(year), f"{year}年"


@dataclass(frozen=True)
class EPSTask:
    task_id: str
    name: str
    status: str
    reason: str
    rows: int | None
    created: str
    mode: str

    @property
    def completed(self) -> bool:
        return self.status.upper() == TASK_COMPLETED

    @property
    def invalid(self) -> bool:
        return self.status.upper() == TASK_INVALID

    def as_dict(self) -> dict[str, Any]:
        return {
            "TaskId": self.task_id,
            "TaskName": self.name,
            "Status": self.status,
            "StatusReason": self.reason,
            "Rows": self.rows,
            "Created": self.created,
            "Mode": self.mode,
        }


def _int(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_tasks(payload: Mapping[str, Any] | None) -> list[EPSTask]:
    """Rows of the download list.  File names and anything link-like are not kept."""

    tasks: list[EPSTask] = []
    for row in (payload or {}).get("list") or []:
        if not isinstance(row, Mapping):
            continue
        tasks.append(
            EPSTask(
                task_id=str(row.get("taskId") or ""),
                name=str(row.get("taskName") or ""),
                status=str(row.get("status") or ""),
                reason=str(row.get("statusReason") or ""),
                rows=_int(row.get("dataCount")),
                created=str(row.get("createTime") or ""),
                mode=str(row.get("downloadMode") or ""),
            )
        )
    return tasks


def claim_eps_task(tasks: Iterable[EPSTask], task_name: str) -> EPSTask | None:
    """This run's row: the one carrying its unique task name, or none yet."""

    mine = [task for task in tasks if task.name == task_name]
    if len(mine) > 1:
        raise EPSTaskError(
            "EPS_TASK_AMBIGUOUS",
            f"{len(mine)} rows are named {task_name!r}; the dialog was probably submitted twice. Nothing is archived",
        )
    return mine[0] if mine else None
