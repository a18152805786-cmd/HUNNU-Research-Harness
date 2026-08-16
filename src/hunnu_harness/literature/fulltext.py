from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .models import FullTextFormat, LiteratureRecord, UNKNOWN
from .normalization import normalize_doi, normalize_title
from .pdf import PDFValidator


_HTML_PREFIXES = (b"<!doctype html", b"<html", b"<?xml")
_CAJ_SIGNATURES = (b"CAJ", b"HNLC", b"KDH")
_DOI_CONTENT_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_MIN_EXPLICIT_TITLE_EVIDENCE_CHARS = 12
_TRAILING_BYLINE_RE = re.compile(
    r"^(?P<title>.+?)\s*[-‐‑‒–—―:：]\s*(?P<byline>[^\r\n]+?)\s*$"
)
_EXPLICIT_AUTHOR_LINE_RE = re.compile(
    r"(?im)^\s*(?:作\s*者|authors?)\s*[:：]\s*(?P<byline>[^\r\n]+)"
)


class ExternalIdentityDecision(str, Enum):
    VERIFIED = "IDENTITY_VERIFIED"
    CONFLICT = "IDENTITY_CONFLICT"
    UNVERIFIED = "EXTERNAL_IDENTITY_UNVERIFIED"


class DOIContentVerification(str, Enum):
    MATCHED = "DOI_MATCH"
    CONFLICT = "DOI_CONFLICT"
    NOT_AVAILABLE = "DOI_NOT_AVAILABLE"
    NOT_SUPPLIED = "DOI_NOT_SUPPLIED"


@dataclass(frozen=True)
class ExternalIdentityVerificationResult:
    decision: ExternalIdentityDecision
    doi_verification: DOIContentVerification
    title_verification: str
    claimed_doi: str = UNKNOWN
    extracted_dois: tuple[str, ...] = ()
    reason: str = UNKNOWN

    @property
    def verified(self) -> bool:
        return self.decision == ExternalIdentityDecision.VERIFIED

    def as_dict(self) -> dict[str, object]:
        return {
            "Decision": self.decision.value,
            "DOIVerification": self.doi_verification.value,
            "TitleVerification": self.title_verification,
            "ClaimedDOI": self.claimed_doi,
            "ExtractedDOIs": list(self.extracted_dois),
            "Reason": self.reason,
        }


@dataclass(frozen=True)
class FullTextValidationResult:
    path: Path
    full_text_format: FullTextFormat
    exists: bool
    non_zero_size: bool
    expected_extension: bool
    basic_signature_valid: bool
    file_size_bytes: int
    pdf_validation_passed: bool = False
    content_title_verification: str = "not_available"
    error: str = UNKNOWN

    @property
    def passed(self) -> bool:
        return (
            self.exists
            and self.non_zero_size
            and self.expected_extension
            and self.basic_signature_valid
            and (self.full_text_format != FullTextFormat.PDF or self.pdf_validation_passed)
        )


def infer_full_text_format(path: Path, declared: FullTextFormat = FullTextFormat.UNKNOWN) -> FullTextFormat:
    if declared != FullTextFormat.UNKNOWN:
        return declared
    suffix = Path(path).suffix.casefold()
    if suffix == ".pdf":
        return FullTextFormat.PDF
    if suffix in {".caj", ".nh", ".kdh"}:
        return FullTextFormat.CAJ
    if suffix:
        return FullTextFormat.OTHER_AUTHORIZED_FORMAT
    return FullTextFormat.UNKNOWN


