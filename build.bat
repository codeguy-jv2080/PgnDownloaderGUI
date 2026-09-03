@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto install
call "setup.bat" --no-pause
if errorlevel 1 goto failed

:install
".venv\Scripts\python.exe" "package_app.py" --check-only
if errorlevel 1 goto failed
echo Installing packaging dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "requirements-dev.txt"
if errorlevel 1 goto failed
echo Updating and verifying the existing Windows application...
".venv\Scripts\python.exe" "package_app.py"
if errorlevel 1 goto failed
echo.
echo Use the same executable: dist\PgnDownloaderGUI\PgnDownloaderGUI.exe
echo App data stays in the user's local app-data folder, outside this build.
pause
exit /b 0

:failed
echo.
echo Build did not complete. Review the error above, then run build.bat again.
pause
exit /b 1
