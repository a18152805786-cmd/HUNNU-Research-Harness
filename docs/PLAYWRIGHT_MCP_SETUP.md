# Playwright MCP setup

The intended Codex MCP registration is:

```powershell
codex mcp add playwright npx "@playwright/mcp@0.0.79"
```

After registration, verify with:

```powershell
codex mcp list
```

The Harness itself keeps browser lifecycle and download auditing local. MCP is the preferred Codex-facing control surface; the local Python backend is a fallback for repeatable profile and file-management operations.

For the v0.2.18 verified baseline, keep the MCP package explicitly pinned:

```text
HarnessBaseline=v0.2.18
PlaywrightMCPBaseline=0.0.79
```

Playwright MCP upgrades are explicit compatibility upgrades: change the
candidate version, run the controlled navigate/observe/click/download checks,
run the Harness regression suite, and accept the new baseline only if it is
compatible; otherwise restore the previous pinned version.

Never install a browser extension or accept an unexpected permission prompt automatically. If the extension mode requires a Chrome Web Store action, the user must perform that action manually.
