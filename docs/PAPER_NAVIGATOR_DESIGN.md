# PAPER NAVIGATOR — PHASE 1 DESIGN

Design derived from the measured facts in [PAPER_NAVIGATOR_DISCOVERY.md](PAPER_NAVIGATOR_DISCOVERY.md).
Every choice below is justified against a discovery finding, not against the
example snippets in the task brief.

---

## 1. Goals

1. One entry point that answers: *does the library have this paper, where is its
   full text, and what else in the library bears on this research question?*
2. WORK-first retrieval. 192 physical files must never appear as 192 candidates.
3. Bilingual (中文 / English) recall over a corpus that is 162 CJK / 17 Latin.
4. Every result explains why it was recalled.
5. Fully offline, no new runtime dependency, no network.
6. Entirely derived and rebuildable: delete the index, rebuild from the catalog.

## 2. Non-goals

- Not a paper store. `library/catalog/papers.jsonl` remains the only source of truth.
- Not an acquisition path. `NOT_IN_LIBRARY` hands off; it never downloads.
- Not a PDF reader, citation manager, or note system.
- Not a novelty oracle. Gap analysis is *local library coverage*, never a claim
  about the literature at large.
- No modification of `paper_id`, SHA-256, canonical paths, filenames, nested
  version structure, catalog schemas, `papers_by_topic`, or hardlinks.

## 3. Module boundaries

New package, `src/hunnu_harness/navigator/`:

| File | Responsibility |
|---|---|
| `catalog.py` | Tolerant read-only WORK/VERSION model over the frozen catalog |
| `tokenize.py` | Bilingual tokenizer (Latin words + CJK unigram/bigram) |
| `lexicon.py` | Auditable cross-language concept table + role facets |
| `query.py` | Query normalisation, field parsing, auditable expansion |
| `ranking.py` | BM25F scorer with per-term provenance |
| `resolver.py` | Derived preferred-version resolution |
| `fulltext.py` | Bounded PDF text extraction + chunking |
| `index.py` | Derived index build / validate / rebuild / status |
| `search.py` | Two-stage pipeline, role assignment, result schema |
| `related.py` | WORK-level relatedness |
| `packs.py` | Reading-pack generation (references only) |
| `citation.py` | Citation parsing, verification, `NOT_IN_LIBRARY` handoff |
| `gaps.py` | Local coverage analysis |
| `fingerprint.py` | Read-only library fingerprint |
| `cli.py` | Subcommand construction + dispatch, called from `hunnu_harness.cli` |

Changes to existing files, **additive only**:

- `paths.py` — new constants (`LIBRARY_TOPICS_JSONL`, `PAPER_RETRIEVAL_ROOT`, …).
- `cli.py` — register the `paper-*` subparsers and dispatch; imports stay lazy,
  matching the existing `library-stage` / `library-import` pattern.

Nothing else in `src/` is touched. The list of files that must not change is
fixed in DISCOVERY §12 and is verified by test, not by intent.

## 4. Data flow

```
                 library/catalog/papers.jsonl      (SOURCE OF TRUTH, read-only)
                 library/catalog/paper_topics.jsonl
                              |
                        CatalogReader  (tolerant: skips bad lines, records them)
                              |
                          WorkRecord × 179
                       (+ VersionRecord × 192)
                              |
        +---------------------+---------------------+
        |                                           |
   metadata documents                        full-text chunks
   (built in memory, always)                 (derived index, optional)
        |                                           |
   Stage 1: BM25F recall  ---- top K ---->  Stage 2: passage rerank
        |                                           |
        +---------------------+---------------------+
                              |
                    role assignment + provenance
                              |
                     PreferredVersionResolver
                              |
                        SearchResult JSON
```

**The metadata layer needs no build step.** With no derived index present at
all, `paper-lookup`, `paper-search`, `paper-fulltext`, `paper-related`,
`paper-pack`, `paper-gaps`, and `paper-verify-citation` all work, computed live
from the catalog (measured: 11 ms to load, 162 ms cold process to first answer). `paper-index build` only adds the full-text layer
and a load-time cache. This makes the Navigator un-brickable: a corrupt or
deleted index degrades to metadata-only, it does not fail.

## 5. Index design

