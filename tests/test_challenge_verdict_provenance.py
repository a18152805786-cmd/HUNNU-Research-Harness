"""A human-verification verdict may not outrun the evidence behind it.

Challenge words in a page body are not a seen challenge. The same keyword scan
fires on a footer that mentions CAPTCHAs and on a paper whose own title is about
them, and it fires identically whether or not a browser ever ran. These tests
pin the separation between what was observed and what is merely written.
"""

from __future__ import annotations

import unittest

from hunnu_harness.literature.adapters.base import HumanActionReason, SourceActionRequired
from hunnu_harness.literature.adapters.sciencedirect import ScienceDirectAdapter

CAPTCHA_PAGE = (
    "<html><body><h1>Are you a robot?</h1>"
    "<p>Please confirm you are a human by completing the captcha challenge below.</p>"
    "</body></html>"
)
PAPER_ABOUT_CAPTCHAS = (
    "<html><head><meta name='citation_title' content='CAPTCHA usability'></head>"
    "<body><h1>A study of CAPTCHA usability in online banking</h1></body></html>"
)
FOOTER_MENTION = (
    "<html><body><article>Ordinary article on AI washing</article>"
    "<footer>We use captcha technology to protect this site.</footer></body></html>"
)
SEARCH_URL = "https://www.sciencedirect.com/search"


def _raised(html: str, **kwargs) -> SourceActionRequired | None:
    try:
        ScienceDirectAdapter.detect_interruption(html, url=SEARCH_URL, **kwargs)
    except SourceActionRequired as error:
        return error
    return None


class ChallengeInvariantTests(unittest.TestCase):
    def test_visible_challenge_requires_an_observation(self) -> None:
        error = SourceActionRequired(
            "x",
            reason=HumanActionReason.CHALLENGE_TEXT_UNVERIFIED,
            challenge_observed=False,
            challenge_visible=True,
            challenge_blocking=True,
        )
        # Claiming visibility without observation is not representable.
        self.assertFalse(error.challenge_visible)
        self.assertFalse(error.challenge_blocking)

    def test_payload_cannot_be_unobserved_and_visible_at_once(self) -> None:
        for error in (
            _raised(CAPTCHA_PAGE),
            _raised(PAPER_ABOUT_CAPTCHAS),
            _raised(FOOTER_MENTION),
        ):
            self.assertIsNotNone(error)
            payload = error.as_dict()
            with self.subTest(reason=payload["HumanActionReason"]):
                if not payload["ChallengeActuallyObserved"]:
                    self.assertFalse(payload["ChallengeVisible"])
                    self.assertNotEqual(
                        payload["HumanActionReason"],
                        HumanActionReason.VISIBLE_CHALLENGE.value,
                    )


class StaticContentTests(unittest.TestCase):
    def test_no_browser_observation_never_reports_a_visible_captcha(self) -> None:
        error = _raised(CAPTCHA_PAGE)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason, HumanActionReason.CHALLENGE_TEXT_UNVERIFIED)
        self.assertFalse(error.challenge_visible)
        self.assertFalse(error.browser_ready_for_manual_action)

    def test_a_paper_about_captchas_is_not_a_captcha(self) -> None:
        error = _raised(PAPER_ABOUT_CAPTCHAS)
        self.assertIsNotNone(error)
        self.assertNotEqual(error.reason, HumanActionReason.VISIBLE_CHALLENGE)
        self.assertFalse(error.challenge_visible)

    def test_a_footer_mention_is_not_a_visible_challenge(self) -> None:
        error = _raised(FOOTER_MENTION)
        self.assertIsNotNone(error)
        self.assertNotEqual(error.reason, HumanActionReason.VISIBLE_CHALLENGE)
        self.assertFalse(error.challenge_visible)

    def test_observed_but_not_visible_stays_unverified(self) -> None:
        error = _raised(CAPTCHA_PAGE, observed=True, challenge_visible=False)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason, HumanActionReason.CHALLENGE_TEXT_UNVERIFIED)
        self.assertTrue(error.challenge_observed)
        self.assertFalse(error.challenge_visible)


class ObservedChallengeTests(unittest.TestCase):
    def test_observed_visible_challenge_is_reported_as_visible(self) -> None:
        error = _raised(CAPTCHA_PAGE, observed=True, challenge_visible=True)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason, HumanActionReason.VISIBLE_CHALLENGE)
        self.assertTrue(error.challenge_observed)
        self.assertTrue(error.challenge_visible)
        self.assertTrue(error.challenge_blocking)
        self.assertTrue(error.browser_ready_for_manual_action)

    def test_normal_page_continues_acquisition(self) -> None:
        ordinary = (
            "<html><head><meta name='citation_title' content='AI washing'></head>"
            "<body><article>Regular ScienceDirect article page.</article></body></html>"
        )
        self.assertIsNone(_raised(ordinary, observed=True))


class LoginVersusChallengeTests(unittest.TestCase):
    def test_login_required_is_not_reported_as_a_challenge(self) -> None:
        login = (
            "<html><body><p>Sign in to continue reading this article.</p></body></html>"
        )
        error = _raised(login, observed=True)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason, HumanActionReason.LOGIN_REQUIRED)
        self.assertFalse(error.challenge_visible)
        self.assertFalse(error.challenge_blocking)

    def test_login_and_visible_challenge_are_distinct_reasons(self) -> None:
        self.assertNotEqual(
            HumanActionReason.LOGIN_REQUIRED, HumanActionReason.VISIBLE_CHALLENGE
        )
        self.assertNotEqual(
            HumanActionReason.CHALLENGE_TEXT_UNVERIFIED,
            HumanActionReason.VISIBLE_CHALLENGE,
        )

    def test_unobserved_login_does_not_promise_a_ready_browser(self) -> None:
        login = (
            "<html><body><p>Institutional login required to continue.</p></body></html>"
        )
        error = _raised(login, observed=False)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason, HumanActionReason.LOGIN_REQUIRED)
        # Nothing observed the page, so no browser is waiting for the user.
        self.assertFalse(error.browser_ready_for_manual_action)


class CNKIOwnershipTests(unittest.TestCase):
    """CNKI keeps its own dormant-challenge semantics; this change is elsewhere."""

    def test_cnki_keeps_its_visibility_based_verdict(self) -> None:
        # CNKI already decides on effective visibility rather than on DOM text.
        # This change adds the same discipline elsewhere; it does not touch it.
        from hunnu_harness.literature.cnki_challenge import ChallengeNodeEvidence

        for name in ("render_visible", "viewport_visible", "effective_visible", "actionable"):
            self.assertTrue(hasattr(ChallengeNodeEvidence, name), name)

    def test_generic_route_gate_still_skips_cnki_urls(self) -> None:
        from hunnu_harness.literature.institutional import (
            HUNNUInstitutionalAccessResolver as Resolver,
        )

        self.assertTrue(Resolver.is_cnki_publisher_url("https://kns.cnki.net/kns8/defaultresult/index"))
        self.assertFalse(Resolver.is_cnki_publisher_url("https://www.sciencedirect.com/search"))


if __name__ == "__main__":
    unittest.main()
