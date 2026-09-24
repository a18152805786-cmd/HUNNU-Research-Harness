# Agent Integration and Global Routing (Harness v0.3.6; routing schema v0.2.9)

## Purpose

The Agent-facing entry point gives Codex a stable way to turn a bounded research-acquisition request into an existing Harness capability. It does not implement a new database adapter, start a browser, or bypass any authentication/security control. Live literature execution is adapter-first: the Harness resolves the adapter from the source plan before it constructs the existing workflow.

```text
Agent request
  -> AgentRequestRouter
  -> LiteratureSourcePlan
  -> AdapterExecutionBroker -> LITERATURE_ADAPTER_REGISTRY
  -> correct LiteratureSourceAdapter -> BrowserCommandPort
  -> LocalPlaywrightExecutor -> existing local Playwright backend
  -> existing LiteratureAcquisitionWorkflow / existing CNRDS workflow
  -> existing adapters, resolver, validation, SHA-256, manifest and audit
  -> Harness Output Root
```

For Harness-managed literature acquisition, Adapter resolution comes before any Harness-managed publisher navigation.

## Stable entry point

Use a structured no-network plan first:

```powershell
.venv\Scripts\python.exe -m hunnu_harness.cli agent-route --request-json request.json --dry-run
```

After an editable/package installation, the equivalent command is:

```powershell
hunnu-harness-agent --request-json request.json --dry-run
```

The result reports intent detection, selected existing sources, exact per-source caps, capability gaps, Output Root, and security policy. `--dry-run` writes a sanitized proof under the configured Output Root and never performs network acquisition.

## Request schema

```json
{
  "TaskType": "literature_search",
  "Query": "AI washing audit monitoring earnings management",
  "Languages": ["zh", "en"],
  "PreferredSources": "auto",
  "MaxCandidates": 30,
  "MaxDownloads": 10,
  "PeerReviewedPreferred": true,
  "ScreeningFirst": true,
  "AuthorizedFullTextOnly": true,
  "FullTextReadingMode": "local",
  "WriteObsidian": false
}
```

## Restricted literature search (CNKI)

Three optional fields confine a literature search to "academic journal articles, from these named journals, published in these years":

```json
{
  "TaskType": "literature_search",
  "Query": "2019-2026 年《示例学刊》《样本评论》中的示例议题研究",
  "PreferredSources": ["CNKI"],
  "ResourceType": "JournalArticle",
  "SourceJournals": ["示例学刊", "样本评论"],
  "YearStart": 2019,
  "YearEnd": 2026,
  "KeywordsCN": ["示例议题", "示例概念"],
  "MaxCandidates": 20
}
```

- `ResourceType` accepts only `JournalArticle` (also `journal_article`, `学术期刊`); any other value is an `INVALID_REQUEST`, never an unrestricted search.
- `SourceJournals` names at most 8 journals and implies `ResourceType=JournalArticle`. Names are searched as written (surrounding 《》 are removed); a name containing a quote or a control character is refused.
- `YearStart`/`YearEnd` may also be spelled `YearFrom`/`YearTo`; two spellings with different years are refused. In a restricted request the years are enforced on every result; in an unrestricted one they remain a screening preference, as before.
- A restriction cannot be combined with `ExactTitles`, `DOIs`, or `Authors` -- those lookups already name their target. `ExactTitles` can also come from quoted text in `Query`; pass `"ExactTitles": []` to clear them.
- Only sources with `supports_restricted_search` (see `hunnu-harness capabilities`; today CNKI alone) can honour it. The router returns `UNSUPPORTED_CAPABILITY` when any selected source cannot, including sources chosen by `PreferredSources=auto`.

