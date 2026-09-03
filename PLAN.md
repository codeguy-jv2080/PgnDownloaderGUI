# Inspection and implementation plan

The plan below was shown in the conversation before implementation began.

## Inspected projects

- `PgnDownloaderGUI` was empty, with no Git metadata, app code, or user data.
- Sibling `pgn-downloader` was clean at commit `d133caef155244b12e04b225ad5e1a9321d9fc1c`.
- The backend is a small Python CLI: Lichess streams PGN; Chess.com downloads monthly JSON archives and selects standard chess games locally.
- Existing filters: provider, public username, color, game mode, and absolute/relative date ranges. The date parser uses local time and inclusive end-of-period dates.
- Existing persistence: exclusively created PGN export files. No database, preferences, import feature, or migration source exists in either inspected project.

## Findings

Requests lack timeouts. Chess.com lacks HTTP error checks, compares player names case-sensitively, and uses platform-default text encoding. Lichess's blank-line progress count is inaccurate and assumes nonempty chunks. Failed exports can leave partial output. The raw CLI imports a generated version module absent from the checkout. No redundant functionality needs removal.

Live API verification additionally found that Lichess requires an identifying application User-Agent for game exports, and Chess.com can redirect public archive requests. The adapter identifies the application, explicitly requests the correct response format, and follows only a bounded number of validated HTTPS redirects to the allowed chess hosts.

## Proposed structure and files

The approved task is to build the GUI in this folder. These new files implement the plan; the sibling repository is unchanged:

```text
main.py                           Desktop/headless entry point
Start PgnDownloaderGUI.vbs         Double-click launcher without a console
setup.bat, build.bat               Source setup and executable build
requirements.txt                  Runtime dependencies
requirements-dev.txt              Test/build dependencies
PgnDownloaderGUI.spec              Windows executable packaging
README.md, PLAN.md                 Usage and inspection record
THIRD_PARTY_NOTICES.md, LICENSE    Source provenance and licenses
.gitignore
app/__init__.py
app/paths.py                      Local paths and instance lock
app/database.py                   SQLite preferences, job journal, schema version
app/models.py                     Validated provider filters/output names
app/downloader.py                 Adapter around unchanged upstream functions
app/jobs.py                       Isolated workers, cancellation, output publication
app/server.py                     Local FastAPI and static files
app/desktop.py                    Native window and user-triggered file actions
app/selftest.py                   Offline frozen-app smoke test
web/index.html, styles.css, app.js Local interface, no remote assets
tests/test_app.py                  API/job/storage checks
tests/test_downloader.py           Focused adapter/filter tests added during implementation
vendor/pgn_downloader/             Four unchanged upstream Python modules
  __init__.py, lichess.py, chess_com.py, date_parser.py
  COPYING, UPSTREAM.md
```

Generated artifacts are isolated in `.venv`, `build`, `dist`, and `.test-data`. Packaging produces `dist/PgnDownloaderGUI/PgnDownloaderGUI.exe` with its companion `_internal` folder.

## Storage and migration

The app runs FastAPI on `127.0.0.1`, presents local HTML/CSS/JavaScript in a native Windows WebView2 window, and keeps preferences/history in `%LOCALAPPDATA%\PgnDownloaderGUI\app.sqlite3`. The downloader remains the backend. No cloud service, CDN, account, or hosted server is needed for the app. Downloading games necessarily contacts the selected chess site.

1. No migration is needed for this first version. Never scan, move, replace, or auto-import preexisting PGNs.
2. New PGNs go to the chosen output folder; an existing destination is always rejected.
3. Workers write unique `.pgn.part` files in the data folder. Completed files are copied to a unique temporary file on the destination volume and atomically published without replacing another file.
4. Cancellation/failure retains the staging file and marks history accordingly. On restart, unfinished jobs become `interrupted`; they do not restart or become completed automatically.
5. SQLite uses `PRAGMA user_version`. Before a future upgrade, take a consistent SQLite backup and add a transactional migration. Reject newer schemas rather than changing them. Do not place databases under the executable directory.
6. Preserve ordinary PGN export behavior. There was no import workflow to port.

Verification is background-only: mocked provider tests, actual subprocess lifecycle checks, an offline HTTP/executable smoke test, and static frontend checks. Native window rendering and file dialogs remain manual checks for the user.

## Lichess failure-handling correction

The captured HTTP 429 body specifically reported an occupied concurrent-export slot. The correction adds bounded automatic retries, accurate waiting messages and a cancelable countdown. It preserves the identifying request headers and existing provider download functions. Partial-file paths are shown only when those files actually exist.

Files updated: `app/downloader.py`, `app/jobs.py`, `app/database.py`, `web/app.js`, `web/index.html`, `web/styles.css`, `tests/test_app.py`, `tests/test_downloader.py`, `README.md`, this plan, `build.bat`, and `Start PgnDownloaderGUI.vbs`. New files: `tests/test_retries.py` and `package_app.py`.

SQLite schema version 2 adds `retry_at` and `attempt` to jobs and a provider cooldown table. An existing database is backed up before the transactional migration. All subsequent app updates must use the existing `dist/PgnDownloaderGUI` folder after checking the program is stopped. Versioned release folders and release-selection pointers are prohibited.

## Appearance and clearing controls

The existing web interface is retained with gray light surfaces, a complete dark theme, and slightly larger type. A header button saves the selected theme in the existing SQLite settings table; the initial page and desktop loading background use that theme. No schema migration is needed.

The form's Clear all control clears the username and unchecks game types. History has a separate confirmed clear action that removes journal rows only, blocks while a download is active, and preserves PGNs, partial files, preferences, and cooldowns.

Files modified: `web/index.html`, `web/styles.css`, `web/app.js`, `app/database.py`, `app/models.py`, `app/jobs.py`, `app/server.py`, `app/desktop.py`, `README.md`, and this plan. New verification files: `tests/test_preferences.py` and `tests/frontend_controls.cjs`. Checks use isolated local data and simulated requests; no live chess-site requests or desktop automation are part of this change.

## Enforced single-installation build policy

The user requires one fixed application installation. Before any rebuild/update, check whether the app is running. If it is stopped, proceed without a closure question. If it is running or locked, stop and ask the user to close it through ordinary chat or terminal text, never pop-up questions. Never build another application copy, versioned release folder, alternate executable, or additional launcher as a workaround.

`package_app.py`, `build.bat`, and the packaging specification enforce the fixed output path and running-app checks. The existing optional VBS launcher points directly to that same path and no longer selects among release folders. The global Codex `AGENTS.md` and this project's `AGENTS.md` persist this rule for all future work and agents. Packaging verification uses mocks; a build request never authorizes closing the running application.
