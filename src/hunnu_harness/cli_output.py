"""One report, two audiences.

Every Harness command that historically printed ``Key=Value`` lines gains a
``--json`` mode through this class.  Plain mode reproduces the historical
line-by-line output exactly (each ``put`` may carry its own plain-mode
rendering, so booleans keep whatever casing the old output used).  JSON mode
puts a single machine-parseable document on stdout, with real types, and every
human-directed sentence on stderr -- those sentences also travel in-band under
``HumanNotes`` so a JSON-only consumer loses nothing.
"""

from __future__ import annotations

import json
import sys
from typing import Any

_UNSET = object()


class CliReport:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json
        self._emissions: list[tuple[str | None, Any, Any]] = []

    def put(self, key: str, value: Any, *, plain: Any = _UNSET) -> None:
        self._emissions.append((key, value, value if plain is _UNSET else plain))

    def note(self, text: str) -> None:
        self._emissions.append((None, text, text))

    def flush(self) -> None:
        if self.as_json:
            payload: dict[str, Any] = {
                key: value for key, value, _plain in self._emissions if key is not None
            }
            payload["HumanNotes"] = [
                value for key, value, _plain in self._emissions if key is None
            ]
            for key, value, _plain in self._emissions:
                if key is None:
                    print(value, file=sys.stderr)
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
            return
        for key, _value, plain in self._emissions:
            print(f"{key}={plain}" if key is not None else plain)


def attach_json_flag(subparsers: Any) -> None:
    """Give every registered subcommand a uniform ``--json`` switch.

    Commands that already emit a single JSON document accept it as a no-op,
    so one contract holds across the whole CLI: with ``--json``, stdout is
    exactly one ``json.loads``-able document.
    """

    for registered in subparsers.choices.values():
        if not any(action.dest == "json" for action in registered._actions):
            registered.add_argument(
                "--json",
                action="store_true",
                help="stdout carries exactly one JSON document; human notes go to stderr",
            )


__all__ = ["CliReport", "attach_json_flag"]
