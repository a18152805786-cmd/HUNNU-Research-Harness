# Post-acquisition topic classification

After an authorized download passes its identity lock and lands in the Global
Paper Library, the Harness files the WORK against the frozen topic taxonomy and
updates the topic view. An Agent does not move papers into folders by hand
(AGENTS.md 69).

## Installation readiness and the frozen configuration

`topic_taxonomy.json` is the operator's frozen configuration in the Output
Root. The distribution does not contain a user's historical personal registry;
`taxonomy_aliases.default.json` contains matching aliases and is not a
replacement taxonomy. A fresh installation must receive an explicitly
reviewed copy of the registry before automatic topic filing is available.
Neither the lexicon nor downloaded papers may be used to invent a replacement.

An optional, newly designed starter is supplied at
`config/topic_taxonomy.default.json`: **general-empirical-economics-v1**, dated
**2026-11-08**, with 8 domains and 39 subtopics. It is not a recovery of the
historical 42-topic personal registry. Its domains organize digital technology
and innovation; corporate finance and governance; accounting, auditing and
capital markets; the financial system and allocation; industry, supply chains
and regions; sustainability; labor, population and macroeconomics; and research
methods and general management. Every shipped alias key and lexicon topic has
one place in the template, so the template does not introduce unmatched topic
names. Domains are organizational groups, not mutually exclusive claims about
a paper's subject. The general category is never an automatic no-match bucket.

Use this template only as an explicitly chosen new initial registry, after
reviewing its labels and SHA-256. Initialization remains a separate command:

```powershell
$starterSha256 = (Get-FileHash .\config\topic_taxonomy.default.json -Algorithm SHA256).Hash
.venv\Scripts\python.exe -m hunnu_harness.cli library-init-taxonomy --source .\config\topic_taxonomy.default.json --sha256 $starterSha256 --json
```

Neither startup nor `doctor` copies this template into Output Root, and the
initialization command still refuses any existing frozen registry. The old
classifier calibration describes its original corpus, not a newly established
accuracy estimate for this starter. Metadata-only or ambiguous papers may
properly remain `REVIEW_REQUIRED`.

`doctor --json` now checks this configuration independently of acquisition:
`ClassificationReady=false` and `Verdict=READY_WITH_WARNINGS` identify a missing
or invalid registry while `AcquisitionReady=true` and exit 0 preserve the safe
ability to download and archive authorized full text. Environment or dependency
errors retain their existing nonzero exit codes. This readiness check does not
read the network or modify the registry.

The public initialization API is
`hunnu_harness.literature.topics.initialize_topic_taxonomy(source,
expected_sha256=...)`. Supply the path to the reviewed existing JSON file and
its complete SHA-256. It validates the closed schema and exact bytes, then
creates only the missing Output Root `topic_taxonomy.json` atomically. An
existing destination is always refused, even when identical; it never changes
the source file, library papers, topic assignments, or the hardlink view.
Domain and subtopic names must already be canonical single Windows directory
components: surrounding whitespace, storage separators, device names, and
invalid path characters are refused rather than normalized. Accepted labels
therefore round-trip unchanged through the stored topic field. A temporary
hardlink cleanup failure after an atomic commit is reported in `Warnings`;
the initialized configuration remains successful and must not be retried as
if nothing had been written.
The equivalent CLI is:

```powershell
.venv\Scripts\python.exe -m hunnu_harness.cli library-init-taxonomy --source C:\reviewed\topic_taxonomy.json --sha256 <reviewed-sha256> --json
```

The file must already be reviewed. This command does not construct labels from
aliases, the lexicon, or papers. `doctor --json` reports `ClassificationReady`
separately so a successful environment check cannot imply that filing is ready.
Restoring this configuration does not automatically reclassify already
archived WORKS. Those must subsequently use the existing controlled
classification/confirmation path; do not edit topic metadata by hand.

