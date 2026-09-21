from __future__ import annotations

import asyncio

import json
import re
import time
from enum import Enum
from collections import defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

from ...browser.commands import (
    BrowserTarget,
    DownloadCommand,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
)
from ...browser.playwright_backend import (
    _INTERSTITIAL_BODY_MARKERS,
    _INTERSTITIAL_TITLE_MARKERS,
)
from .base import (
    LiteratureSourceError,
    HumanActionReason,
    LiteratureSourceAdapter,
    SourceActionRequired,
    SourceLayoutChanged,
    SourceUnavailable,
)
from ..models import (
    AccessDecision,
    AccessType,
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
    UNKNOWN,
)
from ..normalization import normalize_doi, stable_paper_id
from ..security import sanitize_text, sanitize_url


_ARTICLE_PATH = re.compile(r"/science/article/(?:abs/)?pii/([A-Za-z0-9]+)")
_SCIENCEDIRECT_HOSTS = frozenset({"www.sciencedirect.com", "sciencedirect.com"})

# ScienceDirect renders its result list after the document is ready, so the
# first readable version of a perfectly good search page contains no results at
# all.  These are the landmarks that say the page has finished deciding.
_NO_RESULTS_MARKERS = (
    "No results found",
    "Your search for",
    "did not match any",
    "\u672a\u627e\u5230\u7ed3\u679c",
)
# Elsevier's refusal page, read from the live page on 2026-09-19 (the run
# artifacts keep no page content, so this was captured out of band).  Both
# halves are required: the sentence alone is something a search page could
# echo back inside a query, and a hexadecimal token alone is not a refusal.
# Together they are a statement the publisher makes about this session, and
# nothing else on ScienceDirect says it.
_PUBLISHER_BLOCK_WORDING = (
    "there was a problem providing the content you requested",
)
_PUBLISHER_BLOCK_REFERENCE = re.compile(
    r"reference\s+number\s*[:\uff1a]\s*(?P<reference>[0-9a-f]{12,40})",
    re.IGNORECASE,
)
# Elsevier's own code for the refusal, kept separate because it is diagnostic
# detail the user hands to the library, not part of the match.
_PUBLISHER_BLOCK_CODE = re.compile(r"\b(CPE\d{5})\b")
# A bounded wait on those landmarks, never a fixed sleep: a page that has
# already rendered is read immediately.
SEARCH_RENDER_TIMEOUT_SECONDS = 20.0
SEARCH_RENDER_POLL_SECONDS = 0.5

# An article page renders the same way, and its PDF control arrives last of
# all.  The strings 'View PDF' and '/pdfft' are already in the early HTML;
# what is missing is an anchor carrying both, which is exactly what the
# access decision looks for -- so readiness waits on that same anchor rather
# than on any proxy for it.
ARTICLE_RENDER_TIMEOUT_SECONDS = 20.0
ARTICLE_RENDER_POLL_SECONDS = 0.5
# The publisher saying, in so many words, that there is no full text here.
_NO_FULLTEXT_MARKERS = (
    "get access through your institution",
    "purchase pdf",
    "get access",
    "check for this article elsewhere",
    "rent this article",
)
# A third-party title hosted under /org/ says it differently.  It has no access
# box at all, and none of the phrases above occurs anywhere on the page (read
# from the live page for 10.1108/jfra-03-2025-0162, an Emerald title, on
# 2026-09-19).  Where "View PDF" would sit there is a link out to the publisher,
# and the page's link to its own full text is there but disabled.  Both are
# required.  The hand-off alone is a way out, not a refusal: a hosted title the
# session is entitled to could carry it while its own PDF control is still
# rendering.  Both are matched on rendered links and never on the page text,
# which here includes inline script: the words alone -- in an abstract, in the
# page's state -- are not the publisher saying anything.
_PUBLISHER_HANDOFF_LABELS = ("view at publisher",)
_OWN_FULLTEXT_LINK_LABELS = ("view full text",)
_PDF_PATH_MARKERS = ("/pdfft", "/pdf", ".pdf")
_NO_PDF_CONTROL_REASON = (
    "No enabled, official PDF view/download control was present on the article page"
)


@dataclass(frozen=True)
class _Anchor:
    href: str
    text: str
    attributes: dict[str, str]
    in_results: bool = False


# The list that holds search results.  Both the current page and the archived
# fixture use ``ol.search-result-wrapper``; the id is carried too because the
# live page also stamps one on the same element.
_RESULTS_LIST_CLASSES = frozenset({"search-result-wrapper"})
_RESULTS_LIST_IDS = frozenset({"srp-results-list"})


def _classes(attributes: dict[str, str]) -> frozenset[str]:
    """Class tokens, never a substring test.

    The results list also carries ``li.LoginMessageResultItem`` -- a sign-in
    notice sitting among the cards.  Matching ``ResultItem`` as a substring
    would count that notice as a paper.
    """

    return frozenset(attributes.get("class", "").split())


