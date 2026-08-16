from __future__ import annotations

import time
from collections.abc import Callable

from ..models import AuthStatus, BrowserState


_AUTH_REQUIRED_MARKERS = (
    "统一身份认证",
    "学校登录",
    "学号",
    "用户名",
    "密码",
    "验证码",
    "二维码",
    "MFA",
    "CAS",
    "WebVPN",
)
_AUTH_SUCCESS_MARKERS = (
    "学校账号",
    "退出登录",
    "个人中心",
    "湖南师范大学",
)
_EXPIRED_MARKERS = ("登录已过期", "会话已过期", "session expired", "重新登录")


def classify_auth_state(state: BrowserState) -> AuthStatus:
    """Classify visible state without reading cookies or credentials."""
    haystack = " ".join((state.url, state.title, state.body_text)).lower()
    if any(marker.lower() in haystack for marker in _EXPIRED_MARKERS):
        return AuthStatus.SESSION_EXPIRED
    if any(marker.lower() in haystack for marker in _AUTH_SUCCESS_MARKERS):
        return AuthStatus.AUTH_SUCCESS
    if any(marker.lower() in haystack for marker in _AUTH_REQUIRED_MARKERS):
        return AuthStatus.AUTH_REQUIRED
    return AuthStatus.AUTH_UNKNOWN


class ManualLoginRequired(RuntimeError):
    pass


class AuthenticationController:
    def __init__(self, state_reader: Callable[[], BrowserState]):
        self._state_reader = state_reader
        self.status = AuthStatus.AUTH_UNKNOWN

    def check_auth_status(self) -> AuthStatus:
        self.status = classify_auth_state(self._state_reader())
        return self.status

    def wait_for_manual_login(self, *, timeout_seconds: float = 300, poll_seconds: float = 2) -> AuthStatus:
        """Wait for a human to complete authentication; never types credentials."""
        deadline = time.monotonic() + timeout_seconds
        self.status = AuthStatus.AUTH_IN_PROGRESS
        while time.monotonic() < deadline:
            current = self.check_auth_status()
            if current == AuthStatus.AUTH_SUCCESS:
                return current
            if current == AuthStatus.SESSION_EXPIRED:
                self.status = current
                raise ManualLoginRequired("Manual login is required because the session expired.")
            time.sleep(poll_seconds)
        self.status = AuthStatus.AUTH_REQUIRED
        raise ManualLoginRequired("ManualLoginRequired=true; human authentication did not complete before timeout.")
