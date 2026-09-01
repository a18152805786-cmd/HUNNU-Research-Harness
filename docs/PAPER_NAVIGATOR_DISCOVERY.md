# PAPER NAVIGATOR — PHASE 0 DISCOVERY

Read-only inventory of the Harness as it exists before any Navigator code.
**Production changes in this phase: 0.**

Discovery date: 2026-08-30
Harness version at discovery: `0.2.21`
Branch: `agent/v0.2.22-paper-research-navigator` (cut from `agent/v0.2.21-identity-gate-fix`)

Every number below was measured from the live repository and live Output Root,
not copied from the task brief. Where a measured value disagreed with an
assumption, the measured value is recorded and the discrepancy is called out.

---

## 1. Root separation

| Role | Path | Writable by Navigator? |
|---|---|---|
| Core Root (source, tests, docs) | `<Harness Root>` | new module + tests + docs only |
| Output Root (all runtime artifacts) | `<Output Root>` | derived index only, under a new subtree |

`src/hunnu_harness/paths.py` derives every runtime path from `OUTPUT_ROOT` and
raises at import time if the Output Root is inside the Core Root. It already
exports `LIBRARY_ROOT`, `LIBRARY_PAPERS_DIR`, `LIBRARY_NOTES_DIR`,
`LIBRARY_CATALOG_DIR`, `LIBRARY_CATALOG_JSONL`, `LIBRARY_CATALOG_CSV`,
`require_output_path()` and `is_within()`.

There is **no** exported path for `paper_topics.jsonl`, `paper_topics.csv`,
`papers_by_topic/` or `topic_taxonomy.json`. Those artifacts exist on disk and
are referenced by `PAPER_LIBRARY_README.md`, but they were produced by the
FINAL CONSOLIDATION pass, not by Harness code. **Implication:** the Navigator
needs new path constants; adding them to `paths.py` is additive and touches no
existing behaviour.

## 2. Entry points

Two CLIs exist, both `argparse`, both returning `int` from `main()`:

| Entry | Module | Subcommands |
|---|---|---|
| `hunnu-harness` | `hunnu_harness.cli` | `env`, `agent-route`, `browser-start`, `browser-configure-pdf-download`, `library-stage`, `library-import` |
| `hunnu-harness-agent` | `hunnu_harness.agent_entrypoint` | routing/dry-run only |
| `python -m hunnu_harness.literature` | `hunnu_harness.literature.cli` | `plan`, `live-*` acquisition, `finalize-*-capture` |

House style, confirmed by reading `cli.py`:

- `build_parser()` builds the whole tree; `main()` dispatches on `args.command`.
- Heavy imports are done **inside** the dispatch branch, not at module top
  (e.g. `library-stage` imports `ExternalPaperImporter` lazily). Startup stays cheap.
- Machine output is `json.dumps(..., ensure_ascii=False, indent=2, sort_keys=True)`.
- Human output is flat `Key=Value` lines.
- Non-zero exit codes carry meaning (2 = unavailable, 3 = locked, 4/5 = failed run).

**Decision input:** the Navigator attaches to `hunnu_harness.cli` as new
subcommands. No parallel entry point is created.

## 3. Catalog schema (measured)

`library/catalog/papers.jsonl` — 179 lines, one JSON object per **logical work**.
Written atomically, sorted by `paper_id`, `ensure_ascii=False, sort_keys=True`.

Top-level keys present on all 179 records:

```
schema_version  paper_id  doi  title  authors[]  first_author  year  journal
managed_pdf_path  managed_fulltext_path  sha256  source_type  source_locator
acquired_at  imported_at  original_paths[]  version_role  notes_path  status
same_work_different_version  other_version_sha256s[]  versions[]
```

`metadata_corrections[]` appears on 36 of 179 records (append-only, added by
`BibliographicMetadataCorrector`).

Each element of `versions[]` (192 across the corpus):

```
sha256  managed_path  full_text_format  source_type  source_locator
acquired_at  imported_at  original_paths[]  version_role  status  provenance[]
```

`managed_path` is **Output-Root-relative** (`library/papers/<file>`); paths in
`paper_topics.jsonl` and `topic_links.csv` are **absolute Windows paths**. The
Navigator must normalise both.

Measured field coverage:

| Field | Known | Unknown |
|---|---|---|
| `year` | 179 | 0 |
| `journal` | 179 | 0 |
| `doi` | **102** | **77** (`"unknown"`) |
| `keywords` (topics file) | 158 | 21 empty |
| abstract | **0 — the field does not exist** | 179 |

