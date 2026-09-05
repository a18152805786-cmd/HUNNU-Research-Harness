"""Keeping an institutional sign-in alive across a browser lifecycle.

ScienceDirect issues its authenticated session as cookies with no expiry --
``sd_session_id``, ``sd_access``, ``MIAMISESSION`` -- which Chrome persists to
the profile but deletes at startup unless it is continuing the previous
session.  The Harness closes the dedicated browser at the end of a session of
work, so without session restore a sign-in cannot outlive one lifecycle and
every later start arrives as an anonymous off-campus visitor.

The first mechanism, writing ``session.restore_on_startup = 1`` into the
profile's ``Preferences``, verified immediately and was gone after one graceful
lifecycle: the whole ``session`` object came back empty while the sibling
``plugins.always_open_pdf_externally`` edit survived.  That is the signature of
Chrome's tracked-preference migration -- on Windows the key lives in ``Secure
Preferences`` behind a machine-bound MAC, and any copy found in ``Preferences``
is moved out at the next start.  The Harness cannot produce that MAC and must
not try; the operator signed in twice in one day.

These pin the mechanism that replaced it: the dedicated browser is started
with Chrome's own ``--restore-last-session`` switch, which overrides the
preference; the Playwright fallback launch on the same profile passes the same
switch; the Preferences edit is gone; and the CLI command that used to make it
now edits nothing and says so.  The parts of the preference editor that still
stand -- its refusal of any profile but the dedicated one, and the PDF edit
it still makes -- are re-pinned here because they share the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hunnu_harness.browser.pdf_preferences import (
    ResearchChromePdfPreference,
    ResearchChromePreferenceError,
)
from hunnu_harness.browser.persistent_browser import (
    DEFAULT_RESEARCH_DEBUG_PORT,
    SESSION_RESTORE_SWITCH,
    PersistentBrowserStatus,
    endpoint_for,
    start_persistent_browser,
)
from hunnu_harness.browser.playwright_backend import PlaywrightBrowser
from hunnu_harness.cli import main as harness_main
from hunnu_harness.paths import TEMP_DIR

MODULE = "hunnu_harness.browser.persistent_browser"
RUNNING = PersistentBrowserStatus(
    running=True,
    endpoint=endpoint_for(DEFAULT_RESEARCH_DEBUG_PORT),
    port=DEFAULT_RESEARCH_DEBUG_PORT,
    browser_version="Chrome/151.0.0.0",
)
STOPPED = PersistentBrowserStatus(
    running=False,
    endpoint=endpoint_for(DEFAULT_RESEARCH_DEBUG_PORT),
    port=DEFAULT_RESEARCH_DEBUG_PORT,
)

# A Preferences file carries far more than the Harness knows about; these
# neighbours exist so a test fails if anything disturbs them.
NEIGHBOURS = {
    "profile": {"exit_type": "Normal", "name": "Person 1"},
    "download": {"default_directory": "D:" + chr(92) + "somewhere"},
    "extensions": {"settings": {"abc": {"state": 1}}},
    "session": {"restore_on_startup_migrated": True},
}


def _profile(root: Path, preferences: dict) -> Path:
    profile = root / "chrome-profile"
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    (profile / "Default" / "Preferences").write_text(
        json.dumps(preferences, ensure_ascii=False), encoding="utf-8"
    )
    return profile


def _controller(profile: Path, *, running: bool | None = False) -> ResearchChromePdfPreference:
    return ResearchChromePdfPreference(
        profile,
        expected_profile_dir=profile,
        profile_process_check=lambda _: running,
    )


def _chrome() -> Path:
    chrome = TEMP_DIR / "session-restore-chrome.exe"
    chrome.parent.mkdir(parents=True, exist_ok=True)
    chrome.write_bytes(b"")
    return chrome


class LaunchSwitchTests(unittest.TestCase):
    """Session restore is a property of how the browser is started."""

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_browser_start_passes_chromes_own_restore_switch(self) -> None:
        """This failing means a stop-and-start again arrives signed out."""

        spawned: list[list[str]] = []
        with mock.patch(f"{MODULE}.probe", side_effect=[STOPPED, RUNNING]):
            status = start_persistent_browser(
                profile_dir=TEMP_DIR / "session-restore-profile",
                chrome_executable=_chrome(),
                spawn=spawned.append,
            )

        self.assertEqual(len(spawned), 1)
        self.assertIn(SESSION_RESTORE_SWITCH, spawned[0])
        self.assertEqual(SESSION_RESTORE_SWITCH, "--restore-last-session")
        # The switch sits with the other switches, before the URL Chrome opens.
        self.assertLess(spawned[0].index(SESSION_RESTORE_SWITCH), spawned[0].index("about:blank"))
        self.assertIs(status.session_restore, True)
        self.assertIs(status.as_dict()["PersistentBrowserSessionRestore"], True)

    def test_a_deliberately_fresh_start_can_leave_the_switch_out(self) -> None:
        spawned: list[list[str]] = []
        with mock.patch(f"{MODULE}.probe", side_effect=[STOPPED, RUNNING]):
            status = start_persistent_browser(
                profile_dir=TEMP_DIR / "session-restore-profile",
                chrome_executable=_chrome(),
                spawn=spawned.append,
                restore_last_session=False,
            )

        self.assertNotIn(SESSION_RESTORE_SWITCH, spawned[0])
        self.assertIs(status.session_restore, False)
        self.assertIs(status.as_dict()["PersistentBrowserSessionRestore"], False)

    def test_a_browser_that_was_already_running_reports_the_switch_as_unknown(self) -> None:
        """Its command line is not this call's to know, so it is not guessed."""

        with mock.patch(f"{MODULE}.probe", return_value=RUNNING):
            status = start_persistent_browser(
                profile_dir=TEMP_DIR / "session-restore-profile",
                chrome_executable=_chrome(),
                spawn=lambda command: None,
            )

        self.assertIsNone(status.session_restore)
        self.assertEqual(status.as_dict()["PersistentBrowserSessionRestore"], "unknown")

    def test_browser_start_reports_the_switch_it_passed(self) -> None:
        started = PersistentBrowserStatus(
            running=True,
            endpoint=RUNNING.endpoint,
            port=RUNNING.port,
            profile_dir="profile",
            session_restore=True,
        )
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(f"{MODULE}.start_persistent_browser", return_value=started):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = harness_main(["browser-start", "--chrome", str(_chrome()), "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertIs(payload["PersistentBrowserSessionRestore"], True)
        self.assertIs(payload["BrowserLeftRunning"], True)


class _FakeContext:
    def __init__(self) -> None:
        self.pages = [_FakePage()]

    async def close(self) -> None:
        return None


class _FakePage:
    url = "about:blank"

    async def title(self) -> str:
        return ""


class _FakeLaunchingPlaywright:
    def __init__(self) -> None:
        self.chromium = self
        self.launch_kwargs: dict = {}

    async def launch_persistent_context(self, **kwargs):  # noqa: ANN003
        self.launch_kwargs = kwargs
        return _FakeContext()

    async def stop(self) -> None:
        return None


class _Starter:
    def __init__(self, playwright: _FakeLaunchingPlaywright) -> None:
        self._playwright = playwright

    async def start(self) -> _FakeLaunchingPlaywright:
        return self._playwright


class FallbackLaunchTests(unittest.TestCase):
    """A run that launches its own Chrome on the shared profile must not wipe the sign-in."""

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self.profile = TEMP_DIR / "fallback-launch-profile"
        self.profile.mkdir(parents=True, exist_ok=True)

    def test_the_playwright_fallback_launch_passes_the_same_switch(self) -> None:
        fake = _FakeLaunchingPlaywright()
        browser = PlaywrightBrowser(
            profile_dir=self.profile,
            downloads_dir=TEMP_DIR / "fallback-launch-downloads",
            executable_path=_chrome(),
        )
        with mock.patch(f"{MODULE}.probe", return_value=STOPPED), mock.patch(
            "playwright.async_api.async_playwright", return_value=_Starter(fake)
        ):
            asyncio.run(browser.start())

        self.assertFalse(browser.attached)
        self.assertIn(SESSION_RESTORE_SWITCH, fake.launch_kwargs.get("args", []))
        self.assertEqual(fake.launch_kwargs["user_data_dir"], str(browser.profile_dir))


class PreferencesEditGoneTests(unittest.TestCase):
    """The edit Chrome migrates out of Preferences must not come back."""

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_the_preference_editor_no_longer_offers_a_session_restore_edit(self) -> None:
        """This failing means the ineffective Preferences edit is back, and a
        command will again report "configured" for a setting Chrome discards."""

        self.assertFalse(hasattr(ResearchChromePdfPreference, "configure_session_restore"))

    def test_inspecting_reports_what_preferences_says_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-inspect-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), {"session": {"restore_on_startup": 5}})
            before = (profile / "Default" / "Preferences").read_bytes()

            self.assertEqual(_controller(profile).inspect_session_restore(), 5)

            self.assertEqual((profile / "Default" / "Preferences").read_bytes(), before)

    def test_an_absent_value_reads_as_none(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-absent-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))
            self.assertIsNone(_controller(profile).inspect_session_restore())

    def test_a_boolean_is_never_read_as_the_integer_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-bool-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), {"session": {"restore_on_startup": True}})
            self.assertIsNone(_controller(profile).inspect_session_restore())


