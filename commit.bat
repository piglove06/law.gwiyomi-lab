@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem  commit.bat              -> auto message
rem  commit.bat "message"    -> your message
rem  Always runs the secret scan first, then commits and pushes.

if not exist ".git" (
  echo [STOP] Not a git repository. Run git_setup.bat first.
  pause
  exit /b 1
)

call :PY check_secrets.py
if errorlevel 1 (
  echo.
  echo [STOP] Secret scan failed. Nothing was committed.
  pause
  exit /b 1
)

git add -A
git diff --cached --quiet
if not errorlevel 1 (
  echo No changes.
  timeout /t 2 >nul
  exit /b 0
)

echo ------------------------------------------------------------
git diff --cached --stat
echo ------------------------------------------------------------

if not "%~1"=="" (
  set "MSG=%~1"
) else (
  for /f "tokens=1-3 delims=/- " %%a in ("%date%") do set "D=%%a-%%b-%%c"
  for /f "tokens=1-2 delims=:" %%a in ("%time%") do set "T=%%a:%%b"
  set "MSG=chore: save work !D! !T!"
)

git commit -m "!MSG!"
if errorlevel 1 (
  echo [FAIL] commit failed.
  pause
  exit /b 1
)

echo.
echo Pushing to GitHub ...
git push origin main
if errorlevel 1 (
  echo.
  echo [NOTE] Push failed. Commit is saved locally.
  echo        If this is the first push, run once:  git push -u origin main
)

echo.
git log --oneline -5
timeout /t 3 >nul
exit /b 0

:PY
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" %1
) else (
  python %1
)
exit /b %errorlevel%
