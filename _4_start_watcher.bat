@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 4 - auto test watcher

echo ============================================================
echo  STEP 4 - Auto test watcher
echo ------------------------------------------------------------
echo  Runs the test suite whenever a source file changes,
echo  or when the file  _eval\RUN  appears.
echo  Then commits and pushes to GitHub automatically.
echo.
echo  Report is always written to  _eval\latest.md
echo  Leave this window open. Ctrl+C to stop.
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
