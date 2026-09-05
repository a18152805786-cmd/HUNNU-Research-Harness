# Global Paper Library / Personal Literature Corpus (library protocol v0.2.12)

## Boundary

The Library is a long-lived corpus below the configured Harness Output Root. It is not a literature run and it is not the generic/data download archive.

```text
Output Root/
  library/
    papers/                 # managed full-text assets only
    notes/<PaperID>/        # derived reading assets only
    catalog/papers.jsonl    # source of truth
    catalog/papers.csv      # human-readable projection
    import_staging/
      candidates/           # explicit COPY submissions
      review/               # rejected/conflicting candidates
  runs/                     # acquisition evidence and provenance remain here
  downloads/authorized/     # CNRDS/data downloads remain here
```

Every path is derived from `hunnu_harness.paths.OUTPUT_ROOT`; no user-machine path is embedded in Library code. Managed paths recorded in the catalog are Output-Root-relative so the Output Root can be relocated as one unit.

## Identity contract

- Logical work identity: the existing `stable_paper_id()` and `PXXXXXXXXXXXX` format.
- Concrete file identity: SHA-256.
- DOI remains the preferred PaperID identity. Without a DOI, title + year + first author are all required.
- A matching PaperID with a different SHA-256 is `SAME_WORK_DIFFERENT_VERSION`, not a new paper and not an overwrite.
- A matching SHA-256 assigned to a different PaperID, inconsistent metadata/PaperID, insufficient fallback identity, or an unmanaged occupied destination fails closed into `import_staging/review/`.

The first managed version uses `papers/<PaperID>.<ext>`. Later versions use `papers/<PaperID>__<full-SHA256>.<ext>`. The first version is never silently replaced or promoted. Version-role judgment is recorded but remains explicit/user-reviewable.

## Harness acquisition integration

The publisher/database chain is unchanged:

```text
search -> detail identity lock -> authorized official download
       -> run raw/archive validation -> SHA-256 -> run manifest/audit
       -> Global Library reconciliation
```

The run manifest continues to point to the run archive and now also records `LibraryDisposition`, `LibraryStatus`, `LibraryManagedPath`, `LibraryNotesPath`, and `LibraryCatalogPath`. A Library conflict never erases run evidence.

## External Agent contract

There is intentionally no recursive directory or full-disk scanner. WorkBuddy/Hy3 must submit one explicit candidate at a time or call the same API from a separately reviewed inventory loop.

1. COPY one candidate without changing the source:

```powershell
.venv\Scripts\python.exe -m hunnu_harness.cli library-stage --source <explicit-paper.pdf>
```

The command returns a SHA-256-addressed staging path and a provenance sidecar containing the original source path. It performs no MOVE or DELETE.

2. Supply explicit metadata, for example:

```json
{
  "Title": "Example paper",
  "Authors": ["A. Author"],
  "Year": "2026",
  "DOI": "10.1000/example",
  "Journal": "Example Journal",
  "VersionRole": "PublisherPDF",
  "SourceLocator": "WorkBuddyInventory"
}
```

3. Import only the staged candidate:

```powershell
.venv\Scripts\python.exe -m hunnu_harness.cli library-import `
  --source <returned-staging-path> `
  --metadata-json <one-object-metadata.json>
```

Before PaperID generation, supplied metadata is treated as a claimed identity. The importer validates the local PDF and independently checks first-page content using existing DOI/title normalization. A normalized DOI match verifies a DOI claim unless the title explicitly contradicts it. Without a supplied DOI, a normalized title match can verify a complete title/author/year fallback claim. DOI conflict, title mismatch, or unavailable identity evidence fails closed before any managed PDF or catalog write. The gate performs no network lookup and no OCR.

Expected classifications are `NEW_PAPER`, `EXACT_DUPLICATE`, `SAME_WORK_DIFFERENT_VERSION`, `IDENTITY_CONFLICT`, `INVALID_PDF`, `INSUFFICIENT_IDENTITY`, and `EXTERNAL_IDENTITY_UNVERIFIED`. Rejected/conflicting files remain in staging/review; the source and staged candidate are not removed.

4. Read the topic filing on the same result. Once the candidate is a managed WORK, the import runs the post-acquisition classification the download path runs (AGENTS.md 69) and reports it in the download manifest's field set: `ClassificationStatus`, `AssignedTopics`, `AssignedPrimaryTopic`, `AssignedSecondaryTopics`, `ProposedTopics`, `TopicReviewRequired`, `TopicMetadataUpdated`, `TopicViewUpdated`, `ClassificationReason`, and the split Navigator readiness. `CLASSIFIED` means the WORK is filed; `REVIEW_REQUIRED` means it is safely managed, carries no topic yet, and `ProposedTopics` is what `library-confirm-topics` will accept; `SKIPPED_EXISTING` means a further version reused the WORK's existing topics; `FAILED_SAFE` means classification could not run and the WORK still needs filing — the import itself succeeded. `TopicReviewRequired` is true exactly when the WORK still carries no topic. See [POST_ACQUISITION_CLASSIFICATION.md](POST_ACQUISITION_CLASSIFICATION.md).

Python callers may use `ExternalPaperImporter.stage_pdf()` and `ExternalPaperImporter.import_staged_pdf()`. They must preserve the single-writer rule for `papers.jsonl`. An importer built for the real Library attaches the classifier by default; one built for any other root gets none and reports `ClassificationStatus: NOT_ATTEMPTED`, so an isolated tree never reaches the frozen taxonomy or the shared topic store.

## Atomicity and recovery

The managed file is copied to a same-directory temporary file, SHA-256 verified, and committed with a no-overwrite hard-link operation. Catalog temporary files are prepared before commit. The managed file is committed before JSONL, so the catalog cannot claim a missing newly ingested file. `papers.jsonl` is the source of truth and is atomically replaced. `papers.csv` is a rebuildable human-readable projection; JSONL and CSV are not one simultaneous two-file transaction. If JSONL replacement succeeds but CSV projection replacement fails, CSV may be temporarily stale and can be rebuilt from JSONL; this does not lose the authoritative catalog or managed paper assets.

An interruption can at worst leave an unreferenced managed file or a stale CSV projection. It cannot overwrite an existing paper or make JSONL point to a file that was never committed. Any occupied but uncataloged destination is treated as a review conflict on the next attempt.

## Metadata correction safety

Bibliographic correction is limited to title, authors, year, and journal. DOI-
anchored records must retain the `stable_paper_id()` derived from their DOI.
For records without a DOI, a correction to title, first author, or year is
accepted only when the complete corrected fallback identity recomputes to the
existing PaperID; an incomplete or different fallback identity fails closed
with identity escalation. Journal-only correction does not participate in the
fallback identity. Before the catalog commit, a deep immutable snapshot checks
the PaperID, DOI, file hashes and paths, versions, and acquisition/import
provenance (including nested version provenance). Only the
`metadata_corrections` list may change, and it is append-only.

## Notes contract

All generated reading assets belong below `library/notes/<PaperID>/`, for example `reading.md`, `summary.md`, and `evidence.md`. Reading agents must treat `library/papers/` as immutable input and must never write summaries beside or into managed full-text files.

## Migration boundary

v0.2.12 performs no historical run migration. Existing `runs/.../downloads/` files remain untouched. A future migration must begin with a bounded dry-run inventory and may only COPY, verify `source SHA256 == destination SHA256`, and retain all original run evidence.
