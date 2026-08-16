from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit

from ...browser.authorized_file_capture import (
    AcquisitionMethod,
    AuthorizedFileCaptureResult,
    AuthorizedFileCaptureUnavailable,
    BrowserAuthorizedFileCapture,
)
from ...paths import QUARANTINE_DIR
from .base import LiteratureSourceAdapter, SourceActionRequired, SourceLayoutChanged, SourceUnavailable
from .sciencedirect import _Anchor, _ScienceDirectHTMLParser, _meta_all, _meta_first
from ..fulltext import AuthorizedFullTextValidator
from ..models import (
    AccessDecision,
    AccessType,
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
    UNKNOWN,
)
from ..normalization import normalize_doi, normalize_person, normalize_title, stable_paper_id
from ..security import sanitize_url

if TYPE_CHECKING:
    from ..institutional import InstitutionalRouteResult


_ARTICLE_PATH = re.compile(r"/article/(10\.\d{4,9}/[^/?#]+)", re.IGNORECASE)
_PDF_PATH = re.compile(r"/content/pdf/(10\.\d{4,9}/[^?#]+)\.pdf$", re.IGNORECASE)
_REJECT_PDF_LABELS = (
    "supplement",
    "supplementary",
    "supporting information",
    "related article",
    "reference",
    "issue pdf",
    "download issue",
    "download full issue",
)


