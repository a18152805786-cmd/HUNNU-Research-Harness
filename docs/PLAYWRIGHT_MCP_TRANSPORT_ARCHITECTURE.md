# Phase B — Playwright MCP Transport Architecture Design

Status: v0.2.17 local command-layer implementation update; MCP executor remains design-only.

This document is an architecture audit and implementation record. v0.2.17
implements only the backend-neutral command/observation boundary, the local
Playwright executor, and the four adapter migrations. It does not implement
PlaywrightMCPTransport, launch a browser, modify a profile, or perform a
four-publisher smoke test.

## Current release and scope

v0.2.16 is frozen before the v0.2.17 implementation:

- Commit: 550f44f75dadc45d32f1a4d1089b2a38ba45f132
- Tag: v0.2.16
- Branch: main
- Phase A/v0.2.17 control path: AgentRequestRouter -> LiteratureSourcePlan ->
  AdapterExecutionBroker -> LiteratureAdapterFactory -> registered adapter ->
  BrowserCommandPort -> LocalPlaywrightExecutor
- PlaywrightMCPTransportImplemented: false

v0.2.17 implementation facts:

- BrowserCommandContractImplemented: true
- BrowserObservationContractImplemented: true
- DownloadArtifactImplemented: true
- LocalPlaywrightExecutorImplemented: true
- CNKI/SpringerLink/ScienceDirect/OxfordAcademic adapter migration: complete
- BrowserSessionBrokerImplemented: false
- MCPExecutorImplemented: false

The Phase A guarantee is a control-plane guarantee. It prevents the Harness
literature API from silently falling back to a bare browser when adapter
resolution fails. It does not yet prove that an adapter can safely drive the
Codex Playwright MCP session.

Non-goals for this document:

- no PlaywrightMCPTransport implementation;
- no MCPPage, MCPLocator, MCPContext, MCPDownload, or MCPResponse classes;
- no new Chromium process or profile;
- no profile, cookie, storage-state, or credential access;
- no real CNKI, SpringerLink, ScienceDirect, or Oxford Academic acceptance run;
- no changes to research data or OutputRoot contents.

## Current state

### Control-plane and local execution boundary

The v0.2.17 route remains adapter-first and now has a local command boundary:

    Agent request
      -> AgentRequestRouter
      -> LiteratureSourcePlan
      -> AdapterExecutionBroker
      -> registry-backed adapter factory
      -> source adapter
      -> BrowserCommandPort
      -> LocalPlaywrightExecutor
      -> existing local Playwright backend

The relevant implementation is in
src/hunnu_harness/literature/execution.py,
src/hunnu_harness/agent_entrypoint.py,
src/hunnu_harness/browser/commands.py,
src/hunnu_harness/browser/port.py, and
src/hunnu_harness/browser/local_executor.py. The broker, command port, and
executor are source-neutral; publisher selection remains registry-driven
rather than a four-source conditional in the broker or executor.

### Current BrowserTransport finding

CurrentBrowserTransportBackendNeutral=false

CurrentTransportLeaksPlaywrightObjects=true

The current protocol in src/hunnu_harness/browser/transport.py is a
Python-Playwright-shaped compatibility surface:

- BrowserTransport exposes page and downloads_dir;
- BrowserPage exposes url, context, content(), locator(),
  expect_download(), and on();
- validation requires page.context event support;
- validation also requires page.context.request.get for the Springer
  official-PDF path.

This remains a useful compatibility contract for the existing Python backend,
but it is not the formal adapter boundary. The new adapters receive typed
commands and observations; the old object graph is used only inside the local
executor/authorized-capture compatibility implementation.

The existing local backend in
src/hunnu_harness/browser/playwright_backend.py launches a persistent Python
Playwright context. It is not a CDP attach implementation and it checks for
profile lock files before launch. It therefore cannot be used as evidence
that Python can take over the already-running MCP-controlled Research Chrome
session.

### Current MCP evidence

The current Codex tool surface is a tool-call/RPC surface. The observed
Playwright operations include:

    browser_navigate
    browser_click
    browser_snapshot
    browser_type
    browser_fill_form
    browser_select_option
    browser_press_key
    browser_wait_for
    browser_tabs
    browser_navigate_back
    browser_hover
    browser_find
    browser_handle_dialog
    browser_file_upload
    browser_network_request
    browser_network_requests
    browser_evaluate
    browser_run_code_unsafe
    browser_take_screenshot
    browser_console_messages

There is no observed structured browser_download operation and no
Python-callable Page, Locator, BrowserContext, Download, or Response object.
Snapshot references and tab indexes are tool-call values, not stable Python
object handles. browser_run_code_unsafe is explicitly not an acceptable
general-purpose adapter bridge.

