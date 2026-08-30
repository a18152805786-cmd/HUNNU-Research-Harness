from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit

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
from ..fulltext import AuthorizedFullTextValidator
from ..normalization import normalize_doi, normalize_person, normalize_title, stable_paper_id
from ..security import sanitize_url
from ...paths import QUARANTINE_DIR

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
    ) -> None:
        super().__init__(browser)
        self._institutional_route = institutional_route
        self._expected_record: LiteratureRecord | None = None
        self._detail_record: LiteratureRecord | None = None
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
            raise SourceLayoutChanged("Oxford institutional route is not resolved and identity-locked")
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
        parser = cls._parser(html)
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
            and actual.stable_identifier != UNKNOWN
            and expected.stable_identifier != actual.stable_identifier
        ):
            return False
        return expected_doi != UNKNOWN or expected_title != UNKNOWN

    @staticmethod
    def validate_pdf_identity(path: Path, record: LiteratureRecord) -> OxfordPDFIdentityResult:
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]

            reader = PdfReader(str(path), strict=False)
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:2])
        except Exception:
            return OxfordPDFIdentityResult(False, False, False, False)
        title_match = AuthorizedFullTextValidator._title_matches_extracted_text(record.title, text) is True
        expected_doi = normalize_doi(record.doi)
        doi_candidates = {
            normalize_doi(match)
            for match in re.findall(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, flags=re.IGNORECASE)
        }
        doi_match = expected_doi != UNKNOWN and expected_doi in doi_candidates
        first_author = normalize_person(record.first_author)
        author_match = (
            first_author != UNKNOWN
            and first_author.replace(" ", "") in normalize_person(text).replace(" ", "")
        )
        confirmed = doi_match or (title_match and author_match)
        return OxfordPDFIdentityResult(confirmed, title_match, doi_match, author_match)

    @staticmethod
    def _quarantine(path: Path) -> Path:
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        source = Path(path)
        target = QUARANTINE_DIR / source.name
        counter = 1
        while target.exists():
            target = target.with_name(
                f"{source.stem}_identity-mismatch_{counter}{source.suffix}"
            )
            counter += 1
        shutil.move(str(path), str(target))
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

    async def search(self, query: str, request: LiteratureSearchRequest) -> list[LiteratureRecord]:
        search_url = (
            self._gateway_navigation("search-results", query={"q": query})
            if self._gateway_trusted()
            else f"{self.search_origin}/search-results?q={quote_plus(query)}"
        )
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        await self.browser.execute(NavigateCommand(search_url))
        html, current_url = await self._content()
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
        return results

    async def open_result(self, record: LiteratureRecord) -> None:
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
        watch = Path(
            watch_directory
            if watch_directory is not None
            else getattr(self.browser, "downloads_dir", "")
        )
        if not str(watch):
            raise SourceUnavailable("Manual download watch directory is unavailable")
        staging = Path(staging_directory) if staging_directory is not None else watch / "manual-handoff-staging"
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
                            (self.official_host, self.hunnu_gateway_host)
                            if self._gateway_trusted()
                            else (self.official_host,)
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
                state = self.arm_manual_download_handoff(
                    record,
                    access,
                    pdf_viewer_opened=True,
                )
                # The PDF has been served into the viewer, so publisher bytes
                # were fetched even though no file landed yet.
                ticket.record_outcome(
                    ok=False, detail="PDF opened in viewer; manual download handoff armed"
                )
                raise SourceUserDownloadRequired(
                    "ACTION_REQUIRED_USER_DOWNLOAD=true; ACTION_REQUIRED_USER_LOGIN=false; "
                    "BrowserReadyForManualDownload=true; PDF is open in Chrome PDF Viewer; "
                    "click the native Download button once while Harness waits for the file",
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
        record.oxford_unattended_download_ready = result.acquisition_method in {
            AcquisitionMethod.PLAYWRIGHT_DOWNLOAD_EVENT,
            AcquisitionMethod.AUTHORIZED_PDF_RESPONSE,
        }
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
