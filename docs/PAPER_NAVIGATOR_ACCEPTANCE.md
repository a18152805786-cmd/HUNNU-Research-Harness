# PAPER NAVIGATOR — ACCEPTANCE

Signed off against the live 179-WORK / 192-VERSION corpus on 2026-08-30.
Evidence: [PAPER_NAVIGATOR_TEST_REPORT.md](PAPER_NAVIGATOR_TEST_REPORT.md).

---

## FINAL RESULT — PAPER RESEARCH NAVIGATOR

```text
Status:                          PASS

Existing paper library changed:  NO
Expected:                        NO

Works indexed:                   179   (179 expected)
Physical versions recognized:    192   (192 expected)
Topics recognized:               337 assignments (337 expected)
                                  39 unique topic values (39 expected)
Nested variants:                  13   (13 expected)

Search commands implemented:     paper-search
                                 paper-lookup
                                 paper-fulltext
                                 paper-related
                                 paper-pack
                                 paper-gaps
                                 paper-verify-citation
                                 paper-index {status,build,validate,rebuild,drop}
                                 paper-fingerprint

Lookup:                          PASS
Fulltext resolution:             PASS
Variant deduplication:           PASS
Related papers:                  PASS
Reading pack:                    PASS
Citation lookup:                 PASS
NOT_IN_LIBRARY handoff:          PASS
Gap helper:                      PASS
Chinese retrieval:               PASS
English retrieval:               PASS
CAJ handling:                    PASS
Regression:                      PASS

Canonical SHA changes:           0    (0 expected)
Catalog changes:                 0    (0 expected)
Unexpected physical file changes: 0   (0 expected)
Potential paper loss:            NONE (NONE expected)

Test suite:                      688 passed, 0 failed
                                 (550 pre-existing, unchanged + 138 new)
Failure modes exercised:         20 / 20 pass
```

## Library freeze — measured, not asserted

An independent fingerprint script that does not import `hunnu_harness` was run
before any Navigator code existed and again after the whole implementation and
test suite had run:

```
fingerprint_sha256 BEFORE  ddcfd8f1e325d5e3f876817fc0b7ead124fcfcd372421ad171b9869a28908366
fingerprint_sha256 AFTER   ddcfd8f1e325d5e3f876817fc0b7ead124fcfcd372421ad171b9869a28908366
diff of the two files      no differences
```

Covered by that digest: byte-level SHA-256 of all four catalog files, SHA-256 and
size of all 192 physical papers, the complete `paper_id` list, per-work canonical
SHA map, per-version SHA map, managed-path map, per-work topic assignments, and
the full 531-entry `papers_by_topic` listing.

In addition, a runtime guard wraps every mutating filesystem primitive
(`builtins.open`, `Path.open/write_text/write_bytes/unlink/replace/rename/chmod/
mkdir`, `shutil.rmtree`) during a full Navigator exercise and asserts that **zero**
write calls resolve inside `library/` or `papers_by_topic/`.

Nothing in the frozen set was renamed, re-hashed, re-imported, re-linked, or
regenerated. No PDF was copied to a second location.

## Capability coverage

| Brief | Command | Status | Evidence |
|---|---|---|---|
| A — Natural-language search | `paper-search` | PASS | TEST D, E; bilingual recall both directions |
| B — Structured lookup | `paper-lookup` | PASS | TEST A, B, C; title/author/DOI/paper_id/journal |
| C — Full-text availability | `paper-fulltext` | PASS | TEST G, H; all 179 works resolve `AVAILABLE` |
| D — Related papers | `paper-related` | PASS | TEST I; WORK-unique, seed excluded, named overlap |
| E — Research-question search | `paper-search` | PASS | TEST F; CORE/MECHANISM/OUTCOME with reasons |
| F — Reading pack | `paper-pack` | PASS | TEST K; 15 unique works, 0 PDFs copied |
| G — Citation lookup | `paper-verify-citation` | PASS | TEST J; real found, fictional refused |
| H — Research gap helper | `paper-gaps` | PASS | scope statement + interpretation guard enforced by test |

## Requirements the brief called out specifically

