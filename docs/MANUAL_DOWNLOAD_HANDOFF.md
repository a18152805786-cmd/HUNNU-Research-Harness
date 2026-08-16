# ManualDownloadHandoff (v0.2.6)

`ManualDownloadHandoff` is a reusable filesystem-only Human-in-the-loop primitive for an authorized file that is visible in Chrome Native PDF Viewer but whose native toolbar is outside the reliable Playwright page-DOM boundary.

## Preconditions

- `FullTextAccessible=true`
- `OfficialPdfActionConfirmed=true`
- `PDFViewerOpened=true`
- no Playwright download event produced a file
- no authorized PDF response body produced a file

## Flow

1. Resolve the existing browser-configured download directory without changing the Chrome profile.
2. Record a before snapshot containing filename, size, modification time, and optionally the SHA256 of existing PDFs.
3. Report `ACTION_REQUIRED_USER_DOWNLOAD=true` and let the user click the native Download control once.
4. Compare the after snapshot and consider only PDFs created or changed after arming.
5. Wait for temporary download files to disappear and require at least two stable size/mtime observations.
6. Reject zero-byte or non-`%PDF-` candidates; reject ambiguity when multiple valid PDFs remain.
7. Copy the unique candidate into controlled staging, preserving the original manual download and verifying the staging SHA256.
8. Let the source adapter perform local target-identity validation.
9. Pass the staged file to the existing `LiteratureDownloadManager` validator, SHA256, manifest, raw preservation, and normalized archive pipeline.

The primitive contains no Oxford metadata selectors, DOI logic, access parser, or finalizer. Oxford-specific access and identity remain in `OxfordAcademicAdapter`.

## Security boundary

```text
DownloadInitiationMode=USER_MANUAL_NATIVE_VIEWER_CLICK
FileFinalizationMode=HARNESS_AUTOMATIC
WindowsGUIFallback=false
NativePDFViewerAutomation=false
SignedURLReplay=false
AuthenticatedRequestReplay=false
CDPRawPdfCaptureImplemented=false
ExistingLoginStatePreserved=true
```

Snapshots and result artifacts contain local filenames, sizes, mtimes, and safe local paths only. They do not contain browser URLs, queries, cookies, authorization headers, tokens, or browser state.
