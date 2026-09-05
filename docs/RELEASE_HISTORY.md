# Release history

A compact record of what each tagged release changed. Tags `v0.2.12` through
`v0.3.5` are the authoritative artifacts; `git show <tag>` gives the full
tree, and the superseded per-release freeze reviews remain in git history.

Only durable facts are kept here. Test counts, dirty-tree file lists, and
per-run research state were snapshots of their moment and are not repeated —
the current suite and `git log` are the live sources for those.

## Pinned runtime baseline

`@playwright/mcp@0.0.79`, pinned in the Codex MCP configuration since v0.2.18.
Adopting a newer MCP release requires a controlled compatibility probe plus a
full Harness regression before the baseline moves. See
[PLAYWRIGHT_MCP_TRANSPORT_ARCHITECTURE.md](PLAYWRIGHT_MCP_TRANSPORT_ARCHITECTURE.md).

## Releases

| Version | Date | Scope |
|---|---|---|
| v0.2.17 | 2026-08-19 | Browser command layer: `BrowserCommandPort`, typed commands, `LocalPlaywrightExecutor`. Adapters stop touching Playwright objects directly. |
| v0.2.18 | 2026-08-19 | Playwright MCP baseline frozen at `0.0.79`. `BrowserSessionBroker` and `MCPExecutor` land; structured snapshots, challenge-visibility probing, new-page following, bounded download landing. |
| v0.2.19 | 2026-08-20 | `OfficialWeb` routing, fail-closed with explicit domain allowlists and officiality claims. Bounded multi-batch planning with finite retry and aggregate budget enforcement. |
| v0.2.20 | 2026-08-20 | CNKI exact-title refresh/relock compatibility: URL/form query decoding separated from bibliographic title identity, restricted title canonicalization, hidden non-semantic title markers excluded. Challenge, authentication, transport, and PDF logic unchanged. |
| v0.2.21 | 2026-08-20 | Authorized download capture. |
| v0.2.22 | 2026-08-30 | Post-acquisition topic filing closes its loop, and a ScienceDirect acquisition completes end to end for the first time. Topic assignments now carry provenance: automatic, human-confirmed, or unknown for the corpus that predates the record — absence is never read as an automatic decision. `library-confirm-topics` gives `REVIEW_REQUIRED` a formal exit, accepting only what classification proposed for that work and re-checking the frozen taxonomy regardless. The research browser stays alive between runs, so an institutional sign-in survives more than one command; a run attaches to it rather than launching its own, and closing means letting go of the connection. Downloads through an attached browser are caught from the browser's own account of them — source host, paper identifier and completion all validated — because Playwright's download event does not reach an attached page. ScienceDirect pages are read once they have said what they are: search results and the PDF control both render after `domcontentloaded`, and reading earlier reported a paper as absent and an entitled article as paywalled. Corpus resealed at 181 works. |
| v0.3.0 | 2026-09-01 | The zip-distribution vintage (the `dist/zip-release` effort, 25 commits). Deep Output Roots survive: the Windows extended-length prefix is applied at the I/O boundary only, never in persisted or caller-visible paths. Personal-path defaults leave the CLI, docs and tests; system Chrome is auto-discovered, with `HUNNU_RESEARCH_CHROME`, `HUNNU_RESEARCH_PROFILE` and `HUNNU_HARNESS_OUTPUT_ROOT` as the knobs. The catalog `schema_version` is checked at load. Fetch ledger: the daily total becomes a real knob (default 15, `--daily-limit` / `HUNNU_HARNESS_DAILY_FETCH_LIMIT`), `--allow-refetch` no longer lifts it, publisher fetches are paced (15 s spacing, 12 per 10 minutes), and the ceiling refusal informs instead of gatekeeping. Research-direction vocabularies (navigator lexicon, English taxonomy aliases) become packaged, overridable JSON data. Exact dependency pins for the lockfile-less zip; `scripts/build_dist_zip.py` builds it from tracked files only and refuses dirty trees. CLI: `acquire` joins the main console script, every subcommand takes `--json`, the graded exit ladder 0-5 lives in `exit_codes.py`, and `capabilities` / `doctor` judge the build and the machine without network activity. Navigator: an empty or small library explains itself (`EMPTY_LIBRARY`; `paper-gaps` refuses below 30 works). AGENTS.md opens with the section for the agent that will modify this harness, `docs/AGENTS_ENFORCEMENT_AUDIT.md` classifies all 71 rules by their enforcing anchor, and audit logging shares the literature sanitizer. README opens with the install runbook. One version stated per surface: pyproject, `__version__`, the README title and `--version`. |
| v0.3.1 | 2026-09-01 | The pytest self-check asserts shipped defaults: a conftest fixture clears the four ledger knob env vars per test, so a user's raised daily limit no longer fails their own suite. |
| v0.3.2 | 2026-09-01 | The catalog schema gate admits its own lineage (`0.2.10` and `0.2.11`) and still refuses strangers; new records keep stamping `0.2.11`. |
| v0.3.3 | 2026-09-01 | The four-source live acceptance vintage, each fault fixed with its evidence in the audit trail. CNKI 2026 site refresh: dual-layout kns8s / kcms2 / bar.cnki.net recognition with every legacy selector kept as fallback, same-page navigation to the result's own href, the `ecp_` institution header by its real signature, and an access check that re-observes until the page state is unambiguous. The HUNNU library route recognizes the Chaoxing outer-proxy hop and accepts `HUNNU_OXFORD_ROUTE_URL` under the full identity lock; Oxford splits gateway from direct route. Directed downloads learn OUP and CNKI identities and delivery hosts, claim a redirected delivery by its suggested filename or by an adapter-declared bibliographic label, and allowlists honour dot-prefixed suffixes (`.silverchair.com` matches subdomains only). Institutional snapshots retry through interstitials, and every setup-phase error keeps the one-document JSON and graded-exit contract. Corpus resealed at 185 works / 198 physical versions / 345 topic assignments. |
| v0.3.4 | 2026-09-02 | `paper-index build` / `rebuild` fail closed: extraction writes nothing, and when hard failures cover every extraction attempt or more than half of them (canonically `pypdf` missing from the running interpreter, now classified `DEPENDENCY_MISSING`) the build refuses to commit, keeps the previous index byte-identical, emits a `REFUSED` payload naming the cause and exits 4; `--allow-degraded` is the explicit opt-in. `doctor` checks that `pypdf` is importable. A live run's browser teardown can no longer replace the graded JSON report with a traceback. Version headers in AGENTS.md and the agent-integration doc track the release again, this history records v0.3.0 onward, and the README's `browser-start` and test-runner paragraphs describe the shipped behaviour. |
| v0.3.5 | 2026-09-05 | Six fixes, three of them found the hard way during a live acquisition run. `SEARCH_RESULTS.csv` no longer drops the volume, issue and pages the adapters had already parsed. `TargetDOIMatched` / `TargetTitleMatched` distinguish a check that was not performed from one that failed. A CNKI download whose filename Chrome sanitized is now claimed, so a completed download is no longer reported as a timeout (the misreport caused a real duplicate publisher fetch). `browser-stop` asks Chrome to exit gracefully and reports stopped only once the port is quiet and no process holds the profile. The sign-in survives a browser lifecycle through Chrome's own `--restore-last-session` instead of a forged tracked preference — not yet confirmed against a live browser. `library-import` runs the same post-ingest topic classification as an acquisition and reports it in the manifest's field set, and `library-confirm-topics` gates on whether the WORK carries a topic rather than on the classifier's regenerated verdict, so a WORK archived without being filed can always be filed (a manually imported paper had been stranded: `MANAGED`, no topic, refused as `NOT_REVIEW_REQUIRED`). Corpus resealed at 188 works / 201 physical versions / 351 topic assignments. |

## Standing policy across all releases

No release has ever relaxed these, and none may:

- No CAPTCHA or challenge bypass, and no automated challenge interaction.
- No credential entry, cookie export, or MFA automation. Authentication is
  manual and human-performed.
- Licensed full text is acquired only through authorized, entitled routes.
- Research state and downloaded artifacts live under the Harness Output Root,
  never inside this repository.
