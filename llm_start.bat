@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem ------------------------------------------------------------
rem  llm_start.bat - start llama-server (local LLM) if it is not running.
rem  Called by start.bat and by ai_client.restart_local_llm (auto restart).
rem  The only place for LLM settings. History: CHANGELOG.md, notes: CODE_NOTES.md
rem ------------------------------------------------------------
set "LLAMA_DIR=C:\llama"
set "LLAMA_MODEL=unsloth/Qwen3.6-35B-A3B-GGUF:Q4_K_M"
rem  --slot-save-path must stay relative: an absolute path with Korean letters stops llama-server from starting.
set "LLAMA_ARGS=-hf %LLAMA_MODEL% -lm none --no-mmproj -np 1 -c 32768 -ctk q8_0 -ctv q8_0 -fa on -t 6 --jinja --cache-ram 2048 --slot-save-path _cache\slots --port 8080 -fit off -ngl 99 -ncmoe 26"
set "PY=.venv\Scripts\python.exe"
rem  Pinned host RAM for CPU-side experts (~13 GB) fails with "CUDA error: out of memory" on this PC.
set "GGML_CUDA_NO_PINNED=1"

rem  Port 8080 opens only after the model is loaded (~1-3 min), so check the process too.
set "LLM_UP="
netstat -ano | findstr /r /c:":8080 .*LISTENING" >nul && set "LLM_UP=1"
tasklist /FI "IMAGENAME eq llama-server.exe" 2>nul | find /i "llama-server.exe" >nul && set "LLM_UP=1"
if defined LLM_UP (
  echo [LLM] llama-server already running or loading - skip
  exit /b 0
)
if not exist "%LLAMA_DIR%\llama-server.exe" (
  echo [LLM] [ERROR] %LLAMA_DIR%\llama-server.exe not found - AI answers will fail
  exit /b 1
)
echo [LLM] starting llama-server ^(%LLAMA_MODEL%^) ...
rem  llm_loop.bat restarts llama-server if it exits (crash, out of memory).
start "lawfinder-llm" /min cmd /k call llm_loop.bat
exit /b 0