```
authorized download → Target Identity Lock → library ingest
                                                  ↓
                                        WORK-level classification
                                                  ↓
                                     paper_topics.jsonl (canonical)
                                                  ↓
                                     papers_by_topic (hardlink view)
                                                  ↓
                                        Navigator retrieval
```

## What existed before this, and what did not

The taxonomy, the 337 assignments, the hardlink view, and `topic_links.csv`
were all present in the Output Root, but **nothing in `src/` wrote any of
them** — `navigator/catalog.py` read `paper_topics.jsonl`, `navigator/
fingerprint.py` read the view directory, and `TOPIC_TAXONOMY_JSON` was defined
in `paths.py` and never opened. The canonical writer, the taxonomy loader, and
the view generator are new here; the classifier sits on top of them.

## The layers

| Module | Owns |
|---|---|
| `literature/topics.py` | `TopicTaxonomy` (frozen registry), `TopicStore` (atomic JSONL + CSV), `TopicViewBuilder` (hardlink view) |
| `literature/classification.py` | `WorkClassifier` — evidence scoring, thresholds, no I/O |
| `literature/auto_classification.py` | `PostAcquisitionClassifier` — classify / apply / post-ingest decision |

The bilingual tokenizer and the concept vocabulary are the Navigator's
(`navigator.tokenize`, `navigator.lexicon`). There is no second tokenizer and no
second vocabulary.

## Evidence

Two independent signals, weighted by where they appear (title 3.0, keywords 2.0,
abstract 1.5, full text 1.0 capped at three hits per concept, journal 0.5):

1. **Concept lexicon** — `match_concepts()` maps a surface form to a concept,
   and a concept names the taxonomy subtopics it evidences.
2. **Taxonomy names** — a compound subtopic (`融资约束与资本配置`) is split on
   `与和、及/` and its parts are matched directly. This was added after
   calibration showed the lexicon alone sending `全球价值链…` to
   `供应链与产业链` when the truth was `全球价值链与国际化` — the subtopic whose
   own name the title contained. It lifted top-1 precision from 0.636 to 0.974.

   The name signal also fires on **English aliases** of the frozen values
   (`literature/taxonomy_aliases.py`) — the frozen names are Chinese, so an
   English title could never corroborate anything and 16 of 19 English works
   sat in review on a lone lexicon signal. An alias is the standard English
   term for the same construct, never a term fitted to one paper; it matches
   on word boundaries, credits at most one hit per subtopic per field, and one
   English word weighs as a two-character Chinese term in the specificity
   formula.

   **Measurement is an in-corpus replay, and its two language halves are not
   equally self-confirming.** Replaying all 181 works against their existing
   topics: Chinese coverage 106/162 (0.654, bit-identical per work with the
   table on and off) at replay precision 1.000, English coverage 3/19 → 17/19
   (0.895) at replay precision 1.000. The Chinese number largely re-derives
   assignments this same classifier produced (up to 106 of 162 agree
   automatically — a highly self-confirming ground truth); the English ground
   truth predates the alias table almost entirely (only 3 of 19 were ever
   auto-assignable before it), so its replay agreement is closer to an
   independent check — but 19 works is a small sample, and neither number is
   a precision claim about *future* English literature. Broad-word collision
   behaviour is pinned separately: one broad word (`disclosure`,
   `innovation`, `esg`, …) tops out below the 0.75 gate by construction, and
   `tests/test_taxonomy_aliases.py` holds a named canary
   ("AI washing: Strategic disclosure and backlash" accepts AI漂洗, holds
   信息披露 back) plus negative titles for ten broad terms.

   The taxonomy file itself is untouched — an alias can never create a topic
   — and the alias table is a calibration artifact: an edit is acceptable
   only with the per-language replay
   (`CanonicalLibraryBacktestTests::test_english_aliases_hold_their_own_calibration`)
   and the canary/collision suite held or improved.