**Two consequences that shape the whole design:**

1. There is **no abstract field anywhere in the corpus.** Any recall that
   depends on abstract text has to come from extracted full text, not metadata.
2. DOI covers only 57% of works, so DOI can be an exact-lookup key but never
   the primary recall signal.

`schema_version`: 172 records at `0.2.11`, 7 at `0.2.10`. `status`: all `MANAGED`.
`source_type`: 144 `EXTERNAL_IMPORT`, 35 `HARNESS_DOWNLOAD`.

## 4. WORK / VERSION relationship (measured)

```
179 catalog records (logical WORKS)
192 version entries  (physical files)
 13 nested variants  (192 − 179)
```

Versions-per-work histogram: `{1: 170, 2: 6, 3: 2, 4: 1}` → 170 + 12 + 6 + 4 = 192. ✔

Nine works carry `same_work_different_version = true`. Format split across the
192 physical versions: **191 PDF, 1 CAJ**.

The nine multi-version works:

| paper_id | n | formats | note |
|---|---|---|---|
| `P21FAD7D7F87D` | 4 | PDF×4 | largest variant cluster |
| `P23C61576ADCE` | 2 | PDF + **CAJ** | the only CAJ in the corpus |
| `P2ACC050B016C` | 2 | PDF×2 | |
| `P355BFD541C76` | 3 | PDF×3 | |
| `P412F8194672A` | 2 | PDF×2 | both `version_role = unknown` |
| `P44EEB5F4A61A` | 2 | PDF×2 | |
| `P9A9BA8556E49` | 3 | PDF×3 | |
| `PD44821E9D127` | 2 | PDF×2 | |
| `PE3158361F452` | 2 | PDF×2 | both `version_role = unknown` |

### 4.1 CRITICAL FINDING — physical filename is not derivable from `paper_id`

The documented convention is `papers/<PaperID>.<ext>` for the first version and
`papers/<PaperID>__<sha256>.<ext>` for later ones. **Three of the 192 files
violate it**, because FINAL CONSOLIDATION adopted pre-existing orphan files
under their original names:

```
P23C61576ADCE  ->  library/papers/P6B4B9A7DE3F3.pdf
P23C61576ADCE  ->  library/papers/P6B4B9A7DE3F3.caj
PB3731259E47C  ->  library/papers/P6C502A1FD06E.pdf
```

Note `P23C61576ADCE.managed_fulltext_path` = `library/papers/P6B4B9A7DE3F3.pdf`
— a *different* `PXXXXXXXXXXXX` token than the record's own `paper_id`.

**Rule for the Navigator: never construct a path from a `paper_id`, and never
parse a `paper_id` out of a filename. Resolve paths only by reading
`versions[].managed_path`.** A naive implementation would silently mis-resolve
these three files and would report the CAJ work as unreadable.

### 4.2 `version_role` is not usable as-is for preference ordering

Distribution over the 179 top-level records: `unknown` 95, `EXTERNAL_IMPORT` 78,
`CANONICAL_VERSION` 3, `PeerReviewedJournalArticle` 2, `Other` 1. At version
level the only semantically useful values are `CANONICAL_VERSION` and
`FORMAT_VARIANT` (on `P23C61576ADCE`); everything else is provenance, not role.

**Implication:** there is no existing preferred-version mechanism to reuse. The
catalog *does* however already designate a canonical file per work — the
top-level `sha256` / `managed_fulltext_path` pair. That is the authoritative
anchor a derived resolver must respect; the resolver only has to order the
*remaining* versions and handle format readability.

## 5. Topic metadata (measured)

`library/catalog/paper_topics.jsonl` — 179 lines, keyed by `paper_id`:

```
paper_id  title  human_readable_name  canonical_path  canonical_sha256
version_paths  topics  primary_domain  secondary_domains  keywords  metadata_notes
```

Encoding of `topics` — this is easy to get wrong:

```
topics := <assignment>(";"<assignment>)*
assignment := <domain> "\" <subtopic>          # a literal backslash, JSON-escaped
```

Example: `01_人工智能与数字经济\数字化转型;03_公司金融与企业投资\企业投资与现金持有`

Splitting on `\` instead of `;` yields 516 fragments and 146 bogus "topics".
Splitting correctly on `;` yields:

```
337 work-topic assignments        (matches the sealed audit)
 39 unique domain\subtopic pairs  (matches the sealed audit)
 15 domains, 39 subtopics
 73 single-topic works, 106 multi-topic works, 0 zero-topic works
