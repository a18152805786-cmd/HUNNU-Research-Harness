from __future__ import annotations

import asyncio
import dataclasses
import html as html_lib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote_plus, urljoin, urlsplit

from ...browser.commands import (
    BrowserCommandError,
    BrowserTarget,
    ClickCommand,
    DownloadCommand,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
)
from .base import LiteratureSourceAdapter, SourceActionRequired, SourceLayoutChanged, SourceUnavailable
from .sciencedirect import _Anchor, _ScienceDirectHTMLParser, _meta_all, _meta_first
from ..cnki_challenge import (
    CAPTCHA_CHALLENGE_STATES,
    CNKIChallengeDetector,
    ChallengeDiagnostic,
    ChallengeState,
    StaticChallengeEvidence,
    challenge_state_requires_manual_action,
    classify_static_challenge,
    resolve_challenge_state,
)
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


_CNKI_2026_DETAIL_PATH = "/kcms2/article/abstract"
_LEGACY_DETAIL_PATH_MARKERS = ("/article/abstract", "/detail/detail.aspx", "/kcms/detail/")
_DETAIL_PATH_MARKERS = (_CNKI_2026_DETAIL_PATH, *_LEGACY_DETAIL_PATH_MARKERS)
_REJECT_DOWNLOAD_LABELS = ("批量下载", "多篇下载", "相关推荐", "参考文献下载", "整本下载")
_DOWNLOAD_ACTIONS = ("pdf下载", "caj下载", "全文下载", "下载全文", "download pdf", "download caj")
_CNKI_RESOURCE_ORDER_PATH = "/bar/download/order"
_CNKI_2026_RESULT_CLASSES = frozenset({"fz14", "inline"})
# A kns8s result row (``table.result-table-list tr``) names its own record on
# its collect control, ``a.icon-collect[data-dbname][data-filename]``; the
# row's title link is ``a.fz14``.
_CNKI_RESULT_TABLE_CLASS = "result-table-list"
_CNKI_RESULT_ROW_KEY_CLASS = "icon-collect"
_CNKI_RESULT_TITLE_CLASS = "fz14"
_CNKI_2026_DOWNLOAD_CONTROL_IDS = frozenset({"pdfdown", "cajdown"})
_CNKI_INSTITUTION_LABEL_CLASSES = frozenset({"ecp_header_unitname", "ecp_unitaccountname"})
_CNKI_GENERIC_LOGIN_LABELS = frozenset(
    {
        "个人登录",
        "个人账号登录",
        "机构登录",
        "账号登录",
        "登录",
        "登录/注册",
        "登录注册",
        "请登录",
        "立即登录",
        "免费注册",
        "退出",
    }
)
_SEARCH_SETTLE_DELAY_SECONDS = 10.0
_SEARCH_SETTLE_MAX_OBSERVATIONS = 3
_ACCESS_SETTLE_DELAY_SECONDS = 2.0
_ACCESS_SETTLE_MAX_OBSERVATIONS = 4
_CNKI_CLICK_RETRY_DELAY_SECONDS = 1.0
_CNKI_CLICK_RETRIES = 1


class _CNKIObservationUnreadable(SourceUnavailable):
    """The current observation could not be read, so a settle loop may re-read it."""


_CNKI_EXPLICIT_FULLTEXT_BLOCK_MARKERS = (
    "当前机构未获得全文访问权限",
    "机构未获得全文访问权限",
    "机构未订购",
    "未订购",
    "无全文权限",
    "全文权限不足",
    "无权访问全文",
    "获取全文失败",
    "单篇购买",
    "请登录后购买",
    "请登录后阅读",
    "请登录后下载",
    "权限不足",
    "full text not available",
    "purchase full text",
    "login required",
    "sign in to access full text",
)
_INSTITUTIONAL_ACCESS_MARKERS = (
    "当前机构已获得全文访问权限",
    "机构已获得全文访问权限",
    "institutional access",
    "湖南师范大学",
    "hunan normal university",
)
_CNKI_DASH_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d"
_CNKI_DASH_SPACE_RE = re.compile(rf"\s*([{_CNKI_DASH_CHARS}])\s*")
_CNKI_CJK_JOIN_SPACE_RE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
_CNKI_ENUMERATION_SPACE_RE = re.compile(r"\s*([、])\s*")
_CNKI_MARKUP_PUNCT_SPACE_RE = re.compile(r'\s*([?!:：“”‘’「」『』《》〈〉【】〔〕（）])\s*')
_CNKI_SUBTITLE_SEPARATOR_RE = re.compile(r"[:：](?=(?:基于|来自|关于|对))")


def _access_decision_is_settling(decision: AccessDecision) -> bool:
    """Return whether a bounded re-observation may clarify CNKI access."""

    if decision.status == RunStatus.SUCCESS or decision.full_text_accessible:
        return False
    if decision.access_type == AccessType.UNKNOWN:
        return True
    return (
        decision.download_url in ("", UNKNOWN)
        and decision.download_locator in ("", UNKNOWN)
    )


def _decode_cnki_form_query_value(value: str) -> str:
    """Decode a CNKI URL/form value before it enters bibliographic logic.

    ``+`` is a space only at this explicit ``application/x-www-form-urlencoded``
    boundary. This helper must not be applied to a title read from the page.
    """

    return html_lib.unescape(unquote_plus(str(value)))


def _normalize_cnki_plain_title_spacing(value: str) -> str:
    """Remove only nonsemantic spacing introduced around CNKI title markup."""

    text = html_lib.unescape(unicodedata.normalize("NFKC", str(value)))
    text = re.sub(r"\s+", " ", text).strip()
    text = _CNKI_CJK_JOIN_SPACE_RE.sub("", text)
    text = _CNKI_ENUMERATION_SPACE_RE.sub(r"\1", text)
    return _CNKI_DASH_SPACE_RE.sub(r"\1", text)


def _normalize_cnki_observed_title_spacing(value: str) -> str:
    """Also collapse spacing around punctuation split by highlighted markup."""

    return _CNKI_MARKUP_PUNCT_SPACE_RE.sub(
        r"\1",
        _normalize_cnki_plain_title_spacing(value),
    )


def _canonicalize_cnki_exact_query(value: str) -> str:
    """Canonicalize plain text used to build a CNKI exact-title query.

    This is deliberately a query-input operation, not a URL decoder. It
    removes only nonsemantic whitespace observed around nested highlighted
    fragments, enumeration commas, and dash forms in CNKI titles, so
    ``quote_plus`` cannot turn those layout differences into literal ``+``
    characters in CNKI's search box.
    """

    return _normalize_cnki_plain_title_spacing(value)


def _canonicalize_cnki_title_identity(value: str) -> str:
    """Normalize a page-observed bibliographic title without fuzzy matching.

    A literal ``+`` remains a literal ``+`` here. Query/form decoding must
    happen in ``_decode_cnki_form_query_value`` before this helper is called
    when the input is known to be encoded query data.
    """

    text = _normalize_cnki_observed_title_spacing(value)
    # CNKI sometimes renders the same subtitle separator as a colon on the
    # result page and as an em dash on the article page.  Treat only the
    # common subtitle lead-ins as equivalent; other punctuation remains strict.
    text = _CNKI_SUBTITLE_SEPARATOR_RE.sub("——", text)
    return text.casefold()


