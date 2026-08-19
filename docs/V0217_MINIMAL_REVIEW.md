# HUNNU Research Harness v0.2.17 — Minimal Review

## A. Structure

| Check | Result | Evidence |
| --- | --- | --- |
| Browser Command Layer exists | Y | `browser/commands.py`, `browser/port.py` |
| Executor is local-only | Y | `LocalPlaywrightExecutor`; MCP executor is not implemented |
| Source adapters migrated | 4/4 | CNKI, SpringerLink, ScienceDirect, OxfordAcademic |
| Adapter page/locator/context direct access | N | Formal adapter and institutional-resolver paths use `BrowserCommandPort` |

## B. Key breakpoints

- Adapter → `BrowserCommandPort` is the formal entry. The factory/broker still
  accepts a v0.2.16 `BrowserTransport` only as a compatibility input and wraps
  it once.
- `LocalPlaywrightExecutor` is the only production landing point for Python
  Playwright operations. Adapters receive commands, observations, and
  `DownloadArtifact`, not Playwright objects.
- No formal Harness path falls back from a failed command to a raw page.

## C. Minimal behavior checks

The local contract tests cover `navigate`, `observe`, `click`, `download`,
artifact SHA-256, and local Springer-style `AuthenticatedFetch`. No publisher
or Research Chrome smoke test was run. Latest full regression: **391 passed**.

## D. Risk flags

- Publisher-specific executor branches: none.
- Playwright object graph: retained only inside the local executor and legacy
  compatibility helpers (`browser/transport.py`, `playwright_backend.py`,
  `authorized_file_capture.py`, and the legacy `CNKIChallengeDetector.inspect_page`
  entry point).
- MCP integration: not implemented and not claimed; Research Chrome was not
  touched.

## E. One-line conclusion

`v0.2.17` implements Adapter → command-layer → local Playwright decoupling;
MCP execution remains a separate gated v0.2.18 task.