Root: `Output Root/paper_retrieval/` — a **new** subtree. Nothing existing is
written to. All of it is disposable.

```
paper_retrieval/
  index/
    manifest.json            # schema, build time, source digests, counts
    fulltext/
      <paper_id>.jsonl       # chunk records for that work
    fulltext_manifest.json   # per-work extraction status + source SHA
  reading_packs/
    <pack_id>/
      manifest.json  README.md  ranking.json  evidence_plan.json
```

**As built, the index holds full text only.** An earlier sketch also cached the
metadata documents as `works.jsonl`; measurement removed the need. Building the
metadata layer live from the catalog costs 11 ms, so caching it would buy nothing
and would introduce a second thing that can go stale. The metadata layer is
therefore computed on every load and the derived index is purely the full-text
layer.

Chunk record — every field required for citation-grade traceability:

```json
{"chunk_id":"PEC61CA32AC26:ec61ca32ac26:p7:c2","paper_id":"PEC61CA32AC26",
 "version_sha256":"ec61ca32ac26…","managed_path":"library/papers/PEC61CA32AC26.pdf",
 "page":7,"ordinal":2,"text":"…","text_sha256":"…"}
```

`version_sha256` + `page` + `ordinal` is what makes a matched passage locatable
and re-verifiable. `text_sha256` detects silent chunk corruption.

### Staleness

`manifest.json` records the SHA-256 of `papers.jsonl` and `paper_topics.jsonl`
at build time. `fulltext_manifest.json` records, per work, the `source_sha256`
of the version that was extracted. On load:

- catalog digest differs → `index_status = STALE`
- a work's current preferred-version SHA differs from `source_sha256` → that
  work's chunks are `STALE` and are excluded from Stage 2 (metadata recall for
  it still works)
- index absent → `index_status = ABSENT`, metadata-only mode

`index_status` is echoed in **every** machine response. A stale index never
silently answers as if fresh.

### Why the index cannot become a source of truth

It stores no identity it did not read from the catalog, it has no writer other
than `paper-index build`, and `paper-index rebuild` reconstructs it byte-for-byte
from the catalog plus the managed PDFs. Deleting `paper_retrieval/` entirely and
rebuilding is a tested acceptance case (TEST L / §12).

## 6. Search pipeline

### 6.1 Tokenisation (bilingual)

DISCOVERY §7: `normalize_title` collapses a CJK title into one space-free token,
so BM25 on it alone would match nothing. The Navigator tokenizer:

- NFKC-normalise, casefold (handles full-width/half-width, 全角/半角).
- Latin/digit runs → word tokens.
- CJK runs → **character unigrams *and* adjacent bigrams**.
  `人工智能漂洗` → `人 工 智 能 漂 洗` + `人工 工智 智能 能漂 漂洗`.
  Bigrams carry the discriminative signal (`漂洗`, `智能`); unigrams keep recall
  when a term is written differently. This is the standard CJK-without-a-segmenter
  approach and needs no dictionary and no dependency.
- Latin tokens are also emitted in a stemmed-lite form (plural/possessive strip)
  so `washing`/`wash` and `firms`/`firm` co-occur.

### 6.2 Concept lexicon (cross-language recall)

DISCOVERY §8: `AI washing`, `人工智能漂洗`, `AI漂洗`, `漂智` denote one construct
and all occur. A pure tokenizer cannot bridge them.

`lexicon.py` holds an explicit, diffable table:

```python
CONCEPTS = (
    Concept(key="ai_washing", facet=Facet.PHENOMENON,
            terms=("ai washing","aiwashing","人工智能漂洗","ai漂洗","漂洗",
                   "漂智","ai washing disclosure","talk-walk gap","talk walk gap")),
    ...
)
```

Expansion is **capped and reported**. The response always carries:

```json
"query_normalization": {
  "original": "AI washing 如何通过审计风险影响盈余质量？",
  "normalized": "ai washing 如何通过审计风险影响盈余质量",
  "concepts_matched": ["ai_washing","audit_risk","earnings_quality"],
  "expansions": [{"concept":"ai_washing","added":["人工智能漂洗","ai漂洗","漂智"]}]
}
```

An expanded term is scored at a discount (0.6×) relative to a literal query
term, so a synonym match can never outrank a direct match. The agent can always
see exactly what the query became — the brief's auditability requirement.

