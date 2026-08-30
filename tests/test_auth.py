import unittest

from hunnu_harness.auth.state import classify_auth_state
from hunnu_harness.models import AuthStatus, BrowserState


class AuthClassificationTests(unittest.TestCase):
    def test_school_account_is_success(self):
        state = BrowserState(url="https://example.test", title="CNRDS", body_text="学校账号 湖南师范大学")
        self.assertEqual(classify_auth_state(state), AuthStatus.AUTH_SUCCESS)

    def test_password_page_requires_manual_login(self):
        state = BrowserState(url="https://login.example.test", title="统一身份认证", body_text="学号 密码 验证码")
        self.assertEqual(classify_auth_state(state), AuthStatus.AUTH_REQUIRED)

    def test_expired_session_wins(self):
        state = BrowserState(url="https://example.test", title="会话已过期", body_text="学校账号")
        self.assertEqual(classify_auth_state(state), AuthStatus.SESSION_EXPIRED)

    def test_unrecognisable_page_stays_unknown(self):
        """No marker either way means UNKNOWN -- never a guess in either direction."""

        state = BrowserState(
            url="https://example.test/article", title="Some article", body_text="plain body text"
        )
        self.assertEqual(classify_auth_state(state), AuthStatus.AUTH_UNKNOWN)
