@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 0b - finish the interrupted merge

echo ============================================================
echo  STEP 0b - Finish the interrupted GitHub merge
echo ------------------------------------------------------------
echo  Fixing the file is not enough - git also has to be told
echo  the conflict is resolved. This does that, then pushes.
echo ============================================================
echo.

if not exist ".git" (
  echo  [STOP] Not a git repository.
  pause
  exit /b 1
)

echo [1/5] resolving leftover conflicts ...
call :PY resolve_merge.py
if errorlevel 1 (
  echo.
  echo  [STOP] Could not resolve everything. Send the list above to Claude.
  pause
  exit /b 1
)

echo.
echo [2/5] secret scan ...
call :PY check_secrets.py
if errorlevel 1 (
  echo.
  echo  [STOP] Secret scan failed. Nothing was committed.
  pause
  exit /b 1
)

echo.
echo [3/5] committing ...
git add -A
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "chore: merge GitHub repo and move tunnel token out of start.bat"
) else (
  echo    nothing new to commit
)
if exist ".git\rebase-merge" git rebase --continue
if exist ".git\rebase-apply" git rebase --continue

echo.
echo [4/5] making sure the token is not in start.bat ...
findstr /m /c:"--token ey" start.bat >nul 2>&1
if not errorlevel 1 (
  echo.
  echo  [STOP] start.bat still contains the tunnel token. Not pushing.
  pause
  exit /b 1
)
echo    clean.

echo.
echo [5/5] pushing ...
git push -u origin main
if errorlevel 1 (
  echo.
  echo ------------------------------------------------------------
  echo  PUSH FAILED - copy this window and send it to Claude.
  echo ------------------------------------------------------------
  git status --short
  echo.
  pause
  exit /b 1
)

echo.
echo ------------------------------------------------------------
echo  SUCCESS.   https://github.com/piglove06/law.gwiyomi-lab
echo.
echo  From now on you only need:
echo     _3_start_server.bat     (server + tunnel)
echo     _4_start_watcher.bat    (auto test + auto commit/push)
echo ------------------------------------------------------------
echo.
git log --oneline -6
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
