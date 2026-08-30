# Release history

A compact record of what each tagged release changed. Tags `v0.2.12` through
`v0.2.22` are the authoritative artifacts; `git show <tag>` gives the full
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
| v0.2.22 | 2026-08-30 | Post-acquisition topic filing closes its loop, and a ScienceDirect acquisition completes end to end for the first time. Topic assignments now carry provenance: automatic, human-confirmed, or unknown for the corpus that predates the record — absence is never read as an automatic decision. `library-confirm-topics` gives `REVIEW_REQUIRED` a formal exit, accepting only what classification proposed for that work and re-checking the frozen taxonomy regardless. The research browser stays alive between runs, so an institutional sign-in survives more than one command; a run attaches to it rather than launching its own, and closing means letting go of the connection. Downloads through an attached browser are caught from the browser's own account of them — source host, paper identifier and completion all validated — because Playwright's download event does not reach an attached page. ScienceDirect pages are read once they have said what they are: search results and the PDF control both render after `domcontentloaded`, and reading earlier reported a paper as absent and an entitled article as paywalled. Corpus resealed at 181 works. |

## Standing policy across all releases

No release has ever relaxed these, and none may:

- No CAPTCHA or challenge bypass, and no automated challenge interaction.
- No credential entry, cookie export, or MFA automation. Authentication is
  manual and human-performed.
- Licensed full text is acquired only through authorized, entitled routes.
- Research state and downloaded artifacts live under the Harness Output Root,
  never inside this repository.
