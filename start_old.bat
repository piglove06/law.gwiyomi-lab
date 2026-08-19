@echo off
REM ── 예전 버전 백업 (trycloudflare 임시 터널) ──────────────
REM 도메인 터널에 문제가 생겼을 때 임시로 쓰세요.
cd /d "%~dp0"
start "lawfinder-server" cmd /k ".venv\Scripts\activate && uvicorn main:app --host 0.0.0.0 --port 8000"
timeout /t 3
start "lawfinder-tunnel" cmd /k "cloudflared tunnel --url http://localhost:8000"
