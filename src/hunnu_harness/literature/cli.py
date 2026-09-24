from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..browser.playwright_backend import (
    PlaywrightBrowser,
    PlaywrightUnavailable,
    ProfileLockedError,
    ResearchChromeNotRunning,
    discover_chrome_executable,
)
from ..browser.research_chrome_lock import ResearchChromeBusy
from ..cli_output import CliReport, attach_json_flag
from ..exit_codes import (
    EXIT_BUDGET_EXHAUSTED,
    EXIT_CAPABILITY_MISSING,
    EXIT_ENV_NOT_READY,
    EXIT_HUMAN_ACTION_REQUIRED,
    EXIT_OK,
    EXIT_RUN_FAILED,
)
from .fetch_ledger import STATUS_ATTEMPT_LIMIT_REACHED, STATUS_BUDGET_EXHAUSTED
from .adapters.base import LiteratureSourceError
from .adapters.cnki import CNKIAdapter
from .adapters.oxfordacademic import OxfordAcademicAdapter
from .adapters.sciencedirect import ScienceDirectAdapter
from .adapters.springerlink import SpringerLinkAdapter
from .artifacts import CNKI_UPGRADE_RUN_ROOT, OXFORD_UPGRADE_RUN_ROOT, UPGRADE_RUN_ROOT
from .institutional import HUNNUInstitutionalAccessResolver, InstitutionalResolutionTrigger
from .models import LiteratureSearchRequest, RunStatus
from ..paths import _windows_io_path
from .planning import LiteratureSearchPlanner
from .workflow import (
    LiteratureAcquisitionWorkflow,
    finalize_captured_cnki_acceptance,
    finalize_captured_sciencedirect_acceptance,
    finalize_captured_springerlink_acceptance,
)


DEFAULT_PROFILE = Path.home() / "ResearchHarness" / "chrome-profile"


def _live_adapter_type(command: str) -> Any:
    # Resolved when called, so the module's adapter names stay the one seam.
    return {
        "live-cnki": CNKIAdapter,
        "live-springerlink": SpringerLinkAdapter,
        "live-sciencedirect": ScienceDirectAdapter,
        "live-oxfordacademic": OxfordAcademicAdapter,
    }[command]


def _default_profile() -> Path:
    return Path(os.environ.get("HUNNU_RESEARCH_PROFILE", DEFAULT_PROFILE))


