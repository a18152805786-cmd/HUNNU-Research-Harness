from .base import (
    LiteratureSourceAdapter,
    SourceActionRequired,
    SourceLayoutChanged,
    SourceUnavailable,
    SourceUserDownloadRequired,
)
from .cnki import CNKIAdapter
from .oxfordacademic import OxfordAcademicAdapter
from .sciencedirect import ScienceDirectAdapter
from .springerlink import SpringerLinkAdapter

__all__ = [
    "LiteratureSourceAdapter",
    "SourceActionRequired",
    "SourceLayoutChanged",
    "SourceUnavailable",
    "SourceUserDownloadRequired",
    "CNKIAdapter",
    "OxfordAcademicAdapter",
    "ScienceDirectAdapter",
    "SpringerLinkAdapter",
]