Confidence saturates as `score / (score + 3.0)`. One title concept hit alone
reaches 0.5 and is therefore *not* auto-assigned: a lone uncorroborated signal
goes to review by construction.

## Calibrated thresholds

Measured on all 179 frozen works, title and keywords only:

| | |
|---|---|
| Primary auto-assign threshold | **0.75** confidence |
| Secondary rule | margin ratio **>= 0.70** of the leader **and** taxonomy-name corroboration |
| Max topics per work | **3** |
| Primary precision | **1.000** (0 FP) |
| Secondary precision | **1.000** (0 FP over 32 assignments) |
| Overall precision | **1.000** |
| Overall recall | 0.600 |
| Auto coverage | 60.9% |
| Review-required rate | 39.1% |
| Review-suggestion rate | 2.2% |

The secondary rule is a *ratio*, not an absolute floor. An absolute floor admits
any topic clearing a fixed bar however far behind the leader it sits: at a 0.55
floor precision was 0.519, and the same corpus at a 0.70 ratio gives 0.966.

The ratio alone still left five errors, and **raising it does not remove them**.
All five were topics the concept lexicon proposed with no support from the
taxonomy value's own name: one concept naming several sibling subtopics fires
them all at an identical score, so they *tie* with the leader at ratio 1.00 and
no margin can separate them. Measured over the same corpus:

| Secondary rule | assigned | FP | precision |
|---|---|---|---|
| ratio >= 0.70 | 39 | 5 | 0.872 |
| ratio >= 0.80 | 31 | 5 | 0.839 |
| ratio >= 0.90 | 25 | 5 | 0.800 |
| ratio >= 1.00 | 23 | 5 | 0.783 |
| **ratio >= 0.70 + taxonomy-name** | **32** | **0** | **1.000** |
| ratio >= 0.70 + both families | 31 | 0 | 1.000 |
| ratio >= 0.80 + taxonomy-name | 24 | 0 | 1.000 |

Tightening the ratio to 1.00 keeps every error while discarding 16 correct
assignments — precision gets *worse*. Requiring the name signal removes all five
and keeps 32 of 34. Primaries are not subject to this gate: top-1 already
measures 1.000 precision on its own, and adding the requirement would cost
coverage for nothing.

Three topics is the ceiling because 96% of the frozen corpus carries three or
fewer (73 works have one, 68 two, 27 three, 9 four, 2 more).

Recall is deliberately the weaker number. A missing topic is visible in the
review queue; a wrong one is silent.

## Guarantees

- **WORK-level.** One work classifies once. A second version — PDF, CAJ,
  network-first, final — reuses the work's topics (`SKIPPED_EXISTING`).
- **Closed taxonomy.** The classifier can only emit existing values, and
  `TopicStore.commit` re-checks every label before writing. A paper that fits
  nothing returns `REVIEW_REQUIRED`; it never grows the taxonomy.
- **No PDF copies.** View entries are NTFS hardlinks to the one managed file;
  `pdf_copies_created` is always 0. A hardlink that the filesystem refuses is
  recorded as unavailable, never replaced by a copy.
- **Idempotent.** Re-applying writes nothing and creates no duplicate link.
- **Fail-closed, archive-safe.** A classification failure is a metadata
  outcome. The download, the identity lock, and the archived file are already
  valid evidence and stay untouched.
- **Proposed topics are never canonical.** A secondary with score support but
  no name corroboration is reported in `proposed_topics` and written nowhere.

## Statuses

| Status | Meaning |
|---|---|
| `CLASSIFIED` | A primary topic was assigned; nothing is outstanding. |
| `CLASSIFIED_WITH_REVIEW_SUGGESTIONS` | A primary topic was assigned **and** one or more secondaries are proposed for a human. The paper *is* classified; this is not a failure. |
| `REVIEW_REQUIRED` | No primary cleared the gate. Nothing was written. |
| `SKIPPED_EXISTING` | The work already carries topics; they are reused unchanged. |
| `FAILED_SAFE` | Classification could not run. The archived paper is untouched. |

