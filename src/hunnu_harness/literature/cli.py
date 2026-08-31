from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from ..browser.playwright_backend import (
    PlaywrightBrowser,
    PlaywrightUnavailable,
    ProfileLockedError,
    discover_chrome_executable,
)
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
            "Explicitly permit a fetch the daily full-text budget would refuse "
            "(same paper twice today, or the daily ceiling); the attempt is still recorded"
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
            "Explicitly permit a fetch the daily full-text budget would refuse "
            "(same paper twice today, or the daily ceiling); the attempt is still recorded"
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
            "Explicitly permit a fetch the daily full-text budget would refuse "
            "(same paper twice today, or the daily ceiling); the attempt is still recorded"
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
            "Explicitly permit a fetch the daily full-text budget would refuse "
            "(same paper twice today, or the daily ceiling); the attempt is still recorded"
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
    return parser


async def _run_live(args: argparse.Namespace) -> int:
    request = _request_from_args(args)
    staging = Path(args.run_root) / "downloads" / "staging"
    browser = PlaywrightBrowser(
        profile_dir=args.profile,
        downloads_dir=staging,
        executable_path=args.chrome,
        headless=args.headless,
        human_wait_seconds=float(getattr(args, "human_wait", 0.0) or 0.0),
    )
    try:
        await browser.start()
        adapter_type = {
            "live-cnki": CNKIAdapter,
            "live-springerlink": SpringerLinkAdapter,
            "live-sciencedirect": ScienceDirectAdapter,
            "live-oxfordacademic": OxfordAcademicAdapter,
        }[args.command]
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
        print("Status=SOURCE_UNAVAILABLE")
        print(f"Reason={exc}")
        print("ProfileModified=false")
        return 3
    except PlaywrightUnavailable as exc:
        print("Status=SOURCE_UNAVAILABLE")
        print(f"Reason={exc}")
        return 2
    finally:
        # Read the browser's own account of what it did before tearing it down.
        # An Agent that has to infer this from missing output gets it wrong:
        # it reported "no browser opened" for a run in which Chrome launched,
        # reached the article host, and was closed 5 seconds later.
        try:
            lifecycle = await browser.lifecycle()
        except Exception:
            lifecycle = {}
        await browser.close()

    for key in ("BrowserLaunched", "BrowserHeadless", "FinalURL", "FinalPageTitle"):
        if key in lifecycle:
            print(f"{key}={lifecycle[key]}")

    print(f"Status={result.status.value}")
    print(f"Results={len(result.records)}")
    print(f"Downloads={len(result.downloads)}")
    if result.status == RunStatus.ACTION_REQUIRED_USER_LOGIN:
        print("ACTION_REQUIRED_USER_LOGIN=true")
        print(f"Reason={result.action_required_reason}")
        print("BrowserReadyForManualAction=false")
        print("Use the registered Playwright MCP session to keep Research Chrome open for manual login.")
    if result.status == RunStatus.ACTION_REQUIRED_USER_DOWNLOAD:
        print("ACTION_REQUIRED_USER_LOGIN=false")
        print("ACTION_REQUIRED_USER_DOWNLOAD=true")
        print("BrowserReadyForManualDownload=true")
        print(f"Reason={result.action_required_reason}")
        print("Click the Chrome PDF Viewer native Download button once; Harness will ingest the new file.")
    return 0 if result.status in {RunStatus.SUCCESS, RunStatus.PARTIAL_SUCCESS} else 4


def main() -> int:
    args = build_parser().parse_args()
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
        print(f"Status={result.status.value}")
        print(f"LiveAcceptanceSearchPassed={len(result.records) == 1}")
        print(f"LiveAcceptancePDFDownloaded={len(result.downloads) == 1}")
        print(f"LiveAcceptancePDFValidated={bool(result.downloads and result.downloads[0].pdf_validation_passed)}")
        return 0 if result.status == RunStatus.SUCCESS else 5
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
        print(f"Status={result.status.value}")
        print(f"LiveAcceptanceSearchPassed={len(result.records) == 1}")
        print(f"LiveAcceptancePDFDownloaded={len(result.downloads) == 1}")
        print(f"LiveAcceptancePDFValidated={bool(result.downloads and result.downloads[0].pdf_validation_passed)}")
        return 0 if result.status == RunStatus.SUCCESS else 5
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
        print(f"Status={result.status.value}")
        print(f"LiveAcceptanceSearchPassed={len(result.records) == 1}")
        print(f"LiveAcceptanceDownloaded={entry is not None}")
        print(f"LiveAcceptanceFileValidated={bool(entry and entry.file_validation_passed)}")
        print(f"LiveAcceptanceFormat={entry.full_text_format if entry else 'Unknown'}")
        print(f"LiveAcceptanceTargetIdentityConfirmed={bool(record and record.target_identity_confirmed)}")
        return 0 if result.status == RunStatus.SUCCESS else 5
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
