@echo off
chcp 65001 >nul
cd /d "%~dp0"
title lawfinder - start
rem ------------------------------------------------------------
rem  start.bat - starts everything. Double-click this one file.
rem    1 LLM (llm_start.bat)  2 web server :8000  3 Cloudflare tunnel  4 watcher (auto test + commit)
rem  Already running parts are skipped (also an old "_4" watcher window), so running it again is safe.
rem  start.bat nowatch  -> everything except the watcher
rem  History: CHANGELOG.md, notes: CODE_NOTES.md
rem ------------------------------------------------------------
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/4] LLM
call llm_start.bat
cd /d "%~dp0"

echo [2/4] web server
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul
if not errorlevel 1 (
  echo       already running on :8000 - skip
) else (
  start "lawfinder-server" cmd /k ".venv\Scripts\activate && uvicorn main:app --host 0.0.0.0 --port 8000 --reload"
  timeout /t 4 >nul
)

echo [3/4] tunnel
rem  The tunnel token is read from .env. Never write it here - this file goes to a PUBLIC repo.
set "CFTOKEN="
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="CLOUDFLARE_TUNNEL_TOKEN" set "CFTOKEN=%%b"
  )
)
tasklist /FI "IMAGENAME eq cloudflared.exe" 2>nul | find /i "cloudflared.exe" >nul
if not errorlevel 1 (
  echo       already running - skip
) else if not defined CFTOKEN (
  echo       [SKIP] CLOUDFLARE_TUNNEL_TOKEN is not set in .env - local address still works: http://127.0.0.1:8000
) else (
  rem  Passed as TUNNEL_TOKEN env var, not on the command line - otherwise it shows in the window title.
  set "TUNNEL_TOKEN=%CFTOKEN%"
  start "lawfinder-tunnel" /min cmd /k "cloudflared tunnel run 2>&1 | %PY% -u logpipe.py tunnel"
)
set "TUNNEL_TOKEN="
set "CFTOKEN="

echo [4/4] watcher
if /i "%~1"=="nowatch" (
  echo       skipped ^(nowatch^)
) else (
  tasklist /V /FI "IMAGENAME eq cmd.exe" 2>nul | find /i "watcher" >nul
  if not errorlevel 1 (
    echo       already running - skip
  ) else (
    start "lawfinder-watcher" cmd /k "%PY% watch_and_test.py"
  )
)

echo.
echo   https://law.gwiyomi-lab.com   ^(LLM needs 1-3 min to load^)
echo.
timeout /t 5 >nul
