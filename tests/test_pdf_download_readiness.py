"""Readiness probes use an isolated synthetic profile and never configure it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunnu_harness.browser.pdf_preferences import assess_pdf_download_readiness


def _profile(tmp_path: Path, monkeypatch, payload: dict | None = None) -> Path:
    profile = tmp_path / "ResearchChrome"
    (profile / "Default").mkdir(parents=True)
    monkeypatch.setenv("HUNNU_RESEARCH_PROFILE", str(profile))
    if payload is not None:
        (profile / "Default" / "Preferences").write_text(json.dumps(payload), encoding="utf-8")
    return profile


@pytest.mark.parametrize(
    ("payload", "state", "ready"),
    (
        ({"plugins": {"always_open_pdf_externally": True}}, "ENABLED", True),
        ({"plugins": {"always_open_pdf_externally": False}}, "DISABLED", False),
        ({"plugins": {}}, "ABSENT_OR_INVALID", False),
        ({"plugins": {"always_open_pdf_externally": "true"}}, "ABSENT_OR_INVALID", False),
        ({}, "ABSENT_OR_INVALID", False),
    ),
)
def test_only_a_real_enabled_boolean_satisfies_the_profile_precondition(tmp_path, monkeypatch, payload, state, ready):
    profile = _profile(tmp_path, monkeypatch, payload)
    before = (profile / "Default" / "Preferences").read_bytes()
    result = assess_pdf_download_readiness(profile)
    assert result["PdfPreferenceState"] == state
    assert result["ResearchChromeDirectPdfDownloadConfigured"] is ready
    assert result["AutomaticPdfDownloadPreconditionReady"] is ready
    assert result["ConfigurationRequired"] is (not ready)
    assert result["CurrentSessionDownloadVerified"] is False
    assert result["NetworkActivity"] is False
    assert result["ProfileModified"] is False
    assert (profile / "Default" / "Preferences").read_bytes() == before


def test_probe_does_not_run_process_checks_or_touch_other_profile_files(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch, {
        "plugins": {"always_open_pdf_externally": True},
        "unrelated": {"synthetic_marker": "DO_NOT_REPORT_THIS_SENTINEL"},
    })
    cookie_file = profile / "Default" / "Cookies"
    cookie_file.write_bytes(b"synthetic untouched bytes")
    before = {p.relative_to(profile): p.read_bytes() for p in profile.rglob("*") if p.is_file()}

    def forbidden_check(_path):
        raise AssertionError("a read-only setting probe must not inspect processes or change profile state")

    monkeypatch.setattr("hunnu_harness.browser.pdf_preferences._default_profile_process_check", forbidden_check)
    result = assess_pdf_download_readiness(profile)
    after = {p.relative_to(profile): p.read_bytes() for p in profile.rglob("*") if p.is_file()}
    assert after == before
    assert "DO_NOT_REPORT_THIS_SENTINEL" not in json.dumps(result)


@pytest.mark.parametrize("contents", (None, "not json", "[]", "null", "42"))
def test_missing_or_malformed_preferences_report_unavailable_without_creating_files(tmp_path, monkeypatch, contents):
    profile = _profile(tmp_path, monkeypatch)
    path = profile / "Default" / "Preferences"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    before = {p.relative_to(profile): p.read_bytes() for p in profile.rglob("*") if p.is_file()}
    result = assess_pdf_download_readiness(profile)
    assert result["PdfPreferenceState"] == "UNAVAILABLE"
    assert result["ConfigurationRequired"] is True
    assert "browser-configure-pdf-download" in result["ConfigurationAction"]
    assert {p.relative_to(profile): p.read_bytes() for p in profile.rglob("*") if p.is_file()} == before


def test_normal_daily_chrome_profile_remains_out_of_scope(tmp_path, monkeypatch):
    local = tmp_path / "LocalAppData"
    profile = local / "Google" / "Chrome" / "User Data"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("HUNNU_RESEARCH_PROFILE", str(profile))
    result = assess_pdf_download_readiness(profile)
    assert result["PdfPreferenceState"] == "UNAVAILABLE"
    assert result["AutomaticPdfDownloadPreconditionReady"] is False
    assert not profile.exists()
