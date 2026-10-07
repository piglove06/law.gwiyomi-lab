@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 5 - restart llama-server

rem ------------------------------------------------------------
rem  Restart ONLY llama-server (local LLM). Web server and tunnel are not touched.
rem  Use when llama-server died (e.g. CUDA out of memory).
rem  Keep these settings identical to start.bat.
rem ------------------------------------------------------------
set "LLAMA_DIR=C:\llama"
set "LLAMA_MODEL=unsloth/Qwen3.6-35B-A3B-GGUF:Q4_K_M"
set "LLAMA_ARGS=-hf %LLAMA_MODEL% -lm none --no-mmproj -np 1 -c 16384 -ctk q8_0 -ctv q8_0 -fa on -t 6 --jinja --cache-ram 2048 --port 8080"
set "PY=.venv\Scripts\python.exe"

netstat -ano | findstr /r /c:":8080 .*LISTENING" >nul
if not errorlevel 1 (
  echo llama-server is already running on :8080 - nothing to do
  timeout /t 5 >nul
  exit /b 0
)
if not exist "%LLAMA_DIR%\llama-server.exe" (
  echo [ERROR] %LLAMA_DIR%\llama-server.exe not found
  timeout /t 10 >nul
  exit /b 1
)
echo starting llama-server ^(%LLAMA_MODEL%^) ...
rem  llm_loop.bat restarts llama-server automatically if it crashes.
start "lawfinder-llm" /min cmd /k call llm_loop.bat
timeout /t 3 >nul
