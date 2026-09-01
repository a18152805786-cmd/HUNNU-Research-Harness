# Paper Research Navigator — Agent Contract

**If you are an Agent and you need a paper, start here. Do not search the disk.**

The Global Paper Library holds 179 scholarly WORKS as 192 physical files, all
catalogued. Every one of them is reachable through the commands below. There is
no paper in this environment that requires a filesystem hunt to find, and a
`glob` over `Desktop`, `Downloads`, or a personal cloud/thesis archive/
Obsidian folder is both slower and less correct than one Navigator call.

---

## The rule

```text
When you need existing literature:

  1. Query the Navigator first.
  2. Resolve WORK identity (paper_id).
  3. Resolve the preferred version and read that local file.
  4. Only when the answer is NOT_IN_LIBRARY, invoke the acquisition pipeline.
```

Never:

```text
glob the whole disk            search Desktop / Downloads / D:\
guess a path from a paper_id   open a PDF you found by filename
download because a search      re-derive the catalog
  returned nothing
```

## Commands

All commands print sorted UTF-8 JSON on stdout. Run them with the project venv:

```bash
.venv/Scripts/python.exe -m hunnu_harness.cli paper-search --query "AI washing 如何通过审计风险影响盈余质量？" --top 15
```

| Command | Answers |
|---|---|
| `paper-search --query "<q>" [--top N] [--no-fulltext] [--no-expand]` | Which WORKS bear on this question, with roles and evidence |
| `paper-lookup --query "<identity>"` | Is *this specific paper* held? (DOI, PaperID, exact title, author names, `journal:`) |
| `paper-fulltext --paper-id PXXXXXXXXXXXX` | Every version of one WORK and which file to read |
| `paper-related --paper-id PXXXXXXXXXXXX [--top N]` | What else in the library bears on this paper |
| `paper-pack --query "<q>" [--top 15]` | A reading pack of references (copies no PDF) |
| `paper-gaps --query "<q>"` | Local corpus coverage — **not** a novelty claim |
| `paper-verify-citation --citation "<citation>"` | Is this cited work held? If not, an acquisition handoff |
| `paper-index {status,build,validate,rebuild,drop}` | Manage the derived index |
| `paper-fingerprint [--output P] [--compare P] [--summary]` | Prove the library is unchanged |

Exit codes: `0` success · `2` library unavailable · `3` not found / `NOT_IN_LIBRARY` ·
`4` index operation failed · `5` citation matched several works (`AMBIGUOUS`).

Structured output is always UTF-8, whatever the console codepage is. Capture
stdout as bytes and decode it as UTF-8; do not rely on the ambient encoding.

In-process equivalent:

```python
from hunnu_harness.navigator import PaperNavigator
navigator = PaperNavigator()
navigator.search("AI washing audit risk", top=15)
navigator.lookup("DOI:10.13644/j.cnki.cn31-1112.2025.09.012")
navigator.fulltext("PEC61CA32AC26")
```

## Reading the response

Every response carries the same header:

```json
{
  "schema_version": "navigator-0.1",
  "index_status": "FRESH | STALE | ABSENT | UNREADABLE",
  "works_indexed": 179,
  "physical_versions": 192,
  "topic_assignments": 337,
  "catalog_status": "COMPLETE | PARTIAL",
  "degraded": []
}
```

- `index_status` other than `FRESH` means the full-text stage was skipped or
  partial. Metadata recall is unaffected. Run `paper-index rebuild` to refresh.
- `catalog_status: PARTIAL` with a non-empty `degraded` list means some catalog
  records could not be read. **Say so in your answer**; do not present a partial
  library as complete.

Each search result carries:

| Field | Use |
|---|---|
| `paper_id` | The WORK identity. This, not a filename, is what you cite internally. |
| `relevance_role` | `CORE` / `MECHANISM` / `OUTCOME` / `METHOD` / `BACKGROUND` |
| `secondary_roles` | Other roles the same paper also serves |
| `relevance_reason` | Why it was recalled, in words |
| `matched_by` | Per-term provenance: field, term, weight, contribution |
| `concept_evidence` | Which research concepts the work carries, and where |
| `preferred_version.absolute_path` | **The file to open.** Never construct this yourself. |
| `fulltext_status` | `AVAILABLE` / `AVAILABLE_NOT_MACHINE_READABLE` / `FILE_MISSING` / `UNREADABLE` |
| `available_versions` | Every physical version of the WORK |
| `matched_passages` | Page-anchored quotes with `chunk_id`, `page`, `version_sha256` |

## Three things that will bite you if you improvise

1. **Never build a path from a `paper_id`.** Three of the 192 managed files
   carry a historical name that does not match the `paper_id` of the work that
   owns them — `P23C61576ADCE`'s full text is `library/papers/P6B4B9A7DE3F3.pdf`.
   Always read `preferred_version.absolute_path` from the response.

2. **One WORK can have several files.** Nine works do. They are versions of the
   same paper, not different papers. Search results are WORK-level and never
   repeat; `paper-fulltext` shows you all versions of one.