How CNKI honours it: every query is one kns8s search with `crossids=YSTT4HG0` (学术期刊 only), in the 文献来源 field (`korder=LY`, one query per named journal) or, without named journals, the 主题 field (`korder=SU`). CNKI's one-box search takes one field per query, so topic terms in a `SourceJournals` request are left to screening rather than searched. Before any result is returned, the page's own hidden `briefRequest` must state that CNKI searched academic journals only and executed exactly that query, and every result row must be labelled 期刊. Otherwise the query ends with `CNKI_RESTRICTION_UNCONFIRMED` or `CNKI_RESTRICTION_NOT_APPLIED`, nothing from that page is returned, and no further search is sent in the run. Rows from another journal or outside the years are dropped and counted. Each query's `RestrictionOutcome` (scope evidence, rows kept and dropped, the years the page covered) is written to the run's `SEARCH_QUERY_LOG.csv` and `audit/LITERATURE_EVENTS.jsonl`, and `acquire` reports it as `SearchRestrictionOutcome`. Only the first result page is read, and CNKI's default order is newest first (`SortedBy` records the order the page used): a year range the first page never reaches returns no rows, and the note says so -- that is no evidence that the journal has no such articles.

## Public Official Web source

`TaskType=official_web` routes to the separate registered chain
`OfficialWebExecutionBroker -> PublicOfficialWebAdapter -> BrowserCommandPort`.
It does not enter `LiteratureAcquisitionWorkflow`, download papers, or perform
institutional access. An explicit `AllowedDomains` list is mandatory. Initial
URLs and final redirect destinations must remain within that allowlist.

Configured `OfficialDomainClaims` record the official domain plus the declared
journal/sponsor/publisher relationship. A matching claim is
`OFFICIAL_CONFIRMED`; an allowlisted domain without a configured relationship
is only `OFFICIAL_PROBABLE`; title text alone never proves officiality. Login,
CAPTCHA/security challenges, access restrictions, paywalls, non-HTML content,
and out-of-allowlist redirects stop the run. The adapter uses only typed
`Navigate` and `Observe` browser commands.

Discovery and evidence fetch remain separate phases. Candidate discovery may
produce an explicit URL, but evidence is admitted only after a fresh
OfficialWeb fetch validates its allowlist, final URL, configured relationship,
content type, and access state.

## Bounded multi-batch budgets

The single-batch `MaxDownloads` and `MaxDownloadsPerRun` maximum remains 25.
An explicit `TotalDownloadBudget` may exceed 25 only when
`BoundedBatchPlanner` creates a `MultiBatchPlan`. Every batch remains at or
below 25, candidate/download totals are preserved, retries are finite
(`MaxRetries` 0-3), and the coordinator rejects any batch or aggregate result
that exceeds its budget. Automatic retry requires an explicit retryable error
with a committed-download count; unknown exceptions stop the batch because
their commit state cannot be audited. Optional `QuotaGroups` are an additive extension
point. Routing/dry-run planning does not execute the batches.

For v0.1 data acquisition, use `TaskType="data_acquisition"` plus the existing CNRDS fields: `Database`, `Module`, and `Table`; optional bounded query fields include `Stocks`, `DateStart`, `DateEnd`, `Fields`, and `OutputFormat`.

## Routing behaviour

- Literature sources are limited to the already implemented `CNKI`, `SpringerLink`, `ScienceDirect`, and `OxfordAcademic` adapters.
- Oxford Academic requests are routed through `OxfordAcademicAdapter`, which owns the verified HUNNU institutional resolver, identity lock, authorized-download, validation, SHA-256, manifest, and archive rules when a compatible `BrowserCommandPort` exists. The Agent must not first navigate the publisher with raw Playwright MCP. In v0.2.18, live MCP-backed adapter execution is only entered through `BrowserSessionBroker` and the typed `MCPExecutor` boundary; `ManualDownloadHandoff` remains a distinct human-in-the-loop fallback and must never be reported as `OxfordUnattendedDownloadReady=true`. Neither path replays a signed URL or automates Chrome's native PDF Viewer UI.
- `PreferredSources="auto"` selects only those adapters and divides global candidate/download caps across selected sources. It never expands a user's cap.
- A requested unsupported source returns `HarnessCapabilityAvailable=false` with `MissingCapability`; it does not fall back to an improvised browser/download workflow.
- Requests above `MaxCandidates=30` or `MaxDownloads=10` enter `PlanningAndBudgetGate=true`. They require explicit user confirmation before live acquisition.
- `WriteObsidian=true`, online-first full-text reading, and requests carrying authentication material are rejected by the entry point.

