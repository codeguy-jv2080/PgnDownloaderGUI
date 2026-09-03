@echo off
setlocal
cd /d "%~dp0"
set "PGN_NO_PAUSE="
if /i "%~1"=="--no-pause" set "PGN_NO_PAUSE=1"

if exist ".venv\Scripts\python.exe" goto install

py -3.13 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
    set "PGN_PYTHON_COMMAND=py -3.13"
    goto create_venv
)
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
    set "PGN_PYTHON_COMMAND=py -3"
    goto create_venv
)
python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
    set "PGN_PYTHON_COMMAND=python"
    goto create_venv
)
echo Python 3.10 or newer was not found. Python 3.13 is recommended.
echo Install Python for Windows from python.org, then run this setup again.
goto failed

:create_venv
echo Creating the local Python environment...
%PGN_PYTHON_COMMAND% -m venv ".venv"
if errorlevel 1 goto failed

:install
echo Installing PGN Downloader dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "requirements.txt"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, webview"
if errorlevel 1 goto failed
echo.
echo Setup complete. Double-click "Start PgnDownloaderGUI.vbs" to launch the app.
echo The native window uses Microsoft Edge WebView2 Runtime.
if not defined PGN_NO_PAUSE pause
exit /b 0

:failed
echo.
echo Setup did not complete. Review the error above, then run setup.bat again.
if not defined PGN_NO_PAUSE pause
exit /b 1
