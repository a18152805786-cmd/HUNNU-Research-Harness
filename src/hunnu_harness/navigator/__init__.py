"""Paper Research Navigator: a retrieval/navigation layer over the frozen library.

This package finds, ranks, resolves, packages, and routes.  It is not a second
source of truth: ``library/catalog/papers.jsonl``, ``library/papers``, the
version metadata, and the topic metadata remain authoritative, and every index,
cache, ranking, and reading pack produced here is derived and rebuildable.

No code path in this package opens a Library file for writing.
"""

from .catalog import (
    CatalogReader,
    CatalogSnapshot,
    LibraryUnavailable,
    PaperVersion,
    PaperWork,
    TopicAssignment,
)
from .index import IndexStatus, NavigatorIndex
from .lexicon import Facet, RelevanceRole
from .query import ParsedQuery, parse_query
from .resolver import FullTextStatus, PreferredVersionResolver, VersionResolution
from .search import PaperNavigator, SearchResult

__all__ = [
    "CatalogReader",
    "CatalogSnapshot",
    "Facet",
    "FullTextStatus",
    "IndexStatus",
    "LibraryUnavailable",
    "NavigatorIndex",
    "PaperNavigator",
    "PaperVersion",
    "PaperWork",
    "ParsedQuery",
    "PreferredVersionResolver",
    "RelevanceRole",
    "SearchResult",
    "TopicAssignment",
    "VersionResolution",
    "parse_query",
]
