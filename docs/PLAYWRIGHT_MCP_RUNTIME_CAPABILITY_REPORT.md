# Phase B Gate Closure — G3/G4 Runtime Capability Verification

Verification date: 2026-08-19

This is a read-only runtime capability report. The probe used a controlled
data URL and a temporary localhost HTTP fixture only. It did not visit a
publisher, inspect authentication data, start a second browser controller,
restart Chrome, restart an MCP server, or change source code.

## Environment

    HarnessVersion=0.2.16
    GitBranch=main
    GitHead=550f44f75dadc45d32f1a4d1089b2a38ba45f132
    V016Tagged=true
    PlaywrightMCPTransportImplemented=false
    BrowserCommandPortImplemented=false
    BrowserSessionBrokerImplemented=false
    LiveFourSiteSmokeTestReady=false

The working tree contained only the pre-existing untracked architecture
document before this report was added. Production source and adapters were not
changed by the probe.

Evidence labels:

- VERIFIED_RUNTIME: observed from a current MCP call, process, or filesystem result;
- VERIFIED_CONFIG: read from current local configuration;
- VERIFIED_CODE: read from Harness source;
- INFERRED: constrained interpretation of verified evidence;
- UNVERIFIED: not safely tested in this gate-closure run.

## MCP package and tool surface

    MCPPackage=@playwright/mcp
    MCPVersion=0.0.79
    MCPVersionKnown=true
    MCPInstallSource=npx --yes @playwright/mcp@latest
    MCPExecutable=C:\Users\71966\AppData\Local\npm-cache\_npx\9833c18b2d85bc59\node_modules\@playwright\mcp\cli.js
    MCPConfiguration=C:\Users\71966\.codex\config.toml

MCPVersion is VERIFIED_RUNTIME from the package.json used by the running
cli.js. The configuration remains unpinned because it asks npx for
@playwright/mcp@latest; the current cached runtime resolved to 0.0.79.

The observed tool surface includes browser_navigate, browser_click,
browser_snapshot, browser_type, browser_fill_form, browser_wait_for,
browser_tabs, browser_network_requests, and browser_network_request. There is
no dedicated browser_download, wait_for_download, save_download, or artifact
resource tool in the current callable surface.

## G3 — browser and session ownership

### Process and launch evidence

    BrowserLaunchModel=Model 1 — MCP server starts browser
    BrowserProcessOwner=active Playwright MCP CLI process
    WhoStartsBrowserProcess=Playwright MCP cli.js
    ResearchChromeProfilePath=C:\Users\71966\ResearchHarness\chrome-profile
    MCPConnectionOwner=Codex MCP stdio/server lifecycle

The process scan found an active tree equivalent to:

    node.exe cli.js --browser chrome ... --user-data-dir <Research profile>
      -> chrome.exe --user-data-dir=<Research profile>
           --remote-debugging-pipe about:blank

The observed process IDs at verification time were MCP CLI PID 26292 and
Chrome PID 18652. These IDs are evidence for the process relationship at the
probe time only and are not stable identifiers.

This is VERIFIED_RUNTIME evidence for the active launch model. It rules out
the current active route being a Python Playwright attach to an independently
started Chrome process.

The first process scan found 12 Playwright MCP cli.js processes containing the
same Research profile argument; a final non-interfering scan found 16. Only
one had a direct Chrome child in the corresponding snapshots. The remaining
process ownership and whether they are stale, waiting, or separate Codex MCP
sessions are UNVERIFIED. This is a material concurrency/lease risk, not
evidence that concurrent control is safe.

    ProfileOwner=MCP-launched browser context for the active route; formal lease absent
    AuthenticatedSessionOwner=Research Chrome profile plus user's manual authentication policy
    WhoOwnsPageLifecycle=MCP server/tool session, represented by tab index/current page
    ProfileLockObserved=No Singleton* entry observed at the checked profile root
    ConcurrentControllerAllowed=UNVERIFIED
    NoConcurrentSecondControllerRequired=true (policy)

The absence of a Singleton* entry is not a safe-lock proof. The active Chrome
uses remote-debugging-pipe and the repository has no formal profile lease,
owner token, or cross-process admission check.

The current probe page was about:blank/data/localhost only. No publisher page
or publisher authentication state was inspected.

