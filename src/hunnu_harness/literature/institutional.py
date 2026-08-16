from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from .adapters.base import (
    LiteratureSourceAdapter,
    SourceActionRequired,
    SourceLayoutChanged,
    SourceUnavailable,
)
from .models import AccessDecision, AccessType, LiteratureRecord, UNKNOWN
from .normalization import normalize_doi, normalize_title
from .security import sanitize_url


HUNNU_INSTITUTION_NAME = "湖南师范大学"
HUNNU_OFFICIAL_PORTAL = "https://www.hunnu.edu.cn/"
HUNNU_LIBRARY_HOME = "https://lib.hunnu.edu.cn/"


class InstitutionalResolutionTrigger(str, Enum):
    """Auditable reasons that permit institutional-route fallback.

    A known negative authorization decision is deliberately absent. Callers must
    not use this resolver merely because a publisher says the item is not
    licensed.
    """

    INSTITUTIONAL_LOGIN_ENTRY_NOT_FOUND = "InstitutionalLoginEntryFound=false"
    ACCESS_ROUTE_UNKNOWN = "InstitutionalAccessRouteUnknown=true"
    DIRECT_ROUTE_UNAVAILABLE = "DirectInstitutionalRouteUnavailable=true"
    FULL_TEXT_STATUS_UNKNOWN = "FullTextAccessStatus=Unknown"


@dataclass(frozen=True)
class InstitutionalRouteCandidate:
    display_name: str
    navigation_url: str = field(repr=False)
    stable_url: str
    domain: str
    match_basis: str
    confidence: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "DisplayName": self.display_name or UNKNOWN,
            "StableURL": self.stable_url,
            "Domain": self.domain or UNKNOWN,
            "MatchBasis": self.match_basis,
            "Confidence": self.confidence,
        }


@dataclass(frozen=True)
class InstitutionalRouteStep:
    sequence: int
    label: str
    page_title: str
    official_domain: str
    stable_url: str
    route_result: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {
            "Sequence": self.sequence,
            "Label": self.label,
            "PageTitle": self.page_title or UNKNOWN,
            "OfficialDomain": self.official_domain or UNKNOWN,
            "StableURL": self.stable_url,
            "Timestamp": self.timestamp,
            "RouteResult": self.route_result,
        }


@dataclass(frozen=True)
class InstitutionalRouteResult:
    requested_source: str
    resolution_trigger: InstitutionalResolutionTrigger
    institutional_route_resolved: bool
    institutional_target_database_match: bool | str
    full_text_access_rechecked: bool = False
    full_text_accessible: bool | None = None
    access_type: str = AccessType.UNKNOWN.value
    route_result: str = UNKNOWN
    reason: str = UNKNOWN
    route_steps: tuple[InstitutionalRouteStep, ...] = ()
    publisher_entry_url: str = field(default=UNKNOWN, repr=False)
    publisher_navigation_url: str = field(default=UNKNOWN, repr=False)
    institution: str = HUNNU_INSTITUTION_NAME
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def with_access_decision(self, access: AccessDecision) -> "InstitutionalRouteResult":
        return replace(
            self,
            full_text_access_rechecked=True,
            full_text_accessible=access.full_text_accessible,
            access_type=access.access_type.value,
            reason=access.reason,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "InstitutionalRouteUsed": self.institutional_route_resolved,
            "InstitutionalRouteResolved": self.institutional_route_resolved,
            "Institution": self.institution,
            "RequestedSource": self.requested_source,
            "ResolutionTrigger": self.resolution_trigger.value,
            "InstitutionalTargetDatabaseMatch": self.institutional_target_database_match,
            "FullTextAccessRechecked": self.full_text_access_rechecked,
            "FullTextAccessible": (
                self.full_text_accessible if self.full_text_accessible is not None else UNKNOWN
            ),
            "AccessType": self.access_type,
            "RouteResult": self.route_result,
            "Reason": self.reason,
            "GeneratedAt": self.generated_at,
            "RouteSteps": [step.as_dict() for step in self.route_steps],
        }