```

Topics-per-work histogram: `{1: 73, 2: 68, 3: 27, 4: 9, 5: 1, 6: 1}`.

`topic_taxonomy.json` at the Output Root holds the domain → subtopic tree
(15 domains). Densest subtopics: 综合经济与管理 21, 全球价值链与国际化 20,
企业创新 18, 高管激励与管理者行为 18, 数字化转型 16, **AI漂洗 10**.

`keywords` is a `;`-separated free-text field, non-empty on 158/179 works.

## 6. Human-readable topic view

`papers_by_topic/` — 531 files total:

```
524 .pdf hardlinks
  1 .caj hardlink                 -> 525 NTFS_HARDLINK
  4 .url                          ->   4 URL_FALLBACK
                                     ---
                                     529 links   (matches the sealed audit)
  1 topic_links.csv
  1 .final_consolidation_view marker
```

`topic_links.csv` (529 rows) maps
`paper_id, domain, topic, human_readable_name, view_path, canonical_path,
link_type, canonical_sha256, version_role`. It also carries a
`PRIMARY_CANONICAL` marker in `version_role` that the JSONL catalog does not
have, plus a `00_版本变体/{PDF,CAJ}` pseudo-domain listing every physical file.

This is a **browsing view**, not a source of truth. The Navigator reads it only
to cross-check; it never writes there.

## 7. Existing capabilities the Navigator can reuse

| Need | Existing implementation | Reuse verdict |
|---|---|---|
| DOI normalisation | `literature/normalization.py :: normalize_doi` | **reuse as-is** |
| Title normalisation (NFKC, casefold, punctuation→space) | `normalize_title` | **reuse as-is** |
| Person-name normalisation | `normalize_person` | **reuse as-is** |
| Work identity | `stable_paper_id` | **reuse, read-only** |
| File identity | `sha256_file` | **reuse as-is** |
| Path guards | `paths.require_output_path`, `is_within` | **reuse as-is** |
| PDF validity / page count | `literature/pdf.py :: PDFValidator` | **reuse as-is** |
| PDF text extraction | `fulltext.py :: AuthorizedFullTextValidator.extract_pdf_first_page_text` / `extract_pdf_bounded_pages_text` | reuse the pattern; both are **bounded to the first N pages** and unsuitable for whole-document indexing |
| Catalog reader | `GlobalPaperLibrary._load_catalog` | **do not reuse** — private, and it is the writer's loader (raises `LibraryCatalogError` on any anomaly). The Navigator needs a *tolerant* reader that degrades instead of failing the process |

`normalize_title` maps CJK punctuation and Latin punctuation alike to spaces
and keeps all Unicode letters/numbers, so it works for both languages. It does
**not** segment CJK — the whole title collapses to one space-free token. Any
tokenizer built on it must add CJK n-gramming.

### Text extraction — what exists and what does not

- No full-text index exists anywhere in the Harness.
- `Output Root/metadata_cleanup/pdf_fulltext/` holds **27** `.txt` files from a
  one-off 2026-08-17 cleanup script — 15% coverage, produced before FINAL
  CONSOLIDATION, not maintained, not Harness code. **Not usable as an index.**
- No embeddings, no vector store, no cached chunks, no OCR output.
- `library/notes/` has 177 directories and **0 files**. The notes contract is
  declared but unused, so it is free space the Navigator must still not squat in.

### Available libraries (measured in `.venv`, Python 3.12.13)

`pypdf 6.16.1`, `pymupdf 1.28.2`, `pdfplumber 0.11.10`, `pdfminer.six`,
`numpy 2.5.2`, `rapidocr-onnxruntime 1.4.4`, `onnxruntime`, `PyYAML`, `pytest 8.4.2`.

Declared runtime dependency in `pyproject.toml` is only `pypdf>=5,<7`.
There is **no** embedding client, no `sentence-transformers`, no `faiss`, no
network-free local model. `sqlite3` is available from the stdlib.

**Decision input:** a lexical engine can be built on the stdlib alone and stay
inside the declared dependency set. An embedding path would require a new
dependency *and* a network service, and would break the offline guarantee.
See DESIGN §"Why no vector database".

## 8. Corpus profile (drives ranking design)

- 162/179 works have CJK titles; 17 are Latin-only. **Retrieval must be bilingual.**
- 80 distinct journals; top: 中国工业经济 18, 金融研究 14, 管理世界 11, 世界经济 10, 经济研究 10.
- Years span 2005–2026, concentrated 2020–2026 (115 works).
- 13 works are on-topic for AI washing (`AI漂洗` / `AI washing` / `漂智` / greenwashing),
  in both languages — a realistic bilingual acceptance target.

The cross-language problem is concrete: `AI washing`, `人工智能漂洗`, `AI漂洗`,
`漂智`, and `talk-walk gap` all denote the same construct and all occur in the
corpus. Pure string matching in one language recalls at most half of them.

## 9. Test architecture

- `pytest`, `pythonpath = ["src"]`, `testpaths = ["tests"]`, cache under the Output Root.
- Style: `unittest.TestCase` classes, `tempfile.TemporaryDirectory`, `unittest.mock.patch`
  to redirect module-level path constants. Shared helpers in `tests/literature_test_support.py`
  (`write_minimal_pdf`, `write_minimal_pdf_with_text`).
- **Baseline measured at discovery: 550 tests, all passing, 21.5 s.**

Existing tests patch `LIBRARY_*` constants to temp dirs, so they never touch the
real corpus. Navigator acceptance tests that *must* read the real 179 works have
to be explicitly guarded so the suite still passes on a machine without the
Output Root.

## 10. Pre-change fingerprint (regression baseline)

Captured by an **independent** script that does not import `hunnu_harness`, so
it cannot be fooled by a bug in the module under test.

```
catalog_records      : 179
physical_versions    : 192
nested_variants      : 13
papers_file_count    : 192
topic_assignments    : 337
unique_topic_values  : 39
papers_by_topic      : 531
fingerprint_sha256   : ddcfd8f1e325d5e3f876817fc0b7ead124fcfcd372421ad171b9869a28908366
```

The fingerprint covers byte-level SHA-256 of every catalog file, SHA-256 + size
of all 192 physical papers, the full `paper_id` list, per-work canonical and
per-version SHA maps, the managed-path map, per-work topic assignments, and the
complete `papers_by_topic` file listing. Any rename, re-hash, re-import, or
relink changes it.

## 11. Findings that constrain the design

1. **Never derive a path from a `paper_id`** — 3 of 192 files break the naming
   convention (§4.1).
2. **Split `topics` on `;`, then on `\`** — the other order silently invents
   107 phantom topics (§5).
3. **There is no abstract anywhere** — semantic recall must come from extracted
   full text or from topics/keywords, not from a metadata abstract (§3).
4. **DOI covers 57%** — exact key, not a recall backbone (§3).
5. **No preferred-version mechanism exists**, but the catalog's top-level
   `sha256` already pins a canonical file per work; a derived resolver must
   defer to it rather than invent its own idea of canonical (§4.2).
6. **`normalize_title` does not segment CJK** — a CJK-aware tokenizer is required (§7).
7. **`_load_catalog` fails closed by design** — correct for a writer, wrong for
   a navigator, which must degrade to a partial result and say so (§7).
8. **No embedding infrastructure and no network guarantee** — the default engine
   must be lexical and fully offline (§7).
9. **`library/notes/` is declared but empty** — do not colonise it for index data.
10. **Encoding discipline**: every catalog read must specify `utf-8`/`utf-8-sig`
    explicitly. On this machine the default console/stdin codec mangles CJK, and
    an implicit-encoding read of `papers.jsonl` fails with a JSON escape error
    that looks like corruption but is not.

## 12. Files this work must NOT modify

Confirmed unchanged-by-design for the whole Navigator effort:

```
src/hunnu_harness/literature/library.py          (catalog writer)
src/hunnu_harness/literature/normalization.py    (identity)
src/hunnu_harness/literature/fulltext.py         (identity gate)
src/hunnu_harness/literature/models.py
src/hunnu_harness/literature/downloads.py
src/hunnu_harness/literature/workflow.py
src/hunnu_harness/literature/adapters/*
src/hunnu_harness/agent_entrypoint.py
Output Root/library/catalog/*                    (all four catalog files)
Output Root/library/papers/*                     (all 192 physical files)
Output Root/papers_by_topic/**                   (all 531 entries)
Output Root/topic_taxonomy.json
Output Root/PAPER_LIBRARY_README.md
```

`paths.py` and `cli.py` receive **additive** changes only (new constants, new
subcommands). Everything else the Navigator needs lives in a new package.
