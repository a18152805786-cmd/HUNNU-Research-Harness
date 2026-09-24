from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from . import __version__
from .browser.playwright_backend import PlaywrightUnavailable, discover_chrome_executable
from .browser.pdf_preferences import ResearchChromePdfPreference, ResearchChromePreferenceError
from .browser.session import ResearchBrowser
from .cli_output import CliReport, attach_json_flag
from .paths import LIBRARY_ROOT, OUTPUT_ROOT, CORE_ROOT, STAGING_DIR, _windows_io_path


def _bool_plain(value: object) -> object:
    return str(value).lower() if isinstance(value, bool) else value


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer >= 1, got {raw!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected an integer >= 1, got {raw!r}")
    return value


def _audit_report(args: argparse.Namespace, audit_items: dict) -> CliReport:
    report = CliReport(bool(getattr(args, "json", False)))
    for key, value in audit_items.items():
        report.put(key, value, plain=_bool_plain(value))
    return report


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
    start.add_argument("--chrome", type=Path, default=discover_chrome_executable())
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
            "Confirm the topics a person chose for one WORK that carries none yet: "
            "after classification returned REVIEW_REQUIRED, or for a WORK that was "
            "archived without being filed"
        ),
    )
    confirm_topics.add_argument("--paper-id", required=True)
    confirm_topics.add_argument(
        "--topic",
        action="append",
        required=True,
        metavar="DOMAIN" + chr(92) + "SUBTOPIC",
        help=(
            "A topic classification raised for this WORK, to confirm; repeat the "
            "flag to confirm several"
        ),
    )
    confirm_topics.add_argument(
        "--allow-taxonomy-override",
        action="store_true",
        help=(
            "Confirm a taxonomy topic that was not proposed for this WORK. "
            "Off by default so a topic can never be invented"
        ),
    )
    status = sub.add_parser(
        "browser-status",
        help=(
            "Report whether a persistent Research Chrome is listening and whether any "
            "Chrome process holds the profile, without starting one"
        ),
    )
    status.add_argument(
        "--profile",
        type=Path,
        default=Path(
            os.environ.get(
                "HUNNU_RESEARCH_PROFILE",
                Path.home() / "ResearchHarness" / "chrome-profile",
            )
        ),
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
    stop = sub.add_parser(
        "browser-stop",
        help=(
            "Ask the persistent Research Chrome to exit gracefully and report stopped "
            "only once its port is quiet and no Chrome process holds the profile"
        ),
    )
    stop.add_argument(
        "--profile",
        type=Path,
        default=Path(
            os.environ.get(
                "HUNNU_RESEARCH_PROFILE",
                Path.home() / "ResearchHarness" / "chrome-profile",
            )
        ),
    )

    session_restore = sub.add_parser(
        "browser-configure-session-restore",
        help=(
            "Report how an institutional sign-in survives closing the dedicated "
            "Research Chrome: browser-start passes --restore-last-session, and "
            "nothing in the profile is edited"
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

    sub.add_parser(
        "capabilities",
        help="What this build supports: sources, knobs, exit-code ladder (JSON, no probing)",
    )
    sub.add_parser(
        "doctor",
        help="Is this machine ready: Python, Playwright, pypdf, Chrome, Output Root, budget (JSON, no network)",
    )

    acquire = sub.add_parser(
        "acquire",
        help=(
            "Run one bounded live acquisition against a supported source, in the "
            "user's own authenticated Research Chrome (spends their quota)"
        ),
    )
    acquire.add_argument(
        "--source",
        required=True,
        choices=("sciencedirect", "springerlink", "cnki", "oxfordacademic"),
        help="Which publisher adapter to drive",
    )
    acquire_selector = acquire.add_mutually_exclusive_group(required=True)
    acquire_selector.add_argument("--title")
    acquire_selector.add_argument("--doi")
    acquire_selector.add_argument("--request-json", type=Path)
    acquire.add_argument("--run-root", type=Path, default=None)
    acquire.add_argument("--profile", type=Path, default=None)
    acquire.add_argument("--chrome", type=Path, default=None)
    acquire.add_argument("--max-results", type=int, default=None)
    acquire.add_argument("--max-downloads", type=int, choices=range(0, 2), default=None)
    acquire.add_argument("--headless", action="store_true")
    acquire.add_argument(
        "--daily-limit",
        type=int,
        default=None,
        help=(
            "Override the daily publisher fetch total (default 15; also "
            "HUNNU_HARNESS_DAILY_FETCH_LIMIT); this is your account's quota knob"
        ),
    )
    acquire.add_argument(
        "--allow-refetch",
        action="store_true",
        help=(
            "Explicitly permit fetching the same paper again after the "
            "per-identifier repeat check refused it; never lifts the daily total"
        ),
    )
    acquire.add_argument(
        "--human-wait",
        type=float,
        default=None,
        help="Seconds to hold the page for a person to clear a challenge (springerlink only)",
    )

    acquire_batch = sub.add_parser(
        "acquire-batch",
        help=(
            "Run a queue of up to 25 papers through `acquire`, one after another, in the "
            "running Research Chrome; stops at the first manual gate and resumes when rerun"
        ),
    )
    acquire_batch.add_argument(
        "--queue",
        type=Path,
        required=True,
        help='Queue JSON: {"BatchName": "...", "Items": [{"Source": "cnki", "Title": "..."}, '
        '{"Source": "springerlink", "DOI": "..."}]}',
    )
    acquire_batch.add_argument(
        "--batch-root",
        type=Path,
        default=None,
        help="State and per-item runs (default: Output Root/runs/AcquireBatch/<BatchName>)",
    )
    acquire_batch.add_argument(
        "--confirm-budget",
        action="store_true",
        help=(
            "The user has confirmed fetching more than 10 papers from this queue "
            "(AGENTS.md Rule 41); never pass it on your own judgement"
        ),
    )
    acquire_batch.add_argument(
        "--daily-limit",
        type=_positive_int,
        default=None,
        help=(
            "Override the daily publisher fetch total for every item (default 15; also "
            "HUNNU_HARNESS_DAILY_FETCH_LIMIT); this is your account's quota knob"
        ),
    )
    acquire_batch.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only: validate the queue, check the library and budget; no browser, no files",
    )

    from .navigator.cli import add_navigator_subcommands

    add_navigator_subcommands(sub)
    # One contract across the whole CLI: with --json, stdout is exactly one
    # json.loads-able document.  Commands already emitting one accept the
    # flag as a no-op.
    attach_json_flag(sub)
    return parser


def _acquire_to_literature_argv(args: argparse.Namespace) -> list[str]:
    """Translate ``acquire --source X`` onto the literature ``live-X`` surface.

    The literature parser stays the single owner of per-source defaults
    (run roots, result caps); this translation forwards only what the caller
    actually set, so those defaults keep applying.
    """

    argv: list[str] = [f"live-{args.source}"]
    for flag, value in (
        ("--title", args.title),
        ("--doi", args.doi),
        ("--request-json", args.request_json),
        ("--run-root", args.run_root),
        ("--profile", args.profile),
        ("--chrome", args.chrome),
        ("--max-results", args.max_results),
        ("--max-downloads", args.max_downloads),
        ("--daily-limit", args.daily_limit),
        ("--human-wait", args.human_wait),
    ):
        if value is not None:
            argv.extend([flag, str(value)])
    if args.headless:
        argv.append("--headless")
    if args.allow_refetch:
        argv.append("--allow-refetch")
    if getattr(args, "json", False):
        argv.append("--json")
    return argv


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
        report = CliReport(bool(getattr(args, "json", False)))
        report.put("DedicatedProfileStarted", False, plain="false")
        report.put("Reason", str(exc))
        report.flush()
        return 2
    report = _audit_report(args, status.as_dict())
    report.put("DedicatedProfileStarted", True, plain="true")
    report.put("BrowserLeftRunning", True, plain="true")
    report.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "env":
        report = CliReport(bool(getattr(args, "json", False)))
        report.put("CoreRoot", str(CORE_ROOT))
        report.put("OutputRoot", str(OUTPUT_ROOT))
        report.put("LibraryRoot", str(LIBRARY_ROOT))
        report.put("Python", os.sys.executable)
        report.flush()
        return 0
    if args.command == "browser-start":
        return asyncio.run(_start(args))
    if args.command == "browser-configure-pdf-download":
        try:
            audit = ResearchChromePdfPreference(args.profile).configure_direct_download()
        except ResearchChromePreferenceError as exc:
            report = CliReport(bool(getattr(args, "json", False)))
            report.put("PdfDirectDownloadConfigured", False, plain="false")
            report.put("Reason", str(exc))
            report.flush()
            return 2
        report = _audit_report(args, audit.as_dict())
        report.put("PdfDirectDownloadConfigured", True, plain="true")
        report.flush()
        return 0
    if args.command == "browser-status":
        from .browser.persistent_browser import probe, profile_in_use

        # Two facts, from the same sources browser-stop judges by: the port
        # says whether the Harness browser is listening; the profile says
        # whether any Chrome process -- ours, or one opened by hand -- still
        # holds it, which is what a preference edit or a fresh start needs.
        report = _audit_report(args, probe().as_dict())
        held = profile_in_use(args.profile)
        report.put(
            "ProfileInUse",
            "unknown" if held is None else held,
            plain="unknown" if held is None else _bool_plain(held),
        )
        report.flush()
        return 0
    if args.command == "browser-stop":
        from .browser.persistent_browser import stop_persistent_browser

        # The browser is asked to exit through the protocol and "stopped" is
        # claimed only after the port has gone quiet and the profile is free.
        # The old command disconnected and called that stopping; Chrome kept
        # running and the next command refused on the profile lock.
        result = stop_persistent_browser(profile_dir=args.profile)
        _audit_report(args, result.as_dict()).flush()
        return 0 if result.ok else 2
    if args.command == "browser-configure-session-restore":
        from .browser.persistent_browser import SESSION_RESTORE_SWITCH, probe

        # Nothing to edit.  Session restore is a launch property of the
        # dedicated browser: browser-start passes Chrome's own switch, which
        # overrides the restore_on_startup preference.  The Preferences edit
        # this command used to make was migrated out of the file by Chrome at
        # the next start (on Windows the key is tracked and MAC-protected),
        # so it reported "configured" for a setting that never took.  The
        # command stays so a runbook that calls it gets the truth, not an
        # unknown-command error.
        running = probe().running
        report = CliReport(bool(getattr(args, "json", False)))
        report.put("SessionRestoreConfigured", True, plain="true")
        report.put("SessionRestoreMechanism", "LAUNCH_SWITCH")
        report.put("SessionRestoreSwitch", SESSION_RESTORE_SWITCH)
        report.put("PreferencesEdited", False, plain="false")
        report.put("ResearchProfile", str(args.profile))
        report.put("PersistentBrowserRunning", running, plain=_bool_plain(running))
        report.note(
            f"browser-start launches the dedicated Research Chrome with {SESSION_RESTORE_SWITCH}, "
            "Chrome's own 'continue where you left off', so the previous session's cookies are "
            "restored rather than deleted at startup. Nothing in the profile is edited: the "
            "restore_on_startup preference is protected by Chrome on Windows and an edit there "
            "is migrated away at the next start."
        )
        if running:
            report.note(
                "A persistent Research Chrome is running. If it was started by a Harness "
                "version without this switch, run browser-stop and then browser-start to "
                "relaunch it with the switch; a sign-in made in the current browser is "
                "kept only across a graceful stop."
            )
        report.flush()
        return 0
    if args.command == "capabilities":
        from .diagnostics import build_capabilities

        print(json.dumps(build_capabilities(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "doctor":
        from .diagnostics import build_doctor

        payload, exit_code = build_doctor()
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return exit_code
    if args.command == "acquire":
        # The literature module owns the live path end to end (it constructs
        # its own PlaywrightBrowser; no host MCP client is involved).
        from .literature.cli import main as literature_main

        return literature_main(_acquire_to_literature_argv(args))
    if args.command == "acquire-batch":
        from .literature.acquire_batch import run_batch_cli

        return run_batch_cli(args)
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
            report = CliReport(bool(getattr(args, "json", False)))
            report.put("LedgerReadable", False, plain="false")
            report.put("Reason", str(exc))
            report.put("FetchesWillBeRefused", True, plain="true")
            report.flush()
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

        metadata = json.loads(_windows_io_path(args.metadata_json).read_text(encoding="utf-8-sig"))
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