class _ScienceDirectHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = defaultdict(list)
        self.anchors: list[_Anchor] = []
        self.links: list[dict[str, str]] = []
        self.text_parts: list[str] = []
        self.html_language = UNKNOWN
        self._anchor_stack: list[tuple[str, dict[str, str], list[str]]] = []
        self._json_ld_depth = 0
        self._json_ld_parts: list[str] = []
        # Where the results actually live.  Anchors are recorded with whether
        # they sat inside this region, so recommendations, navigation and
        # footer links can never be mistaken for search results.
        self._list_stack: list[bool] = []
        self._item_depth = 0
        self.result_cards = 0

    @property
    def _in_results(self) -> bool:
        return any(self._list_stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): (value or "") for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "html" and attributes.get("lang"):
            self.html_language = attributes["lang"]
        elif lowered == "meta":
            name = (attributes.get("name") or attributes.get("property") or attributes.get("http-equiv") or "").casefold()
            content = attributes.get("content", "").strip()
            if name and content:
                self.meta[name].append(content)
        elif lowered == "link":
            self.links.append(attributes)
        elif lowered == "ol":
            self._list_stack.append(
                bool(_classes(attributes) & _RESULTS_LIST_CLASSES)
                or attributes.get("id", "") in _RESULTS_LIST_IDS
            )
        elif lowered == "li":
            if self._in_results and self._item_depth == 0:
                self.result_cards += 1
            self._item_depth += 1
        elif lowered == "a":
            self._anchor_stack.append((attributes.get("href", ""), attributes, []))
        elif lowered == "script" and "ld+json" in attributes.get("type", "").casefold():
            self._json_ld_depth += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "a" and self._anchor_stack:
            href, attributes, parts = self._anchor_stack.pop()
            self.anchors.append(
                _Anchor(
                    href=href,
                    text=" ".join(parts).strip(),
                    attributes=attributes,
                    in_results=self._in_results,
                )
            )
        elif lowered == "ol" and self._list_stack:
            self._list_stack.pop()
        elif lowered == "li" and self._item_depth:
            self._item_depth -= 1
        elif lowered == "script" and self._json_ld_depth:
            self._json_ld_depth -= 1

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if not stripped:
            return
        self.text_parts.append(stripped)
        if self._anchor_stack:
            self._anchor_stack[-1][2].append(stripped)
        if self._json_ld_depth:
            self._json_ld_parts.append(data)

    @property
    def body_text(self) -> str:
        return " ".join(self.text_parts)

    @property
    def json_ld(self) -> list[Any]:
        if not self._json_ld_parts:
            return []
        raw = "".join(self._json_ld_parts).strip()
        try:
            return [json.loads(raw)]
        except json.JSONDecodeError:
            return []


def _meta_first(parser: _ScienceDirectHTMLParser, *names: str) -> str:
    for name in names:
        values = parser.meta.get(name.casefold(), [])
        if values:
            return values[0].strip() or UNKNOWN
    return UNKNOWN


def _meta_all(parser: _ScienceDirectHTMLParser, *names: str) -> tuple[str, ...]:
    values: list[str] = []
    for name in names:
        values.extend(parser.meta.get(name.casefold(), []))
    return tuple(dict.fromkeys(item.strip() for item in values if item.strip()))


def _walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _article_json_ld(parser: _ScienceDirectHTMLParser) -> dict[str, Any]:
    for root in parser.json_ld:
        for item in _walk_json(root):
            item_type = item.get("@type", "")
            types = item_type if isinstance(item_type, list) else [item_type]
            if any(str(value).casefold() in {"scholarlyarticle", "article"} for value in types):
                return item
    return {}


def _article_title(parser: _ScienceDirectHTMLParser) -> str:
    """The title an article page gives for itself, or UNKNOWN.

    Extracted so metadata extraction and the wait that precedes it cannot
    drift apart: the page counts as having said what it is exactly when this
    finds the title the identity lock will be checked against.
    """

    title = _meta_first(parser, "citation_title", "dc.title", "og:title")
    if title == UNKNOWN:
        article_json = _article_json_ld(parser)
        title = str(article_json.get("headline", article_json.get("name", UNKNOWN))).strip() or UNKNOWN
    return title


def _interstitial_gate_present(parser: _ScienceDirectHTMLParser) -> bool:
    """Whether the page reads as the publisher's bot-check interstitial.

    The markers are the browser layer's, borrowed rather than copied.  This
    only ever classifies: nothing here, or anywhere downstream of it, clicks,
    types, reloads, or otherwise answers the gate.
    """

    body = parser.body_text.casefold()
    return any(
        marker in body
        for marker in _INTERSTITIAL_TITLE_MARKERS + _INTERSTITIAL_BODY_MARKERS
    )


def _json_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name", "")).strip()
    return str(value).strip()


class SearchPageType(str, Enum):
    """What a page claiming to be search results actually is."""

    RESULTS_PRESENT = "RESULTS_PRESENT"
    GENUINE_ZERO_RESULTS = "GENUINE_ZERO_RESULTS"
    # Neither the results list nor a no-results notice has rendered yet.  This
    # is the state the adapter used to read the page in, and report as though
    # the search had come back empty.
    PENDING = "PENDING"
    NON_SEARCH_PAGE = "NON_SEARCH_PAGE"
    # The publisher refused this session outright and said so, with a support
    # reference number.  It is not a layout change and not an empty search,
    # and unlike the bot-check interstitial it will not clear by waiting.
    PUBLISHER_BLOCKED = "PUBLISHER_BLOCKED"


@dataclass(frozen=True)
class SearchPageObservation:
    """What the page says about itself, separately from what was parsed.

    Kept apart from the parsed records on purpose.  An empty result list and a
    page whose results could not be read are the same thing to a caller that
    only sees a count, and telling them apart is the difference between "this
    paper is not on ScienceDirect" and "the Harness cannot read ScienceDirect
    any more".  A lower-tier Agent should never have to guess which it was.
    """

    page_type: SearchPageType
    result_cards: int = 0
    article_links: int = 0

    @property
    def decided(self) -> bool:
        return self.page_type is not SearchPageType.PENDING

    def as_dict(self) -> dict[str, Any]:
        return {
            "SearchPageObserved": True,
            "SearchPageType": self.page_type.value,
            "ResultContainersObserved": self.result_cards,
            "ArticleLinksObserved": self.article_links,
        }


