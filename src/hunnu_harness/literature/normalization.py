from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

from .models import UNKNOWN, LiteratureRecord


_DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def normalize_doi(value: str | None) -> str:
    if not value:
        return UNKNOWN
    candidate = unquote(str(value)).strip()
    candidate = re.sub(r"^\s*(?:doi\s*:\s*)", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", candidate, flags=re.IGNORECASE)
    match = _DOI_RE.search(candidate)
    if not match:
        return UNKNOWN
    doi = match.group(0).split("?", 1)[0].split("#", 1)[0]
    doi = doi.rstrip("\"'.,;:，。；：)]}>")
    return doi.casefold()


def normalize_title(value: str | None) -> str:
    if not value:
        return UNKNOWN
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    normalized: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char.isalnum() or category.startswith("L") or category.startswith("N"):
            normalized.append(char)
        elif char.isspace() or category.startswith("P") or category.startswith("S"):
            normalized.append(" ")
    result = re.sub(r"\s+", " ", "".join(normalized)).strip()
    return result or UNKNOWN


def normalize_person(value: str | None) -> str:
    return normalize_title(value)


def stable_paper_id(*, doi: str = UNKNOWN, title: str = UNKNOWN, year: str = UNKNOWN, authors: tuple[str, ...] = ()) -> str:
    normalized_doi = normalize_doi(doi)
    if normalized_doi != UNKNOWN:
        identity = f"doi:{normalized_doi}"
    else:
        first_author = normalize_person(authors[0]) if authors else UNKNOWN
        identity = f"title:{normalize_title(title)}|year:{year}|author:{first_author}"
    return "P" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_component(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", normalized)
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip(" ._")
    if not normalized:
        normalized = fallback
    if normalized.upper() in _WINDOWS_RESERVED:
        normalized = f"_{normalized}"
    return normalized


def normalized_fulltext_filename(
    record: LiteratureRecord,
    *,
    suffix: str,
    max_length: int = 180,
) -> str:
    year = _safe_component(str(record.year), fallback="unknown")
    first_author = record.first_author
    if first_author != UNKNOWN:
        if "," in first_author:
            first_author = first_author.split(",", 1)[0]
        elif " " in first_author:
            first_author = first_author.rsplit(" ", 1)[-1]
    author = _safe_component(first_author, fallback="UnknownAuthor")
    title_words = record.title.split()
    if len(title_words) > 10:
        short_title = " ".join(title_words[:10])
    elif len(title_words) == 1 and len(record.title) > 48:
        short_title = record.title[:48]
    else:
        short_title = record.title
    title = _safe_component(short_title, fallback="Untitled")
    suffix = suffix.casefold().strip()
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".bin"
    stem = f"{year}_{author}_{title}"
    allowed = max(24, max_length - len(suffix))
    return stem[:allowed].rstrip(" ._") + suffix


def normalized_pdf_filename(record: LiteratureRecord, *, max_length: int = 180) -> str:
    return normalized_fulltext_filename(record, suffix=".pdf", max_length=max_length)
