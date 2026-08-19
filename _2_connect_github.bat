@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 2 - first push to github

echo ============================================================
echo  STEP 2 - Push to GitHub  (only needed once)
echo ------------------------------------------------------------
echo  A GitHub sign-in window will open in your browser.
echo  Sign in as  piglove06  and click Authorize.
echo ============================================================
echo.

if not exist ".git" (
  echo  [STOP] Not a git repository. Run _1_setup_git.bat first.
  echo.
  pause
  exit /b 1
)

git rev-parse --verify HEAD >nul 2>&1
if errorlevel 1 (
  echo  [STOP] No commit yet - nothing to push.
  echo         That is why no sign-in window appears.
  echo         Run _1_setup_git.bat and check it reaches "[5/5] commit".
  echo.
  pause
  exit /b 1
)

echo Remote:
git remote -v
echo.

git branch -M main

echo Pushing (attempt 1) ...
git push -u origin main 2>nul
if not errorlevel 1 goto :ok

echo.
echo  Rejected - the GitHub repo probably already has a README.
echo  Merging it in, then retrying ...
echo.
git pull --rebase origin main
if errorlevel 1 (
  git rebase --abort >nul 2>&1
  git pull --no-rebase --allow-unrelated-histories -m "chore: merge remote" origin main
  if errorlevel 1 goto :fail
)

echo.
echo Pushing (attempt 2) ...
git push -u origin main
if errorlevel 1 goto :fail

:ok
echo.
echo ------------------------------------------------------------
echo  SUCCESS.  https://github.com/piglove06/law.gwiyomi-lab
echo.
echo  From now on _4_start_watcher.bat pushes automatically.
echo ------------------------------------------------------------
echo.
git log --oneline -5
echo.
pause
exit /b 0

:fail
echo.
echo ------------------------------------------------------------
echo  PUSH FAILED. Copy this whole window and send it to Claude.
echo ------------------------------------------------------------
git status --short
echo.
pause
exit /b 1
