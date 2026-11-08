from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlencode, urljoin, urlsplit, urlunsplit

from ...browser.commands import (
    BrowserTarget,
    DownloadCaptureSpec,
    DownloadCommand,
    NavigateCommand,
    ObserveCommand,
)
from ...browser.authorized_file_capture import (
    AcquisitionMethod,
    AuthorizedFileCaptureResult,
)
from ...browser.manual_download_handoff import (
    ManualDownloadHandoff,
    ManualDownloadHandoffError,
    ManualDownloadHandoffResult,
    ManualDownloadHandoffState,
)
from .base import (
    LiteratureSourceAdapter,
    SourceActionRequired,
    SourceLayoutChanged,
    SourceUnavailable,
    SourceUserDownloadRequired,
    authorized_capture_result_from_artifact,
)
from .sciencedirect import _Anchor, _ScienceDirectHTMLParser, _meta_all, _meta_first
from ..models import (
    AccessDecision,
    AccessType,
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
    UNKNOWN,
)
from ..fulltext import AuthorizedFullTextValidator, DOIContentVerification, verify_external_paper_identity
from ..normalization import normalize_doi, normalize_person, normalize_title, stable_paper_id
from ..security import sanitize_url
from ...paths import QUARANTINE_DIR, _logical_path, _windows_io_path

if TYPE_CHECKING:
    from ..institutional import InstitutionalRouteResult


_ARTICLE_PATH = re.compile(
    r"/(?P<journal>[a-z0-9_-]+)/article/(?P<volume>[^/?#]+)/(?P<issue>[^/?#]+)/"
    r"(?P<page>[^/?#]+)/(?P<identifier>\d+)",
    re.IGNORECASE,
)
_PDF_PATH = re.compile(r"/[a-z0-9_-]+/article-pdf/[^?#]+\.pdf$", re.IGNORECASE)
_REJECT_PDF_LABELS = (
    "supplement",
    "supplementary",
    "supporting information",
    "related article",
    "correction",
    "erratum",
    "issue pdf",
    "download issue",
    "data supplement",
)

# Silverchair search cards are distinct from the article links in navigation,
# recommendations and promotional panels. Never fall back to all page links:
# with a one-result budget a recommendation used to consume the only slot.
_SEARCH_CARD_CLASSES = frozenset({"sr-list", "sr-list-item", "sr-list__item"})
_SEARCH_TITLE_CLASSES = frozenset({"article-link", "item-title", "sri-title"})
_VOID_HTML_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})
SEARCH_RENDER_MAX_OBSERVATIONS = 21
SEARCH_RENDER_POLL_SECONDS = 0.5
_PUBLIC_DOI_METADATA_ORIGIN = "https://api.crossref.org"
_PUBLIC_DOI_METADATA_MAX_CHARS = 2_000_000
_NO_RESULTS_MARKERS = (
    "no results found", "no results were found", "your search did not return any results",
)