The current MCP registration in the local Codex configuration starts Chrome
with the dedicated Research Chrome profile and an OutputRoot staging
directory, but the package is registered as @playwright/mcp@latest. This
configuration establishes intent, not a version-pinned or runtime-verified
session/download contract.

## Adapter browser dependency inventory

The inventory below is based on the current adapter source and shared browser
helpers. “Direct MCP representation” means a stable representation suitable
for a formal Harness protocol, not merely that an Agent could attempt a
similar free-form tool call.

| Operation | Used by | Importance | Direct MCP representation | Stateful semantics |
| --- | --- | --- | --- | --- |
| goto(url) | CNKI, SpringerLink, ScienceDirect, OxfordAcademic | Core search/open/access path | Yes, through browser_navigate, subject to page/session identity | Session and current page are required |
| page.url | All four; challenge and identity checks | Core provenance and identity | Not guaranteed as a standalone result; must be part of a structured observation | Must refer to the same page as the command |
| page.content() | All four through _content() | Core result parsing, access checks, metadata, challenge checks | No direct safe equivalent in the observed tool surface; snapshot is not raw HTML | Observation must be tied to a page revision |
| locator(selector) | All four download paths | Core target selection | Snapshot refs or a broker target descriptor can represent a target; a Python locator cannot | Ref becomes stale after page changes |
| locator.filter(...).first | CNKI, SpringerLink, ScienceDirect, OxfordAcademic downloads | Core disambiguation | Not as a long-lived object; requires fresh snapshot plus bounded target resolution | Ambiguity must fail closed |
| locator.click() | All four download paths and capture helper | Core authorized action | Yes through browser_click after Harness target resolution | Must be one operation with an idempotency key |
| expect_download() | CNKI, ScienceDirect; shared capture for SpringerLink/OxfordAcademic | Core artifact acquisition | No direct download event tool was observed | Requires an explicit pending/completed artifact protocol |
| download.suggested_filename | CNKI, ScienceDirect; shared capture | File naming and provenance | No direct result field was observed | Must be returned as sanitized artifact metadata |
| download.save_as(path) | All four, directly or through capture helper | Required for controlled local validation | No direct MCP equivalent was observed | MCP must write to broker-controlled staging or return a controlled artifact |
| context.pages / browser_tabs | Shared capture and popup handling | Page lifecycle and popup control | browser_tabs can observe/select tabs, but does not expose Page objects | Page IDs and tab ownership must be explicit |
| context.on("page"/"download"/"response") | BrowserAuthorizedFileCapture | Download/popup/response correlation | No event subscription API was observed | Executor must return correlated observations |
| context.request.get | SpringerLink official stable PDF path | Authenticated response artifact | browser_network_request is inspection-oriented and not a safe artifact contract | Requires explicit authenticated-artifact capability |
| response.ok/status/url/headers/body | SpringerLink and shared capture | PDF response validation and fallback | No stable structured response-body artifact contract was observed | Body/path must be broker-owned, bounded, and auditable |
| navigation_provenance | CNKI challenge detector | Security and route provenance | No current MCP equivalent | Must be supplied by the session broker or replaced with a typed observation |
| downloads_dir | All four and manual handoff | OutputRoot separation and validation | Configuration points into OutputRoot, but completion/path ownership is unverified | Directory ownership must belong to Harness artifact handling |
| manual file scan | OxfordAcademic ManualDownloadHandoff | Human download fallback | MCP-independent filesystem operation | State can persist if the watch directory contract is stable |

### Source-specific summary

CNKI:

- navigation and page URL/content for search, result opening, access checks,
  metadata, and challenge inspection;
- locator filtering and first-match selection for the official action;
- download event, suggested filename, and save_as for the PDF;
- additional navigation_provenance consumed by challenge detection.

SpringerLink:

- navigation and page URL/content for search, opening, and access checks;
- locator filtering plus BrowserAuthorizedFileCapture for the trusted gateway
  action;
- an alternate official-PDF path using page.context.request.get and
  response.body();
- response status, URL, headers, and PDF bytes are therefore part of the
  current dependency surface.

ScienceDirect:

- navigation and page URL/content for search, opening, and access checks;
- locator filtering, expect_download, suggested filename, and save_as for
  the PDF.

OxfordAcademic:

- navigation and page URL/content for search, opening, and access checks;
- identity-locked locator action through BrowserAuthorizedFileCapture;
- manual download handoff when the native PDF viewer is reached;
- controlled staging, stable-file detection, and SHA-256 validation in the
  handoff path.