def _cnki_known(value: str) -> bool:
    return bool(value and value != UNKNOWN)


def _cnki_authors_identity(authors: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for author in authors:
        if not _cnki_known(author):
            continue
        # CNKI result rows may append affiliation markers (for example
        # ``李红 1; 包群 2``) while the article page exposes one author link
        # per person.  Strip only trailing numeric markers at this identity
        # boundary; the bibliographic author text itself remains unchanged.
        for item in re.split(r"[;；,，、]+", str(author)):
            value = re.sub(r"\s*\d+\s*$", "", item).strip()
            if value:
                normalized.append(_canonicalize_cnki_title_identity(value))
    return tuple(normalized)


def _is_cnki_host(hostname: str | None) -> bool:
    host = (hostname or "").casefold().rstrip(".")
    return host == "cnki.net" or host.endswith(".cnki.net")


def _is_cnki_detail_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    path = parsed.path.casefold()
    return _is_cnki_host(parsed.hostname) and any(marker in path for marker in _DETAIL_PATH_MARKERS)


def _cnki_search_result_layout_priority(anchor: _Anchor, absolute_url: str) -> int | None:
    """Prefer the 2026 SPA result-row anchor, then retain legacy fallbacks."""

    if not _is_cnki_detail_url(absolute_url):
        return None
    path = urlsplit(absolute_url).path.casefold()
    classes = frozenset(anchor.attributes.get("class", "").casefold().split())
    if path.startswith(_CNKI_2026_DETAIL_PATH) and _CNKI_2026_RESULT_CLASSES.issubset(classes):
        return 0
    if path.startswith(_CNKI_2026_DETAIL_PATH):
        return 1
    return 2


def _cnki_result_opens_new_tab(anchor: _Anchor, absolute_url: str) -> bool:
    """Identify the current SPA result link that must be followed in-page."""

    classes = frozenset(anchor.attributes.get("class", "").casefold().split())
    return (
        _is_cnki_detail_url(absolute_url)
        and urlsplit(absolute_url).path.casefold().startswith(_CNKI_2026_DETAIL_PATH)
        and "fz14" in classes
        and anchor.attributes.get("target", "").casefold() == "_blank"
    )


def _is_cnki_resource_order_url(value: str) -> bool:
    """Recognize CNKI's current-article order action without treating it as a file URL."""

    if not value or value == UNKNOWN:
        return False
    parsed = urlsplit(value)
    return (
        (parsed.hostname or "").casefold() == "bar.cnki.net"
        and parsed.path.casefold().rstrip("/") == _CNKI_RESOURCE_ORDER_PATH
        and bool(parse_qs(parsed.query).get("id"))
    )


def _cnki_authenticated_institution_label(value: str) -> str:
    """Return a concrete institution label, never a generic login control."""

    label = re.sub(r"\s+", " ", value).strip()
    compact = re.sub(r"\s+", "", label).casefold()
    if not compact or compact in _CNKI_GENERIC_LOGIN_LABELS:
        return ""
    if "个人登录" in compact or "个人账号登录" in compact:
        return ""
    return label


def _cnki_download_layout_priority(anchor: _Anchor, absolute_url: str) -> int:
    """Prefer current ``pdfDown``/``cajDown`` controls within one format."""

    identities = {
        anchor.attributes.get("id", "").casefold(),
        anchor.attributes.get("name", "").casefold(),
    }
    if identities & _CNKI_2026_DOWNLOAD_CONTROL_IDS and _is_cnki_resource_order_url(absolute_url):
        return 0
    return 1


def _cnki_full_text_format(label: str, href: str) -> FullTextFormat:
    """Classify one already-screened CNKI single-paper download control."""

    value = f"{label} {href}".casefold()
    if "pdf" in value:
        return FullTextFormat.PDF
    if any(marker in value for marker in ("caj", ".nh", ".kdh")):
        return FullTextFormat.CAJ
    return FullTextFormat.OTHER_AUTHORIZED_FORMAT


def _cnki_full_text_preference(full_text_format: FullTextFormat) -> int:
    """Return the frozen acquisition preference: PDF, then CAJ, then other."""

    return {
        FullTextFormat.PDF: 0,
        FullTextFormat.CAJ: 1,
        FullTextFormat.OTHER_AUTHORIZED_FORMAT: 2,
    }[full_text_format]


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


#: Offsets at least this far outside the document are treated as "parked"
#: rather than merely scrolled: CNKI preloads its verification component at
#: coordinates such as ``top:-1000000px``, and ``left:-9999px`` is the classic
#: off-screen idiom.  A sticky header at ``top:-40px`` stays on screen.
_OFFSCREEN_OFFSET_THRESHOLD_PX = 2000
_OVERLAY_NAME_MARKERS = ("mask", "overlay", "modal", "dialog", "backdrop", "popup", "shade")
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: Text fragments that indicate a CNKI verification component *exists*.  They
#: are shared by every static probe so no module keeps its own private list.
_CHALLENGE_TEXT_MARKERS = (
    "拖动下方拼图完成验证",
    "captcha",
    "验证码",
    "滑块",
    "拼图",
    "安全验证",
    "人机验证",
    "访问过于频繁",
)


def _contains_challenge_marker(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in _CHALLENGE_TEXT_MARKERS)


def _style_declarations(style: str) -> dict[str, str]:
    """Split an already whitespace-stripped inline style into declarations."""

    declarations: dict[str, str] = {}
    for item in style.split(";"):
        name, separator, value = item.partition(":")
        if separator and name:
            declarations[name] = value
    return declarations


def _css_px(value: str) -> float | None:
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)(?:px)?", value)
    return float(match.group(1)) if match else None


def _style_is_inert(declarations: dict[str, str]) -> bool:
    """Return whether an inline style parks an element outside the rendered page.

    This is the static counterpart of the runtime detector's viewport and
    ancestor-opacity checks.  It proves a component is *not* on screen; it
    never proves that one is.
    """

    opacity = declarations.get("opacity")
    if opacity is not None:
        value = _css_px(opacity)
        if value is not None and value <= 0:
            return True
    for axis in ("top", "left", "right", "bottom", "text-indent", "margin-left", "margin-top"):
        offset = _css_px(declarations.get(axis, ""))
        if offset is not None and offset <= -_OFFSCREEN_OFFSET_THRESHOLD_PX:
            return True
    transform = declarations.get("transform", "")
    if any(
        offset is not None and offset <= -_OFFSCREEN_OFFSET_THRESHOLD_PX
        for offset in (_css_px(item) for item in re.findall(r"-?\d+(?:\.\d+)?px", transform))
    ):
        return True
    if declarations.get("clip-path", "").startswith("inset(100%"):
        return True
    if re.fullmatch(r"rect\(0(?:px)?,0(?:px)?,0(?:px)?,0(?:px)?\)", declarations.get("clip", "")):
        return True
    for axis in ("width", "height"):
        size = _css_px(declarations.get(axis, ""))
        if size is not None and size <= 0:
            return True
    return False


