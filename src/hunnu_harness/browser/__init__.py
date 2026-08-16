"""Playwright browser lifecycle and authorized download primitives."""

from .pdf_preferences import (
    PDF_DIRECT_DOWNLOAD_PREFERENCE,
    PdfPreferenceAudit,
    ResearchChromePdfPreference,
    ResearchChromePreferenceError,
    ResearchChromeProfileInUse,
)

from .manual_download_handoff import (
    ManualDownloadCandidateAmbiguous,
    ManualDownloadCandidateRejected,
    ManualDownloadDetection,
    ManualDownloadHandoff,
    ManualDownloadHandoffError,
    ManualDownloadHandoffResult,
    ManualDownloadHandoffState,
    ManualDownloadHandoffTimeout,
)

__all__ = [
    "PDF_DIRECT_DOWNLOAD_PREFERENCE",
    "PdfPreferenceAudit",
    "ResearchChromePdfPreference",
    "ResearchChromePreferenceError",
    "ResearchChromeProfileInUse",
    "ManualDownloadCandidateAmbiguous",
    "ManualDownloadCandidateRejected",
    "ManualDownloadDetection",
    "ManualDownloadHandoff",
    "ManualDownloadHandoffError",
    "ManualDownloadHandoffResult",
    "ManualDownloadHandoffState",
    "ManualDownloadHandoffTimeout",
]
