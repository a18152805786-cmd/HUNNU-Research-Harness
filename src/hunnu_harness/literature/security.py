"""Sanitization and leak scanning for everything the Harness writes down.

TO THE MODIFYING AGENT: this is the single sanitization policy for audit
logs and agent-facing output.  Narrowing a pattern here, or exempting a key
because redaction "hides useful detail", widens what reaches disk -- ask the
user first, in so many words.  Over-redaction is the intended failure mode.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from ..paths import _windows_io_path


SENSITIVE_TERMS = (
    "password",
    "passwd",
    "cookie",
    "authorization",
    "bearer",
    "sessionid",
    "token",
    "saml",
    "oauth",
)

_SENSITIVE_KEY_FRAGMENTS = {
    *SENSITIVE_TERMS,
    "secret",
    "credential",
    "otp",
    "mfa",
    "session",
    "authheader",
    "accesskey",
    "signature",
}

_SAFE_BOOLEAN_SECURITY_KEYS = {
    "currentsessiondownloadverified",
}

_LABELED_SECRET = re.compile(
    r"(?i)\b(password|passwd|cookie|authorization|sessionid|token|secret|credential|signature|otp|mfa|saml(?:_?assertion)?|oauth(?:_?credential)?)\b"
    r"\s*[:=]\s*(?:bearer\s+)?([^\s,;&]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[^\s,;]+")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _sensitive_key(key: str) -> bool:
    lowered = re.sub(r"[^a-z0-9]", "", key.casefold())
    if lowered in _SAFE_BOOLEAN_SECURITY_KEYS:
        return False
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def sanitize_url(value: str) -> str:
    """Return a stable URL without userinfo, query parameters, or fragments."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<REDACTED_URL>"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "<REDACTED_URL>"
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def sanitize_text(value: str) -> str:
    text = str(value)
    text = _BEARER_SECRET.sub("Authorization: <REDACTED>", text)
    text = _LABELED_SECRET.sub(lambda match: f"{match.group(1)}=<REDACTED>", text)
    text = _URL_PATTERN.sub(lambda match: sanitize_url(match.group(0)), text)
    return text


def sanitize_value(value: Any, key: str | None = None) -> Any:
    if key is not None and _sensitive_key(key):
        if value is False or value is None or value == "<REDACTED>":
            return value
        return "<REDACTED>"
    if isinstance(value, dict):
        return {str(item_key): sanitize_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


class LiteratureAuditLogger:
    """Append-only JSONL logger with key, text, and URL sanitization."""

    def __init__(self, path: Path):
        self.path = Path(path)
        _windows_io_path(self.path.parent).mkdir(parents=True, exist_ok=True)

    def log(self, action: str, *, status: str, **details: Any) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": sanitize_text(action),
            "status": sanitize_text(status),
            **sanitize_value(details),
        }
        with _windows_io_path(self.path).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


@dataclass(frozen=True)
class SensitiveLeakFinding:
    path: Path
    line_number: int
    term: str
    redacted_evidence: str


def _looks_like_safe_policy_value(value: str) -> bool:
    normalized = value.strip("\"'`<>[]{}().").casefold()
    return normalized.startswith(("redacted_test_value", "not_persisted_test_value")) or normalized in {
        "false",
        "true",
        "none",
        "null",
        "unknown",
        "redacted",
        "notstored",
        "not_stored",
        "disabled",
        "0",
    }


def scan_text_for_sensitive_leaks(text: str, *, path: Path = Path("<memory>")) -> list[SensitiveLeakFinding]:
    """Scan every required term, reporting only credential-shaped occurrences.

    Policy statements such as ``CredentialsStored=false`` are not leaks. Values
    adjacent to sensitive labels and bearer credentials are leaks. Evidence is
    always redacted before it leaves this function.
    """

    findings: list[SensitiveLeakFinding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        bearer = re.search(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{6,})", line)
        if bearer and not _looks_like_safe_policy_value(bearer.group(1)):
            findings.append(
                SensitiveLeakFinding(path, line_number, "bearer", "Bearer <REDACTED>")
            )
        for match in _LABELED_SECRET.finditer(line):
            term, candidate = match.group(1).casefold(), match.group(2)
            if candidate == "<REDACTED>" or _looks_like_safe_policy_value(candidate):
                continue
            findings.append(
                SensitiveLeakFinding(path, line_number, term, f"{term}=<REDACTED>")
            )
    return findings


def scan_files_for_sensitive_leaks(paths: Iterable[Path]) -> list[SensitiveLeakFinding]:
    findings: list[SensitiveLeakFinding] = []
    for path in paths:
        candidate = Path(path)
        candidate_io = _windows_io_path(candidate)
        if not candidate_io.exists() or not candidate_io.is_file():
            continue
        try:
            content = candidate_io.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text_for_sensitive_leaks(content, path=candidate))
    return findings
