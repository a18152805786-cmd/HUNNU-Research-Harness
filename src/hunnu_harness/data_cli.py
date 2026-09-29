"""``hunnu-harness data-acquire``: one whole CNRDS or RESSET table, or one EPS query, end to end.

The data counterpart of ``acquire``.  It only ever attaches to the running
Research Chrome (start it with ``browser-start``; the sign-ins there are the
user's), holds the Research Chrome lock for the run, and answers on the
graded exit ladder in ``exit_codes``: 2 whenever a person has to act -- sign
in, subscribe, register a personal account, type a RESSET CAPTCHA, press
EPS's 确认提交, or decide after a throttle warning -- and never retries past it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .cli_output import CliReport
from .databases.cnrds_catalog import (
    HUNNU_LIBRARY_ROUTE,
    SCHOOL_ACCOUNT_BASE_DATABASES,
    CNRDSQueueError,
    CNRDSRequestError,
    cnrds_view_url,
    validate_view_request,
)
from .exit_codes import (
    EXIT_CAPABILITY_MISSING,
    EXIT_ENV_NOT_READY,
    EXIT_HUMAN_ACTION_REQUIRED,
    EXIT_OK,
    EXIT_RUN_FAILED,
)
from .models import DownloadRequest
from .paths import RUNS_ROOT, _windows_io_path

DEFAULT_PROFILE = Path(os.environ.get("HUNNU_RESEARCH_PROFILE", Path.home() / "ResearchHarness" / "chrome-profile"))


def add_data_acquire_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "data-acquire",
        help=(
            "Download one whole CNRDS or RESSET table, or one EPS query, that the HUNNU institutional accounts "
            "hold, in the running Research Chrome (attach-only; archived with SHA-256 and a manifest)"
        ),
    )
    parser.add_argument(
        "--database", default="CNRDS", choices=("CNRDS", "RESSET", "EPS", "cnrds", "resset", "eps")
    )
    parser.add_argument(
        "--module",
        default="",
        help="CNRDS only: database code, e.g. CNSP, CNFS, CERD, CRED (see docs/INSTITUTIONAL_DATA_ACCESS.md)",
    )
    parser.add_argument(
        "--table",
        default="",
        help="CNRDS: the table's name (ViewName), e.g. 个股年回报率; RESSET: the table code, e.g. EMPINFO",
    )
    parser.add_argument("--cube", default="", help="EPS: the cube id after cubeId= in the page address, e.g. 892")
    parser.add_argument(
        "--indicator",
        action="append",
        default=[],
        help="EPS: an indicator exactly as the EPS tree labels it (spacing aside); repeat for more",
    )
    parser.add_argument(
        "--regions",
        default="",
        help="EPS: 'provinces' for the 31 provinces, or comma-separated region names; omit for cubes without 地区",
    )
    parser.add_argument("--years", default="", help="EPS: a year or a range, e.g. 2010-2023")
    parser.add_argument("--format", default="dta", help="dta (default), csv, xlsx, txt, xls, xml or html")
    parser.add_argument("--run-root", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument(
        "--queue-timeout-minutes",
        type=float,
        default=15.0,
        help="How long to wait for CNRDS to prepare the file in the shared download queue",
    )
    parser.add_argument(
        "--collect-task",
        default=None,
        help="Collect a finished task an earlier run queued (its DownloadTaskId) instead of queueing the table again",
    )
    parser.add_argument(
        "--human-wait-minutes",
        type=float,
        default=10.0,
        help=(
            "RESSET and EPS: how long to wait for a person to act (RESSET: type the CAPTCHA and press 下载数据; "
            "EPS: press 确认提交)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate the request only; no browser")


def _request(args: argparse.Namespace) -> DownloadRequest:
    database = str(args.database or "CNRDS").strip().upper()
    if database == "EPS":
        from .databases.eps_catalog import EPSRequestError, parse_year_range

        years = str(getattr(args, "years", "") or "").strip()
        try:
            first, last = parse_year_range(years) if years else ("", "")
        except EPSRequestError:
            first, last = years, None  # validate_eps_request reports it
        regions = tuple(r.strip() for r in str(getattr(args, "regions", "") or "").replace("，", ",").split(",") if r.strip())
        return DownloadRequest(
            database="EPS",
            module="",
            table=str(getattr(args, "cube", "") or "").strip(),
            stocks=regions,
            date_start=first or None,
            date_end=last or None,
            fields=tuple(str(i) for i in (getattr(args, "indicator", None) or [])),
            output_format=str(args.format or "dta").strip().lower(),
        )
    return DownloadRequest(
        database=database,
        module=str(args.module or "").strip(),
        table=str(args.table or "").strip(),
        output_format=str(args.format or "dta").strip().lower(),
    )


def run_data_acquire_cli(args: argparse.Namespace) -> int:
    report = CliReport(bool(getattr(args, "json", False)))
    try:
        code = asyncio.run(_run(args, report))
    finally:
        report.flush()
    return code


async def _run(args: argparse.Namespace, report: CliReport) -> int:
    request = _request(args)
    if request.database == "EPS":
        return await _run_eps(args, report, request)
    if not request.table:
        report.put("Database", request.database)
        report.put("Status", "INVALID_REQUEST")
        report.put("Reason", f"--table is required for {request.database}")
        return EXIT_RUN_FAILED
    if request.database == "RESSET":
        return await _run_resset(args, report, request)
    report.put("Database", request.database)
    report.put("Module", request.module)
    report.put("Table", request.table)
    try:
        db, table, fmt = validate_view_request(request)
    except CNRDSRequestError as exc:
        report.put("Status", "UNSUPPORTED_CAPABILITY" if exc.code != "CNRDS_UNKNOWN_FORMAT" else "INVALID_REQUEST")
        report.put("Reason", str(exc))
        report.put("HarnessCapabilityAvailable", False, plain="false")
        report.put("MissingCapability", exc.code)
        return EXIT_RUN_FAILED
    report.put("TablePage", cnrds_view_url(db, table))
    report.put("DatabaseNameCN", SCHOOL_ACCOUNT_BASE_DATABASES[db][0])
    report.put("OutputFormat", fmt)
    if getattr(args, "dry_run", False):
        report.put("Status", "VALIDATED")
        report.put("DryRun", True, plain="true")
        return EXIT_OK

    from .browser.playwright_backend import (
        PlaywrightBrowser,
        PlaywrightUnavailable,
        ProfileLockedError,
        ResearchChromeNotRunning,
    )
    from .browser.research_chrome_lock import ResearchChromeBusy
    from .databases.cnrds import CNRDSAdapter, CNRDSGate
    from .downloads.manager import DownloadManager, DownloadTimeout
    from .workflows import run_cnrds_view_download

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = Path(args.run_root) if args.run_root else RUNS_ROOT / "DataAcquire" / f"{stamp}_{db}"
    staging = run_root / "downloads" / "staging"
    _windows_io_path(staging).mkdir(parents=True, exist_ok=True)
    report.put("RunRoot", str(run_root))
    browser = PlaywrightBrowser(profile_dir=args.profile, downloads_dir=staging, require_attach=True)
    manager = DownloadManager(watch_dirs=(staging,))
    claimed: dict[str, Any] = {}
    try:
        await browser.start()
        adapter = CNRDSAdapter(browser)
        record = await run_cnrds_view_download(
            adapter,
            manager,
            request,
            queue_timeout_seconds=max(60.0, float(args.queue_timeout_minutes) * 60.0),
            on_task=lambda task: claimed.update(task.as_dict()),
            collect_task_id=getattr(args, "collect_task", None),
        )
    except CNRDSGate as gate:
        report.put("Status", gate.code)
        report.put("Reason", str(gate))
        if claimed:
            report.put("ClaimedTask", claimed)
        if gate.code == "ACTION_REQUIRED_USER_LOGIN":
            report.put("ACTION_REQUIRED_USER_LOGIN", True, plain="true")
            report.put("LibraryRoute", HUNNU_LIBRARY_ROUTE)
        return EXIT_HUMAN_ACTION_REQUIRED if gate.human else EXIT_ENV_NOT_READY
    except CNRDSQueueError as exc:
        report.put("Status", exc.code)
        report.put("Reason", str(exc))
        return EXIT_RUN_FAILED
    except DownloadTimeout as exc:
        report.put("Status", "DOWNLOAD_TIMEOUT")
        report.put("Reason", str(exc))
        if claimed:
            report.put("ClaimedTask", claimed)
        return EXIT_RUN_FAILED
    except ProfileLockedError as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_ENV_NOT_READY
    except (ResearchChromeBusy, ResearchChromeNotRunning) as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_ENV_NOT_READY
    except PlaywrightUnavailable as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_CAPABILITY_MISSING
    except Exception as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:300])
        return EXIT_ENV_NOT_READY
    finally:
        try:
            await browser.close()
        except Exception:
            pass
    report.put("Status", "SUCCESS")
    report.put("ClaimedTask", claimed)
    report.put("ArchivedPath", record.archived_path)
    report.put("SHA256", record.sha256)
    report.put("OriginalFilename", record.original_filename)
    report.put("SourcePage", record.source_url)
    return EXIT_OK


async def _run_resset(args: argparse.Namespace, report: CliReport, request: DownloadRequest) -> int:
    from .databases.resset_catalog import (
        HUNNU_LIBRARY_ROUTE as RESSET_LIBRARY_ROUTE,
        KNOWN_TABLES,
        RESSETQueueError,
        RESSETRequestError,
        validate_resset_request,
    )

    report.put("Database", "RESSET")
    report.put("Table", request.table)
    try:
        code, output_type = validate_resset_request(request)
    except RESSETRequestError as exc:
        report.put("Status", "UNSUPPORTED_CAPABILITY" if exc.code != "RESSET_UNKNOWN_FORMAT" else "INVALID_REQUEST")
        report.put("Reason", str(exc))
        report.put("HarnessCapabilityAvailable", False, plain="false")
        report.put("MissingCapability", exc.code)
        return EXIT_RUN_FAILED
    report.put("TableCode", code)
    if code in KNOWN_TABLES:
        report.put("TableNameCN", KNOWN_TABLES[code])
    report.put("OutputType", output_type)
    if getattr(args, "dry_run", False):
        report.put("Status", "VALIDATED")
        report.put("DryRun", True, plain="true")
        return EXIT_OK

    import sys

    from .browser.playwright_backend import (
        PlaywrightBrowser,
        PlaywrightUnavailable,
        ProfileLockedError,
        ResearchChromeNotRunning,
    )
    from .browser.research_chrome_lock import ResearchChromeBusy
    from .databases.resset import RESSETAdapter, RESSETGate
    from .downloads.manager import DownloadManager, DownloadTimeout
    from .workflows import run_resset_download

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = Path(args.run_root) if args.run_root else RUNS_ROOT / "DataAcquire" / f"{stamp}_RESSET_{code}"
    staging = run_root / "downloads" / "staging"
    _windows_io_path(staging).mkdir(parents=True, exist_ok=True)
    report.put("RunRoot", str(run_root))
    browser = PlaywrightBrowser(profile_dir=args.profile, downloads_dir=staging, require_attach=True)
    manager = DownloadManager(watch_dirs=(staging,))
    claimed: dict[str, Any] = {}
    wait_seconds = max(60.0, float(getattr(args, "human_wait_minutes", 10.0)) * 60.0)

    def ready(hit: Any) -> None:
        # Said the moment the form is ready, not in the final report: the
        # person has to act while this run is still waiting.
        print(
            f"ACTION_REQUIRED_USER_CAPTCHA: in the Research Chrome's {hit.title} ({hit.code}) page, type the "
            f"4-character 验证码 and press 下载数据 within {int(wait_seconds // 60)} minutes.",
            file=sys.stderr,
            flush=True,
        )
        report.put("HumanStepAnnounced", f"{hit.database} / {hit.title} ({hit.code})")

    try:
        await browser.start()
        adapter = RESSETAdapter(browser)
        record = await run_resset_download(
            adapter,
            manager,
            request,
            human_wait_seconds=wait_seconds,
            on_ready=ready,
            on_task=lambda task: claimed.update(task.as_dict()),
            collect_task_id=getattr(args, "collect_task", None),
        )
    except RESSETGate as gate:
        report.put("Status", gate.code)
        report.put("Reason", str(gate))
        if gate.code == "ACTION_REQUIRED_USER_LOGIN":
            report.put("ACTION_REQUIRED_USER_LOGIN", True, plain="true")
            report.put("LibraryRoute", RESSET_LIBRARY_ROUTE)
        return EXIT_HUMAN_ACTION_REQUIRED if gate.human else EXIT_ENV_NOT_READY
    except (RESSETQueueError, RESSETRequestError) as exc:
        report.put("Status", exc.code)
        report.put("Reason", str(exc))
        if claimed:
            report.put("ClaimedTask", claimed)
        return EXIT_RUN_FAILED
    except DownloadTimeout as exc:
        report.put("Status", "DOWNLOAD_TIMEOUT")
        report.put("Reason", str(exc))
        if claimed:
            report.put("ClaimedTask", claimed)
        return EXIT_RUN_FAILED
    except ProfileLockedError as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_ENV_NOT_READY
    except (ResearchChromeBusy, ResearchChromeNotRunning) as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_ENV_NOT_READY
    except PlaywrightUnavailable as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_CAPABILITY_MISSING
    except Exception as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:300])
        return EXIT_ENV_NOT_READY
    finally:
        try:
            await browser.close()
        except Exception:
            pass
    from .databases.resset_catalog import format_matches

    report.put("Status", "SUCCESS")
    report.put("ClaimedTask", claimed)
    if format_matches(output_type, str(claimed.get("Format", ""))) is False:
        report.put("FormatMismatch", {"Requested": output_type, "DownloadCentre": claimed.get("Format")})
    report.put("ArchivedPath", record.archived_path)
    report.put("SHA256", record.sha256)
    report.put("OriginalFilename", record.original_filename)
    report.put("SourcePage", record.source_url)
    return EXIT_OK


async def _run_eps(args: argparse.Namespace, report: CliReport, request: DownloadRequest) -> int:
    from .databases.eps_catalog import (
        EPS_CUBE_URL,
        HUNNU_LIBRARY_ROUTE as EPS_LIBRARY_ROUTE,
        EPSRequestError,
        EPSTaskError,
        validate_eps_request,
    )

    report.put("Database", "EPS")
    report.put("Cube", request.table)
    try:
        query = validate_eps_request(request)
    except EPSRequestError as exc:
        report.put("Status", "UNSUPPORTED_CAPABILITY" if exc.code == "EPS_REQUEST_TOO_LARGE" else "INVALID_REQUEST")
        report.put("Reason", str(exc))
        if exc.code == "EPS_REQUEST_TOO_LARGE":
            report.put("HarnessCapabilityAvailable", False, plain="false")
            report.put("MissingCapability", exc.code)
        return EXIT_RUN_FAILED
    report.put("CubePage", EPS_CUBE_URL.format(cube_id=query.cube_id))
    report.put("Indicators", list(query.indicators))
    report.put("Regions", len(query.regions))
    report.put("Years", f"{query.years[0]}-{query.years[-1]}")
    report.put("ExpectedRows", query.rows)
    report.put("OutputFormat", query.output_format)
    if getattr(args, "dry_run", False):
        report.put("Status", "VALIDATED")
        report.put("DryRun", True, plain="true")
        return EXIT_OK

    import sys

    from .browser.playwright_backend import (
        PlaywrightBrowser,
        PlaywrightUnavailable,
        ProfileLockedError,
        ResearchChromeNotRunning,
    )
    from .browser.research_chrome_lock import ResearchChromeBusy
    from .databases.base import UnexpectedPageState
    from .databases.eps import EPSAdapter, EPSGate
    from .downloads.manager import DownloadManager, DownloadTimeout
    from .workflows import run_eps_download

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = Path(args.run_root) if args.run_root else RUNS_ROOT / "DataAcquire" / f"{stamp}_EPS_{query.cube_id}"
    staging = run_root / "downloads" / "staging"
    _windows_io_path(staging).mkdir(parents=True, exist_ok=True)
    report.put("RunRoot", str(run_root))
    browser = PlaywrightBrowser(profile_dir=args.profile, downloads_dir=staging, require_attach=True)
    manager = DownloadManager(watch_dirs=(staging,))
    claimed: dict[str, Any] = {}
    wait_seconds = max(60.0, float(getattr(args, "human_wait_minutes", 10.0)) * 60.0)

    def ready(dialog: dict[str, Any]) -> None:
        # Said the moment the dialog is ready: the person acts while the run waits.
        print(
            f"ACTION_REQUIRED_USER_SUBMIT: in the Research Chrome's EPS page, check the 下载 dialog "
            f"({dialog.get('cube_title')}; task {dialog.get('task_name')}; {dialog.get('estimate'):,} rows; "
            f"{query.output_format}) and press 确认提交 within {int(wait_seconds // 60)} minutes. "
            "Keep the task name as it is: the run finds its file by it.",
            file=sys.stderr,
            flush=True,
        )
        report.put("HumanStepAnnounced", dialog.get("task_name"))
        report.put("DialogEstimate", dialog.get("estimate"))

    try:
        await browser.start()
        adapter = EPSAdapter(browser)
        record = await run_eps_download(
            adapter,
            manager,
            request,
            human_wait_seconds=wait_seconds,
            on_ready=ready,
            on_task=lambda task: claimed.update(task.as_dict()),
        )
    except EPSGate as gate:
        report.put("Status", gate.code)
        report.put("Reason", str(gate))
        if gate.code == "ACTION_REQUIRED_CAMPUS_NETWORK":
            report.put("LibraryRoute", EPS_LIBRARY_ROUTE)
        return EXIT_HUMAN_ACTION_REQUIRED if gate.human else EXIT_RUN_FAILED
    except (EPSRequestError, EPSTaskError) as exc:
        report.put("Status", exc.code)
        report.put("Reason", str(exc))
        if claimed:
            report.put("ClaimedTask", claimed)
        return EXIT_RUN_FAILED
    except UnexpectedPageState as exc:
        report.put("Status", "SOURCE_LAYOUT_CHANGED")
        report.put("Reason", str(exc))
        return EXIT_RUN_FAILED
    except DownloadTimeout as exc:
        report.put("Status", "DOWNLOAD_TIMEOUT")
        report.put("Reason", str(exc))
        if claimed:
            report.put("ClaimedTask", claimed)
        return EXIT_RUN_FAILED
    except ProfileLockedError as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_ENV_NOT_READY
    except (ResearchChromeBusy, ResearchChromeNotRunning) as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_ENV_NOT_READY
    except PlaywrightUnavailable as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_CAPABILITY_MISSING
    except Exception as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:300])
        return EXIT_ENV_NOT_READY
    finally:
        try:
            await browser.close()
        except Exception:
            pass
    report.put("Status", "SUCCESS")
    report.put("ClaimedTask", claimed)
    report.put("ArchivedPath", record.archived_path)
    report.put("SHA256", record.sha256)
    report.put("OriginalFilename", record.original_filename)
    report.put("SourcePage", record.source_url)
    return EXIT_OK
