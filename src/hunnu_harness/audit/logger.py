from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import _windows_io_path


_SENSITIVE_KEYS = {"password", "passwd", "secret", "token", "cookie", "otp", "mfa", "authorization"}


def _safe(value: Any, key: str | None = None) -> Any:
    if key and key.lower() in _SENSITIVE_KEYS:
        return "<REDACTED>"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


class AuditLogger:
    def __init__(self, path: Path):
        self.path = path
        _windows_io_path(self.path.parent).mkdir(parents=True, exist_ok=True)

    def log(self, action: str, *, status: str, **details: Any) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "status": status,
            **_safe(details),
        }
        with _windows_io_path(self.path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
