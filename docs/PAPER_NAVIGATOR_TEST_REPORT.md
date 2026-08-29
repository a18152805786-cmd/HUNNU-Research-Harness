# PAPER NAVIGATOR — TEST REPORT

Measured on the live corpus, 2026-08-30.
Harness `0.2.21` + Navigator `navigator-0.1`, Python 3.12.13 (`.venv`), Windows 11.

---

## 1. Suite results

```
Baseline before any Navigator code   550 passed in 21.5 s
After Navigator                      688 passed in 29.5 s
```

| Suite | Tests | Result |
|---|---|---|
| Pre-existing Harness suite | 550 | **all pass, unchanged** |
| `tests/test_navigator_core.py` (synthetic, temp dirs) | 62 | **pass** |
| `tests/test_navigator_acceptance.py` (real 179 WORKS) | 76 | **pass** |
| **Total** | **688** | **0 failures, 0 errors** |

No pre-existing test was modified, skipped, or weakened. The 138 new tests are
purely additive.

## 2. Acceptance tests on the real corpus

Every test below reads the live Global Paper Library. They skip cleanly when the
Output Root is absent rather than failing.

### Corpus shape — the Navigator sees what the sealed audit recorded

| Measure | Expected | Measured |
|---|---|---|
| WORKS | 179 | **179** ✔ |
| Physical versions | 192 | **192** ✔ |
| Nested variants | 13 | **13** ✔ |
| Work-topic assignments | 337 | **337** ✔ |
| Unique topic values | 39 | **39** ✔ |
| Zero-topic works | 0 | **0** ✔ |
| Catalog read degradations | 0 | **0** ✔ |

### TEST A — exact title lookup · PASS

| Case | Result |
|---|---|
| `人工智能漂洗抹杀了企业技术创新吗` | 1 exact match → `P4451110E69BF` |
| Preferred version resolved and readable | `AVAILABLE`, file present on disk |
| English exact title (bank-loans paper) | 1 exact match → `PBBC02C012124` |

### TEST B — author lookup · PASS

| Case | Result |
|---|---|
| `author:王海森 李纲` | recalls `P4451110E69BF` |
| Bare `王海森 李纲` (no field prefix) | recalls `P4451110E69BF` |
| `author:袁春生` | recalls `PEC61CA32AC26` |

### TEST C — DOI lookup · PASS

- 102 works carry a DOI. **40 sampled, every one resolved to exactly one WORK** —
  `exact_match_count == 1`, correct `paper_id`.
- `https://doi.org/…` URL form resolves identically.
- Unknown DOI returns `NOT_IN_LIBRARY` with an **empty** match list.

### TEST D — Chinese semantic/topic query · PASS

`人工智能漂洗 审计风险`, metadata-only, top 10:

```
 1  PEC61CA32AC26  CORE   198.20  企业人工智能漂洗与审计师风险决策——来自多模态数据的证据
 2  PE46E6E8E1E31  CORE   105.38  人工智能伦理漂洗现象及其治理
 3  P4451110E69BF  CORE   100.13  人工智能漂洗抹杀了企业技术创新吗
 4  P33A652DF0A84  CORE    99.81  双重期望差距对企业人工智能漂洗的影响
 5  P7CEBC0A4F626  MECH    92.91  货币政策、内部控制质量与债务融资成本
 6  PF7C70A2D2093  CORE    91.56  虚假的智能，脆弱的合作：客户企业漂智…
```

The intended paper ranks first; ≥3 CORE works recalled; every result carries
`matched_by` provenance and a `relevance_reason`.

### TEST E — English query · PASS

