@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 1 - git setup

set REPO=https://github.com/piglove06/law.gwiyomi-lab.git

echo ============================================================
echo  STEP 1 - Create the local git repository
echo ============================================================
echo.

where git >nul 2>&1
if errorlevel 1 (
  echo  [STOP] git is not installed.
  echo         https://git-scm.com/download/win
  echo.
  pause
  exit /b 1
)

if not exist ".gitignore" (
  echo  [STOP] .gitignore is missing - .env could be committed.
  echo.
  pause
  exit /b 1
)

if exist ".git" (
  echo Repository already exists. Skipping init.
) else (
  echo [1/5] git init ...
  git init -b main
  if errorlevel 1 goto :fail
)

echo [2/5] local config ...
git config user.name  "piglove06"
git config user.email "piglove06@users.noreply.github.com"
git config core.autocrlf false
git config core.quotepath false

echo [3/5] remote ...
git remote remove origin >nul 2>&1
git remote add origin %REPO%
git remote -v

echo [4/5] secret scan ...
call :PY check_secrets.py
if errorlevel 1 (
  echo.
  echo  [STOP] Secret scan failed. Nothing was committed.
  echo         Fix the items listed above, then run this again.
  echo.
  pause
  exit /b 1
)

echo [5/5] commit ...
git add -A
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "chore: initial import - v1.28"
) else (
  echo   nothing new to commit
)

echo.
echo ------------------------------------------------------------
echo  DONE.  Next:  _2_connect_github.bat
echo ------------------------------------------------------------
echo.
git status --short
echo.
pause
exit /b 0

:PY
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" %1
) else (
  python %1
)
exit /b %errorlevel%

:fail
echo.
echo  [FAIL] See the error above.
pause
exit /b 1
