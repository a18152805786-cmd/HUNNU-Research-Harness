# HUNNU Research Harness v0.2.20 Freeze Review

## Release

- HarnessVersion: `0.2.20`
- ReleaseType: patch
- ParentCommit: `65c9bdcf0d96ec2a9d4dfff0075b4c79cc869c69`
- ReleaseTag: `v0.2.20`
- HistoricalTagPreserved: `v0.2.19`
- Branch: `agent/v0.2.18-freeze`

## Scope

This freeze contains the minimal CNKI compatibility correction for exact-title
refresh/result relock, including separation of URL/form query decoding from
bibliographic title identity, restricted title canonicalization, and exclusion
of hidden non-semantic title UI markers. It also records the institution-auth
classification fix already present in the working tree and synchronizes the
package/runtime/document version metadata to `0.2.20`.

Challenge policy, authentication policy, BrowserCommandPort/MCP observation,
and PDF download logic were not changed by the exact-title patch.

## Validation

- Version metadata tests: `2 passed`
- CNKI adapter file tests: `44 passed`
- CNKI-selected tests: `69 passed`
- Full test suite: `449 passed`
- `git diff --check`: passed
- Runtime package version: `0.2.20`
- Installed distribution version: `0.2.20`

## Research acquisition state at freeze

- Main manuscript: `C:\Users\<user>\Desktop\manuscript.docx`
- A-class citations: `16`
- Verified PDFs: `1`
- Reused existing PDFs: `1`
- New PDFs in this controlled run: `0`
- ExactTitleRelockVerified: `true`
- TargetResultOpened: `true`
- DetailPageIdentityVerified: `true`
- ControlledPDFDownloaded: `false`
- ControlledPDFValidated: `false`
- Blocking function: `CNKIAdapter.check_fulltext_access_html`
- Blocking reason: the HTML access classifier did not recognize the already-
  proven semantic institution-authentication header as institutional full-text
  entitlement and returned `FULLTEXT_NOT_AUTHORIZED` before `DownloadCommand`.

The acquisition report and manifests remain in the separate Harness Output
Root. No unauthorized access, CAPTCHA bypass, credential entry, cookie export,
or security-policy relaxation was performed.

## Commit boundary

The release commit stages only Harness source, tests, active version metadata,
current integration documentation, and this freeze review. The unrelated empty
untracked `.commandcode/taste/taste.md` file is intentionally not staged.
