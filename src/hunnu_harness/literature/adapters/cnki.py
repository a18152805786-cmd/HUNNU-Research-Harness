from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit

from ...browser.commands import BrowserTarget, DownloadCommand, NavigateCommand, ObserveCommand
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


class _CNKIHTMLParser(_ScienceDirectHTMLParser):
    """Capture CNKI's stable semantic blocks in addition to citation metadata."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: dict[str, list[str]] = defaultdict(list)
        self.author_links: list[str] = []
        self._capture_stack: list[tuple[str, str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        attributes = {key.casefold(): (value or "") for key, value in attrs}
        lowered = tag.casefold()
        classes = set(attributes.get("class", "").casefold().split())
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
        return f"{cls.search_origin}/kns8s/defaultresult/index?korder={order}&kw={quote_plus(query)}"

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

    @staticmethod
    def identity_matches(search_record: LiteratureRecord, detail_record: LiteratureRecord) -> tuple[bool, str]:
        if search_record.doi != UNKNOWN and detail_record.doi != UNKNOWN:
            return (search_record.doi == detail_record.doi, "DOI exact match" if search_record.doi == detail_record.doi else "DOI mismatch")
        if search_record.stable_identifier != UNKNOWN and detail_record.stable_identifier != UNKNOWN:
            if search_record.stable_identifier == detail_record.stable_identifier:
                return True, "Stable identifier match"
        matched = normalize_title(search_record.title) == normalize_title(detail_record.title)
        return matched, "Normalized title match" if matched else "Target title mismatch"

    async def _content(self) -> tuple[str, str]:
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        observation = await self.browser.execute(
            ObserveCommand(
                include_html=True,
                text_probes=tuple(pattern.pattern for _, pattern in CNKIChallengeDetector.marker_patterns),
            )
        )
        await self._inspect_live_challenge(observation)
        return observation.require_html(), observation.url

    async def search(self, query: str, request: LiteratureSearchRequest) -> list[LiteratureRecord]:
        mode = "exact_title" if query in request.exact_titles else ("author" if query in request.authors else "keyword")
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        await self.browser.execute(NavigateCommand(self.build_search_url(query, mode=mode)))
        html, current_url = await self._content()
        results = self.parse_search_results_html(
            html,
            query=query,
            source_url=current_url,
            max_results=min(request.max_results_per_source, request.max_search_results),
        )
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
        await self.browser.execute(NavigateCommand(target_url))

    async def extract_metadata(self, *, search_query: str) -> LiteratureRecord:
        html, current_url = await self._content()
        record = self.parse_article_html(html, source_url=current_url, search_query=search_query)
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
        html, current_url = await self._content()
        return self.parse_article_html(html, source_url=current_url).abstract

    async def check_fulltext_access(self) -> AccessDecision:
        html, current_url = await self._content()
        if self._expected_record is not None:
            detail = self.parse_article_html(html, source_url=current_url, search_query=self._expected_record.search_query)
            matches, reason = self.identity_matches(self._expected_record, detail)
            if not matches:
                raise SourceLayoutChanged(f"CNKI target identity lock failed before access check: {reason}")
        return self.check_fulltext_access_html(html, source_url=current_url)

    async def download_fulltext(self, record: LiteratureRecord, access: AccessDecision) -> Path:
        if not access.full_text_accessible or not access.authorized_access:
            raise PermissionError("FULLTEXT_NOT_AUTHORIZED")
        html, current_url = await self._content()
        detail = self.parse_article_html(html, source_url=current_url, search_query=record.search_query)
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
                        css="a, button",
                        text_regex=rf"^\s*{re.escape(label)}\s*$",
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
