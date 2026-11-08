@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "LAB_PY=.venv\Scripts\python.exe"
if not exist "%LAB_PY%" (
    py -3.11 -m venv .venv
    if errorlevel 1 exit /b 1
)
"%LAB_PY%" -c "import pywinauto" 2>nul
if errorlevel 1 (
    "%LAB_PY%" -m pip install pywinauto
    if errorlevel 1 exit /b 1
)
if /I "%~nx1"=="explore.py" set "DRUZHISHE_TEST_CONSOLE_TITLE=Druzhishe test %RANDOM%-%RANDOM%"
if defined DRUZHISHE_TEST_CONSOLE_TITLE title %DRUZHISHE_TEST_CONSOLE_TITLE%
"%LAB_PY%" "%~dp0%~1" %2
exit /b %errorlevel%
