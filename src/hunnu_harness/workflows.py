from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .databases.cnrds_catalog import cnrds_view_url, validate_view_request
from .downloads.manager import DownloadManager
from .models import DownloadRecord, DownloadRequest


async def run_cnrds_download(adapter: Any, manager: DownloadManager, request: DownloadRequest) -> DownloadRecord:
    """Run a guarded CNRDS query and archive the resulting original file.

    An adapter that knows the table-page route takes it for every subscribed
    table (CNFS included); the v0.1 menu route remains for an adapter that
    predates it.
    """

    if hasattr(adapter, "open_view"):
        return await run_cnrds_view_download(adapter, manager, request)
    await adapter.open()
    await adapter.open_table(request.table)
    if request.stocks:
        await adapter.select_stocks(list(request.stocks))
    if request.date_start and request.date_end:
        await adapter.select_date_range(request.date_start, request.date_end)
    if request.fields:
        await adapter.select_fields(list(request.fields))
    await adapter.preview()
    baseline = manager.snapshot()
    returned_path = await adapter.download(request)
    if returned_path:
        download_path = Path(returned_path)
    else:
        download_path = manager.wait_for_new_download(baseline)
    return manager.archive_file(download_path, request, source_url=request.source_url)


async def run_cnrds_view_download(
    adapter: Any,
    manager: DownloadManager,
    request: DownloadRequest,
    *,
    queue_timeout_seconds: float = 900.0,
    download_timeout_seconds: float = 600.0,
    on_task: Any = None,
    collect_task_id: str | None = None,
) -> DownloadRecord:
    """Download one whole CNRDS table through the school account's download queue.

    Order matters and every step can stop the run: the request is validated
    before the browser is touched; the account's subscription and the table
    page are confirmed before any control is pressed; the download summary must
    confirm table, period, codes and format before anything is queued; a
    dialog that would lead to trial or partial data ends the run instead of
    being clicked through; the queued task is claimed by difference, never as
    "the newest one"; and the file is archived with its SHA-256 and manifest.
    The manifest's source is the table page, never the signed storage URL the
    file came from.

    ``collect_task_id`` collects a task an earlier run queued but did not wait
    out, instead of queueing the same table again on a shared account.
    """

    db, table, fmt = validate_view_request(request)
    await adapter.open_view(db, table)
    if collect_task_id:
        task = await adapter.find_finished_task(collect_task_id, db, table)
    else:
        applied = await adapter.choose_whole_table(fmt)
        before, _clicked_at = await adapter.queue_download(table, fmt, applied)
        task = await adapter.wait_for_task(before, table, timeout_seconds=queue_timeout_seconds, db=db)
    if on_task is not None:
        on_task(task)
    baseline = manager.snapshot()
    await adapter.fetch_task_file(task)
    download_path = manager.wait_for_new_download(baseline, timeout_seconds=download_timeout_seconds)
    source = cnrds_view_url(db, table)
    archived_request = replace(request, module=db, table=table, output_format=fmt, source_url=source)
    return manager.archive_file(download_path, archived_request, source_url=source)


async def run_resset_download(
    adapter: Any,
    manager: DownloadManager,
    request: DownloadRequest,
    *,
    human_wait_seconds: float = 600.0,
    download_timeout_seconds: float = 900.0,
    on_ready: Any = None,
    on_task: Any = None,
    collect_task_id: str | None = None,
) -> DownloadRecord:
    """Download one whole RESSET table; the CAPTCHA and the 下载数据 press are a person's.

    The request is validated before the browser is touched; the institutional
    sign-in and the account's permitted databases are confirmed; the table is
    resolved by its code inside a permitted database; the form is prepared and
    read back as a whole-table request; then ``on_ready`` tells the person to
    type the CAPTCHA and press 下载数据, and the run only watches the download
    centre until the new row is ready.  The file is archived with its SHA-256
    and manifest; the manifest's source is the table page, never a download token.
    """

    from urllib.parse import urljoin

    from .databases.resset import RESSET_TABLE_BASE
    from .databases.resset_catalog import validate_resset_request

    code, output_type = validate_resset_request(request)
    await adapter.open()
    hit = await adapter.resolve_table(code)
    if collect_task_id:
        await adapter.open_table(hit)  # reads the table's 总记录数 for the whole-table check
        task = await adapter.find_task(collect_task_id, hit, output_type)
    else:
        await adapter.open_table(hit)
        await adapter.prepare_whole_table(output_type)
        before = await adapter.list_tasks()
        present = getattr(adapter, "present_for_person", None)
        if present is not None:
            await present()
        if on_ready is not None:
            on_ready(hit)
        task = await adapter.wait_for_person_and_task(before, hit, timeout_seconds=human_wait_seconds)
    if on_task is not None:
        on_task(task)
    baseline = manager.snapshot()
    await adapter.fetch_task_file(task)
    download_path = manager.wait_for_new_download(baseline, timeout_seconds=download_timeout_seconds)
    source = urljoin(RESSET_TABLE_BASE, hit.href)
    # The archive records the format the download centre says the file has,
    # which a person's page may have changed after the form was prepared.
    recorded_format = (getattr(task, "fmt", "") or "").strip() or output_type
    archived_request = replace(
        request, database="RESSET", module=hit.database, table=code, output_format=recorded_format, source_url=source
    )
    return manager.archive_file(download_path, archived_request, source_url=source)


async def run_eps_download(
    adapter: Any,
    manager: DownloadManager,
    request: DownloadRequest,
    *,
    human_wait_seconds: float = 600.0,
    download_timeout_seconds: float = 180.0,
    on_ready: Any = None,
    on_task: Any = None,
    now: Any = None,
) -> DownloadRecord:
    """One EPS query; the 确认提交 press is a person's.

    The request is validated before the browser is touched.  The campus-IP
    account and its right to the cube are confirmed, every requested member
    is ticked and the header's counts read back, and the download dialog is
    filled with a task name unique to this run.  Then ``on_ready`` tells the
    person to press 确认提交, and the run only follows its own row in the
    group's download list.  The file the page hands the browser is archived
    with its SHA-256 and manifest; the manifest's source is the cube page.
    """

    from datetime import datetime

    from .databases.eps_catalog import EPS_CUBE_URL, unique_task_name, validate_eps_request

    query = validate_eps_request(request)
    await adapter.open()
    dimensions = await adapter.open_cube(query)
    await adapter.select_query(query, dimensions)
    task_name = unique_task_name(query.cube_id, now or datetime.now())
    dialog = await adapter.open_download_dialog(query, task_name)
    baseline = manager.snapshot()
    present = getattr(adapter, "present_for_person", None)
    if present is not None:
        await present()
    if on_ready is not None:
        on_ready(dialog)
    task = await adapter.wait_for_person_and_task(task_name, timeout_seconds=human_wait_seconds)
    if on_task is not None:
        on_task(task)
    download_path = manager.wait_for_new_download(baseline, timeout_seconds=download_timeout_seconds)
    source = EPS_CUBE_URL.format(cube_id=query.cube_id)
    # The shared manifest schema names its member list "stocks"; for EPS it
    # carries the regions, and "fields" carries the indicators.
    archived_request = replace(
        request,
        database="EPS",
        module=dialog.get("cube_title") or f"EPS cube {query.cube_id}",
        table=str(query.cube_id),
        stocks=query.regions,
        fields=query.indicators,
        date_start=str(query.years[0]),
        date_end=str(query.years[-1]),
        output_format=Path(download_path).suffix.lstrip(".").lower() or query.output_format,
        source_url=source,
    )
    return manager.archive_file(download_path, archived_request, source_url=source)
