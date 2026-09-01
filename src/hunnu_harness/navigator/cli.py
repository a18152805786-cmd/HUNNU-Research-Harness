"""Navigator subcommands for the ``hunnu-harness`` CLI.

Follows the existing entry-point conventions: ``build_parser``-style
registration, machine output as sorted UTF-8 JSON, meaningful exit codes, and
lazy construction so an unrelated Harness command pays nothing for this module.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .catalog import LibraryUnavailable
from ..paths import _windows_io_path


EXIT_OK = 0
EXIT_LIBRARY_UNAVAILABLE = 2
EXIT_NOT_FOUND = 3
EXIT_INDEX_FAILED = 4
#: A citation matched several works equally well.  Distinct from NOT_FOUND
#: because the next action differs: disambiguate, do not acquire.
EXIT_AMBIGUOUS = 5


def add_navigator_subcommands(sub: argparse._SubParsersAction) -> None:
    """Register every ``paper-*`` subcommand on an existing subparser action."""

    search = sub.add_parser(
        "paper-search",
        help="Search the Global Paper Library by natural-language or research question",
    )
    search.add_argument("--query", required=True)
    search.add_argument("--top", type=int, default=10)
    search.add_argument(
        "--no-fulltext",
        action="store_true",
        help="Metadata/topic recall only; skip the full-text rerank stage",
    )
    search.add_argument(
        "--no-expand",
        action="store_true",
        help="Disable cross-language concept expansion",
    )

    lookup = sub.add_parser(
        "paper-lookup",
        help="Resolve an explicit identity: DOI, PaperID, exact title, author names, journal",
    )
    lookup.add_argument("--query", required=True)
    lookup.add_argument("--limit", type=int, default=10)

    fulltext = sub.add_parser(
        "paper-fulltext",
        help="Report every managed version of one WORK and which one to read",
    )
    fulltext.add_argument("--paper-id", required=True)

    related = sub.add_parser("paper-related", help="Find WORKS related to one WORK")
    related.add_argument("--paper-id", required=True)
    related.add_argument("--top", type=int, default=10)

    pack = sub.add_parser(
        "paper-pack",
        help="Build a reading pack of references for a research question; copies no PDF",
    )
    pack.add_argument("--query", required=True)
    pack.add_argument("--top", type=int, default=15)
    pack.add_argument("--no-fulltext", action="store_true")
    pack.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the pack without writing it to the Output Root",
    )

    gaps = sub.add_parser(
        "paper-gaps",
        help="Local library coverage analysis for a research question (never a novelty claim)",
    )
    gaps.add_argument("--query", required=True)
    gaps.add_argument("--top", type=int, default=25)

    verify = sub.add_parser(
        "paper-verify-citation",
        help="Check whether a cited work is already held; hand off to acquisition if not",
    )
    verify.add_argument("--citation", required=True)
    verify.add_argument("--max-candidates", type=int, default=5)

    index = sub.add_parser("paper-index", help="Manage the derived retrieval index")
    index.add_argument("action", choices=("status", "build", "validate", "rebuild", "drop"))
    index.add_argument("--page-limit", type=int, default=None)
    index.add_argument("--quiet", action="store_true", help="Suppress per-work build progress")

    fingerprint = sub.add_parser(
        "paper-fingerprint",
        help="Capture or compare a read-only fingerprint of the frozen library",
    )
    fingerprint.add_argument("--output", type=Path, default=None)
    fingerprint.add_argument("--compare", type=Path, default=None)
    fingerprint.add_argument("--summary", action="store_true", help="Print counts instead of the full payload")


NAVIGATOR_COMMANDS = frozenset(
    {
        "paper-search",
        "paper-lookup",
        "paper-fulltext",
        "paper-related",
        "paper-pack",
        "paper-gaps",
        "paper-verify-citation",
        "paper-index",
        "paper-fingerprint",
    }
)


def emit_utf8(text: str) -> None:
    """Write machine-readable output as UTF-8, whatever the console codepage is.

    The corpus is 90% Chinese, so this boundary decides whether an agent can read
    the answer at all.  The previous implementation printed through the ambient
    text stream and only fell back to UTF-8 bytes on ``UnicodeEncodeError``.
    That rescues encodings which fail loudly -- cp1252, ascii -- and misses the
    one that matters on this machine: GBK *can* encode CJK, so it raises nothing
    and quietly emits GBK bytes, which any UTF-8 reader sees as mojibake.

    Structured output declares its own encoding rather than inheriting one.
    This is not a re-encoding guess: the string is already correct Unicode
    (verified end to end), and it is written once, as UTF-8.
    """

    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        # A text-only stream (pytest capture, an in-process harness). It already
        # holds Unicode, so there is nothing to correct.
        print(text)
        return
    sys.stdout.flush()
    stream.write(text.encode("utf-8") + b"\n")
    stream.flush()


def _emit(payload: Any) -> None:
    emit_utf8(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _library_unavailable_payload(exc: Exception) -> dict[str, Any]:
    """Explain the empty state instead of leaving an agent to guess.

    On a fresh install there is no Global Paper Library at all, and the first
    thing anyone runs into is this handler.  "Unavailable" alone reads as
    breakage; the note says what is actually true -- nothing has been acquired
    yet -- and what fills the library.
    """

    return {
        "status": "LIBRARY_UNAVAILABLE",
        "reason": str(exc),
        "empty_library_note": (
            "A fresh install starts with no Global Paper Library -- this state "
            "is expected, not broken. The library fills as the acquisition "
            "pipeline archives validated full texts (hunnu-harness acquire / "
            "the live-* commands), or as individual PDFs pass library-stage + "
            "library-import. Once at least one work is archived, every "
            "paper-* command answers normally."
        ),
    }


def _navigator():
    from .search import PaperNavigator

    return PaperNavigator()


def run_navigator_command(args: argparse.Namespace) -> int:
    """Dispatch one ``paper-*`` command.  Never raises for expected conditions."""

    command = args.command
    try:
        if command == "paper-fingerprint":
            return _run_fingerprint(args)
        if command == "paper-index":
            return _run_index(args)

        navigator = _navigator()

        if command == "paper-search":
            payload = navigator.search(
                args.query,
                top=args.top,
                use_fulltext=not args.no_fulltext,
                expand=not args.no_expand,
            )
            _emit(payload)
            return EXIT_OK if payload["results"] else EXIT_NOT_FOUND

        if command == "paper-lookup":
            payload = navigator.lookup(args.query, limit=args.limit)
            _emit(payload)
            return EXIT_OK if payload["status"] != "NOT_IN_LIBRARY" else EXIT_NOT_FOUND

        if command == "paper-fulltext":
            payload = navigator.fulltext(args.paper_id)
            _emit(payload)
            return EXIT_OK if payload["status"] == "FOUND" else EXIT_NOT_FOUND

        if command == "paper-related":
            from .related import RelatedWorkFinder

            payload = RelatedWorkFinder(navigator).find(args.paper_id, top=args.top)
            _emit(payload)
            return EXIT_OK if payload["status"] == "FOUND" else EXIT_NOT_FOUND

        if command == "paper-pack":
            from .packs import ReadingPackBuilder

            payload = ReadingPackBuilder(navigator).build(
                args.query,
                top=args.top,
                use_fulltext=not args.no_fulltext,
                write=not args.dry_run,
            )
            _emit(payload)
            return EXIT_OK if payload["entry_count"] else EXIT_NOT_FOUND

        if command == "paper-gaps":
            from .gaps import CoverageAnalyzer

            _emit(CoverageAnalyzer(navigator).analyze(args.query, top=args.top))
            return EXIT_OK

        if command == "paper-verify-citation":
            from .citation import CitationStatus, CitationVerifier

            payload = CitationVerifier(navigator).verify(
                args.citation, max_candidates=args.max_candidates
            )
            _emit(payload)
            if payload["status"] == CitationStatus.IN_LIBRARY.value:
                return EXIT_OK
            if payload["status"] == CitationStatus.AMBIGUOUS.value:
                return EXIT_AMBIGUOUS
            return EXIT_NOT_FOUND

    except LibraryUnavailable as exc:
        _emit(_library_unavailable_payload(exc))
        return EXIT_LIBRARY_UNAVAILABLE

    raise SystemExit(f"Unknown Navigator command: {command}")


def _run_index(args: argparse.Namespace) -> int:
    from .catalog import CatalogReader
    from .fulltext import FullTextExtractor
    from .index import NavigatorIndex

    try:
        snapshot = CatalogReader().load()
    except LibraryUnavailable as exc:
        _emit(_library_unavailable_payload(exc))
        return EXIT_LIBRARY_UNAVAILABLE

    extractor = (
        FullTextExtractor(page_limit=args.page_limit) if args.page_limit else FullTextExtractor()
    )
    index = NavigatorIndex(extractor=extractor)

    if args.action == "status":
        status, detail = index.status(snapshot)
        _emit(
            {
                "status": status.value,
                "detail": detail,
                "works_in_catalog": snapshot.work_count,
                "physical_versions": snapshot.version_count,
                "index_dir": str(index.index_dir),
            }
        )
        return EXIT_OK

    if args.action == "validate":
        report = index.validate(snapshot)
        _emit(report)
        return EXIT_OK if report["valid"] else EXIT_INDEX_FAILED

    if args.action == "drop":
        index.drop()
        _emit({"status": "DROPPED", "index_dir": str(index.index_dir)})
        return EXIT_OK

    def progress(paper_id: str, position: int, total: int) -> None:
        if not args.quiet:
            print(f"[{position}/{total}] {paper_id}", file=sys.stderr)

    build = index.rebuild if args.action == "rebuild" else index.build
    _emit(build(snapshot, progress=progress))
    return EXIT_OK


def _run_fingerprint(args: argparse.Namespace) -> int:
    from .fingerprint import LibraryFingerprinter, load_fingerprint

    try:
        current = LibraryFingerprinter().capture()
    except LibraryUnavailable as exc:
        _emit(_library_unavailable_payload(exc))
        return EXIT_LIBRARY_UNAVAILABLE

    if args.output:
        _windows_io_path(Path(args.output)).write_text(current.to_json(), encoding="utf-8", newline="\n")

    if args.compare:
        comparison = current.compare(load_fingerprint(Path(args.compare)))
        _emit(
            {
                "status": "COMPARED",
                "baseline": str(args.compare),
                "written": str(args.output) if args.output else None,
                **comparison,
            }
        )
        return EXIT_OK if comparison["identical"] else EXIT_INDEX_FAILED

    if args.summary:
        payload = current.as_dict()
        _emit(
            {
                "status": "CAPTURED",
                "written": str(args.output) if args.output else None,
                "catalog_records": payload["catalog_records"],
                "physical_versions": payload["physical_versions"],
                "nested_variants": payload["nested_variants"],
                "papers_file_count": payload["papers_file_count"],
                "topic_assignments": payload["topic_assignment_count"],
                "unique_topic_values": payload["unique_topic_values"],
                "papers_by_topic_entries": payload["papers_by_topic_entries"],
                "fingerprint_sha256": payload["fingerprint_sha256"],
            }
        )
        return EXIT_OK

    _emit(current.as_dict())
    return EXIT_OK


__all__ = [
    "EXIT_AMBIGUOUS",
    "EXIT_INDEX_FAILED",
    "EXIT_LIBRARY_UNAVAILABLE",
    "EXIT_NOT_FOUND",
    "EXIT_OK",
    "NAVIGATOR_COMMANDS",
    "add_navigator_subcommands",
    "emit_utf8",
    "run_navigator_command",
]
