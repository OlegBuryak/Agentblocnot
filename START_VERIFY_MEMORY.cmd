@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
where py >nul 2>&1
if errorlevel 1 (
    python "%~dp0verify_persisted_memory.py"
) else (
    py -3.11 "%~dp0verify_persisted_memory.py"
)
set "TEST_RESULT=%errorlevel%"
pause
exit /b %TEST_RESULT%
