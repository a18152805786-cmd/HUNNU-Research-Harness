# HUNNU Research Harness v0.2.19 Freeze Review

## HarnessVersion

- HarnessVersion: `0.2.19`
- GitHeadBefore: `bad036a19fcf6136039396f230c763b086aa0bef`
- GitBranchBefore: `agent/v0.2.18-freeze`
- PlaywrightMCPVersion: `@playwright/mcp@0.0.79`

## Dirty-tree boundary

The pre-freeze dirty tree was re-audited immediately before this review.

### Group A — v0.2.18 MCP/CNKI live-acceptance hardening

- `src/hunnu_harness/browser/commands.py`
- `src/hunnu_harness/browser/local_executor.py`
- `src/hunnu_harness/browser/mcp_executor.py`
- `src/hunnu_harness/browser/session_broker.py`
- `src/hunnu_harness/literature/adapters/cnki.py`
- `tests/test_cnki_adapter.py`
- `tests/test_mcp_executor.py`
- `tests/fixtures/literature/cnki_article_snapshot.yml`
- `tests/fixtures/literature/cnki_search_snapshot.yml`

Purpose: structured snapshots, challenge-visibility probing, new-page following,
bounded download landing, and CNKI MCP live-acceptance hardening.

Previous evidence: v0.2.18 static and MCP/CNKI acceptance coverage was complete
before this freeze; the current full suite also passes all Group A regressions.

### Group B — v0.2.19 OfficialWeb and bounded multi-batch research

- `AGENTS.md`
- `README.md`
- `docs/AGENT_INTEGRATION.md`
- `pyproject.toml`
- `src/hunnu_harness/__init__.py`
- `src/hunnu_harness/agent_entrypoint.py`
- `src/hunnu_harness/batching.py`
- `src/hunnu_harness/official_web.py`
- `tests/test_version_metadata.py`
- `tests/test_bounded_batching.py`
- `tests/test_official_web.py`
- `docs/FREEZE_REVIEW.md`

Purpose: registered and fail-closed OfficialWeb routing with explicit domain
allowlists and officiality claims, plus bounded multi-batch planning and finite
retry/aggregate-budget enforcement.

OverlappingFiles: `NONE`
UnclassifiedFiles: `.commandcode/taste/taste.md` (empty, unrelated user file; not staged)

## OfficialWeb live acceptance

HarnessRoute: `AgentRequestRouter -> OfficialWebExecutionBroker -> PublicOfficialWebAdapter -> BrowserSessionBroker -> MCPExecutor -> typed Playwright MCP`

### Domain 1

- Journal: `世界经济`
- URL: `https://sjjj.magtech.com.cn/`
- FinalURL: `https://sjjj.magtech.com.cn/CN/home`
- Officiality: `OFFICIAL_CONFIRMED`
- Navigate: `PASS`
- Snapshot: `PASS`
- Evidence: `PASS`
- Evidence excerpt: `《世界经济》动态`, `投稿须知`, and the footer identifying `《世界经济》编辑部` and the Chinese Academy of Social Sciences institute address.

### Domain 2

- Journal: `中国工业经济`
- URL: `https://ciejournal.ajcass.com/`
- FinalURL: `https://ciejournal.ajcass.com/`
- Officiality: `OFFICIAL_CONFIRMED`
- Navigate: `PASS`
- Snapshot: `PASS`
- Evidence: `PASS`
- Evidence excerpt: `关于本刊`, publisher/editor information, and the footer identifying the Chinese Academy of Social Sciences Institute of Industrial Economics as sponsor.

LiveDomainsTested: `2`
DifferentOfficialDomain: `true`
NegativeAllowlistURL: `https://example.com/`
OutOfAllowlistBlocked: `true`
NegativeStatus: `UNSUPPORTED_ACCESS`
NegativeError: `Initial URL is outside AllowedDomains`
OfficialWebLiveAcceptance: `PASS`

## Regression tests

- TestsBeforeFreeze: `434`
- TestsBeforeFreezePassed: `434`
- TestsBeforeFreezeFailed: `0`
- Command: `.venv\\Scripts\\python.exe -m unittest discover -s tests -v`
- `git diff --check`: passed.

## Multi-batch dry run

- TotalPlanned: `36`
- BatchCount: `2`
- Batches: `18 + 18`
- LargestBatch: `18`
- PerBatchLimit: `25` (preserved)
- TotalBudget: `36` (preserved)
- PlanningAndBudgetGate: `true`
- NoBatchExceeds25: `true`
- InfiniteRetryImpossible: `true` (finite `MaxRetries=1` in the accepted plan)
- DownloadsPerformed: `0`

## Security and sensitive-data review

- SecurityBypassPerformed: `false`
- TemporaryBrowserUsed: `false`
- NodeReplUsedAsBrowser: `false`
- FourJournalResearchStarted: `false`
- No login, CAPTCHA, MFA, paywall, or security challenge was bypassed.
- No cookies, browser state, credentials, tokens, or article downloads were staged or committed.
- Pattern review found only policy/documentation references to password/cookie/token terms; no secret values, credentials, or session material were found.
- SecretsDetected: `false`

## Commit plan

1. Commit Group A only as v0.2.18 MCP/CNKI live-acceptance hardening.
2. Verify Group B remains unstaged.
3. Commit Group B, including this review, as v0.2.19 OfficialWeb + bounded multi-batch research.
4. Create the non-overwriting tag `v0.2.19` only after both commits pass post-commit checks.

