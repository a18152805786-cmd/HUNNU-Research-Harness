from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

from ...browser.commands import BrowserTarget, DownloadCommand, NavigateCommand, ObserveCommand
from .base import LiteratureSourceAdapter, SourceActionRequired, SourceLayoutChanged, SourceUnavailable
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
from ..security import sanitize_url


_ARTICLE_PATH = re.compile(r"/science/article/(?:abs/)?pii/([A-Za-z0-9]+)")
_PDF_PATH_MARKERS = ("/pdfft", "/pdf", ".pdf")


@dataclass(frozen=True)
class _Anchor:
    href: str
    text: str
    attributes: dict[str, str]


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
        elif lowered == "a":
            self._anchor_stack.append((attributes.get("href", ""), attributes, []))
        elif lowered == "script" and "ld+json" in attributes.get("type", "").casefold():
            self._json_ld_depth += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "a" and self._anchor_stack:
            href, attributes, parts = self._anchor_stack.pop()
            self.anchors.append(_Anchor(href=href, text=" ".join(parts).strip(), attributes=attributes))
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


def _json_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name", "")).strip()
    return str(value).strip()


class ScienceDirectAdapter(LiteratureSourceAdapter):
    name = "ScienceDirect"
    supports_unattended_download = True
    supports_preflight = True
    search_origin = "https://www.sciencedirect.com"

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
    def detect_interruption(cls, html: str, *, url: str = "") -> None:
        parser = cls._parser(html)
        haystack = f"{url} {_meta_first(parser, 'title', 'og:title')} {parser.body_text}".casefold()
        captcha_markers = ("captcha", "recaptcha", "verify you are human", "security challenge", "滑块", "验证码")
        if any(marker in haystack for marker in captcha_markers):
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=CAPTCHA or human verification required; BrowserReadyForManualAction=true"
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
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=School or database login required; BrowserReadyForManualAction=true"
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
            match = _ARTICLE_PATH.search(anchor.href)
            title = re.sub(r"\s+", " ", anchor.text).strip()
            if not match or not title:
                continue
            stable_identifier = match.group(1)
            if stable_identifier in seen:
                continue
            seen.add(stable_identifier)
            page = urljoin(cls.search_origin, anchor.href)
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

        title = _meta_first(parser, "citation_title", "dc.title", "og:title")
        if title == UNKNOWN:
            title = str(article_json.get("headline", article_json.get("name", UNKNOWN))).strip() or UNKNOWN
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
    def check_fulltext_access_html(cls, html: str, *, source_url: str) -> AccessDecision:
        cls.detect_interruption(html, url=source_url)
        parser = cls._parser(html)
        article_host = urlsplit(source_url).hostname or ""
        candidate: _Anchor | None = None
        candidate_url = UNKNOWN
        for anchor in parser.anchors:
            absolute = urljoin(source_url, anchor.href)
            parsed = urlsplit(absolute)
            text = f"{anchor.text} {anchor.attributes.get('aria-label', '')} {anchor.attributes.get('title', '')}".casefold()
            has_pdf_path = any(marker in parsed.path.casefold() for marker in _PDF_PATH_MARKERS)
            has_pdf_label = "pdf" in text and any(action in text for action in ("view", "download", "查看", "下载"))
            same_source = parsed.hostname in {article_host, "www.sciencedirect.com", "sciencedirect.com"}
            if same_source and has_pdf_path and has_pdf_label:
                candidate = anchor
                candidate_url = absolute
                break

        if candidate is None:
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.METADATA_ONLY,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="No enabled, official PDF view/download control was present on the article page",
            )

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
        html, current_url = await self._content()
        results = self.parse_search_results_html(
            html,
            query=query,
            source_url=current_url,
            max_results=min(request.max_results_per_source, request.max_search_results),
        )
        if not results and "sciencedirect" not in current_url.casefold():
            raise SourceUnavailable("ScienceDirect search navigation did not reach the expected source")
        return results

    async def open_result(self, record: LiteratureRecord) -> None:
        parsed = urlsplit(record.source_page)
        if parsed.hostname not in {"www.sciencedirect.com", "sciencedirect.com"} or not _ARTICLE_PATH.search(parsed.path):
            raise SourceLayoutChanged("Result URL is not a stable ScienceDirect article page")
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        await self.browser.execute(NavigateCommand(record.source_page))

    async def extract_metadata(self, *, search_query: str) -> LiteratureRecord:
        html, current_url = await self._content()
        return self.parse_article_html(html, source_url=current_url, search_query=search_query)

    async def extract_abstract(self) -> str:
        html, current_url = await self._content()
        return self.parse_article_html(html, source_url=current_url).abstract

    async def check_fulltext_access(self) -> AccessDecision:
        html, current_url = await self._content()
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
        try:
            artifact = await self.browser.execute(
                DownloadCommand(
                    target=BrowserTarget(
                        css=download_target,
                    ),
                    suggested_filename=f"{record.paper_id}.pdf",
                )
            )
            return artifact.local_path
        except Exception as exc:
            raise SourceUnavailable(f"Authorized PDF control did not produce a browser download: {type(exc).__name__}") from exc

    async def get_citation(self) -> dict[str, Any]:
        record = await self.extract_metadata(search_query=UNKNOWN)
        return {
            "Title": record.title,
            "Authors": list(record.authors),
            "Journal": record.journal,
            "Year": record.year,
            "DOI": record.doi,
        }