The shared
src/hunnu_harness/browser/authorized_file_capture.py helper is the strongest
evidence that the current surface is not merely a small navigation API. It
registers page and context listeners, correlates download/response/page events,
calls download.save_as(), and may read response.body(). A future MCP boundary
must replace this event-object dependency with structured observations and
artifact references; it must not pretend that MCP supplies the same objects.

## Answers to the core audit questions

### Q1 — Is the current transport abstraction backend-neutral?

No.

The protocol name is generic, but its required members are not. page,
context, locator, expect_download, on, context.request.get, response.body(),
and download.save_as() are all Python Playwright semantics or direct
dependencies on them. A class that merely forwards those names to MCP would
be a compatibility emulation layer, not a genuine transport abstraction.

The current contract is reusable as a local-backend compatibility layer during
migration. It should not be the Phase B MCP contract.

### Q2 — What is the MCP execution model?

MCP is an RPC/tool-call model with server-side browser state and structured
tool results. It is not a stateful Python object model exposed to the Harness.

The available calls can navigate, inspect snapshots, target visible elements,
fill or click, wait, inspect tabs, and inspect network requests. They do not
give the adapter a Python object whose methods can be awaited later. The
snapshot target references also have a lifetime and page-revision problem that
must be represented explicitly.

### Q3 — Should a fake Playwright object tree be built?

FakePlaywrightObjectGraphRecommended=false

The proposed MCPPage -> MCPLocator -> MCPContext -> MCPDownload ->
MCPResponse tree would have to simulate:

- locator lifetime and stale references after navigation or DOM changes;
- context page creation, popup ordering, and listener removal;
- download event timing and local file ownership;
- response body delivery and network correlation;
- asynchronous error and cancellation semantics;
- target references that are meaningful to the MCP server but not to Python.

That work would create a large, fragile second Playwright implementation and
would hide backend capability gaps from the adapter. It is especially unsafe
for download validation and security challenge handling. The design therefore
rejects this option as the default.

## Architecture options

### Option A — Low-level Playwright object emulation

Shape:

    Source Adapter
      -> BrowserTransport.page
      -> MCPPage / MCPLocator / MCPContext / ...
      -> MCP tools

Advantages:

- smallest immediate adapter diff;
- existing Playwright adapter code appears reusable;
- familiar API for the local backend.

Costs and risks:

- highest API coverage requirement and highest long-term maintenance cost;
- fragile locator and snapshot synchronization;
- no natural mapping for expect_download, context events, or response.body();
- popup and page lifecycle would be simulated rather than owned;
- errors would be translated twice;
- encourages the false claim that MCP is Python Playwright.

Decision: reject as the primary architecture. It is acceptable only as an
internal implementation detail of the local Python backend, never as the MCP
boundary.

### Option B — Command-oriented browser transport

Shape:

    Source Adapter
      -> BrowserCommandPort
      -> LocalPlaywrightExecutor or MCPCommandExecutor

Candidate commands are small and typed:

    Navigate
    ObservePage
    ResolveTarget
    Click
    Fill
    SelectOption
    PressKey
    Wait
    RequestDownloadArtifact
    InspectTabs

Candidate observations are also typed:

    PageObservation
    TargetObservation
    ActionObservation
    DownloadObservation
    ChallengeObservation
    SessionObservation
    FailureObservation

Advantages:

- no Page/Locator/Context leakage;
- the local backend can wrap Playwright internally;
- MCP mapping is natural at the command/result level;
- fake executors can test adapters without a browser;
- stale targets, page identity, and operation IDs can be explicit.

Costs and risks:

- all four adapters need a staged browser-operation refactor;
- current HTML parsing dependencies need a deliberate content/observation
  decision;
- download and authenticated-response artifacts need new contracts;
- the command vocabulary must remain small and source-neutral.

Decision: required transport direction and one half of the recommendation.

### Option C — Higher-level publisher browser drivers

Shape:

    Source Adapter
      -> PublisherBrowserDriver
      -> command transport

Advantages:

- a driver could hide publisher-specific UI sequencing;
- a difficult publisher could receive a dedicated state machine.

Costs and risks:

- the existing adapters already own publisher-specific search, identity,
  download, and security behavior;
- a driver that repeats those rules would split responsibility and duplicate
  policy;
- a driver-per-publisher does not itself solve MCP session ownership or
  download artifacts;
- it can reintroduce four-source branching at a lower layer.

Decision: do not use as the primary boundary. Permit a thin publisher driver
only when it is an execution helper owned by the corresponding adapter and
does not duplicate source policy.

### Option D — Harness tool boundary / Agent-mediated execution

Shape:

    Harness adapter state machine
      -> structured command envelope
      -> Agent/MCP executor
      -> structured observation envelope
      -> Harness resumes

