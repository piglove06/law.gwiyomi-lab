@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 5 - restart llama-server

rem ------------------------------------------------------------
rem  llama-server(로컬 LLM)만 다시 켭니다. 웹 서버·터널은 건드리지 않습니다.
rem  llama-server 가 GPU 메모리 부족(CUDA out of memory) 등으로 꺼졌을 때 씁니다.
rem  아래 설정은 start.bat 과 같아야 합니다 (바꾸면 둘 다 바꾸세요).
rem ------------------------------------------------------------
set "LLAMA_DIR=C:\llama"
set "LLAMA_MODEL=unsloth/Qwen3.6-35B-A3B-GGUF:Q4_K_M"
set "LLAMA_ARGS=-hf %LLAMA_MODEL% -lm none --no-mmproj -np 1 -c 16384 -ctk q8_0 -ctv q8_0 -fa on -t 6 --jinja --port 8080"
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
if exist "%PY%" (
  start "lawfinder-llm" /min cmd /k "%LLAMA_DIR%\llama-server.exe %LLAMA_ARGS% 2>&1 | %PY% -u logpipe.py llm"
) else (
  start "lawfinder-llm" /min cmd /k "%LLAMA_DIR%\llama-server.exe %LLAMA_ARGS%"
)
timeout /t 3 >nul