`AI washing bank loans` → `PBBC02C012124` ("The impact of AI washing on
enterprises' access to bank loans") ranks **first**.

Cross-language recall verified in both directions:

- English `AI washing` recalls the Chinese works `PEC61CA32AC26` and `P4451110E69BF`.
- Chinese `人工智能漂洗` recalls the English work `PBBC02C012124`.

The expansion that made this possible is reported in the response
(`query_normalization.expansions`), not hidden.

### TEST F — mechanism query · PASS

`AI washing earnings quality audit mechanism` returns differentiated roles, each
justified by a named concept:

```
role       secondary   paper
CORE       +MECHANISM  PEC61CA32AC26  企业人工智能漂洗与审计师风险决策
MECHANISM  +OUTCOME    P16291547A7AD  Audit effort and earnings management
MECHANISM  +OUTCOME    P4ADEAFA4F6FF  Does Ineffective Internal Control over Financial Rep…
CORE                   PE46E6E8E1E31  人工智能伦理漂洗现象及其治理
OUTCOME    +MECHANISM  P48F6ACEC90DC  How does financial reporting quality relate to…
```

All three question concepts (`ai_washing`, `audit_risk`, `earnings_quality`) are
detected. A Chinese phrasing of the same question also differentiates roles.

**A defect was found and fixed here.** Role assignment initially used fixed
precedence (MECHANISM before OUTCOME), which silently hid the second role of a
paper that genuinely serves both. Roles are now chosen by evidence strength
(title 3.0 > topics/keywords 2.0 > journal/full text 1.0), with the others
reported as `secondary_roles`.

### TEST G — variant handling · PASS

`P21FAD7D7F87D` holds four physical PDFs:

| Check | Result |
|---|---|
| `version_count` | 4 |
| Preferred version | the catalog's own canonical SHA |
| Occurrences in a search result list | **1** |
| Repeats across 5 broad queries (`创新`, `AI washing`, `供应链`, `审计`, `数字化转型`), top 25 each | **0** |
| All 9 multi-version works resolve deterministically | yes, verified twice each |

`PB3731259E47C` — whose canonical file is named `P6C502A1FD06E.pdf` — resolves
correctly, proving no path is derived from a `paper_id`.

### TEST H — CAJ handling · PASS

`P23C61576ADCE` (the corpus's only CAJ, files named `P6B4B9A7DE3F3.*`):

| Check | Result |
|---|---|
| Versions detected | 2 — `{PDF, CAJ}` |
| Preferred | PDF, `is_catalog_canonical: true` |
| Work-level status | `AVAILABLE`, `readable: true` |
| CAJ version | `exists: true`, `machine_readable: false` |
| CAJ reason | "format not machine-readable in this environment" |
| CAJ file on disk | present, `.caj` extension confirmed |

The CAJ is reported as lawfully held but not extractable — not as missing, and
not as a reason to re-acquire.

### TEST I — multi-topic works · PASS

- 38 works carry ≥3 topic assignments; 11 carry ≥4.
- **Every** topic of a multi-topic work independently recalls it (each subtopic
  queried separately, top 30).
- Relatedness uses multiple shared subtopics: e.g. `P1820E64BDBBD` (4 topics) →
  `PBF0C1885D028` sharing 融资约束与资本配置 + 金融错配与资源配置 + 银行信贷与金融发展.
- Related results are WORK-unique and never include the seed.

### TEST J — NOT_IN_LIBRARY · PASS

| Fictional input | Result |
|---|---|
| `Okonkwo, R. & Halvorsen, T. (2031). Quantum ledger washing…` | `NOT_IN_LIBRARY`, `match: null`, `paper_id: null` |
| `赵子龙、孙尚香(2030). 量子账簿漂洗对区块链审计鉴证的抑制效应…` | `NOT_IN_LIBRARY`, no candidates |
| `doi:10.9999/fabricated.2030.0001` | `NOT_IN_LIBRARY` |
| Handoff returned | identity + `acquisition_request_hint` + `agent-route` entry point |
| Real citations still found | Chinese title → `P4451110E69BF`; real DOI → correct WORK |

**A hallucination risk was found and fixed here.** The first implementation fell
back to lexical similarity when an exact identity missed, so an unknown DOI
returned 10 plausible-looking papers. An exact-identity query (DOI, `paper_id`,
or an explicit `title:`/`doi:` filter) now never falls back; approximate matches
are separately labelled `FOUND_APPROXIMATE` with `confidence: APPROXIMATE`.

### TEST K — reading pack · PASS

`paper-pack --query "AI washing 如何通过审计风险影响盈余质量？" --top 15`:

| Check | Result |
|---|---|
| Entries | 15 |
| Unique WORKS | 15 (no duplicates) |
| PDFs copied | **0** — verified by scanning the pack tree for `*.pdf`/`*.caj` |
| Every `canonical_path` valid | yes, all 15 files exist on disk |
| Every entry has a 64-char verified SHA-256 | yes |
| Every `paper_id` present in the catalog | yes |
| Files written | `manifest.json`, `ranking.json`, `evidence_plan.json`, `README.md` |

The README groups papers by reading order (CORE → MECHANISM → OUTCOME → METHOD →
BACKGROUND) with the reason each was included.

### TEST L — regression / freeze · PASS

Verified by an **independent** script that does not import `hunnu_harness`, so a
bug in the module under test cannot conceal a change:

```
                       BEFORE                                                             AFTER
catalog_records        179                                                                179
physical_versions      192                                                                192
nested_variants        13                                                                 13
papers_file_count      192                                                                192
topic_assignments      337                                                                337
unique_topic_values    39                                                                 39
papers_by_topic        531                                                                531
fingerprint_sha256     ddcfd8f1e325d5e3f876817fc0b7ead124fcfcd372421ad171b9869a28908366   (identical)
```

`diff` of the two full fingerprint files: **no differences**. The fingerprint
covers byte-level SHA-256 of every catalog file, SHA-256 + size of all 192
physical papers, the full `paper_id` list, per-work canonical and per-version SHA
maps, the managed-path map, per-work topic assignments, and the complete
`papers_by_topic` listing.

The shipped `paper-fingerprint` command was cross-checked against the independent
script: `catalog_files`, `papers_files`, `canonical_sha_map`, `managed_path_map`,
`topics_per_paper`, and `papers_by_topic` are **identical** section by section.

Additionally, `test_no_navigator_call_ever_opens_a_library_path_for_writing`
wraps `builtins.open`, `Path.open`, `write_text`, `write_bytes`, `unlink`,
`replace`, `rename`, `chmod`, `mkdir`, and `shutil.rmtree` for the duration of a
full Navigator exercise (search, lookup, fulltext, related, verify-citation,
gaps, pack, fingerprint) and asserts **zero** mutating calls resolve inside
`library/` or `papers_by_topic/`. This replaced an earlier grep-based check that
produced a false positive — a guard that cannot distinguish a real write from a
substring is not a guard.

## 3. Failure modes

20 of 20 pass. Exercised against the real catalog copied into a temp directory,
with damage injected into the copy; the originals were never touched.

| Condition | Behaviour | Verified |
|---|---|---|
| Catalog missing | `LibraryUnavailable`, exit 2, no traceback | ✔ |
| Corrupt JSONL line | line skipped, other 179 works load, damage reported | ✔ |
| Record without `paper_id` | skipped and reported | ✔ |
| Duplicate `paper_id` | first kept, duplicate reported | ✔ |
| Corrupt catalog → search | still answers, `catalog_status: PARTIAL` | ✔ |
| Topics file missing | catalog loads, search degrades but answers | ✔ |
| Canonical file missing on disk | `FILE_MISSING`; work still searchable | ✔ |
| Index absent | `ABSENT`, metadata-only, full answers | ✔ |
| Index manifest corrupt | `UNREADABLE`, metadata-only, full answers | ✔ |
| Catalog changed after build | `STALE` detected | ✔ |
| Unknown DOI | `NOT_IN_LIBRARY` | ✔ |
| Unknown `paper_id` | `NOT_IN_LIBRARY` | ✔ |
| CAJ / unsupported format | `AVAILABLE_NOT_MACHINE_READABLE`, no crash | ✔ |
| Embedding/rerank service raises | lexical result returned, error reported | ✔ |
| Fictional citation | `NOT_IN_LIBRARY` + handoff | ✔ |

Nothing in this table writes to the library or aborts the Harness.

## 4. Index build

```
works               179
extracted OK        178
image-only PDF        1   (P2CCFF87ED94E — genuinely scanned, no text layer)
chunks             8367
pages read         3245
index size          18 MB
```

**A second defect was found and fixed during the build.** `PBCB753837688` failed
with `UnicodeEncodeError: surrogates not allowed` — a damaged PDF font map makes
`pypdf` emit unpaired surrogates, which are unencodable, so hashing the chunk
aborted the whole document and discarded 10 valid chunks. Extraction now strips
surrogates at the source; that work indexes cleanly and coverage went from
177/179 to 178/179.

The one remaining gap is a real scanned PDF, correctly reported as
`NO_EXTRACTABLE_TEXT`. It remains fully searchable on metadata and topics.

### Rebuild from scratch — byte-for-byte reproducible

`paper-index rebuild` deletes the entire derived index and reconstructs it from
the catalog plus the managed full texts. All 179 chunk files were SHA-256 hashed
before the rebuild and compared after:

```
files before: 179      files after: 179
only in before: []     only in after: []     content differs: []
REBUILD REPRODUCES INDEX BYTE-FOR-BYTE: True
```

(The `manifest.json` build timestamp is excluded from the comparison; its content
fields — 179 works, 192 versions, 178 full-text works, 8367 chunks — reproduce
exactly.) `paper-index validate` afterwards: `valid: true`, `problems: []`,
`status: FRESH`.

The library fingerprint was captured again after the rebuild and is still
identical to the pre-Navigator baseline, so destroying and reconstructing the
entire index touches nothing in the frozen corpus.

Unit-tested equivalently on synthetic data
(`test_index_is_never_the_source_of_truth`): drop → rebuild produces identical
extraction status counts, and the catalog is unaffected.

## 5. Performance

Best of 3 runs, warm process, real corpus:

| Operation | Target | Measured | |
|---|---|---|---|
| Catalog load (cold) | < 150 ms | **11 ms** | ✔ |
| Full-text index load (cold, 8367 chunks) | — | **107 ms** | ✔ |
| `lookup` exact DOI | < 200 ms | **0.1 ms** | ✔ |
| `lookup` exact title | < 200 ms | **0.1 ms** | ✔ |
| `search` metadata-only | < 300 ms | **112 ms** | ✔ |
| `search` + Stage 2 full text | < 1500 ms | **546 ms** | ✔ |
| `fulltext` resolution | — | **< 0.1 ms** | ✔ |
| `related` top 10 | — | **15 ms** | ✔ |
| `pack` top 15 | < 2000 ms | **132 ms** | ✔ |
| `gaps` | — | **157 ms** | ✔ |
| Cold process → first metadata search | — | **162 ms** | ✔ |
| Full index build (179 works, 3245 pages) | one-off | ~4 min | — |

**A performance defect was found and fixed.** Stage 2 initially took 1839 ms
because every query re-tokenised ~2000 chunks. Tokenised chunk documents are now
cached for the process lifetime: **1839 ms → 546 ms**, a 3.4× improvement, with
identical results.

Every target in the design was met.

## 6. Growth

At 179 works the metadata scan is 112 ms, dominated by tokenising the query's
expansion set rather than by corpus size. Scaling properties:

- Stage 1 is O(N) in works. At 10,000 works the metadata documents are roughly
  12 MB and a scan stays well under a second with the load-time inverted index.
- Stage 2 is O(K) where K is the rerank depth (40 by default) — **independent of
  corpus size**.
- Full-text chunks are one file per work, so builds are incremental: a single
  work can be re-extracted without touching the other 178 (exercised for real
  when re-indexing the two failed works).

Beyond ~10,000 works the in-memory postings should move to stdlib `sqlite3` FTS5
behind the same `ranking.py` interface. Not built now; building it for 179 works
would be over-engineering.

## 7. Defects found and fixed

| # | Defect | Severity | Fix |
|---|---|---|---|
| 1 | Unknown DOI fell back to lexical similarity and returned 10 plausible papers | **High** — invites citing a paper the library does not hold | Exact-identity queries never fall back; approximate matches are separately labelled |
| 2 | Unpaired surrogates from a damaged PDF font map aborted extraction of a whole document | Medium | Surrogates stripped at extraction; 178/179 coverage |
| 3 | Fixed role precedence hid a paper's second role | Medium | Evidence-weighted role selection + `secondary_roles` |
| 4 | Stage 2 re-tokenised all candidate chunks per query (1839 ms) | Medium | Process-lifetime chunk document cache (546 ms) |
| 5 | `audit` / `审计` missing from the audit concept, so "audit mechanism" did not match | Medium | Terms added; regression-tested |
| 6 | Write-guard test was a grep and produced a false positive on `cli.py` | Low (test-only) | Replaced with a runtime filesystem interception guard |
| 7 | CJK bigram fragments made human-readable reasons unreadable | Low | `display_terms()` filter for human-facing text only |
| 8 | Matched-passage page list repeated page numbers | Low | Deduplicated and sorted |

## 8. What was not tested

- **CAJ text extraction** — no CAJ extractor exists in this environment. The one
  CAJ is verified as present, format-detected, and correctly not preferred over
  its PDF sibling; extracting its text was neither attempted nor claimed.
- **Semantic reranking** — the hook exists and is verified absent-safe (a raising
  reranker degrades to lexical), but no embedding provider was configured, so
  ranking quality with embeddings is untested and unclaimed.
- **Corpora above ~200 works** — growth properties in §6 are analytical, not measured.
- **Retrieval quality against a human-labelled relevance set** — no gold standard
  exists for this corpus. The acceptance tests assert specific known-correct
  outcomes, which is weaker than a measured precision/recall figure.