### Reconnect probe

    MCPClientReconnectTested=false
    MCPClientReconnectPreservesBrowser=UNVERIFIED
    MCPClientReconnectPreservesSession=UNVERIFIED
    MCPClientReconnectPreservesPages=UNVERIFIED
    MCPClientReconnectPreservesPageIdentity=UNVERIFIED

    MCPServerRestartTested=false
    MCPServerRestartPreservesBrowserProcess=UNVERIFIED
    MCPServerRestartPreservesSession=UNVERIFIED

    BrowserProcessRestartTested=false

No safe client reconnect/reinitialization control is exposed in the current
thread, and no MCP server or browser process was killed or restarted. Repeated
tool calls in the same live session are not a reconnect test. The process tree
shows current parent/child ownership, but it does not establish behavior after
disconnect or restart.

### Page identity and multiple pages

    StablePageIdentifierAvailable=false
    PageIdentifierType=tab index plus implicit current page; snapshot refs for elements
    PageIdentifierLifetime=tab index while server session is alive; snapshot refs are observation-scoped
    PageIdentifierSurvivesReconnect=UNVERIFIED

The runtime returned tab indexes and URLs/titles. It did not return a stable
page ID, CDP target ID, or durable page handle. Snapshot refs such as e4 and
f2e2 were usable for the immediately preceding snapshot, but are not a
page-level identity.

The controlled multi-page probe verified:

    CanEnumeratePages=true
    CanSelectSpecificPage=true (by tab index)
    CanDetectNewPage=true (by observing browser_tabs list after tabs.new)
    CanCloseSpecificPage=true (by tab index)
    PopupEventDetection=UNVERIFIED

The explicit browser_tabs new/list/select/close route works. A click on a
target=_blank data URL did not create a second tab in this probe, so automatic
popup detection and page-event correlation remain unverified.

### G3 decision

    BrowserOwnershipKnown=true
    ProfileOwnershipKnown=PARTIAL
    AuthenticatedSessionOwnershipKnown=PARTIAL
    MCPConnectionOwnershipKnown=PARTIAL
    ReconnectModelKnownEnough=false
    PageIdentityModelKnownEnough=false
    NoNeedForSecondConcurrentPythonController=true
    G3=false

G3 remains open. The active launch owner is known, but a broker cannot yet
reliably resume the same session/page after reconnect, and there is no formal
lease protecting the profile from the multiple MCP server processes observed.

## G4 — download artifact and path capability

### Controlled download probe

The first probe used a same-page data URL containing a small link with a
download filename. It was deliberately not a publisher action.

    DedicatedDownloadToolAvailable=false
    DownloadTriggeredByClick=true
    DownloadCompletionSignalAvailable=true
    DownloadCompletionSignalType=MCP textual Events: Downloading file / Downloaded file
    ReturnedFilename=phase-b-gate-test.txt
    ReturnedLocalPath=..\HUNNU-Research-Harness-Output\staging\playwright-output\phase-b-gate-test.txt
    ReturnedArtifactId=UNAVAILABLE
    ReturnedURI=UNAVAILABLE

The MCP click result contained both a downloading event and a downloaded
event, including a relative local path. No structured artifact object, artifact
ID, URI, MIME type, or SHA-256 was returned.

The path resolved on the Windows host to:

    DownloadDirectory=C:\Users\71966\Desktop\HUNNU-Research-Harness-Output\staging\playwright-output
    FileActuallyExists=true
    FileSize=24
    FileSHA256=D618FE416682B6CC0C18D742398FA973A1021309BF501293F811801E2A146274
    FileContent=phase-b-gate-download-v1

The Harness venv Python runtime read the same file:

    HarnessPython=C:\Users\71966\Desktop\HUNNU-Research-Harness\.venv\Scripts\python.exe
    HarnessPythonVersion=3.12.13
    HarnessCanReadDownloadedArtifact=true
    HarnessCanCopyArtifact=true
    ArtifactCopyPreservesHash=true
    HarnessCanMoveArtifact=UNTESTED
    PathStableAfterMCPCall=true (for this controlled probe)

The file was copied to a temporary external staging directory and the source
and copy hashes matched. The temporary copy and all probe files were removed
after verification.

### Filesystem mapping

    MCPFilesystemNamespace=Windows host filesystem
    HarnessFilesystemNamespace=Windows host filesystem
    PathTranslationRequired=false
    DownloadDirectoryConfigurable=true (VERIFIED_CONFIG)
    CanTargetHarnessStagingDirectly=true (current config and probe)

The current MCP process receives --output-dir pointing into Harness OutputRoot
staging, and the observed local path was readable by the Harness Python
runtime without WSL/container path translation.

