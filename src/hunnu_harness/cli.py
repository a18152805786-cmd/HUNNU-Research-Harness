from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from . import __version__
from .browser.playwright_backend import PlaywrightUnavailable
from .browser.pdf_preferences import ResearchChromePdfPreference, ResearchChromePreferenceError
from .browser.session import ResearchBrowser
from .paths import LIBRARY_ROOT, OUTPUT_ROOT, CORE_ROOT, STAGING_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hunnu-harness")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("env", help="Print safe runtime paths")
    agent_route = sub.add_parser(
        "agent-route",
        help="Validate and route a bounded Agent research-acquisition request without network activity",
    )
    from .agent_entrypoint import add_agent_route_arguments

    add_agent_route_arguments(agent_route)
    start = sub.add_parser("browser-start", help="Start a dedicated Playwright Chrome profile")
    start.add_argument("--profile", type=Path, default=Path(os.environ.get("HUNNU_RESEARCH_PROFILE", Path.home() / "ResearchHarness" / "chrome-profile")))
    start.add_argument("--downloads", type=Path, default=STAGING_DIR)
    start.add_argument("--chrome", type=Path, default=Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
    start.add_argument("--headless", action="store_true")
    pdf_download = sub.add_parser(
        "browser-configure-pdf-download",
        help="Enable direct PDF downloads only in the stopped dedicated Research Chrome profile",
    )
    pdf_download.add_argument(
        "--profile",
        type=Path,
        default=Path(
            os.environ.get(
                "HUNNU_RESEARCH_PROFILE",
                Path.home() / "ResearchHarness" / "chrome-profile",
            )
        ),
    )
    library_stage = sub.add_parser(
        "library-stage",
        help="COPY one explicit PDF candidate into controlled Library staging; never scan or move",
    )
    library_stage.add_argument("--source", type=Path, required=True)
    library_import = sub.add_parser(
        "library-import",
        help="Validate and import one staged PDF using an explicit metadata JSON object",
    )
    library_import.add_argument("--source", type=Path, required=True)
    library_import.add_argument("--metadata-json", type=Path, required=True)

    confirm_topics = sub.add_parser(
        "library-confirm-topics",
        help=(
            "Confirm the topics a person chose for one WORK after classification "
            "returned REVIEW_REQUIRED"
        ),
    )
    confirm_topics.add_argument("--paper-id", required=True)
    confirm_topics.add_argument(
        "--topic",
        action="append",
        required=True,
        metavar="DOMAIN" + chr(92) + "SUBTOPIC",
        help="A proposed topic to confirm; repeat the flag to confirm several",
    )
    confirm_topics.add_argument(
        "--allow-taxonomy-override",
        action="store_true",
        help=(
            "Confirm a taxonomy topic that was not proposed for this WORK. "
            "Off by default so a topic can never be invented"
        ),
    )
    sub.add_parser(
        "browser-status",
        help="Report whether a persistent Research Chrome is running, without starting one",
    )
    # There is deliberately no "browser-auth-status" command.  One existed
    # briefly, reading cookie names and expiry times from the profile's
    # Cookies database -- values untouched, but still a second authentication
    # oracle doing exactly what AGENTS.md 6/22 and classify_auth_state()'s own
    # contract rule out.  Under the frozen boundary (no cookie inspection, no
    # navigation, no starting a browser) a pre-run probe can only answer
    # AUTH_UNKNOWN whenever the browser is closed -- which is precisely when a
    # pre-run check happens -- so the command could not serve its purpose.
    # Sign-in staleness is surfaced where it is actually observable: a live
    # run stops with ACTION_REQUIRED_USER_LOGIN on the page state it sees, and
    # the write-ahead fetch ledger keeps an interrupted run from burning quota.
    sub.add_parser(
        "browser-stop",
        help="Close the persistent Research Chrome, ending its signed-in session",
    )

    session_restore = sub.add_parser(
        "browser-configure-session-restore",
        help=(
            "Let an institutional sign-in survive closing the dedicated Research "
            "Chrome profile, so later runs are not anonymous"
        ),
    )
    session_restore.add_argument(
        "--profile",
        type=Path,
        default=Path(
            os.environ.get(
                "HUNNU_RESEARCH_PROFILE",
                Path.home() / "ResearchHarness" / "chrome-profile",
            )
        ),
    )

    sub.add_parser(
        "library-provenance-status",
        help=(
            "Report the topic-assignment provenance record and its own digest, "
            "which is separate from the corpus fingerprint"
        ),
    )

    sub.add_parser(
        "library-fetch-budget",
        help=(
            "Report today's full-text fetch budget: attempts used, remaining "
            "allowance, and per-identifier counts, so a run can check before it starts"
        ),
    )

    from .navigator.cli import add_navigator_subcommands

    add_navigator_subcommands(sub)
    return parser


async def _start(args: argparse.Namespace) -> int:
    """Start the dedicated browser and leave it running.

    It used to close the browser it had just started, which made the command
    useless for the thing it is for: an institutional sign-in lives in the
    browser process, so closing it threw the sign-in away before anyone could
    use it.  The browser now stays up until `browser-stop`.
    """

    from .browser.persistent_browser import PersistentBrowserError, start_persistent_browser

    try:
        status = start_persistent_browser(
            profile_dir=args.profile,
            chrome_executable=args.chrome,
        )
    except PersistentBrowserError as exc:
        print("DedicatedProfileStarted=false")
        print(f"Reason={exc}")
        return 2
    for key, value in status.as_dict().items():
        print(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
    print("DedicatedProfileStarted=true")
    print("BrowserLeftRunning=true")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "env":
        print(f"CoreRoot={CORE_ROOT}")
        print(f"OutputRoot={OUTPUT_ROOT}")
        print(f"LibraryRoot={LIBRARY_ROOT}")
        print(f"Python={os.sys.executable}")
        return 0
    if args.command == "browser-start":
        return asyncio.run(_start(args))
    if args.command == "browser-configure-pdf-download":
        try:
            audit = ResearchChromePdfPreference(args.profile).configure_direct_download()
        except ResearchChromePreferenceError as exc:
            print(f"PdfDirectDownloadConfigured=false")
            print(f"Reason={exc}")
            return 2
        for key, value in audit.as_dict().items():
            print(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
        print("PdfDirectDownloadConfigured=true")
        return 0
    if args.command == "browser-status":
        from .browser.persistent_browser import probe

        status = probe()
        for key, value in status.as_dict().items():
            print(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
        return 0
    if args.command == "browser-stop":
        import asyncio as _asyncio

        from .browser.persistent_browser import probe

        status = probe()
        if not status.running:
            print("PersistentBrowserRunning=false")
            print("Reason=No persistent Research Chrome is listening")
            return 0

        async def _shutdown() -> None:
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                browser = await playwright.chromium.connect_over_cdp(status.endpoint)
                await browser.close()

        _asyncio.run(_shutdown())
        print("PersistentBrowserStopped=true")
        print("SessionEnded=true")
        return 0
    if args.command == "browser-configure-session-restore":
        try:
            audit = ResearchChromePdfPreference(args.profile).configure_session_restore()
        except ResearchChromePreferenceError as exc:
            print("SessionRestoreConfigured=false")
            print(f"Reason={exc}")
            return 2
        for key, value in audit.as_dict().items():
            print(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
        print("SessionRestoreConfigured=true")
        return 0
    if args.command == "agent-route":
        from .agent_entrypoint import route_from_cli_args

        return route_from_cli_args(args)
    if args.command == "library-stage":
        from .literature.library import ExternalPaperImporter

        staged = ExternalPaperImporter().stage_pdf(args.source)
        print(json.dumps(staged.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "library-provenance-status":
        from .literature.topic_confirmation import TOPIC_PROVENANCE_PATH, TopicProvenanceStore

        fingerprint = TopicProvenanceStore().fingerprint()
        payload = {"TopicProvenancePath": str(TOPIC_PROVENANCE_PATH), **fingerprint.as_dict()}
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        # A missing sidecar is a legitimate state, not a failure: it means no
        # assignment has been recorded yet, not that anything is wrong.  Only a
        # record that will not parse is reported as a problem.
        return 0 if fingerprint.intact else 2

    if args.command == "library-fetch-budget":
        from .literature.fetch_ledger import FetchLedgerError, FulltextFetchLedger

        try:
            usage = FulltextFetchLedger().usage_today()
        except FetchLedgerError as exc:
            # A ledger that cannot be read refuses fetches (fail closed), so
            # say that plainly instead of printing a half-true budget.
            print("LedgerReadable=false")
            print(f"Reason={exc}")
            print("FetchesWillBeRefused=true")
            return 2
        print(json.dumps(usage, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.command == "library-confirm-topics":
        from .literature.auto_classification import PostAcquisitionClassifier
        from .literature.topic_confirmation import ConfirmationStatus

        result = PostAcquisitionClassifier().confirm_topics(
            args.paper_id,
            args.topic,
            allow_taxonomy_override=args.allow_taxonomy_override,
        )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        accepted = {ConfirmationStatus.CONFIRMED, ConfirmationStatus.ALREADY_CONFIRMED}
        return 0 if result.status in accepted else 2

    if args.command == "library-import":
        from .literature.library import ExternalPaperImporter, LibraryDisposition

        metadata = json.loads(args.metadata_json.read_text(encoding="utf-8-sig"))
        if not isinstance(metadata, dict):
            raise ValueError("Library import metadata JSON must contain one object")
        result = ExternalPaperImporter().import_staged_pdf(args.source, metadata)
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        accepted = {
            LibraryDisposition.NEW_PAPER,
            LibraryDisposition.EXACT_DUPLICATE,
            LibraryDisposition.SAME_WORK_DIFFERENT_VERSION,
        }
        return 0 if result.disposition in accepted else 2
    from .navigator.cli import NAVIGATOR_COMMANDS, run_navigator_command

    if args.command in NAVIGATOR_COMMANDS:
        return run_navigator_command(args)
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