class ArticlePageState(str, Enum):
    """What an article page is, before anything is extracted from it.

    ``extract_metadata`` used to read the page the instant navigation
    returned.  A page still held by the publisher's bot-check, or one whose
    own document was still arriving, then came back with no title -- and that
    surfaced as "target identity lock failed" for every result in turn, while
    the loop went on opening further article pages against a publisher that
    was already challenging.
    """

    METADATA_PRESENT = "METADATA_PRESENT"
    # CAPTCHA or login text: ``detect_interruption`` stays the one place that
    # reports it, so this only says the page has decided.
    INTERRUPTED = "INTERRUPTED"
    PAGE_GATE = "PAGE_GATE"
    # The publisher refused this session and said so with a reference number.
    # Unlike PAGE_GATE this does not clear by waiting.
    PUBLISHER_BLOCKED = "PUBLISHER_BLOCKED"
    PENDING = "PENDING"

    @property
    def decided(self) -> bool:
        return self is not ArticlePageState.PENDING


class ArticleReadiness(str, Enum):
    """What an article page has managed to say about its full text yet."""

    FULLTEXT_AUTHORIZED = "FULLTEXT_AUTHORIZED"
    FULLTEXT_NOT_AUTHORIZED = "FULLTEXT_NOT_AUTHORIZED"
    # Still assembling.  The distinction that matters: this is not a refusal,
    # and reporting it as one told callers a paper they are entitled to was out
    # of reach.
    PENDING_RENDER = "PENDING_RENDER"
    INTERRUPTED = "INTERRUPTED"
    READINESS_TIMEOUT = "READINESS_TIMEOUT"


@dataclass(frozen=True)
class ArticleReadinessObservation:
    readiness: ArticleReadiness
    pdf_control_url: str = UNKNOWN
    pdf_control_pii: str = UNKNOWN
    page_pii: str = UNKNOWN

    @property
    def decided(self) -> bool:
        return self.readiness is not ArticleReadiness.PENDING_RENDER

    def as_dict(self) -> dict[str, Any]:
        return {
            "ArticleReadiness": self.readiness.value,
            "DownloadControlResolved": self.pdf_control_url,
            "DownloadControlPII": self.pdf_control_pii,
            "ArticlePII": self.page_pii,
        }


