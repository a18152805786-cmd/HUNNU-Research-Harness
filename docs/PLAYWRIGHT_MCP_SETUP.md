# Playwright MCP setup

The intended Codex MCP registration is:

```powershell
codex mcp add playwright npx "@playwright/mcp@latest"
```

After registration, verify with:

```powershell
codex mcp list
```

The Harness itself keeps browser lifecycle and download auditing local. MCP is the preferred Codex-facing control surface; the local Python backend is a fallback for repeatable profile and file-management operations.

Never install a browser extension or accept an unexpected permission prompt automatically. If the extension mode requires a Chrome Web Store action, the user must perform that action manually.