### 6.3 Stage 1 — metadata / topic recall (BM25F)

One document per WORK, built from fields with independent weights:

| Field | Weight | Source |
|---|---|---|
| `title` | 3.0 | `papers.jsonl.title` |
| `topics` | 2.5 | `paper_topics.jsonl.topics` (domain + subtopic, `;` then `\`) |
| `keywords` | 2.0 | `paper_topics.jsonl.keywords` |
| `authors` | 1.6 | `papers.jsonl.authors[]` + `first_author` |
| `journal` | 1.0 | `papers.jsonl.journal` |
| `human_name` | 0.8 | `paper_topics.jsonl.human_readable_name` |

BM25 parameters `k1=1.2`, `b=0.75`; field weights applied to term frequency
before the saturation function (BM25F). Topic weight is deliberately second only
to title — DISCOVERY §5 shows topics are dense (337 assignments, 0 zero-topic
works) and hand-curated, so they are the highest-precision signal after the title.

Exact-identity short circuits run first and bypass ranking entirely:
normalised DOI, `paper_id`, and exact normalised title.

### 6.4 Stage 2 — full-text passage rerank

Runs only on the Stage-1 top **K = 40** (configurable), never on all 179. With
an absent or stale index it is skipped and the response says so.

Passage score = BM25 over the chunk token stream, capped contribution
(`0.45 × normalised passage score`) added to the Stage-1 score, so full text
refines the ranking but a single lucky page cannot dominate curated metadata.
Up to 3 matched passages are returned per work, each with `page`, `chunk_id`,
`version_sha256` and the matched terms.

### 6.5 Role assignment

Not a second ranking — a *classification of why this work matched*, derived from
which lexicon facet its matched terms belong to. Facets:

| Facet | Role emitted | Meaning |
|---|---|---|
| `PHENOMENON` | `CORE` | matched the subject of the question itself |
| `MECHANISM` | `MECHANISM` | matched the channel (audit risk, financing constraint, agency cost…) |
| `OUTCOME` | `OUTCOME` | matched the dependent construct (earnings quality, innovation, firm value…) |
| `METHOD` | `METHOD` | matched design/identification vocabulary (DID, IV, RDD, 双重差分…) |
| — | `BACKGROUND` | recalled by general topic overlap only |

A work matching the phenomenon facet **and** ranked in the top tier is `CORE`;
otherwise its dominant matched facet names the role. Works recalled only via
topic/keyword overlap are `BACKGROUND`. Every result also carries
`relevance_reason`, generated from its own provenance records — a sentence built
from what actually matched, not a canned string.

### 6.6 Provenance (`matched_by`)

Every result carries a list, ordered by contribution:

```json
"matched_by":[
 {"signal":"topic","field":"topics","term":"AI漂洗","weight":2.5,"expanded_from":null},
 {"signal":"title","field":"title","term":"漂洗","weight":3.0,"expanded_from":"ai_washing"},
 {"signal":"fulltext","field":"page:7","term":"审计风险","weight":0.45,"chunk_id":"…"}
]
```

No result is ever returned with a bare score and no explanation.

## 7. Preferred version resolution

DISCOVERY §4.2: there is no existing preferred-version mechanism, but the
catalog already pins one canonical file per work via the top-level `sha256`.
The derived resolver **defers to that anchor** and only orders the remainder:

```
score = 1000 × (version.sha256 == work.sha256)          # catalog's own canonical anchor
      +  100 × role_rank(version.version_role)          # CANONICAL_VERSION > PeerReviewed…
                                                        #  > PublisherPDF > import/download
                                                        #  > FORMAT_VARIANT > unknown
      +   10 × format_rank(full_text_format)            # PDF > CAJ > other
      +    1 × machine_readable(path)                   # exists and parses
      tie-break: lexicographic sha256 (deterministic)
```

The anchor term dominates, so the resolver can never disagree with the catalog
about which file is canonical. Roles/format only order variants. This is
**derived**: it writes nothing and changes no recorded relationship.

`fulltext_status` at WORK level:

| Value | Condition |
|---|---|
| `AVAILABLE` | preferred version exists on disk and is machine-readable |
| `AVAILABLE_NOT_MACHINE_READABLE` | file present, format not extractable (the CAJ case) |
| `FILE_MISSING` | catalog references a file that is not on disk |
| `UNREADABLE` | present but fails validation |

The single CAJ work (`P23C61576ADCE`) has a PDF sibling, so it resolves to
`AVAILABLE` with the PDF preferred and the CAJ listed as an available version —
exactly the TEST H expectation.

## 8. Why no vector database

DISCOVERY §7 measured the environment: no embedding client, no vector store, no
local model, and `pyproject.toml` declares one runtime dependency. Adding
embeddings would mean a new dependency **and** a network service, breaking the
offline guarantee for a 179-work corpus where a full BM25F pass over every work
takes single-digit milliseconds.

Chosen: **lexical BM25F + curated topics + auditable cross-language lexicon**,
pure stdlib, deterministic, explainable, testable.

The semantic rerank hook exists but is **off by default and absent-safe**:
`search.py` takes an optional `reranker` callable. If none is supplied — the
default, and the only supported state offline — Stage 2 is lexical. Nothing in
the pipeline can fail because an embedding service is unavailable, because
nothing calls one. Should embeddings ever be introduced, they enter through that
one seam and the lexical path remains the fallback.

## 9. Growth

- Metadata BM25F is O(N) per query. At 179 works a full scan is ~5 ms; at 10,000
  works the metadata documents are roughly 12 MB and a scan stays under ~400 ms
  with an inverted index built at load.
- Stage 2 is O(K), independent of N (K = 40 by default).
- Full-text chunks are stored one file per work, so the build is incremental and
  a single work can be re-extracted without touching the rest.

Beyond ~10,000 works the in-memory postings should move to stdlib `sqlite3`
FTS5 behind the same `ranking.py` interface. That is a documented later step,
not built now — building it now would be over-engineering for 179 works.

## 10. CLI / agent surface

House style is flat hyphenated subcommands on `hunnu-harness` (DISCOVERY §2),
so the brief's `paper search …` becomes:

| Brief | Implemented |
|---|---|
| `paper search "<q>"` | `hunnu-harness paper-search --query "<q>" [--top N] [--no-fulltext]` |
| `paper lookup "<id>"` | `hunnu-harness paper-lookup --query "<identity>"` (accepts `DOI:…`, `PXXXXXXXXXXXX`, title, authors) |
| `paper fulltext <id>` | `hunnu-harness paper-fulltext --paper-id PXXXXXXXXXXXX` |
| `paper related <id>` | `hunnu-harness paper-related --paper-id PXXXXXXXXXXXX [--top N]` |
| `paper pack "<q>"` | `hunnu-harness paper-pack --query "<q>" [--top 15]` |
| `paper gaps "<q>"` | `hunnu-harness paper-gaps --query "<q>"` |
| `paper verify-citation "<c>"` | `hunnu-harness paper-verify-citation --citation "<c>"` |
| — | `hunnu-harness paper-index {status,build,validate,rebuild}` |
| — | `hunnu-harness paper-fingerprint [--output PATH] [--compare PATH]` |

All emit `json.dumps(..., ensure_ascii=False, indent=2, sort_keys=True)` to
stdout, matching `library-stage` / `library-import`. Exit codes: `0` success,
`2` library/catalog unavailable, `3` not found / `NOT_IN_LIBRARY`, `4` index
operation failed.

Python API mirrors the CLI (`PaperNavigator.search/lookup/fulltext/related/
pack/gaps/verify_citation`) so an in-process agent need not shell out.

## 11. Result schema

```json
{
  "schema_version": "navigator-0.1",
  "query": "...",
  "query_normalization": { "original": "...", "normalized": "...",
                            "concepts_matched": [...], "expansions": [...] },
  "index_status": "FRESH | STALE | ABSENT | PARTIAL",
  "degraded": [],
  "works_searched": 179,
  "results": [
    { "paper_id": "PEC61CA32AC26", "title": "...", "authors": [...],
      "year": "2026", "journal": "...", "doi": "unknown",
      "topics": ["01_人工智能与数字经济\\AI漂洗", "..."],
      "relevance_score": 18.42, "relevance_role": "CORE",
      "relevance_reason": "...", "matched_by": [...],
      "fulltext_status": "AVAILABLE",
      "preferred_version": {"managed_path":"library/papers/PEC61CA32AC26.pdf",
                            "absolute_path":"C:\\...","format":"PDF",
                            "sha256":"...","version_role":"...",
                            "machine_readable": true},
      "available_versions": [ ... ],
      "matched_passages": [ {"chunk_id":"...","page":7,"version_sha256":"...",
                             "terms":["审计风险"],"text":"..."} ] }
  ]
}
```

Field names follow the existing catalog vocabulary (`paper_id`, `managed_path`,
`sha256`, `full_text_format` → `format`) rather than inventing a parallel one.

## 12. Failure modes

| Condition | Behaviour |
|---|---|
| `papers.jsonl` missing | exit 2, `{"status":"LIBRARY_UNAVAILABLE","reason":...}`; never a traceback |
| `papers.jsonl` line unparseable | skip the line, append to `degraded[]`, serve the rest, `index_status=PARTIAL` |
| record missing `paper_id` | same as above |
| `paper_topics.jsonl` missing | metadata search continues without the topic field; `degraded[]` records it |
| canonical file missing on disk | `fulltext_status=FILE_MISSING`; work still searchable |
| index stale | `index_status=STALE`, Stage 2 skipped for affected works |
| index corrupt / unreadable | treated as `ABSENT`; metadata-only; `degraded[]` records it |
| CAJ / unsupported format | `AVAILABLE_NOT_MACHINE_READABLE`; never a crash |
| PDF unparseable at build | that work recorded `EXTRACTION_FAILED`; build continues |
| unknown DOI | `NOT_IN_LIBRARY` + handoff object |
| duplicate `paper_id` in catalog | first wins, duplicate into `degraded[]` |
| reranker raises | caught; lexical result returned; `degraded[]` records it |

**Nothing in this table writes to the library or aborts the Harness.**

## 13. Reading packs

`paper-pack` writes to `paper_retrieval/reading_packs/<pack_id>/` and copies
**no PDF**. `manifest.json` holds `paper_id`, `managed_path`, `absolute_path`,
`preferred_version`, `relevance_role`, `relevance_score`, `relevance_reason`,
`matched_by`, `fulltext_status`. A `SHA-256` per referenced canonical file is
recorded so a pack can be re-validated later against a changed library.
`pack_id` = `PACK_<UTC timestamp>_<8-hex of query hash>` — deterministic per
query, unique per run. Duplicate WORKs are impossible: the pack is built from
the WORK-level result list.

## 14. Rollback

1. `git revert` the Navigator commits, **or** delete `src/hunnu_harness/navigator/`
   and the two additive hunks in `paths.py` / `cli.py`.
2. `rm -r "<Output Root>/paper_retrieval"`.

That is the whole surface. No catalog migration, no file move, no rename, no
re-hash. The 179 works and 192 versions are never in the rollback path.

## 15. Tests

| Layer | Coverage |
|---|---|
| Unit (synthetic fixtures, temp dirs) | tokenizer, lexicon expansion, BM25F, query parsing, resolver ordering, chunking, all 13 failure modes |
| Acceptance (real 179 WORK corpus) | TEST A–L from the brief, skipped cleanly when the Output Root is absent |
| Regression | independent fingerprint before/after must be byte-identical |
| Freeze | the DISCOVERY §12 file list is asserted unmodified |

Acceptance tests read the live corpus but open every file read-only and assert
the fingerprint afterwards.

## 16. Performance expectations

Targets on this machine (179 works / 192 versions), to be measured and reported
in the test report, not asserted here:

| Operation | Target |
|---|---|
| catalog load (cold) | < 150 ms |
| `paper-lookup` exact | < 200 ms |
| `paper-search` metadata-only | < 300 ms |
| `paper-search` with Stage 2 | < 1.5 s |
| `paper-pack --top 15` | < 2 s |
| full index build (192 files) | one-off, minutes |

## 17. Files that will NOT be modified

The DISCOVERY §12 list, unchanged. Additionally: no file under
`Output Root/library/`, `Output Root/papers_by_topic/`, or `Output Root/论文审稿`
is opened for writing by any Navigator code path. This is enforced by test, not
by convention.
