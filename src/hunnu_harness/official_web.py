"""Bounded acquisition of public evidence from configured official websites.

OfficialWeb is deliberately separate from literature full-text acquisition. It
uses the shared browser command port, never downloads papers, and fails closed
when a destination leaves the task allowlist or presents an access gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
from urllib.parse import urlsplit

from .browser.commands import NavigateCommand, ObservationUnavailable, ObserveCommand
from .browser.port import BrowserCommandPort, ensure_browser_command_port


class OfficialityStatus(str, Enum):
    OFFICIAL_CONFIRMED = "OFFICIAL_CONFIRMED"
    OFFICIAL_PROBABLE = "OFFICIAL_PROBABLE"
    UNVERIFIED = "UNVERIFIED"


class OfficialWebStatus(str, Enum):
    SUCCESS = "SUCCESS"
    MANUAL_AUTH_REQUIRED = "MANUAL_AUTH_REQUIRED"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    UNSUPPORTED_ACCESS = "UNSUPPORTED_ACCESS"


class OfficialWebError(RuntimeError):
    status = OfficialWebStatus.UNSUPPORTED_ACCESS


class DomainNotAllowed(OfficialWebError):
    pass


class ManualAuthenticationRequired(OfficialWebError):
    status = OfficialWebStatus.MANUAL_AUTH_REQUIRED


class PublicAccessRestricted(OfficialWebError):
    status = OfficialWebStatus.ACCESS_RESTRICTED


def normalize_domain(value: str) -> str:
    raw = str(value).strip().casefold().rstrip(".")
    if "://" in raw:
        raw = (urlsplit(raw).hostname or "").casefold().rstrip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    if not raw or any(character in raw for character in " /?#@"):
        raise ValueError(f"Invalid domain: {value!r}")
    return raw


def domain_matches(host: str, allowed_domain: str) -> bool:
    normalized_host = normalize_domain(host)
    normalized_allowed = normalize_domain(allowed_domain)
    return normalized_host == normalized_allowed or normalized_host.endswith("." + normalized_allowed)


@dataclass(frozen=True)
class OfficialDomainClaim:
    domain: str
    source_type: str
    relationship: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", normalize_domain(self.domain))
        if not self.source_type.strip() or not self.relationship.strip():
            raise ValueError("Official domain claims require SourceType and Relationship")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OfficialDomainClaim":
        return cls(
            domain=str(value.get("Domain", value.get("domain", ""))),
            source_type=str(value.get("SourceType", value.get("source_type", ""))),
            relationship=str(value.get("Relationship", value.get("relationship", ""))),
        )


@dataclass(frozen=True)
class OfficialWebRequest:
    urls: tuple[str, ...]
    allowed_domains: tuple[str, ...]
    official_domain_claims: tuple[OfficialDomainClaim, ...] = ()
    allow_discovery: bool = False
    max_pages: int = 10

    def __post_init__(self) -> None:
        if not self.urls:
            raise ValueError("OfficialWeb requires at least one explicit URL")
        if not self.allowed_domains:
            raise ValueError("AllowedDomains is required; OfficialWeb fails closed without it")
        if self.max_pages < 1 or self.max_pages > 20:
            raise ValueError("MaxPages must be between 1 and 20")
        object.__setattr__(
            self,
            "allowed_domains",
            tuple(dict.fromkeys(normalize_domain(value) for value in self.allowed_domains)),
        )
        for url in self.urls:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("OfficialWeb URLs must be absolute HTTP(S) URLs")
        if len(self.urls) > self.max_pages:
            raise ValueError("URL count exceeds MaxPages")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OfficialWebRequest":
        def strings(raw: Any) -> tuple[str, ...]:
            if raw is None:
                return ()
            if isinstance(raw, str):
                return tuple(part.strip() for part in re.split(r"[\n,;；，]+", raw) if part.strip())
            return tuple(str(part).strip() for part in raw if str(part).strip())

        raw_claims = value.get("OfficialDomainClaims", value.get("official_domain_claims", ()))
        claims = tuple(
            claim if isinstance(claim, OfficialDomainClaim) else OfficialDomainClaim.from_mapping(claim)
            for claim in raw_claims
        )
        discovery = value.get("AllowDiscovery", value.get("allow_discovery", False))
        if not isinstance(discovery, bool):
            raise ValueError("AllowDiscovery must be boolean")
        return cls(
            urls=strings(value.get("URLs", value.get("urls"))),
            allowed_domains=strings(value.get("AllowedDomains", value.get("allowed_domains"))),
            official_domain_claims=claims,
            allow_discovery=discovery,
            max_pages=int(value.get("MaxPages", value.get("max_pages", 10))),
        )


@dataclass(frozen=True)
class OfficialWebEvidence:
    url: str
    final_url: str
    domain: str
    page_title: str
    source_type: str
    fetched_at: str
    http_status: int | None
    content_type: str
    text_content: str
    canonical_url: str
    officiality_status: OfficialityStatus
    officiality_basis: tuple[str, ...] = ()
    navigation_provenance: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "URL": self.url,
            "FinalURL": self.final_url,
            "Domain": self.domain,
            "PageTitle": self.page_title,
            "SourceType": self.source_type,
            "FetchedAt": self.fetched_at,
            "HTTPStatus": self.http_status if self.http_status is not None else "UNKNOWN",
            "ContentType": self.content_type,
            "TextContent": self.text_content,
            "CanonicalURL": self.canonical_url,
            "OfficialityStatus": self.officiality_status.value,
            "OfficialityBasis": list(self.officiality_basis),
            "NavigationProvenance": list(self.navigation_provenance),
        }


@dataclass(frozen=True)
class OfficialWebRunResult:
    status: OfficialWebStatus
    evidence: tuple[OfficialWebEvidence, ...] = ()
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "Status": self.status.value,
            "Evidence": [item.as_dict() for item in self.evidence],
            "Errors": list(self.errors),
        }


_CHALLENGE_MARKERS = (
    r"captcha",
    r"人机验证",
    r"安全验证",
    r"拖动.{0,12}(?:滑块|拼图).{0,12}验证",
    r"verify you are human",
)
_LOGIN_PATH_MARKERS = ("/login", "/signin", "/cas/", "/sso/", "/oauth/")
_LOGIN_BLOCK_MARKERS = (r"统一身份认证", r"请输入.{0,12}(?:密码|验证码)", r"sign in to continue")
_RESTRICTED_MARKERS = (r"access denied", r"forbidden", r"付费墙", r"仅限订阅", r"robots denied")
_OBSERVATION_PROBES = _CHALLENGE_MARKERS + _LOGIN_BLOCK_MARKERS + _RESTRICTED_MARKERS


class PublicOfficialWebAdapter:
    name = "OfficialWeb"

    def __init__(self, browser: BrowserCommandPort | Any):
        self.browser = ensure_browser_command_port(browser)

    @staticmethod
    def _allowed(host: str, request: OfficialWebRequest) -> bool:
        return any(domain_matches(host, allowed) for allowed in request.allowed_domains)

    @staticmethod
    def _plain_text(value: str) -> str:
        without_scripts = re.sub(
            r"<(script|style|noscript)\b[^>]*>.*?</\1>",
            " ",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
        return re.sub(r"\s+", " ", without_tags).strip()

    @staticmethod
    def _canonical_url(html: str | None, final_url: str) -> str:
        if html:
            match = re.search(
                r'<link\b[^>]*rel=["\'][^"\']*canonical[^"\']*["\'][^>]*href=["\']([^"\']+)',
                html,
                flags=re.IGNORECASE,
            )
            if match:
                candidate = match.group(1).strip()
                parsed = urlsplit(candidate)
                if parsed.scheme in {"http", "https"} and parsed.hostname:
                    return candidate
        return final_url

    @staticmethod
    def _officiality(host: str, request: OfficialWebRequest) -> tuple[OfficialityStatus, str, tuple[str, ...]]:
        # A configured claim establishes ownership context; live navigation
        # provenance is added by fetch() before a confirmed result is emitted.
        for claim in request.official_domain_claims:
            if domain_matches(host, claim.domain):
                return (
                    OfficialityStatus.OFFICIAL_CONFIRMED,
                    claim.source_type,
                    (
                        f"configured official domain: {claim.domain}",
                        f"declared relationship: {claim.relationship}",
                        "final host matches configured claim",
                    ),
                )
        if any(domain_matches(host, allowed) for allowed in request.allowed_domains):
            return (
                OfficialityStatus.OFFICIAL_PROBABLE,
                "AllowedOfficialCandidate",
                ("final host matches task allowlist", "no configured sponsor/publisher claim"),
            )
        return OfficialityStatus.UNVERIFIED, "Unverified", ("no official domain evidence",)

    @staticmethod
    def _gate(
        url: str,
        title: str,
        visible_text: str | None,
        fallback_text: str,
        http_status: int | None,
        target_observations: tuple[Any, ...],
    ) -> None:
        lowered_url = url.casefold()
        combined = f"{title} {visible_text or ''}"
        if any(marker in lowered_url for marker in _LOGIN_PATH_MARKERS) or any(
            re.search(pattern, combined, flags=re.IGNORECASE) for pattern in _LOGIN_BLOCK_MARKERS
        ):
            raise ManualAuthenticationRequired("OfficialWeb reached a login or authentication gate")
        if any(re.search(pattern, combined, flags=re.IGNORECASE) for pattern in _CHALLENGE_MARKERS):
            raise ManualAuthenticationRequired("OfficialWeb reached a CAPTCHA or security challenge")
        challenge_probe_count = 0
        for evidence in target_observations:
            marker = str(getattr(evidence, "marker", ""))
            if not any(re.search(pattern, marker, flags=re.IGNORECASE) for pattern in _CHALLENGE_MARKERS):
                continue
            challenge_probe_count += 1
            box = getattr(evidence, "bounding_box", None) or {}
            x = float(box.get("x", 0)) if box else 0
            y = float(box.get("y", 0)) if box else 0
            width = float(box.get("width", 0)) if box else 0
            height = float(box.get("height", 0)) if box else 0
            proven_offscreen = bool(
                box
                and (x + width <= 0 or y + height <= 0)
                and getattr(evidence, "inspection_complete", False)
            )
            if proven_offscreen:
                continue
            visible = getattr(evidence, "playwright_visible", None)
            blocking = bool(getattr(evidence, "blocking_overlay", False))
            if visible is not False or blocking:
                raise ManualAuthenticationRequired(
                    "OfficialWeb challenge evidence is visible, blocking, or cannot be classified safely"
                )
        if http_status in {401, 403, 407, 429, 451} or any(
            re.search(pattern, combined, flags=re.IGNORECASE) for pattern in _RESTRICTED_MARKERS
        ):
            raise PublicAccessRestricted("OfficialWeb public access is restricted")
        fallback_login = any(
            re.search(pattern, fallback_text, flags=re.IGNORECASE)
            for pattern in _LOGIN_BLOCK_MARKERS
        )
        fallback_challenge_without_probe = challenge_probe_count == 0 and any(
            re.search(pattern, fallback_text, flags=re.IGNORECASE)
            for pattern in _CHALLENGE_MARKERS
        )
        if visible_text is None and (fallback_login or fallback_challenge_without_probe):
            raise ManualAuthenticationRequired(
                "OfficialWeb access-gate text was present but render visibility was unavailable"
            )

    async def fetch(self, url: str, request: OfficialWebRequest) -> OfficialWebEvidence:
        initial = urlsplit(url)
        if not initial.hostname or not self._allowed(initial.hostname, request):
            raise DomainNotAllowed("Initial URL is outside AllowedDomains")
        await self.browser.execute(NavigateCommand(url))
        try:
            observation = await self.browser.execute(
                ObserveCommand(
                    include_html=True,
                    include_visible_text=True,
                    text_probes=_OBSERVATION_PROBES,
                )
            )
        except ObservationUnavailable:
            observation = await self.browser.execute(
                ObserveCommand(
                    include_html=False,
                    include_visible_text=False,
                    text_probes=_OBSERVATION_PROBES,
                )
            )
        final = urlsplit(observation.url)
        if not final.hostname or not self._allowed(final.hostname, request):
            raise DomainNotAllowed("Redirect destination is outside AllowedDomains")

        metadata = dict(observation.metadata)
        raw_status = metadata.get("HTTPStatus")
        http_status = int(raw_status) if isinstance(raw_status, (int, str)) and str(raw_status).isdigit() else None
        content_type = str(metadata.get("ContentType", "")).split(";", 1)[0].strip().casefold()
        html = observation.html
        structured = observation.structured_content if isinstance(observation.structured_content, str) else ""
        if not content_type:
            content_type = "text/html" if html is not None or structured else "unknown"
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise PublicAccessRestricted(f"Unsupported public page Content-Type: {content_type}")
        if http_status is not None and http_status >= 400:
            raise PublicAccessRestricted(f"OfficialWeb returned HTTP {http_status}")
        fallback_text = self._plain_text(html) if html else structured
        text = observation.visible_text or fallback_text
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            raise PublicAccessRestricted("OfficialWeb page did not expose public text content")
        self._gate(
            observation.url,
            observation.title,
            observation.visible_text,
            fallback_text,
            http_status,
            observation.target_observations,
        )
        officiality, source_type, basis = self._officiality(final.hostname, request)
        provenance = tuple(
            str(item)
            for item in getattr(self.browser, "navigation_provenance", ())
            if str(item).strip()
        )
        if officiality == OfficialityStatus.OFFICIAL_CONFIRMED and not provenance:
            officiality = OfficialityStatus.OFFICIAL_PROBABLE
            basis = (*basis, "navigation provenance unavailable")
        elif provenance:
            basis = (*basis, "controlled navigation provenance recorded")
        return OfficialWebEvidence(
            url=url,
            final_url=observation.url,
            domain=normalize_domain(final.hostname),
            page_title=observation.title,
            source_type=source_type,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            http_status=http_status,
            content_type=content_type,
            text_content=text,
            canonical_url=self._canonical_url(html, observation.url),
            officiality_status=officiality,
            officiality_basis=basis,
            navigation_provenance=provenance,
        )


OFFICIAL_WEB_ADAPTER_REGISTRY = {"OfficialWeb": PublicOfficialWebAdapter}


class OfficialWebExecutionBroker:
    """Resolve OfficialWeb before any public-page navigation."""

    def __init__(self, registry: Mapping[str, type[PublicOfficialWebAdapter]] | None = None):
        self.registry = dict(registry or OFFICIAL_WEB_ADAPTER_REGISTRY)

    def create(self, source: str, browser: BrowserCommandPort | Any) -> PublicOfficialWebAdapter:
        adapter_type = self.registry.get(str(source).strip())
        if adapter_type is None or adapter_type is not PublicOfficialWebAdapter:
            raise ValueError(f"No registered OfficialWeb adapter for {source!r}")
        return adapter_type(browser)

    async def execute(
        self,
        request: OfficialWebRequest,
        *,
        browser: BrowserCommandPort | Any,
        source: str = "OfficialWeb",
    ) -> OfficialWebRunResult:
        adapter = self.create(source, browser)
        evidence: list[OfficialWebEvidence] = []
        errors: list[str] = []
        for url in request.urls:
            try:
                evidence.append(await adapter.fetch(url, request))
            except OfficialWebError as exc:
                errors.append(f"{url}: {exc}")
                return OfficialWebRunResult(status=exc.status, evidence=tuple(evidence), errors=tuple(errors))
        return OfficialWebRunResult(
            status=OfficialWebStatus.SUCCESS,
            evidence=tuple(evidence),
            errors=tuple(errors),
        )


__all__ = [
    "DomainNotAllowed",
    "OFFICIAL_WEB_ADAPTER_REGISTRY",
    "OfficialDomainClaim",
    "OfficialWebEvidence",
    "OfficialWebExecutionBroker",
    "OfficialWebRequest",
    "OfficialWebRunResult",
    "OfficialWebStatus",
    "OfficialityStatus",
    "PublicOfficialWebAdapter",
    "domain_matches",
    "normalize_domain",
]
