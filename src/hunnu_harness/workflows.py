from __future__ import annotations

from pathlib import Path
from typing import Any

from .downloads.manager import DownloadManager
from .models import DownloadRecord, DownloadRequest


async def run_cnrds_download(adapter: Any, manager: DownloadManager, request: DownloadRequest) -> DownloadRecord:
    """Run a guarded CNRDS query and archive the resulting original file."""
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