class AuthorizedFullTextValidator:
    """Validate an authorized publisher/database download without format conversion."""

    @classmethod
    def validate(
        cls,
        path: Path,
        declared_format: FullTextFormat = FullTextFormat.UNKNOWN,
        *,
        record: LiteratureRecord | None = None,
    ) -> FullTextValidationResult:
        path = Path(path)
        full_text_format = infer_full_text_format(path, declared_format)
        if not path.exists() or not path.is_file():
            return FullTextValidationResult(path, full_text_format, False, False, False, False, 0, error="File does not exist")
        try:
            size = path.stat().st_size
            if size > 0:
                with path.open("rb") as handle:
                    header = handle.read(1024)
            else:
                header = b""
        except OSError as exc:
            return FullTextValidationResult(path, full_text_format, False, False, False, False, 0, error=str(exc))
        if size <= 0:
            return FullTextValidationResult(path, full_text_format, True, False, False, False, size, error="File is empty")

        stripped = header.lstrip().lower()
        if any(stripped.startswith(prefix) for prefix in _HTML_PREFIXES) or b"<html" in stripped[:512]:
            return FullTextValidationResult(
                path,
                full_text_format,
                True,
                True,
                cls._expected_extension(path, full_text_format),
                False,
                size,
                error="DownloadedHTMLInsteadOfFullText",
            )

        expected_extension = cls._expected_extension(path, full_text_format)
        if full_text_format == FullTextFormat.PDF:
            pdf = PDFValidator.validate(path)
            title_check = cls._verify_pdf_title(path, record)
            return FullTextValidationResult(
                path,
                full_text_format,
                pdf.exists,
                pdf.non_zero_size,
                expected_extension,
                pdf.pdf_header_valid,
                pdf.file_size_bytes,
                pdf_validation_passed=pdf.passed,
                content_title_verification=title_check,
                error=pdf.error,
            )

        if full_text_format == FullTextFormat.CAJ:
            signature = any(header.startswith(marker) for marker in _CAJ_SIGNATURES)
            error = UNKNOWN if signature and expected_extension else (
                "Unexpected CAJ extension" if not expected_extension else "Unrecognized CAJ-family file signature"
            )
            return FullTextValidationResult(
                path,
                full_text_format,
                True,
                True,
                expected_extension,
                signature,
                size,
                error=error,
            )

        signature = bool(header) and not header.startswith(b"%PDF-")
        error = UNKNOWN if signature and expected_extension else "Unknown or mismatched authorized full-text format"
        return FullTextValidationResult(
            path,
            full_text_format,
            True,
            True,
            expected_extension,
            signature,
            size,
            error=error,
        )

    @staticmethod
    def _expected_extension(path: Path, full_text_format: FullTextFormat) -> bool:
        suffix = path.suffix.casefold()
        if full_text_format == FullTextFormat.PDF:
            return suffix == ".pdf"
        if full_text_format == FullTextFormat.CAJ:
            return suffix in {".caj", ".nh", ".kdh"}
        return bool(suffix)

    @staticmethod
    def _verify_pdf_title(path: Path, record: LiteratureRecord | None) -> str:
        if record is None or record.title in ("", UNKNOWN):
            return "not_available"
        text = AuthorizedFullTextValidator.extract_pdf_first_page_text(path)
        if text is None:
            return "not_available"
        matched = AuthorizedFullTextValidator._title_matches_extracted_text(
            record.title,
            text,
            record.authors,
        )
        if matched is None:
            return "not_available"
        return "matched" if matched else "not_matched"

    @staticmethod
    def extract_pdf_first_page_text(path: Path) -> str | None:
        """Return local first-page text, or None when extraction is unavailable."""

        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]

            reader = PdfReader(str(path), strict=False)
            if not reader.pages:
                return None
            return reader.pages[0].extract_text() or ""
        except Exception:
            return None

    @staticmethod
    def _title_matches_extracted_text(
        title: str,
        extracted_text: str,
        authors: tuple[str, ...] = (),
    ) -> bool | None:
        """Match a title, including a corroborated trailing author byline.

        The byline exception is deliberately narrow: the claimed title must end
        in a title/byline separator plus one exact supplied author, and the same
        author must appear on an explicitly labelled first-page author line.
        The remaining core title must still pass the original deterministic
        title match.  DOI evidence never relaxes this check.
        """

        expected = normalize_title(title)
        actual = normalize_title(extracted_text)
        if expected == UNKNOWN or actual == UNKNOWN:
            return None
        if expected in actual:
            return True
        compact_actual = actual.replace(" ", "")
        if expected.replace(" ", "") in compact_actual:
            return True

        byline_match = _TRAILING_BYLINE_RE.fullmatch(title)
        if byline_match is None:
            return False
        compact_byline = normalize_title(byline_match.group("byline")).replace(" ", "")
        compact_authors = {
            normalized.replace(" ", "")
            for author in authors
            if (normalized := normalize_title(author)) != UNKNOWN
        }
        if not compact_byline or compact_byline not in compact_authors:
            return False
        if not any(
            compact_byline in normalize_title(match.group("byline")).replace(" ", "")
            for match in _EXPLICIT_AUTHOR_LINE_RE.finditer(extracted_text)
        ):
            return False

        core_title = normalize_title(byline_match.group("title"))
        if (
            core_title == UNKNOWN
            or len(core_title.replace(" ", "")) < _MIN_EXPLICIT_TITLE_EVIDENCE_CHARS
        ):
            return False
        return core_title in actual or core_title.replace(" ", "") in compact_actual


