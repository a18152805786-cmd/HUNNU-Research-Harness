"""Bounded full-text extraction and page-anchored chunking.

The Harness already extracts PDF text, but only the first N pages -- that is
correct for an identity gate and useless for retrieval.  This module extracts a
whole document, bounded by an explicit page cap, and cuts it into chunks that
remain locatable: every chunk carries the version SHA-256, the page number, and
its ordinal on that page, so a passage returned by a search can be pointed at
and re-read.

Managed PDFs are opened read-only and never modified.  Extraction failure for
one work is recorded and skipped; it never aborts a build.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .catalog import PaperVersion


#: Hard bound on pages read from any single document.
DEFAULT_PAGE_LIMIT = 120

#: Target characters per chunk.  Large enough to hold an argument, small enough
#: that a matched passage points at something a reader can locate on the page.
DEFAULT_CHUNK_CHARS = 1200
DEFAULT_CHUNK_OVERLAP = 150

#: Below this, a page is treated as image-only / unextractable rather than empty.
MIN_PAGE_CHARS = 12


EXTRACTION_OK = "OK"
EXTRACTION_EMPTY = "NO_EXTRACTABLE_TEXT"
EXTRACTION_FAILED = "EXTRACTION_FAILED"
EXTRACTION_UNSUPPORTED = "UNSUPPORTED_FORMAT"
EXTRACTION_MISSING = "FILE_MISSING"


@dataclass(frozen=True)
class Chunk:
    """One locatable passage of a managed full text."""

    chunk_id: str
    paper_id: str
    version_sha256: str
    managed_path: str
    page: int
    ordinal: int
    text: str
    text_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "paper_id": self.paper_id,
            "version_sha256": self.version_sha256,
            "managed_path": self.managed_path,
            "page": self.page,
            "ordinal": self.ordinal,
            "text": self.text,
            "text_sha256": self.text_sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Chunk":
        return cls(
            chunk_id=str(payload["chunk_id"]),
            paper_id=str(payload["paper_id"]),
            version_sha256=str(payload.get("version_sha256", "")),
            managed_path=str(payload.get("managed_path", "")),
            page=int(payload.get("page", 0)),
            ordinal=int(payload.get("ordinal", 0)),
            text=str(payload.get("text", "")),
            text_sha256=str(payload.get("text_sha256", "")),
        )


@dataclass(frozen=True)
class ExtractionResult:
    paper_id: str
    version_sha256: str
    managed_path: str
    status: str
    pages_read: int
    chunks: tuple[Chunk, ...]
    detail: str = ""

    def as_manifest_entry(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "source_sha256": self.version_sha256,
            "managed_path": self.managed_path,
            "status": self.status,
            "pages_read": self.pages_read,
            "chunk_count": len(self.chunks),
            "detail": self.detail,
        }


def sanitize_extracted_text(text: str) -> str:
    """Drop code points that cannot survive a UTF-8 round trip.

    A PDF with a damaged font map makes ``pypdf`` emit unpaired surrogates.
    They are not encodable, so hashing the chunk, writing the index, or handing
    the passage to any JSON consumer would raise.  Discarding them costs a few
    unreadable characters; letting them through costs the whole document.
    """

    if not text:
        return ""
    return "".join(char for char in text if not 0xD800 <= ord(char) <= 0xDFFF)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _normalise_whitespace(text: str) -> str:
    return " ".join(text.split())


def split_page_text(text: str, *, chunk_chars: int, overlap: int) -> list[str]:
    """Cut one page into overlapping chunks on whitespace boundaries."""

    cleaned = _normalise_whitespace(text)
    if len(cleaned) <= chunk_chars:
        return [cleaned] if cleaned else []
    chunks: list[str] = []
    start = 0
    length = len(cleaned)
    while start < length:
        end = min(length, start + chunk_chars)
        if end < length:
            # Prefer a space boundary so a chunk does not split a token.
            boundary = cleaned.rfind(" ", start + chunk_chars // 2, end)
            if boundary > start:
                end = boundary
        piece = cleaned[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks


def iter_page_text(path: Path, *, page_limit: int = DEFAULT_PAGE_LIMIT) -> Iterator[tuple[int, str]]:
    """Yield ``(page_number, text)`` for a PDF, one-indexed.

    Uses ``pypdf``, the Harness's declared PDF dependency, in the same
    ``strict=False`` mode the existing validators use.
    """

    from pypdf import PdfReader  # type: ignore[import-not-found]

    reader = PdfReader(str(path), strict=False)
    for index, page in enumerate(reader.pages[: max(0, int(page_limit))], start=1):
        try:
            yield index, sanitize_extracted_text(page.extract_text() or "")
        except Exception:  # one damaged page must not lose the rest
            yield index, ""


class FullTextExtractor:
    """Extract and chunk one managed version, failing soft."""

    def __init__(
        self,
        *,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> None:
        self.page_limit = page_limit
        self.chunk_chars = chunk_chars
        self.chunk_overlap = chunk_overlap

    def extract(self, paper_id: str, version: PaperVersion) -> ExtractionResult:
        path = version.absolute_path
        base = {
            "paper_id": paper_id,
            "version_sha256": version.sha256,
            "managed_path": version.managed_path,
        }
        if not path.is_file():
            return ExtractionResult(**base, status=EXTRACTION_MISSING, pages_read=0, chunks=())
        if version.full_text_format.upper() != "PDF":
            return ExtractionResult(
                **base,
                status=EXTRACTION_UNSUPPORTED,
                pages_read=0,
                chunks=(),
                detail=f"no text extractor for {version.full_text_format}",
            )

        chunks: list[Chunk] = []
        pages_read = 0
        try:
            for page_number, raw in iter_page_text(path, page_limit=self.page_limit):
                pages_read += 1
                if len(raw.strip()) < MIN_PAGE_CHARS:
                    continue
                for ordinal, piece in enumerate(
                    split_page_text(raw, chunk_chars=self.chunk_chars, overlap=self.chunk_overlap)
                ):
                    chunks.append(
                        Chunk(
                            chunk_id=f"{paper_id}:{version.sha256[:12]}:p{page_number}:c{ordinal}",
                            paper_id=paper_id,
                            version_sha256=version.sha256,
                            managed_path=version.managed_path,
                            page=page_number,
                            ordinal=ordinal,
                            text=piece,
                            text_sha256=_sha256_text(piece),
                        )
                    )
        except Exception as exc:
            return ExtractionResult(
                **base,
                status=EXTRACTION_FAILED,
                pages_read=pages_read,
                chunks=tuple(chunks),
                detail=f"{type(exc).__name__}: {exc}",
            )

        if not chunks:
            return ExtractionResult(
                **base,
                status=EXTRACTION_EMPTY,
                pages_read=pages_read,
                chunks=(),
                detail="no extractable text layer (scanned or image-only PDF)",
            )
        return ExtractionResult(**base, status=EXTRACTION_OK, pages_read=pages_read, chunks=tuple(chunks))


__all__ = [
    "Chunk",
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_PAGE_LIMIT",
    "EXTRACTION_EMPTY",
    "EXTRACTION_FAILED",
    "EXTRACTION_MISSING",
    "EXTRACTION_OK",
    "EXTRACTION_UNSUPPORTED",
    "ExtractionResult",
    "FullTextExtractor",
    "sanitize_extracted_text",
    "iter_page_text",
    "split_page_text",
]