class ScienceDirectAdapter(LiteratureSourceAdapter):
    name = "ScienceDirect"
    supports_unattended_download = True
    supports_preflight = True
    search_origin = "https://www.sciencedirect.com"
    # What the last search page said about itself, for the run report.
    last_search_observation: SearchPageObservation | None = None
    last_article_readiness: ArticleReadinessObservation | None = None
    last_article_readiness_wait_ms: int = 0

    @staticmethod
    def _parser(html: str) -> _ScienceDirectHTMLParser:
        stripped = html.strip()
        if stripped.startswith('"'):
            try:
                decoded = json.loads(stripped)
                if isinstance(decoded, str):
                    html = decoded
            except json.JSONDecodeError:
                pass
        parser = _ScienceDirectHTMLParser()
        parser.feed(html)
        return parser

    @classmethod
    def detect_interruption(
        cls,
        html: str,
        *,
        url: str = "",
        observed: bool = False,
        challenge_visible: bool = False,
    ) -> None:
        """Decide whether a human is needed, and say how that was established.

        ``observed`` means this HTML came from a live browser observation of the
        page rather than from static content. Without it a keyword hit is only
        challenge *text*: the same scan fires on a footer that mentions CAPTCHAs
        and on a paper whose own title is about them, so it may not be reported
        as a challenge the user can see and solve.

        CNKI is deliberately not routed here -- its challenge state has a single
        owner in ``CNKIChallengeDetector``, which already distinguishes a
        preloaded off-viewport component from an active one.
        """

        parser = cls._parser(html)
        haystack = f"{url} {_meta_first(parser, 'title', 'og:title')} {parser.body_text}".casefold()
        captcha_markers = ("captcha", "recaptcha", "verify you are human", "security challenge", "滑块", "验证码")
        if any(marker in haystack for marker in captcha_markers):
            if observed and challenge_visible:
                raise SourceActionRequired(
                    "ACTION_REQUIRED_USER_LOGIN=true; "
                    "Reason=Visible human verification challenge observed on the page; "
                    "BrowserReadyForManualAction=true",
                    reason=HumanActionReason.VISIBLE_CHALLENGE,
                    challenge_observed=True,
                    challenge_visible=True,
                    challenge_blocking=True,
                    browser_ready_for_manual_action=True,
                )
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; "
                "Reason=Challenge text present in page content but not confirmed visible by a browser observation; "
                "BrowserReadyForManualAction=false",
                reason=HumanActionReason.CHALLENGE_TEXT_UNVERIFIED,
                challenge_observed=observed,
                challenge_visible=False,
                challenge_blocking=False,
                browser_ready_for_manual_action=False,
            )
        has_article_metadata = _meta_first(parser, "citation_title", "dc.title") != UNKNOWN
        login_url = any(marker in url.casefold() for marker in ("/login", "/signin", "sso", "shibboleth", "cas."))
        blocking_login = any(
            marker in haystack
            for marker in (
                "sign in to continue",
                "authentication required to continue",
                "session has expired",
                "institutional login required",
                "登录已过期",
                "统一身份认证",
            )
        )
        if (login_url or blocking_login) and not has_article_metadata:
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=School or database login required; "
                "BrowserReadyForManualAction=true",
                reason=HumanActionReason.LOGIN_REQUIRED,
                challenge_observed=observed,
                browser_ready_for_manual_action=observed,
            )

    @classmethod
    def parse_search_results_html(
        cls,
        html: str,
        *,
        query: str,
        source_url: str = "https://www.sciencedirect.com/search",
        max_results: int = 30,
    ) -> list[LiteratureRecord]:
        cls.detect_interruption(html, url=source_url)
        parser = cls._parser(html)
        records: list[LiteratureRecord] = []
        seen: set[str] = set()
        for anchor in parser.anchors:
            # Only anchors inside the results list.  A ScienceDirect page also
            # carries recommendations and related articles that point at real
            # papers; treating those as results would hand the workflow
            # candidates the search never returned.
            if not anchor.in_results:
                continue
            match = _ARTICLE_PATH.search(anchor.href)
            title = re.sub(r"\s+", " ", anchor.text).strip()
            if not match or not title:
                continue
            stable_identifier = match.group(1)
            if stable_identifier in seen:
                continue
            page = urljoin(cls.search_origin, anchor.href)
            # An absolute href on another host resolves to that host, so the
            # article path alone is not enough to say this is a ScienceDirect
            # paper.  ``open_result`` already refuses such a URL; refusing it
            # here keeps it from ever becoming a candidate.
            if urlsplit(page).hostname not in _SCIENCEDIRECT_HOSTS:
                continue
            seen.add(stable_identifier)
            records.append(
                LiteratureRecord(
                    paper_id=stable_paper_id(title=title, year=UNKNOWN),
                    title=title,
                    source_database=cls.name,
                    source_page=sanitize_url(page),
                    stable_identifier=stable_identifier,
                    search_query=query,
                )
            )
            if len(records) >= max_results:
                break
        return records

    @classmethod
    def observe_search_page(cls, html: str, *, source_url: str) -> SearchPageObservation:
        """Classify the page without parsing it, so an empty result is explainable.

        Deliberately does not raise for a login or challenge page: the caller
        polls this while the page is still settling, and ``detect_interruption``
        remains the one place that stops a run.
        """

        parser = cls._parser(html)
        # Read first, and only on a page with no results on it: a refusal is
        # decided on its first read, because waiting out the render budget for
        # a page that will never render is the 20s this run used to spend.
        article_links = sum(
            1
            for anchor in parser.anchors
            if anchor.in_results and _ARTICLE_PATH.search(anchor.href)
        )
        if not article_links and not parser.result_cards:
            if cls.publisher_block_evidence(parser) is not None:
                return SearchPageObservation(page_type=SearchPageType.PUBLISHER_BLOCKED)
        if article_links or parser.result_cards:
            return SearchPageObservation(
                page_type=SearchPageType.RESULTS_PRESENT,
                result_cards=parser.result_cards,
                article_links=article_links,
            )
        try:
            # The one detector, rather than a second list of markers to drift
            # out of step with it.  Here its verdict is turned into a page type;
            # raising stays the caller's job.
            cls.detect_interruption(html, url=source_url)
        except LiteratureSourceError:
            return SearchPageObservation(page_type=SearchPageType.NON_SEARCH_PAGE)
        body = parser.body_text.casefold()
        # The explicit no-results notice is read first.  A search page echoes
        # the query back, so a query that happens to contain a gate word ("one
        # moment", "attention required") must not turn a genuinely empty search
        # into a gate -- which matters now that a gate stops the run.
        if any(marker.casefold() in body for marker in _NO_RESULTS_MARKERS):
            return SearchPageObservation(page_type=SearchPageType.GENUINE_ZERO_RESULTS)
        # A bot-check interstitial is the browser layer's to wait out, but if it
        # is still on screen when the page is read, this is not a search page and
        # polling it for twenty seconds would only delay saying so.
        if _interstitial_gate_present(parser):
            return SearchPageObservation(page_type=SearchPageType.NON_SEARCH_PAGE)
        return SearchPageObservation(page_type=SearchPageType.PENDING)

    @classmethod
    def parse_article_html(
        cls,
        html: str,
        *,
        source_url: str,
        search_query: str = UNKNOWN,
    ) -> LiteratureRecord:
        cls.detect_interruption(html, url=source_url)
        parser = cls._parser(html)
        article_json = _article_json_ld(parser)

        title = _article_title(parser)
        authors = _meta_all(parser, "citation_author", "dc.creator")
        if not authors:
            json_authors = article_json.get("author", [])
            if not isinstance(json_authors, list):
                json_authors = [json_authors]
            authors = tuple(filter(None, (_json_name(author) for author in json_authors)))

        date = _meta_first(parser, "citation_publication_date", "citation_date", "dc.date")
        if date == UNKNOWN:
            date = str(article_json.get("datePublished", UNKNOWN))
        year_match = re.search(r"(?:18|19|20|21)\d{2}", date)
        year = year_match.group(0) if year_match else UNKNOWN

        journal = _meta_first(parser, "citation_journal_title", "prism.publicationname")
        if journal == UNKNOWN:
            part = article_json.get("isPartOf", {})
            journal = _json_name(part) or UNKNOWN
        doi = normalize_doi(_meta_first(parser, "citation_doi", "dc.identifier", "prism.doi"))
        volume = _meta_first(parser, "citation_volume", "prism.volume")
        issue = _meta_first(parser, "citation_issue", "prism.number")
        first_page = _meta_first(parser, "citation_firstpage", "prism.startingpage")
        last_page = _meta_first(parser, "citation_lastpage", "prism.endingpage")
        article_number = _meta_first(parser, "citation_article_number")
        if first_page != UNKNOWN and last_page != UNKNOWN and first_page != last_page:
            pages = f"{first_page}-{last_page}"
        elif first_page != UNKNOWN:
            pages = first_page
        elif article_number != UNKNOWN:
            pages = article_number
        else:
            pages = UNKNOWN

        abstract = _meta_first(parser, "citation_abstract", "description", "dc.description", "og:description")
        if abstract == UNKNOWN:
            abstract = str(article_json.get("description", UNKNOWN)).strip() or UNKNOWN
        raw_keywords = _meta_all(parser, "citation_keywords", "keywords", "dc.subject")
        keywords: list[str] = []
        for value in raw_keywords:
            keywords.extend(item.strip() for item in re.split(r"[;,；，]", value) if item.strip())

        issn = _meta_first(parser, "citation_issn", "prism.issn")
        language = _meta_first(parser, "citation_language", "dc.language")
        if language == UNKNOWN:
            language = parser.html_language if parser.html_language != UNKNOWN else UNKNOWN
        stable_match = _ARTICLE_PATH.search(urlsplit(source_url).path)
        stable_identifier = stable_match.group(1) if stable_match else (doi if doi != UNKNOWN else UNKNOWN)

        json_type = str(article_json.get("@type", "")).casefold()
        publisher_evidence = journal != UNKNOWN and ("scholarlyarticle" in json_type or bool(_meta_all(parser, "citation_journal_title")))
        publication_status = (
            PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value
            if publisher_evidence
            else PublicationStatus.UNKNOWN.value
        )
        publication_type = "JournalArticle" if publisher_evidence else UNKNOWN
        paper_id = stable_paper_id(doi=doi, title=title, year=year, authors=authors)
        return LiteratureRecord(
            paper_id=paper_id,
            title=title,
            authors=authors,
            year=year,
            journal=journal,
            volume=volume,
            issue=issue,
            pages_or_article_number=pages,
            doi=doi,
            issn=issn,
            language=language,
            publication_type=publication_type,
            publication_status=publication_status,
            abstract=abstract,
            keywords=tuple(dict.fromkeys(keywords)),
            source_database=cls.name,
            source_page=sanitize_url(source_url),
            stable_identifier=stable_identifier,
            search_query=search_query,
        )

    @classmethod
    def _pdf_control(
        cls, parser: _ScienceDirectHTMLParser, *, source_url: str
    ) -> tuple[_Anchor | None, str]:
        """The enabled, official PDF control, or nothing.

        Extracted so the access decision and the readiness wait cannot drift
        apart: waiting on some proxy for "the page looks ready" and then
        deciding on something else is how a page gets read one moment too
        early.  Readiness now ends exactly when this returns a control.
        """

        article_host = urlsplit(source_url).hostname or ""
        for anchor in parser.anchors:
            absolute = urljoin(source_url, anchor.href)
            parsed = urlsplit(absolute)
            text = f"{anchor.text} {anchor.attributes.get('aria-label', '')} {anchor.attributes.get('title', '')}".casefold()
            has_pdf_path = any(marker in parsed.path.casefold() for marker in _PDF_PATH_MARKERS)
            has_pdf_label = "pdf" in text and any(action in text for action in ("view", "download", "查看", "下载"))
            same_source = parsed.hostname in {article_host, "www.sciencedirect.com", "sciencedirect.com"}
            if same_source and has_pdf_path and has_pdf_label:
                return anchor, absolute
        return None, UNKNOWN

    @staticmethod
    def _metadata_only_decision(reason: str) -> AccessDecision:
        """The one fail-closed refusal, so its two callers cannot drift apart."""

        return AccessDecision(
            full_text_accessible=False,
            access_type=AccessType.METADATA_ONLY,
            authorized_access=False,
            status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
            reason=reason,
        )

    @classmethod
    def check_fulltext_access_html(cls, html: str, *, source_url: str) -> AccessDecision:
        cls.detect_interruption(html, url=source_url)
        parser = cls._parser(html)
        candidate, candidate_url = cls._pdf_control(parser, source_url=source_url)

        if candidate is None:
            return cls._metadata_only_decision(_NO_PDF_CONTROL_REASON)

        haystack = parser.body_text.casefold()
        open_markers = (
            "open access",
            "creative commons",
            "open archive",
            "funded by",
        )
        access_type = (
            AccessType.OPEN_ACCESS
            if any(marker in haystack for marker in open_markers)
            else AccessType.INSTITUTIONAL_AUTHENTICATED
        )
        return AccessDecision(
            full_text_accessible=True,
            access_type=access_type,
            authorized_access=True,
            status=RunStatus.SUCCESS,
            reason="Official article page exposed an enabled View/Download PDF control",
            download_url=candidate_url,
            download_locator="a[href*='/pdfft'], a[href*='/pdf'], a[href$='.pdf']",
        )

    async def _content(self) -> tuple[str, str]:
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        observation = await self.browser.execute(ObserveCommand(include_html=True))
        return observation.require_html(), observation.url

    async def search(
        self,
        query: str,
        request: LiteratureSearchRequest,
    ) -> list[LiteratureRecord]:
        url = f"{self.search_origin}/search?qs={quote_plus(query)}"
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        await self.browser.execute(NavigateCommand(url))
        html, current_url, observation = await self._settled_search_page(source_url=url)
        self.last_search_observation = observation
        if observation.page_type is SearchPageType.PUBLISHER_BLOCKED:
            # Before detect_interruption: a refusal is not a challenge to pass,
            # and it must not be reported as one.
            raise self._publisher_block_stop(
                "search", self.publisher_block_evidence(self._parser(html)) or "PublisherReference=unknown"
            )
        if observation.page_type is SearchPageType.NON_SEARCH_PAGE:
            # Challenge first: a page that is not a search page never reaches the
            # result parser.  CAPTCHA and login text stay ``detect_interruption``'s
            # to report, unchanged; what is left is the bot-check interstitial,
            # which has no CAPTCHA words and used to fall through the parser as
            # an empty -- and apparently successful -- search.
            self.detect_interruption(html, url=current_url)
            raise self._interstitial_stop("search")
        results = self.parse_search_results_html(
            html,
            query=query,
            source_url=current_url,
            max_results=min(request.max_results_per_source, request.max_search_results),
        )
        if not results and "sciencedirect" not in current_url.casefold():
            raise SourceUnavailable("ScienceDirect search navigation did not reach the expected source")
        if not results and observation.page_type is SearchPageType.RESULTS_PRESENT:
            # The page has results and the parser produced none.  Reporting this
            # as an empty search would tell the caller the paper is not on
            # ScienceDirect, which is the opposite of what the page says.
            raise SourceLayoutChanged(
                "ScienceDirect search results could not be read: "
                f"ResultContainersObserved={observation.result_cards}; "
                f"ArticleLinksObserved={observation.article_links}; "
                "ParsedResultCount=0; ParserFailureDetected=true"
            )
        if not results and observation.page_type is SearchPageType.PENDING:
            # Out of time with neither a results list nor a no-results notice.
            # Only GENUINE_ZERO_RESULTS may be reported as an empty search;
            # anything else would be inventing an answer the publisher never gave.
            raise SourceLayoutChanged(
                "SEARCH_READINESS_TIMEOUT: the ScienceDirect search page presented "
                "neither a results list nor a no-results notice within "
                f"{SEARCH_RENDER_TIMEOUT_SECONDS:g}s; ParsedResultCount=0"
            )
        return results

    @staticmethod
    def _publisher_block_stop(stage: str, evidence: str) -> SourceActionRequired:
        """The stop for an explicit refusal by the publisher.

        This is a human's decision, not a wait: the page carries a support
        reference number and clears on the publisher's terms, not on ours.
        Reporting it as SOURCE_LAYOUT_CHANGED sent the next query into an
        active block and told the user their adapter had broken; on
        2026-09-19 it also produced the conclusion that the campus IP had been
        banned, which was wrong.  The reason quotes the publisher's own
        reference so it can be given to the library as-is.
        """

        return SourceActionRequired(
            "ACTION_REQUIRED_USER_DECISION=true; "
            f"Reason=ScienceDirect refused this session on the {stage} page and returned a "
            f"support reference rather than content; {evidence}; "
            "the run stopped without retrying and nothing was interacted with",
            reason=HumanActionReason.UNSPECIFIED,
        )

    @staticmethod
    def _interstitial_stop(stage: str) -> SourceActionRequired:
        """The stop for a bot-check interstitial that did not clear by itself.

        The browser layer has already spent its bounded wait on it.  What is
        left is the publisher's access control, and that is a person's to pass:
        the run stops here rather than sending the next query into an active
        challenge.  Gate text was read from a live page but its visibility was
        never probed, so this claims no more than CHALLENGE_TEXT_UNVERIFIED.
        """

        return SourceActionRequired(
            "ACTION_REQUIRED_USER_LOGIN=true; "
            f"Reason=Publisher bot-check interstitial still on the ScienceDirect {stage} page "
            "after the bounded wait; it was not interacted with; "
            "BrowserReadyForManualAction=false",
            reason=HumanActionReason.CHALLENGE_TEXT_UNVERIFIED,
            challenge_observed=True,
            challenge_visible=False,
            challenge_blocking=False,
            browser_ready_for_manual_action=False,
        )

    @staticmethod
    def _no_fulltext_notice(
        parser: _ScienceDirectHTMLParser, *, source_url: str, page_pii: str
    ) -> bool:
        """Whether the page says, in so many words, that the full text is not here."""

        body = parser.body_text.casefold()
        if any(marker in body for marker in _NO_FULLTEXT_MARKERS):
            return True
        # The hosted third-party page: a link out to the publisher, and this
        # article's own full-text link present but disabled.
        handed_off = own_fulltext_disabled = False
        for anchor in parser.anchors:
            label = " ".join(anchor.text.split()).casefold()
            if any(item in label for item in _PUBLISHER_HANDOFF_LABELS):
                handed_off = True
            elif (
                any(item in label for item in _OWN_FULLTEXT_LINK_LABELS)
                and anchor.attributes.get("aria-disabled", "").casefold() == "true"
            ):
                target = _ARTICLE_PATH.search(urlsplit(urljoin(source_url, anchor.href)).path)
                if (
                    target is not None
                    and page_pii != UNKNOWN
                    and target.group(1).casefold() == page_pii.casefold()
                ):
                    own_fulltext_disabled = True
        return handed_off and own_fulltext_disabled

    @classmethod
    def observe_article_readiness(
        cls, html: str, *, source_url: str
    ) -> ArticleReadinessObservation:
        """Whether the article page has said anything decisive about full text.

        The authorized landmark is the access decision's own control, found
        through the same predicate, and it must belong to the article being
        read: a PDF control for some other paper is not this paper's full text,
        so it never authorises anything.  Nor does it decide anything: with it
        on the page or without it, only the publisher's own notice refuses, and
        a page offering neither this article's control nor a notice is undecided.
        """

        page_match = _ARTICLE_PATH.search(urlsplit(source_url).path)
        page_pii = page_match.group(1) if page_match else UNKNOWN
        try:
            cls.detect_interruption(html, url=source_url)
        except LiteratureSourceError:
            return ArticleReadinessObservation(
                readiness=ArticleReadiness.INTERRUPTED, page_pii=page_pii
            )

        parser = cls._parser(html)
        control, control_url = cls._pdf_control(parser, source_url=source_url)
        control_pii = UNKNOWN
        if control is not None:
            control_match = _ARTICLE_PATH.search(urlsplit(control_url).path)
            control_pii = control_match.group(1) if control_match else UNKNOWN
            if page_pii == UNKNOWN or control_pii.casefold() == page_pii.casefold():
                return ArticleReadinessObservation(
                    readiness=ArticleReadiness.FULLTEXT_AUTHORIZED,
                    pdf_control_url=control_url,
                    pdf_control_pii=control_pii,
                    page_pii=page_pii,
                )
            # Someone else's PDF -- on the live page, a recommended article's.
            # Not a refusal and certainly not an authorisation.  It used to end
            # the reading here as well, so once the recommendations had rendered
            # the page could no longer be heard refusing, and sat out the whole
            # window undecided.  It is kept as evidence and the reading goes on.

        # Without this article's own control, only the publisher's own words
        # decide anything; with neither, keep waiting.
        return ArticleReadinessObservation(
            readiness=(
                ArticleReadiness.FULLTEXT_NOT_AUTHORIZED
                if cls._no_fulltext_notice(parser, source_url=source_url, page_pii=page_pii)
                else ArticleReadiness.PENDING_RENDER
            ),
            pdf_control_url=control_url,
            pdf_control_pii=control_pii,
            page_pii=page_pii,
        )

    async def _observe_until_decided(self, classify, *, timeout: float, poll: float):
        """Read the page, then keep reading until it says something decisive.

        Shared by the search page and the article page because they fail the
        same way: ``page.goto`` returns while ScienceDirect is still building
        the part that answers the question, and a page read there looks exactly
        like a page with nothing to give.  A page that has already rendered
        costs one observation, so this never becomes a fixed delay.

        A read that fails because the page is between documents -- the
        publisher's bot-check reloads itself, and Playwright refuses
        ``page.content()`` while a navigation is in flight -- is the same
        "not decided yet", not a reason to abandon the query.  It used to
        escape as a bare ``ObservationUnavailable``, which the workflow could
        only log by class name.  Re-reading is all this does: it never
        navigates, reloads, or touches the page, and it ends at the same
        deadline either way.
        """

        started = time.monotonic()
        deadline = started + timeout
        html = current_url = verdict = None
        unreadable: ObservationUnavailable | None = None
        while True:
            try:
                html, current_url = await self._content()
            except ObservationUnavailable as exc:
                unreadable = exc
            else:
                verdict = classify(html, current_url)
                if verdict.decided:
                    break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(poll)
        if verdict is None:
            # Not one readable observation in the whole window.
            cause = unreadable.__cause__ if unreadable is not None else None
            detail = " ".join(str(unreadable).split())
            if cause is not None:
                detail = f"{detail} ({type(cause).__name__}: {' '.join(str(cause).split())})"
            raise SourceUnavailable(
                "PAGE_OBSERVATION_TIMEOUT: the ScienceDirect page could not be read "
                f"at any point within {timeout:g}s; "
                f"LastObservationError={sanitize_text(detail)[:300]}"
            ) from unreadable
        waited_ms = int((time.monotonic() - started) * 1000)
        return html, current_url, verdict, waited_ms

    async def _settled_search_page(self, *, source_url: str) -> tuple[str, str, SearchPageObservation]:
        """Read the search page once it has decided what it is."""

        html, current_url, observation, _waited = await self._observe_until_decided(
            lambda body, url: self.observe_search_page(body, source_url=url),
            timeout=SEARCH_RENDER_TIMEOUT_SECONDS,
            poll=SEARCH_RENDER_POLL_SECONDS,
        )
        return html, current_url, observation

    async def open_result(self, record: LiteratureRecord) -> None:
        parsed = urlsplit(record.source_page)
        if parsed.hostname not in _SCIENCEDIRECT_HOSTS or not _ARTICLE_PATH.search(parsed.path):
            raise SourceLayoutChanged("Result URL is not a stable ScienceDirect article page")
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        await self.browser.execute(NavigateCommand(record.source_page))

    async def current_target_matches(self, record: LiteratureRecord) -> bool:
        """Observe -- never navigate -- to see whether we are already on the target.

        Identity is the PII, not the URL string: ``/pii/S…`` and ``/abs/pii/S…``
        are the same article, and query strings and fragments are noise.  A
        host that is not ScienceDirect, an unparseable path, or any observation
        failure all answer ``False``, so an uncertain probe costs a redundant
        navigation rather than a wrong reuse of a page we cannot identify.
        """

        expected = _ARTICLE_PATH.search(urlsplit(record.source_page).path)
        if expected is None or self.browser is None:
            return False
        try:
            observation = await self.browser.execute(ObserveCommand())
            current = urlsplit(observation.url)
        except Exception:
            return False
        if current.hostname not in _SCIENCEDIRECT_HOSTS:
            return False
        actual = _ARTICLE_PATH.search(current.path)
        if actual is None:
            return False
        return actual.group(1).casefold() == expected.group(1).casefold()

    @classmethod
    def publisher_block_evidence(cls, parser: "_ScienceDirectHTMLParser") -> str | None:
        """The publisher's refusal, quoted back, or ``None``.

        Conservative on purpose: the wording and a reference number must both
        be present.  ee971e0 had to reorder these checks once already, because
        a query containing "one moment" would otherwise have turned a real
        search into a gate -- a search page echoes the query back, and an
        abstract can mention anything, but neither produces a support
        reference number next to that sentence.
        """

        body = parser.body_text
        folded = body.casefold()
        if not any(marker in folded for marker in _PUBLISHER_BLOCK_WORDING):
            return None
        reference = _PUBLISHER_BLOCK_REFERENCE.search(body)
        if reference is None:
            return None
        code = _PUBLISHER_BLOCK_CODE.search(body)
        detail = f"PublisherReference={reference.group('reference')}"
        if code is not None:
            detail = f"{detail}; PublisherCode={code.group(1)}"
        return detail

    @classmethod
    def observe_article_page(cls, html: str, *, source_url: str) -> ArticlePageState:
        """Classify an article page without extracting from it.

        Like ``observe_search_page`` this never raises, because it is polled
        while the page settles.  A title outranks gate words: an article that
        has said what it is cannot be a gate, whatever its abstract mentions.
        """

        try:
            cls.detect_interruption(html, url=source_url)
        except LiteratureSourceError:
            return ArticlePageState.INTERRUPTED
        parser = cls._parser(html)
        if _article_title(parser) != UNKNOWN:
            return ArticlePageState.METADATA_PRESENT
        # A refused article page presents no title, so it used to wait out the
        # render budget and then fail the identity lock with a reason that
        # blamed the document.
        if cls.publisher_block_evidence(parser) is not None:
            return ArticlePageState.PUBLISHER_BLOCKED
        if _interstitial_gate_present(parser):
            return ArticlePageState.PAGE_GATE
        return ArticlePageState.PENDING

    async def extract_metadata(self, *, search_query: str) -> LiteratureRecord:
        """Extract metadata, but only once the page has said what it is.

        The extraction itself is untouched: the same HTML yields the same
        record, and the identity lock downstream is exactly as strict.  What
        changed is that a gate stops the run as a gate instead of as a failed
        identity lock, and a document still arriving is given the same bounded
        window the access check already had.  A page that never presents a
        title is parsed as before and fails the lock closed, as before.
        """

        html, current_url, state, _waited = await self._observe_until_decided(
            lambda body, url: self.observe_article_page(body, source_url=url),
            timeout=ARTICLE_RENDER_TIMEOUT_SECONDS,
            poll=ARTICLE_RENDER_POLL_SECONDS,
        )
        if state is ArticlePageState.PUBLISHER_BLOCKED:
            raise self._publisher_block_stop(
                "article", self.publisher_block_evidence(self._parser(html)) or "PublisherReference=unknown"
            )
        if state is ArticlePageState.PAGE_GATE:
            raise self._interstitial_stop("article")
        return self.parse_article_html(html, source_url=current_url, search_query=search_query)

    async def extract_abstract(self) -> str:
        html, current_url = await self._content()
        return self.parse_article_html(html, source_url=current_url).abstract

    async def check_fulltext_access(self) -> AccessDecision:
        """Decide access, but only once the page has finished saying what it is.

        The decision itself is untouched: the same HTML yields the same verdict
        it always did.  What changed is that the page is no longer read while
        ScienceDirect is still rendering the control the decision looks for --
        a moment at which an entitled, open-access article was indistinguishable
        from one behind a paywall, and was reported as the latter.

        A refusal is returned here rather than asked of that decision.  The
        HTML decision takes the first PDF-like control it meets, which is right
        only while readiness vouches that the control is this article's; a page
        can refuse with another article's PDF control still on it.
        """

        html, current_url, observation, waited_ms = await self._observe_until_decided(
            lambda body, url: self.observe_article_readiness(body, source_url=url),
            timeout=ARTICLE_RENDER_TIMEOUT_SECONDS,
            poll=ARTICLE_RENDER_POLL_SECONDS,
        )
        self.last_article_readiness = observation
        self.last_article_readiness_wait_ms = waited_ms
        if observation.readiness is ArticleReadiness.PENDING_RENDER:
            # Out of time with nothing decisive on the page.  Guessing either
            # way would be inventing an answer the publisher never gave.
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.UNKNOWN,
                authorized_access=False,
                status=RunStatus.SOURCE_LAYOUT_CHANGED,
                reason=(
                    "ARTICLE_READINESS_TIMEOUT: the article page never presented "
                    "either an enabled PDF control for this article or an explicit "
                    f"no-full-text notice within {ARTICLE_RENDER_TIMEOUT_SECONDS:g}s"
                ),
            )
        if observation.readiness is ArticleReadiness.FULLTEXT_NOT_AUTHORIZED:
            # The page refused, and the refusal is returned as one.  Handed to
            # the HTML decision, the first PDF-like control on the page would
            # answer instead -- on a hosted page a recommended article's -- and
            # the refusal would come back as an authorisation of the wrong paper.
            if observation.pdf_control_url == UNKNOWN:
                return self._metadata_only_decision(_NO_PDF_CONTROL_REASON)
            return self._metadata_only_decision(
                "The article page gave an explicit no-full-text notice and no PDF "
                "control for this article; the PDF controls on it belong to other "
                f"articles (first seen: PII {observation.pdf_control_pii})"
            )
        return self.check_fulltext_access_html(html, source_url=current_url)

    async def download_fulltext(self, record: LiteratureRecord, access: AccessDecision) -> Path:
        if not access.full_text_accessible or not access.authorized_access:
            raise PermissionError("FULLTEXT_NOT_AUTHORIZED")
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        article_match = _ARTICLE_PATH.search(urlsplit(record.source_page).path)
        download_url = urlsplit(access.download_url)
        download_match = _ARTICLE_PATH.search(download_url.path)
        if (
            article_match is None
            or download_match is None
            or article_match.group(1).casefold() != download_match.group(1).casefold()
            or not any(marker in download_url.path.casefold() for marker in _PDF_PATH_MARKERS)
        ):
            raise SourceLayoutChanged("Authorized PDF control is not bound to the locked ScienceDirect article")
        download_path = download_url.path
        download_target = f'a[href="{download_path}"], a[href^="{download_path}?"]'
        # Authorization and PII binding have passed; budget the fetch before
        # the click is issued.  The PII is the ScienceDirect-side stable
        # identity of these bytes, so it is the ledger key.
        ticket = self.authorize_publisher_fetch(
            record, identifier=f"pii:{article_match.group(1).casefold()}"
        )
        try:
            artifact = await self.browser.execute(
                DownloadCommand(
                    target=BrowserTarget(
                        css=download_target,
                    ),
                    suggested_filename=f"{record.paper_id}.pdf",
                )
            )
        except Exception as exc:
            # The message, not just the class.  "DownloadFailure" alone sent two
            # rounds of investigation looking in the wrong place while the real
            # reason -- which step of the download did not happen -- was already
            # known to the layer that raised it.
            detail = str(exc).strip() or type(exc).__name__
            ticket.record_outcome(ok=False, detail=detail)
            raise SourceUnavailable(
                f"Authorized PDF control did not produce a browser download: {detail}"
            ) from exc
        ticket.record_outcome(ok=True, detail="browser download completed")
        return artifact.local_path

    async def get_citation(self) -> dict[str, Any]:
        record = await self.extract_metadata(search_query=UNKNOWN)
        return {
            "Title": record.title,
            "Authors": list(record.authors),
            "Journal": record.journal,
            "Year": record.year,
            "DOI": record.doi,
        }