| Requirement | Met | How |
|---|---|---|
| WORK-first retrieval; 192 versions never become 192 candidates | ✔ | Ranking operates on `PaperWork`; a version is never a candidate. Verified across 5 broad queries × top 25: zero repeats. |
| Reuse existing canonical/preferred mechanisms where they exist | ✔ | Discovery found none, but the catalog's top-level `sha256` already pins a canonical file. The derived resolver weights that anchor 10× above every other signal, so it can never disagree with the catalog. |
| Two-stage retrieval | ✔ | Stage 1 metadata/topic BM25F over 179 works; Stage 2 full-text rerank over the top 40 only — never re-reads all 192 files. |
| Reuse existing extracted text if present | ✔ | Checked: only 27 stale `.txt` files from a 2026-08-17 one-off script, 15% coverage, pre-consolidation. Not reused; a fresh derived index was built instead, and the reason is recorded. |
| No blind vector database | ✔ | Lexical BM25F + curated topics + auditable lexicon. No new dependency, no network, fully offline. Semantic hook exists, is off by default, and is verified absent-safe. |
| Lexical fallback if embeddings unavailable | ✔ | Nothing calls an embedding service. A raising reranker is caught and the lexical result is returned with the error reported. |
| Chinese + English retrieval | ✔ | CJK unigram+bigram tokenizer plus an explicit cross-language concept table. `AI washing` ↔ `人工智能漂洗` ↔ `AI漂洗` ↔ `漂智` all verified. |
| Auditable query normalization | ✔ | Every response carries `query_normalization` with the original, the normalized form, concepts matched, every expansion added, and the weights applied. |
| Topic metadata as a first-class signal | ✔ | Topics weighted 2.5, second only to title. A work whose title does not match but whose curated topic does is still recalled (unit-tested). |
| Unified machine schema | ✔ | `navigator-0.1`, using the catalog's own vocabulary (`paper_id`, `managed_path`, `sha256`) rather than a parallel one. |
| Attach to existing CLI | ✔ | Nine subcommands on `hunnu-harness`, matching the existing lazy-import, sorted-JSON, exit-code conventions. No parallel entry point. |
| Retrieval provenance, no black-box score | ✔ | `matched_by` (field, term, weight, contribution, source, concept), `concept_evidence`, and a generated `relevance_reason`. No result is emitted without one. |
| Passage citations traceable | ✔ | Every passage carries `paper_id`, `version_sha256`, `managed_path`, `page`, `ordinal`, `chunk_id`, `text_sha256`. Verified that each `version_sha256` belongs to its WORK. |
| Reading packs copy no PDFs | ✔ | Verified by scanning the pack tree: 0 `*.pdf` / `*.caj` files. Each entry records the canonical path plus a verified SHA-256. |
| `NOT_IN_LIBRARY` → handoff, no silent download | ✔ | No network code path exists. Handoff returns identity + an `agent-route` request hint. |
| Gap helper stays a coverage claim | ✔ | Scope statement and interpretation guard in every response, asserted by test. |
| Index build / validate / rebuild / status | ✔ | All four implemented; `rebuild` reconstructs from the catalog alone. |
| Index is never a source of truth | ✔ | Metadata retrieval works with the index absent (162 ms cold). Deleting `paper_retrieval/` loses no paper, version, or topic assignment. |
| Rollback without touching the library | ✔ | Delete one package + two additive hunks + one derived directory. The 179 works are not in the rollback path. |

## Scope discipline

Built: find, rank, resolve, package, route.

Deliberately not built: a PDF viewer, a Zotero/citation-manager clone, an
Obsidian clone, a second paper database, a novelty oracle, and a vector store.

## Changes to existing files

| File | Change |
|---|---|
| `src/hunnu_harness/paths.py` | **additive** — new path constants for topic metadata and the derived `paper_retrieval/` tree. No existing constant altered. |
| `src/hunnu_harness/cli.py` | **additive** — registers the `paper-*` subparsers and dispatches them, using the existing lazy-import pattern. Two hunks. |
| `AGENTS.md` | **additive** — Rules 64–68, the Navigator section. No existing rule altered. |

Everything else is new: `src/hunnu_harness/navigator/` (14 modules), two test
files, five documents.

Unchanged, as required: `literature/library.py`, `literature/normalization.py`,
`literature/fulltext.py`, `literature/models.py`, `literature/downloads.py`,
`literature/workflow.py`, every adapter, `agent_entrypoint.py`, all four catalog
files, all 192 physical papers, all 531 `papers_by_topic` entries,
`topic_taxonomy.json`, and `PAPER_LIBRARY_README.md`.

## Known limitations

These are stated because they bound what the PASS above means.

1. **One work has no extractable text.** `P2CCFF87ED94E` is a scanned,
   image-only PDF. It is fully searchable on metadata and topics but contributes
   no passages. OCR was not attempted — `rapidocr-onnxruntime` is present in the
   venv but adding an OCR path is a scope decision for the user, not a Navigator
   requirement.

2. **CAJ text is not extractable.** The corpus's single CAJ is detected, listed,
   and correctly deprioritised behind its PDF sibling. No CAJ extractor exists
   in this environment and none was written.

3. **Retrieval quality has no gold standard.** The acceptance tests assert
   specific known-correct outcomes on the real corpus. That is stronger than
   synthetic tests and weaker than a measured precision/recall figure against a
   human-labelled relevance set, which does not exist for this corpus.

4. **The concept lexicon is hand-built and corpus-specific.** 21 concepts
   covering this library's subject matter. It is explicit and diffable by design,
   but a query about a construct not in the table falls back to pure lexical
   matching — correct behaviour, and lower recall than a curated concept would give.

5. **Growth beyond ~10,000 works is analytical, not measured.** The Stage-2 cost
   is already corpus-size independent; Stage 1 would need the documented move to
   SQLite FTS5 well beyond the current scale.

6. **Semantic reranking is unexercised.** The hook is verified absent-safe but no
   embedding provider was configured, so nothing is claimed about ranking quality
   with embeddings.

## Verdict

```text
PASS
```

The Navigator answers "find it → is the full text here → recall by research
question → find related work → build a reading pack → locate the full text for
citation checking" through one entry point, over the real 179-WORK corpus, with
every result explaining itself — and the sealed library is byte-identical before
and after.
