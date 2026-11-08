@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===============================================
echo DRUZHISHE NOTEPAD TRAINER - MEMORY TEST 0.3.9
echo ===============================================
echo.
where py >nul 2>&1
if %errorlevel%==0 (
  py -3.11 "%~dp0trainer.py" --memory-test
) else (
  python "%~dp0trainer.py" --memory-test
)
echo.
pause
