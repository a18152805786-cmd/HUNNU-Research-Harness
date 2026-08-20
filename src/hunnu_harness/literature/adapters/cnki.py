from __future__ import annotations

import html as html_lib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote_plus, urljoin, urlsplit

from ...browser.commands import (
    BrowserTarget,
    ClickCommand,
    DownloadCommand,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
)
from .base import LiteratureSourceAdapter, SourceActionRequired, SourceLayoutChanged, SourceUnavailable
from .sciencedirect import _Anchor, _ScienceDirectHTMLParser, _meta_all, _meta_first
from ..cnki_challenge import CNKIChallengeDetector, ChallengeDiagnostic, ChallengeState
from ..models import (
    AccessDecision,
    AccessType,
    FullTextFormat,
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
    UNKNOWN,
)
from ..normalization import normalize_doi, normalize_title, stable_paper_id
from ..security import sanitize_url


_DETAIL_PATH_MARKERS = ("/article/abstract", "/detail/detail.aspx", "/kcms/detail/")
_REJECT_DOWNLOAD_LABELS = ("批量下载", "多篇下载", "相关推荐", "参考文献下载", "整本下载")
_DOWNLOAD_ACTIONS = ("pdf下载", "caj下载", "全文下载", "下载全文", "download pdf", "download caj")
_INSTITUTIONAL_ACCESS_MARKERS = (
    "当前机构已获得全文访问权限",
    "机构已获得全文访问权限",
    "institutional access",
    "湖南师范大学",
    "hunan normal university",
)
_CNKI_DASH_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d"
_CNKI_DASH_SPACE_RE = re.compile(rf"\s*([{_CNKI_DASH_CHARS}])\s*")


def _decode_cnki_form_query_value(value: str) -> str:
    """Decode a CNKI URL/form value before it enters bibliographic logic.

    ``+`` is a space only at this explicit ``application/x-www-form-urlencoded``
    boundary. This helper must not be applied to a title read from the page.
    """

    return html_lib.unescape(unquote_plus(str(value)))


def _canonicalize_cnki_exact_query(value: str) -> str:
    """Canonicalize plain text used to build a CNKI exact-title query.

    This is deliberately a query-input operation, not a URL decoder. It
    removes only nonsemantic whitespace adjacent to the dash forms observed
    in CNKI titles, so ``quote_plus`` cannot turn that layout difference into
    a literal ``+`` in CNKI's search box.
    """

    text = html_lib.unescape(unicodedata.normalize("NFKC", str(value)))
    text = re.sub(r"\s+", " ", text).strip()
    return _CNKI_DASH_SPACE_RE.sub(r"\1", text)


def _canonicalize_cnki_title_identity(value: str) -> str:
    """Normalize a page-observed bibliographic title without fuzzy matching.

    A literal ``+`` remains a literal ``+`` here. Query/form decoding must
    happen in ``_decode_cnki_form_query_value`` before this helper is called
    when the input is known to be encoded query data.
    """

    text = html_lib.unescape(unicodedata.normalize("NFKC", str(value)))
    text = re.sub(r"\s+", " ", text).strip()
    text = _CNKI_DASH_SPACE_RE.sub(r"\1", text)
    return text.casefold()


def _cnki_known(value: str) -> bool:
    return bool(value and value != UNKNOWN)


def _cnki_authors_identity(authors: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_canonicalize_cnki_title_identity(author) for author in authors if _cnki_known(author))


def _is_cnki_host(hostname: str | None) -> bool:
    host = (hostname or "").casefold().rstrip(".")
    return host == "cnki.net" or host.endswith(".cnki.net")


def _stable_identifier(value: str) -> str:
    parsed = urlsplit(value)
    query = parse_qs(parsed.query)
    filename = next((values[0] for key, values in query.items() if key.casefold() == "filename" and values), "")
    dbcode = next((values[0] for key, values in query.items() if key.casefold() == "dbcode" and values), "")
    if filename:
        return f"{dbcode.casefold()}:{filename}" if dbcode else filename
    match = re.search(r"/(?:article/abstract|detail)/([^/?#]+)", parsed.path, re.IGNORECASE)
    return match.group(1) if match else UNKNOWN


def _text_field(text: str, *labels: str) -> str:
    label_group = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:^|\s)(?:{label_group})\s*[：:]\s*(.+?)(?=\s(?:作者|来源|期刊|摘要|关键词|DOI|ISSN|卷|期|页码|出版日期|网络首发)\s*[：:]|$)",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else UNKNOWN


def _split_people(value: str) -> tuple[str, ...]:
    if value == UNKNOWN:
        return ()
    return tuple(item.strip() for item in re.split(r"[;,；，、\s]+", value) if item.strip())


def _split_keywords(value: str) -> tuple[str, ...]:
    if value == UNKNOWN:
        return ()
    return tuple(dict.fromkeys(item.strip() for item in re.split(r"[;,；，]+", value) if item.strip()))


def _snapshot_unquote(value: str | None) -> str:
    if value is None:
        return ""
    try:
        return str(json.loads(f'"{value}"'))
    except (TypeError, json.JSONDecodeError):
        return value.replace(r'\"', '"').replace(r"\\", "\\")


