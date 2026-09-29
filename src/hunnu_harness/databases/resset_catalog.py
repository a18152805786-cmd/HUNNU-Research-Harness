"""RESSET through the HUNNU library: pure rules for reading its pages.

Verified live on 2026-09-29.  The library's database navigation lists
"RESSET金融研究数据库" (detail page id 27560); its 网络地址 opens
``db.resset.com``, which signs the browser in as the library's institutional
account on its own -- the page then shows 帐户类别 机构用户 and 湖南师范大学.
No credential is typed by anyone on that route.  (The detail page also
publishes a shared username and password; agents never enter them.)

Downloads differ from CNRDS in two ways that matter:

* the download form ends in a four-character image CAPTCHA.  A person reads
  and types it and presses 下载数据; the harness prepares the form before and
  collects the file after, and never touches the CAPTCHA;
* the download centre lists tasks per *browser* (a ``browserId`` kept in the
  page's local storage), not per account, and keeps each link for 48 hours.

Everything here is pure; ``resset.py`` drives the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import quote

from ..models import DownloadRequest

RESSET_MAIN_URL = "https://db.resset.com/common/main.jsp"
RESSET_DOWNLOAD_CENTRE_URL = "https://db.resset.com/db/download/downloadTask.jsp"
RESSET_TASK_LIST_ACTION = "/db/download/downloadTaskUser.action"
RESSET_STOCK_DB_MSG_ID = "4028818a2206ecbc012206edd0d10001"  # RESSET 股票

HUNNU_LIBRARY_ROUTE = (
    "https://lib.hunnu.edu.cn/ -> 数据库导航 -> RESSET金融研究数据库 -> 网络地址 -> "
    "db.resset.com (signs in as the library's institutional account; nothing is typed)"
)

#: Output formats a request may name, mapped to the form's ``outputType`` value.
RESSET_FORMATS: dict[str, str] = {
    "dta": "stata17",
    "stata12": "stata12",
    "csv": "csvutf",
    "xlsx": "excel2007",
    "xls": "excel",
    "txt": "tabtxt",
}

#: Tables verified to exist in the HUNNU subscription (RESSET 股票) that the
#: 《危与机》 replication looks for.  Any table code can be requested; these
#: are documentation, not a whitelist.
KNOWN_TABLES: dict[str, str] = {
    "EMPINFO": "员工构成信息",
    "MAJSALECUSTSUP": "财务附注_主要销售客户和供应商",
    "PURANDSALE": "采销情况",
    "SUPPCUSTD": "公司供应商与客户",
    "CACTCTRL": "公司实际控制人",
}


class RESSETRequestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def normalize_table_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", str(value or "")).upper()


def validate_resset_request(request: DownloadRequest) -> tuple[str, str]:
    """``(table_code, outputType)``; whole tables only, like the CNRDS route."""

    if str(request.database or "").strip().upper() != "RESSET":
        raise RESSETRequestError("RESSET_WRONG_SOURCE", f"not a RESSET request: {request.database!r}")
    code = normalize_table_code(request.table)
    if not code:
        raise RESSETRequestError("RESSET_UNKNOWN_TABLE", "Table must be a RESSET table code such as EMPINFO")
    if request.stocks or request.fields or request.date_start or request.date_end:
        raise RESSETRequestError(
            "RESSET_SUBSET_UNSUPPORTED",
            "only whole tables are supported (all periods, all companies, all fields); filter after download",
        )
    fmt = str(request.output_format or "dta").strip().lower()
    if fmt not in RESSET_FORMATS:
        raise RESSETRequestError("RESSET_UNKNOWN_FORMAT", f"output format must be one of {sorted(RESSET_FORMATS)}")
    return code, RESSET_FORMATS[fmt]


def search_url(keyword: str, db_msg_id: str = RESSET_STOCK_DB_MSG_ID) -> str:
    return (
        "/db/table/searchMsgSolrList.jsp?sAction=true&dbMsgId="
        + db_msg_id
        + "&databaseId=&searchScope=all&searchType=fuzzy&queryMsg="
        + quote(keyword)
        + "&language=cn&hf="
    )


def institutional_session(body_text: str) -> bool:
    """The main page's user block for the library's institutional account."""

    text = str(body_text or "")
    return "机构用户" in text and "湖南师范大学" in text


def parse_permitted_databases(body_text: str) -> list[str]:
    """Database names from the main page's 权限数据库 box (股票, 债券, ...).

    The box reads ``权限数据库 (共16个库)`` and then the names, separated by
    tabs and line breaks; the green 已订 marks beside them are images, so they
    are not in the text.  When the header states a count, the list must match
    it, or nothing is returned.
    """

    text = str(body_text or "")
    start = text.find("权限数据库")
    if start < 0:
        return []
    end = text.find("点击查看详细数据库权限", start)
    block = text[start:end if end > 0 else start + 1500]
    header = re.match(r"权限数据库\s*[（(]\s*共\s*(\d+)\s*个库\s*[)）]", block)
    body = block[header.end():] if header else block[len("权限数据库"):]
    names = [part.strip() for part in re.split(r"[\t\n\r]+", body)]
    names = [name for name in names if name and not name.startswith("→") and "已订" not in name]
    if header and len(names) != int(header.group(1)):
        return []
    return names


@dataclass(frozen=True)
class TableHit:
    database: str
    title: str
    code: str
    href: str


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.anchors.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def parse_table_hits(search_html: str) -> list[TableHit]:
    """Table links from a search result page: ``RESSET 股票 - 员工构成信息 ( EMPINFO )``."""

    collector = _AnchorCollector()
    collector.feed(str(search_html or ""))
    hits: list[TableHit] = []
    for href, text in collector.anchors:
        if "dataSearch.jsp" not in href:
            continue
        match = re.search(r"(RESSET[^-]*?)\s*-\s*(.+?)\s*\(\s*([A-Za-z0-9_]+)\s*\)\s*$", text)
        if not match:
            continue
        hits.append(TableHit(match.group(1).strip(), match.group(2).strip(), match.group(3).upper(), href))
    return hits


def pick_table(hits: Iterable[TableHit], code: str, permitted: Iterable[str]) -> TableHit:
    """The one hit for ``code`` inside a database the account is permitted."""

    wanted = normalize_table_code(code)
    allowed = {name.replace(" ", "") for name in permitted}
    exact = [hit for hit in hits if hit.code == wanted]
    if not exact:
        raise RESSETRequestError("RESSET_TABLE_NOT_FOUND", f"no RESSET table with code {wanted}")
    usable = [hit for hit in exact if hit.database.replace("RESSET", "").replace(" ", "") in allowed]
    if not usable:
        raise RESSETRequestError(
            "RESSET_NOT_SUBSCRIBED",
            f"{wanted} lives in {exact[0].database}, which is not among the account's 权限数据库",
        )
    unique = {hit.href: hit for hit in usable}
    if len(unique) > 1:
        raise RESSETRequestError("RESSET_TABLE_AMBIGUOUS", f"{len(unique)} permitted tables carry code {wanted}")
    return next(iter(unique.values()))


# ---------------------------------------------------------------------------
# Download centre
# ---------------------------------------------------------------------------
#
# Rows are ``tr.tableShow``: 序号, 下载时间, 数据集名称, 下载链接生成情况, 格式,
# 大小（MB）, 数据量（条）, 链接.  The link cell calls
# ``downloadtask(id, token, path, button)``; the token and path are dropped on
# first read so that no report or manifest can carry them -- only the row id,
# which the page's own button needs, is kept.


@dataclass(frozen=True)
class RessetTask:
    task_id: str
    name: str
    created: str
    status: str
    fmt: str
    size_mb: str
    rows: str

    @property
    def finished(self) -> bool:
        # A row whose download button exists is ready whatever its status says.
        has_button = bool(self.task_id) and not self.task_id.startswith("row:")
        return has_button or self.status_ready(self.status)

    @staticmethod
    def status_ready(status: str) -> bool:
        # Found live on 2026-09-29: a finished row's 下载链接生成情况 reads "100%",
        # not 已完成 -- the run waited out a file that had been ready for minutes.
        text = str(status or "").strip()
        if "未" in text or "失败" in text:
            return False
        return text.startswith("100%") or any(marker in text for marker in ("完成", "成功", "已生成"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "TaskId": self.task_id,
            "DatasetName": self.name,
            "Created": self.created,
            "Status": self.status,
            "Format": self.fmt,
            "SizeMB": self.size_mb,
            "Rows": self.rows,
            "Finished": self.finished,
        }


class _RowCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self._row: dict[str, Any] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr" and "tableShow" in (attributes.get("class") or ""):
            self._row = {"cells": [], "onclicks": []}
        elif self._row is not None and tag == "td":
            self._cell = []
        if self._row is not None:
            onclick = attributes.get("onclick") or attributes.get("href") or ""
            if "downloadtask" in onclick:
                self._row["onclicks"].append(onclick)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._cell is not None:
            self._row["cells"].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def parse_task_rows(html: str) -> list[RessetTask]:
    """Download-centre rows, with download tokens and paths discarded."""

    collector = _RowCollector()
    collector.feed(str(html or ""))
    tasks: list[RessetTask] = []
    for row in collector.rows:
        cells = row["cells"] + [""] * 8
        task_id = ""
        for onclick in row["onclicks"]:
            match = re.search(r"downloadtask\(\s*['\"]?([^,'\"]+)['\"]?\s*,", onclick)
            if match:
                task_id = match.group(1).strip()
                break
        tasks.append(
            RessetTask(
                task_id=task_id or f"row:{cells[0]}:{cells[1]}",
                name=cells[2],
                created=cells[1],
                status=cells[3],
                fmt=cells[4],
                size_mb=cells[5],
                rows=cells[6],
            )
        )
    return tasks


class RESSETQueueError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def parse_total_records(body_text: str) -> int | None:
    """The table page's 总记录数 (e.g. ``总记录数 939,736``)."""

    match = re.search(r"总记录数\s*([\d,]+)", str(body_text or ""))
    return int(match.group(1).replace(",", "")) if match else None


