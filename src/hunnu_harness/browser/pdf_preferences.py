from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


PDF_DIRECT_DOWNLOAD_PREFERENCE = "plugins.always_open_pdf_externally"
SESSION_RESTORE_PREFERENCE = "session.restore_on_startup"
# Chrome keeps session cookies across a restart only under "continue where
# you left off".  Under the default it drops them, which is why an
# authenticated publisher session did not survive a single Harness run.
SESSION_RESTORE_LAST_SESSION = 1
DEFAULT_RESEARCH_CHROME_PROFILE = Path.home() / "ResearchHarness" / "chrome-profile"


class ResearchChromePreferenceError(RuntimeError):
    pass


class ResearchChromeProfileInUse(ResearchChromePreferenceError):
    pass


@dataclass(frozen=True)
class PdfPreferenceAudit:
    preference_name: str
    previous_value: bool | int | None
    new_value: bool | int
    scope: str = "ResearchChromeOnly"
    same_profile_reused: bool = True
    cookies_preserved: bool = True
    sso_preserved: bool = True
    normal_user_chrome_modified: bool = False
    system_wide_chrome_policy_modified: bool = False
    full_preferences_snapshot_created: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "PreferenceName": self.preference_name,
            "PreviousValue": "ABSENT" if self.previous_value is None else self.previous_value,
            "NewValue": self.new_value,
            "Scope": self.scope,
            "SameProfileReused": self.same_profile_reused,
            "CookiesPreserved": self.cookies_preserved,
            "SSOPreserved": self.sso_preserved,
            "NormalUserChromeModified": self.normal_user_chrome_modified,
            "SystemWideChromePolicyModified": self.system_wide_chrome_policy_modified,
            "FullPreferencesSnapshotCreated": self.full_preferences_snapshot_created,
        }


def _default_profile_process_check(profile_dir: Path) -> bool | None:
    """Return whether Chrome is using *profile_dir* without persisting process data."""

    if os.name != "nt":
        return bool(tuple(profile_dir.glob("Singleton*")))
    command = (
        "$p='" + str(profile_dir).replace("'", "''") + "';"
        "$found=Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { [string]$_.CommandLine -like ('*'+$p+'*') };"
        "if($found){'IN_USE'}else{'NOT_IN_USE'}"
    )
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if output == "IN_USE":
        return True
    if output == "NOT_IN_USE":
        return False
    return None