Each command must carry at least run_id, source, adapter identity,
session_id, page_id, operation_id, command kind, target/URL constraints, and
an idempotency key. The Agent may execute only that command against the
dedicated MCP page and must return the corresponding observation or an
explicit failure/challenge result.

Advantages:

- matches MCP's actual Agent tool-call nature;
- does not require Python to own or attach to the MCP browser process;
- makes manual challenge pauses a first-class state;
- supports explicit continuation and audit evidence;
- prevents a formal Harness run from turning into autonomous browsing.

Costs and risks:

- requires a reliable command/observation coordinator;
- Agent/tool result fidelity and page identity must be checked;
- an MCP disconnect or stale target needs explicit recovery semantics;
- download artifact return and local path ownership remain a hard gate;
- deterministic replay must use fake executors rather than natural-language
  prompts.

Decision: required session/agent boundary and the second half of the
recommendation.

## Recommended architecture

RecommendedArchitecture=Hybrid(B+D)

Use a command-oriented, source-neutral BrowserCommandPort for adapter
execution, and use a Harness-owned session broker/command-observation
protocol when the executor is Codex Playwright MCP.

Proposed shape:

    AgentRequestRouter
      -> LiteratureSourcePlan
      -> AdapterExecutionBroker
      -> registered Source Adapter
      -> BrowserCommandPort
      -> BrowserSessionBroker
          -> LocalPlaywrightExecutor
          -> MCPCommandExecutor / Agent-mediated executor
      -> typed observation or typed artifact
      -> adapter/workflow validation

The source adapter remains responsible for publisher-specific:

- search and result interpretation;
- target identity lock;
- DOI and metadata rules;
- authorized download policy;
- source-specific challenge interpretation;
- file validation and security policy.

The command port is responsible for:

- navigation and bounded observation;
- target resolution and action execution;
- page/tab identity;
- operation correlation and idempotency;
- challenge and pause observations;
- download artifact handoff.

The session broker is responsible for:

- session/page leases and ownership;
- operation IDs and continuation tokens;
- rejecting commands for the wrong run/source/page;
- correlating tool results;
- persisting pause/resume checkpoints;
- fail-closed handling when MCP cannot provide a required result.

The local executor may use real Playwright objects internally. That is an
implementation detail of the local backend, not part of the shared command
contract. The MCP executor must map structured commands to MCP calls and
return structured observations; it must not expose fake Python object trees.

### Target and observation lifetime

A target reference should be opaque and short-lived:

    TargetRef = (session_id, page_id, observation_revision, target_id)

The broker must reject a target when the page has navigated, the observation
revision is stale, or the target cannot be uniquely identified. A fresh
observation may be requested as a bounded recovery, but the system must not
silently choose a different element or fall back to coordinate clicking.

### Content observation decision

The current adapters parse page.content() in several search/access paths.
An MCP accessibility snapshot is not equivalent to raw HTML. Phase B must
choose one of these explicit contracts before adapter production refactoring:

1. a safe, bounded PageObservation content artifact supplied by the executor;
2. structured extraction commands that return only the fields adapters need;
3. a deliberate adapter migration from HTML parsing to accessible/typed
   observations.

browser_evaluate and browser_run_code_unsafe must not be used as an implicit
unbounded HTML bridge. If a required observation cannot be provided by the
selected backend, the adapter capability is unsupported and execution fails
closed.

## Browser session ownership

### Current evidence and limits

The current Codex configuration intends to run Playwright MCP against:

- Chrome executable: the installed Research Chrome/Chrome executable;
- dedicated user-data-dir: C:\Users\71966\ResearchHarness\chrome-profile;
- MCP output directory under the Harness OutputRoot staging area.

The local Python Playwright backend separately launches a persistent context
and rejects a profile containing Singleton* lock files. The repository has no
formal cross-process session lease, CDP attach contract, or MCP-to-Harness
ownership handshake. A current chrome process is present, but its ownership
and command line were not used as proof of the active MCP session.

ResearchChromeOwnershipNeedsFurtherVerification=true

### Proposed ownership model

| Resource | Proposed owner | Rule |
| --- | --- | --- |
| Browser process for MCP route | MCP server launched for the Codex MCP connection | Python Harness must not launch a second browser for the same run |
| Research Chrome profile | User/administered dedicated profile | Never reset, clone, clear, or open concurrently through a second controller |
| Authenticated publisher session | Chrome profile plus user's manual authentication | Harness can consume observations; it never receives credentials or performs login |
| MCP connection | Codex MCP client/server lifecycle | The session broker records a logical connection/session ID and detects disconnect |
| Page/tab lifecycle | MCP server, under session-broker commands | Adapter receives page IDs/observations, not Page objects |
| Run state and evidence | Harness | Checkpoints, artifacts, validation, manifest, and audit records remain Harness-owned |
| Download staging | Harness OutputRoot | MCP must write or copy into a broker-approved staging location and return a controlled artifact reference |