@dataclass(frozen=True)
class _Anchor:
    href: str
    text: str
    attributes: dict[str, str]


class _RouteHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[_Anchor] = []
        self._anchor_href: str | None = None
        self._anchor_attributes: dict[str, str] = {}
        self._anchor_text: list[str] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self._body_parts: list[str] = []
        self.has_password_input = False
        self.has_auth_form = False
        self.has_challenge_control = False

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._title_parts)).strip()

    @property
    def body_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._body_parts)).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): (value or "") for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "a":
            self._anchor_href = values.get("href", "")
            self._anchor_attributes = values
            self._anchor_text = []
        elif lowered == "title":
            self._in_title = True
        elif lowered == "input":
            identity = " ".join(
                values.get(name, "") for name in ("type", "name", "id", "class", "placeholder")
            ).casefold()
            if values.get("type", "").casefold() == "password":
                self.has_password_input = True
            if any(marker in identity for marker in ("captcha", "verifycode", "verification", "otp", "mfa")):
                self.has_challenge_control = True
        elif lowered == "form":
            action = values.get("action", "").casefold()
            if any(marker in action for marker in ("authserver", "/cas", "/login", "/signin", "saml", "oauth")):
                self.has_auth_form = True

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "a" and self._anchor_href is not None:
            text = re.sub(r"\s+", " ", " ".join(self._anchor_text)).strip()
            self.anchors.append(_Anchor(self._anchor_href, text, self._anchor_attributes))
            self._anchor_href = None
            self._anchor_attributes = {}
            self._anchor_text = []
        elif lowered == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if not value:
            return
        self._body_parts.append(value)
        if self._in_title:
            self._title_parts.append(value)
        if self._anchor_href is not None:
            self._anchor_text.append(value)


