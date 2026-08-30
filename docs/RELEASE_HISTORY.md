# Release history

A compact record of what each tagged release changed. Tags `v0.2.12` through
`v0.2.21` are the authoritative artifacts; `git show <tag>` gives the full
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

## Standing policy across all releases

No release has ever relaxed these, and none may:

- No CAPTCHA or challenge bypass, and no automated challenge interaction.
- No credential entry, cookie export, or MFA automation. Authentication is
  manual and human-performed.
- Licensed full text is acquired only through authorized, entitled routes.
- Research state and downloaded artifacts live under the Harness Output Root,
  never inside this repository.
