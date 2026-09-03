# PGN Downloader project rules

## One application installation

- Before rebuilding or updating, check whether the app is running. If it is stopped, proceed without asking. If it is running, ask the user to close it and wait.
- Update only `dist/PgnDownloaderGUI/PgnDownloaderGUI.exe` and its supporting files in that same application folder. Preserve that launch path.
- Never create versioned or timestamped release folders, alternate executable names, duplicate application distributions, release-selection pointers, or additional launchers.
- Do not create new or duplicate `.vbs` launchers unless the user explicitly requests and authorizes one. Normal updates use the existing `.exe` and launch path.
- If the app is still running or a file is locked, stop and ask the user to close it. Never kill the app or build a second copy elsewhere.
- Use normal chat or terminal text for closure requests, never pop-up question tools. Do not repeat a closure question after the app is verified stopped or the user has confirmed it.
- Use the guarded `package_app.py` build entry point. Do not invoke PyInstaller directly to bypass its running-app checks.
- Compiler intermediates may use the existing `build` cache. They are not alternate application distributions and must not be presented as launch targets.
- Existing historical release copies are not permission to create more. Consolidation or deletion must respect the user's approval and avoid touching user data.

## Verification

- Do not send live test requests to Lichess or Chess.com without separate explicit user approval. Use mocked provider responses and isolated local test data.
- Do not open or control the desktop, browser, or app windows for verification. Use background commands and offline tests.
- Preserve PGNs, partial files, SQLite settings/history, and other user data during application updates.

These instructions apply to every agent working in this project.