def _request_from_args(args: argparse.Namespace) -> LiteratureSearchRequest:
    if getattr(args, "request_json", None):
        payload = json.loads(_windows_io_path(Path(args.request_json)).read_text(encoding="utf-8-sig"))
        return LiteratureSearchRequest.from_mapping(payload)
    if getattr(args, "text", None):
        return LiteratureSearchRequest.from_natural_language(args.text)
    title = getattr(args, "title", None)
    doi = getattr(args, "doi", None)
    original = f"Find exact literature item: {title or doi}"
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": original,
            "ResearchQuestion": original,
            "ExactTitles": [title] if title else [],
            "DOIs": [doi] if doi else [],
            "MaxSearchResults": getattr(args, "max_results", 5),
            "MaxResultsPerSource": getattr(args, "max_results", 5),
            "MaxDownloads": getattr(args, "max_downloads", 1),
            "MaxDownloadsPerRun": getattr(args, "max_downloads", 1),
            "RequireFullText": getattr(args, "max_downloads", 1) > 0,
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hunnu_harness.literature")
    subparsers = parser.add_subparsers(dest="command", required=True)
    daily_limit_help = (
        "Override the daily publisher fetch total (default 15; also configurable "
        "with HUNNU_HARNESS_DAILY_FETCH_LIMIT); this is your account's quota knob"
    )

    plan = subparsers.add_parser("plan", help="Parse a natural-language request and print bounded queries")
    plan.add_argument("--text", required=True)
    plan.add_argument("--max-queries", type=int, default=8)

    live = subparsers.add_parser("live-sciencedirect", help="Run one bounded Research Chrome acquisition")
    selector = live.add_mutually_exclusive_group(required=True)
    selector.add_argument("--title")
    selector.add_argument("--doi")
    selector.add_argument("--request-json", type=Path)
    live.add_argument("--run-root", type=Path, default=UPGRADE_RUN_ROOT)
    live.add_argument("--profile", type=Path, default=_default_profile())
    live.add_argument("--chrome", type=Path, default=discover_chrome_executable())
    live.add_argument("--max-results", type=int, default=5)
    live.add_argument("--max-downloads", type=int, choices=range(0, 2), default=1)
    live.add_argument("--headless", action="store_true")
    live.add_argument("--daily-limit", type=int, default=None, help=daily_limit_help)
    live.add_argument(
        "--allow-refetch",
        action="store_true",
        help=(
            "Explicitly permit fetching the same paper again after the per-identifier "
            "repeat check refused it; never lifts the daily total (that is --daily-limit); "
            "the attempt is still recorded"
        ),
    )
    live.add_argument(
        "--human-wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Keep Chrome open this long for a person to pass a page gate themselves",
    )

    springer_live = subparsers.add_parser("live-springerlink", help="Run one bounded Springer Link acquisition")
    springer_selector = springer_live.add_mutually_exclusive_group(required=True)
    springer_selector.add_argument("--title")
    springer_selector.add_argument("--doi")
    springer_selector.add_argument("--request-json", type=Path)
    springer_live.add_argument("--run-root", type=Path, default=UPGRADE_RUN_ROOT)
    springer_live.add_argument("--profile", type=Path, default=_default_profile())
    springer_live.add_argument("--chrome", type=Path, default=discover_chrome_executable())
    springer_live.add_argument("--max-results", type=int, default=5)
    springer_live.add_argument("--max-downloads", type=int, choices=range(0, 2), default=1)
    springer_live.add_argument("--headless", action="store_true")
    springer_live.add_argument("--daily-limit", type=int, default=None, help=daily_limit_help)
    springer_live.add_argument(
        "--allow-refetch",
        action="store_true",
        help=(
            "Explicitly permit fetching the same paper again after the per-identifier "
            "repeat check refused it; never lifts the daily total (that is --daily-limit); "
            "the attempt is still recorded"
        ),
    )

    cnki_live = subparsers.add_parser("live-cnki", help="Run one bounded CNKI acquisition")
    cnki_selector = cnki_live.add_mutually_exclusive_group(required=True)
    cnki_selector.add_argument("--title")
    cnki_selector.add_argument("--doi")
    cnki_selector.add_argument("--request-json", type=Path)
    cnki_live.add_argument("--run-root", type=Path, default=CNKI_UPGRADE_RUN_ROOT)
    cnki_live.add_argument("--profile", type=Path, default=_default_profile())
    cnki_live.add_argument("--chrome", type=Path, default=discover_chrome_executable())
    cnki_live.add_argument("--max-results", type=int, default=3)
    cnki_live.add_argument("--max-downloads", type=int, choices=range(0, 2), default=1)
    cnki_live.add_argument("--headless", action="store_true")
    cnki_live.add_argument("--daily-limit", type=int, default=None, help=daily_limit_help)
    cnki_live.add_argument(
        "--allow-refetch",
        action="store_true",
        help=(
            "Explicitly permit fetching the same paper again after the per-identifier "
            "repeat check refused it; never lifts the daily total (that is --daily-limit); "
            "the attempt is still recorded"
        ),
    )

    oxford_live = subparsers.add_parser(
        "live-oxfordacademic",
        help="Run one bounded Oxford Academic acquisition through the verified HUNNU route",
    )
    oxford_selector = oxford_live.add_mutually_exclusive_group(required=True)
    oxford_selector.add_argument("--title")
    oxford_selector.add_argument("--doi")
    oxford_selector.add_argument("--request-json", type=Path)
    oxford_live.add_argument("--run-root", type=Path, default=OXFORD_UPGRADE_RUN_ROOT)
    oxford_live.add_argument("--profile", type=Path, default=_default_profile())
    oxford_live.add_argument("--chrome", type=Path, default=discover_chrome_executable())
    oxford_live.add_argument("--max-results", type=int, default=1)
    oxford_live.add_argument("--max-downloads", type=int, choices=range(0, 2), default=1)
    oxford_live.add_argument("--headless", action="store_true")
    oxford_live.add_argument("--daily-limit", type=int, default=None, help=daily_limit_help)
    oxford_live.add_argument(
        "--allow-refetch",
        action="store_true",
        help=(
            "Explicitly permit fetching the same paper again after the per-identifier "
            "repeat check refused it; never lifts the daily total (that is --daily-limit); "
            "the attempt is still recorded"
        ),
    )

    capture = subparsers.add_parser(
        "finalize-sciencedirect-capture",
        help="Finalize sanitized MCP DOM evidence and an authorized PDF",
    )
    capture.add_argument("--request-json", type=Path, required=True)
    capture.add_argument("--query", required=True)
    capture.add_argument("--search-html", type=Path, required=True)
    capture.add_argument("--article-html", type=Path, required=True)
    capture.add_argument("--article-url", required=True)
    capture.add_argument("--pdf", type=Path)
    capture.add_argument("--run-root", type=Path, default=UPGRADE_RUN_ROOT)

    springer_capture = subparsers.add_parser(
        "finalize-springerlink-capture",
        help="Finalize sanitized Springer DOM evidence and an authorized PDF",
    )
    springer_capture.add_argument("--request-json", type=Path, required=True)
    springer_capture.add_argument("--query", required=True)
    springer_capture.add_argument("--search-html", type=Path, required=True)
    springer_capture.add_argument("--article-html", type=Path, required=True)
    springer_capture.add_argument("--article-url", required=True)
    springer_capture.add_argument("--pdf", type=Path)
    springer_capture.add_argument("--run-root", type=Path, default=UPGRADE_RUN_ROOT)

    cnki_capture = subparsers.add_parser(
        "finalize-cnki-capture",
        help="Finalize sanitized CNKI DOM evidence and an authorized PDF/CAJ file",
    )
    cnki_capture.add_argument("--request-json", type=Path, required=True)
    cnki_capture.add_argument("--query", required=True)
    cnki_capture.add_argument("--search-html", type=Path, required=True)
    cnki_capture.add_argument("--article-html", type=Path, required=True)
    cnki_capture.add_argument("--article-url", required=True)
    cnki_capture.add_argument("--fulltext", type=Path)
    cnki_capture.add_argument(
        "--authorized-browser-download-confirmed",
        action="store_true",
        help="Confirm the file came from the target's normal single-paper control in the authenticated browser",
    )
    cnki_capture.add_argument("--run-root", type=Path, default=CNKI_UPGRADE_RUN_ROOT)

    # Machine-output mode is uniform across every subcommand: one JSON document
    # on stdout, human-directed sentences on stderr.  Commands that already
    # emit a single JSON document (plan) accept the flag as a no-op.
    attach_json_flag(subparsers)
    return parser


