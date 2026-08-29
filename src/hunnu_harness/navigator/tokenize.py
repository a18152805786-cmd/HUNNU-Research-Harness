"""Bilingual tokenisation for a corpus that is 162 CJK / 17 Latin works.

``literature.normalization.normalize_title`` is reused for canonicalisation --
it already handles NFKC, casefolding, full-width/half-width folding, and maps
both CJK and Latin punctuation to spaces.  What it deliberately does not do is
segment CJK: a Chinese title comes back as one space-free run, so a term-based
scorer built on it alone would match nothing.

This module adds the missing step.  CJK runs are emitted as character unigrams
*and* adjacent bigrams.  Bigrams carry the discriminative signal (``漂洗``,
``审计``, ``盈余``); unigrams keep recall when a construct is written slightly
differently.  That is the standard segmenter-free approach for Chinese
retrieval and it needs no dictionary and no new dependency.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from ..literature.models import UNKNOWN
from ..literature.normalization import normalize_title


_CJK_RANGES = (
    (0x3400, 0x4DBF),    # CJK Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0x3040, 0x30FF),    # Hiragana + Katakana
    (0xAC00, 0xD7AF),    # Hangul syllables
)

_LATIN_RUN_RE = re.compile(r"[0-9a-z]+")

# Latin stop words that carry no retrieval signal in this corpus.  Deliberately
# short: an aggressive stop list hurts a 179-work library more than it helps.
_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
        "from", "how", "in", "is", "it", "its", "of", "on", "or", "that", "the",
        "their", "this", "to", "was", "were", "what", "when", "which", "with",
    }
)

# Chinese function characters that appear in almost every title and therefore
# discriminate nothing when taken as single-character tokens.
_CJK_STOP_CHARS = frozenset("的了与和及其在为对是被把从而且或者之所以我们你他她它")


def is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def contains_cjk(text: str) -> bool:
    return any(is_cjk(char) for char in text or "")


def fold(text: str | None) -> str:
    """Canonicalise text for matching without losing CJK content.

    Uses the Harness title normaliser so that Navigator matching and Library
    identity agree on case, width, and punctuation.
    """

    normalized = normalize_title(text)
    return "" if normalized == UNKNOWN else normalized


def _strip_latin_suffix(token: str) -> str:
    """Very small stemmer: enough to join ``firms``/``firm``, not a linguist."""

    if len(token) > 4:
        for suffix in ("ies",):
            if token.endswith(suffix):
                return token[: -len(suffix)] + "y"
        for suffix in ("sses", "ches", "shes"):
            if token.endswith(suffix):
                return token[:-2]
        for suffix in ("ing", "ers"):
            if token.endswith(suffix):
                return token[: -len(suffix)]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str | None, *, keep_stop_words: bool = False) -> list[str]:
    """Return the searchable token stream for one piece of text.

    Latin runs contribute the literal token plus a lightly stemmed form.  CJK
    runs contribute unigrams and adjacent bigrams.  Order is preserved so that
    callers can compute positional statistics if they ever need to.
    """

    folded = fold(text)
    if not folded:
        return []

    tokens: list[str] = []
    buffer: list[str] = []

    def flush_cjk() -> None:
        if not buffer:
            return
        run = "".join(buffer)
        buffer.clear()
        for char in run:
            if keep_stop_words or char not in _CJK_STOP_CHARS:
                tokens.append(char)
        for index in range(len(run) - 1):
            tokens.append(run[index : index + 2])

    # Pass 1: CJK runs -> unigrams + bigrams.  A run ends at any non-CJK
    # character, so a bigram never spans a word boundary or a Latin insert.
    for char in folded:
        if is_cjk(char):
            buffer.append(char)
        else:
            flush_cjk()
    flush_cjk()

    # Pass 2: Latin/digit runs.  ``fold()`` has already lowercased and mapped
    # punctuation to spaces, and CJK characters never match this pattern, so a
    # second sweep over the same string is safe and keeps the two alphabets
    # independent.
    for match in _LATIN_RUN_RE.finditer(folded):
        token = match.group(0)
        if not keep_stop_words and token in _STOP_WORDS:
            continue
        tokens.append(token)
        stemmed = _strip_latin_suffix(token)
        if stemmed != token:
            tokens.append(stemmed)

    return tokens


def tokenize_unique(text: str | None) -> set[str]:
    return set(tokenize(text))


@dataclass(frozen=True)
class TokenizedField:
    """A named field ready for weighted scoring."""

    name: str
    weight: float
    tokens: tuple[str, ...]

    @property
    def length(self) -> int:
        return len(self.tokens)


def normalize_person_query(text: str | None) -> list[str]:
    """Split an author query into individual name candidates.

    ``"王海森 李纲"`` is two authors, not one name; CJK names are whitespace
    separated in this corpus while Latin names are not, so a CJK run of 2-4
    characters is treated as a whole name rather than being split further.
    """

    folded = fold(text)
    if not folded:
        return []
    parts = [part for part in folded.split() if part]
    names: list[str] = []
    latin_buffer: list[str] = []
    for part in parts:
        if contains_cjk(part):
            if latin_buffer:
                names.append(" ".join(latin_buffer))
                latin_buffer = []
            names.append(part)
        else:
            latin_buffer.append(part)
    if latin_buffer:
        names.append(" ".join(latin_buffer))
    return names


def display_terms(tokens: Iterable[str], *, limit: int = 5) -> tuple[str, ...]:
    """Pick the tokens worth showing a human.

    The scorer needs CJK unigrams for recall, but ``「器」`` in an explanation
    tells a reader nothing.  For display, prefer multi-character tokens and drop
    bare CJK characters entirely; if that leaves nothing, fall back to whatever
    there is rather than showing an empty reason.
    """

    ordered = list(dict.fromkeys(token for token in tokens if token))
    meaningful = [token for token in ordered if len(token) > 1]
    chosen = meaningful or ordered
    chosen.sort(key=lambda token: (-len(token), token))
    return tuple(chosen[:limit])


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


__all__ = [
    "TokenizedField",
    "contains_cjk",
    "display_terms",
    "fold",
    "is_cjk",
    "normalize_person_query",
    "strip_accents",
    "tokenize",
    "tokenize_unique",
]
