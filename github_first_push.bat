@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo  FIRST PUSH TO GITHUB   (run this only once)
echo ============================================================
echo.
echo  A GitHub sign-in window may open in your browser.
echo  Sign in as  piglove06  and click Allow / Authorize.
echo  Windows remembers it, so this is a one-time step.
echo.
echo  Press a key to start...
pause >nul

if not exist ".git" (
  echo.
  echo  [STOP] This folder is not a git repository yet.
  echo         Run  git_setup.bat  first, then run this file again.
  echo.
  pause
  exit /b 1
)

echo.
echo ------------------------------------------------------------
echo  Remote:
git remote -v
echo ------------------------------------------------------------
echo.

rem  If there is no commit yet, there is nothing to push and git will
rem  never ask you to sign in. This is the usual reason the GitHub
rem  login window never appears.
git rev-parse --verify HEAD >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [STOP] The repository has no commit yet - nothing to push.
  echo         That is why no GitHub sign-in window appeared.
  echo.
  echo         Run  git_setup.bat  again and check that it reaches
  echo         "[5/5] first commit ..." without stopping.
  echo.
  pause
  exit /b 1
)

rem Make sure the branch is named main.
git branch -M main

echo Attempt 1 - pushing ...
git push -u origin main 2>nul
if not errorlevel 1 goto :ok

echo.
echo  Push was rejected. This usually means the GitHub repo
echo  already has files (a README created with the repo).
echo  Merging them in, then trying again ...
echo.

git pull --rebase origin main
if errorlevel 1 (
  echo.
  echo  Rebase failed. Trying a plain merge instead ...
  git rebase --abort >nul 2>&1
  git pull --no-rebase --allow-unrelated-histories -m "chore: merge remote repo" origin main
  if errorlevel 1 goto :fail
)

echo.
echo Attempt 2 - pushing ...
git push -u origin main
if errorlevel 1 goto :fail

:ok
echo.
echo ------------------------------------------------------------
echo  SUCCESS.  Check it here:
echo    https://github.com/piglove06/law.gwiyomi-lab
echo.
echo  From now on, watch.bat commits and pushes automatically.
echo ------------------------------------------------------------
echo.
echo  Latest commits:
git log --oneline -5
echo.
pause
exit /b 0

:fail
echo.
echo ------------------------------------------------------------
echo  PUSH FAILED.  Copy this whole window and send it to Claude.
echo.
echo  Common causes:
echo    1) Sign-in window was cancelled  -^> run this file again
echo    2) Wrong repo URL - see the "Remote:" lines above
echo    3) No permission on that repo with this GitHub account
echo ------------------------------------------------------------
echo.
echo  Status:
git status --short
echo.
pause
exit /b 1
