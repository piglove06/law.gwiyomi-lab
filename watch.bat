@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Law Helper - auto test watcher

echo ============================================================
echo  Auto test watcher
echo ------------------------------------------------------------
echo  Runs the test suite whenever a source file changes,
echo  or when the file  _eval\RUN  appears.
echo  Report is always written to  _eval\latest.md
echo.
echo  * The server (start.bat) must be running.
echo  * Leave this window open. Ctrl+C to stop.
echo ============================================================
echo.

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" watch_and_test.py %*
) else (
  python watch_and_test.py %*
)

echo.
echo Watcher stopped.
pause
