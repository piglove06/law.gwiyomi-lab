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
rem  v1.49 (2026-10-30) --slot-save-path : kv_reuse.py saves/restores the fixed prompt-prefix state there
rem    (answer/clarify instructions, ~3k tokens). Remove it to turn that off (kv_reuse skips by itself).
rem    Relative path on purpose: an absolute path with Korean letters made llama-server fail to start
rem    ("No mapping for the Unicode character ... code page", 2026-10-30 22:33). Runs from this folder.
set "LLAMA_ARGS=-hf %LLAMA_MODEL% -lm none --no-mmproj -np 1 -c 16384 -ctk q8_0 -ctv q8_0 -fa on -t 6 --jinja --cache-ram 2048 --slot-save-path _cache\slots --port 8080"
set "PY=.venv\Scripts\python.exe"

rem  2026-10-15 - check the process too, not only the port. While the model is loading (~3 min)
rem  port 8080 is not open yet, so running 3 right after 5 started a SECOND llama-server (RAM 95%%).
set "LLM_UP="
netstat -ano | findstr /r /c:":8080 .*LISTENING" >nul && set "LLM_UP=1"
tasklist /FI "IMAGENAME eq llama-server.exe" 2>nul | find /i "llama-server.exe" >nul && set "LLM_UP=1"
if defined LLM_UP (
  echo llama-server is already running or still loading - nothing to do
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