class _OxfordSearchHTMLParser(HTMLParser):
    """Read title links only inside publisher search-result cards."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[_Anchor] = []
        self.result_cards = 0
        self.text_parts: list[str] = []
        self._elements: list[tuple[str, frozenset[str]]] = []
        self._anchors: list[tuple[str, dict[str, str], list[str], bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = {key.casefold(): value or "" for key, value in attrs}
        classes = frozenset(values.get("class", "").split())
        if classes & _SEARCH_CARD_CLASSES:
            self.result_cards += 1
        if tag not in _VOID_HTML_TAGS:
            self._elements.append((tag, classes))
        if tag == "a":
            in_card = any(tokens & _SEARCH_CARD_CLASSES for _, tokens in self._elements)
            is_title = bool(classes & _SEARCH_TITLE_CLASSES) or any(
                name in {"h2", "h3", "h4", "h5"} or tokens & _SEARCH_TITLE_CLASSES
                for name, tokens in self._elements
            )
            self._anchors.append((values.get("href", ""), values, [], in_card and is_title))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._anchors:
            href, attrs, parts, is_result = self._anchors.pop()
            if is_result:
                self.anchors.append(_Anchor(href, " ".join(parts).strip(), attrs, True))
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index][0] == tag:
                del self._elements[index:]
                break

    def handle_data(self, data: str) -> None:
        if not any(tag in {"script", "style", "template", "noscript"} for tag, _ in self._elements):
            self.text_parts.append(data)
        if self._anchors and data.strip():
            self._anchors[-1][2].append(data.strip())

    @property
    def body_text(self) -> str:
        return " ".join(self.text_parts)


def _host(value: str) -> str:
    try:
        return (urlsplit(value).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _article_match(value: str) -> re.Match[str] | None:
    try:
        return _ARTICLE_PATH.search(urlsplit(value).path)
    except ValueError:
        return None


def _stable_identifier(value: str) -> str:
    match = _article_match(value)
    return match.group("identifier") if match else UNKNOWN


def _canonical_article_url(value: str) -> str:
    match = _article_match(value)
    if not match:
        return UNKNOWN
    return "https://academic.oup.com" + match.group(0)


@dataclass(frozen=True)
class OxfordPDFIdentityResult:
    confirmed: bool
    title_matched: bool
    doi_matched: bool
    author_matched: bool


class OxfordAcademicAdapter(LiteratureSourceAdapter):
    """Oxford Academic adapter for visible, normally authorized article access.

    Signed or gateway URLs remain in memory only. Stable article provenance is
    reconstructed on ``academic.oup.com`` before any record is serialized.
    """

    name = "OxfordAcademic"
    supports_unattended_download = True
    supports_preflight = True
    search_origin = "https://academic.oup.com"
    official_host = "academic.oup.com"
    hunnu_gateway_host = "yclib.hunnu.edu.cn"

    def __init__(
        self,
        browser: Any,
        *,
        institutional_route: "InstitutionalRouteResult | None" = None,
        allow_capture_outside_output_for_tests: bool = False,
        capture_timeout_ms: int = 45_000,
        research_chrome_direct_pdf_download_configured: bool = False,
        manual_download_watch_directory: Path | None = None,
    ) -> None:
        super().__init__(browser)
        configured_watch = manual_download_watch_directory or os.environ.get(
            "HUNNU_HARNESS_MANUAL_DOWNLOAD_WATCH_DIRECTORY"
        )
        self._manual_download_watch_directory = Path(configured_watch) if configured_watch else None
        self._institutional_route = institutional_route
        self._expected_record: LiteratureRecord | None = None
        self._detail_record: LiteratureRecord | None = None
        self._resolved_doi_record: LiteratureRecord | None = None
        self._last_capture: AuthorizedFileCaptureResult | None = None
        self._manual_download_handoff: ManualDownloadHandoff | None = None
        self._manual_download_handoff_state: ManualDownloadHandoffState | None = None
        self._last_manual_download_handoff: ManualDownloadHandoffResult | None = None
        self._allow_capture_outside_output_for_tests = allow_capture_outside_output_for_tests
        self._capture_timeout_ms = capture_timeout_ms
        self._research_chrome_direct_pdf_download_configured = (
            research_chrome_direct_pdf_download_configured
        )

    @property
    def last_capture(self) -> AuthorizedFileCaptureResult | None:
        return self._last_capture

    @property
    def manual_download_handoff_state(self) -> ManualDownloadHandoffState | None:
        return self._manual_download_handoff_state

    @property
    def last_manual_download_handoff(self) -> ManualDownloadHandoffResult | None:
        return self._last_manual_download_handoff

    def bind_institutional_route(self, route: "InstitutionalRouteResult") -> None:
        from ..institutional import HUNNUInstitutionalAccessResolver

        if not route.institutional_route_resolved or route.institutional_target_database_match is not True:
            from ..security import sanitize_text

            reason = sanitize_text(route.reason)[:600]
            raise SourceLayoutChanged(
                "Oxford institutional route is not resolved and identity-locked: " + reason
            )
        if HUNNUInstitutionalAccessResolver.canonical_source(route.requested_source) != self.name:
            raise SourceLayoutChanged("Institutional route requested a source other than OxfordAcademic")
        self._institutional_route = route

    def _gateway_trusted(self) -> bool:
        if self._institutional_route is None:
            return False
        return (
            self._institutional_route.institutional_route_resolved
            and self._institutional_route.institutional_target_database_match is True
        )

    @classmethod
    def _is_trusted_url(cls, value: str, *, institutional_route_verified: bool) -> bool:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme not in {"http", "https"}:
            return False
        if host == cls.official_host:
            return True
        return (
            institutional_route_verified
            and host == cls.hunnu_gateway_host
            and parsed.path.casefold().startswith("/vpn/")
        )

    @classmethod
    def _resolve_article_link(
        cls,
        source_url: str,
        href: str,
        *,
        institutional_route_verified: bool,
    ) -> str:
        absolute = urljoin(source_url, href)
        if cls._is_trusted_url(
            absolute,
            institutional_route_verified=institutional_route_verified,
        ):
            return absolute
        source = urlsplit(source_url)
        href_parts = urlsplit(href)
        if not (
            institutional_route_verified
            and (source.hostname or "").casefold() == cls.hunnu_gateway_host
            and source.path.casefold().startswith("/vpn/")
            and (_ARTICLE_PATH.search(href_parts.path) or _PDF_PATH.search(href_parts.path))
        ):
            return absolute
        source_article = _ARTICLE_PATH.search(source.path)
        source_pdf = _PDF_PATH.search(source.path)
        if source_article:
            prefix = source.path[: source_article.start()]
        elif source_pdf:
            prefix = source.path[: source_pdf.start()]
        else:
            search_marker = source.path.casefold().rfind("/search-results")
            prefix = source.path[:search_marker] if search_marker >= 0 else source.path.rstrip("/")
        query = href_parts.query or source.query
        return urlunsplit(
            (
                source.scheme,
                source.netloc,
                prefix.rstrip("/") + "/" + href_parts.path.lstrip("/"),
                query,
                "",
            )
        )

    @staticmethod
    def _parser(html: str) -> _ScienceDirectHTMLParser:
        parser = _ScienceDirectHTMLParser()
        parser.feed(html)
        return parser

    @classmethod
    def detect_interruption(cls, html: str, *, url: str = "") -> None:
        parser = cls._parser(html)
        title = _meta_first(parser, "citation_title", "dc.title", "og:title")
        has_article_metadata = title != UNKNOWN
        haystack = f"{url} {_meta_first(parser, 'title', 'og:title')} {parser.body_text}".casefold()
        challenge = any(
            marker in haystack
            for marker in (
                "captcha",
                "recaptcha",
                "verify you are human",
                "security verification",
                "checking your browser",
                "正在进行安全验证",
                "验证码",
                "滑块",
            )
        )
        if challenge and not has_article_metadata:
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=Oxford/HUNNU human verification required; "
                "BrowserReadyForManualAction=true"
            )
        login_url = any(marker in url.casefold() for marker in ("/login", "/signin", "/saml", "/oauth", "/cas/"))
        blocking_login = any(
            marker in haystack
            for marker in (
                "sign in to continue",
                "authentication required",
                "session has expired",
                "institutional login required",
                "统一身份认证",
                "二次认证",
            )
        )
        if (login_url or blocking_login) and not has_article_metadata:
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=Oxford/HUNNU manual authentication required; "
                "BrowserReadyForManualAction=true"
            )

    @classmethod
    def parse_search_results_html(
        cls,
        html: str,
        *,
        query: str,
        source_url: str = "https://academic.oup.com/search-results",
        max_results: int = 30,
        institutional_route_verified: bool = False,
    ) -> list[LiteratureRecord]:
        cls.detect_interruption(html, url=source_url)
        parser = _OxfordSearchHTMLParser()
        parser.feed(html)
        records: list[LiteratureRecord] = []
        seen: set[str] = set()
        for anchor in parser.anchors:
            absolute = cls._resolve_article_link(
                source_url,
                anchor.href,
                institutional_route_verified=institutional_route_verified,
            )
            if not cls._is_trusted_url(
                absolute,
                institutional_route_verified=institutional_route_verified,
            ):
                continue
            identifier = _stable_identifier(absolute)
            title = re.sub(r"\s+", " ", anchor.text).strip()
            if identifier == UNKNOWN or not title or identifier in seen:
                continue
            seen.add(identifier)
            canonical_url = _canonical_article_url(absolute)
            paper_id = stable_paper_id(title=title)
            records.append(
                LiteratureRecord(
                    paper_id=paper_id,
                    title=title,
                    source_database=cls.name,
                    source_page=canonical_url,
                    navigation_url=absolute,
                    stable_identifier=identifier,
                    search_query=query,
                    canonical_paper_id=paper_id,
                )
            )
            if len(records) >= max_results:
                break
        return records

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
        title = _meta_first(parser, "citation_title", "dc.title", "og:title")
        authors = _meta_all(parser, "citation_author", "dc.creator")
        date = _meta_first(parser, "citation_publication_date", "citation_online_date", "dc.date")
        year_match = re.search(r"(?:18|19|20|21)\d{2}", date)
        year = year_match.group(0) if year_match else UNKNOWN
        journal = _meta_first(parser, "citation_journal_title", "prism.publicationname")
        doi = normalize_doi(_meta_first(parser, "citation_doi", "dc.identifier", "prism.doi"))
        volume = _meta_first(parser, "citation_volume", "prism.volume")
        issue = _meta_first(parser, "citation_issue", "prism.number")
        first_page = _meta_first(parser, "citation_firstpage", "prism.startingpage")
        last_page = _meta_first(parser, "citation_lastpage", "prism.endingpage")
        pages = (
            f"{first_page}-{last_page}"
            if first_page != UNKNOWN and last_page not in {UNKNOWN, first_page}
            else first_page
        )
        abstract = _meta_first(parser, "citation_abstract", "description", "dc.description", "og:description")
        raw_keywords = _meta_all(parser, "citation_keywords", "keywords", "dc.subject")
        keywords = tuple(
            dict.fromkeys(
                item.strip()
                for value in raw_keywords
                for item in re.split(r"[;,；，]", value)
                if item.strip()
            )
        )
        language = _meta_first(parser, "citation_language", "dc.language")
        if language == UNKNOWN and parser.html_language != UNKNOWN:
            language = parser.html_language
        issn = _meta_first(parser, "citation_issn", "prism.issn")
        stable_identifier = _stable_identifier(source_url)
        canonical_url = _canonical_article_url(source_url)
        publisher_evidence = title != UNKNOWN and journal != UNKNOWN
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
            publication_type="JournalArticle" if publisher_evidence else UNKNOWN,
            publication_status=(
                PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value
                if publisher_evidence
                else PublicationStatus.UNKNOWN.value
            ),
            abstract=abstract,
            keywords=keywords,
            source_database=cls.name,
            source_page=canonical_url,
            navigation_url=source_url,
            stable_identifier=stable_identifier,
            search_query=search_query,
            canonical_paper_id=paper_id,
        )

    @staticmethod
    def identity_matches(expected: LiteratureRecord, actual: LiteratureRecord) -> bool:
        expected_doi = normalize_doi(expected.doi)
        actual_doi = normalize_doi(actual.doi)
        if expected_doi != UNKNOWN and actual_doi != UNKNOWN and expected_doi != actual_doi:
            return False
        if expected_doi != UNKNOWN and actual_doi == UNKNOWN:
            return False
        expected_title = normalize_title(expected.title)
        actual_title = normalize_title(actual.title)
        if expected_title != UNKNOWN and expected_title != actual_title:
            return False
        if (
            expected.stable_identifier != UNKNOWN
            and expected.stable_identifier != actual.stable_identifier
        ):
            return False
        return expected_doi != UNKNOWN or expected_title != UNKNOWN

    @staticmethod
    def validate_pdf_identity(path: Path, record: LiteratureRecord) -> OxfordPDFIdentityResult:
        try:
            verification = verify_external_paper_identity(path, record)
            text = AuthorizedFullTextValidator.extract_pdf_first_page_text(path) or ""
        except Exception:
            return OxfordPDFIdentityResult(False, False, False, False)
        title_match = verification.title_verification == "matched"
        expected_doi = normalize_doi(record.doi)
        # The shared local helper excludes References and rejects conflicting
        # article DOI evidence. A DOI cited by another paper is never identity.
        doi_match = verification.doi_verification == DOIContentVerification.MATCHED
        first_author = normalize_person(record.first_author)
        author_match = (
            first_author != UNKNOWN
            and first_author.replace(" ", "") in normalize_person(text).replace(" ", "")
        )
        title_evidence_required = normalize_title(record.title) != UNKNOWN
        confirmed = (
            verification.verified
            and (not title_evidence_required or title_match)
            and (doi_match if expected_doi != UNKNOWN else title_match and author_match)
        )
        return OxfordPDFIdentityResult(confirmed, title_match, doi_match, author_match)

    @staticmethod
    def _quarantine(path: Path) -> Path:
        base = _logical_path(QUARANTINE_DIR)
        _windows_io_path(base).mkdir(parents=True, exist_ok=True)
        source = _logical_path(path)
        target = base / source.name
        counter = 1
        while _windows_io_path(target).exists():
            target = target.with_name(
                f"{source.stem}_identity-mismatch_{counter}{source.suffix}"
            )
            counter += 1
        shutil.move(str(_windows_io_path(source)), str(_windows_io_path(target)))
        return target

    @classmethod
    def check_fulltext_access_html(
        cls,
        html: str,
        *,
        source_url: str,
        institutional_route_verified: bool = False,
    ) -> AccessDecision:
        cls.detect_interruption(html, url=source_url)
        parser = cls._parser(html)
        metadata_pdf = _meta_first(parser, "citation_pdf_url")
        metadata_pdf_stable = sanitize_url(metadata_pdf) if metadata_pdf != UNKNOWN else UNKNOWN
        candidate: _Anchor | None = None
        candidate_url = UNKNOWN
        for anchor in parser.anchors:
            absolute = cls._resolve_article_link(
                source_url,
                anchor.href,
                institutional_route_verified=institutional_route_verified,
            )
            parsed = urlsplit(absolute)
            label = re.sub(
                r"\s+",
                " ",
                " ".join(
                    (
                        anchor.text,
                        anchor.attributes.get("aria-label", ""),
                        anchor.attributes.get("title", ""),
                    )
                ),
            ).strip().casefold()
            disabled = "disabled" in anchor.attributes or anchor.attributes.get("aria-disabled", "").casefold() == "true"
            rejected = any(marker in label for marker in _REJECT_PDF_LABELS)
            metadata_path = (
                urlsplit(metadata_pdf_stable).path.casefold()
                if metadata_pdf_stable != UNKNOWN
                else ""
            )
            action_path = parsed.path.casefold()
            stable_matches_metadata = (
                metadata_pdf_stable == UNKNOWN
                or sanitize_url(absolute) == metadata_pdf_stable
                or (metadata_path and action_path.endswith(metadata_path))
                or (
                    metadata_path
                    and Path(action_path).name == Path(metadata_path).name
                )
            )
            if (
                cls._is_trusted_url(
                    absolute,
                    institutional_route_verified=institutional_route_verified,
                )
                and _PDF_PATH.search(parsed.path)
                and "pdf" in label
                and not disabled
                and not rejected
                and stable_matches_metadata
            ):
                candidate = anchor
                candidate_url = absolute
                break

        haystack = parser.body_text.casefold()
        if candidate is None:
            explicit_denial = any(
                marker in haystack
                for marker in (
                    "you do not have access",
                    "get access",
                    "purchase this article",
                    "rent this article",
                    "sign in for access",
                )
            )
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.METADATA_ONLY if explicit_denial else AccessType.UNKNOWN,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason=(
                    "NotAccessible: Oxford page did not expose an authorized main-article PDF action"
                    if explicit_denial
                    else "Unknown: Oxford full-text status could not be established"
                ),
            )

        if "open access" in haystack or "creative commons" in haystack:
            access_type = AccessType.OPEN_ACCESS
            state = "OpenAccess"
        elif "purchased" in haystack:
            access_type = AccessType.INSTITUTIONAL_AUTHENTICATED
            state = "Purchased"
        elif any(
            marker in haystack
            for marker in (
                "access provided by",
                "institutional access",
                "signed in via",
                "湖南师范大学",
            )
        ):
            access_type = AccessType.INSTITUTIONAL_AUTHENTICATED
            state = "InstitutionalAuthenticated"
        elif "subscription" in haystack:
            access_type = AccessType.INSTITUTIONAL_AUTHENTICATED
            state = "SubscriptionAccessible"
        else:
            access_type = AccessType.PUBLIC_FULL_TEXT
            state = "PublicFullText"
        return AccessDecision(
            full_text_accessible=True,
            access_type=access_type,
            authorized_access=True,
            status=RunStatus.SUCCESS,
            reason=f"{state}: enabled official Oxford main-article PDF action confirmed",
            download_url=candidate_url,
            download_locator="a[href*='/article-pdf/']",
        )

    async def _content(self) -> tuple[str, str]:
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        observation = await self.browser.execute(ObserveCommand(include_html=True))
        return observation.require_html(), observation.url

    def _gateway_navigation(self, relative_path: str, *, query: dict[str, str] | None = None) -> str:
        if not self._gateway_trusted() or self._institutional_route is None:
            raise SourceLayoutChanged("Verified Oxford institutional gateway route is unavailable")
        gateway = urlsplit(self._institutional_route.publisher_navigation_url)
        if (gateway.hostname or "").casefold() != self.hunnu_gateway_host:
            raise SourceLayoutChanged("Oxford institutional route has no trusted gateway navigation URL")
        path = gateway.path.rstrip("/") + "/" + relative_path.lstrip("/")
        parameters = list(parse_qsl(gateway.query, keep_blank_values=True))
        if query:
            parameters.extend(query.items())
        return urlunsplit((gateway.scheme, gateway.netloc, path, urlencode(parameters), ""))

    @classmethod
    def _public_doi_metadata_record(cls, payload: Any, *, requested_doi: str) -> LiteratureRecord:
        """Validate a claimed identity from one public Crossref work record.

        Only ``resource.primary.URL`` can supply the publisher landing page.
        The DOI resolver URL and full-text/TDM ``link`` entries are never used.
        This does not authorize full text: Oxford must independently confirm
        the DOI, title and stable article identifier afterwards.
        """

        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise SourceLayoutChanged("Oxford exact DOI metadata response was not a confirmed Crossref work")
        message = payload.get("message")
        message_doi = message.get("DOI") if isinstance(message, dict) else None
        if not isinstance(message_doi, str) or message_doi.strip().casefold() != requested_doi:
            raise SourceLayoutChanged("Oxford exact DOI metadata response did not match the requested DOI")
        titles = message.get("title")
        if not isinstance(titles, list) or len(titles) != 1 or not isinstance(titles[0], str) or not titles[0].strip():
            raise SourceLayoutChanged("Oxford exact DOI metadata did not supply one unambiguous title")
        resource = message.get("resource")
        primary = resource.get("primary") if isinstance(resource, dict) else None
        landing = primary.get("URL") if isinstance(primary, dict) else None
        if not isinstance(landing, str):
            raise SourceLayoutChanged("Oxford exact DOI metadata had no primary publisher landing URL")
        try:
            parsed = urlsplit(landing)
            safe = (
                parsed.scheme == "https"
                and parsed.hostname is not None
                and parsed.hostname.casefold().rstrip(".") == cls.official_host
                and parsed.port in {None, 443}
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and "%" not in parsed.path
                and re.search(r"[\x00-\x20\x7f\\]", landing) is None
                and _ARTICLE_PATH.fullmatch(parsed.path.rstrip("/")) is not None
                and all(
                    part not in {".", ".."} and re.fullmatch(r"[A-Za-z0-9._-]+", part)
                    for part in parsed.path.split("/")[1:]
                )
                and ".pdf" not in parsed.path.casefold()
                and landing == _canonical_article_url(landing)
            )
        except ValueError:
            safe = False
        if not safe:
            raise SourceLayoutChanged(
                "Oxford exact DOI primary URL was not a canonical unsigned Oxford article landing page"
            )
        paper_id = stable_paper_id(doi=requested_doi)
        return LiteratureRecord(
            paper_id=paper_id,
            title=titles[0].strip(),
            doi=requested_doi,
            source_database=cls.name,
            source_page=_canonical_article_url(landing),
            navigation_url=landing,
            stable_identifier=_stable_identifier(landing),
            search_query=requested_doi,
            canonical_paper_id=paper_id,
        )

    async def _resolve_public_doi_metadata(self, doi: str) -> list[LiteratureRecord]:
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        self._resolved_doi_record = None
        # This fixed, unauthenticated bibliographic endpoint is separate from
        # the publisher allowlist. It never supplies or downloads full text.
        endpoint = f"{_PUBLIC_DOI_METADATA_ORIGIN}/works/{quote(doi, safe='')}"
        await self.browser.execute(NavigateCommand(endpoint))
        observation = await self.browser.execute(
            ObserveCommand(include_html=False, include_visible_text=True)
        )
        try:
            parsed = urlsplit(observation.url)
            endpoint_matches = (
                parsed.scheme == "https"
                and parsed.hostname == "api.crossref.org"
                and parsed.port in {None, 443}
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and unquote(parsed.path).casefold() == f"/works/{doi}".casefold()
            )
        except ValueError:
            endpoint_matches = False
        if not endpoint_matches:
            raise SourceLayoutChanged("Oxford DOI metadata navigation left the fixed Crossref DOI endpoint")
        text = observation.visible_text
        if not isinstance(text, str) or not text.strip() or len(text) > _PUBLIC_DOI_METADATA_MAX_CHARS:
            raise SourceLayoutChanged("Oxford DOI metadata JSON observation is unavailable or exceeds its bound")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            # A rendered challenge must still hand off immediately; do not
            # poll it, fall back to a request client, or change providers.
            self.detect_interruption(text, url=observation.url)
            raise SourceLayoutChanged("Oxford DOI metadata page did not expose readable public JSON") from exc
        record = self._public_doi_metadata_record(payload, requested_doi=doi)
        await self.open_result(record)
        metadata = await self.extract_metadata(search_query=doi)
        if normalize_doi(metadata.doi) != doi or not metadata.target_identity_confirmed:
            raise SourceLayoutChanged("Oxford page did not confirm the public DOI metadata identity")
        self._resolved_doi_record = metadata
        return [metadata]

    async def search(self, query: str, request: LiteratureSearchRequest) -> list[LiteratureRecord]:
        exact_doi = normalize_doi(query)
        if exact_doi != UNKNOWN and exact_doi in {normalize_doi(value) for value in request.dois}:
            return await self._resolve_public_doi_metadata(exact_doi)
        self._resolved_doi_record = None
        gateway_navigation_available = (
            self._gateway_trusted()
            and self._institutional_route is not None
            and _host(self._institutional_route.publisher_navigation_url)
            == self.hunnu_gateway_host
        )
        search_url = (
            self._gateway_navigation("search-results", query={"q": query})
            if gateway_navigation_available
            else f"{self.search_origin}/search-results?q={quote_plus(query)}"
        )
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        await self.browser.execute(NavigateCommand(search_url))
        for attempt in range(SEARCH_RENDER_MAX_OBSERVATIONS):
            html, current_url = await self._content()
            # A challenge is a handoff, never something this render wait
            # retries through. Each observation checks it before readiness.
            self.detect_interruption(html, url=current_url)
            if not self._is_trusted_url(
                current_url, institutional_route_verified=self._gateway_trusted()
            ):
                raise SourceUnavailable("Oxford search navigation did not reach a trusted source route")
            if not urlsplit(current_url).path.rstrip("/").endswith("/search-results"):
                raise SourceLayoutChanged("Oxford search navigation did not reach the search-results page")
            parser = _OxfordSearchHTMLParser()
            parser.feed(html)
            body = parser.body_text.casefold()
            empty = any(marker in body for marker in _NO_RESULTS_MARKERS)
            if parser.anchors or empty:
                break
            if attempt + 1 == SEARCH_RENDER_MAX_OBSERVATIONS:
                raise SourceLayoutChanged(
                    "Oxford search did not expose recognized result title cards or an explicit zero-result state"
                )
            await asyncio.sleep(SEARCH_RENDER_POLL_SECONDS)
        results = self.parse_search_results_html(
            html,
            query=query,
            source_url=current_url,
            max_results=min(request.max_results_per_source, request.max_search_results),
            institutional_route_verified=self._gateway_trusted(),
        )
        if not results and not self._is_trusted_url(
            current_url,
            institutional_route_verified=self._gateway_trusted(),
        ):
            raise SourceUnavailable("Oxford search navigation did not reach a trusted source route")
        if not results and not empty:
            raise SourceLayoutChanged(
                "Oxford result cards did not expose a trusted article identity; zero results were not confirmed"
            )
        exact_doi = normalize_doi(query)
        if exact_doi != UNKNOWN:
            # A result page can quote a DOI in a snippet or reference. Only
            # the selected article's own verified metadata establishes an
            # exact DOI hit; never stamp the query DOI onto an unknown card.
            matched: list[LiteratureRecord] = []
            for result in results:
                await self.open_result(result)
                metadata = await self.extract_metadata(search_query=query)
                if normalize_doi(metadata.doi) == exact_doi:
                    matched.append(metadata)
                else:
                    metadata.target_identity_confirmed = False
            if results and not matched:
                raise SourceLayoutChanged(
                    "OXFORD_EXACT_DOI_LOOKUP_UNCONFIRMED: no article metadata matched the requested "
                    f"DOI among {len(results)} bounded search candidate(s); "
                    "this is not evidence of zero publisher results"
                )
            return matched
        return results

    async def open_result(self, record: LiteratureRecord) -> None:
        resolved = self._resolved_doi_record
        self._resolved_doi_record = None
        if resolved is record and record.target_identity_confirmed:
            # The workflow opens a search result again before extracting its
            # metadata. Avoid a second navigation only after a fresh live
            # observation proves the exact same Oxford identity still holds.
            html, current_url = await self._content()
            self.detect_interruption(html, url=current_url)
            if self._is_trusted_url(current_url, institutional_route_verified=self._gateway_trusted()):
                current = self.parse_article_html(html, source_url=current_url, search_query=record.search_query)
                if self.identity_matches(record, current):
                    current.target_identity_confirmed = True
                    self._expected_record = record
                    self._detail_record = current
                    return
            raise SourceLayoutChanged("Oxford DOI lookup page identity changed before the workflow opened it")
        target = record.navigation_url if record.navigation_url != UNKNOWN else record.source_page
        if (
            self._gateway_trusted()
            and _host(target) == self.official_host
            and self._institutional_route is not None
            and _host(self._institutional_route.publisher_navigation_url) == self.hunnu_gateway_host
        ):
            article = _article_match(target)
            if article is None:
                raise SourceLayoutChanged("Oxford article path is unavailable for institutional routing")
            target = self._gateway_navigation(article.group(0))
        if not self._is_trusted_url(
            target,
            institutional_route_verified=self._gateway_trusted(),
        ) or _article_match(target) is None:
            raise SourceLayoutChanged("Result URL is not a trusted stable Oxford article page")
        self._expected_record = record
        self._detail_record = None
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        await self.browser.execute(NavigateCommand(target))

    async def extract_metadata(self, *, search_query: str) -> LiteratureRecord:
        html, current_url = await self._content()
        if not self._is_trusted_url(
            current_url,
            institutional_route_verified=self._gateway_trusted(),
        ):
            raise SourceLayoutChanged("Oxford article navigation left the trusted route")
        record = self.parse_article_html(html, source_url=current_url, search_query=search_query)
        if self._expected_record is not None:
            if not self.identity_matches(self._expected_record, record):
                self._expected_record.target_identity_confirmed = False
                raise SourceLayoutChanged("Oxford search/detail target identity lock failed")
            record.paper_id = self._expected_record.paper_id
            record.canonical_paper_id = self._expected_record.paper_id
            record.target_identity_confirmed = True
        self._detail_record = record
        return record

    async def extract_abstract(self) -> str:
        html, current_url = await self._content()
        return self.parse_article_html(html, source_url=current_url).abstract

    async def check_fulltext_access(self) -> AccessDecision:
        if self._detail_record is None and self._expected_record is not None:
            await self.extract_metadata(search_query=self._expected_record.search_query)
        if self._detail_record is None or not self._detail_record.target_identity_confirmed:
            raise SourceLayoutChanged("Oxford target identity must be locked before access checking")
        html, current_url = await self._content()
        return self.check_fulltext_access_html(
            html,
            source_url=current_url,
            institutional_route_verified=self._gateway_trusted(),
        )

    def _response_matches_official_action(self, response_url: str, action_url: str) -> bool:
        if not self._is_trusted_url(
            response_url,
            institutional_route_verified=self._gateway_trusted(),
        ):
            return False
        response_path = urlsplit(response_url).path.casefold()
        action_path = urlsplit(action_url).path.casefold()
        action_name = Path(action_path).name
        return bool(
            _PDF_PATH.search(response_path)
            and (
                sanitize_url(response_url) == sanitize_url(action_url)
                or action_path in response_path
                or (action_name and response_path.endswith(f"/{action_name}"))
            )
        )

    @staticmethod
    def _native_pdf_viewer_opened(observation: Any) -> bool:
        candidates = [str(getattr(observation, "url", ""))]
        candidates.extend(
            str(getattr(summary, "url", ""))
            for summary in tuple(getattr(observation, "page_inventory", ()) or ())
        )
        for value in candidates:
            try:
                if urlsplit(value).path.casefold().endswith(".pdf"):
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _access_state(access: AccessDecision) -> str:
        state = access.reason.split(":", 1)[0].strip()
        return state if state in {
            "OpenAccess",
            "Purchased",
            "InstitutionalAuthenticated",
            "SubscriptionAccessible",
            "PublicFullText",
        } else UNKNOWN

    def arm_manual_download_handoff(
        self,
        record: LiteratureRecord,
        access: AccessDecision,
        *,
        pdf_viewer_opened: bool,
        watch_directory: Path | None = None,
        staging_directory: Path | None = None,
    ) -> ManualDownloadHandoffState:
        if not record.target_identity_confirmed:
            raise SourceLayoutChanged("Oxford target identity is not confirmed")
        if not access.full_text_accessible or not access.authorized_access:
            raise PermissionError("FULLTEXT_NOT_AUTHORIZED")
        if not pdf_viewer_opened:
            raise SourceUnavailable("PDFViewerOpened=false")
        if not self._is_trusted_url(
            access.download_url,
            institutional_route_verified=self._gateway_trusted(),
        ) or not _PDF_PATH.search(urlsplit(access.download_url).path):
            raise SourceLayoutChanged("OfficialPdfActionConfirmed=false")
        # Capture releases Chrome's run-specific download policy. A later
        # native Viewer click uses Chrome's configured default, which must be
        # explicitly supplied rather than guessed from the old staging path.
        configured_watch = watch_directory or self._manual_download_watch_directory
        if configured_watch is None or not Path(configured_watch).is_absolute():
            raise SourceUnavailable("ManualDownloadWatchDirectoryUnconfigured=true; configure an explicit absolute native download directory before clicking Download")
        watch = Path(configured_watch)
        if staging_directory is not None:
            staging = Path(staging_directory)
        else:
            controlled_root = getattr(self.browser, "downloads_dir", None)
            if not controlled_root:
                raise SourceUnavailable("Controlled manual download staging directory is unavailable")
            staging = Path(controlled_root) / "manual-handoff-staging"
        handoff = ManualDownloadHandoff(
            watch,
            staging,
            allow_outside_output_for_tests=self._allow_capture_outside_output_for_tests,
        )
        state = handoff.arm(hash_existing_pdfs=True)
        self._manual_download_handoff = handoff
        self._manual_download_handoff_state = state
        record.official_pdf_action_confirmed = True
        record.source_access_status = self._access_state(access)
        record.manual_download_required = True
        record.manual_download_handoff_armed = True
        record.user_native_viewer_click_required = True
        record.manual_download_handoff_used = True
        record.automatic_download_initiation = False
        record.automatic_download_detection = False
        record.oxford_unattended_download_ready = False
        record.acquisition_method = "MANUAL_NATIVE_VIEWER_DOWNLOAD_HANDOFF"
        record.source_host = self.official_host
        record.source_route = "HUNNU_GATEWAY_TO_OXFORD" if self._gateway_trusted() else "OXFORD_DIRECT"
        record.institutional_route_used = self._gateway_trusted()
        return state

    async def complete_manual_download_handoff(
        self,
        record: LiteratureRecord,
        *,
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 0.5,
        stable_observations: int = 2,
    ) -> Path:
        if self._manual_download_handoff is None or self._manual_download_handoff_state is None:
            raise SourceUnavailable("ManualDownloadHandoffArmed=false")
        try:
            detection = await self._manual_download_handoff.wait_for_completed_download(
                self._manual_download_handoff_state,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                stable_observations=stable_observations,
            )
            result = self._manual_download_handoff.stage(
                detection,
                controlled_filename=f"{record.paper_id}.pdf",
            )
        except ManualDownloadHandoffError as exc:
            raise SourceUnavailable(str(exc)) from exc
        identity = self.validate_pdf_identity(result.staged_path, record)
        record.target_title_matched = identity.title_matched
        record.target_doi_matched = identity.doi_matched
        record.target_identity_confirmed = identity.confirmed
        record.manual_download_detected = result.manual_download_detected
        record.human_download_action = True
        record.original_manual_download_preserved = result.original_manual_download_preserved
        record.download_initiation_mode = result.download_initiation_mode
        record.file_finalization_mode = result.file_finalization_mode
        record.user_native_viewer_click_required = True
        record.manual_download_handoff_used = True
        record.oxford_unattended_download_ready = False
        if not identity.confirmed:
            quarantined = self._quarantine(result.staged_path)
            raise SourceLayoutChanged(
                "Manual Oxford PDF failed local target identity validation; Quarantine=true; "
                f"QuarantineFile={quarantined.name}; OriginalManualDownloadPreserved=true"
            )
        self._last_manual_download_handoff = result
        return result.staged_path

    async def download_fulltext(self, record: LiteratureRecord, access: AccessDecision) -> Path:
        if not record.target_identity_confirmed:
            raise SourceLayoutChanged("Oxford target identity is not confirmed")
        if not access.full_text_accessible or not access.authorized_access:
            raise PermissionError("FULLTEXT_NOT_AUTHORIZED")
        if not self._is_trusted_url(
            access.download_url,
            institutional_route_verified=self._gateway_trusted(),
        ) or not _PDF_PATH.search(urlsplit(access.download_url).path):
            raise SourceLayoutChanged("OfficialPdfActionConfirmed=false")
        record.official_pdf_action_confirmed = True
        record.source_access_status = self._access_state(access)
        record.unattended_download_attempted = True
        record.research_chrome_direct_pdf_download_configured = (
            self._research_chrome_direct_pdf_download_configured
        )
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        source_route = "HUNNU_GATEWAY_TO_OXFORD" if self._gateway_trusted() else "OXFORD_DIRECT"
        provenance_host = self.hunnu_gateway_host if _host(access.download_url) == self.hunnu_gateway_host else self.official_host
        # Authorization, identity confirmation, and URL trust have passed;
        # budget the fetch before the click is issued.
        ticket = self.authorize_publisher_fetch(record)
        try:
            artifact = await self.browser.execute(
                DownloadCommand(
                    target=BrowserTarget(
                        css=access.download_locator,
                        text_regex=r"^\s*(?:download\s+)?pdf\s*$",
                    ),
                    suggested_filename=f"{record.paper_id}.pdf",
                    timeout_ms=self._capture_timeout_ms,
                    capture=DownloadCaptureSpec(
                        trusted_hosts=(
                            (
                                self.official_host,
                                self.hunnu_gateway_host,
                                "oup.silverchair-cdn.com",
                                ".silverchair.com",
                            )
                            if self._gateway_trusted()
                            else (
                                self.official_host,
                                "oup.silverchair-cdn.com",
                                ".silverchair.com",
                            )
                        ),
                        provenance_host=provenance_host,
                        source_route=source_route,
                        expected_url=access.download_url,
                        allow_outside_output_for_tests=self._allow_capture_outside_output_for_tests,
                    ),
                )
            )
            result = authorized_capture_result_from_artifact(artifact)
        except Exception as exc:
            try:
                observation = await self.browser.execute(
                    ObserveCommand(include_html=False, include_visible_text=False)
                )
            except Exception:
                observation = None
            if observation is not None and self._native_pdf_viewer_opened(observation):
                # The PDF has been served into the viewer, so publisher bytes
                # were fetched even though no file landed yet.
                ticket.record_outcome(
                    ok=False, detail="PDF opened in viewer; native download requires user action"
                )
                try:
                    state = self.arm_manual_download_handoff(
                        record,
                        access,
                        pdf_viewer_opened=True,
                    )
                except (SourceUnavailable, ManualDownloadHandoffError, OSError, ValueError) as arm_error:
                    raise SourceUserDownloadRequired(
                        "ACTION_REQUIRED_USER_DOWNLOAD=true; PDF is open in Chrome PDF Viewer; "
                        "ManualDownloadHandoffArmed=false; do not click Download until an explicit "
                        f"native download watch directory is configured: {arm_error}",
                    ) from exc
                raise SourceUserDownloadRequired(
                    "ACTION_REQUIRED_USER_DOWNLOAD=true; ACTION_REQUIRED_USER_LOGIN=false; "
                    "BrowserReadyForManualDownload=true; PDF is open in Chrome PDF Viewer; "
                    "click the native Download button once after the persisted handoff receipt is available; "
                    "then run the local manual-download-resume command",
                    handoff_state=state,
                ) from exc
            ticket.record_outcome(ok=False, detail=str(exc).strip() or type(exc).__name__)
            raise SourceUnavailable(str(exc)) from exc
        self._last_capture = result
        record.acquisition_method = result.acquisition_method.value
        record.source_host = result.source_host
        record.source_route = result.source_route
        record.institutional_route_used = self._gateway_trusted()
        record.download_event_emitted = result.download_event_emitted
        record.authorized_pdf_response_captured = result.authorized_pdf_response_captured
        record.manual_download_required = False
        record.manual_download_handoff_armed = False
        record.manual_download_detected = False
        record.human_download_action = False
        record.user_native_viewer_click_required = False
        record.manual_download_handoff_used = False
        record.automatic_download_initiation = True
        record.automatic_download_detection = True
        # A capture route alone is not readiness evidence. In particular a
        # directed Chrome download must carry a real browser event, and local
        # PDF identity still has to pass before readiness can become true.
        record.oxford_unattended_download_ready = False
        record.download_initiation_mode = "HARNESS_OFFICIAL_PDF_ACTION_CLICK"
        record.file_finalization_mode = "HARNESS_AUTOMATIC"
        identity = self.validate_pdf_identity(result.path, record)
        record.target_title_matched = identity.title_matched
        record.target_doi_matched = identity.doi_matched
        record.target_identity_confirmed = identity.confirmed
        if not identity.confirmed:
            quarantined = self._quarantine(result.path)
            # The bytes arrived and then failed validation -- the exact failure
            # shape the write-ahead ledger exists to count.
            ticket.record_outcome(
                ok=False,
                detail=f"fetched bytes failed identity validation; quarantined as {quarantined.name}",
            )
            raise SourceLayoutChanged(
                f"Captured Oxford PDF failed local target identity validation; Quarantine=true; "
                f"QuarantineFile={quarantined.name}"
            )
        automatic_event = (
            result.acquisition_method in {
                AcquisitionMethod.PLAYWRIGHT_DOWNLOAD_EVENT,
                AcquisitionMethod.BROWSER_DIRECTED_DOWNLOAD,
            }
            and result.download_event_emitted
            and not result.authorized_pdf_response_captured
        )
        automatic_response = (
            result.acquisition_method is AcquisitionMethod.AUTHORIZED_PDF_RESPONSE
            and result.authorized_pdf_response_captured
            and not result.download_event_emitted
        )
        record.oxford_unattended_download_ready = bool(
            (automatic_event or automatic_response)
            and not record.manual_download_handoff_used
            and not record.user_native_viewer_click_required
            and not record.human_download_action
        )
        ticket.record_outcome(ok=True, detail="download completed")
        return result.path

    async def get_citation(self) -> dict[str, Any]:
        record = await self.extract_metadata(search_query=UNKNOWN)
        return {
            "Title": record.title,
            "Authors": list(record.authors),
            "Journal": record.journal,
            "Year": record.year,
            "DOI": record.doi,
        }


__all__ = ["OxfordAcademicAdapter", "OxfordPDFIdentityResult"]