def whole_table_problem(task: "RessetTask", total_records: int | None, output_type: str) -> str | None:
    """Why a claimed row is not the whole table that was prepared, or ``None``.

    Found live on 2026-09-29: the person typed the CAPTCHA in another tab of
    the same table, still at the page's defaults (last month, Excel2007); the
    row that came back had 3,912 rows of the table's 939,736.  A row whose
    size or format does not match the prepared request is not archived.
    """

    if format_matches(output_type, task.fmt) is False:
        return f"the row's format is {task.fmt!r}, not the prepared {output_type!r}"
    try:
        rows = int(str(task.rows).replace(",", "").strip())
    except ValueError:
        rows = None
    if total_records and rows is not None and rows < 0.99 * total_records:
        return f"the row holds {rows:,} records of the table's {total_records:,}"
    return None


def claim_resset_task(before: Iterable[RessetTask], after: Iterable[RessetTask], code: str, title: str) -> RessetTask | None:
    """The new download-centre row for this table, or ``None`` while there is none.

    New means absent before the person pressed 下载数据 (compared by created
    time and name, since a row has no id until its file exists).
    """

    seen = {(task.created, task.name) for task in before}
    fresh = [
        task
        for task in after
        if (task.created, task.name) not in seen and (code in task.name.upper() or (title and title in task.name))
    ]
    unique = {(task.created, task.name): task for task in fresh}
    if len(unique) > 1:
        raise RESSETQueueError("RESSET_TASK_AMBIGUOUS", f"{len(unique)} new download-centre rows for {code}")
    return next(iter(unique.values()), None)


#: What the download centre's 格式 column may say for each requested outputType.
FORMAT_TOKENS: dict[str, tuple[str, ...]] = {
    "stata17": ("stata17", "dta"),
    "stata12": ("stata12", "dta"),
    "csvutf": ("csv",),
    "excel2007": ("excel2007", "xlsx"),
    "excel": ("excel", "xls"),
    "tabtxt": ("txt",),
}


def format_matches(requested: str, shown: str) -> bool | None:
    """Does the row's 格式 match what was asked?  ``None`` when the row does not say.

    Found live on 2026-09-29: the form was read back as STATA17 before the
    person pressed 下载数据, yet the page then showed Excel2007 selected.  The
    row's own column, not the request, is what the archive must record.
    """

    text = str(shown or "").strip().casefold()
    if not text:
        return None
    return any(token in text for token in FORMAT_TOKENS.get(requested, (requested,)))