## Navigator readiness

Three capabilities, reported separately, because they become ready at different
moments:

| Field | Source | When it becomes true |
|---|---|---|
| `NavigatorMetadataReady` | `papers.jsonl` via `CatalogReader` | as soon as ingest commits |
| `NavigatorTopicReady` | `paper_topics.jsonl` | as soon as classification commits |
| `NavigatorFulltextIndexStatus` | the Navigator's own `IndexStatus` | `FRESH` only after the index is rebuilt |

Topic and metadata changes are visible **immediately with no index rebuild** —
the Navigator reads `paper_topics.jsonl` directly (AGENTS.md 67), verified by
mutating an isolated copy and re-reading through `CatalogReader`.

The derived index carries full-text chunks only. A newly archived work is not in
it, so `NavigatorFulltextIndexStatus` is **`STALE`**, never `FRESH` and never a
made-up `READY` — the value is the Navigator's own status enum
(`FRESH`/`STALE`/`ABSENT`/`UNREADABLE`), not a parallel vocabulary. An existing
work already covered by a fresh index keeps `FRESH`: an acquisition event does
not by itself mark the index stale.

There is deliberately **no single `NavigatorReady` flag**. It would have to
claim "ready" while passage retrieval silently missed the paper. Rebuilding is a
separate explicit operation (`paper-index rebuild`) and is never triggered by
classification.

## Manifest fields

`DownloadManifestEntry` reports `ClassificationStatus`, `AssignedTopics`,
`TopicReviewRequired`, `TopicMetadataUpdated`, `TopicViewUpdated`, and
`ClassificationReason`. `CLASSIFIED` means filing is done. `REVIEW_REQUIRED`
means the paper is safely archived and its topics need a human — never a reason
to download it again.

## The import path files too

`library-import` runs the same `classify_after_ingest` step once the staged
PDF is a managed WORK (`NEW_PAPER`, `SAME_WORK_DIFFERENT_VERSION`, or
`EXACT_DUPLICATE`), and `LibraryIngestResult` reports it in the manifest's
field set above. A manually imported paper is an acquisition like any other
(AGENTS.md 69): it is filed, or it lands in `REVIEW_REQUIRED` with its
proposals for `library-confirm-topics`. An importer with no classifier
attached — only an isolated library ever has none — reports
`ClassificationStatus: NOT_ATTEMPTED` with `TopicReviewRequired: true`, so
"nothing was filed" is a statement on the result rather than an absence from it.
A classification failure is reported as `FAILED_SAFE`, `TopicReviewRequired:
true`, and never fails the import.

Before this, the import ran no classification and said nothing: a WORK came
out `MANAGED` with no topic, and because confirmation gated on the classifier's
*regenerated* verdict rather than on the WORK's state, a paper the classifier
was confident about was refused as `NOT_REVIEW_REQUIRED` — "classified", with
nothing filed and no sanctioned way to file it. Confirmation now gates on
whether the WORK carries a topic; what it can confirm without the override is
everything classification raised for the WORK — its proposals, and for an
unfiled WORK also what it would have assigned.

## Method-title context and controlled reclassification (2026-11-08)

The classifier version `work-classifier-2026-11-08-method-context-v1` distinguishes
an explicit methodological DML title from a paper applying machine learning.
Titles such as *Double/debiased machine learning for treatment and structural
parameters* support the existing `计量方法与研究设计` topic. In this narrow title
context the generic phrase `machine learning` names the estimator and does not
also earn an AI-adoption topic. A DML mention in the body, an application title
ending in “a double machine learning approach”, or “treatment effects of digital
transformation” does not receive this override. Other AI/robot terms retain
their usual evidence. Missing method topics yield review rather than inventing
a taxonomy value. This conservative title rule is not a claim that every DML
application has been semantically classified correctly.