## Live invocation boundary

The dry-run CLI intentionally has no live-browser mode. The preferred live API is `AgentRequestRouter.invoke_literature(plan, browser=browser_command_port, run_root=...)`. Harness resolves `plan.Source` through `LITERATURE_ADAPTER_REGISTRY`, validates the adapter identity and command-port contract, constructs the correct adapter, and only then constructs `LiteratureAcquisitionWorkflow`. A v0.2.16 local `BrowserTransport` is accepted only as a compatibility input and is wrapped once by `LocalPlaywrightExecutor`; adapters do not receive its page/context object graph. A legacy `adapter=` argument is retained for compatibility and tests, but it must be the exact registered class for the plan source (`type(adapter) is expected_type`), must already be bound to the supplied/validated command port, and must not be a subclass substitution. Source values are stripped before registry lookup and identity comparison. Missing or invalid resolution fails closed; no raw browser fallback is available. Do not begin a Harness-managed literature run by directly calling `browser_navigate`, `browser_click`, or another publisher browser operation.

The Python runtime's formal adapter boundary is the small command-oriented `BrowserCommandPort` (`Navigate`, `Observe`, `Click`, `Download`, and local `AuthenticatedFetch`). `LocalPlaywrightExecutor` translates those commands to the existing local Playwright backend and returns structured observations or Harness-owned `DownloadArtifact` values. v0.2.18 adds `BrowserSessionBroker` plus `MCPExecutor`: the broker holds logical session/page generation and the executor maps typed commands to an allow-listed host-mediated Playwright MCP tool client. The dedicated Research Chrome / Playwright MCP surface remains the sole browser controller; raw MCP tools are never a substitute for the adapter execution broker. Explicit manual/diagnostic browser work remains separate from Harness-managed literature execution. MCP full HTML and authenticated response-body capture remain explicit unsupported capabilities and fail closed.

Likewise, `AgentRequestRouter.invoke_data()` delegates only to the existing guarded CNRDS workflow and Download Manager.

Authentication remains manual. The Agent entry point never handles passwords, cookies, browser storage, tokens, headers, CAPTCHA, MFA, or login state.

## Output and reading policy

All runtime evidence, including dry-run results, goes beneath:

```text
Output Root (by default, the sibling directory `<repository-name>-Output`, or `HUNNU_HARNESS_OUTPUT_ROOT`)
```

The Core Root remains code, tests, fixtures, configuration, and documentation only. For authorized full text, use the web page for search/metadata/access/normal download, then read the validated local file for full-text evidence extraction and analysis:

```text
DefaultFullTextReadingMode=LocalFile
BrowserFullTextReadingPreferred=false
AuthorizedDownloadPreferred=true
```

## Multi-source unattended preflight

For a multi-source request where the user plans to leave the computer or explicitly requests unattended execution, the Agent routing decision sets:

```text
UnattendedExecutionRequested=true
RunMultiSourcePreflight=true
```

The caller builds a `SourceCapabilityRegistry` from the existing Adapter registry and supplies source-matched handlers. User-controlled Playwright MCP pages may remain available for manual action, but they are not `BrowserCommandPort` instances and do not become a direct entry point into formal acquisition. `MultiSourcePreflightCoordinator` performs a dynamic-N authentication/reachability sweep, one batched manual-action gate, affected-source-only resume, and one current-session authorized download with validation and Target Identity Lock per source. `LiteratureAdapterPreflightHandler` rejects a handler whose adapter identity does not match the preflight source.

The Coordinator contains no source-specific branches. A future Adapter joins by registering its capabilities and a preflight handler. An existing local PDF or manual download handoff is not current-session unattended readiness evidence.

Runtime manifests are written below `<Output Root>\runs\Harness_V028_MultiSource_Preflight_Coordinator`. The formal research run stays stopped until every planned source is ready; partial execution requires explicit user approval.
