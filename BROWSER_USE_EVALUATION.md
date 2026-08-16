# Browser Use fallback evaluation

v0.1 does not add Browser Use by default. Playwright/MCP is the primary control surface because the Harness needs URL, title, DOM, accessible labels, and download events rather than screenshot-only decisions. Browser Use should be considered only if a specific CNRDS dynamic component cannot be operated with stable DOM or accessibility locators after a concrete test.

Before adding a second browser framework, record the failing Playwright interaction, the required extra dependency, credential/API-key implications, and why the fallback is worth the added complexity.