def _host(value: str) -> str:
    try:
        return (urlsplit(value).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _canonical_article_url(doi: str, source_url: str) -> str:
    normalized = normalize_doi(doi)
    if normalized != UNKNOWN:
        return f"https://link.springer.com/article/{normalized}"
    try:
        parsed = urlsplit(source_url)
    except ValueError:
        return UNKNOWN
    match = _ARTICLE_PATH.search(parsed.path)
    if (parsed.hostname or "").casefold() == "link.springer.com" and match:
        return f"https://link.springer.com{match.group(0)}"
    return UNKNOWN


@dataclass(frozen=True)
class SpringerPDFIdentityResult:
    confirmed: bool
    title_matched: bool
    doi_matched: bool
    author_matched: bool


class SpringerLinkAdapter(LiteratureSourceAdapter):
    """Bounded Springer Nature Link adapter using publisher citation metadata.

    The adapter treats an official, enabled PDF control as access evidence. It
    never attempts to obtain credentials, bypass access checks, or follow a
    non-Springer PDF host.
    """

    name = "SpringerLink"
    supports_unattended_download = True
    supports_preflight = True
    search_origin = "https://link.springer.com"
    official_host = "link.springer.com"
    hunnu_gateway_host = "yclib.hunnu.edu.cn"

    def __init__(
        self,
        browser: Any,
        *,
        institutional_route: "InstitutionalRouteResult | None" = None,
        allow_capture_outside_output_for_tests: bool = False,
        capture_timeout_ms: int = 45_000,
    ) -> None:
        super().__init__(browser)
        self._institutional_route = institutional_route
        self._expected_record: LiteratureRecord | None = None
        self._detail_record: LiteratureRecord | None = None
        self._last_capture: AuthorizedFileCaptureResult | None = None
        self._allow_capture_outside_output_for_tests = allow_capture_outside_output_for_tests
        self._capture_timeout_ms = capture_timeout_ms

    @property
    def last_capture(self) -> AuthorizedFileCaptureResult | None:
        return self._last_capture

    def bind_institutional_route(self, route: "InstitutionalRouteResult") -> None:
        from ..institutional import HUNNUInstitutionalAccessResolver

        if not route.institutional_route_resolved or route.institutional_target_database_match is not True:
            raise SourceLayoutChanged("Springer institutional route is not resolved and identity-locked")
        if HUNNUInstitutionalAccessResolver.canonical_source(route.requested_source) != self.name:
            raise SourceLayoutChanged("Institutional route requested a source other than SpringerLink")
        route_host = _host(route.publisher_navigation_url)
        if route_host not in {self.official_host, self.hunnu_gateway_host}:
            raise SourceLayoutChanged("Springer institutional route has no trusted publisher destination")
        self._institutional_route = route
        if route_host == self.hunnu_gateway_host and not self._gateway_trusted():
            self._institutional_route = None
            raise SourceLayoutChanged("Springer HUNNU gateway route lacks resolver verification evidence")

    def _gateway_trusted(self) -> bool:
        if self._institutional_route is None:
            return False
        from ..institutional import HUNNUInstitutionalAccessResolver

        route = self._institutional_route
        try:
            gateway = urlsplit(route.publisher_navigation_url)
        except ValueError:
            return False
        if not (
            route.institutional_route_resolved
            and route.institutional_target_database_match is True
            and HUNNUInstitutionalAccessResolver.canonical_source(route.requested_source) == self.name
            and gateway.scheme == "https"
            and (gateway.hostname or "").casefold() == self.hunnu_gateway_host
            and gateway.path.casefold().startswith("/vpn/")
            and route.publisher_entry_url == "https://yclib.hunnu.edu.cn/vpn/"
            and route.route_steps
        ):
            return False
        final_step = route.route_steps[-1]
        return (
            final_step.official_domain.casefold() == self.hunnu_gateway_host
            and final_step.stable_url == "https://yclib.hunnu.edu.cn/vpn/"
        )

    def _gateway_navigation(
        self,
        relative_path: str,
        *,
        query: list[tuple[str, str]] | None = None,
    ) -> str:
        if not self._gateway_trusted() or self._institutional_route is None:
            raise SourceLayoutChanged("Verified Springer institutional gateway route is unavailable")
        gateway = urlsplit(self._institutional_route.publisher_navigation_url)
        path = gateway.path.rstrip("/") + "/" + relative_path.lstrip("/")
        parameters = list(parse_qsl(gateway.query, keep_blank_values=True))
        if query:
            parameters.extend(query)
        return urlunsplit((gateway.scheme, gateway.netloc, path, urlencode(parameters), ""))

    def _gateway_url_is_bound(self, value: str) -> bool:
        if not self._gateway_trusted() or self._institutional_route is None:
            return False
        try:
            candidate = urlsplit(value)
            gateway = urlsplit(self._institutional_route.publisher_navigation_url)
        except ValueError:
            return False
        prefix = gateway.path.rstrip("/") + "/"
        return (
            candidate.scheme == gateway.scheme
            and candidate.netloc.casefold() == gateway.netloc.casefold()
            and candidate.path.startswith(prefix)
        )

    def _is_trusted_url(self, value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        if (parsed.hostname or "").casefold() == self.official_host:
            return True
        return self._gateway_url_is_bound(value)

    def _resolve_gateway_link(self, source_url: str, href: str) -> str:
        absolute = urljoin(source_url, href)
        if not self._gateway_trusted() or not self._gateway_url_is_bound(source_url):
            return absolute
        if self._gateway_url_is_bound(absolute):
            return absolute
        href_parts = urlsplit(href)
        if _ARTICLE_PATH.search(href_parts.path) or _PDF_PATH.search(href_parts.path):
            return self._gateway_navigation(
                href_parts.path,
                query=list(parse_qsl(href_parts.query, keep_blank_values=True)),
            )
        return absolute

    @staticmethod
    def _parser(html: str) -> _ScienceDirectHTMLParser:
        parser = _ScienceDirectHTMLParser()
        parser.feed(html)
        return parser

    @classmethod
    def detect_interruption(cls, html: str, *, url: str = "") -> None:
        parser = cls._parser(html)
        haystack = f"{url} {_meta_first(parser, 'title', 'og:title')} {parser.body_text}".casefold()
        if any(
            marker in haystack
            for marker in (
                "captcha",
                "recaptcha",
                "verify you are human",
                "security verification",
                "security challenge",
                "验证码",
                "滑块",
            )
        ):
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=CAPTCHA or human verification required; "
                "BrowserReadyForManualAction=true"
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
                "统一身份认证",
            )
        )
        if (login_url or blocking_login) and not has_article_metadata:
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=School or database login required; "
                "BrowserReadyForManualAction=true"
            )

    @classmethod
    def parse_search_results_html(
        cls,
        html: str,
        *,
        query: str,
        source_url: str = "https://link.springer.com/search",
        max_results: int = 30,
    ) -> list[LiteratureRecord]:
        cls.detect_interruption(html, url=source_url)
        parser = cls._parser(html)
        records: list[LiteratureRecord] = []
        seen: set[str] = set()
        for anchor in parser.anchors:
            match = _ARTICLE_PATH.search(anchor.href)
            title = re.sub(r"\s+", " ", anchor.text).strip()
            if not match or not title:
                continue
            doi = normalize_doi(match.group(1))
            if doi in seen:
                continue
            seen.add(doi)
            records.append(
                LiteratureRecord(
                    paper_id=stable_paper_id(doi=doi, title=title),
                    title=title,
                    doi=doi,
                    source_database=cls.name,
                    source_page=sanitize_url(urljoin(cls.search_origin, anchor.href)),
                    stable_identifier=doi,
                    search_query=query,
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
        if doi == UNKNOWN:
            match = _ARTICLE_PATH.search(urlsplit(source_url).path)
            doi = normalize_doi(match.group(1)) if match else UNKNOWN
        volume = _meta_first(parser, "citation_volume", "prism.volume")
        issue = _meta_first(parser, "citation_issue", "prism.number")
        first_page = _meta_first(parser, "citation_firstpage", "prism.startingpage")
        last_page = _meta_first(parser, "citation_lastpage", "prism.endingpage")
        if first_page != UNKNOWN and last_page != UNKNOWN and first_page != last_page:
            pages = f"{first_page}-{last_page}"
        else:
            pages = first_page
        abstract = _meta_first(parser, "citation_abstract", "description", "dc.description", "og:description")
        raw_keywords = _meta_all(parser, "citation_keywords", "keywords", "dc.subject")
        keywords: list[str] = []
        for value in raw_keywords:
            keywords.extend(item.strip() for item in re.split(r"[;,；，]", value) if item.strip())
        language = _meta_first(parser, "citation_language", "dc.language")
        if language == UNKNOWN and parser.html_language != UNKNOWN:
            language = parser.html_language
        issn = _meta_first(parser, "citation_issn", "prism.issn")
        publisher_evidence = journal != UNKNOWN and doi != UNKNOWN
        canonical_url = _canonical_article_url(doi, source_url)
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
            keywords=tuple(dict.fromkeys(keywords)),
            source_database=cls.name,
            source_page=canonical_url,
            navigation_url=source_url,
            stable_identifier=doi,
            search_query=search_query,
            canonical_paper_id=paper_id,
        )

    @staticmethod
    def identity_matches(expected: LiteratureRecord, actual: LiteratureRecord) -> bool:
        expected_doi = normalize_doi(expected.doi)
        actual_doi = normalize_doi(actual.doi)
        if expected_doi != UNKNOWN or actual_doi != UNKNOWN:
            return expected_doi != UNKNOWN and expected_doi == actual_doi
        expected_title = normalize_title(expected.title)
        actual_title = normalize_title(actual.title)
        return expected_title != UNKNOWN and expected_title == actual_title

    @staticmethod
    def validate_pdf_identity(path: Path, record: LiteratureRecord) -> SpringerPDFIdentityResult:
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]

            reader = PdfReader(str(path), strict=False)
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:2])
        except Exception:
            return SpringerPDFIdentityResult(False, False, False, False)
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
        confirmed = doi_match if expected_doi != UNKNOWN else title_match and author_match
        return SpringerPDFIdentityResult(confirmed, title_match, doi_match, author_match)

    def _quarantine(self, path: Path) -> Path:
        if self._allow_capture_outside_output_for_tests:
            base = Path(getattr(self.browser, "downloads_dir", Path(path).parent)) / "quarantine"
        else:
            base = QUARANTINE_DIR
        base.mkdir(parents=True, exist_ok=True)
        source = Path(path)
        target = base / source.name
        counter = 1
        while target.exists():
            target = target.with_name(f"{source.stem}_identity-mismatch_{counter}{source.suffix}")
            counter += 1
        shutil.move(str(source), str(target))
        return target

    @classmethod
    def check_fulltext_access_html(cls, html: str, *, source_url: str) -> AccessDecision:
        cls.detect_interruption(html, url=source_url)
        parser = cls._parser(html)
        candidate: _Anchor | None = None
        candidate_url = UNKNOWN
        for anchor in parser.anchors:
            absolute = urljoin(source_url, anchor.href)
            parsed = urlsplit(absolute)
            label = f"{anchor.text} {anchor.attributes.get('aria-label', '')}".casefold()
            disabled = "disabled" in anchor.attributes or anchor.attributes.get("aria-disabled", "").casefold() == "true"
            if (
                parsed.hostname == "link.springer.com"
                and _PDF_PATH.search(parsed.path)
                and "pdf" in label
                and "download" in label
                and not disabled
            ):
                candidate = anchor
                candidate_url = absolute
                break
        if candidate is None:
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.METADATA_ONLY,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="No enabled official Springer PDF download control was present",
            )

        haystack = parser.body_text.casefold()
        if "open access" in haystack or "creative commons" in haystack:
            access_type = AccessType.OPEN_ACCESS
        elif any(
            marker in haystack
            for marker in (
                "you have full access",
                "access provided by hunan normal university",
                "access provided by 湖南师范大学",
            )
        ):
            access_type = AccessType.INSTITUTIONAL_AUTHENTICATED
        else:
            access_type = AccessType.PUBLIC_FULL_TEXT
        return AccessDecision(
            full_text_accessible=True,
            access_type=access_type,
            authorized_access=True,
            status=RunStatus.SUCCESS,
            reason="Official Springer article page exposed an enabled Download PDF control",
            download_url=candidate_url,
            download_locator="a[href^='/content/pdf/']",
        )

    def _check_gateway_fulltext_access_html(self, html: str, *, source_url: str) -> AccessDecision:
        if not self._gateway_url_is_bound(source_url):
            raise SourceLayoutChanged("Springer page is outside the bound institutional gateway route")
        self.detect_interruption(html, url=source_url)
        parser = self._parser(html)
        candidate_url = UNKNOWN
        for anchor in parser.anchors:
            absolute = self._resolve_gateway_link(source_url, anchor.href)
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
            disabled = (
                "disabled" in anchor.attributes
                or anchor.attributes.get("aria-disabled", "").casefold() == "true"
            )
            if (
                self._gateway_url_is_bound(absolute)
                and _PDF_PATH.search(parsed.path)
                and "pdf" in label
                and "download" in label
                and not disabled
                and not any(marker in label for marker in _REJECT_PDF_LABELS)
            ):
                candidate_url = absolute
                break
        haystack = parser.body_text.casefold()
        institutional_signal = any(
            marker in haystack
            for marker in (
                "access provided by hunan normal university",
                "access provided by 湖南师范大学",
                "you have full access",
                "full access",
            )
        ) or bool(re.search(r"\baccess\s+provided\s+by\s+\S", parser.body_text, re.IGNORECASE))
        explicit_denial = any(
            marker in haystack
            for marker in (
                "you do not have full access",
                "you do not have access",
                "purchase this article",
                "get access",
            )
        )
        if candidate_url == UNKNOWN or not institutional_signal or explicit_denial:
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.UNKNOWN,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason=(
                    "Verified HUNNU route did not expose both institutional access state "
                    "and an enabled official Springer PDF control"
                ),
            )
        return AccessDecision(
            full_text_accessible=True,
            access_type=AccessType.INSTITUTIONAL_AUTHENTICATED,
            authorized_access=True,
            status=RunStatus.SUCCESS,
            reason=(
                "InstitutionalAuthenticated: verified HUNNU route, locked Springer article, "
                "and enabled official PDF control confirmed"
            ),
            download_url=candidate_url,
            download_locator="a[href*='/content/pdf/']",
        )

    async def _content(self) -> tuple[str, str]:
        page = getattr(self.browser, "page", None)
        if page is None:
            raise SourceUnavailable("Browser page is unavailable")
        return await page.content(), page.url

    async def search(self, query: str, request: LiteratureSearchRequest) -> list[LiteratureRecord]:
        await self.browser.goto(f"{self.search_origin}/search?query={quote_plus(query)}")
        html, current_url = await self._content()
        results = self.parse_search_results_html(
            html,
            query=query,
            source_url=current_url,
            max_results=min(request.max_results_per_source, request.max_search_results),
        )
        if not results and "link.springer.com" not in current_url.casefold():
            raise SourceUnavailable("Springer search navigation did not reach the expected source")
        return results

    async def open_result(self, record: LiteratureRecord) -> None:
        parsed = urlsplit(record.source_page)
        if parsed.hostname != "link.springer.com" or not _ARTICLE_PATH.search(parsed.path):
            raise SourceLayoutChanged("Result URL is not a stable Springer article page")
        target = record.source_page
        if self._gateway_trusted():
            article = _ARTICLE_PATH.search(parsed.path)
            if article is None:
                raise SourceLayoutChanged("Springer article path is unavailable for institutional routing")
            target = self._gateway_navigation(article.group(0))
        if not self._is_trusted_url(target):
            raise SourceLayoutChanged("Result URL is outside the trusted Springer route")
        record.navigation_url = target
        self._expected_record = record
        self._detail_record = None
        await self.browser.goto(target)

    async def extract_metadata(self, *, search_query: str) -> LiteratureRecord:
        html, current_url = await self._content()
        if not self._is_trusted_url(current_url):
            raise SourceLayoutChanged("Springer article navigation left the trusted route")
        record = self.parse_article_html(html, source_url=current_url, search_query=search_query)
        if self._expected_record is not None:
            if not self.identity_matches(self._expected_record, record):
                self._expected_record.target_identity_confirmed = False
                raise SourceLayoutChanged("Springer search/detail target identity lock failed")
            record.paper_id = self._expected_record.paper_id
            record.canonical_paper_id = self._expected_record.paper_id
            record.target_identity_confirmed = True
        record.institutional_route_used = self._gateway_trusted()
        record.institution = "湖南师范大学" if self._gateway_trusted() else UNKNOWN
        record.source_route = "HUNNU_GATEWAY_TO_SPRINGER" if self._gateway_trusted() else "SPRINGER_DIRECT"
        self._detail_record = record
        return record

    async def extract_abstract(self) -> str:
        html, current_url = await self._content()
        return self.parse_article_html(html, source_url=current_url).abstract

    async def check_fulltext_access(self) -> AccessDecision:
        if self._detail_record is None and self._expected_record is not None:
            await self.extract_metadata(search_query=self._expected_record.search_query)
        if self._detail_record is None or not self._detail_record.target_identity_confirmed:
            raise SourceLayoutChanged("Springer target identity must be locked before access checking")
        html, current_url = await self._content()
        if self._gateway_trusted():
            return self._check_gateway_fulltext_access_html(html, source_url=current_url)
        return self.check_fulltext_access_html(html, source_url=current_url)

    async def download_fulltext(self, record: LiteratureRecord, access: AccessDecision) -> Path:
        if not access.full_text_accessible or not access.authorized_access:
            raise PermissionError("FULLTEXT_NOT_AUTHORIZED")
        parsed = urlsplit(access.download_url)
        page = getattr(self.browser, "page", None)
        downloads_dir = getattr(self.browser, "downloads_dir", None)
        if page is None or downloads_dir is None:
            raise SourceUnavailable("Browser download context is unavailable")
        if self._gateway_trusted():
            if not record.target_identity_confirmed:
                raise SourceLayoutChanged("Springer target identity is not confirmed")
            if not self._gateway_url_is_bound(access.download_url) or not _PDF_PATH.search(parsed.path):
                raise SourceLayoutChanged("Institutional PDF URL is outside the bound Springer gateway")
            locator = page.locator(access.download_locator).filter(
                has_text=re.compile(r"^\s*download\s+pdf\s*$", re.IGNORECASE)
            ).first
            capture = BrowserAuthorizedFileCapture(
                Path(downloads_dir),
                allow_outside_output_for_tests=self._allow_capture_outside_output_for_tests,
            )
            try:
                result = await capture.capture_pdf(
                    page=page,
                    official_action=locator.click,
                    trusted_hosts=(self.hunnu_gateway_host,),
                    response_url_is_expected=lambda _: False,
                    controlled_filename=f"{record.paper_id}.pdf",
                    provenance_host=self.hunnu_gateway_host,
                    source_route="HUNNU_GATEWAY_TO_SPRINGER",
                    timeout_ms=self._capture_timeout_ms,
                    require_download_event=True,
                )
            except AuthorizedFileCaptureUnavailable as exc:
                raise SourceUnavailable(str(exc)) from exc
            if result.acquisition_method != AcquisitionMethod.PLAYWRIGHT_DOWNLOAD_EVENT:
                raise SourceUnavailable("Institutional Springer acquisition emitted no download event")
            self._last_capture = result
            record.acquisition_method = result.acquisition_method.value
            record.source_host = result.source_host
            record.source_route = result.source_route
            record.institutional_route_used = True
            record.institution = "湖南师范大学"
            record.download_event_emitted = result.download_event_emitted
            record.authorized_pdf_response_captured = False
            record.official_pdf_action_confirmed = True
            record.source_access_status = "InstitutionalAuthenticated"
            record.unattended_download_attempted = True
            record.automatic_download_initiation = True
            record.automatic_download_detection = True
            record.user_native_viewer_click_required = False
            record.manual_download_handoff_used = False
            record.human_download_action = False
            record.download_initiation_mode = "HARNESS_OFFICIAL_PDF_ACTION_CLICK"
            record.file_finalization_mode = "HARNESS_AUTOMATIC"
            identity = self.validate_pdf_identity(result.path, record)
            record.target_title_matched = identity.title_matched
            record.target_doi_matched = identity.doi_matched
            record.target_identity_confirmed = identity.confirmed
            if not identity.confirmed:
                quarantined = self._quarantine(result.path)
                raise SourceLayoutChanged(
                    "Captured Springer PDF failed local target identity validation; "
                    f"Quarantine=true; QuarantineFile={quarantined.name}"
                )
            return result.path
        if parsed.hostname != "link.springer.com" or not _PDF_PATH.search(parsed.path):
            raise SourceLayoutChanged("Download URL is not a stable official Springer PDF path")
        try:
            response = await page.context.request.get(access.download_url, timeout=45_000)
            if not response.ok:
                raise SourceUnavailable(f"Official Springer PDF returned HTTP {response.status}")
            payload = await response.body()
            target = Path(downloads_dir) / (Path(parsed.path).name or f"{record.paper_id}.pdf")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return target
        except SourceUnavailable:
            raise
        except Exception as exc:
            raise SourceUnavailable(
                f"Authorized Springer PDF request failed: {type(exc).__name__}"
            ) from exc

    async def get_citation(self) -> dict[str, Any]:
        record = await self.extract_metadata(search_query=UNKNOWN)
        return {
            "Title": record.title,
            "Authors": list(record.authors),
            "Journal": record.journal,
            "Year": record.year,
            "DOI": record.doi,
        }


__all__ = ["SpringerLinkAdapter", "SpringerPDFIdentityResult"]
