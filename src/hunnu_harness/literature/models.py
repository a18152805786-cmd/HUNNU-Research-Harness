from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


UNKNOWN = "unknown"


class RunStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    ACTION_REQUIRED_USER_LOGIN = "ACTION_REQUIRED_USER_LOGIN"
    ACTION_REQUIRED_USER_DOWNLOAD = "ACTION_REQUIRED_USER_DOWNLOAD"
    NO_RESULTS = "NO_RESULTS"
    FULLTEXT_NOT_AUTHORIZED = "FULLTEXT_NOT_AUTHORIZED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    SOURCE_LAYOUT_CHANGED = "SOURCE_LAYOUT_CHANGED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class AccessType(str, Enum):
    OPEN_ACCESS = "OpenAccess"
    INSTITUTIONAL_AUTHENTICATED = "InstitutionalAuthenticated"
    PUBLIC_FULL_TEXT = "PublicFullText"
    METADATA_ONLY = "MetadataOnly"
    UNKNOWN = "Unknown"


class FullTextFormat(str, Enum):
    PDF = "PDF"
    CAJ = "CAJ"
    OTHER_AUTHORIZED_FORMAT = "OtherAuthorizedFormat"
    UNKNOWN = "Unknown"


class PublicationStatus(str, Enum):
    PEER_REVIEWED_JOURNAL_ARTICLE = "PeerReviewedJournalArticle"
    ONLINE_FIRST = "OnlineFirst"
    ACCEPTED_MANUSCRIPT = "AcceptedManuscript"
    WORKING_PAPER = "WorkingPaper"
    PREPRINT = "Preprint"
    CONFERENCE_PAPER = "ConferencePaper"
    PROFESSIONAL_ARTICLE = "ProfessionalArticle"
    OTHER = "Other"
    UNKNOWN = "Unknown"


class ScreeningDecision(str, Enum):
    KEEP = "KEEP"
    MAYBE = "MAYBE"
    REJECT = "REJECT"
    UNSCREENED = "UNSCREENED"


def _canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _value(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    normalized = {_canonical_key(str(key)): value for key, value in mapping.items()}
    for name in names:
        key = _canonical_key(name)
        if key in normalized:
            return normalized[key]
    return default


def _tuple_of_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = re.split(r"[\n,;；，]+", value)
    else:
        parts = list(value)
    return tuple(str(item).strip() for item in parts if str(item).strip())


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int, field_name: str) -> int:
    if value is None or value == "":
        return default
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return parsed


def _optional_year(value: Any, *, field_name: str) -> int | None:
    if value in (None, "", UNKNOWN):
        return None
    year = int(value)
    if year < 1800 or year > 2200:
        raise ValueError(f"{field_name} must be between 1800 and 2200")
    return year