async def _run_live(args: argparse.Namespace) -> int:
    report = CliReport(bool(getattr(args, "json", False)))
    exit_code, _result = await _run_live_into(args, report)
    report.flush()
    return exit_code


async def _run_live_into(args: argparse.Namespace, report: CliReport) -> tuple[int, Any]:
    """Run one live acquisition, reporting into ``report`` without printing it.

    Returns the graded exit code and the workflow result (``None`` when the
    run stopped before the workflow produced one).  ``acquire-batch`` runs
    every queue item through here, so an item gets exactly the single-paper
    path -- the same pacing, fetch ledger, identity lock, validation, archive
    and exit ladder -- rather than a second implementation of it.
    """

    try:
        request = _request_from_args(args)
    except (TypeError, ValueError) as exc:
        # A request that fails validation (an unknown ResourceType, say) is a
        # graded refusal like any other, never a traceback.  No browser yet.
        report.put("Status", "INVALID_REQUEST")
        report.put("Reason", str(exc))
        return EXIT_RUN_FAILED, None
    adapter_type = _live_adapter_type(args.command)
    if request.restricted_search and not getattr(adapter_type, "supports_restricted_search", False):
        # Searching anyway would return unrestricted results under a restricted
        # request.  Refused before the Research Chrome is touched.
        report.put("Status", "UNSUPPORTED_CAPABILITY")
        report.put(
            "MissingCapability",
            f"{adapter_type.name} cannot confine a search to ResourceType/SourceJournals; "
            "restricted search is implemented for CNKI only",
        )
        return EXIT_RUN_FAILED, None
    staging = Path(args.run_root) / "downloads" / "staging"
    browser = PlaywrightBrowser(
        profile_dir=args.profile,
        downloads_dir=staging,
        executable_path=args.chrome,
        headless=args.headless,
        human_wait_seconds=float(getattr(args, "human_wait", 0.0) or 0.0),
        require_attach=bool(getattr(args, "require_attached_browser", False)),
    )
    adapter = None
    try:
        await browser.start()
        adapter = adapter_type(browser)
        # The write-ahead fetch budget refuses a repeat by default; the flag is
        # the explicit, per-run override and is recorded on the attempt row.
        adapter.allow_refetch = bool(getattr(args, "allow_refetch", False))
        adapter.daily_fetch_limit = getattr(args, "daily_limit", None)
        resolver = None
        trigger = None
        if args.command == "live-springerlink":
            resolver = HUNNUInstitutionalAccessResolver(browser)
            trigger = InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN
        elif args.command == "live-oxfordacademic":
            resolver = HUNNUInstitutionalAccessResolver(browser)
            trigger = InstitutionalResolutionTrigger.DIRECT_ROUTE_UNAVAILABLE
            route = await resolver.resolve(adapter.name, trigger=trigger)
            adapter.bind_institutional_route(route)
        workflow = LiteratureAcquisitionWorkflow(
            adapter,
            run_root=args.run_root,
            human_like_delay_seconds=1.5,
            institutional_resolver=resolver,
            institutional_trigger=trigger,
        )
        result = await workflow.run(request)
    except ProfileLockedError as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        report.put("ProfileModified", False, plain="false")
        return EXIT_ENV_NOT_READY, None
    except (ResearchChromeBusy, ResearchChromeNotRunning) as exc:
        # Nothing touched the browser: the lock is taken, and the attach-only
        # check made, before the first browser command.
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_ENV_NOT_READY, None
    except PlaywrightUnavailable as exc:
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", str(exc))
        return EXIT_CAPABILITY_MISSING, None
    except LiteratureSourceError as exc:
        # Setup-phase source errors (an unresolved institutional route, a
        # source-side stop before the workflow loop) must honor the same
        # contract as everything else: one JSON document, a graded exit --
        # never a naked traceback.  Found live: Oxford's unresolved route
        # escaped as a raw SourceLayoutChanged.
        status = getattr(exc, "status", RunStatus.SOURCE_UNAVAILABLE)
        report.put("Status", status.value)
        report.put("Reason", str(exc))
        if status in {RunStatus.ACTION_REQUIRED_USER_LOGIN, RunStatus.ACTION_REQUIRED_USER_DOWNLOAD}:
            return EXIT_HUMAN_ACTION_REQUIRED, None
        return EXIT_ENV_NOT_READY, None
    except Exception as exc:
        reason = f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:300]
        report.put("Status", "SOURCE_UNAVAILABLE")
        report.put("Reason", reason)
        return EXIT_ENV_NOT_READY, None
    finally:
        # Read the browser's own account of what it did before tearing it down.
        # An Agent that has to infer this from missing output gets it wrong:
        # it reported "no browser opened" for a run in which Chrome launched,
        # reached the article host, and was closed 5 seconds later.
        try:
            lifecycle = await browser.lifecycle()
        except Exception:
            lifecycle = {}
        # Teardown is best-effort for the same reason: a driver that fails
        # to stop after the run has finished must not replace the graded
        # JSON report with a traceback and exit 1.
        try:
            await browser.close()
        except Exception as exc:
            print(
                f"warning: browser teardown failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    for key in ("BrowserLaunched", "BrowserHeadless", "FinalURL", "FinalPageTitle"):
        if key in lifecycle:
            report.put(key, lifecycle[key])

    report.put("Status", result.status.value)
    report.put("Results", len(result.records))
    report.put("Downloads", len(result.downloads))
    if request.restricted_search:
        # Whether CNKI demonstrably searched what was asked, per query, and
        # what was discarded -- so "Results" is never read as filtered when
        # the page could not confirm it.
        requested = {
            "ResourceType": request.resource_type,
            "SourceJournals": list(request.source_journals),
            "YearStart": request.year_start,
            "YearEnd": request.year_end,
        }
        outcomes = adapter.search_restriction_reports() if adapter is not None else []
        report.put(
            "SearchRestriction",
            requested,
            plain=json.dumps(requested, ensure_ascii=False, sort_keys=True),
        )
        report.put(
            "SearchRestrictionOutcome",
            outcomes,
            plain=json.dumps(outcomes, ensure_ascii=False, sort_keys=True),
        )
    budget_refusals = sum(
        1
        for record in result.records
        if getattr(record, "error_status", None)
        in {STATUS_BUDGET_EXHAUSTED, STATUS_ATTEMPT_LIMIT_REACHED}
    )
    if budget_refusals:
        report.put("FetchBudgetRefusals", budget_refusals)
    if result.status == RunStatus.ACTION_REQUIRED_USER_LOGIN:
        report.put("ACTION_REQUIRED_USER_LOGIN", True, plain="true")
        report.put("Reason", result.action_required_reason)
        report.put("BrowserReadyForManualAction", False, plain="false")
        report.note(
            "Hand the gate back to the user before starting another source that needs the "
            "same institutional session: run `hunnu-harness browser-start` to hold the "
            "dedicated Research Chrome open, say which page needs the manual step, and wait."
        )
    if result.status == RunStatus.ACTION_REQUIRED_USER_DOWNLOAD:
        report.put("ACTION_REQUIRED_USER_LOGIN", False, plain="false")
        report.put("ACTION_REQUIRED_USER_DOWNLOAD", True, plain="true")
        report.put("BrowserReadyForManualDownload", True, plain="true")
        report.put("Reason", result.action_required_reason)
        report.note(
            "Click the Chrome PDF Viewer native Download button once; Harness will ingest the new file."
        )
    return _live_exit_code(result, budget_refusals), result


def _live_exit_code(result: Any, budget_refusals: int) -> int:
    """Map one finished run onto the graded exit ladder (see exit_codes).

    The one judgment call: a partial run that still downloaded something
    exits 0 even when later fetches hit the budget -- files landed, and the
    report says both facts.  Only a run whose every attempted download was
    refused on budget exits 3, because "stop asking today" is then the whole
    story.
    """

    if result.status in {RunStatus.ACTION_REQUIRED_USER_LOGIN, RunStatus.ACTION_REQUIRED_USER_DOWNLOAD}:
        return EXIT_HUMAN_ACTION_REQUIRED
    if budget_refusals and not result.downloads:
        return EXIT_BUDGET_EXHAUSTED
    if result.status in {RunStatus.SUCCESS, RunStatus.PARTIAL_SUCCESS, RunStatus.NO_RESULTS}:
        return EXIT_OK
    if result.status in {RunStatus.SOURCE_UNAVAILABLE, RunStatus.SOURCE_LAYOUT_CHANGED}:
        return EXIT_ENV_NOT_READY
    return EXIT_RUN_FAILED


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        request = _request_from_args(args)
        payload = {
            "Request": request.as_dict(),
            "Queries": [
                {"GeneratedQuery": item.query, "Filters": item.filters, "Rationale": item.rationale}
                for item in LiteratureSearchPlanner(max_queries=args.max_queries).plan(request)
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"live-sciencedirect", "live-springerlink", "live-cnki", "live-oxfordacademic"}:
        return asyncio.run(_run_live(args))
    if args.command == "finalize-sciencedirect-capture":
        request = _request_from_args(args)
        result = finalize_captured_sciencedirect_acceptance(
            request=request,
            query=args.query,
            search_html=_windows_io_path(args.search_html).read_text(encoding="utf-8"),
            article_html=_windows_io_path(args.article_html).read_text(encoding="utf-8"),
            article_url=args.article_url,
            downloaded_pdf=args.pdf,
            run_root=args.run_root,
        )
        report = CliReport(bool(getattr(args, "json", False)))
        report.put("Status", result.status.value)
        report.put("LiveAcceptanceSearchPassed", len(result.records) == 1)
        report.put("LiveAcceptancePDFDownloaded", len(result.downloads) == 1)
        report.put(
            "LiveAcceptancePDFValidated",
            bool(result.downloads and result.downloads[0].pdf_validation_passed),
        )
        report.flush()
        return EXIT_OK if result.status == RunStatus.SUCCESS else EXIT_RUN_FAILED
    if args.command == "finalize-springerlink-capture":
        request = _request_from_args(args)
        result = finalize_captured_springerlink_acceptance(
            request=request,
            query=args.query,
            search_html=_windows_io_path(args.search_html).read_text(encoding="utf-8"),
            article_html=_windows_io_path(args.article_html).read_text(encoding="utf-8"),
            article_url=args.article_url,
            downloaded_pdf=args.pdf,
            run_root=args.run_root,
        )
        report = CliReport(bool(getattr(args, "json", False)))
        report.put("Status", result.status.value)
        report.put("LiveAcceptanceSearchPassed", len(result.records) == 1)
        report.put("LiveAcceptancePDFDownloaded", len(result.downloads) == 1)
        report.put(
            "LiveAcceptancePDFValidated",
            bool(result.downloads and result.downloads[0].pdf_validation_passed),
        )
        report.flush()
        return EXIT_OK if result.status == RunStatus.SUCCESS else EXIT_RUN_FAILED
    if args.command == "finalize-cnki-capture":
        request = _request_from_args(args)
        result = finalize_captured_cnki_acceptance(
            request=request,
            query=args.query,
            search_html=_windows_io_path(args.search_html).read_text(encoding="utf-8"),
            article_html=_windows_io_path(args.article_html).read_text(encoding="utf-8"),
            article_url=args.article_url,
            downloaded_fulltext=args.fulltext,
            run_root=args.run_root,
            authorized_browser_download_confirmed=args.authorized_browser_download_confirmed,
        )
        entry = result.downloads[0] if result.downloads else None
        record = result.records[0] if result.records else None
        report = CliReport(bool(getattr(args, "json", False)))
        report.put("Status", result.status.value)
        report.put("LiveAcceptanceSearchPassed", len(result.records) == 1)
        report.put("LiveAcceptanceDownloaded", entry is not None)
        report.put("LiveAcceptanceFileValidated", bool(entry and entry.file_validation_passed))
        report.put("LiveAcceptanceFormat", entry.full_text_format if entry else "Unknown")
        report.put(
            "LiveAcceptanceTargetIdentityConfirmed",
            bool(record and record.target_identity_confirmed),
        )
        report.flush()
        return EXIT_OK if result.status == RunStatus.SUCCESS else EXIT_RUN_FAILED
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
