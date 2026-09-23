"""Machine-readable answers to "what can this build do" and "is this machine ready".

``capabilities`` is static: it introspects the build (sources, knobs, exit
ladder) and never touches the environment.  ``doctor`` is dynamic: it probes
this machine -- Python floor, Playwright and pypdf imports, Chrome discovery, Output Root
writability, ledger readability, vocabulary state -- and grades the first
blocking finding on the shared exit ladder (see exit_codes).  Neither ever
performs network activity; an agent runs them before spending anything.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from typing import Any

from . import __version__
from .exit_codes import (
    EXIT_CAPABILITY_MISSING,
    EXIT_ENV_NOT_READY,
    EXIT_OK,
)

_SOURCE_ADAPTERS = ("sciencedirect", "springerlink", "cnki", "oxfordacademic")


def build_capabilities() -> dict[str, Any]:
    from .literature.adapters.cnki import CNKIAdapter
    from .literature.adapters.oxfordacademic import OxfordAcademicAdapter
    from .literature.adapters.sciencedirect import ScienceDirectAdapter
    from .literature.adapters.springerlink import SpringerLinkAdapter
    from .literature import fetch_ledger, search_pace
    from .navigator.cli import NAVIGATOR_COMMANDS

    sources = []
    for cli_name, adapter in (
        ("sciencedirect", ScienceDirectAdapter),
        ("springerlink", SpringerLinkAdapter),
        ("cnki", CNKIAdapter),
        ("oxfordacademic", OxfordAcademicAdapter),
    ):
        sources.append(
            {
                "cli_source": cli_name,
                "adapter": adapter.name,
                "supports_search": bool(adapter.supports_search),
                "supports_fulltext_access_check": bool(adapter.supports_fulltext_access_check),
                "supports_authorized_download": bool(adapter.supports_authorized_download),
                "supports_unattended_download": bool(adapter.supports_unattended_download),
                "supports_preflight": bool(adapter.supports_preflight),
            }
        )
    return {
        "HarnessVersion": __version__,
        "Sources": sources,
        "AcquisitionEntry": "hunnu-harness acquire --source <cli_source> ...",
        "BatchAcquisitionEntry": (
            "hunnu-harness acquire-batch --queue <queue.json> [--dry-run] "
            "(up to 25 papers, run one after another; stops at the first manual gate)"
        ),
        "ConcurrentAcquisition": (
            "refused: one process at a time holds the Research Chrome lock "
            "(Output Root/audit/research_chrome.lock)"
        ),
        "NavigatorCommands": sorted(NAVIGATOR_COMMANDS),
        "ExitCodeLadder": {
            "0": "run completed (zero hits included; read Results)",
            "1": "run failed outside the ladder; read the report",
            "2": "human required: login, manual download, or a human decision",
            "3": "daily fetch budget refused every attempted download",
            "4": "capability missing on this machine (installable)",
            "5": "environment not ready (locked profile, unreachable source)",
        },
        "Knobs": {
            "HUNNU_HARNESS_OUTPUT_ROOT": "runtime output root (default: sibling <repo>-Output)",
            "HUNNU_RESEARCH_PROFILE": "dedicated Chrome profile directory",
            "HUNNU_RESEARCH_CHROME": "explicit Chrome executable (else auto-discovery)",
            fetch_ledger.DAILY_FETCH_LIMIT_ENV: (
                f"daily publisher fetch total (default {fetch_ledger.GLOBAL_DAILY_LIMIT}; "
                "also --daily-limit)"
            ),
            fetch_ledger.MIN_FETCH_INTERVAL_ENV: (
                f"minimum seconds between fetches (default {fetch_ledger.MIN_FETCH_INTERVAL_SECONDS})"
            ),
            fetch_ledger.BURST_WINDOW_ENV: (
                f"burst window seconds (default {fetch_ledger.BURST_WINDOW_SECONDS})"
            ),
            fetch_ledger.BURST_LIMIT_ENV: (
                f"fetches allowed per burst window (default {fetch_ledger.BURST_WINDOW_LIMIT})"
            ),
            search_pace.MIN_SEARCH_INTERVAL_ENV: (
                "minimum seconds between publisher searches "
                f"(default {search_pace.MIN_SEARCH_INTERVAL_SECONDS})"
            ),
            search_pace.SEARCH_BURST_WINDOW_ENV: (
                f"search burst window seconds (default {search_pace.SEARCH_BURST_WINDOW_SECONDS})"
            ),
            search_pace.SEARCH_BURST_LIMIT_ENV: (
                f"searches allowed per burst window (default {search_pace.SEARCH_BURST_LIMIT})"
            ),
        },
        "NotKnobs": {
            "per_identifier_daily_limit": (
                "2 fetches per paper per day is loop detection, not quota; "
                "deliberately not configurable"
            ),
        },
        "NetworkActivity": False,
    }


def _check(ok: bool, detail: str) -> dict[str, Any]:
    return {"ok": bool(ok), "detail": detail}


def build_doctor() -> tuple[dict[str, Any], int]:
    from .browser.playwright_backend import discover_chrome_executable
    from .literature.fetch_ledger import FetchLedgerError, FulltextFetchLedger
    from .navigator.lexicon import CONCEPTS
    from .paths import OUTPUT_ROOT

    checks: dict[str, dict[str, Any]] = {}

    checks["python"] = _check(
        sys.version_info >= (3, 11),
        f"{sys.version.split()[0]} (requires >= 3.11)",
    )

    playwright_present = importlib.util.find_spec("playwright") is not None
    checks["playwright"] = _check(
        playwright_present,
        "importable" if playwright_present else
        'not installed; run: pip install -e ".[browser]"',
    )

    # pypdf is a hard dependency, so this only fails under a foreign
    # interpreter -- exactly the case that once turned a `paper-index
    # rebuild` into an empty index.  The build now refuses on its own; the
    # doctor names the cause before anyone gets that far.
    pypdf_present = importlib.util.find_spec("pypdf") is not None
    checks["pypdf"] = _check(
        pypdf_present,
        "importable" if pypdf_present else
        "not importable in this interpreter; paper-index build/rebuild would "
        'refuse to commit -- run: pip install -e "." or use the project .venv',
    )

    chrome = discover_chrome_executable()
    checks["chrome"] = _check(
        chrome is not None,
        str(chrome)
        if chrome is not None
        else (
            "no system Chrome found (checked HUNNU_RESEARCH_CHROME and the "
            "standard install locations); Playwright's bundled Chromium will "
            "be used if installed -- install Google Chrome or run "
            "python -m playwright install chromium"
        ),
    )

    try:
        probe = OUTPUT_ROOT / "temp" / f".doctor-{uuid.uuid4().hex[:8]}.tmp"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks["output_root"] = _check(True, str(OUTPUT_ROOT))
    except OSError as exc:
        checks["output_root"] = _check(False, f"{OUTPUT_ROOT} is not writable: {exc}")

    try:
        usage = FulltextFetchLedger().usage_today()
        checks["fetch_budget"] = _check(
            True,
            f"{usage['AttemptsToday']} attempts today, "
            f"{usage['RemainingGlobalBudget']} of {usage['GlobalDailyLimit']} remaining",
        )
    except (FetchLedgerError, ValueError) as exc:
        checks["fetch_budget"] = _check(False, f"ledger refused: {exc}")

    checks["vocabulary"] = _check(
        True,
        f"{len(CONCEPTS)} concepts loaded"
        if CONCEPTS
        else "empty (legal state; concept expansion and facets are inactive)",
    )

    # The first blocking finding grades the exit. Environment problems come
    # first: installing Playwright will not help while the Output Root is
    # unwritable.
    if not checks["python"]["ok"] or not checks["output_root"]["ok"] or not checks["fetch_budget"]["ok"]:
        verdict, exit_code = "ENV_NOT_READY", EXIT_ENV_NOT_READY
    elif not checks["playwright"]["ok"] or not checks["pypdf"]["ok"]:
        verdict, exit_code = "CAPABILITY_MISSING", EXIT_CAPABILITY_MISSING
    else:
        verdict, exit_code = "READY", EXIT_OK

    payload: dict[str, Any] = {
        "HarnessVersion": __version__,
        "Verdict": verdict,
        "Checks": checks,
        "NetworkActivity": False,
        "Note": (
            "READY means acquisition can start; it does not assert you are "
            "signed in (login state is only observable in a live run) or that "
            "any specific publisher is reachable."
        ),
    }
    if chrome is None and playwright_present:
        payload["ChromeFallback"] = (
            "system Chrome absent: runs will fall back to Playwright's bundled "
            "Chromium if that is installed"
        )
    return payload, exit_code


__all__ = ["build_capabilities", "build_doctor"]