This model preserves the existing login state by making one controller
responsible for the profile. It explicitly forbids “Python launches another
Chromium profile” as a Phase B shortcut.

### Required ownership gates

Before implementation, verify:

- exactly how the current MCP server starts or attaches to Chrome;
- whether the profile is currently MCP-owned, user-owned, or shared;
- profile-lock and CDP-conflict behavior;
- whether MCP reconnect preserves page/session identity;
- whether multiple planned sources use distinct pages under one owner;
- whether the configured OutputRoot is the actual MCP artifact directory;
- how the MCP server is versioned and how its tool schema is pinned.

Until these are verified, ResearchChromeLoginStatePreservable=true is a
policy/design property, not a live acceptance claim.

## Download model

### Current gap

The four adapters currently rely on at least one of:

    click -> expect_download -> download.save_as
    click -> response event -> response.body -> local file
    manual file appearance -> stable-file scan -> controlled staging copy

The observed MCP tool surface can perform a click, but it does not provide a
formal download event, a controlled local artifact reference, or a guaranteed
mapping from the server's output directory to a completed Harness artifact.
The network request inspection tools are not a substitute for an authorized,
auditable download artifact API.

Therefore:

    HowDoesDownloadStart =
      Harness issues a bounded, identity-locked Click/RequestDownload command;
      the MCP executor performs the visible normal authorized action.

    HowDoesHarnessKnowDownloadCompleted =
      A future executor must return a correlated DownloadObservation with
      operation_id and a completed artifact status. A click result alone is
      insufficient.

    HowDoesHarnessGetLocalPath =
      The future executor must return a broker-approved artifact reference or
      path inside OutputRoot staging. An arbitrary Agent-provided path is not
      trusted.

    HowDoesHarnessValidateFile =
      Harness reads the controlled local artifact, validates format and
      target identity, computes SHA-256, then creates the manifest and audit
      evidence. MCP-side metadata never replaces Harness validation.

    HowDoesMCPDownloadDirectoryMapToOutputRoot =
      The current configuration points MCP output under OutputRoot staging,
      but the completion/path mapping is not yet contractually verified.

### Required future DownloadObservation

A minimum typed observation should include:

    operation_id
    session_id
    page_id
    status
    artifact_id
    broker-controlled path or artifact reference
    sanitized suggested filename
    byte size
    acquisition method
    sanitized source/provenance URL

Harness remains authoritative for file existence, PDF signature, target
identity, DOI/metadata checks, SHA-256, manifest, and archive placement.

The Springer official-PDF response-body path is a specific blocker. A future
MCP executor must either provide a safe authenticated response artifact with
trusted-host and expected-URL evidence, or Springer must use a supported
visible download action. It must not silently fall back to raw network
inspection or unsafe evaluated code.

### Manual download handoff

The existing OxfordAcademic ManualDownloadHandoff is reusable because it is a
filesystem state machine rather than a Playwright object model. It can watch a
broker-approved MCP output directory, require a stable new PDF, copy it into
controlled staging, and verify SHA-256. It still requires a verified directory
ownership and completion contract before it can be used with MCP.

## Security verification and manual resume

Security verification, CAPTCHA, publisher challenges, login confirmation,
MFA, CAS, and WebVPN remain manual-only gates.

### Proposed flow

    MCP observation detects a blocking challenge
      -> ChallengeObservation
      -> Harness persists checkpoint and pauses
      -> user performs the permitted manual action in the same MCP page/profile
      -> user confirms completion
      -> broker resumes with the same session_id/page_id
      -> fresh observation and route/identity checks
      -> adapter continues

ChallengeObservation must contain a sanitized challenge kind, blocking state,
session/page identity, current URL/provenance, and the expected continuation
operation. It must never contain a password, OTP, cookie, token, or storage
state.

The checkpoint should include:

    run_id
    source
    plan_id
    adapter type and normalized source
    session_id
    page_id
    operation_id
    current workflow phase
    target identity lock
    challenge kind and pause reason
    expected next command
    download/artifact state

Resume must reject a different page, a different profile/session, or a
changed identity target unless the user explicitly restarts at a safe adapter
boundary. No retry may be used to solve or bypass a challenge.

## Adapter-first and dynamic N-source invariants

The recommended boundary preserves:

    Plan
      -> AdapterExecutionBroker
      -> registered adapter
      -> BrowserCommandPort
      -> controlled executor

The Agent cannot replace the adapter with an autonomous MCP browsing sequence
inside a Harness-managed literature run. If the selected executor is
Agent-mediated, the Agent may execute only the structured command envelope
issued by the broker. A free-form browser_navigate before adapter resolution is
outside the formal Harness route and must not be presented as a supported
Harness execution path.

