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
    from .navigator.cli import add_navigator_subcommands

    add_navigator_subcommands(sub)
    return parser


async def _start(args: argparse.Namespace) -> int:
    browser = ResearchBrowser(profile_dir=args.profile, downloads_dir=args.downloads, chrome_executable=args.chrome, headless=args.headless)
    try:
        await browser.start()
    except PlaywrightUnavailable as exc:
        print(str(exc))
        return 2
    state = await browser.status()
    print(f"CurrentURL={state.url}")
    print(f"PageTitle={state.title}")
    print(f"AuthenticationStatus={state.auth_status.value}")
    print("DedicatedProfileStarted=true")
    await browser.stop()
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
    if args.command == "agent-route":
        from .agent_entrypoint import route_from_cli_args

        return route_from_cli_args(args)
    if args.command == "library-stage":
        from .literature.library import ExternalPaperImporter

        staged = ExternalPaperImporter().stage_pdf(args.source)
        print(json.dumps(staged.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
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