class CliCommandTests(unittest.TestCase):
    """browser-configure-session-restore edits nothing and reports the mechanism."""

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = harness_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_the_command_leaves_preferences_byte_identical_and_names_the_switch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-cli-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))
            before = (profile / "Default" / "Preferences").read_bytes()
            with mock.patch(f"{MODULE}.probe", return_value=STOPPED):
                code, out, err = self._run(
                    ["browser-configure-session-restore", "--profile", str(profile), "--json"]
                )

            self.assertEqual(code, 0)
            self.assertEqual((profile / "Default" / "Preferences").read_bytes(), before)
            payload = json.loads(out)
            self.assertIs(payload["SessionRestoreConfigured"], True)
            self.assertEqual(payload["SessionRestoreMechanism"], "LAUNCH_SWITCH")
            self.assertEqual(payload["SessionRestoreSwitch"], "--restore-last-session")
            self.assertIs(payload["PreferencesEdited"], False)
            self.assertIs(payload["PersistentBrowserRunning"], False)
            # The old report's preference audit is gone with the edit.
            self.assertNotIn("PreferenceName", payload)
            self.assertNotIn("NewValue", payload)
            self.assertTrue(any("browser-start" in note for note in payload["HumanNotes"]))
            self.assertIn("browser-start", err)

    def test_the_command_no_longer_needs_the_browser_stopped(self) -> None:
        """Nothing is edited, so a running browser is reported, not refused --
        with the advice to relaunch one started before the switch existed."""

        with tempfile.TemporaryDirectory(prefix="sr-cli-running-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))
            with mock.patch(f"{MODULE}.probe", return_value=RUNNING):
                code, out, _err = self._run(
                    ["browser-configure-session-restore", "--profile", str(profile), "--json"]
                )

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIs(payload["PersistentBrowserRunning"], True)
        self.assertTrue(any("browser-stop" in note for note in payload["HumanNotes"]))

    def test_plain_output_keeps_key_value_lines(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-cli-plain-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))
            with mock.patch(f"{MODULE}.probe", return_value=STOPPED):
                code, out, _err = self._run(
                    ["browser-configure-session-restore", "--profile", str(profile)]
                )

        self.assertEqual(code, 0)
        self.assertIn("SessionRestoreConfigured=true\n", out)
        self.assertIn("PreferencesEdited=false\n", out)
        self.assertIn("SessionRestoreSwitch=--restore-last-session\n", out)


