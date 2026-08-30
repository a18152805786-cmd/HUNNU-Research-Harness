"""Keeping an institutional sign-in alive across a Harness run.

ScienceDirect issues its authenticated session as cookies with no expiry --
``sd_session_id``, ``sd_access``, ``MIAMISESSION`` -- and Chrome discards those
at startup unless the profile is set to continue where it left off.  The Harness
closes the browser in a ``finally`` at the end of every acquisition run, so
without this preference a sign-in cannot outlive one run, and every later run
arrives as an anonymous off-campus visitor.

These pin the preference edit itself: that it touches one value, that it refuses
any profile but the dedicated one, and that it refuses to run while Chrome is
holding the file.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hunnu_harness.browser.pdf_preferences import (
    SESSION_RESTORE_LAST_SESSION,
    SESSION_RESTORE_PREFERENCE,
    ResearchChromePdfPreference,
    ResearchChromePreferenceError,
    ResearchChromeProfileInUse,
)
from hunnu_harness.paths import TEMP_DIR

# A Preferences file carries far more than the Harness knows about; these
# neighbours exist so a test fails if the edit disturbs anything around it.
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


class SessionRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_the_preference_is_set_to_continue_where_it_left_off(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-set-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))
            controller = _controller(profile)
            self.assertIsNone(controller.inspect_session_restore())

            audit = controller.configure_session_restore()

            self.assertEqual(audit.preference_name, SESSION_RESTORE_PREFERENCE)
            self.assertEqual(audit.previous_value, None)
            self.assertEqual(audit.new_value, SESSION_RESTORE_LAST_SESSION)
            self.assertEqual(controller.inspect_session_restore(), SESSION_RESTORE_LAST_SESSION)

    def test_nothing_else_in_the_file_moves(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-neighbours-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))
            controller = _controller(profile)

            controller.configure_session_restore()

            payload = json.loads(
                (profile / "Default" / "Preferences").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["profile"], NEIGHBOURS["profile"])
            self.assertEqual(payload["download"], NEIGHBOURS["download"])
            self.assertEqual(payload["extensions"], NEIGHBOURS["extensions"])
            # The existing key inside the same object survives alongside the new one.
            self.assertTrue(payload["session"]["restore_on_startup_migrated"])
            self.assertEqual(payload["session"]["restore_on_startup"], 1)

    def test_a_profile_with_no_session_object_gains_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-nosession-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), {"profile": {"exit_type": "Normal"}})
            controller = _controller(profile)

            controller.configure_session_restore()

            payload = json.loads(
                (profile / "Default" / "Preferences").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["session"], {"restore_on_startup": 1})
            self.assertEqual(payload["profile"], {"exit_type": "Normal"})

    def test_an_existing_value_is_replaced_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-replace-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), {"session": {"restore_on_startup": 5}})
            controller = _controller(profile)
            self.assertEqual(controller.inspect_session_restore(), 5)

            audit = controller.configure_session_restore()

            self.assertEqual(audit.previous_value, 5)
            payload = json.loads(
                (profile / "Default" / "Preferences").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["session"], {"restore_on_startup": 1})

    def test_repeating_it_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-idem-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))
            controller = _controller(profile)
            controller.configure_session_restore()
            before = (profile / "Default" / "Preferences").read_bytes()

            audit = controller.configure_session_restore()

            self.assertEqual(audit.previous_value, SESSION_RESTORE_LAST_SESSION)
            self.assertEqual((profile / "Default" / "Preferences").read_bytes(), before)

    def test_it_refuses_while_chrome_holds_the_profile(self) -> None:
        """Chrome rewrites Preferences as it exits, so an edit now would be lost."""

        with tempfile.TemporaryDirectory(prefix="sr-running-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))
            before = (profile / "Default" / "Preferences").read_bytes()
            controller = _controller(profile, running=True)

            with self.assertRaises(ResearchChromeProfileInUse):
                controller.configure_session_restore()

            self.assertEqual((profile / "Default" / "Preferences").read_bytes(), before)

    def test_it_refuses_when_the_running_state_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-unknown-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))
            controller = _controller(profile, running=None)
            with self.assertRaises(ResearchChromeProfileInUse):
                controller.configure_session_restore()

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


class PdfPreferenceStillWorksTests(unittest.TestCase):
    """The PDF preference shares the mechanism, so it is re-pinned here."""

    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def test_both_preferences_can_be_set_independently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-both-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), dict(NEIGHBOURS))
            controller = _controller(profile)

            controller.configure_direct_download()
            controller.configure_session_restore()

            payload = json.loads(
                (profile / "Default" / "Preferences").read_text(encoding="utf-8")
            )
            self.assertIs(payload["plugins"]["always_open_pdf_externally"], True)
            self.assertEqual(payload["session"]["restore_on_startup"], 1)
            self.assertEqual(payload["profile"], NEIGHBOURS["profile"])

    def test_a_boolean_is_never_read_as_the_integer_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sr-bool-", dir=TEMP_DIR) as tmp:
            profile = _profile(Path(tmp), {"session": {"restore_on_startup": True}})
            self.assertIsNone(_controller(profile).inspect_session_restore())


if __name__ == "__main__":
    unittest.main()