@dataclass(frozen=True)
class LiteratureSearchRequest:
    original_research_request: str
    research_question: str = ""
    keywords_cn: tuple[str, ...] = ()
    keywords_en: tuple[str, ...] = ()
    exact_titles: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    dois: tuple[str, ...] = ()
    year_start: int | None = None
    year_end: int | None = None
    preferred_languages: tuple[str, ...] = ("zh", "en")
    preferred_publication_types: tuple[str, ...] = (PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value,)
    peer_reviewed_preferred: bool = True
    journal_priority: tuple[str, ...] = ()
    max_search_results: int = 30
    max_downloads: int = 5
    max_results_per_source: int = 30
    max_downloads_per_run: int = 5
    require_full_text: bool = False
    ai_assisted_screening: bool = False

    def __post_init__(self) -> None:
        if not self.original_research_request.strip():
            raise ValueError("OriginalResearchRequest is required for auditability")
        if self.year_start and self.year_end and self.year_start > self.year_end:
            raise ValueError("YearStart cannot be later than YearEnd")
        if self.max_downloads > self.max_downloads_per_run:
            object.__setattr__(self, "max_downloads", self.max_downloads_per_run)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "LiteratureSearchRequest":
        original = str(
            _value(
                mapping,
                "OriginalResearchRequest",
                "original_research_request",
                default=_value(mapping, "ResearchQuestion", "research_question", default=""),
            )
        ).strip()
        max_per_run = _bounded_int(
            _value(mapping, "MaxDownloadsPerRun", "max_downloads_per_run"),
            default=5,
            minimum=0,
            maximum=25,
            field_name="MaxDownloadsPerRun",
        )
        return cls(
            original_research_request=original,
            research_question=str(_value(mapping, "ResearchQuestion", "research_question", default="")).strip(),
            keywords_cn=_tuple_of_strings(_value(mapping, "KeywordsCN", "keywords_cn")),
            keywords_en=_tuple_of_strings(_value(mapping, "KeywordsEN", "keywords_en")),
            exact_titles=_tuple_of_strings(_value(mapping, "ExactTitles", "exact_titles")),
            authors=_tuple_of_strings(_value(mapping, "Authors", "authors")),
            dois=_tuple_of_strings(_value(mapping, "DOIs", "dois")),
            year_start=_optional_year(_value(mapping, "YearStart", "year_start"), field_name="YearStart"),
            year_end=_optional_year(_value(mapping, "YearEnd", "year_end"), field_name="YearEnd"),
            preferred_languages=_tuple_of_strings(
                _value(mapping, "PreferredLanguages", "preferred_languages", default=("zh", "en"))
            ),
            preferred_publication_types=_tuple_of_strings(
                _value(
                    mapping,
                    "PreferredPublicationTypes",
                    "preferred_publication_types",
                    default=(PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value,),
                )
            ),
            peer_reviewed_preferred=bool(
                _value(mapping, "PeerReviewedPreferred", "peer_reviewed_preferred", default=True)
            ),
            journal_priority=_tuple_of_strings(_value(mapping, "JournalPriority", "journal_priority")),
            max_search_results=_bounded_int(
                _value(mapping, "MaxSearchResults", "max_search_results"),
                default=30,
                minimum=1,
                maximum=200,
                field_name="MaxSearchResults",
            ),
            max_downloads=_bounded_int(
                _value(mapping, "MaxDownloads", "max_downloads"),
                default=5,
                minimum=0,
                maximum=25,
                field_name="MaxDownloads",
            ),
            max_results_per_source=_bounded_int(
                _value(mapping, "MaxResultsPerSource", "max_results_per_source"),
                default=30,
                minimum=1,
                maximum=100,
                field_name="MaxResultsPerSource",
            ),
            max_downloads_per_run=max_per_run,
            require_full_text=bool(_value(mapping, "RequireFullText", "require_full_text", default=False)),
            ai_assisted_screening=bool(
                _value(mapping, "AI_ASSISTED", "ai_assisted_screening", default=False)
            ),
        )

    @classmethod
    def from_natural_language(
        cls,
        text: str,
        *,
        current_year: int | None = None,
    ) -> "LiteratureSearchRequest":
        """Parse bounded, explicit controls from a research request.

        This is deliberately conservative. It recognizes limits, years, DOI values,
        and a small auditable topic vocabulary; it does not invent an unbounded
        synonym expansion.
        """

        if not text.strip():
            raise ValueError("Research request cannot be empty")
        now_year = current_year or datetime.now().year
        lowered = text.casefold()

        # Dependency direction: models -> navigator.lexicon -> navigator.tokenize;
        # the lexicon does not import literature modules.
        from ..navigator.lexicon import _is_latin_term, match_concepts

        matches = match_concepts(text)
        keywords_en = tuple(
            dict.fromkeys(
                match.matched_term for match in matches if _is_latin_term(match.matched_term)
            )
        )
        keywords_cn = tuple(
            dict.fromkeys(
                match.matched_term for match in matches if not _is_latin_term(match.matched_term)
            )
        )

        dois = tuple(
            match.rstrip(".,;，。；)]}")
            for match in re.findall(r"10\.\d{4,9}/[^\s<>\"']+", text, flags=re.IGNORECASE)
        )
        quoted = tuple(
            value.strip()
            for value in re.findall(r"[\"“](.+?)[\"”]", text)
            if len(value.strip()) >= 8
        )

        year_start = year_end = None
        range_match = re.search(r"(20\d{2})\s*(?:-|–|—|至|到)\s*(20\d{2})", text)
        recent_match = re.search(r"(?:近\s*|last\s+)(\d{1,2})\s*(?:年|years?)", lowered)
        after_match = re.search(r"(20\d{2})\s*年?(?:以后|之后|以来|after)", lowered)
        if range_match:
            year_start, year_end = int(range_match.group(1)), int(range_match.group(2))
        elif recent_match:
            years = max(1, min(25, int(recent_match.group(1))))
            year_start, year_end = now_year - years + 1, now_year
        elif after_match:
            year_start, year_end = int(after_match.group(1)), now_year

        candidates_match = re.search(r"最多(?:保留|候选|检索)?\s*(\d{1,3})\s*篇", text)
        downloads_match = re.search(r"最多下载\s*(\d{1,2})\s*篇", text)
        max_results = min(200, int(candidates_match.group(1))) if candidates_match else 30
        max_downloads = min(25, int(downloads_match.group(1))) if downloads_match else 5
        if any(marker in lowered for marker in ("不要下载", "先不下载", "do not download", "metadata only")):
            max_downloads = 0

        if ("中文" in text or "中英文" in text) and ("英文" in text or "中英文" in text):
            languages = ("zh", "en")
        elif "只找中文" in text:
            languages = ("zh",)
        elif "只找英文" in text or "english only" in lowered:
            languages = ("en",)
        else:
            languages = ("zh", "en")

        return cls(
            original_research_request=text.strip(),
            research_question=text.strip(),
            keywords_cn=keywords_cn,
            keywords_en=keywords_en,
            exact_titles=quoted,
            dois=dois,
            year_start=year_start,
            year_end=year_end,
            preferred_languages=languages,
            peer_reviewed_preferred=("同行评议" in text or "peer reviewed" in lowered or "peer-reviewed" in lowered),
            max_search_results=max_results,
            max_downloads=max_downloads,
            max_results_per_source=min(100, max_results),
            max_downloads_per_run=max_downloads,
            require_full_text=max_downloads > 0 and any(
                marker in lowered for marker in ("全文", "full text", "download", "下载")
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "OriginalResearchRequest": self.original_research_request,
            "ResearchQuestion": self.research_question or UNKNOWN,
            "KeywordsCN": list(self.keywords_cn),
            "KeywordsEN": list(self.keywords_en),
            "ExactTitles": list(self.exact_titles),
            "Authors": list(self.authors),
            "DOIs": list(self.dois),
            "YearStart": self.year_start if self.year_start is not None else UNKNOWN,
            "YearEnd": self.year_end if self.year_end is not None else UNKNOWN,
            "PreferredLanguages": list(self.preferred_languages),
            "PreferredPublicationTypes": list(self.preferred_publication_types),
            "PeerReviewedPreferred": self.peer_reviewed_preferred,
            "JournalPriority": list(self.journal_priority),
            "MaxSearchResults": self.max_search_results,
            "MaxDownloads": self.max_downloads,
            "MaxResultsPerSource": self.max_results_per_source,
            "MaxDownloadsPerRun": self.max_downloads_per_run,
            "RequireFullText": self.require_full_text,
            "AI_ASSISTED": self.ai_assisted_screening,
        }


@dataclass
class LiteratureRecord:
    paper_id: str
    title: str = UNKNOWN
    authors: tuple[str, ...] = ()
    year: str = UNKNOWN
    journal: str = UNKNOWN
    volume: str = UNKNOWN
    issue: str = UNKNOWN
    pages_or_article_number: str = UNKNOWN
    doi: str = UNKNOWN
    issn: str = UNKNOWN
    language: str = UNKNOWN
    publication_type: str = UNKNOWN
    publication_status: str = PublicationStatus.UNKNOWN.value
    abstract: str = UNKNOWN
    keywords: tuple[str, ...] = ()
    source_database: str = UNKNOWN
    source_page: str = UNKNOWN
    navigation_url: str = field(default=UNKNOWN, repr=False)
    stable_identifier: str = UNKNOWN
    access_type: str = AccessType.UNKNOWN.value
    search_query: str = UNKNOWN
    full_text_accessible: bool = False
    full_text_downloaded: bool = False
    full_text_format: str = FullTextFormat.UNKNOWN.value
    original_filename: str = UNKNOWN
    normalized_filename: str = UNKNOWN
    local_path: str = UNKNOWN
    sha256: str = UNKNOWN
    file_size_bytes: int | str = UNKNOWN
    download_timestamp: str = UNKNOWN
    relevance_score: float = 0.0
    screening_decision: str = ScreeningDecision.UNSCREENED.value
    screening_reason: str = UNKNOWN
    ai_assisted: bool = False
    duplicate_detected: bool = False
    duplicate_reason: str = UNKNOWN
    canonical_paper_id: str = UNKNOWN
    same_work_different_version: bool = False
    archived_as_alternate_version: bool = False
    pdf_validation_passed: bool = False
    file_validation_passed: bool = False
    target_identity_confirmed: bool = False
    content_title_verification: str = "not_available"
    acquisition_method: str = UNKNOWN
    source_host: str = UNKNOWN
    source_route: str = UNKNOWN
    institutional_route_used: bool = False
    institution: str = UNKNOWN
    download_event_emitted: bool = False
    authorized_pdf_response_captured: bool = False
    target_title_matched: bool = False
    target_doi_matched: bool = False
    official_pdf_action_confirmed: bool = False
    source_access_status: str = UNKNOWN
    manual_download_required: bool = False
    manual_download_handoff_armed: bool = False
    manual_download_detected: bool = False
    human_download_action: bool = False
    original_manual_download_preserved: bool = False
    download_initiation_mode: str = UNKNOWN
    file_finalization_mode: str = UNKNOWN
    unattended_download_attempted: bool = False
    automatic_download_initiation: bool = False
    automatic_download_detection: bool = False
    user_native_viewer_click_required: bool = False
    manual_download_handoff_used: bool = False
    oxford_unattended_download_ready: bool = False
    research_chrome_direct_pdf_download_configured: bool = False
    error_status: str = UNKNOWN
    error_reason: str = UNKNOWN

    @property
    def abstract_available(self) -> bool:
        return self.abstract not in ("", UNKNOWN)

    @property
    def first_author(self) -> str:
        return self.authors[0] if self.authors else UNKNOWN

    def as_metadata_dict(self) -> dict[str, Any]:
        return {
            "PaperID": self.paper_id,
            "Title": self.title,
            "Authors": list(self.authors) if self.authors else [UNKNOWN],
            "Year": self.year,
            "Journal": self.journal,
            "Volume": self.volume,
            "Issue": self.issue,
            "PagesOrArticleNumber": self.pages_or_article_number,
            "DOI": self.doi,
            "ISSN": self.issn,
            "Language": self.language,
            "PublicationType": self.publication_type,
            "PublicationStatus": self.publication_status,
            "Abstract": self.abstract,
            "Keywords": list(self.keywords) if self.keywords else [UNKNOWN],
            "SourceDatabase": self.source_database,
            "SourcePage": self.source_page,
            "StableIdentifier": self.stable_identifier,
            "AccessType": self.access_type,
            "SearchQuery": self.search_query,
            "FullTextAccessible": self.full_text_accessible,
            "FullTextDownloaded": self.full_text_downloaded,
            "FullTextFormat": self.full_text_format,
            "OriginalFilename": self.original_filename,
            "NormalizedFilename": self.normalized_filename,
            "LocalPath": self.local_path,
            "SHA256": self.sha256,
            "FileSizeBytes": self.file_size_bytes,
            "DownloadTimestamp": self.download_timestamp,
            "RelevanceScore": self.relevance_score,
            "ScreeningDecision": self.screening_decision,
            "ScreeningReason": self.screening_reason,
            "AI_ASSISTED": self.ai_assisted,
            "DuplicateDetected": self.duplicate_detected,
            "DuplicateReason": self.duplicate_reason,
            "CanonicalPaperID": self.canonical_paper_id,
            "SameWorkDifferentVersion": self.same_work_different_version,
            "ArchivedAsAlternateVersion": self.archived_as_alternate_version,
            "PDFValidationPassed": self.pdf_validation_passed,
            "FileValidationPassed": self.file_validation_passed,
            "TargetIdentityConfirmed": self.target_identity_confirmed,
            "ContentTitleVerification": self.content_title_verification,
            "AcquisitionMethod": self.acquisition_method,
            "SourceHost": self.source_host,
            "SourceRoute": self.source_route,
            "InstitutionalRouteUsed": self.institutional_route_used,
            "Institution": self.institution,
            "DownloadEventEmitted": self.download_event_emitted,
            "AuthorizedPdfResponseCaptured": self.authorized_pdf_response_captured,
            "TargetTitleMatched": self.target_title_matched,
            "TargetDOIMatched": self.target_doi_matched,
            "OfficialPdfActionConfirmed": self.official_pdf_action_confirmed,
            "SourceAccessStatus": self.source_access_status,
            "ManualDownloadRequired": self.manual_download_required,
            "ManualDownloadHandoffArmed": self.manual_download_handoff_armed,
            "ManualDownloadDetected": self.manual_download_detected,
            "HumanDownloadAction": self.human_download_action,
            "OriginalManualDownloadPreserved": self.original_manual_download_preserved,
            "DownloadInitiationMode": self.download_initiation_mode,
            "FileFinalizationMode": self.file_finalization_mode,
            "UnattendedDownloadAttempted": self.unattended_download_attempted,
            "AutomaticDownloadInitiation": self.automatic_download_initiation,
            "AutomaticDownloadDetection": self.automatic_download_detection,
            "UserNativeViewerClickRequired": self.user_native_viewer_click_required,
            "ManualDownloadHandoffUsed": self.manual_download_handoff_used,
            "OxfordUnattendedDownloadReady": self.oxford_unattended_download_ready,
            "ResearchChromeDirectPdfDownloadConfigured": self.research_chrome_direct_pdf_download_configured,
            "ErrorStatus": self.error_status,
            "ErrorReason": self.error_reason,
        }

    def as_result_row(self) -> dict[str, Any]:
        return {
            "PaperID": self.paper_id,
            "Title": self.title,
            "Authors": "; ".join(self.authors) if self.authors else UNKNOWN,
            "Year": self.year,
            "Journal": self.journal,
            "Volume": self.volume,
            "Issue": self.issue,
            "PagesOrArticleNumber": self.pages_or_article_number,
            "DOI": self.doi,
            "Language": self.language,
            "PublicationStatus": self.publication_status,
            "Source": self.source_database,
            "SearchQuery": self.search_query,
            "AbstractAvailable": self.abstract_available,
            "FullTextAccessible": self.full_text_accessible,
            "FullTextDownloaded": self.full_text_downloaded,
            "FullTextFormat": self.full_text_format,
            "RelevanceScore": self.relevance_score,
            "ScreeningDecision": self.screening_decision,
            "ScreeningReason": self.screening_reason,
            "LocalPath": self.local_path,
            "SHA256": self.sha256,
            "FileValidationPassed": self.file_validation_passed,
            "TargetIdentityConfirmed": self.target_identity_confirmed,
            "ContentTitleVerification": self.content_title_verification,
        }


@dataclass(frozen=True)
class AccessDecision:
    full_text_accessible: bool
    access_type: AccessType
    authorized_access: bool
    status: RunStatus
    reason: str
    download_url: str = field(default=UNKNOWN, repr=False)
    download_locator: str = field(default=UNKNOWN, repr=False)
    full_text_format: FullTextFormat = FullTextFormat.PDF


@dataclass(frozen=True)
class PDFValidationResult:
    path: Path
    exists: bool
    non_zero_size: bool
    pdf_header_valid: bool
    pdf_readable: bool | None
    page_count: int | None
    file_size_bytes: int
    error: str = UNKNOWN

    @property
    def passed(self) -> bool:
        return self.exists and self.non_zero_size and self.pdf_header_valid and self.pdf_readable is not False

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["path"] = str(self.path)
        result["PDFValidationPassed"] = self.passed
        return result


@dataclass(frozen=True)
class DownloadManifestEntry:
    paper_id: str
    source: str
    title: str
    doi: str
    access_type: str
    authorized_access: bool
    original_url_or_stable_identifier: str
    original_filename: str
    normalized_filename: str
    download_timestamp: str
    file_size_bytes: int
    sha256: str
    local_path: str
    pdf_validation_passed: bool
    full_text_format: str = FullTextFormat.PDF.value
    file_validation_passed: bool = False
    target_identity_confirmed: bool = False
    content_title_verification: str = "not_available"
    acquisition_method: str = UNKNOWN
    source_host: str = UNKNOWN
    source_route: str = UNKNOWN
    institutional_route_used: bool = False
    institution: str = UNKNOWN
    download_event_emitted: bool = False
    authorized_pdf_response_captured: bool = False
    signed_url_persisted: bool = False
    query_string_persisted: bool = False
    authorization_header_persisted: bool = False
    cookie_persisted: bool = False
    target_title_matched: bool = False
    target_doi_matched: bool = False
    official_pdf_action_confirmed: bool = False
    source_access_status: str = UNKNOWN
    manual_download_required: bool = False
    manual_download_handoff_armed: bool = False
    manual_download_detected: bool = False
    human_download_action: bool = False
    original_manual_download_preserved: bool = False
    download_initiation_mode: str = UNKNOWN
    file_finalization_mode: str = UNKNOWN
    unattended_download_attempted: bool = False
    automatic_download_initiation: bool = False
    automatic_download_detection: bool = False
    user_native_viewer_click_required: bool = False
    manual_download_handoff_used: bool = False
    oxford_unattended_download_ready: bool = False
    research_chrome_direct_pdf_download_configured: bool = False
    library_disposition: str = UNKNOWN
    library_status: str = UNKNOWN
    library_managed_path: str = UNKNOWN
    library_notes_path: str = UNKNOWN
    library_catalog_path: str = UNKNOWN
    library_reason: str = UNKNOWN
    classification_status: str = UNKNOWN
    assigned_topics: tuple[str, ...] = ()
    assigned_primary_topic: str = UNKNOWN
    assigned_secondary_topics: tuple[str, ...] = ()
    proposed_topics: tuple[str, ...] = ()
    topic_review_required: bool = False
    navigator_metadata_ready: bool = False
    navigator_topic_ready: bool = False
    navigator_fulltext_index_status: str = UNKNOWN
    topic_metadata_updated: bool = False
    topic_view_updated: bool = False
    classification_reason: str = UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "PaperID": self.paper_id,
            "Source": self.source,
            "Title": self.title,
            "DOI": self.doi,
            "AccessType": self.access_type,
            "AuthorizedAccess": self.authorized_access,
            "OriginalURLOrStableIdentifier": self.original_url_or_stable_identifier,
            "OriginalFilename": self.original_filename,
            "NormalizedFilename": self.normalized_filename,
            "DownloadTimestamp": self.download_timestamp,
            "FileSizeBytes": self.file_size_bytes,
            "SHA256": self.sha256,
            "LocalPath": self.local_path,
            "PDFValidationPassed": self.pdf_validation_passed,
            "FullTextFormat": self.full_text_format,
            "FileValidationPassed": self.file_validation_passed,
            "TargetIdentityConfirmed": self.target_identity_confirmed,
            "ContentTitleVerification": self.content_title_verification,
            "AcquisitionMethod": self.acquisition_method,
            "SourceHost": self.source_host,
            "SourceRoute": self.source_route,
            "InstitutionalRouteUsed": self.institutional_route_used,
            "Institution": self.institution,
            "DownloadEventEmitted": self.download_event_emitted,
            "AuthorizedPdfResponseCaptured": self.authorized_pdf_response_captured,
            "SignedURLPersisted": self.signed_url_persisted,
            "QueryStringPersisted": self.query_string_persisted,
            "AuthorizationHeaderPersisted": self.authorization_header_persisted,
            "CookiePersisted": self.cookie_persisted,
            "TargetTitleMatched": self.target_title_matched,
            "TargetDOIMatched": self.target_doi_matched,
            "OfficialPdfActionConfirmed": self.official_pdf_action_confirmed,
            "SourceAccessStatus": self.source_access_status,
            "ManualDownloadRequired": self.manual_download_required,
            "ManualDownloadHandoffArmed": self.manual_download_handoff_armed,
            "ManualDownloadDetected": self.manual_download_detected,
            "HumanDownloadAction": self.human_download_action,
            "OriginalManualDownloadPreserved": self.original_manual_download_preserved,
            "DownloadInitiationMode": self.download_initiation_mode,
            "FileFinalizationMode": self.file_finalization_mode,
            "UnattendedDownloadAttempted": self.unattended_download_attempted,
            "AutomaticDownloadInitiation": self.automatic_download_initiation,
            "AutomaticDownloadDetection": self.automatic_download_detection,
            "UserNativeViewerClickRequired": self.user_native_viewer_click_required,
            "ManualDownloadHandoffUsed": self.manual_download_handoff_used,
            "OxfordUnattendedDownloadReady": self.oxford_unattended_download_ready,
            "ResearchChromeDirectPdfDownloadConfigured": self.research_chrome_direct_pdf_download_configured,
            "LibraryDisposition": self.library_disposition,
            "LibraryStatus": self.library_status,
            "LibraryManagedPath": self.library_managed_path,
            "LibraryNotesPath": self.library_notes_path,
            "LibraryCatalogPath": self.library_catalog_path,
            "LibraryReason": self.library_reason,
            "ClassificationStatus": self.classification_status,
            "AssignedTopics": list(self.assigned_topics),
            "AssignedPrimaryTopic": self.assigned_primary_topic,
            "AssignedSecondaryTopics": list(self.assigned_secondary_topics),
            "ProposedTopics": list(self.proposed_topics),
            "TopicReviewRequired": self.topic_review_required,
            "NavigatorMetadataReady": self.navigator_metadata_ready,
            "NavigatorTopicReady": self.navigator_topic_ready,
            "NavigatorFulltextIndexStatus": self.navigator_fulltext_index_status,
            "TopicMetadataUpdated": self.topic_metadata_updated,
            "TopicViewUpdated": self.topic_view_updated,
            "ClassificationReason": self.classification_reason,
        }


@dataclass(frozen=True)
class QueryLogEntry:
    timestamp: str
    source: str
    original_research_request: str
    generated_query: str
    filters: str
    results_returned: int
    results_inspected: int
    errors: str = UNKNOWN

    def as_row(self) -> dict[str, Any]:
        return {
            "Timestamp": self.timestamp,
            "Source": self.source,
            "OriginalResearchRequest": self.original_research_request,
            "GeneratedQuery": self.generated_query,
            "Filters": self.filters,
            "ResultsReturned": self.results_returned,
            "ResultsInspected": self.results_inspected,
            "Errors": self.errors,
        }


@dataclass
class LiteratureRunResult:
    status: RunStatus
    records: list[LiteratureRecord]
    downloads: list[DownloadManifestEntry]
    errors: list[str] = field(default_factory=list)
    action_required_reason: str = UNKNOWN