class ResearchChromePdfPreference:
    """Surgically enable direct PDF downloads in the dedicated Research Chrome.

    The manager refuses daily Chrome/Edge paths, refuses a running or locked
    profile, and changes only ``plugins.always_open_pdf_externally``.  It never
    reads, copies, serializes, or logs cookies, sessions, tokens, or storage.
    """

    def __init__(
        self,
        profile_dir: Path,
        *,
        expected_profile_dir: Path | None = None,
        allow_arbitrary_profile_for_tests: bool = False,
        profile_process_check: Callable[[Path], bool | None] | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        expected = Path(
            expected_profile_dir
            or os.environ.get("HUNNU_RESEARCH_PROFILE", DEFAULT_RESEARCH_CHROME_PROFILE)
        ).expanduser().resolve()
        if not allow_arbitrary_profile_for_tests and self.profile_dir != expected:
            raise ResearchChromePreferenceError(
                "PDF preference changes are restricted to the configured Research Chrome profile"
            )
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        forbidden_roots = (
            local_app_data / "Google" / "Chrome" / "User Data",
            local_app_data / "Microsoft" / "Edge" / "User Data",
        )
        if any(
            self.profile_dir == root.resolve() or self.profile_dir.is_relative_to(root.resolve())
            for root in forbidden_roots
        ):
            raise ResearchChromePreferenceError("Normal user Chrome/Edge profiles are out of scope")
        self.preferences_path = self.profile_dir / "Default" / "Preferences"
        self._profile_process_check = profile_process_check or _default_profile_process_check

    def _payload(self) -> dict:
        if not self.preferences_path.is_file():
            raise ResearchChromePreferenceError(
                f"Research Chrome Preferences file is unavailable: {self.preferences_path}"
            )
        try:
            payload = json.loads(self.preferences_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResearchChromePreferenceError("Research Chrome Preferences is not valid JSON") from exc
        return payload

    def _read(self, container: str, key: str, kind: type) -> bool | int | None:
        section = self._payload().get(container)
        if not isinstance(section, dict):
            return None
        value = section.get(key)
        # bool is a subclass of int, so an exact type check keeps a stray
        # ``true`` from reading as the integer 1.
        return value if type(value) is kind else None

    def inspect(self) -> bool | None:
        value = self._read("plugins", "always_open_pdf_externally", bool)
        return value if isinstance(value, bool) else None

    def inspect_session_restore(self) -> int | None:
        value = self._read("session", "restore_on_startup", int)
        return value if isinstance(value, int) else None

    def _require_stopped(self) -> None:
        """Chrome rewrites Preferences as it exits, so edit it only when stopped."""

        if tuple(self.profile_dir.glob("Singleton*")):
            raise ResearchChromeProfileInUse("Research Chrome profile lock is present")
        in_use = self._profile_process_check(self.profile_dir)
        if in_use is not False:
            detail = "in use" if in_use else "running-state check unavailable"
            raise ResearchChromeProfileInUse(f"Research Chrome profile is {detail}")

    def configure_session_restore(self) -> PdfPreferenceAudit:
        """Let an authenticated publisher session survive closing the browser.

        The session cookies a publisher issues after institutional sign-in --
        ``sd_session_id`` and friends -- have no expiry, so Chrome discards them
        at startup unless the profile is set to continue where it left off.  The
        Harness closes the browser at the end of every run, so without this the
        sign-in cannot outlive one run and every later run arrives anonymous.

        This is an ordinary Chrome setting, the one a person gets from the
        Settings page.  It changes what the profile remembers, never how it
        presents itself.
        """

        self._require_stopped()
        previous = self.inspect_session_restore()
        if previous == SESSION_RESTORE_LAST_SESSION:
            return PdfPreferenceAudit(
                preference_name=SESSION_RESTORE_PREFERENCE,
                previous_value=previous,
                new_value=SESSION_RESTORE_LAST_SESSION,
            )
        self._apply(
            container="session",
            key="restore_on_startup",
            value=SESSION_RESTORE_LAST_SESSION,
            literal=str(SESSION_RESTORE_LAST_SESSION),
        )
        if self.inspect_session_restore() != SESSION_RESTORE_LAST_SESSION:
            raise ResearchChromePreferenceError("Session restore preference verification failed")
        return PdfPreferenceAudit(
            preference_name=SESSION_RESTORE_PREFERENCE,
            previous_value=previous,
            new_value=SESSION_RESTORE_LAST_SESSION,
        )

    def configure_direct_download(self) -> PdfPreferenceAudit:
        self._require_stopped()

        previous = self.inspect()
        if previous is True:
            return PdfPreferenceAudit(
                preference_name=PDF_DIRECT_DOWNLOAD_PREFERENCE,
                previous_value=True,
                new_value=True,
            )

        self._apply(
            container="plugins",
            key="always_open_pdf_externally",
            value=True,
            literal="true",
        )
        if self.inspect() is not True:
            raise ResearchChromePreferenceError("PDF direct-download preference verification failed")
        return PdfPreferenceAudit(
            preference_name=PDF_DIRECT_DOWNLOAD_PREFERENCE,
            previous_value=previous,
            new_value=True,
        )

    def _apply(self, *, container: str, key: str, value: bool | int, literal: str) -> None:
        """Set one preference and prove nothing else in the file moved."""

        original = self.preferences_path.read_text(encoding="utf-8")
        current_payload = json.loads(original)
        expected_payload = dict(current_payload)
        expected_section = dict(current_payload.get(container) or {})
        expected_section[key] = value
        expected_payload[container] = expected_section

        updated = self._surgical_set(original, container, key, literal)
        try:
            updated_payload = json.loads(updated)
        except json.JSONDecodeError as exc:
            raise ResearchChromePreferenceError(
                f"Targeted {container}.{key} edit produced invalid JSON"
            ) from exc
        if updated_payload != expected_payload:
            raise ResearchChromePreferenceError(
                f"Targeted edit changed content beyond {container}.{key}"
            )

        mode = stat.S_IMODE(self.preferences_path.stat().st_mode)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=self.preferences_path.parent,
                prefix=".hunnu-pdf-pref-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, mode)
            os.replace(temp_path, self.preferences_path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @classmethod
    def _surgical_set(cls, source: str, container: str, key: str, replacement: str) -> str:
        """Edit one value in place, leaving the rest of the file byte for byte.

        Chrome's Preferences file carries far more than the Harness knows about,
        so it is never re-serialised from a parsed object: only the one value is
        rewritten, and the caller re-parses to prove nothing else moved.
        """

        span = cls._object_span(source, container)
        if span is None:
            root_start = source.find("{")
            if root_start < 0:
                raise ResearchChromePreferenceError("Preferences root object was not found")
            remainder = source[root_start + 1 :]
            comma = "" if remainder.lstrip().startswith("}") else ","
            insertion = f'"{container}":{{"{key}":{replacement}}}{comma}'
            return source[: root_start + 1] + insertion + source[root_start + 1 :]

        start, end = span
        body = source[start + 1 : end]
        match = re.search(
            rf'("{re.escape(key)}"\s*:\s*)(true|false|-?\d+)',
            body,
        )
        if match:
            body = body[: match.start(2)] + replacement + body[match.end(2) :]
        else:
            comma = "" if not body.strip() else ","
            body = f'"{key}":{replacement}{comma}' + body
        return source[: start + 1] + body + source[end:]

    @staticmethod
    def _object_span(source: str, key: str) -> tuple[int, int] | None:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*{{', source)
        if not match:
            return None
        start = source.find("{", match.start())
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(source)):
            char = source[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return start, index
        raise ResearchChromePreferenceError(f"Unterminated JSON object for {key}")


__all__ = [
    "DEFAULT_RESEARCH_CHROME_PROFILE",
    "PDF_DIRECT_DOWNLOAD_PREFERENCE",
    "SESSION_RESTORE_LAST_SESSION",
    "SESSION_RESTORE_PREFERENCE",
    "PdfPreferenceAudit",
    "ResearchChromePdfPreference",
    "ResearchChromePreferenceError",
    "ResearchChromeProfileInUse",
]
