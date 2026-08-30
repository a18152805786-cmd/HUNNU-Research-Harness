# Post-acquisition topic classification

After an authorized download passes its identity lock and lands in the Global
Paper Library, the Harness files the WORK against the frozen topic taxonomy and
updates the topic view. An Agent does not move papers into folders by hand
(AGENTS.md 69).

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