The transport and broker remain source-neutral. Source-specific behavior stays
in registered adapters and source contracts. No transport implementation or
coordinator should add CNKI/SpringerLink/ScienceDirect/OxfordAcademic
conditionals. Future sources register capabilities and use the same command
protocol.

## Failure semantics

| Failure | Retry | Pause/manual action | Outcome |
| --- | --- | --- | --- |
| TransportUnavailable | No uncontrolled retry; bounded reconnect only with a verified session lease | No, unless reconnect needs user action | Fail source closed |
| MCPDisconnected | One broker-defined recovery attempt if session continuity is proven | Yes if user must restore the same session | Pause, then fail source if continuity is unproven |
| SessionLost or profile conflict | No second browser/profile | Yes; user or operator must restore ownership | Fail source closed |
| UnsupportedBrowserOperation | No raw MCP fallback | No | Fail source/configuration closed |
| LocatorNotFound | Bounded fresh-observation retry only | No | Fail action/source if still absent |
| Ambiguous target or stale TargetRef | No automatic selection | No | Fail closed; require a new identity-locked observation |
| UnexpectedPopup | No autonomous accept/close | Pause for inspection if it may be a challenge | Fail source if not classified safely |
| SecurityChallenge/CAPTCHA/login confirmation | No automated retry/solve | Yes, manual action in the same session | Pause; resume only after explicit confirmation |
| DownloadFailed | Bounded retry only if action idempotence is proven | Possibly, if user download handoff is the defined fallback | Fail source if artifact remains uncertain |
| DownloadArtifactMissing or path unverified | No | No | Fail closed before validation/manifest |
| Authenticated response artifact unsupported | No alternate raw request | No | Mark adapter/backend capability unsupported |
| UnsupportedAdapterCapability | No | No | Reject before publisher action |

No failure path may fall back from an adapter command to an untracked bare
browser operation.

## Capability contract

The current SourcePreflightCapabilities registry describes source-level
search/access/download/preflight behavior. It should not be replaced with
four publisher branches. A small, separate executor capability contract may
be added only after the command boundary is selected.

Candidate capabilities are:

    supports_navigation
    supports_page_observation
    supports_targeted_action
    supports_wait
    supports_tab_identity
    supports_download_artifact
    supports_authenticated_response_artifact
    supports_manual_resume

Do not expose supports_locator, supports_page_object, or supports_context_events
as the public contract. Those names would reproduce the Playwright object
graph. Capability checks should occur before execution and should be attached
to the adapter plan/operation, not hardcoded to the four current sources.

## Testing strategy for Phase B implementation

### Level 1 — Command/transport contract tests

Use a deterministic fake executor to verify:

- command schema, session/page/operation identity, and idempotency;
- navigation and page observation;
- target resolution, stale-reference rejection, and bounded action retry;
- wait and tab identity;
- DownloadObservation creation, artifact path restrictions, and missing-path
  fail-closed behavior;
- ChallengeObservation, pause, continuation, and disconnect semantics.

### Level 2 — Adapter plus fake command transport

Run CNKI, SpringerLink, ScienceDirect, and OxfordAcademic adapters against
scripted observations and artifacts, without the internet. Verify:

- adapter-first invocation;
- source-specific identity and metadata behavior;
- target locking and security verification;
- authorized download validation and SHA-256;
- manual handoff state transitions;
- Springer response-artifact capability rejection when absent.

### Level 3 — MCP executor integration

Use a controllable test page or mock MCP server/tool registry. Verify:

    Harness command -> exact MCP tool call -> structured observation

Cover navigation, snapshot/target revision, click, tab selection, download
artifact handoff, challenge pause, disconnect, and continuation. Do not use
publisher sites or browser_run_code_unsafe as the integration shortcut.

### Level 4 — Publisher smoke and acceptance

Only after all implementation gates pass, use the existing dedicated Research
Chrome profile for controlled CNKI, SpringerLink, ScienceDirect, and Oxford
Academic smoke tests. Authentication remains manual. These tests are not part
of this Phase B design turn.

## Migration plan and estimated scope

CompletedInV0217:

- src/hunnu_harness/browser/commands.py;
- src/hunnu_harness/browser/port.py;
- src/hunnu_harness/browser/local_executor.py;
- src/hunnu_harness/literature/institutional.py;
- four source adapters' browser-operation portions;
- command-layer, adapter-migration, and regression tests;
- version, Agent policy, and architecture documentation.

FilesLikelyToChangeForMCP (future implementation, not changed in v0.2.17):

