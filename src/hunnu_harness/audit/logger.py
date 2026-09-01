from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..literature.security import sanitize_text, sanitize_value
from ..paths import _windows_io_path


class AuditLogger:
    """Append-only JSONL audit log with full-strength sanitization.

    This logger used to carry its own redaction: an exact-match set of eight
    key names.  ``Set-Cookie`` sailed past it (exact match, not fragment
    match), and a signed URL inside an ordinary value -- ``detail``, ``url``
    -- went to disk verbatim.  The literature pipeline's sanitizer
    (literature/security.py) already handles both: fragment-based key
    matching, labeled-secret and bearer scrubbing inside string values, and
    query/userinfo stripping for every URL.  There is exactly one sanitization
    policy now; this class delegates to it.
    """

    def __init__(self, path: Path):
        self.path = path
        _windows_io_path(self.path.parent).mkdir(parents=True, exist_ok=True)

    def log(self, action: str, *, status: str, **details: Any) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": sanitize_text(action),
            "status": sanitize_text(status),
            **sanitize_value(details),
        }
        with _windows_io_path(self.path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