def _snapshot_links(snapshot: str) -> list[tuple[str, str, int, str]]:
    """Return labelled links from an MCP accessibility snapshot.

    Each tuple contains ``(label, url, line_index, link_line)``.  Unlabelled
    author links are resolved from the nearby ``text:`` child without exposing
    or evaluating the live DOM.
    """

    link_pattern = re.compile(
        r'^\s*-\s*link(?:\s+"(?P<label>(?:\\.|[^"])*)")?'
        r'(?:\s+\[[^\]]+\])*\s*:\s*$'
    )
    url_pattern = re.compile(r"^\s*-\s*/url:\s*(?P<url>\S.*?)\s*$")
    text_pattern = re.compile(r"^\s*-\s*text:\s*(?P<text>.+?)\s*$")
    lines = snapshot.splitlines()
    links: list[tuple[str, str, int, str]] = []
    for index, line in enumerate(lines):
        match = link_pattern.match(line)
        if not match:
            continue
        url = ""
        url_index = index
        for candidate_index in range(index + 1, min(index + 4, len(lines))):
            url_match = url_pattern.match(lines[candidate_index])
            if url_match:
                url = url_match.group("url").strip()
                url_index = candidate_index
                break
        if not url:
            continue
        label = _snapshot_unquote(match.group("label")).strip()
        if not label:
            for candidate_index in range(url_index + 1, min(url_index + 5, len(lines))):
                text_match = text_pattern.match(lines[candidate_index])
                if text_match:
                    label = text_match.group("text").strip().strip('"')
                    break
        links.append((re.sub(r"\s+", " ", label).strip(), url, index, line))
    return links


def _snapshot_node_text(line: str) -> str:
    text_match = re.match(r"^\s*-\s*text:\s*(.+?)\s*$", line)
    if text_match:
        return text_match.group(1).strip().strip('"')
    suffix_match = re.search(r"\](?::\s*(.+?))\s*$", line)
    if suffix_match:
        return suffix_match.group(1).strip().strip('"')
    labelled_match = re.match(
        r'^\s*-\s*(?:generic|paragraph|heading|cell)\s+"(?P<label>(?:\\.|[^"])*)"',
        line,
    )
    return _snapshot_unquote(labelled_match.group("label")).strip() if labelled_match else ""