def _normalized_label(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def _is_hunnu_domain(host: str) -> bool:
    host = host.casefold().rstrip(".")
    return host == "hunnu.edu.cn" or host.endswith(".hunnu.edu.cn")


class InstitutionalAccessResolver(ABC):
    """Adapter-independent fallback route with an independent access recheck."""

    @abstractmethod
    async def resolve(
        self,
        requested_source: str,
        *,
        trigger: InstitutionalResolutionTrigger,
    ) -> InstitutionalRouteResult:
        raise NotImplementedError

    async def resolve_and_recheck(
        self,
        adapter: LiteratureSourceAdapter,
        record: LiteratureRecord,
        *,
        trigger: InstitutionalResolutionTrigger,
    ) -> tuple[InstitutionalRouteResult, AccessDecision]:
        route = await self.resolve(adapter.name, trigger=trigger)
        if not route.institutional_route_resolved or route.institutional_target_database_match is not True:
            raise SourceLayoutChanged(
                "Institutional route was not resolved with an unambiguous target database lock"
            )

        bind_route = getattr(adapter, "bind_institutional_route", None)
        if callable(bind_route):
            bind_route(route)

        await adapter.open_result(record)
        current = await adapter.extract_metadata(search_query=record.search_query)
        if not self.records_match_identity(record, current):
            record.target_identity_confirmed = False
            raise SourceLayoutChanged(
                "Target paper identity changed after institutional routing; download stopped"
            )
        record.target_identity_confirmed = True
        access = await adapter.check_fulltext_access()
        return route.with_access_decision(access), access

    @staticmethod
    def records_match_identity(expected: LiteratureRecord, actual: LiteratureRecord) -> bool:
        expected_doi = normalize_doi(expected.doi)
        actual_doi = normalize_doi(actual.doi)
        if expected_doi != UNKNOWN or actual_doi != UNKNOWN:
            return expected_doi != UNKNOWN and expected_doi == actual_doi
        expected_title = normalize_title(expected.title)
        actual_title = normalize_title(actual.title)
        if expected_title == UNKNOWN or actual_title == UNKNOWN or expected_title != actual_title:
            return False
        if expected.stable_identifier != UNKNOWN and actual.stable_identifier != UNKNOWN:
            return expected.stable_identifier == actual.stable_identifier
        return True


class HUNNUInstitutionalAccessResolver(InstitutionalAccessResolver):
    """Navigate only visible HUNNU pages to an explicitly requested database."""

    _SOURCE_ALIASES = {
        "SpringerLink": (
            "SpringerLink",
            "Springer Link",
            "Springer Nature Link",
            "Springer Nature",
            "Springer电子期刊",
            "Springer 电子期刊",
        ),
        "OxfordAcademic": (
            "Oxford Academic",
            "OxfordAcademic",
            "Oxford Journals",
            "Oxford Journals Collection",
            "Oxford University Press",
            "OUP",
            "Oxford Journals Collection牛津期刊现刊库",
            "牛津期刊现刊库",
        ),
    }
    _SOURCE_DOMAINS = {
        "SpringerLink": ("link.springer.com",),
        "OxfordAcademic": ("academic.oup.com",),
    }
    _TRUSTED_LIBRARY_SERVICE_DOMAINS = (
        "wisdom.chaoxing.com",
        "hunnulib.mh.chaoxing.com",
    )
    _HUNNU_GATEWAY_DOMAINS = ("yclib.hunnu.edu.cn",)
    _LIBRARY_MARKERS = ("湖南师范大学图书馆", "图书馆", "library")
    _RESOURCE_MARKERS = (
        "外文数据库",
        "数字资源",
        "电子资源",
        "数据库导航",
        "资源导航",
        "数据库",
    )

    def __init__(
        self,
        browser: Any,
        *,
        portal_url: str = HUNNU_OFFICIAL_PORTAL,
        library_url: str = HUNNU_LIBRARY_HOME,
    ) -> None:
        self.browser = getattr(browser, "backend", browser)
        self.portal_url = sanitize_url(portal_url)
        self.library_url = sanitize_url(library_url)
        if not _is_hunnu_domain(_hostname(self.portal_url)):
            raise ValueError("HUNNU portal must use an official hunnu.edu.cn domain")
        if not _is_hunnu_domain(_hostname(self.library_url)):
            raise ValueError("HUNNU library must use an official hunnu.edu.cn domain")

    @classmethod
    def canonical_source(cls, requested_source: str) -> str:
        requested = _normalized_label(requested_source)
        for canonical, aliases in cls._SOURCE_ALIASES.items():
            if requested in {_normalized_label(alias) for alias in aliases}:
                return canonical
        raise SourceLayoutChanged(f"Unsupported institutional target source: {requested_source}")

    @staticmethod
    def is_official_hunnu_url(url: str) -> bool:
        parsed = urlsplit(url)
        return parsed.scheme in {"http", "https"} and _is_hunnu_domain(parsed.hostname or "")

    @classmethod
    def is_trusted_hunnu_route_url(cls, url: str) -> bool:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        return parsed.scheme in {"http", "https"} and (
            _is_hunnu_domain(host) or host in cls._TRUSTED_LIBRARY_SERVICE_DOMAINS
        )

    @classmethod
    def is_hunnu_gateway_url(cls, url: str) -> bool:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        return (
            parsed.scheme in {"http", "https"}
            and host in cls._HUNNU_GATEWAY_DOMAINS
            and parsed.path.casefold().startswith("/vpn/")
        )

    @classmethod
    def is_official_source_url(cls, requested_source: str, url: str) -> bool:
        canonical = cls.canonical_source(requested_source)
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        return parsed.scheme in {"http", "https"} and host in cls._SOURCE_DOMAINS[canonical]

    @classmethod
    def source_identity_matches_text(cls, requested_source: str, text: str) -> bool:
        canonical = cls.canonical_source(requested_source)
        evidence = _normalized_label(text)
        return any(
            _normalized_label(alias) in evidence
            for alias in cls._SOURCE_ALIASES[canonical]
        )

    @classmethod
    def is_verified_source_destination(
        cls,
        requested_source: str,
        url: str,
        *,
        page_title: str,
    ) -> bool:
        if cls.is_official_source_url(requested_source, url):
            return True
        return cls.is_hunnu_gateway_url(url) and cls.source_identity_matches_text(
            requested_source, page_title
        )

    @classmethod
    def provenance_url(cls, url: str) -> str:
        """Keep route evidence stable without serializing opaque gateway paths."""

        if cls.is_hunnu_gateway_url(url):
            parsed = urlsplit(url)
            return f"{parsed.scheme}://{parsed.hostname}/vpn/"
        return sanitize_url(url)

    @classmethod
    def _parse(cls, html: str) -> _RouteHTMLParser:
        parser = _RouteHTMLParser()
        parser.feed(html)
        return parser

    @classmethod
    def detect_manual_authentication(cls, html: str, *, url: str = "") -> None:
        parser = cls._parse(html)
        normalized_body = _normalized_label(f"{parser.title} {parser.body_text}")
        normalized_url = url.casefold()
        captcha = parser.has_challenge_control or any(
            marker in normalized_body
            for marker in (
                "请完成验证码",
                "请输入验证码",
                "拖动滑块",
                "安全验证",
                "verifyyouarehuman",
                "recaptcha",
            )
        )
        if captcha:
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=CAPTCHA or human verification required; "
                "BrowserReadyForManualAction=true"
            )

        auth_host_or_path = any(
            marker in normalized_url
            for marker in ("authserver", "/cas/", "/login", "/signin", "/saml", "/oauth")
        )
        second_factor = any(
            marker in normalized_body
            for marker in ("短信验证码", "二次认证", "双重验证", "onetimepassword", "multifactorauthentication")
        )
        if parser.has_password_input or parser.has_auth_form or auth_host_or_path or second_factor:
            raise SourceActionRequired(
                "ACTION_REQUIRED_USER_LOGIN=true; Reason=HUNNU or database manual authentication required; "
                "BrowserReadyForManualAction=true"
            )

    @classmethod
    def discover_library_entries(cls, html: str, *, base_url: str) -> list[InstitutionalRouteCandidate]:
        return cls._discover_hunnu_entries(
            html,
            base_url=base_url,
            markers=cls._LIBRARY_MARKERS,
            basis="HUNNU library name",
        )

    @classmethod
    def discover_resource_entries(cls, html: str, *, base_url: str) -> list[InstitutionalRouteCandidate]:
        return cls._discover_hunnu_entries(
            html,
            base_url=base_url,
            markers=cls._RESOURCE_MARKERS,
            basis="HUNNU visible resource label",
        )

    @classmethod
    def _discover_hunnu_entries(
        cls,
        html: str,
        *,
        base_url: str,
        markers: Iterable[str],
        basis: str,
    ) -> list[InstitutionalRouteCandidate]:
        candidates: list[InstitutionalRouteCandidate] = []
        normalized_markers = tuple(_normalized_label(marker) for marker in markers)
        for anchor in cls._parse(html).anchors:
            navigation_url = urljoin(base_url, anchor.href)
            if not cls.is_trusted_hunnu_route_url(navigation_url):
                continue
            evidence = " ".join(
                (
                    anchor.text,
                    anchor.attributes.get("title", ""),
                    anchor.attributes.get("aria-label", ""),
                )
            )
            normalized_evidence = _normalized_label(evidence)
            matches = [marker for marker in normalized_markers if marker and marker in normalized_evidence]
            if not matches:
                continue
            candidates.append(
                InstitutionalRouteCandidate(
                    display_name=re.sub(r"\s+", " ", evidence).strip(),
                    navigation_url=navigation_url,
                    stable_url=sanitize_url(navigation_url),
                    domain=_hostname(navigation_url),
                    match_basis=basis,
                    confidence=80 + max(len(marker) for marker in matches),
                )
            )
        return cls._deduplicate_candidates(candidates)

    @classmethod
    def discover_database_entries(
        cls,
        html: str,
        *,
        base_url: str,
        requested_source: str,
    ) -> list[InstitutionalRouteCandidate]:
        canonical = cls.canonical_source(requested_source)
        aliases = tuple(_normalized_label(alias) for alias in cls._SOURCE_ALIASES[canonical])
        target_domains = cls._SOURCE_DOMAINS[canonical]
        candidates: list[InstitutionalRouteCandidate] = []
        for anchor in cls._parse(html).anchors:
            navigation_url = urljoin(base_url, anchor.href)
            host = _hostname(navigation_url)
            evidence = " ".join(
                (
                    anchor.text,
                    anchor.attributes.get("title", ""),
                    anchor.attributes.get("aria-label", ""),
                    anchor.attributes.get("data-name", ""),
                )
            )
            normalized_evidence = _normalized_label(evidence)
            alias_match = any(alias and alias in normalized_evidence for alias in aliases)
            domain_match = host in target_domains
            hunnu_detail = cls.is_trusted_hunnu_route_url(navigation_url)
            if not domain_match and not (alias_match and hunnu_detail):
                continue
            confidence = 120 if alias_match and domain_match else 100 if alias_match else 90
            basis_parts = []
            if alias_match:
                basis_parts.append("database-name")
            if domain_match:
                basis_parts.append("official-domain")
            if hunnu_detail and not domain_match:
                basis_parts.append("HUNNU-detail")
            candidates.append(
                InstitutionalRouteCandidate(
                    display_name=re.sub(r"\s+", " ", evidence).strip() or canonical,
                    navigation_url=navigation_url,
                    stable_url=sanitize_url(navigation_url),
                    domain=host,
                    match_basis="+".join(basis_parts),
                    confidence=confidence,
                )
            )
        return cls._deduplicate_candidates(candidates)

    @classmethod
    def discover_gateway_entries(
        cls,
        html: str,
        *,
        base_url: str,
    ) -> list[InstitutionalRouteCandidate]:
        candidates: list[InstitutionalRouteCandidate] = []
        for anchor in cls._parse(html).anchors:
            navigation_url = urljoin(base_url, anchor.href)
            if not cls.is_hunnu_gateway_url(navigation_url):
                continue
            evidence = " ".join(
                (
                    anchor.text,
                    anchor.attributes.get("title", ""),
                    anchor.attributes.get("aria-label", ""),
                )
            )
            candidates.append(
                InstitutionalRouteCandidate(
                    display_name=re.sub(r"\s+", " ", evidence).strip() or "HUNNU institutional gateway",
                    navigation_url=navigation_url,
                    stable_url=cls.provenance_url(navigation_url),
                    domain=_hostname(navigation_url),
                    match_basis="HUNNU-visible-gateway",
                    confidence=110,
                )
            )
        return cls._deduplicate_candidates(candidates)

    @staticmethod
    def _deduplicate_candidates(
        candidates: Iterable[InstitutionalRouteCandidate],
    ) -> list[InstitutionalRouteCandidate]:
        unique: dict[str, InstitutionalRouteCandidate] = {}
        for candidate in candidates:
            # Keep opaque gateway paths out of provenance, but retain their
            # distinction in memory so two different destinations become an
            # ambiguity instead of being silently collapsed.
            identity = sanitize_url(candidate.navigation_url)
            previous = unique.get(identity)
            if previous is None or candidate.confidence > previous.confidence:
                unique[identity] = candidate
        return sorted(
            unique.values(),
            key=lambda item: (
                -item.confidence,
                item.stable_url.casefold(),
                sanitize_url(item.navigation_url).casefold(),
            ),
        )

    @staticmethod
    def choose_candidate(
        candidates: Iterable[InstitutionalRouteCandidate],
    ) -> tuple[InstitutionalRouteCandidate | None, bool | str]:
        ordered = list(candidates)
        if not ordered:
            return None, False
        top_score = max(item.confidence for item in ordered)
        best = [item for item in ordered if item.confidence == top_score]
        if len(best) != 1:
            return None, "uncertain"
        return best[0], True

    async def _snapshot_after_goto(self, url: str) -> tuple[str, str, str]:
        await self.browser.goto(url)
        page = getattr(self.browser, "page", None)
        if page is None:
            raise SourceUnavailable("Browser page is unavailable for institutional routing")
        html = await page.content()
        current_url = str(page.url)
        title_method = getattr(page, "title", None)
        title = await title_method() if callable(title_method) else self._parse(html).title
        self.detect_manual_authentication(html, url=current_url)
        return html, current_url, title

    @staticmethod
    def _step(
        steps: list[InstitutionalRouteStep],
        *,
        label: str,
        title: str,
        url: str,
        result: str,
    ) -> None:
        steps.append(
            InstitutionalRouteStep(
                sequence=len(steps) + 1,
                label=label,
                page_title=title,
                official_domain=_hostname(url),
                stable_url=HUNNUInstitutionalAccessResolver.provenance_url(url),
                route_result=result,
            )
        )

    def _failure(
        self,
        requested_source: str,
        trigger: InstitutionalResolutionTrigger,
        steps: list[InstitutionalRouteStep],
        *,
        match: bool | str,
        reason: str,
    ) -> InstitutionalRouteResult:
        return InstitutionalRouteResult(
            requested_source=requested_source,
            resolution_trigger=trigger,
            institutional_route_resolved=False,
            institutional_target_database_match=match,
            route_result="SOURCE_LAYOUT_CHANGED",
            reason=reason,
            route_steps=tuple(steps),
        )

    async def resolve(
        self,
        requested_source: str,
        *,
        trigger: InstitutionalResolutionTrigger,
    ) -> InstitutionalRouteResult:
        canonical = self.canonical_source(requested_source)
        if not isinstance(trigger, InstitutionalResolutionTrigger):
            raise ValueError("An explicit InstitutionalResolutionTrigger is required")

        steps: list[InstitutionalRouteStep] = []
        portal_html, portal_url, portal_title = await self._snapshot_after_goto(self.portal_url)
        if not self.is_official_hunnu_url(portal_url):
            return self._failure(
                canonical,
                trigger,
                steps,
                match=False,
                reason="HUNNU official portal navigation left the official domain",
            )
        self._step(
            steps,
            label="HUNNU Official Portal",
            title=portal_title,
            url=portal_url,
            result="official portal reached",
        )

        library_candidates = self.discover_library_entries(portal_html, base_url=portal_url)
        library_entry, library_match = self.choose_candidate(library_candidates)
        if library_match == "uncertain":
            return self._failure(
                canonical,
                trigger,
                steps,
                match="uncertain",
                reason="Multiple equally ranked HUNNU library entries were found",
            )
        library_target = library_entry.navigation_url if library_entry else self.library_url
        library_html, library_url, library_title = await self._snapshot_after_goto(library_target)
        if not self.is_trusted_hunnu_route_url(library_url):
            return self._failure(
                canonical,
                trigger,
                steps,
                match=False,
                reason="HUNNU library navigation left the official domain",
            )
        self._step(
            steps,
            label="Library / Electronic Resources",
            title=library_title,
            url=library_url,
            result="official library reached",
        )

        database_candidates = self.discover_database_entries(
            library_html,
            base_url=library_url,
            requested_source=canonical,
        )
        if not database_candidates:
            resource_entries = self.discover_resource_entries(library_html, base_url=library_url)
            resource_entry, resource_match = self.choose_candidate(resource_entries)
            if resource_match == "uncertain":
                return self._failure(
                    canonical,
                    trigger,
                    steps,
                    match="uncertain",
                    reason="Multiple equally ranked HUNNU resource-navigation entries were found",
                )
            if resource_entry is None:
                return self._failure(
                    canonical,
                    trigger,
                    steps,
                    match=False,
                    reason="No visible HUNNU electronic-resource entry was found",
                )
            resource_html, resource_url, resource_title = await self._snapshot_after_goto(
                resource_entry.navigation_url
            )
            if not self.is_trusted_hunnu_route_url(resource_url):
                return self._failure(
                    canonical,
                    trigger,
                    steps,
                    match=False,
                    reason="Electronic-resource navigation left the HUNNU official domain",
                )
            self._step(
                steps,
                label="HUNNU Electronic Resources",
                title=resource_title,
                url=resource_url,
                result="resource list reached",
            )
            database_candidates = self.discover_database_entries(
                resource_html,
                base_url=resource_url,
                requested_source=canonical,
            )

        database_entry, database_match = self.choose_candidate(database_candidates)
        if database_entry is None:
            return self._failure(
                canonical,
                trigger,
                steps,
                match=database_match,
                reason=(
                    "InstitutionalTargetDatabaseMatch=uncertain"
                    if database_match == "uncertain"
                    else f"No verified HUNNU entry for {canonical} was found"
                ),
            )

        destination = database_entry.navigation_url
        if self.is_trusted_hunnu_route_url(destination) and not self.is_hunnu_gateway_url(destination):
            detail_html, detail_url, detail_title = await self._snapshot_after_goto(destination)
            self._step(
                steps,
                label=f"{canonical} Database Detail",
                title=detail_title,
                url=detail_url,
                result="target database name matched on HUNNU",
            )
            if not self.source_identity_matches_text(
                canonical,
                f"{detail_title} {self._parse(detail_html).body_text[:5000]}",
            ):
                return self._failure(
                    canonical,
                    trigger,
                    steps,
                    match=False,
                    reason="HUNNU database detail page did not identify the requested source",
                )
            publisher_entries = [
                item
                for item in self.discover_database_entries(
                    detail_html,
                    base_url=detail_url,
                    requested_source=canonical,
                )
                if self.is_official_source_url(canonical, item.navigation_url)
            ]
            publisher_entries.extend(
                self.discover_gateway_entries(detail_html, base_url=detail_url)
            )
            publisher_entry, publisher_match = self.choose_candidate(publisher_entries)
            if publisher_entry is None:
                return self._failure(
                    canonical,
                    trigger,
                    steps,
                    match=publisher_match,
                    reason="HUNNU database detail did not expose one unambiguous official publisher link",
                )
            destination = publisher_entry.navigation_url

        if not (
            self.is_official_source_url(canonical, destination)
            or self.is_hunnu_gateway_url(destination)
        ):
            return self._failure(
                canonical,
                trigger,
                steps,
                match=False,
                reason="Candidate destination did not match the requested publisher domain",
            )

        _, publisher_url, publisher_title = await self._snapshot_after_goto(destination)
        if not self.is_verified_source_destination(
            canonical,
            publisher_url,
            page_title=publisher_title,
        ):
            return self._failure(
                canonical,
                trigger,
                steps,
                match=False,
                reason="Publisher navigation did not reach the requested official domain",
            )
        self._step(
            steps,
            label=f"{canonical} / Publisher",
            title=publisher_title,
            url=publisher_url,
            result="official publisher reached through HUNNU route",
        )
        return InstitutionalRouteResult(
            requested_source=canonical,
            resolution_trigger=trigger,
            institutional_route_resolved=True,
            institutional_target_database_match=True,
            full_text_access_rechecked=False,
            full_text_accessible=None,
            route_result="SUCCESS",
            reason="HUNNU official route reached the requested publisher; source access remains unchecked",
            route_steps=tuple(steps),
            publisher_entry_url=self.provenance_url(publisher_url),
            publisher_navigation_url=publisher_url,
        )


__all__ = [
    "HUNNU_INSTITUTION_NAME",
    "HUNNU_LIBRARY_HOME",
    "HUNNU_OFFICIAL_PORTAL",
    "HUNNUInstitutionalAccessResolver",
    "InstitutionalAccessResolver",
    "InstitutionalResolutionTrigger",
    "InstitutionalRouteCandidate",
    "InstitutionalRouteResult",
    "InstitutionalRouteStep",
]
