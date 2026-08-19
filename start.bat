@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ------------------------------------------------------------
rem  The Cloudflare tunnel token is read from .env
rem  Never write it in this file - this file goes to a PUBLIC repo.
rem ------------------------------------------------------------
set "CFTOKEN="
if exist "%~dp0.env" (
  for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0.env") do (
    if /i "%%a"=="CLOUDFLARE_TUNNEL_TOKEN" set "CFTOKEN=%%b"
  )
)

echo [1/2] starting server ...
start "lawfinder-server" cmd /k ".venv\Scripts\activate && uvicorn main:app --host 0.0.0.0 --port 8000 --reload"
timeout /t 4 >nul

if not defined CFTOKEN (
  echo.
  echo  [SKIP] CLOUDFLARE_TUNNEL_TOKEN is not set in .env
  echo         Local address still works: http://127.0.0.1:8000
  echo         Run _0_fix_token.bat to move the token into .env
  echo.
  timeout /t 6 >nul
  exit /b 0
)

echo [2/2] starting tunnel ...
start "lawfinder-tunnel" /min cmd /k "cloudflared tunnel run --token %CFTOKEN%"
timeout /t 3 >nul

echo.
echo   https://law.gwiyomi-lab.com
echo.
timeout /t 3 >nul
