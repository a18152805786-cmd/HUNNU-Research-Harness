# Environment audit — HUNNU Research Harness v0.1

审计日期：2026-08-14

## Runtime

| Check | Result | Evidence |
|---|---|---|
| Windows | PASS | Windows 11 专业版, build 26200, 64-bit |
| NodeInstalled | true | Node v22.23.2 |
| NpmInstalled | true | npm 12.0.2 |
| NpxInstalled | true | npx 12.0.2; `npx.cmd` located under the Hermes Node runtime |
| PythonInstalled | true | Python 3.11.15 |
| GitInstalled | true | Git 2.54.0.windows.1 |
| CodexInstalled | true | Codex Windows app is installed; direct WindowsApps CLI invocation is blocked by the app-container permission boundary |
| ChromeInstalled | true | `C:\Program Files\Google\Chrome\Application\chrome.exe` |
| FDMDetected | true | Free Download Manager is installed and running |
| PlaywrightMCPAlreadyInstalled | false | Not present in global npm packages before this task |
| PlaywrightMCPInstalled | true | Frozen v0.2.18 baseline: `npx --yes @playwright/mcp@0.0.79 --version` returned 0.0.79 |
| PlaywrightMCPReachable | true | Local MCP server initialized over HTTP, returned `tools/list`, navigated to example.com, and emitted a download event |

## Existing browser and MCP state

- Chrome has multiple existing user profiles. The Harness does not use the daily profile by default.
- A new dedicated profile directory was created at `C:\Users\<user>\ResearchHarness\chrome-profile`.
- FDM is running and may intercept ordinary Chrome downloads. The Harness therefore uses a separate staging directory and recommends native browser downloads for the dedicated profile.
- Existing Codex configuration already contains `node_repl` and Zotero MCP servers. A Playwright MCP entry was added to the same config using the Hermes `npx.cmd` path, Chrome executable, dedicated profile, and a research output directory. No credentials, cookies, or storage state were exported.

## Safe test results

- Python unit tests: 5 passed.
- MCP initialize: HTTP 200 with an MCP session ID.
- MCP tool discovery: `tools/list` returned Playwright tools.
- Public-page smoke test: `https://example.com` opened and its accessibility snapshot exposed the page title and link.
- Download smoke test: an `httpbin` response with `Content-Disposition: attachment` produced `hunnu-harness-smoke.txt` and a Playwright download event. The test artifact is outside the Harness raw archive and contains no user data.

## v0.1 CNRDS end-to-end acceptance

- `HarnessV01CNRDSTestPassed=true`
- Manual school-account authentication was detected in CNRDS.
- `CNRDS → CNFS → 现金流量表` was confirmed through the page URL, title, and DOM.
- The minimal query used only stock `000001`, dates `2024-01-01` to `2024-12-31`, and fields `股票代码`, `统计日期`, `经营活动产生的现金流量净额`.
- CNRDS preview returned four 2024 records; the actual CSV download was captured by Playwright MCP and verified by the Harness Download Manager.
- The original compressed download was archived without overwrite, SHA-256 was computed, and a JSON manifest was created without credentials or session data.

## Limitations

- The CNRDS CNFS selectors and state guards have passed one small, user-supervised end-to-end validation run. Larger queries remain intentionally out of scope for v0.1 acceptance.
- Playwright Extension mode for attaching to an already-open Chrome tab is not installed or enabled. v0.1 uses the dedicated profile path; Extension mode remains a documented next step requiring manual Chrome extension installation if desired.
- No school authentication was attempted by automation.