This verifies current host path mapping for a generic file. It does not define
ownership, cleanup, collision, atomic-completion, or multi-session isolation.

### Completion and artifact identity

    ReliableDownloadCompletionSignal=true for the controlled simple download
    CompletionSignalType=server-generated textual event plus local file presence
    PartialFileRisk=UNVERIFIED
    DownloadReturnedLocalPath=true (textual relative path)
    DownloadReturnedArtifactRef=false

| Field | Result |
| --- | --- |
| artifact_id | UNAVAILABLE |
| suggested_filename | VERIFIED_RUNTIME |
| local_path | VERIFIED_RUNTIME as textual relative path, then resolved locally |
| mime_type | UNAVAILABLE |
| size | VERIFIED_RUNTIME from filesystem |
| sha256 | DERIVABLE by Harness; not returned by MCP |
| source_url | Not returned by download result |
| page_id | UNAVAILABLE; only current page/tab context |
| started_at/completed_at | Not returned as structured fields |

The simple probe verifies a generic click-to-file path, but not the typed
DownloadObservation contract required by the proposed broker.

A separate link from an opaque data origin to the localhost HTTP fixture
navigated to the text file instead of producing a download event. This was a
cross-origin data-page limitation and is not counted as proof of a same-origin
HTTP attachment/download contract.

### Authenticated response artifact

The tool surface exposes browser_network_requests and
browser_network_request with part=response-body. A local HTTP fixture verified
the generic path:

    GET http://127.0.0.1:8765/phase-b-gate-test.txt -> 200
    browser_network_request(index=1, part=response-body) -> phase-b-gate-download-v1

This is evidence that generic network response-body capture is available. It
is not an authenticated request test.

    AuthenticatedRequestAPIAvailable=UNVERIFIED
    AuthenticatedResponseBodyAvailable=UNVERIFIED
    GenericNetworkResponseBodyCapture=true
    CanReuseBrowserCookiesForRequest=UNVERIFIED
    AuthenticatedDownloadFromBrowserSession=UNVERIFIED

The current tool call observes an indexed response; it does not expose a formal
Harness command for an authenticated browser-context request, nor does it
prove cookie reuse or safe response-artifact export. SpringerLink's current
context.request.get/response.body path remains a Phase B blocker.

### G4 decision

    GenericDownloadArtifactContractVerified=true
    AuthenticatedResponseArtifactVerified=false
    DownloadCanBeTriggered=true
    DownloadCompletionObservable=true
    HarnessCanAccessArtifact=true
    ArtifactLocalPathOrEquivalentKnown=true
    ArtifactCanReachHarnessStaging=true
    ArtifactValidationPossible=true
    G4=false

G4 remains open for the full Phase B adapter boundary. The generic controlled
download succeeded end to end, but the current runtime provides only a
textual path/event result, not a stable structured artifact contract, and the
authenticated response-body/cookie reuse path remains unverified.

## Page content observation

    FullHTMLObservationAvailable=false (no safe dedicated HTML-content tool)
    StructuredSnapshotAvailable=true
    SnapshotStableEnoughForAdapterParsing=UNVERIFIED

browser_snapshot returned an accessibility tree with target refs and URLs.
The current safe tool surface did not return page.content() HTML. The
browser_evaluate and browser_run_code_unsafe tools could theoretically execute
JavaScript, but they are not accepted as a formal adapter transport or content
bridge in this gate.

The current adapter HTML-parsing dependency therefore remains unresolved for
Phase B and was not hidden behind the generic snapshot result.

## Gate result and next-step decision

    G3=false
    G4=false
    ProceedToV017=false

Remaining blockers:

1. define and verify one MCP/profile ownership lease and the policy for the
   multiple same-profile MCP server processes;
2. test MCP client reconnect without restarting the browser, then separately
   decide whether server restart is supported;
3. define a stable page/session identity beyond an implicit current page and
   tab index;
4. define a structured DownloadObservation/artifact handoff with completion,
   collision, cleanup, and controlled staging semantics;
5. decide whether Springer authenticated response acquisition is supported by a
   safe capability or is rejected for the MCP backend;
6. choose a safe, bounded page-content observation contract;
7. pin the MCP package version before relying on tool semantics.

No production implementation should begin under this gate result. The probe
does not authorize BrowserCommandPort, BrowserSessionBroker, or
PlaywrightMCPTransport implementation.
