@echo off
rem ------------------------------------------------------------
rem  llm_loop.bat - keeps llama-server running.
rem  If llama-server exits (crash, out of memory, CUDA error), wait 20 s
rem  and start it again. Close this window to stop the loop.
rem  Started by start.bat / _5_restart_llm.bat with LLAMA_DIR, LLAMA_ARGS
rem  and PY already set (they are inherited from the parent window).
rem ------------------------------------------------------------
cd /d "%~dp0"
if not defined LLAMA_DIR set "LLAMA_DIR=C:\llama"
if not defined PY set "PY=.venv\Scripts\python.exe"
set /a LLM_RUNS=0

:loop
set /a LLM_RUNS+=1
echo [llm_loop] %date% %time% start llama-server (run %LLM_RUNS%)
if exist "%PY%" (
  "%LLAMA_DIR%\llama-server.exe" %LLAMA_ARGS% 2>&1 | "%PY%" -u logpipe.py llm
) else (
  "%LLAMA_DIR%\llama-server.exe" %LLAMA_ARGS%
)
echo [llm_loop] %date% %time% llama-server exited - restarting in 20 s (close this window to stop)
timeout /t 20 /nobreak >nul
goto loop
