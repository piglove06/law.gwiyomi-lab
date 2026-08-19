@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 0c - repair git and push

echo ============================================================
echo  STEP 0c - Repair the tangled git state and push
echo ------------------------------------------------------------
echo  The earlier pull left a rebase half-finished, so the same
echo  conflict kept coming back. This backs up your folder first,
echo  cleans the git state, restores the files, then pushes.
echo ============================================================
echo.
pause

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" repair_git.py
) else (
  python repair_git.py
)

echo.
pause
