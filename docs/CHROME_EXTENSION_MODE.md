# Existing Chrome tab mode

v0.1 does not install a browser extension automatically. Playwright MCP exposes an `--extension` mode, but that mode requires the official Playwright extension to be installed and manually approved in Chrome.

When this mode is needed:

1. Open the dedicated research Chrome profile, not the daily profile.
2. Install/enable the official Playwright extension manually after checking its publisher and permissions.
3. Complete the HUNNU authentication manually.
4. Tell Codex that the session is ready.
5. Start the MCP server with `--extension` and verify the reported URL/title before any CNRDS action.

If the extension is not available, use the v0.1 persistent-profile mode. Never export cookies or storage state to make an existing tab portable.