def _style_is_blocking_overlay(declarations: dict[str, str], names: set[str]) -> bool:
    """Return whether inline markup declares a layer that covers the page.

    Only positioned layers count, and only when they are either named as a
    mask/overlay or stacked above the page content.  A parked component is
    excluded by the caller before this runs.
    """

    position = declarations.get("position", "")
    if position not in {"fixed", "absolute", "sticky"}:
        return False
    if any(marker in name for name in names for marker in _OVERLAY_NAME_MARKERS):
        return True
    z_index = _css_px(declarations.get("z-index", ""))
    return z_index is not None and z_index >= 100


class _CNKIHTMLParser(_ScienceDirectHTMLParser):
    """Capture CNKI's stable semantic blocks in addition to citation metadata."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: dict[str, list[str]] = defaultdict(list)
        self.author_links: list[str] = []
        self.institution_login_status = False
        self.authenticated_institution_labels: list[str] = []
        self.visible_text_parts: list[str] = []
        self.overlay_text_parts: list[str] = []
        self.document_title_parts: list[str] = []
        self.current_article_markers: set[str] = set()
        self._document_title_depth = 0
        self._capture_stack: list[tuple[str, str, list[str]]] = []
        # Subtree state is tracked by position in the open-element stack rather
        # than by tag name: a parked ``<div>`` wrapper must stay parked while
        # its ordinary ``<div>`` children open and close inside it.
        self._open_tags: list[str] = []
        self._hidden_depths: list[int] = []
        self._non_content_depths: list[int] = []
        self._overlay_depths: list[int] = []
        self._institution_header_depths: list[int] = []
        self._institution_label_captures: list[tuple[int, list[str]]] = []
        # kns8s result rows.  ``_anchor_result_rows`` runs parallel to
        # ``anchors`` and holds the row each anchor closed in; each row keeps
        # the (dbname, filename) pairs its own collect controls carry.
        self._anchor_result_rows: list[int | None] = []
        self._result_row_pairs: list[set[tuple[str, str]]] = []
        self._result_table_depths: list[int] = []
        self._result_row: tuple[int, int] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        attributes = {key.casefold(): (value or "") for key, value in attrs}
        lowered = tag.casefold()
        classes = set(attributes.get("class", "").casefold().split())
        style = re.sub(r"\s+", "", attributes.get("style", "").casefold())
        declarations = _style_declarations(style)
        inert = (
            "hidden" in attributes
            or attributes.get("aria-hidden", "").casefold() == "true"
            or declarations.get("display") == "none"
            or declarations.get("visibility") in {"hidden", "collapse"}
            or _style_is_inert(declarations)
        )
        if lowered not in _VOID_ELEMENTS:
            self._open_tags.append(lowered)
            depth = len(self._open_tags)
            if inert:
                # Parked, transparent, and declared-hidden subtrees are recorded
                # so their text never counts as rendered on screen.
                self._hidden_depths.append(depth)
            elif _style_is_blocking_overlay(declarations, classes | {attributes.get("id", "").casefold()}):
                self._overlay_depths.append(depth)
            if lowered in {"script", "style", "noscript", "template"}:
                self._non_content_depths.append(depth)
            if lowered == "title":
                self._document_title_depth += 1
            if "ecp_header_login_area" in classes:
                self._institution_header_depths.append(depth)
            if lowered == "table" and _CNKI_RESULT_TABLE_CLASS in classes:
                self._result_table_depths.append(depth)
            elif lowered == "tr" and self._result_table_depths:
                self._result_row = (len(self._result_row_pairs), depth)
                self._result_row_pairs.append(set())
        if self._result_row is not None and _CNKI_RESULT_ROW_KEY_CLASS in classes:
            dbname = attributes.get("data-dbname", "").strip()
            filename = attributes.get("data-filename", "").strip()
            if dbname and filename:
                self._result_row_pairs[self._result_row[0]].add((dbname.casefold(), filename))
        if "ecp_header_login_status1" in classes:
            self.institution_login_status = True
        elif (
            {"ecp_header_login_status", "ecp_header_login"}.issubset(classes)
            and bool(self._institution_header_depths)
        ):
            self.institution_login_status = True
        if "ecp_header_unitname" in classes:
            label = _cnki_authenticated_institution_label(attributes.get("title", ""))
            if label and "display:none" not in style and "visibility:hidden" not in style:
                self.authenticated_institution_labels.append(label)
        if (
            classes & _CNKI_INSTITUTION_LABEL_CLASSES
            and bool(self._institution_header_depths)
            and not self._hidden_depths
            and lowered not in _VOID_ELEMENTS
        ):
            self._institution_label_captures.append((len(self._open_tags), []))
        key = ""
        if lowered == "h1":
            key = "title"
            self.current_article_markers.add("title")
        elif lowered == "h3" and attributes.get("id", "").casefold() == "authorpart":
            key = "authors"
            self.current_article_markers.add("authors")
        elif attributes.get("id", "").casefold() == "chdivsummary":
            key = "abstract"
            self.current_article_markers.add("abstract")
        elif "keywords" in classes:
            key = "keywords"
        elif "top-tip" in classes:
            key = "source"
            self.current_article_markers.add("source")
        if attributes.get("id", "").casefold() in _CNKI_2026_DOWNLOAD_CONTROL_IDS:
            self.current_article_markers.add("download")
        if key:
            self._capture_stack.append((lowered, key, []))

    def handle_data(self, data: str) -> None:
        super().handle_data(data)
        if self._document_title_depth:
            self.document_title_parts.append(data.strip())
        if self._hidden_depths or self._non_content_depths:
            return
        stripped = data.strip()
        if stripped:
            self.visible_text_parts.append(stripped)
            if self._overlay_depths:
                self.overlay_text_parts.append(stripped)
        for _, parts in self._institution_label_captures:
            parts.append(data)
        for _, _, parts in self._capture_stack:
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "title" and self._document_title_depth:
            self._document_title_depth -= 1
        if lowered == "a" and self._capture_stack and self._anchor_stack:
            if any(key == "authors" for _, key, _ in self._capture_stack):
                author = re.sub(r"\s+", " ", " ".join(self._anchor_stack[-1][2])).strip()
                if author:
                    self.author_links.append(author)
        anchor_count = len(self.anchors)
        super().handle_endtag(tag)
        if len(self.anchors) > anchor_count:
            self._anchor_result_rows.append(self._result_row[0] if self._result_row is not None else None)
        self._close_element(lowered)
        self._finish_institution_label_captures()
        if self._capture_stack and self._capture_stack[-1][0] == lowered:
            _, key, parts = self._capture_stack.pop()
            value = re.sub(r"\s+", " ", " ".join(parts)).strip()
            if value:
                self.blocks[key].append(value)

    def _close_element(self, tag: str) -> None:
        """Close the nearest matching open element and any unclosed descendants.

        Real CNKI markup leaves elements unclosed, so an end tag closes back to
        its nearest matching start tag.  Subtree state is then trimmed to the
        remaining depth, which keeps a parked wrapper active for exactly its
        own subtree and never leaks past it.
        """

        if tag in _VOID_ELEMENTS:
            return
        for index in range(len(self._open_tags) - 1, -1, -1):
            if self._open_tags[index] != tag:
                continue
            del self._open_tags[index:]
            remaining = len(self._open_tags)
            self._hidden_depths = [depth for depth in self._hidden_depths if depth <= remaining]
            self._non_content_depths = [depth for depth in self._non_content_depths if depth <= remaining]
            self._overlay_depths = [depth for depth in self._overlay_depths if depth <= remaining]
            self._institution_header_depths = [
                depth for depth in self._institution_header_depths if depth <= remaining
            ]
            self._result_table_depths = [depth for depth in self._result_table_depths if depth <= remaining]
            if self._result_row is not None and self._result_row[1] > remaining:
                self._result_row = None
            return

    def _finish_institution_label_captures(self) -> None:
        remaining = len(self._open_tags)
        open_captures: list[tuple[int, list[str]]] = []
        for depth, parts in self._institution_label_captures:
            if depth <= remaining:
                open_captures.append((depth, parts))
                continue
            label = _cnki_authenticated_institution_label(" ".join(parts))
            if label and label not in self.authenticated_institution_labels:
                self.authenticated_institution_labels.append(label)
        self._institution_label_captures = open_captures

    def first_block(self, key: str) -> str:
        values = self.blocks.get(key, [])
        return values[0] if values else UNKNOWN

    def result_row_key(self, anchor_index: int) -> str:
        """The CNKI key of the result row titled by ``anchors[anchor_index]``, or ``""``.

        The key is ``dbname:filename`` from that row's own collect control, and
        only a row carrying exactly one such pair has one.  Only the row's title
        link (``a.fz14``) takes it: another link in the row -- a citation
        count, say -- and an anchor outside ``table.result-table-list`` rows
        have none.
        """

        row = self._anchor_result_rows[anchor_index]
        classes = self.anchors[anchor_index].attributes.get("class", "").casefold().split()
        if row is None or _CNKI_RESULT_TITLE_CLASS not in classes or len(self._result_row_pairs[row]) != 1:
            return ""
        dbname, filename = next(iter(self._result_row_pairs[row]))
        return f"{dbname}:{filename}"

    @property
    def visible_body_text(self) -> str:
        return " ".join(self.visible_text_parts)

    @property
    def overlay_body_text(self) -> str:
        return " ".join(self.overlay_text_parts)

    @property
    def document_title(self) -> str:
        """The ``<title>`` element, i.e. the page's own identity.

        This is deliberately distinct from ``og:title``/``citation_title``,
        which carry the *article* title and may legitimately name CAPTCHA
        research.
        """

        return " ".join(part for part in self.document_title_parts if part)

    @property
    def uses_2026_article_layout(self) -> bool:
        return len(
            self.current_article_markers & {"authors", "abstract", "source", "download"}
        ) >= 2


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
        # query string -> (mode, search term) actually sent to CNKI, so a result
        # can be refreshed with the same search that found it (see open_result).
        self._search_inputs: dict[str, tuple[str, str]] = {}
        # result title link -> the CNKI key of the kns8s row it titles, so a
        # result relocks onto its own row, not the first row with its title.
        self._result_row_keys: dict[str, str] = {}

    @staticmethod
    def _parser(html: str) -> _CNKIHTMLParser:
        parser = _CNKIHTMLParser()
        parser.feed(html)
        return parser

    @classmethod
    def static_challenge_evidence(cls, html: str, *, url: str = "") -> StaticChallengeEvidence:
        """Collect static CNKI challenge evidence without claiming activity."""

        parser = cls._parser(html)
        return cls._static_challenge_evidence(parser, url=url)

    @classmethod
    def _static_challenge_evidence(
        cls,
        parser: _CNKIHTMLParser,
        *,
        url: str = "",
    ) -> StaticChallengeEvidence:
        text_present = _contains_challenge_marker(parser.body_text)
        # ``visible_body_text`` excludes declared-hidden, transparent, and
        # parked subtrees, so on-screen text is real rendering evidence rather
        # than DOM presence.
        on_screen = _contains_challenge_marker(parser.visible_body_text)
        # The document's own ``<title>`` identifies the page; ``og:title`` and
        # ``citation_title`` carry the *article* title, which may legitimately
        # be research about CAPTCHAs and must never gate the run.
        page_identity_is_challenge = _contains_challenge_marker(
            parser.document_title
        ) or _contains_challenge_marker(urlsplit(url).path)
        return StaticChallengeEvidence(
            text_present=text_present,
            on_screen_text_present=on_screen,
            blocking_overlay=_contains_challenge_marker(parser.overlay_body_text),
            business_evidence=cls._normal_business_evidence(parser, url=url),
            page_identity_is_challenge=page_identity_is_challenge and not cls._has_metadata(parser),
        )

    @staticmethod
    def _has_metadata(parser: _CNKIHTMLParser) -> bool:
        return _meta_first(parser, "citation_title", "dc.title") != UNKNOWN

    @classmethod
    def _normal_business_evidence(cls, parser: _CNKIHTMLParser, *, url: str = "") -> bool:
        return (
            cls._has_metadata(parser)
            or any(
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
            )
            or any(
                _is_cnki_host(urlsplit(urljoin(url or cls.search_origin, anchor.href)).hostname)
                and any(
                    path_marker in urlsplit(urljoin(url or cls.search_origin, anchor.href)).path.casefold()
                    for path_marker in _DETAIL_PATH_MARKERS
                )
                for anchor in parser.anchors
            )
        )

    @classmethod
    def detect_interruption(
        cls,
        html: str,
        *,
        url: str = "",
        challenge_state: ChallengeState | None = None,
    ) -> None:
        """Stop for manual action only on authoritative challenge/login evidence.

        ``challenge_state`` carries the runtime verdict already produced by
        :class:`CNKIChallengeDetector` for this very observation.  When it is
        supplied it is authoritative, so this parse can never re-escalate a
        page the detector has already classified as dormant.
        """

        parser = cls._parser(html)
        has_metadata = cls._has_metadata(parser)
        normal_business_evidence = cls._normal_business_evidence(parser, url=url)
        cls.enforce_challenge_state(
            resolve_challenge_state(
                static_state=classify_static_challenge(
                    cls._static_challenge_evidence(parser, url=url)
                ),
                runtime_state=challenge_state,
            )
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
        # A login prompt only blocks the run when it is actually rendered.  CNKI
        # keeps a personal-login box in the markup of ordinary result pages, so
        # the same rule that governs challenge text governs login text: page
        # identity (URL and title) plus on-screen body copy, never parked or
        # declared-hidden DOM text.
        login_haystack = (
            f"{url} {_meta_first(parser, 'title', 'og:title')} {parser.visible_body_text}".casefold()
        )
        blocking_login = any(
            marker in login_haystack
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

    def _runtime_challenge_state(self) -> ChallengeState | None:
        """The authoritative state for the observation currently being parsed.

        ``_content`` inspects every observation with ``CNKIChallengeDetector``
        before returning it, so this value is the verdict for exactly the
        content the caller is about to parse.
        """

        diagnostic = self._last_challenge_diagnostic
        return diagnostic.state if diagnostic is not None else None

    @staticmethod
    def enforce_challenge_state(state: ChallengeState) -> None:
        """The one place CNKI turns a challenge state into a manual-action stop.

        Every CNKI caller routes through here, so a state that is ``NONE`` or
        ``DORMANT`` continues the run and a state that is ``VISIBLE``,
        ``BLOCKING``, or ``UNCERTAIN`` stops it.  No challenge interaction of
        any kind is attempted in either direction.
        """

        if not challenge_state_requires_manual_action(state):
            return
        captcha = state in CAPTCHA_CHALLENGE_STATES
        reason = (
            "CNKI CAPTCHA or security verification required"
            if captcha
            else f"CNKI challenge state is {state.value}"
        )
        raise SourceActionRequired(
            "ACTION_REQUIRED_USER_LOGIN=true; "
            f"Reason={reason}; "
            f"ChallengeState={state.value}; "
            "BrowserReadyForManualAction=true; "
            f"CaptchaDetected={'true' if captcha else 'uncertain'}; "
            "CaptchaBypassAttempted=false"
        )

    @classmethod
    def enforce_challenge_diagnostic(cls, diagnostic: ChallengeDiagnostic) -> None:
        if not diagnostic.target_page_confirmed:
            raise SourceLayoutChanged(
                "TargetPageIdentity=uncertain; CNKI automation stopped before any business action"
            )
        cls.enforce_challenge_state(diagnostic.state)

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
        challenge_state: ChallengeState | None = None,
    ) -> list[LiteratureRecord]:
        cls.detect_interruption(html, url=source_url, challenge_state=challenge_state)
        parser = cls._parser(html)
        records: list[LiteratureRecord] = []
        # identity -> the row keys already recorded under it; "" stands for an
        # anchor without one.
        seen: dict[str, set[str]] = {}
        ranked_anchors: list[tuple[int, int, _Anchor, str]] = []
        for index, anchor in enumerate(parser.anchors):
            absolute = urljoin(source_url, anchor.href)
            layout_priority = _cnki_search_result_layout_priority(anchor, absolute)
            if layout_priority is None:
                continue
            ranked_anchors.append((layout_priority, index, anchor, absolute))
        for _layout_priority, index, anchor, absolute in sorted(ranked_anchors):
            title = _normalize_cnki_observed_title_spacing(anchor.text)
            if not title or any(label in title for label in _REJECT_DOWNLOAD_LABELS):
                continue
            stable_identifier = _stable_identifier(absolute)
            identity = stable_identifier if stable_identifier != UNKNOWN else normalize_title(title)
            # A signed 2026 result link names no dbcode/filename, so its identity
            # is the title, and one newspaper headline can head several different
            # articles.  Anchors sharing an identity stay one record, except that
            # the title links of two rows whose own CNKI keys differ never merge.
            row_key = parser.result_row_key(index)
            recorded_row_keys = seen.setdefault(identity, set())
            if recorded_row_keys and (
                not row_key or "" in recorded_row_keys or row_key in recorded_row_keys
            ):
                continue
            recorded_row_keys.add(row_key)
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
        challenge_state: ChallengeState | None = None,
    ) -> list[LiteratureRecord]:
        """Parse bounded CNKI result links from an MCP accessibility snapshot.

        Snapshot text naming a verification component is never escalated here:
        an accessibility tree cannot show whether a component is on screen, so
        only the runtime ``challenge_state`` may stop the run.
        """

        cls.enforce_challenge_state(resolve_challenge_state(runtime_state=challenge_state))
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
        challenge_state: ChallengeState | None = None,
    ) -> LiteratureRecord:
        cls.detect_interruption(html, url=source_url, challenge_state=challenge_state)
        parser = cls._parser(html)
        body = re.sub(r"\s+", " ", parser.body_text).strip()
        title_candidates = [
            value
            for value in parser.blocks.get("title", [])
            if value not in {"自动登录", "找回密码"}
        ]
        semantic_title = title_candidates[-1] if title_candidates else UNKNOWN
        meta_title = _meta_first(parser, "citation_title", "dc.title", "og:title")
        title = (
            semantic_title
            if parser.uses_2026_article_layout and semantic_title != UNKNOWN
            else meta_title
        )
        if title == UNKNOWN:
            title = semantic_title
        meta_authors = _meta_all(parser, "citation_author", "dc.creator")
        semantic_authors = tuple(dict.fromkeys(parser.author_links))
        authors = (
            semantic_authors
            if parser.uses_2026_article_layout and semantic_authors
            else meta_authors
        )
        if not authors:
            authors = semantic_authors
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
            r"^(.+?)\s*\.\s*((?:18|19|20|21)\d{2})\s*(?:[,，]\s*\d+)?\s*\(([^)]+)\)\s*[:：]\s*([0-9]+(?:\s*[-–—]\s*[0-9]+)?)",
            source_line,
        )
        if source_match:
            journal = source_match.group(1).strip() or journal
            if parser.uses_2026_article_layout or date == UNKNOWN:
                date = source_match.group(2)
            if parser.uses_2026_article_layout or year == UNKNOWN:
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
            if parser.uses_2026_article_layout or issue == UNKNOWN:
                issue = source_match.group(3).strip()
            if parser.uses_2026_article_layout or pages == UNKNOWN:
                pages = re.sub(r"\s+", "", source_match.group(4)).replace("–", "-").replace("—", "-")
        if pages == UNKNOWN:
            pages = _text_field(body, "页码", "页")
        meta_abstract = _meta_first(parser, "citation_abstract", "description", "dc.description")
        semantic_abstract = parser.first_block("abstract")
        abstract = (
            semantic_abstract
            if parser.uses_2026_article_layout and semantic_abstract != UNKNOWN
            else meta_abstract
        )
        if abstract == UNKNOWN:
            abstract = semantic_abstract
        if abstract == UNKNOWN:
            abstract = _text_field(body, "摘要")
        keyword_values = _meta_all(parser, "citation_keywords", "keywords", "dc.subject")
        meta_keywords = _split_keywords(";".join(keyword_values)) if keyword_values else ()
        semantic_keywords = _split_keywords(parser.first_block("keywords"))
        keywords = (
            semantic_keywords
            if parser.uses_2026_article_layout and semantic_keywords
            else meta_keywords
        )
        if not keywords:
            keywords = semantic_keywords
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
        challenge_state: ChallengeState | None = None,
    ) -> LiteratureRecord:
        """Extract CNKI article metadata from the structured MCP snapshot."""

        cls.enforce_challenge_state(resolve_challenge_state(runtime_state=challenge_state))
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
            r"((?:18|19|20|21)\d{2})\s*(?:[,，]\s*\d+)?\s*\(([^)]+)\)\s*[：:]\s*([0-9]+(?:\s*[-–—]\s*[0-9]+)?)",
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
        if journal != UNKNOWN:
            status = (
                PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value
                if any(marker in body for marker in ("同行评议", "peer reviewed", "peer-reviewed"))
                else PublicationStatus.UNKNOWN.value
            )
            return "JournalArticle", status
        if "学位论文" in body:
            return "Dissertation", PublicationStatus.OTHER.value
        if "会议论文" in body:
            return "ConferencePaper", PublicationStatus.CONFERENCE_PAPER.value
        if "报纸" in body:
            return "Newspaper", PublicationStatus.OTHER.value
        if "期刊" in body:
            status = (
                PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value
                if any(marker in body for marker in ("同行评议", "peer reviewed", "peer-reviewed"))
                else PublicationStatus.UNKNOWN.value
            )
            return "JournalArticle", status
        return UNKNOWN, PublicationStatus.UNKNOWN.value

    @classmethod
    def check_fulltext_access_html(
        cls,
        html: str,
        *,
        source_url: str,
        challenge_state: ChallengeState | None = None,
    ) -> AccessDecision:
        cls.detect_interruption(html, url=source_url, challenge_state=challenge_state)
        parser = cls._parser(html)
        candidates: list[tuple[int, int, int, _Anchor, str, FullTextFormat]] = []
        for index, anchor in enumerate(parser.anchors):
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
            if absolute != UNKNOWN:
                if absolute.casefold().startswith("javascript:"):
                    absolute = UNKNOWN
                elif not _is_cnki_host(urlsplit(absolute).hostname):
                    continue
            candidate_format = _cnki_full_text_format(label, anchor.href)
            candidates.append(
                (
                    _cnki_full_text_preference(candidate_format),
                    _cnki_download_layout_priority(anchor, absolute),
                    index,
                    anchor,
                    absolute,
                    candidate_format,
                )
            )
        if not candidates:
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.METADATA_ONLY,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="No enabled, official single-paper CNKI full-text control was present",
                full_text_format=FullTextFormat.UNKNOWN,
            )
        _preference, _layout_priority, _index, candidate, candidate_url, candidate_format = sorted(
            candidates,
            key=lambda item: item[:3],
        )[0]
        body = parser.visible_body_text.casefold()
        explicit_access_block = any(marker.casefold() in body for marker in _CNKI_EXPLICIT_FULLTEXT_BLOCK_MARKERS)
        open_access = any(marker in body for marker in ("开放获取", "open access", "public full text"))
        explicit_institutional_access = any(
            marker.casefold() in body
            for marker in ("当前机构已获得全文访问权限", "机构已获得全文访问权限", "institutional access")
        )
        institution_header_authenticated = (
            parser.institution_login_status and bool(parser.authenticated_institution_labels)
        )
        resource_scoped_action = _is_cnki_resource_order_url(candidate_url)
        institutional_access = explicit_institutional_access or (
            institution_header_authenticated and resource_scoped_action
        )
        direct_public_file = candidate_url != UNKNOWN and urlsplit(candidate_url).path.casefold().endswith(
            (".pdf", ".caj", ".nh", ".kdh")
        )
        positive_access = open_access or institutional_access or direct_public_file
        locator_label = re.sub(r"\s+", " ", candidate.text).strip()
        if positive_access and explicit_access_block:
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.UNKNOWN,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="CNKI exposed conflicting current-article full-text access and authorization-block signals; FULLTEXT_ACCESS_UNKNOWN",
                download_url=candidate_url,
                download_locator=locator_label,
                full_text_format=candidate_format,
            )
        if not positive_access and explicit_access_block:
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.METADATA_ONLY,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="The current CNKI article exposed an explicit full-text authorization block without an authorized access signal",
                download_url=candidate_url,
                download_locator=locator_label,
                full_text_format=candidate_format,
            )
        if not positive_access:
            return AccessDecision(
                full_text_accessible=False,
                access_type=AccessType.UNKNOWN,
                authorized_access=False,
                status=RunStatus.FULLTEXT_NOT_AUTHORIZED,
                reason="CNKI exposed an enabled single-paper full-text control but no unambiguous authorization state; FULLTEXT_ACCESS_UNKNOWN",
                download_url=candidate_url,
                download_locator=locator_label,
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
            download_locator=locator_label,
            full_text_format=candidate_format,
        )

    @classmethod
    def check_fulltext_access_snapshot(
        cls,
        snapshot: str,
        *,
        source_url: str,
        challenge_state: ChallengeState | None = None,
    ) -> AccessDecision:
        """Verify a single-paper CNKI full-text control in a structured snapshot."""

        cls.enforce_challenge_state(resolve_challenge_state(runtime_state=challenge_state))
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
            full_text_format = _cnki_full_text_format(compact, href)
            preference = _cnki_full_text_preference(full_text_format)
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
    def search_detail_title_key(title: str) -> str:
        """The title the workflow's search/detail lock compares for CNKI.

        The result parser drops whitespace between two CJK ideographs, which
        newspaper headlines use to separate phrases (``甲乙  丙丁``), while the
        article parser keeps the h1's spacing, so one record reaches the lock
        in two spellings.  Both sides pass through the result parser's own
        spacing rule before ``normalize_title``; a different character, a
        subtitle, or the spacing between Latin words still has to match.
        """

        return normalize_title(_normalize_cnki_observed_title_spacing(title or ""))

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
        except ObservationUnavailable as html_error:
            try:
                observation = await self.browser.execute(
                    ObserveCommand(
                        include_html=False,
                        include_visible_text=False,
                        text_probes=probes,
                    )
                )
            except ObservationUnavailable:
                raise _CNKIObservationUnreadable(
                    "CNKI HTML observation was unavailable and the fallback structured "
                    "browser snapshot observation was also unavailable"
                ) from html_error
            await self._inspect_live_challenge(observation)
            snapshot = observation.structured_content
            if not isinstance(snapshot, str) or not snapshot.strip():
                raise _CNKIObservationUnreadable(
                    "CNKI HTML observation was unavailable and the fallback structured "
                    "browser snapshot contained no usable content"
                ) from html_error
            return "snapshot", snapshot, observation.url
        await self._inspect_live_challenge(observation)
        return "html", observation.require_html(), observation.url

    @classmethod
    def _search_outcome_is_stable(cls, content_kind: str, content: str) -> bool:
        """Return whether CNKI has rendered an explicit terminal search state."""

        text = cls._parser(content).visible_body_text if content_kind == "html" else content
        compact = re.sub(r"\s+", "", html_lib.unescape(text)).casefold()
        if re.search(r"共(?:为您)?找到(?:约)?[\d,，]+(?:条|篇|项)?(?:结果|记录|文献)?", compact):
            return True
        return bool(
            re.search(
                r"(?:未找到|没有找到|未检索到|没有检索到|暂无|无符合条件的).{0,40}(?:结果|记录|文献)",
                compact,
            )
        )

    async def _settled_search_records(
        self,
        *,
        query: str,
        max_results: int,
    ) -> tuple[list[LiteratureRecord], str, frozenset[str]]:
        """Observe a bounded CNKI result window until it reaches a terminal state."""

        current_url = self.search_origin
        last_unreadable: _CNKIObservationUnreadable | None = None
        readable_observation_seen = False
        for observation_number in range(_SEARCH_SETTLE_MAX_OBSERVATIONS):
            try:
                content_kind, content, current_url = await self._content()
            except _CNKIObservationUnreadable as exc:
                last_unreadable = exc
            else:
                readable_observation_seen = True
                parser = self.parse_search_results_html if content_kind == "html" else self.parse_search_results_snapshot
                records = parser(
                    content,
                    query=query,
                    source_url=current_url,
                    max_results=max_results,
                    challenge_state=self._runtime_challenge_state(),
                )
                if records or self._search_outcome_is_stable(content_kind, content):
                    same_page_navigation_urls: set[str] = set()
                    if content_kind == "html":
                        page = self._parser(content)
                        for index, anchor in enumerate(page.anchors):
                            absolute = urljoin(current_url, anchor.href)
                            if _cnki_result_opens_new_tab(anchor, absolute):
                                same_page_navigation_urls.add(absolute)
                            row_key = page.result_row_key(index)
                            if row_key and _is_cnki_detail_url(absolute):
                                self._result_row_keys[absolute] = row_key
                    return records, current_url, frozenset(same_page_navigation_urls)
            if observation_number + 1 < _SEARCH_SETTLE_MAX_OBSERVATIONS:
                await asyncio.sleep(_SEARCH_SETTLE_DELAY_SECONDS)
        if not readable_observation_seen and last_unreadable is not None:
            raise last_unreadable
        if not _is_cnki_host(urlsplit(current_url).hostname):
            raise SourceUnavailable("CNKI search navigation did not reach an official CNKI host")
        raise SourceUnavailable(
            "CNKI search results did not reach a terminal state after bounded observation"
        )

    async def search(self, query: str, request: LiteratureSearchRequest) -> list[LiteratureRecord]:
        mode, search_term = self._search_input(query, request)
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        self._search_inputs[query] = (mode, search_term)
        await self.browser.execute(NavigateCommand(self.build_search_url(search_term, mode=mode)))
        result_limit = min(request.max_results_per_source, request.max_search_results)
        parse_limit = (
            30
            if mode == "exact_title"
            else result_limit
        )
        results, current_url, _same_page_navigation_urls = await self._settled_search_records(
            query=query,
            max_results=parse_limit,
        )
        if mode == "exact_title":
            wanted = _canonicalize_cnki_title_identity(search_term)
            exact_matches = [
                record for record in results if _canonicalize_cnki_title_identity(record.title) == wanted
            ]
            if exact_matches:
                results = exact_matches
            else:
                # CNKI occasionally replaces a subtitle dash with a colon in
                # the rendered title (for example ``——基于`` vs ``：基于``).
                # Allow one bounded punctuation-equivalent result to proceed;
                # the fresh detail-page identity lock remains authoritative.
                normalized_wanted = normalize_title(search_term)
                normalized_matches = [
                    record for record in results if normalize_title(record.title) == normalized_wanted
                ]
                results = normalized_matches if len(normalized_matches) == 1 else []
        results = results[:result_limit]
        if not results and not _is_cnki_host(urlsplit(current_url).hostname):
            raise SourceUnavailable("CNKI search navigation did not reach an official CNKI host")
        return results

    def _relock_candidate(
        self,
        record: LiteratureRecord,
        fresh_records: list[LiteratureRecord],
        row_key: str,
    ) -> LiteratureRecord | None:
        """The fresh result ``record`` relocks onto, or ``None``.

        ``identity_matches`` decides, as before.  When ``record`` was read from a
        kns8s row with its own CNKI key, a fresh row carrying a different key is
        a different record under the same title -- one newspaper headline can
        head several articles -- so it is never chosen, and the row carrying the
        same key is.  A fresh row without a key is judged by identity alone.
        """

        matches = [candidate for candidate in fresh_records if self.identity_matches(record, candidate)[0]]
        if row_key:
            keyed = [
                (candidate, self._result_row_keys.get(candidate.navigation_url, ""))
                for candidate in matches
            ]
            same_row = [candidate for candidate, key in keyed if key == row_key]
            if same_row:
                return same_row[0]
            matches = [candidate for candidate, key in keyed if not key]
        return matches[0] if matches else None

    async def open_result(self, record: LiteratureRecord) -> None:
        target_url = record.navigation_url if record.navigation_url != UNKNOWN else record.source_page
        if not _is_cnki_detail_url(target_url):
            raise SourceLayoutChanged("Result URL is not a stable official CNKI detail page")
        self._expected_record = record
        self._detail_record = None
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        # CNKI result-detail URLs carry short-lived signed query parameters.
        # Reusing one after screening can return a genuine CNKI 404 even though
        # the record still exists.  Refresh the exact-title result page and use
        # only the newly observed link instead of replaying the stale URL.
        row_key = self._result_row_keys.get(record.navigation_url, "")
        exact_title_query = _canonicalize_cnki_exact_query(record.title)
        await self.browser.execute(NavigateCommand(self.build_search_url(exact_title_query, mode="exact_title")))
        fresh_records, _current_url, same_page_navigation_urls = await self._settled_search_records(
            query=record.search_query if record.search_query != UNKNOWN else record.title,
            max_results=30,
        )
        fresh_record = self._relock_candidate(record, fresh_records, row_key)
        original_search = self._search_inputs.get(record.search_query)
        if fresh_record is None and original_search is not None and original_search[0] != "exact_title":
            # Some CNKI titles are unreachable by an exact-title query even though
            # the record exists -- e.g. "“互联网+”为什么加出了业绩", where the
            # nested quotes around "+" return zero hits.  When the record was
            # found by a different search, refresh once with that same search.
            # The relock rule is unchanged: identity_matches must still hold.
            mode, term = original_search
            await self.browser.execute(NavigateCommand(self.build_search_url(term, mode=mode)))
            fresh_records, _current_url, same_page_navigation_urls = await self._settled_search_records(
                query=record.search_query,
                max_results=30,
            )
            fresh_record = self._relock_candidate(record, fresh_records, row_key)
            if (
                fresh_record is not None
                and _is_cnki_detail_url(fresh_record.navigation_url)
                and urlsplit(fresh_record.navigation_url).path.casefold().startswith(_CNKI_2026_DETAIL_PATH)
            ):
                # Follow the fresh 2026 detail link in-page, as the exact-title path
                # does.  A click on this result page can leave a second tab on the
                # same article, which the page-identity gate then refuses to act on.
                same_page_navigation_urls = frozenset(same_page_navigation_urls | {fresh_record.navigation_url})
        if fresh_record is None:
            raise SourceLayoutChanged("CNKI exact-title refresh could not relock the requested result")
        if fresh_record.navigation_url in same_page_navigation_urls:
            # The current result table deliberately opens article links in a
            # new tab.  Follow the freshly parsed absolute href on the bound
            # page instead, then require an official CNKI detail-page landing.
            navigation_result = await self.browser.execute(
                NavigateCommand(fresh_record.navigation_url)
            )
            final_url = str(getattr(navigation_result, "url", ""))
            if not _is_cnki_detail_url(final_url):
                raise SourceLayoutChanged(
                    "CNKI result navigation remained outside an official detail page "
                    "after same-page navigation"
                )
            return
        # CNKI's HTML result parser can preserve nonsemantic whitespace beside
        # a dash while the accessibility label exposes the same title without
        # it.  Use the restricted exact-query canonical form for the click
        # target too; the multi-field identity lock above remains authoritative.
        click_titles = tuple(
            dict.fromkeys(
                (
                    _canonicalize_cnki_exact_query(fresh_record.title),
                    _normalize_cnki_observed_title_spacing(fresh_record.title),
                    fresh_record.title.replace(":", "："),
                    fresh_record.title.replace("？", "?"),
                    fresh_record.title,
                    record.title.replace(":", "：") if record.title != UNKNOWN else record.title,
                    record.title.replace("？", "?") if record.title != UNKNOWN else record.title,
                    record.title,
                )
            )
        )
        # CNKI can expose the result row in the settled HTML before the
        # accessibility snapshot has attached an executable link reference.
        # Give that bounded result window one short settle interval before the
        # first exact-title click; the retry below remains fail-closed.
        await asyncio.sleep(_CNKI_CLICK_RETRY_DELAY_SECONDS)
        last_find_error: BrowserCommandError | None = None
        for attempt in range(_CNKI_CLICK_RETRIES + 1):
            for click_title in click_titles:
                try:
                    click_result = await self.browser.execute(
                        ClickCommand(
                            BrowserTarget(text=click_title, exact_text=True),
                            follow_new_page=True,
                            close_origin_when_sole_page=True,
                        )
                    )
                    # Retain the bounded navigation fallback for legacy result
                    # layouts.  The current new-tab result table has already
                    # taken the explicit same-page path above.
                    clicked_url = getattr(click_result, "url", "")
                    if not _is_cnki_detail_url(str(clicked_url)):
                        navigation_result = await self.browser.execute(
                            NavigateCommand(fresh_record.navigation_url)
                        )
                        final_url = str(getattr(navigation_result, "url", ""))
                        if not _is_cnki_detail_url(final_url):
                            raise SourceLayoutChanged(
                                "CNKI result navigation remained outside an official detail page "
                                "after the fresh-link fallback"
                            )
                    return
                except BrowserCommandError as exc:
                    if "browser_find returned no executable snapshot ref" not in str(exc):
                        raise
                    last_find_error = exc
            if attempt < _CNKI_CLICK_RETRIES:
                await asyncio.sleep(_CNKI_CLICK_RETRY_DELAY_SECONDS)
        if last_find_error is not None:
            raise last_find_error

    async def extract_metadata(self, *, search_query: str) -> LiteratureRecord:
        content_kind, content, current_url = await self._content()
        parser = self.parse_article_html if content_kind == "html" else self.parse_article_snapshot
        record = parser(
            content,
            source_url=current_url,
            search_query=search_query,
            challenge_state=self._runtime_challenge_state(),
        )
        if self._expected_record is not None:
            matches, reason = self.identity_matches(self._expected_record, record)
            if not matches:
                raise SourceLayoutChanged(f"CNKI target identity lock failed: {reason}")
            record.paper_id = self._expected_record.paper_id
            record.canonical_paper_id = self._expected_record.paper_id
            record.target_identity_confirmed = True
            if (
                record.navigation_url == UNKNOWN
                and self._expected_record.navigation_url in self._result_row_keys
            ):
                # Keep the result link that reached this page, so reopening this
                # record for its download relocks the same result row.
                record.navigation_url = self._expected_record.navigation_url
        self._detail_record = record
        return record

    async def extract_abstract(self) -> str:
        content_kind, content, current_url = await self._content()
        parser = self.parse_article_html if content_kind == "html" else self.parse_article_snapshot
        return parser(
            content,
            source_url=current_url,
            challenge_state=self._runtime_challenge_state(),
        ).abstract

    async def check_fulltext_access(self) -> AccessDecision:
        last_unreadable: _CNKIObservationUnreadable | None = None
        readable_observation_seen = False
        decision: AccessDecision | None = None
        for observation_number in range(_ACCESS_SETTLE_MAX_OBSERVATIONS):
            try:
                content_kind, content, current_url = await self._content()
            except _CNKIObservationUnreadable as exc:
                last_unreadable = exc
            else:
                readable_observation_seen = True
                article_parser = self.parse_article_html if content_kind == "html" else self.parse_article_snapshot
                if self._expected_record is not None:
                    detail = article_parser(
                        content,
                        source_url=current_url,
                        search_query=self._expected_record.search_query,
                        challenge_state=self._runtime_challenge_state(),
                    )
                    matches, reason = self.identity_matches(self._expected_record, detail)
                    if not matches:
                        raise SourceLayoutChanged(f"CNKI target identity lock failed before access check: {reason}")
                access_parser = self.check_fulltext_access_html if content_kind == "html" else self.check_fulltext_access_snapshot
                decision = access_parser(
                    content,
                    source_url=current_url,
                    challenge_state=self._runtime_challenge_state(),
                )
                if not _access_decision_is_settling(decision):
                    return decision
            if observation_number + 1 < _ACCESS_SETTLE_MAX_OBSERVATIONS:
                await asyncio.sleep(_ACCESS_SETTLE_DELAY_SECONDS)
        if not readable_observation_seen and last_unreadable is not None:
            raise last_unreadable
        assert decision is not None
        if (
            decision.access_type is AccessType.UNKNOWN
            and not decision.full_text_accessible
            and not decision.authorized_access
        ):
            return dataclasses.replace(
                decision,
                status=RunStatus.SOURCE_LAYOUT_CHANGED,
                reason=f"ACCESS_READINESS_TIMEOUT: {decision.reason}",
            )
        return decision

    async def download_fulltext(self, record: LiteratureRecord, access: AccessDecision) -> Path:
        if not access.full_text_accessible or not access.authorized_access:
            raise PermissionError("FULLTEXT_NOT_AUTHORIZED")
        content_kind, content, current_url = await self._content()
        parser = self.parse_article_html if content_kind == "html" else self.parse_article_snapshot
        detail = parser(
            content,
            source_url=current_url,
            search_query=record.search_query,
            challenge_state=self._runtime_challenge_state(),
        )
        matches, reason = self.identity_matches(record, detail)
        if not matches:
            raise SourceLayoutChanged(f"CNKI target identity lock failed before download: {reason}")
        record.target_identity_confirmed = True
        if self.browser is None:
            raise SourceUnavailable("Browser command port is unavailable")
        label = access.download_locator
        if label in ("", UNKNOWN) or any(marker in label for marker in _REJECT_DOWNLOAD_LABELS):
            raise SourceLayoutChanged("No safe single-paper CNKI download control was locked")
        # Authorization, identity lock, and control vetting have passed; budget
        # the fetch before the click is issued.  CNKI's own stable identifier
        # is the ledger key, with the normalized DOI as the fallback.
        ticket = self.authorize_publisher_fetch(record)
        try:
            suffix = {FullTextFormat.PDF: ".pdf", FullTextFormat.CAJ: ".caj"}.get(access.full_text_format, ".bin")
            artifact = await self.browser.execute(
                DownloadCommand(
                    target=BrowserTarget(
                        text=label,
                        exact_text=True,
                    ),
                    suggested_filename=f"{record.paper_id}{suffix}",
                    identity_labels=(record.title,)
                    if record.title not in ("", UNKNOWN) and record.title.strip()
                    else (),
                )
            )
        except Exception as exc:
            ticket.record_outcome(ok=False, detail=str(exc).strip() or type(exc).__name__)
            exception_detail = str(exc)
            if len(exception_detail) > 500:
                exception_detail = f"{exception_detail[:497]}..."
            raise SourceUnavailable(
                f"Authorized CNKI control did not produce a browser download: "
                f"{type(exc).__name__}: {exception_detail}"
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
