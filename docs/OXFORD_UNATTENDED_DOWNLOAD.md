# Oxford unattended PDF download (v0.2.7)

## Mechanism

`ResearchChromePdfPreference` performs one targeted offline change to the existing dedicated Research Chrome profile:

```text
PreferenceName=plugins.always_open_pdf_externally
Scope=ResearchChromeOnly
```

The command refuses ordinary Chrome/Edge profile roots, an unexpected profile path, a running profile, profile lock files, or an unavailable running-process check. It changes only the PDF handling key and does not copy or snapshot the full Preferences file, clear browser data, change system policy, or create a new profile.

After the exact same profile is restarted, `OxfordAcademicAdapter` uses the verified HUNNU institutional route when required, confirms the article identity and official main-article PDF action, and clicks that action. Chrome downloads the PDF directly and Playwright emits a normal download event. The resulting completed local file enters the existing staging, validator, Target Identity Lock, SHA-256, manifest, and archive pipeline.

## Acceptance state

Fully unattended success requires all of the following:

```text
AcquisitionMethod=PLAYWRIGHT_DOWNLOAD_EVENT
AutomaticDownloadInitiation=true
AutomaticDownloadDetection=true
UserNativeViewerClickRequired=false
ManualDownloadHandoffUsed=false
OxfordUnattendedDownloadReady=true
```

`ManualDownloadHandoff` remains available for recovery, but its use always keeps `OxfordUnattendedDownloadReady=false`.

## Permanent safety boundaries

```text
ResearchChromeOnly=true
NormalUserChromeModified=false
SystemWideChromePolicyModified=false
WindowsGUIFallback=false
NativePDFViewerAutomation=false
SignedURLReplay=false
AuthenticatedRequestReplay=false
DefaultFullTextReadingMode=LocalFile
```