- a new session broker and MCP/Agent executor integration module;
- a version-pinned MCP capability/configuration record;
- targeted MCP command/observation/artifact integration tests;
- documentation and capability contract tests.

AdaptersRequiringRefactor (v0.2.17 status):

- all four adapters' browser-operation portions are migrated to
  BrowserCommandPort;
- publisher search, result interpretation, identity lock, DOI/metadata,
  authorized-action policy, security rules, and file validation should remain
  in the adapters/workflow;
- the first refactor should replace direct page/locator/context access, not
  rewrite publisher business rules.

NewModulesImplementedInV0217:

- typed BrowserCommand and BrowserObservation models;
- BrowserCommandPort;
- LocalPlaywrightExecutor;
- Harness-owned DownloadArtifact model and result validation boundary.

NewModulesNeededForMCP:

- BrowserSessionBroker with operation/continuation state;
- MCP/Agent command executor or bridge.

ExistingModulesReusable:

- AdapterExecutionBroker and LiteratureAdapterFactory;
- SourcePreflightContract and dynamic capability registry;
- LiteratureAcquisitionWorkflow;
- Target Identity Lock, validators, manifests, audit, and SHA-256 logic;
- ManualDownloadHandoff after directory ownership is verified;
- OutputRoot path protections;
- security/manual-resume policy.

CompatibilityLayerNeeded=true

Compatibility policy:

- retain AdapterExecutionBroker and the current caller-facing literature API;
- retain the existing Python Playwright backend during migration;
- retain the current BrowserTransport only as a legacy/local compatibility
  surface while adapters move to BrowserCommandPort;
- deprecate direct adapter access to page, context, locator, and Playwright
  event objects;
- replace the MCP-facing boundary with command/observation and artifact
  contracts, not with fake Playwright classes.

EstimatedRefactorScope=medium/high, staged across at least two implementation
releases. The scope is driven by the current HTML parsing and download-event
dependencies, not by the adapter registry or Phase A broker.

## Version proposal

RecommendedNextImplementationVersion=v0.2.18

RecommendedImplementationPhases:

1. v0.2.17 — completed: define and test the command/observation contract; add
   a local Playwright command executor; migrate the four adapters; do not
   connect live MCP.
2. v0.2.18 — implement the MCP/Agent session broker and executor, including
   operation IDs, page identity, challenge continuation, and a verified
   DownloadObservation/artifact contract on a controllable test page.
3. v0.2.19 — run staged Research Chrome and publisher acceptance, beginning
   with one source at a time and only then the four-source dynamic-N route.

The v0.2.17 local command-layer items are implemented in the current working
tree; the MCP items remain design proposals.

## Open questions and architecture blockers

ArchitectureBlockers:

1. No verified MCP download event/artifact/local-path contract exists in the
   current observed tool surface.
2. The current MCP-to-Chrome profile ownership and reconnect/lease behavior is
   not formally verified.
3. The current tool surface does not provide a safe direct equivalent for
   page.content() or the Springer authenticated response-body artifact path.
4. The MCP package is configured as @playwright/mcp@latest rather than a
   tested, recorded version.
5. Snapshot target-reference lifetime and page identity across navigation,
   popup, disconnect, and resume are not yet specified.

OpenQuestions:

- Does the actual MCP server expose a stable download artifact path or event
  in a versioned protocol that is not visible in the current generic tool
  metadata?
- Can the MCP server expose a bounded, auditable content/structured-extraction
  result without unsafe arbitrary JavaScript?
- Is there a supported attach/lease mechanism, or must the MCP server remain
  the sole browser controller?
- How are multiple pages assigned to multiple planned sources, and how are
  page IDs preserved after reconnect?
- Can authenticated response artifacts be returned without exposing cookies,
  authorization headers, tokens, or unrestricted network access?
- What is the exact lifecycle and cleanup rule for MCP output files after
  Harness validation and manifest creation?

## Implementation gates

The current design audit records these gates:

| Gate | Status | Evidence or remaining condition |
| --- | --- | --- |
| G1_CurrentAdapterBrowserDependenciesMapped | PASS | Four adapters plus shared capture/handoff helpers were inspected |
| G2_MCPToolCapabilitiesConfirmed | PASS with version caveat | Current Codex MCP tool metadata was inspected; package pinning remains open |
| G3_ResearchChromeOwnershipConfirmed | FAIL / open | Config intent is known, but owner, lease, profile lock, and reconnect semantics are not verified |
| G4_DownloadPathBehaviorConfirmed | FAIL / blocker | OutputRoot configuration is known, but completion and controlled artifact mapping are not verified |
| G5_ManualResumeStateModelDefined | PASS (design) | Checkpoint, challenge observation, same-session resume, and fail-closed rules are specified |
| G6_RecommendedBoundarySelected | PASS (design) | Hybrid(B+D): command port plus Harness session broker |
| G7_NoFakePlaywrightObjectGraph | PASS (design) | Object emulation is explicitly rejected |
| G8_TestStrategyDefined | PASS (design) | Four test levels and no-live-publisher sequencing are specified |

