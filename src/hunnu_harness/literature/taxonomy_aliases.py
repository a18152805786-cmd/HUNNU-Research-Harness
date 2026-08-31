"""English aliases for a configurable taxonomy's subtopic names.

The taxonomy-name signal corroborates an assignment: a title containing a
configured subtopic's own name can be credited through the same signal path as
the taxonomy itself.  The taxonomy remains frozen; aliases are additive data
that can be replaced wholesale or left empty in an Output Root override.

Like the Navigator lexicon, this table is explicit rather than learned.  It is
diffable, testable, and reviewable, while the matching and scoring rules remain
implemented here.

Matching rules, and why they differ from the Chinese name terms:

* **Word boundaries.**  Chinese name terms match by substring because CJK has
  no word boundaries.  A Latin alias matched the same way can fire inside
  other words, so alias hits require word boundaries, exactly as the lexicon's
  Latin terms do.
* **Best hit only, per field.**  The parts of a compound Chinese name are
  distinct constructs and both appearing is genuinely stronger evidence, so
  they accumulate.  Aliases for one subtopic are *synonymous variants* of one
  construct; counting several of them would multiply one piece of evidence,
  so only the most specific hit per field is credited.
* **Specificity by word count.**  A Chinese name term's weight grows with its
  character length.  The equivalent unit for English is the word: one English
  word carries about what a two-character Chinese term does (``创新`` /
  ``innovation``), and a two-word phrase what a four-character compound does
  (``融资约束`` / ``financing constraints``).  ``ALIAS_WORD_CJK_EQUIVALENT``
  states that conversion; the weight then goes through the same formula and
  the same cap as a Chinese name term.

What is deliberately absent:

* Bare high-frequency abbreviations already covered by the taxonomy name
  itself.  Duplicating one here would double-credit one hit.
* Generic tool words.  A work can name a tool without being *about* tools;
  crediting a method subtopic from that alone would misfile applied work.
* Aliases for the catch-all taxonomy value -- routing English works into it
  automatically is exactly what review is for.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from .. import paths
from ..navigator.tokenize import fold

# One English word counts as this many CJK characters when its specificity
# weight is computed (see the module docstring).
ALIAS_WORD_CJK_EQUIVALENT = 2

_DEFAULT_RESOURCE_NAME = "taxonomy_aliases.default.json"
_UNSET = object()


def _invalid_aliases(source: object, detail: str) -> ValueError:
    return ValueError(
        f"Invalid taxonomy aliases at {source}: {detail}. "
        "Repair it as JSON with a top-level 'aliases' object whose values "
        "are Latin-string arrays, or remove the override to use the packaged "
        "default."
    )


def _read_json(source: Any) -> Any:
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise _invalid_aliases(source, f"JSON parsing failed ({exc.msg})") from exc
    except (OSError, UnicodeError) as exc:
        raise _invalid_aliases(source, f"the file could not be read ({exc})") from exc


def _aliases_from_payload(payload: Any, *, source: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("aliases"), Mapping):
        raise _invalid_aliases(source, "the top level must contain an 'aliases' object")

    aliases: dict[str, tuple[str, ...]] = {}
    for subtopic, values in payload["aliases"].items():
        if not isinstance(subtopic, str) or not subtopic.strip():
            raise _invalid_aliases(source, "alias keys must be non-empty strings")
        if not isinstance(values, list):
            raise _invalid_aliases(source, f"aliases[{subtopic!r}] must be an array of strings")
        if any(not isinstance(alias, str) or not alias.strip() for alias in values):
            raise _invalid_aliases(source, f"aliases[{subtopic!r}] contains an empty or non-string alias")
        if any(any(ord(char) >= 0x2E80 for char in alias) for alias in values):
            raise _invalid_aliases(source, f"aliases[{subtopic!r}] must contain Latin-only aliases")
        if any(not fold(alias) for alias in values):
            raise _invalid_aliases(source, f"aliases[{subtopic!r}] contains an alias that folds to nothing")
        aliases[subtopic] = tuple(alias.strip() for alias in values)
    return aliases


def load_taxonomy_aliases(
    *, override_path: Path | str | None = None, default_resource: Any = _UNSET
) -> dict[str, tuple[str, ...]]:
    """Load the user override, packaged default, or a valid empty mapping."""

    candidate = Path(override_path or paths.TAXONOMY_ALIASES_JSON)
    if candidate.exists():
        return _aliases_from_payload(_read_json(candidate), source=candidate)

    resource = default_resource
    if resource is _UNSET:
        resource = resources.files(__package__).joinpath(_DEFAULT_RESOURCE_NAME)
    if resource is not None and resource.is_file():
        return _aliases_from_payload(_read_json(resource), source=resource)
    return {}


# Keyed by the configured subtopic value, exactly as it appears in the
# taxonomy.  Invalid data is rejected by the loader rather than repaired.
ENGLISH_SUBTOPIC_ALIASES: Mapping[str, tuple[str, ...]] = load_taxonomy_aliases()


def _folded_table() -> dict[str, tuple[str, ...]]:
    """Fold every alias once, longest-word-count first so the best hit wins early."""

    table: dict[str, tuple[str, ...]] = {}
    for subtopic, aliases in ENGLISH_SUBTOPIC_ALIASES.items():
        folded = [term for term in (fold(alias) for alias in aliases) if term]
        folded.sort(key=lambda term: (-len(term.split()), -len(term), term))
        table[subtopic] = tuple(dict.fromkeys(folded))
    return table


_FOLDED_ALIASES = _folded_table()


def folded_aliases_for(subtopic: str) -> tuple[str, ...]:
    """Every folded alias for one frozen subtopic value, best-first."""

    return _FOLDED_ALIASES.get(subtopic, ())


def alias_word_count(folded_alias: str) -> int:
    return len(folded_alias.split())


def _boundary_hit(padded: str, term: str) -> bool:
    """True when *term* occurs in *padded* on word boundaries.

    Same contract as the lexicon's Latin matching: ``ai`` must not fire inside
    ``said`` or ``chain``.  *padded* is the folded text wrapped in one space on
    each side so the boundary test needs no edge cases.
    """

    start = 0
    while True:
        position = padded.find(term, start)
        if position < 0:
            return False
        before = padded[position - 1]
        after_index = position + len(term)
        after = padded[after_index] if after_index < len(padded) else " "
        if not before.isalnum() and not after.isalnum():
            return True
        start = position + 1


def best_alias_hit(folded_text: str, subtopic: str) -> str | None:
    """The most specific alias of *subtopic* that occurs in *folded_text*.

    Returns one folded alias or ``None``.  One hit at most, by design: the
    aliases of a subtopic are synonymous variants of a single construct, and
    crediting several of them would count the same evidence twice.
    """

    if not folded_text:
        return None
    padded = f" {folded_text} "
    for alias in _FOLDED_ALIASES.get(subtopic, ()):
        if _boundary_hit(padded, alias):
            return alias
    return None


__all__ = [
    "ALIAS_WORD_CJK_EQUIVALENT",
    "ENGLISH_SUBTOPIC_ALIASES",
    "alias_word_count",
    "best_alias_hit",
    "folded_aliases_for",
]