def verify_external_paper_identity(
    path: Path,
    record: LiteratureRecord,
    *,
    validation: FullTextValidationResult | None = None,
) -> ExternalIdentityVerificationResult:
    """Verify a claimed external identity against local PDF content only.

    This deliberately performs no network lookup and makes no OCR attempt.  A
    complete metadata claim is not sufficient: at least its normalized DOI or,
    when no DOI was supplied, its normalized title must be confirmed in the
    locally extractable first-page text.
    """

    path = Path(path)
    validation = validation or AuthorizedFullTextValidator.validate(
        path,
        FullTextFormat.PDF,
        record=record,
    )
    claimed_doi = normalize_doi(record.doi)
    first_page_text = AuthorizedFullTextValidator.extract_pdf_first_page_text(path)
    title_verification = validation.content_title_verification
    if title_verification == "not_matched":
        non_doi_text = normalize_title(_DOI_CONTENT_RE.sub("", first_page_text or ""))
        if (
            non_doi_text == UNKNOWN
            or len(non_doi_text.replace(" ", "")) < _MIN_EXPLICIT_TITLE_EVIDENCE_CHARS
        ):
            # A DOI-only or otherwise too-thin first page is weak title
            # extraction, not affirmative evidence of a different title.
            title_verification = "not_available"
    extracted_dois = tuple(
        sorted(
            {
                normalized
                for candidate in _DOI_CONTENT_RE.findall(first_page_text or "")
                if (normalized := normalize_doi(candidate)) != UNKNOWN
            }
        )
    )

    if claimed_doi == UNKNOWN:
        doi_verification = DOIContentVerification.NOT_SUPPLIED
    elif extracted_dois == (claimed_doi,):
        doi_verification = DOIContentVerification.MATCHED
    elif extracted_dois:
        doi_verification = DOIContentVerification.CONFLICT
    else:
        doi_verification = DOIContentVerification.NOT_AVAILABLE

    evidence = (
        f"DOIVerification={doi_verification.value}; "
        f"TitleVerification={title_verification}"
    )
    if not validation.passed:
        return ExternalIdentityVerificationResult(
            ExternalIdentityDecision.UNVERIFIED,
            doi_verification,
            title_verification,
            claimed_doi,
            extracted_dois,
            f"External identity cannot be verified before structural validation passes; {evidence}",
        )
    if doi_verification == DOIContentVerification.CONFLICT:
        return ExternalIdentityVerificationResult(
            ExternalIdentityDecision.CONFLICT,
            doi_verification,
            title_verification,
            claimed_doi,
            extracted_dois,
            f"PDF contains DOI evidence that conflicts with the supplied DOI; {evidence}",
        )
    if title_verification == "not_matched":
        return ExternalIdentityVerificationResult(
            ExternalIdentityDecision.CONFLICT,
            doi_verification,
            title_verification,
            claimed_doi,
            extracted_dois,
            f"PDF first-page title evidence contradicts the supplied title; {evidence}",
        )
    if doi_verification == DOIContentVerification.MATCHED:
        return ExternalIdentityVerificationResult(
            ExternalIdentityDecision.VERIFIED,
            doi_verification,
            title_verification,
            claimed_doi,
            extracted_dois,
            f"External identity verified by normalized DOI content; {evidence}",
        )
    if claimed_doi == UNKNOWN and title_verification == "matched":
        return ExternalIdentityVerificationResult(
            ExternalIdentityDecision.VERIFIED,
            doi_verification,
            title_verification,
            claimed_doi,
            extracted_dois,
            f"External identity verified by normalized title content; {evidence}",
        )
    return ExternalIdentityVerificationResult(
        ExternalIdentityDecision.UNVERIFIED,
        doi_verification,
        title_verification,
        claimed_doi,
        extracted_dois,
        f"PDF content does not provide sufficient independent identity evidence; {evidence}",
    )