MCP executor/session-broker implementation must not start until G3 and G4 are
resolved, and the content-observation and MCP-version questions are either
resolved or explicitly reflected in the selected adapter capabilities. This
restriction does not block the completed local command-layer work in v0.2.17.

## Final design decision

The current BrowserTransport is adequate only as a Python Playwright-shaped
Phase A/local-backend compatibility contract. It is not suitable as a direct
MCP contract because it leaks stateful Playwright objects and event semantics.

The recommended Phase B boundary is:

    Source Adapter
      -> typed BrowserCommandPort
      -> Harness-owned BrowserSessionBroker
      -> MCP command/observation executor
      -> dedicated Research Chrome / Playwright MCP

This keeps adapter-first enforcement in the Harness, keeps publisher logic in
the adapters, preserves the existing login-state policy, supports manual
resume, and gives Harness ownership of download validation. It does not claim
that the MCP bridge is implemented today.

PhaseBImplementationStarted=true (local command layer only)
BrowserCommandLayerImplemented=true
MCPExecutorImplemented=false
BrowserSessionBrokerImplemented=false
PlaywrightMCPTransportImplemented=false
LiveFourSiteSmokeTestReady=false


## Runtime verification update — 2026-08-19

A bounded runtime probe was performed after the v0.2.16 freeze. It used only
about:blank, data URLs, and a temporary localhost fixture. No publisher,
publisher login state, source adapter, or formal research artifact was used.

### G3 runtime facts

Current process evidence confirms:

    BrowserLaunchModel=Model 1
    WhoStartsBrowserProcess=Playwright MCP cli.js
    ResearchChromeProfilePath=C:\Users\71966\ResearchHarness\chrome-profile
    ActiveMCPCLI -> Chrome child with --user-data-dir=<Research profile>
    ChromeLaunchTransport=--remote-debugging-pipe

The active process tree at probe time was MCP CLI PID 26292 -> Chrome PID
18652. This is runtime evidence for the current launch model. The profile root
had no Singleton* entry at inspection time; this is not a safe concurrency
proof.

The first process scan found 12 Playwright MCP cli.js processes carrying the
same profile argument; a final non-interfering scan found 16. Only one had a
direct Chrome child in the corresponding snapshot. The remaining process
ownership is unverified and creates a material same-profile lease risk.

No MCP client reconnect, MCP server restart, or browser restart was attempted.
The current tool surface exposed tab indexes and an implicit current page, not
a stable page ID or durable CDP target ID. Explicit tabs.new/list/close worked
on a controlled page, but popup-event correlation was not verified.

Therefore:

    ConcurrentControllerAllowed=UNVERIFIED
    MCPClientReconnectTested=false
    PageIdentifierType=tab index plus observation-scoped snapshot refs
    PageIdentifierSurvivesReconnect=UNVERIFIED
    G3=false

### G4 runtime facts

The current callable MCP surface has no dedicated download/artifact tool. A
controlled click on a download link returned textual:

    Downloading file phase-b-gate-test.txt
    Downloaded file phase-b-gate-test.txt to <relative OutputRoot staging path>

The file resolved to the configured Windows staging directory, was readable by
Harness Python 3.12.13, and copied with the same SHA-256:

    FileSize=24
    FileSHA256=D618FE416682B6CC0C18D742398FA973A1021309BF501293F811801E2A146274
    ArtifactId=UNAVAILABLE
    StructuredDownloadObservation=UNAVAILABLE
    PathTranslationRequired=false

A localhost HTTP GET was listed by browser_network_requests and its response
body was returned by browser_network_request(part=response-body). This
verifies generic response-body inspection, not authenticated browser-context
request/cookie reuse.

Therefore:

    GenericDownloadArtifactContractVerified=true
    AuthenticatedResponseArtifactVerified=false
    FullHTMLObservationAvailable=false
    StructuredSnapshotAvailable=true
    G4=false

### Updated gate blockers

G3 remains blocked by missing reconnect semantics, missing durable page
identity, absent profile lease, and multiple same-profile MCP server
processes.

G4 remains blocked by the lack of a structured artifact ID/completion/path
contract and by the unverified Springer-style authenticated response-body
path. The generic click-to-file path is evidence for a future adapter, but it
is not enough to claim the full MCP backend contract is closed.

All probe files, temporary local server state, and temporary copied artifacts
were removed after verification. No production code was changed.
