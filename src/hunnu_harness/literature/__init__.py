"""Auditable, human-authenticated literature acquisition for Harness v0.2."""

from .models import (
    AccessDecision,
    AccessType,
    FullTextFormat,
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
    ScreeningDecision,
)
from .institutional import (
    HUNNUInstitutionalAccessResolver,
    InstitutionalAccessResolver,
    InstitutionalResolutionTrigger,
    InstitutionalRouteResult,
    InstitutionalRouteStep,
)
from .cnki_challenge import (
    CNKIChallengeDetector,
    ChallengeDiagnostic,
    ChallengeState,
    TargetPageIdentityError,
)
from .preflight import (
    AuthenticationSweepStatus,
    MultiSourcePreflightCoordinator,
    PreflightStatus,
    SourceCapabilityRegistry,
    SourcePreflightCapabilities,
)
from .library import (
    ExternalPaperImporter,
    GlobalPaperLibrary,
    LibraryDisposition,
    LibraryIngestResult,
    StagedPaperCandidate,
)
from .fulltext import (
    DOIContentVerification,
    ExternalIdentityDecision,
    ExternalIdentityVerificationResult,
    verify_external_paper_identity,
)
from .metadata_correction import (
    BibliographicCorrection,
    BibliographicCorrectionResult,
    BibliographicMetadataCorrector,
)
from .execution import (
    AdapterExecutionBroker,
    AdapterIdentityError,
    AdapterResolutionError,
    LiteratureAdapterFactory,
)

__all__ = [
    "AccessDecision",
    "AccessType",
    "FullTextFormat",
    "LiteratureRecord",
    "LiteratureSearchRequest",
    "PublicationStatus",
    "RunStatus",
    "ScreeningDecision",
    "HUNNUInstitutionalAccessResolver",
    "InstitutionalAccessResolver",
    "InstitutionalResolutionTrigger",
    "InstitutionalRouteResult",
    "InstitutionalRouteStep",
    "CNKIChallengeDetector",
    "ChallengeDiagnostic",
    "ChallengeState",
    "TargetPageIdentityError",
    "AuthenticationSweepStatus",
    "MultiSourcePreflightCoordinator",
    "PreflightStatus",
    "SourceCapabilityRegistry",
    "SourcePreflightCapabilities",
    "ExternalPaperImporter",
    "GlobalPaperLibrary",
    "LibraryDisposition",
    "LibraryIngestResult",
    "StagedPaperCandidate",
    "DOIContentVerification",
    "ExternalIdentityDecision",
    "ExternalIdentityVerificationResult",
    "verify_external_paper_identity",
    "BibliographicCorrection",
    "BibliographicCorrectionResult",
    "BibliographicMetadataCorrector",
    "AdapterExecutionBroker",
    "AdapterIdentityError",
    "AdapterResolutionError",
    "LiteratureAdapterFactory",
]