A settled WORK is changed through `TopicReclassifier`, separate from the
unfiled-work confirmation operation:

```python
from hunnu_harness.literature.topic_reclassification import TopicReclassifier

api = TopicReclassifier()
preview = api.reclassify(paper_id, reason="Correct a methodological title misfiled as AI")
# Inspect preview.old_topics, preview.new_topics and preview.classification.
result = api.reclassify(
    paper_id,
    reason="Correct a methodological title misfiled as AI",
    expected_fingerprint=preview.current_fingerprint,
    dry_run=False,
)
```

The default preview reads only. Apply requires a nonempty reason and either
`expected_current_topics` (ordered, including the primary topic) or the preview's
fingerprint; when both are supplied both must match. The fingerprint includes
the complete topic row, exact classification input (built once per invocation),
full latest provenance, catalog identity, canonical file and frozen taxonomy
hashes. It also binds the classifier version, scoring options, effective lexicon
and aliases, so an input or rule change invalidates a previous preview. A
`HUMAN_CONFIRMED` assignment additionally needs
`authorize_human_confirmed_fingerprint` equal to this exact current fingerprint.
The formal provenance fields and source marker must agree (`AUTO_CLASSIFIED`
with `HARNESS`, `HUMAN_CONFIRMED` with `HUMAN`). Missing, malformed or
contradictory provenance cannot be guessed to be
automatic: apply refuses and leaves the existing assignment in place. An
unfiled WORK continues through acquisition classification or confirmation.
Reclassification accepts no arbitrary replacement label; it recomputes using
the classifier and checks the existing frozen taxonomy.

All paths must share one complete standard Output layout. The operation checks
the canonical PDF identity and refuses occupied topic-view names. The existing
canonical apply path updates JSONL, CSV and hardlink view; an append-only
`TOPIC_RECLASSIFIED` provenance entry retains old/new topics, previous source
and record, the reason, precondition fingerprint and classifier version. A
reentrant OS-backed topic writer lock serializes apply, confirmation and
provenance writes without entering the browser or spending download quota.
It lives outside `library/catalog`, so it is not a corpus fingerprint input.

An ordinary write failure restores the exact previous metadata bytes and old
hardlinks. If the filesystem also refuses rollback, `RECOVERY_REQUIRED` reports
a retained `Output/temp/topic-reclassification/<id>/PREIMAGE.json` package with
metadata preimages; it never reports a successful apply or silently discards
recovery evidence. The package contains no copied PDF. Process termination or
storage failure can require manual recovery from that package; this is an
exception rollback transaction, not a filesystem-wide atomic-commit primitive.
Source PDFs and frozen taxonomy are read-only. The derived retrieval index is
left intact and reports `STALE` after a topic change (or `ABSENT` if missing);
run the existing `paper-index rebuild` afterwards to refresh it. Unchanged
assignments are `ALREADY_CURRENT` and do not append duplicate provenance.

### Windows read-only aliases and retained recovery packages

Managed full texts are read-only. Their NTFS hardlinks share that attribute, so
ordinary `unlink` can refuse a topic-view change even though the PDF bytes are
valid. Topic-view removal and transaction rollback use the dedicated
`remove_readonly_hardlink_alias` boundary: it verifies the explicit alias and
managed path have the same file identity, then uses Windows
`FileDispositionInfoEx` with `IGNORE_READONLY_ATTRIBUTE` to remove that alias
name. It never clears the managed inode's read-only attribute, changes PDF
bytes, or falls back to deleting the managed original. Unsupported APIs or
unverifiable paths fail closed. Tests include real read-only Windows hardlinks,
not only normal writable temporary PDFs.

`TopicReclassifier.recover(...)` is the narrow public recovery operation for a
retained package when metadata rollback completed but view cleanup failed:

