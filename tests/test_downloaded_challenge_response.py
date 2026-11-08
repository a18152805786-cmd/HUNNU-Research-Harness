"""Regressions from the retained Springer HTML response; no publisher traffic."""

import asyncio
from pathlib import Path

import pytest

from hunnu_harness.browser.commands import (
    DownloadCommand, DownloadArtifact,
    PageHandle, SessionHandle,
)
from hunnu_harness.literature.adapters.base import SourceActionRequired
from hunnu_harness.literature.adapters.springerlink import SpringerLinkAdapter
from hunnu_harness.literature.downloads import LiteratureDownloadManager
from hunnu_harness.literature.fulltext import AuthorizedFullTextValidator
from hunnu_harness.literature.models import FullTextFormat


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
ARTICLE_URL = "https://link.springer.com/article/10.1007/s11573-023-01162-8"
CHALLENGE = b"<!DOCTYPE html><html><head><title>Client Challenge</title></head><body>Required site content could not load.</body></html>"


@pytest.mark.parametrize("title", ["Client Challenge", "Validate User", "Just a moment..."])
def test_downloaded_challenge_document_is_rejected_without_a_visibility_claim(tmp_path, title):
    source = tmp_path / "response.pdf"
    source.write_text(f"<!DOCTYPE html><title>{title}</title><body>challenge</body>", encoding="utf-8")
    result = AuthorizedFullTextValidator.validate(source, FullTextFormat.PDF)
    assert not result.passed
    assert result.error == "DownloadedHTMLInsteadOfFullText"
    assert result.manual_action_required


def test_words_in_an_ordinary_html_article_do_not_become_a_challenge_verdict(tmp_path):
    source = tmp_path / "article.pdf"
    source.write_text("<html><title>Research about client challenges</title><body>Client Challenge CAPTCHA <noscript>JavaScript is disabled</noscript></body></html>", encoding="utf-8")
    result = AuthorizedFullTextValidator.validate(source, FullTextFormat.PDF)
    assert not result.passed
    assert not result.manual_action_required


def test_inline_svg_titles_cannot_hide_or_create_a_document_challenge(tmp_path):
    source = tmp_path / "response.pdf"
    source.write_text("<html><head><title>Client Challenge</title></head><body><svg><title>logo</title></svg></body></html>", encoding="utf-8")
    assert AuthorizedFullTextValidator.validate(source).manual_action_required
    source.write_text("<html><body><svg><title>Client Challenge</title></svg><title>Research article</title></body></html>", encoding="utf-8")
    assert not AuthorizedFullTextValidator.validate(source).manual_action_required


class _ResponsePort:
    session = SessionHandle("response-test")
    page_handle = PageHandle("main")

    def __init__(self, source):
        self.source = source
        self.downloads_dir = source.parent
        self.calls = []

    async def execute(self, command):
        assert isinstance(command, DownloadCommand)
        self.calls.append(command)
        return DownloadArtifact.from_path(self.source)


def test_springer_records_one_failed_fetch_and_hands_off_the_retained_response(tmp_path):
    source = tmp_path / "response.pdf"
    source.write_bytes(CHALLENGE)
    port = _ResponsePort(source)
    adapter = SpringerLinkAdapter(port)
    html = (FIXTURES / "springerlink_article_open_access.html").read_text(encoding="utf-8")
    record = adapter.parse_article_html(html, source_url=ARTICLE_URL)
    record.target_identity_confirmed = True
    access = adapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
    with pytest.raises(SourceActionRequired) as error:
        asyncio.run(adapter.download_fulltext(record, access))
    assert len(port.calls) == 1
    assert source.read_bytes() == CHALLENGE
    evidence = error.value.as_dict()
    assert not evidence["ChallengeActuallyObserved"]
    assert not evidence["ChallengeVisible"]
    assert not evidence["BrowserReadyForManualAction"]
    from hunnu_harness.literature.fetch_ledger import FulltextFetchLedger
    usage = FulltextFetchLedger().usage_today()
    assert usage["AttemptsToday"] == 1
    assert "ok=False" in usage["Identifiers"][0]["last_outcome"]


def test_challenge_response_cannot_be_archived_as_an_authorized_pdf(tmp_path):
    source = tmp_path / "response.pdf"
    source.write_bytes(CHALLENGE)
    html = (FIXTURES / "springerlink_article_open_access.html").read_text(encoding="utf-8")
    record = SpringerLinkAdapter.parse_article_html(html, source_url=ARTICLE_URL)
    access = SpringerLinkAdapter.check_fulltext_access_html(html, source_url=ARTICLE_URL)
    manager = LiteratureDownloadManager(tmp_path / "downloads", allow_outside_project_for_tests=True)
    with pytest.raises(SourceActionRequired):
        manager.archive_authorized_pdf(source, record, access)
    assert not list(manager.archive_dir.iterdir())
    assert source.read_bytes() == CHALLENGE
