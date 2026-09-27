# PGN Downloader for Windows

A local Windows GUI for the existing **pgn-downloader** engine. Download public games from Lichess and Chess.com without entering commands.

## Launch

Double-click **dist\PgnDownloaderGUI\PgnDownloaderGUI.exe**. This is the fixed application path and remains the same for updates. The existing `Start PgnDownloaderGUI.vbs` is optional and points to that same executable; no release-selection pointer is used.

The packaged app includes Python and its dependencies. Keep the complete `PgnDownloaderGUI` distribution folder together, including `_internal`. Microsoft Edge WebView2 Runtime and .NET Framework 4.6.2 or newer must be available on Windows. The runtime preflight reports missing components without changing your system.

For source use, double-click `setup.bat` once with Python 3.10 or newer installed, then use the VBS launcher. Source setup downloads dependencies into the project's `.venv`; it does not install them globally. If a built executable exists, launch `.venv\Scripts\pythonw.exe main.py` from development tools to run edited source instead.

## Download games

1. Select **Lichess** or **Chess.com** and enter the public player username.
2. Choose white, black, or either color, and the desired game types.
3. Optionally enter dates: `2025`, `2025-06`, `2025-06-01`, or relative values such as `7d`, `3m`, `1y`, `12h`. Dates follow the original downloader's local-time rules. From includes the start of the period; Until includes its end. Blank dates cover all available games.
4. Choose an output folder with **Browse**, or enter an absolute local folder path. Leave the filename blank for the original naming convention (`lichess_username.pgn`, optionally `-white` or `-black`).
5. Select **Download games**. Follow progress, cancel if needed, and open the completed PGN or its folder. **History** records past attempts and can reuse their filters.

One download runs at a time. Existing files are never replaced: enter a different filename if one already exists. A repeated download does not append or deduplicate games. Chess.com retains the original standard-chess-only behavior, and correspondence is mapped to its daily mode. Lichess supports the original variant filters.

If a site returns HTTP 429, the job shows **Waiting for Lichess** or **Waiting for Chess.com**, explains the rejection, and displays a retry countdown. The app distinguishes an occupied concurrent-download slot from a general rate limit. It honors `Retry-After` when supplied and waits at least 60 seconds, increasing subsequent waits to 120, 240, then 300 seconds. Automatic retries stop after at most one hour of scheduled waiting or 15 retries. A longer server-mandated delay is preserved without making an early request.

**Cancel** remains available while waiting. Known cooldowns persist across cancelling a job or restarting the app, so starting again cannot immediately repeat a rejected request. A successful download clears its provider's cooldown. Public-game downloads do not require a sign-in or token. The app cannot clear a concurrency counter on the chess site's server.

Large accounts may take time. No percent estimate is invented because the upstream APIs do not provide a reliable total. Chess.com reports games while downloading; Lichess reports bytes and counts games on completion. Zero matching games is a successful empty PGN export.

## Appearance and clearing

The **Dark mode / Light mode** button at the top of every page switches between a charcoal theme and a softer gray light theme. The choice is saved in the local SQLite settings and applied on the next launch. Labels, controls, and supporting text are slightly larger for easier reading.

**Clear all** at the top of the download form clears the username and unchecks game types on both platforms. The other download options stay as entered. It does not start or cancel a download.

**Clear history** on the History page removes all history entries after confirmation. Saved PGNs and partial files stay on disk, and preferences and known server cooldowns are retained. This button is unavailable during an active download; finish or cancel that download first.

## Local data and recovery

- Settings and history: `%LOCALAPPDATA%\PgnDownloaderGUI\app.sqlite3`.
- Default PGN folder: `%USERPROFILE%\Downloads\PGN Downloads`, changeable in the app.
- Unfinished downloads: `%LOCALAPPDATA%\PgnDownloaderGUI\partials`.
- Desktop logs: `%LOCALAPPDATA%\PgnDownloaderGUI\app.log`; startup errors: `startup-error.log`.

The app's window, assets, server, and storage are local. Internet access is needed only to obtain games from the selected chess site. There is no cloud storage, telemetry, CDN, or application account.

Closing the app cancels any active download, including a waiting retry. Failed/cancelled jobs retain their partial file for recovery when one actually exists, and do not publish it as a completed PGN. A forced stop is recorded as **Interrupted** at the next launch. Automatic HTTP 429 retries repeat the rejected request before reading its body; they do not restart or append a partially downloaded stream. Use a new download to retry a broken stream. Existing PGN files elsewhere are left untouched.

To back up, close the app, copy the data folder, and separately copy any PGN output folders you use. There was no initial migration: both inspected projects had no database or GUI storage. Schema version 2 adds retry timing and cooldown records; before upgrading an existing version-1 database, the app creates `app-before-v2-<timestamp>.sqlite3`. The migration runs in a transaction and preserves existing history and preferences. Newer database schema versions are rejected. See `PLAN.md`.

## Development and verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe main.py --self-test --data-dir .test-data\source-smoke --report .test-data\source-report.json
```

Use a fresh self-test data directory/report each time. The smoke test starts a real loopback server, authenticates API calls, spawns an offline fixture worker, checks UTF-8 output and overwrite protection, and verifies SQLite persistence. It does not open a window or contact chess sites.

For a developer-only headless local server:

```powershell
.\.venv\Scripts\python.exe main.py --serve
```

It prints a loopback URL with a temporary session token. The normal native launcher handles this internally. Native Browse/Open actions are available only in the desktop window.

Updates always use **dist\PgnDownloaderGUI** in place. `build.bat` and `package_app.py` first check whether PGN Downloader is running. If it is stopped, the build proceeds without asking. If it is running or its files are locked, the build stops and asks the user to close it through ordinary terminal text; it never closes the app itself or creates another release folder or alternate executable.

The build preserves the existing launch path, updates the packaged files in the same folder, and runs an offline executable self-test with isolated data. It does not terminate the user's app. Compiler intermediates remain under `build`. Do not invoke PyInstaller directly to bypass the guarded build entry point. The standing rule is recorded in this project's `AGENTS.md` and the user's global Codex instructions.

Background tests cover provider behavior, validation, worker success/failure/cancellation, crash recovery, single-instance protection, and destination collisions. The native window and native folder/file actions require a manual check; automated desktop control is not part of this project's verification.

Live requests to Lichess require the user's separate explicit approval. Appearance and clearing checks use simulated responses and isolated local data only.

## Project layout

`web` holds the local HTML/CSS/JavaScript. `app/server.py` exposes FastAPI, `database.py` stores settings/history in SQLite, `jobs.py` owns worker processes and safe file publication, and `downloader.py` adapts the original engine. `desktop.py` owns the native window. `vendor/pgn_downloader` contains unchanged source copied from the inspected local repository; the sibling checkout is not a runtime dependency.

The upstream source commit, license, and adapter changes are documented in `vendor/pgn_downloader/UPSTREAM.md` and `THIRD_PARTY_NOTICES.md`.

Implementation references: [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/), [FastAPI static files](https://fastapi.tiangolo.com/tutorial/static-files/), [pywebview documentation](https://pywebview.flowrl.com/guide/).

## Windows security

The packaged Windows executable is not code-signed. Windows SmartScreen or
another reputation-based security prompt may therefore appear on first launch.

## License

PGN Downloader GUI is licensed under GPL-3.0-or-later. See `LICENSE` and
`THIRD_PARTY_NOTICES.md`. The bundled upstream downloader and other third-party
components retain their own applicable notices and license terms.
