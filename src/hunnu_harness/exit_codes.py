"""The graded exit contract for the acquisition surface.

An agent that does not parse JSON must still branch correctly at the gates
that matter -- above all, it must stop and tell a person instead of retrying
its way around a login wall or a spent budget.  These codes are that channel:

* ``0``  the run completed (including a completed search with zero hits --
         read ``Results`` in the report);
* ``1``  the run failed for a reason outside this ladder; read the report;
* ``2``  a human is required: institutional login, a manual download click,
         or a request that needs a human decision.  Never retried past;
* ``3``  today's fetch budget refused every download this run attempted.
         The lever is ``--daily-limit`` / ``HUNNU_HARNESS_DAILY_FETCH_LIMIT``
         and it belongs to the user, not the agent;
* ``4``  a capability is missing on this machine (for example the Playwright
         browser extra is not installed).  Installing it is the fix;
* ``5``  the environment is not ready (locked profile, unreachable source).
         Retrying without changing the environment will not help.

Scope: ``hunnu-harness acquire`` (and the underlying ``live-*`` commands),
``doctor`` and ``capabilities``.  Two older command families keep their
documented, command-local codes: ``agent-route`` (0 routable / 2 needs a
human decision -- compatible with this ladder's reading of 2) and the
Navigator ``paper-*`` commands (their codes answer retrieval questions and
are documented in docs/PAPER_RESEARCH_NAVIGATOR.md).
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_HUMAN_ACTION_REQUIRED = 2
EXIT_BUDGET_EXHAUSTED = 3
EXIT_CAPABILITY_MISSING = 4
EXIT_ENV_NOT_READY = 5

__all__ = [
    "EXIT_BUDGET_EXHAUSTED",
    "EXIT_CAPABILITY_MISSING",
    "EXIT_ENV_NOT_READY",
    "EXIT_HUMAN_ACTION_REQUIRED",
    "EXIT_OK",
    "EXIT_RUN_FAILED",
]