class PreferenceEditorStillStandsTests(unittest.TestCase):
    """The PDF preference shares the file and its guards; they are re-pinned here."""

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_the_pdf_preference_is_still_edited_in_place(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-pdf-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))

            _controller(profile).configure_direct_download()

            payload = json.loads(
                (profile / "Default" / "Preferences").read_text(encoding="utf-8")
            )
            self.assertIs(payload["plugins"]["always_open_pdf_externally"], True)
            self.assertEqual(payload["profile"], NEIGHBOURS["profile"])
            # The session object is left exactly as Chrome had it.
            self.assertEqual(payload["session"], NEIGHBOURS["session"])

    def test_it_refuses_a_profile_that_is_not_the_dedicated_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-wrong-", dir=TEMP_DIR) as tmp:
            root = Path(tmp)
            profile = _profile(root, dict(NEIGHBOURS))
            with self.assertRaises(ResearchChromePreferenceError):
                ResearchChromePdfPreference(
                    profile, expected_profile_dir=root / "somewhere-else"
                )

    def test_the_daily_chrome_profile_is_out_of_scope(self) -> None:
        """The one profile this must never touch is the browser the user lives in."""

        import os

        local = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
        daily = local / "Google" / "Chrome" / "User Data" / "Default"
        with self.assertRaises(ResearchChromePreferenceError):
            ResearchChromePdfPreference(daily, expected_profile_dir=daily)


if __name__ == "__main__":
    unittest.main()
