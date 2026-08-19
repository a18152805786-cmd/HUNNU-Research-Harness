# Agent Integration and Global Routing (Harness v0.2.17; routing schema v0.2.8)

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

For v0.1 data acquisition, use `TaskType="data_acquisition"` plus the existing CNRDS fields: `Database`, `Module`, and `Table`; optional bounded query fields include `Stocks`, `DateStart`, `DateEnd`, `Fields`, and `OutputFormat`.

## Routing behaviour

- Literature sources are limited to the already implemented `CNKI`, `SpringerLink`, `ScienceDirect`, and `OxfordAcademic` adapters.
- Oxford Academic requests are routed through `OxfordAcademicAdapter`, which owns the verified HUNNU institutional resolver, identity lock, authorized-download, validation, SHA-256, manifest, and archive rules when a compatible `BrowserCommandPort` exists. The Agent must not first navigate the publisher with Playwright MCP. `PlaywrightMCPTransportImplemented=false` in v0.2.17, so live MCP-backed adapter execution is not claimed or initiated; `ManualDownloadHandoff` remains a distinct human-in-the-loop fallback and must never be reported as `OxfordUnattendedDownloadReady=true`. Neither path replays a signed URL or automates Chrome's native PDF Viewer UI.
- `PreferredSources="auto"` selects only those adapters and divides global candidate/download caps across selected sources. It never expands a user's cap.
- A requested unsupported source returns `HarnessCapabilityAvailable=false` with `MissingCapability`; it does not fall back to an improvised browser/download workflow.
- Requests above `MaxCandidates=30` or `MaxDownloads=10` enter `PlanningAndBudgetGate=true`. They require explicit user confirmation before live acquisition.
- `WriteObsidian=true`, online-first full-text reading, and requests carrying authentication material are rejected by the entry point.

## Live invocation boundary

The dry-run CLI intentionally has no live-browser mode. The preferred live API is `AgentRequestRouter.invoke_literature(plan, browser=browser_command_port, run_root=...)`. Harness resolves `plan.Source` through `LITERATURE_ADAPTER_REGISTRY`, validates the adapter identity and command-port contract, constructs the correct adapter, and only then constructs `LiteratureAcquisitionWorkflow`. A v0.2.16 local `BrowserTransport` is accepted only as a compatibility input and is wrapped once by `LocalPlaywrightExecutor`; adapters do not receive its page/context object graph. A legacy `adapter=` argument is retained for compatibility and tests, but it must be the exact registered class for the plan source (`type(adapter) is expected_type`), must already be bound to the supplied/validated command port, and must not be a subclass substitution. Source values are stripped before registry lookup and identity comparison. Missing or invalid resolution fails closed; no raw browser fallback is available. Do not begin a Harness-managed literature run by directly calling `browser_navigate`, `browser_click`, or another publisher browser operation.

The Python runtime's formal adapter boundary is the small command-oriented `BrowserCommandPort` (`Navigate`, `Observe`, `Click`, `Download`, and local `AuthenticatedFetch`). `LocalPlaywrightExecutor` translates those commands to the existing local Playwright backend and returns structured observations or Harness-owned `DownloadArtifact` values. `PlaywrightMCPTransportImplemented=false` in v0.2.17: the runtime does not implement a Python bridge for Codex Playwright MCP. The dedicated Research Chrome / Playwright MCP surface remains a user-controlled surface for manual/diagnostic browser work and existing manual-authentication workflows; it must not be called directly as a substitute for the adapter execution broker. A stable MCP transport is a separate Phase B task.

Likewise, `AgentRequestRouter.invoke_data()` delegates only to the existing guarded CNRDS workflow and Download Manager.

Authentication remains manual. The Agent entry point never handles passwords, cookies, browser storage, tokens, headers, CAPTCHA, MFA, or login state.

## Output and reading policy

All runtime evidence, including dry-run results, goes beneath:

```text
C:\Users\71966\Desktop\HUNNU-Research-Harness-Output
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

Runtime manifests are written below `C:\Users\71966\Desktop\HUNNU-Research-Harness-Output\runs\Harness_V028_MultiSource_Preflight_Coordinator`. The formal research run stays stopped until every planned source is ready; partial execution requires explicit user approval.
