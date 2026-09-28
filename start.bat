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

rem ------------------------------------------------------------
rem  Local LLM (llama.cpp llama-server). Ollama started by itself,
rem  llama-server does not - so start it here.
rem  -hf uses the already-downloaded file in the cache (no re-download).
rem ------------------------------------------------------------
set "LLAMA_DIR=C:\llama"
set "LLAMA_MODEL=unsloth/Qwen3.6-35B-A3B-GGUF:Q4_K_M"
set "LLAMA_ARGS=-hf %LLAMA_MODEL% -lm none --no-mmproj -np 1 -c 16384 -ctk q8_0 -ctv q8_0 -fa on -t 6 --jinja --port 8080"

rem ------------------------------------------------------------
rem  Log windows: logpipe.py adds Seoul time to each line, removes
rem  color codes, hides noise, and saves the full log to _runs\
rem  (llama-server prints elapsed time, cloudflared prints UTC).
rem ------------------------------------------------------------
set "PY=.venv\Scripts\python.exe"

netstat -ano | findstr /r /c:":8080 .*LISTENING" >nul
if not errorlevel 1 (
  echo [0/2] llama-server already running on :8080 - skip
) else if exist "%LLAMA_DIR%\llama-server.exe" (
  echo [0/2] starting llama-server ^(%LLAMA_MODEL%^) ...
  if exist "%PY%" (
    start "lawfinder-llm" /min cmd /k "%LLAMA_DIR%\llama-server.exe %LLAMA_ARGS% 2>&1 | %PY% -u logpipe.py llm"
  ) else (
    start "lawfinder-llm" /min cmd /k "%LLAMA_DIR%\llama-server.exe %LLAMA_ARGS%"
  )
) else (
  echo [0/2] [SKIP] %LLAMA_DIR%\llama-server.exe not found - AI answers will fail
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
rem  The token is passed as an environment variable (TUNNEL_TOKEN), not on the
rem  command line - otherwise it shows up in the window title.
set "TUNNEL_TOKEN=%CFTOKEN%"
if exist "%PY%" (
  start "lawfinder-tunnel" /min cmd /k "cloudflared tunnel run 2>&1 | %PY% -u logpipe.py tunnel"
) else (
  start "lawfinder-tunnel" /min cmd /k "cloudflared tunnel run"
)
set "TUNNEL_TOKEN="
timeout /t 3 >nul

echo.
echo   https://law.gwiyomi-lab.com
echo.
timeout /t 3 >nul