class _CNKIHTMLParser(_ScienceDirectHTMLParser):
    """Capture CNKI's stable semantic blocks in addition to citation metadata."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: dict[str, list[str]] = defaultdict(list)
        self.author_links: list[str] = []
        self.institution_login_status = False
        self.authenticated_institution_labels: list[str] = []
        self._capture_stack: list[tuple[str, str, list[str]]] = []
        self._hidden_element_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        attributes = {key.casefold(): (value or "") for key, value in attrs}
        lowered = tag.casefold()
        classes = set(attributes.get("class", "").casefold().split())
        style = re.sub(r"\s+", "", attributes.get("style", "").casefold())
        if (
            "hidden" in attributes
            or attributes.get("aria-hidden", "").casefold() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            self._hidden_element_tags.append(lowered)
        if "ecp_header_login_status1" in classes:
            self.institution_login_status = True
        if "ecp_header_unitname" in classes:
            label = re.sub(r"\s+", " ", attributes.get("title", "")).strip()
            if label and "display:none" not in style and "visibility:hidden" not in style:
                self.authenticated_institution_labels.append(label)
        key = ""
        if lowered == "h1":
            key = "title"
        elif lowered == "h3" and attributes.get("id", "").casefold() == "authorpart":
            key = "authors"
        elif attributes.get("id", "").casefold() == "chdivsummary":
            key = "abstract"
        elif "keywords" in classes:
            key = "keywords"
        elif "top-tip" in classes:
            key = "source"
        if key:
            self._capture_stack.append((lowered, key, []))

    def handle_data(self, data: str) -> None:
        super().handle_data(data)
        if self._hidden_element_tags:
            return
        for _, _, parts in self._capture_stack:
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "a" and self._capture_stack and self._anchor_stack:
            if any(key == "authors" for _, key, _ in self._capture_stack):
                author = re.sub(r"\s+", " ", " ".join(self._anchor_stack[-1][2])).strip()
                if author:
                    self.author_links.append(author)
        super().handle_endtag(tag)
        for index in range(len(self._hidden_element_tags) - 1, -1, -1):
            if self._hidden_element_tags[index] == lowered:
                del self._hidden_element_tags[index]
                break
        if self._capture_stack and self._capture_stack[-1][0] == lowered:
            _, key, parts = self._capture_stack.pop()
            value = re.sub(r"\s+", " ", " ".join(parts)).strip()
            if value:
                self.blocks[key].append(value)

    def first_block(self, key: str) -> str:
        values = self.blocks.get(key, [])
        return values[0] if values else UNKNOWN


class CNKIAdapter(LiteratureSourceAdapter):
    """Bounded CNKI adapter for manually authenticated, normally authorized access."""

    name = "CNKI"
    supports_unattended_download = True
    supports_preflight = True
    search_origin = "https://kns.cnki.net"

    def __init__(self, browser: Any):
        super().__init__(browser)
        self._expected_record: LiteratureRecord | None = None
        self._detail_record: LiteratureRecord | None = None
        self._last_challenge_diagnostic: ChallengeDiagnostic | None = None

    @staticmethod
    def _parser(html: str) -> _CNKIHTMLParser:
        parser = _CNKIHTMLParser()
        parser.feed(html)
        return parser

    @classmethod
    def detect_interruption(cls, html: str, *, url: str = "") -> None:
        parser = cls._parser(html)
        haystack = f"{url} {_meta_first(parser, 'title', 'og:title')} {parser.body_text}".casefold()
        challenge_markers = ("captcha", "验证码", "滑块", "安全验证", "人机验证", "访问过于频繁")
        challenge_node_detected = any(marker in haystack for marker in challenge_markers)
        has_metadata = _meta_first(parser, "citation_title", "dc.title") != UNKNOWN
        normal_business_evidence = has_metadata or any(
            marker in parser.body_text.casefold()
            for marker in (
                "中文文献",
                "外文文献",
                "主题检索",
                "检索结果",
                "结果中检索",
                "学术期刊",
                "摘要",
                "关键词",
            )
        ) or any(
            _is_cnki_host(urlsplit(urljoin(url or cls.search_origin, anchor.href)).hostname)
            and any(path_marker in urlsplit(urljoin(url or cls.search_origin, anchor.href)).path.casefold() for path_marker in _DETAIL_PATH_MARKERS)
            for anchor in parser.anchors
        )
        page_identity_is_challenge = any(
            marker in _meta_first(parser, "title", "og:title").casefold()
            for marker in challenge_markers
        )
        if challenge_node_detected and (page_identity_is_challenge or not normal_business_evidence):
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=CNKI CAPTCHA or security verification required; "
                "BrowserReadyForManualAction=true"
            )
        login_url = any(marker in url.casefold() for marker in ("/login", "passport.cnki", "cas.", "carsi", "webvpn"))
        institution_authenticated = (
            parser.institution_login_status
            and bool(parser.authenticated_institution_labels)
            and normal_business_evidence
            and not login_url
        )
        if institution_authenticated:
            return
        blocking_login = any(
            marker in haystack
            for marker in ("统一身份认证", "请登录", "登录已失效", "重新登录", "短信验证", "二次认证")
        )
        if (login_url or blocking_login) and not has_metadata:
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=HUNNU/CNKI manual authentication required; "
                "BrowserReadyForManualAction=true"
            )

    @property
    def last_challenge_diagnostic(self) -> ChallengeDiagnostic | None:
        return self._last_challenge_diagnostic

    @staticmethod
    def enforce_challenge_diagnostic(diagnostic: ChallengeDiagnostic) -> None:
        if not diagnostic.target_page_confirmed:
            raise SourceLayoutChanged(
                "TargetPageIdentity=uncertain; CNKI automation stopped before any business action"
            )
        if diagnostic.state in {ChallengeState.VISIBLE, ChallengeState.BLOCKING, ChallengeState.UNCERTAIN}:
            detected = "true" if diagnostic.captcha_detected else "uncertain"
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; "
                f"Reason=CNKI challenge state is {diagnostic.state.value}; "
                "BrowserReadyForManualAction=true; "
                f"CaptchaDetected={detected}; CaptchaBypassAttempted=false"
            )

    async def _inspect_live_challenge(self, observation: Any) -> ChallengeDiagnostic:
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        provenance = getattr(self.browser, "navigation_provenance", ())
        if isinstance(provenance, str):
            provenance = (provenance,)
        diagnostic = CNKIChallengeDetector.inspect_observation(
            observation,
            route_provenance=tuple(provenance or ()),
        )
        self._last_challenge_diagnostic = diagnostic
        self.enforce_challenge_diagnostic(diagnostic)
        return diagnostic

    @classmethod
    def build_search_url(cls, query: str, *, mode: str = "keyword") -> str:
        order = {"exact_title": "TI", "title": "TI", "author": "AU", "keyword": "SU"}.get(mode, "SU")
        query_value = _canonicalize_cnki_exact_query(query) if mode in {"exact_title", "title"} else query
        return f"{cls.search_origin}/kns8s/defaultresult/index?korder={order}&kw={quote_plus(query_value)}"

    @classmethod
    def parse_search_results_html(
        cls,
        html: str,
        *,
        query: str,
        source_url: str = "https://kns.cnki.net/kns8s/defaultresult/index",
        max_results: int = 30,
    ) -> list[LiteratureRecord]:
        cls.detect_interruption(html, url=source_url)
        parser = cls._parser(html)
        records: list[LiteratureRecord] = []
        seen: set[str] = set()
        for anchor in parser.anchors:
            absolute = urljoin(source_url, anchor.href)
            parsed = urlsplit(absolute)
            if not _is_cnki_host(parsed.hostname) or not any(marker in parsed.path.casefold() for marker in _DETAIL_PATH_MARKERS):
                continue
            title = re.sub(r"\s+", " ", anchor.text).strip()
            if not title or any(label in title for label in _REJECT_DOWNLOAD_LABELS):
                continue
            stable_identifier = _stable_identifier(absolute)
            identity = stable_identifier if stable_identifier != UNKNOWN else normalize_title(title)
            if identity in seen:
                continue
            seen.add(identity)
            records.append(
                LiteratureRecord(
                    paper_id=stable_paper_id(title=title),
                    title=title,
                    language="zh" if re.search(r"[\u3400-\u9fff]", title) else UNKNOWN,
                    source_database=cls.name,
                    source_page=sanitize_url(absolute),
                    navigation_url=absolute,
                    stable_identifier=stable_identifier,
                    search_query=query,
                    canonical_paper_id=stable_paper_id(title=title),
                )
            )
            if len(records) >= max_results:
                break
        return records

    @classmethod
    def parse_search_results_snapshot(
        cls,
        snapshot: str,
        *,
        query: str,
        source_url: str = "https://kns.cnki.net/kns8s/defaultresult/index",
        max_results: int = 30,
    ) -> list[LiteratureRecord]:
        """Parse bounded CNKI result links from an MCP accessibility snapshot."""

        records: list[LiteratureRecord] = []
        seen: set[str] = set()
        lines = snapshot.splitlines()
        title_headers = tuple(
            index
            for index, line in enumerate(lines)
            if re.search(r'^\s*-\s*columnheader\s+"题名"', line)
        )
        if not title_headers:
            return records
        result_start = title_headers[-1]
        result_end = next(
            (
                index
                for index in range(result_start + 1, len(lines))
                if re.match(r"^\s*-\s*contentinfo\b", lines[index])
            ),
            len(lines),
        )
        for title, href, line_index, _link_line in _snapshot_links(snapshot):
            if not (result_start < line_index < result_end):
                continue
            absolute = urljoin(source_url, href)
            parsed = urlsplit(absolute)
            if not _is_cnki_host(parsed.hostname) or not any(
                marker in parsed.path.casefold() for marker in _DETAIL_PATH_MARKERS
            ):
                continue
            title = re.sub(r"\s+", " ", title).strip()
            if (
                not title
                or title.isdigit()
                or any(label in title for label in _REJECT_DOWNLOAD_LABELS)
                or "anchor=citnet" in parsed.query.casefold()
            ):
                continue
            stable_identifier = _stable_identifier(absolute)
            identity = stable_identifier if stable_identifier != UNKNOWN else normalize_title(title)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            paper_id = stable_paper_id(title=title)
            records.append(
                LiteratureRecord(
                    paper_id=paper_id,
                    title=title,
                    language="zh" if re.search(r"[\u3400-\u9fff]", title) else UNKNOWN,
                    source_database=cls.name,
                    source_page=sanitize_url(absolute),
                    navigation_url=absolute,
                    stable_identifier=stable_identifier,
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
        body = re.sub(r"\s+", " ", parser.body_text).strip()
        title = _meta_first(parser, "citation_title", "dc.title", "og:title")
        if title == UNKNOWN:
            title_candidates = [value for value in parser.blocks.get("title", []) if value not in {"自动登录", "找回密码"}]
            title = title_candidates[-1] if title_candidates else UNKNOWN
        authors = _meta_all(parser, "citation_author", "dc.creator")
        if not authors and parser.author_links:
            authors = tuple(dict.fromkeys(parser.author_links))
        if not authors:
            raw_authors = parser.first_block("authors")
            numbered = tuple(item for item in re.split(r"\d+", raw_authors) if item)
            authors = numbered or _split_people(_text_field(body, "作者"))
        date = _meta_first(parser, "citation_publication_date", "citation_date", "dc.date")
        if date == UNKNOWN:
            date = _text_field(body, "出版日期", "发表时间", "网络首发")
        year_match = re.search(r"(?:18|19|20|21)\d{2}", date)
        year = year_match.group(0) if year_match else UNKNOWN
        journal = _meta_first(parser, "citation_journal_title", "prism.publicationname")
        if journal == UNKNOWN:
            journal = _text_field(body, "来源", "期刊")
        source_line = parser.first_block("source")
        source_match = re.search(
            r"^(.+?)\s*\.\s*((?:18|19|20|21)\d{2})\s*\(([^)]+)\)\s*:\s*([0-9]+(?:\s*[-–—]\s*[0-9]+)?)",
            source_line,
        )
        if source_match:
            journal = source_match.group(1).strip() or journal
            if date == UNKNOWN:
                date = source_match.group(2)
            if year == UNKNOWN:
                year = source_match.group(2)
        doi = normalize_doi(_meta_first(parser, "citation_doi", "dc.identifier", "prism.doi"))
        if doi == UNKNOWN:
            doi = normalize_doi(_text_field(body, "DOI"))
        volume = _meta_first(parser, "citation_volume", "prism.volume")
        if volume == UNKNOWN:
            volume = _text_field(body, "卷")
        issue = _meta_first(parser, "citation_issue", "prism.number")
        if issue == UNKNOWN:
            issue = _text_field(body, "期")
        first_page = _meta_first(parser, "citation_firstpage", "prism.startingpage")
        last_page = _meta_first(parser, "citation_lastpage", "prism.endingpage")
        pages = f"{first_page}-{last_page}" if first_page != UNKNOWN and last_page not in (UNKNOWN, first_page) else first_page
        if source_match:
            if issue == UNKNOWN:
                issue = source_match.group(3).strip()
            if pages == UNKNOWN:
                pages = re.sub(r"\s+", "", source_match.group(4)).replace("–", "-").replace("—", "-")
        if pages == UNKNOWN:
            pages = _text_field(body, "页码", "页")
        abstract = _meta_first(parser, "citation_abstract", "description", "dc.description")
        if abstract == UNKNOWN:
            abstract = parser.first_block("abstract")
        if abstract == UNKNOWN:
            abstract = _text_field(body, "摘要")
        keyword_values = _meta_all(parser, "citation_keywords", "keywords", "dc.subject")
        keywords = _split_keywords(";".join(keyword_values)) if keyword_values else _split_keywords(parser.first_block("keywords"))
        if not keywords:
            keywords = _split_keywords(_text_field(body, "关键词"))
        issn = _meta_first(parser, "citation_issn", "prism.issn")
        if issn == UNKNOWN:
            issn = _text_field(body, "ISSN")
        language = _meta_first(parser, "citation_language", "dc.language")
        if language == UNKNOWN:
            language = "zh" if re.search(r"[\u3400-\u9fff]", f"{title}{abstract}") else UNKNOWN
        stable_identifier = _stable_identifier(source_url)
        if stable_identifier == UNKNOWN:
            for anchor in parser.anchors:
                stable_identifier = _stable_identifier(urljoin(source_url, anchor.href))
                if stable_identifier != UNKNOWN:
                    break
        publication_type, publication_status = cls._publication_classification(body, journal)
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
            keywords=keywords,
            source_database=cls.name,
            source_page=sanitize_url(source_url),
            stable_identifier=stable_identifier,
            search_query=search_query,
            canonical_paper_id=paper_id,
        )

    @classmethod
    def parse_article_snapshot(
        cls,
        snapshot: str,
        *,
        source_url: str,
        search_query: str = UNKNOWN,
    ) -> LiteratureRecord:
        """Extract CNKI article metadata from the structured MCP snapshot."""

        lines = snapshot.splitlines()
        title = UNKNOWN
        title_index = -1
        title_pattern = re.compile(
            r'^\s*-\s*heading\s+"(?P<title>(?:\\.|[^"])*)"'
            r'(?:\s+\[[^\]]+\])*\s*\[level=1\]'
        )
        for index, line in enumerate(lines):
            match = title_pattern.match(line)
            if match:
                title = re.sub(r"\s+", " ", _snapshot_unquote(match.group("title"))).strip()
                title_index = index
                break
        if title == UNKNOWN:
            raise SourceLayoutChanged("CNKI structured article snapshot has no level-1 target title")

        links = _snapshot_links(snapshot)
        abstract_marker = next((i for i, line in enumerate(lines) if "摘要：" in line or "摘要:" in line), len(lines))
        keyword_marker = next(
            (i for i, line in enumerate(lines) if i >= abstract_marker and ("关键词：" in line or "关键词:" in line)),
            len(lines),
        )

        authors: list[str] = []
        keywords: list[str] = []
        journal = UNKNOWN
        for label, href, line_index, _link_line in links:
            parsed = urlsplit(urljoin(source_url, href))
            path = parsed.path.casefold()
            if "/author/detail" in path and title_index < line_index < abstract_marker and label:
                clean = re.sub(r"\d+$", "", label).strip()
                if clean and clean not in authors:
                    authors.append(clean)
            elif "/keyword/detail" in path and label:
                clean = label.strip().rstrip(";；,， ")
                if clean and clean not in keywords:
                    keywords.append(clean)
            elif journal == UNKNOWN and line_index < title_index and parsed.hostname and parsed.hostname.casefold().endswith("cnki.net"):
                if "/knavi/detail" in path and label and "数据库收录" not in label:
                    journal = label.rstrip(" .。·").strip()

        abstract_parts: list[str] = []
        if abstract_marker < len(lines):
            marker_line = _snapshot_node_text(lines[abstract_marker])
            inline = re.sub(r"^.*?摘要\s*[：:]\s*", "", marker_line).strip()
            if inline and inline != marker_line:
                abstract_parts.append(inline)
            for line in lines[abstract_marker + 1 : keyword_marker]:
                value = _snapshot_node_text(line)
                if value and value not in {"摘要", "摘要：", "摘要:"}:
                    abstract_parts.append(value)
        abstract = re.sub(r"\s+", " ", " ".join(abstract_parts)).strip() or UNKNOWN

        header_start = max(0, title_index - 40)
        header_end = min(len(lines), abstract_marker)
        header_text = re.sub(
            r"\s+",
            " ",
            " ".join(filter(None, (_snapshot_node_text(line) for line in lines[header_start:header_end]))),
        ).strip()
        body_text = re.sub(
            r"\s+",
            " ",
            " ".join(filter(None, (_snapshot_node_text(line) for line in lines))),
        ).strip()

        date_match = re.search(
            r"(?:网络首发时间|出版日期|发表时间)\s*[：:]\s*((?:18|19|20|21)\d{2}(?:[-/.]\d{1,2})?(?:[-/.]\d{1,2})?)",
            header_text,
        )
        source_match = re.search(
            r"((?:18|19|20|21)\d{2})\s*\(([^)]+)\)\s*[：:]\s*([0-9]+(?:\s*[-–—]\s*[0-9]+)?)",
            header_text,
        )
        date = date_match.group(1) if date_match else (source_match.group(1) if source_match else UNKNOWN)
        year_match = re.search(r"(?:18|19|20|21)\d{2}", date)
        year = year_match.group(0) if year_match else UNKNOWN
        issue = source_match.group(2).strip() if source_match else UNKNOWN
        pages = (
            re.sub(r"\s+", "", source_match.group(3)).replace("–", "-").replace("—", "-")
            if source_match
            else UNKNOWN
        )
        volume_match = re.search(r"(?:第\s*)?(\d+)\s*卷", header_text)
        volume = volume_match.group(1) if volume_match else UNKNOWN

        doi_match = re.search(r"\b10\.\d{4,9}/[^\s<>\]\[\"'，；;]+", body_text, flags=re.IGNORECASE)
        doi = normalize_doi(doi_match.group(0).rstrip(".。)）")) if doi_match else UNKNOWN
        issn_match = re.search(r"\b\d{4}-\d{3}[\dXx]\b", body_text)
        issn = issn_match.group(0).upper() if issn_match else UNKNOWN
        language = "zh" if re.search(r"[\u3400-\u9fff]", f"{title}{abstract}") else UNKNOWN
        stable_identifier = _stable_identifier(source_url)
        publication_type, publication_status = cls._publication_classification(header_text, journal)
        if "网络首发" in header_text:
            publication_type = "JournalArticle"
            publication_status = PublicationStatus.ONLINE_FIRST.value
        paper_id = stable_paper_id(doi=doi, title=title, year=year, authors=authors)
        return LiteratureRecord(
            paper_id=paper_id,
            title=title,
            authors=tuple(authors),
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
            keywords=tuple(keywords),
            source_database=cls.name,
            source_page=sanitize_url(source_url),
            stable_identifier=stable_identifier,
            search_query=search_query,
            canonical_paper_id=paper_id,
        )

    @staticmethod
    def _publication_classification(body: str, journal: str) -> tuple[str, str]:
        if "学位论文" in body:
            return "Dissertation", PublicationStatus.OTHER.value
        if "会议论文" in body:
            return "ConferencePaper", PublicationStatus.CONFERENCE_PAPER.value
        if "报纸" in body:
            return "Newspaper", PublicationStatus.OTHER.value
        if journal != UNKNOWN or "期刊" in body:
            status = (
                PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value
                if any(marker in body for marker in ("同行评议", "peer reviewed", "peer-reviewed"))
                else PublicationStatus.UNKNOWN.value
            )
            return "JournalArticle", status
        return UNKNOWN, PublicationStatus.UNKNOWN.value

    @classmethod
    def check_fulltext_access_html(cls, html: str, *, source_url: str) -> AccessDecision:
        cls.detect_interruption(html, url=source_url)
        parser = cls._parser(html)
        candidate: _Anchor | None = None
        candidate_url = UNKNOWN
        candidate_format = FullTextFormat.UNKNOWN
        for anchor in parser.anchors:
            label = re.sub(
                r"\s+",
                "",
                f"{anchor.text} {anchor.attributes.get('aria-label', '')} {anchor.attributes.get('title', '')}",
            ).casefold()
            if any(re.sub(r"\s+", "", marker).casefold() in label for marker in _REJECT_DOWNLOAD_LABELS):
                continue
            if not any(action in label for action in _DOWNLOAD_ACTIONS):
                continue
            disabled = "disabled" in anchor.attributes or anchor.attributes.get("aria-disabled", "").casefold() == "true"
            if disabled:
                continue
            absolute = urljoin(source_url, anchor.href) if anchor.href else UNKNOWN
            if absolute != UNKNOWN and not absolute.casefold().startswith("javascript:"):
                if not _is_cnki_host(urlsplit(absolute).hostname):
                    continue
                candidate_url = absolute
            lower = f"{label} {anchor.href}".casefold()
            candidate_format = FullTextFormat.PDF if "pdf" in lower else (
                FullTextFormat.CAJ if any(marker in lower for marker in ("caj", ".nh", ".kdh")) else FullTextFormat.OTHER_AUTHORIZED_FORMAT
            )
            candidate = anchor
            break
        if candidate is None:
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.METADATA_ONLY,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="No enabled, official single-paper CNKI full-text control was present",
                full_text_format=FullTextFormat.UNKNOWN,
            )
        body = parser.body_text.casefold()
        open_access = any(marker in body for marker in ("开放获取", "open access", "public full text"))
        institutional_access = any(
            marker in body
            for marker in ("当前机构已获得全文访问权限", "机构已获得全文访问权限", "institutional access")
        )
        direct_public_file = candidate_url != UNKNOWN and urlsplit(candidate_url).path.casefold().endswith(
            (".pdf", ".caj", ".nh", ".kdh")
        )
        if not (open_access or institutional_access or direct_public_file):
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.METADATA_ONLY,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="CNKI exposed a download/order control but no verified institutional, open, or direct public full-text state",
                download_url=candidate_url,
                download_locator=re.sub(r"\s+", " ", candidate.text).strip(),
                full_text_format=candidate_format,
            )
        access_type = AccessType.INSTITUTIONAL_AUTHENTICATED if institutional_access else AccessType.PUBLIC_FULL_TEXT
        locator_label = re.sub(r"\s+", " ", candidate.text).strip()
        return AccessDecision(
            full_text_accessible=True,
            access_type=access_type,
            authorized_access=True,
            status=RunStatus.SUCCESS,
            reason=f"Official CNKI article page exposed an enabled single-paper {candidate_format.value} control",
            download_url=candidate_url,
            download_locator=locator_label,
            full_text_format=candidate_format,
        )

    @classmethod
    def check_fulltext_access_snapshot(cls, snapshot: str, *, source_url: str) -> AccessDecision:
        """Verify a single-paper CNKI full-text control in a structured snapshot."""

        candidates: list[tuple[int, str, str, FullTextFormat]] = []
        for label, href, _line_index, link_line in _snapshot_links(snapshot):
            compact = re.sub(r"\s+", "", label).casefold()
            if any(re.sub(r"\s+", "", marker).casefold() in compact for marker in _REJECT_DOWNLOAD_LABELS):
                continue
            if not any(action in compact for action in _DOWNLOAD_ACTIONS):
                continue
            if "disabled" in link_line.casefold() or "aria-disabled=true" in link_line.casefold():
                continue
            absolute = urljoin(source_url, href) if href else UNKNOWN
            if absolute != UNKNOWN and not absolute.casefold().startswith("javascript:"):
                if not _is_cnki_host(urlsplit(absolute).hostname):
                    continue
            lower = f"{compact} {href}".casefold()
            full_text_format = (
                FullTextFormat.PDF
                if "pdf" in lower
                else FullTextFormat.CAJ
                if any(marker in lower for marker in ("caj", ".nh", ".kdh"))
                else FullTextFormat.OTHER_AUTHORIZED_FORMAT
            )
            preference = {
                FullTextFormat.PDF: 0,
                FullTextFormat.CAJ: 1,
                FullTextFormat.OTHER_AUTHORIZED_FORMAT: 2,
            }[full_text_format]
            candidates.append((preference, label, absolute, full_text_format))
        if not candidates:
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.METADATA_ONLY,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="No enabled, official single-paper CNKI full-text control was present",
                full_text_format=FullTextFormat.UNKNOWN,
            )

        _preference, label, candidate_url, candidate_format = sorted(candidates, key=lambda item: item[0])[0]
        body = snapshot.casefold()
        open_access = any(marker in body for marker in ("开放获取", "open access", "public full text"))
        institutional_access = any(marker in body for marker in _INSTITUTIONAL_ACCESS_MARKERS)
        direct_public_file = candidate_url != UNKNOWN and urlsplit(candidate_url).path.casefold().endswith(
            (".pdf", ".caj", ".nh", ".kdh")
        )
        if not (open_access or institutional_access or direct_public_file):
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.METADATA_ONLY,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="CNKI exposed a download/order control but no verified institutional, open, or direct public full-text state",
                download_url=candidate_url,
                download_locator=label,
                full_text_format=candidate_format,
            )
        access_type = AccessType.INSTITUTIONAL_AUTHENTICATED if institutional_access else AccessType.PUBLIC_FULL_TEXT
        return AccessDecision(
            full_text_accessible=True,
            access_type=access_type,
            authorized_access=True,
            status=RunStatus.SUCCESS,
            reason=f"Official CNKI article page exposed an enabled single-paper {candidate_format.value} control",
            download_url=candidate_url,
            download_locator=label,
            full_text_format=candidate_format,
        )

    @staticmethod
    def identity_matches(search_record: LiteratureRecord, detail_record: LiteratureRecord) -> tuple[bool, str]:
        title_matches = _canonicalize_cnki_title_identity(search_record.title) == _canonicalize_cnki_title_identity(
            detail_record.title
        )
        if not title_matches:
            return False, "Target title mismatch"
        if search_record.authors and detail_record.authors:
            if _cnki_authors_identity(search_record.authors) != _cnki_authors_identity(detail_record.authors):
                return False, "Author mismatch"
        if _cnki_known(search_record.year) and _cnki_known(detail_record.year):
            if str(search_record.year) != str(detail_record.year):
                return False, "Year mismatch"
        if _cnki_known(search_record.journal) and _cnki_known(detail_record.journal):
            if _canonicalize_cnki_title_identity(search_record.journal) != _canonicalize_cnki_title_identity(
                detail_record.journal
            ):
                return False, "Journal mismatch"
        if search_record.doi != UNKNOWN and detail_record.doi != UNKNOWN:
            return (search_record.doi == detail_record.doi, "DOI exact match" if search_record.doi == detail_record.doi else "DOI mismatch")
        if search_record.stable_identifier != UNKNOWN and detail_record.stable_identifier != UNKNOWN:
            if search_record.stable_identifier == detail_record.stable_identifier:
                return True, "Stable identifier match"
            return False, "Stable identifier mismatch"
        return True, "Normalized title match"

    @staticmethod
    def _search_input(query: str, request: LiteratureSearchRequest) -> tuple[str, str]:
        raw = query.strip()
        author_match = re.fullmatch(r'author:\s*"(.+?)"', raw, flags=re.IGNORECASE)
        if author_match:
            return "author", author_match.group(1).strip()
        unwrapped = raw[1:-1].strip() if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"') else raw
        if any(
            _canonicalize_cnki_title_identity(unwrapped) == _canonicalize_cnki_title_identity(title)
            for title in request.exact_titles
        ):
            return "exact_title", unwrapped
        if any(unwrapped == author for author in request.authors):
            return "author", unwrapped
        return "keyword", unwrapped

    async def _content(self) -> tuple[str, str, str]:
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        probes = tuple(pattern.pattern for _, pattern in CNKIChallengeDetector.marker_patterns)
        try:
            observation = await self.browser.execute(
                ObserveCommand(
                    include_html=True,
                    text_probes=probes,
                )
            )
        except ObservationUnavailable:
            observation = await self.browser.execute(
                ObserveCommand(
                    include_html=False,
                    include_visible_text=False,
                    text_probes=probes,
                )
            )
            await self._inspect_live_challenge(observation)
            snapshot = observation.structured_content
            if not isinstance(snapshot, str) or not snapshot.strip():
                raise SourceUnavailable("CNKI structured browser snapshot is unavailable")
            return "snapshot", snapshot, observation.url
        await self._inspect_live_challenge(observation)
        return "html", observation.require_html(), observation.url

    async def search(self, query: str, request: LiteratureSearchRequest) -> list[LiteratureRecord]:
        mode, search_term = self._search_input(query, request)
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        await self.browser.execute(NavigateCommand(self.build_search_url(search_term, mode=mode)))
        content_kind, content, current_url = await self._content()
        parser = self.parse_search_results_html if content_kind == "html" else self.parse_search_results_snapshot
        result_limit = min(request.max_results_per_source, request.max_search_results)
        parse_limit = (
            min(30, max(10, result_limit * 5))
            if mode == "exact_title"
            else result_limit
        )
        results = parser(
            content,
            query=query,
            source_url=current_url,
            max_results=parse_limit,
        )
        if mode == "exact_title":
            wanted = _canonicalize_cnki_title_identity(search_term)
            results = [record for record in results if _canonicalize_cnki_title_identity(record.title) == wanted]
        results = results[:result_limit]
        if not results and content_kind == "snapshot" and "共找到" not in content:
            content_kind, content, current_url = await self._content()
            parser = self.parse_search_results_html if content_kind == "html" else self.parse_search_results_snapshot
            results = parser(
                content,
                query=query,
                source_url=current_url,
                max_results=parse_limit,
            )
            if mode == "exact_title":
                wanted = _canonicalize_cnki_title_identity(search_term)
                results = [record for record in results if _canonicalize_cnki_title_identity(record.title) == wanted]
            results = results[:result_limit]
        if not results and not _is_cnki_host(urlsplit(current_url).hostname):
            raise SourceUnavailable("CNKI search navigation did not reach an official CNKI host")
        return results

    async def open_result(self, record: LiteratureRecord) -> None:
        target_url = record.navigation_url if record.navigation_url != UNKNOWN else record.source_page
        parsed = urlsplit(target_url)
        if not _is_cnki_host(parsed.hostname) or not any(marker in parsed.path.casefold() for marker in _DETAIL_PATH_MARKERS):
            raise SourceLayoutChanged("Result URL is not a stable official CNKI detail page")
        self._expected_record = record
        self._detail_record = None
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        # CNKI result-detail URLs carry short-lived signed query parameters.
        # Reusing one after screening can return a genuine CNKI 404 even though
        # the record still exists.  Refresh the exact-title result page and
        # click its newly observed link instead of replaying the stale URL.
        exact_title_query = _canonicalize_cnki_exact_query(record.title)
        await self.browser.execute(NavigateCommand(self.build_search_url(exact_title_query, mode="exact_title")))
        content_kind, content, current_url = await self._content()
        parser = self.parse_search_results_html if content_kind == "html" else self.parse_search_results_snapshot
        fresh_records = parser(
            content,
            query=record.search_query if record.search_query != UNKNOWN else record.title,
            source_url=current_url,
            max_results=10,
        )
        if not fresh_records and content_kind == "snapshot" and "共找到" not in content:
            content_kind, content, current_url = await self._content()
            parser = self.parse_search_results_html if content_kind == "html" else self.parse_search_results_snapshot
            fresh_records = parser(
                content,
                query=record.search_query if record.search_query != UNKNOWN else record.title,
                source_url=current_url,
                max_results=10,
            )
        fresh_record = next(
            (candidate for candidate in fresh_records if self.identity_matches(record, candidate)[0]),
            None,
        )
        if fresh_record is None:
            raise SourceLayoutChanged("CNKI exact-title refresh could not relock the requested result")
        # CNKI's HTML result parser can preserve nonsemantic whitespace beside
        # a dash while the accessibility label exposes the same title without
        # it.  Use the restricted exact-query canonical form for the click
        # target too; the multi-field identity lock above remains authoritative.
        click_title = _canonicalize_cnki_exact_query(fresh_record.title)
        await self.browser.execute(
            ClickCommand(
                BrowserTarget(text=click_title, exact_text=True),
                follow_new_page=True,
                close_origin_when_sole_page=True,
            )
        )

    async def extract_metadata(self, *, search_query: str) -> LiteratureRecord:
        content_kind, content, current_url = await self._content()
        parser = self.parse_article_html if content_kind == "html" else self.parse_article_snapshot
        record = parser(content, source_url=current_url, search_query=search_query)
        if self._expected_record is not None:
            matches, reason = self.identity_matches(self._expected_record, record)
            if not matches:
                raise SourceLayoutChanged(f"CNKI target identity lock failed: {reason}")
            record.paper_id = self._expected_record.paper_id
            record.canonical_paper_id = self._expected_record.paper_id
            record.target_identity_confirmed = True
        self._detail_record = record
        return record

    async def extract_abstract(self) -> str:
        content_kind, content, current_url = await self._content()
        parser = self.parse_article_html if content_kind == "html" else self.parse_article_snapshot
        return parser(content, source_url=current_url).abstract

    async def check_fulltext_access(self) -> AccessDecision:
        content_kind, content, current_url = await self._content()
        article_parser = self.parse_article_html if content_kind == "html" else self.parse_article_snapshot
        if self._expected_record is not None:
            detail = article_parser(content, source_url=current_url, search_query=self._expected_record.search_query)
            matches, reason = self.identity_matches(self._expected_record, detail)
            if not matches:
                raise SourceLayoutChanged(f"CNKI target identity lock failed before access check: {reason}")
        access_parser = self.check_fulltext_access_html if content_kind == "html" else self.check_fulltext_access_snapshot
        return access_parser(content, source_url=current_url)

    async def download_fulltext(self, record: LiteratureRecord, access: AccessDecision) -> Path:
        if not access.full_text_accessible or not access.authorized_access:
            raise PermissionError("FULLTEXT_NOT_AUTHORIZED")
        content_kind, content, current_url = await self._content()
        parser = self.parse_article_html if content_kind == "html" else self.parse_article_snapshot
        detail = parser(content, source_url=current_url, search_query=record.search_query)
        matches, reason = self.identity_matches(record, detail)
        if not matches:
            raise SourceLayoutChanged(f"CNKI target identity lock failed before download: {reason}")
        record.target_identity_confirmed = True
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        label = access.download_locator
        if label in ("", UNKNOWN) or any(marker in label for marker in _REJECT_DOWNLOAD_LABELS):
            raise SourceLayoutChanged("No safe single-paper CNKI download control was locked")
        try:
            suffix = {FullTextFormat.PDF: ".pdf", FullTextFormat.CAJ: ".caj"}.get(access.full_text_format, ".bin")
            artifact = await self.browser.execute(
                DownloadCommand(
                    target=BrowserTarget(
                        text=label,
                        exact_text=True,
                    ),
                    suggested_filename=f"{record.paper_id}{suffix}",
                )
            )
            return artifact.local_path
        except Exception as exc:
            raise SourceUnavailable(
                f"Authorized CNKI control did not produce a browser download: {type(exc).__name__}"
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
