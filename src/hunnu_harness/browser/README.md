# Browser layer

The v0.1 browser layer uses a dedicated persistent Playwright profile. It does not read cookies or export storage state. The `PlaywrightBrowser` backend can launch an installed Chrome executable with `accept_downloads=True`.

Connecting to an already-open daily Chrome tab is intentionally not automatic. The safe v0.1 path is either:

1. launch the dedicated profile and complete school authentication manually; or
2. explicitly start a dedicated Chrome instance with a remote-debugging endpoint, then configure `connect_over_cdp` in a future extension.

Do not point the harness at the user's daily profile by default.