3. **`AVAILABLE_NOT_MACHINE_READABLE` is not missing.** The corpus holds one CAJ.
   The file is lawfully held and present; this environment simply has no CAJ text
   extractor. Do not "fix" this by re-downloading.

## Citation verification

```bash
.venv/Scripts/python.exe -m hunnu_harness.cli paper-verify-citation --citation "王海森、李纲(2026). 人工智能漂洗抹杀了企业技术创新吗. 中国工业经济."
```

Shorthand works too — a surname and a year are enough when they name exactly one
work in the library:

```bash
.venv/Scripts/python.exe -m hunnu_harness.cli paper-verify-citation --citation "Biddle et al. 2009"
```

There are three outcomes, and they need different next actions:

| `status` | Meaning | Next action |
|---|---|---|
| `IN_LIBRARY` | Exactly one work matches. `paper_id` and the file to read are returned. | Read the local full text |
| `AMBIGUOUS` | Several works fit the citation equally well. `paper_id` is `null`. | Add a title fragment or a DOI. **Never pick one from `candidates` yourself.** |
| `NOT_IN_LIBRARY` | No work clears the evidence bar. | Use the returned `handoff` with the acquisition pipeline |

A citation must carry at least two agreeing identity signals (author, year,
title) to resolve at all. `"Biddle"` alone returns `NOT_IN_LIBRARY`: one surname
names no particular paper.

`IN_LIBRARY` returns the `paper_id` and the file to read, or:

```json
{
  "status": "NOT_IN_LIBRARY",
  "match": null,
  "suggested_next_action": "ACQUISITION",
  "handoff": {
    "identity": {"title": "...", "authors": [...], "year": "...", "doi": "..."},
    "acquisition_request_hint": {"TaskType": "literature_search", "...": "..."},
    "entry_point": "hunnu-harness agent-route --request-json <request.json>"
  },
  "unverified_candidates": [ {"...": "...", "verified": false} ]
}
```

`unverified_candidates` are near misses shown for human judgement. **They are not
the cited paper.** A candidate with `verified: false` must never be reported as
held, and must never be cited. The same rule applies to the `candidates` list on
an `AMBIGUOUS` result: several works fitting a citation equally well is not
permission to choose one.

The Navigator does not download. On `NOT_IN_LIBRARY`, hand off to the existing
acquisition chain (AGENTS.md §38–44), which keeps authorization, Target Identity
Lock, validation, SHA-256, and manifest guarantees intact.

## Gap analysis is about this library only

`paper-gaps` reports what the **local corpus** covers. Its output carries an
explicit scope statement and an interpretation guard. Absence in a 179-work
personal library is not evidence about the state of the research literature, and
must never be reported as novelty. To say anything about the literature, run the
acquisition/search pipeline against external databases first.

## What the Navigator will not do

- It will not download, and has no network path.
- It will not modify the catalog, `paper_id`s, SHA-256s, canonical paths,
  filenames, version structure, `papers_by_topic`, or hardlinks.
- It will not return a paper the library does not hold. An exact-identity query
  that misses returns `NOT_IN_LIBRARY` with no substitute.

## Derived data

Everything the Navigator writes lives under `<Output Root>/paper_retrieval/`:

```text
paper_retrieval/
  index/                 derived retrieval index (rebuildable)
  reading_packs/<id>/    manifest.json, README.md, ranking.json, evidence_plan.json
```

All of it is disposable. `paper-index rebuild` reconstructs the index from the
catalog and the managed full texts; deleting `paper_retrieval/` entirely loses
nothing but time. **The index is never a source of truth** — `papers.jsonl`,
`library/papers`, and the topic metadata remain authoritative.

The Navigator works with no index at all: metadata and topic recall are computed
live from the catalog in about 160 ms. Building the index only adds full-text
passage search.

## Verifying the library is untouched

Capture a baseline before doing anything risky:

```bash
.venv/Scripts/python.exe -m hunnu_harness.cli paper-fingerprint --output baseline.json --summary
```

Compare against it afterwards:

```bash
.venv/Scripts/python.exe -m hunnu_harness.cli paper-fingerprint --compare baseline.json
```

`--compare` exits `0` only when the library is byte-identical to the baseline,
and prints a field-by-field diff otherwise. The baseline is a snapshot of a
living corpus, so keep it outside the Core Root (it goes stale the moment a
paper is legitimately imported) and regenerate it after each intentional change.
The fingerprint covers every catalog file, all 192 managed files, the per-work
canonical and version SHA maps, the managed-path map, the topic assignments, and
the complete `papers_by_topic` listing.

## Turning the Navigator off

1. Delete `src/hunnu_harness/navigator/` and revert the two additive hunks in
   `paths.py` and `cli.py`, **or** `git revert` the Navigator commits.
2. `rm -r "<Output Root>/paper_retrieval"`.

The 179 works and 192 versions are not in the rollback path.