```python
preview = api.recover(
    recovery_path,
    reason="Restore the original view after failed reclassification",
    expected_preimage_sha256=reviewed_preimage_sha256,
)
# Inspect preview.remove_aliases and preview.restore_aliases.
result = api.recover(
    recovery_path,
    reason="Restore the original view after failed reclassification",
    expected_preimage_sha256=reviewed_preimage_sha256,
    expected_current_fingerprint=preview.current_fingerprint,
    dry_run=False,
)
```

All four current metadata files must already be byte-for-byte equal to the four
preimages. If metadata differs, recovery refuses with `METADATA_CHANGED`; it
does not overwrite a subsequent classification or human decision. The original
alias list must exactly match that WORK's restored topics, and all current
aliases must be hardlinks to its verified canonical PDF. Apply requires the
reviewed preimage hash plus a fresh fingerprint binding current metadata,
preimages, alias paths/file identities, frozen taxonomy and managed PDF. It
restores missing old aliases before removing redundant aliases. It keeps every
preimage and appends a new `RECOVERY_RESULT_*.json` receipt; it changes no
assignment provenance, metadata, PDF or taxonomy. Recovery from other partial
states requires a separate audit, rather than a generalized overwrite option.

### CLI sequence

Preview a filed WORK, inspect its proposed replacement, then apply the exact
preview fingerprint. `--apply` is the opt-in to writing; omitting it is read-only.
For a `HUMAN_CONFIRMED` WORK, an additional targeted authorization is required;
do not supply that flag unless that particular replacement was authorized.

```powershell
$reclassReason = 'Correct a methodological DML title misfiled as AI'
$paperId = 'P210C53C008A6'
$held = .venv\Scripts\python.exe -m hunnu_harness.cli paper-lookup --query $paperId --json | ConvertFrom-Json
$managedPath = $held.matches[0].preferred_version.absolute_path
$pdfBefore = (Get-FileHash -LiteralPath $managedPath -Algorithm SHA256).Hash
$preview = .venv\Scripts\python.exe -m hunnu_harness.cli library-reclassify --paper-id $paperId --reason $reclassReason --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $preview.ReclassificationStatus -ne 'DRY_RUN') { throw 'Preview refused' }
# Review OldTopics, NewTopics, PreviousAssignmentSource and Classification here.
$applied = .venv\Scripts\python.exe -m hunnu_harness.cli library-reclassify --paper-id $paperId --reason $reclassReason --expect-fingerprint $preview.CurrentFingerprint --apply --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $applied.ReclassificationStatus -ne 'RECLASSIFIED') { throw 'Apply did not complete; inspect its receipt' }
.venv\Scripts\python.exe -m hunnu_harness.cli paper-index rebuild --json
.venv\Scripts\python.exe -m hunnu_harness.cli paper-lookup --query $paperId --json
if ((Get-FileHash -LiteralPath $managedPath -Algorithm SHA256).Hash -ne $pdfBefore) { throw 'Canonical PDF changed' }
```

If an apply returned `RECOVERY_REQUIRED`, finish its local recovery first:

```powershell
$recoveryPreview = .venv\Scripts\python.exe -m hunnu_harness.cli library-reclassification-recover --recovery-path <RecoveryPath> --preimage-sha256 <reviewed-PREIMAGE-SHA256> --reason 'Restore original hardlink view' --json | ConvertFrom-Json
# Inspect RemoveAliases and RestoreAliases before applying this recovery.
.venv\Scripts\python.exe -m hunnu_harness.cli library-reclassification-recover --recovery-path <RecoveryPath> --preimage-sha256 <reviewed-PREIMAGE-SHA256> --reason 'Restore original hardlink view' --expect-fingerprint $recoveryPreview.CurrentFingerprint --apply --json
```

After a successful view-only recovery the metadata and its index fingerprint
remain the old ones. A later reclassification must generate a new preview; an
old apply receipt is never reused as a fresh authorization.
