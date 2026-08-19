@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem  One cycle: commit -> run tests -> open report

echo [1/3] commit current changes ...
call commit.bat "chore: save before test run"

echo.
echo [2/3] running tests (1-3 min per scenario) ...
echo.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" eval_run.py %*
) else (
  python eval_run.py %*
)
set RC=%errorlevel%

echo.
echo [3/3] opening latest report ...
for /f "delims=" %%f in ('dir /b /o-d "_eval\report_*.md" 2^>nul') do (
  start "" "_eval\%%f"
  goto :done
)
echo   no report found.

:done
echo.
if %RC%==0 (echo All scenarios passed.) else (echo Some scenarios failed - see the report.)
pause
exit /b %RC%
