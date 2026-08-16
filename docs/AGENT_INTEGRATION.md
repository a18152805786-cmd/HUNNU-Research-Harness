# Agent Integration and Global Routing (v0.2.8)

## Purpose

The Agent-facing entry point gives Codex a stable way to turn a bounded research-acquisition request into an existing Harness capability. It is a thin routing layer. It does not implement a new database adapter, start a browser, or bypass any authentication/security control.

```text
Agent request
  -> AgentRequestRouter
  -> existing LiteratureAcquisitionWorkflow / existing CNRDS workflow
  -> existing adapters, resolver, validation, SHA-256, manifest and audit
  -> Harness Output Root
```

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
- Oxford Academic reuses the verified HUNNU institutional resolver. The Agent first attempts unattended download with the exact existing Research Chrome profile, whose profile-local `plugins.always_open_pdf_externally` preference can be configured offline by `browser-configure-pdf-download`. Harness then clicks the identity-locked official PDF action and expects a normal Playwright download event before staging, validation, Target Identity Lock, SHA-256, manifest, and archive. No ordinary Chrome profile or system policy is changed. If this automatic path is unavailable, `ManualDownloadHandoff` remains a distinct human-in-the-loop fallback and must never be reported as `OxfordUnattendedDownloadReady=true`. Neither path replays a signed URL or automates Chrome's native PDF Viewer UI.
- `PreferredSources="auto"` selects only those adapters and divides global candidate/download caps across selected sources. It never expands a user's cap.
- A requested unsupported source returns `HarnessCapabilityAvailable=false` with `MissingCapability`; it does not fall back to an improvised browser/download workflow.
- Requests above `MaxCandidates=30` or `MaxDownloads=10` enter `PlanningAndBudgetGate=true`. They require explicit user confirmation before live acquisition.
- `WriteObsidian=true`, browser-first full-text reading, and requests carrying authentication material are rejected by the entry point.

## Live invocation boundary

The dry-run CLI intentionally has no live-browser mode. A caller that has already selected the dedicated, user-authenticated Playwright MCP page may pass its existing supported adapter to `AgentRequestRouter.invoke_literature()`. That method delegates to the existing `LiteratureAcquisitionWorkflow`; it does not create a browser/profile or replace the Adapter, Institutional Access Resolver, identity lock, download validator, SHA-256, manifest, or audit pipeline.

Likewise, `AgentRequestRouter.invoke_data()` delegates only to the existing guarded CNRDS workflow and Download Manager.

Authentication remains manual. The Agent entry point never handles passwords, cookies, browser storage, tokens, headers, CAPTCHA, MFA, or login state.

## Output and reading policy

All runtime evidence, including dry-run results, goes beneath:

```text
C:\Users\<user>\Desktop\HUNNU-Research-Harness-Output
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

The caller builds a `SourceCapabilityRegistry` from the existing Adapter registry and supplies handlers backed by the dedicated Playwright MCP pages. `MultiSourcePreflightCoordinator` performs a dynamic-N authentication/reachability sweep, one batched manual-action gate, affected-source-only resume, and one current-session authorized download with validation and Target Identity Lock per source.

The Coordinator contains no source-specific branches. A future Adapter joins by registering its capabilities and a preflight handler. An existing local PDF or manual download handoff is not current-session unattended readiness evidence.

Runtime manifests are written below `C:\Users\<user>\Desktop\HUNNU-Research-Harness-Output\runs\Harness_V028_MultiSource_Preflight_Coordinator`. The formal research run stays stopped until every planned source is ready; partial execution requires explicit user approval.
