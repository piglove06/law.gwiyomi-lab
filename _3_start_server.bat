@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 3 - start server

echo ============================================================
echo  STEP 3 - Start the server (and the tunnel)
echo ------------------------------------------------------------
echo  Two new windows will open. Leave them running.
echo  Local :  http://127.0.0.1:8000
echo ============================================================
echo.

call start.bat
