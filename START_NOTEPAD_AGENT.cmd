@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo ДРУЖИЩЕ — АГЕНТ БЛОКНОТ
echo 1. Записать заметку из Данные\Задания\TASK.txt
echo 2. Выполнить команду из Данные\Задания\COMMAND.json
echo 3. Прогнать доступные проверки команд
echo 4. Открыть каталог команд
echo 5. Открыть схему агента
echo 6. Проверить оконные команды с чистого запуска
echo 7. Открыть папку Данные
echo 8. Проверить печать в PDF
echo 9. Проверить поиск и замену
echo K. Проверить клавиатуру с памятью и Qwen
echo B. Проверить четыре команды буфера с памятью и Qwen
echo R. Повторить проблемные проверки из последнего отчёта
echo L. Стенд Qwen: исправить известный сбой и проверить правку
choice /C 123456789KBRL /N /M "Выбери 1–9, K, B, R или L: "
set "MENU_ITEM=%errorlevel%"
if "%MENU_ITEM%"=="1" call "%~dp0START_WRITE_NOTE.cmd"
if "%MENU_ITEM%"=="2" call "%~dp0START_COMMAND.cmd"
if "%MENU_ITEM%"=="3" call "%~dp0START_FULL_TEST.cmd"
if "%MENU_ITEM%"=="4" start "" "%~dp0COMMANDS.txt"
if "%MENU_ITEM%"=="5" start "" "%~dp0AGENT_ROADMAP.png"
if "%MENU_ITEM%"=="6" (
    call "%~dp0RUN_UIA.cmd" explore.py --clean-windows
    pause
)
if "%MENU_ITEM%"=="7" start "" "%~dp0Данные"
if "%MENU_ITEM%"=="8" (
    call "%~dp0RUN_UIA.cmd" explore.py --print-only
    pause
)
if "%MENU_ITEM%"=="9" (
    call "%~dp0RUN_UIA.cmd" explore.py --search-only
    pause
)
if "%MENU_ITEM%"=="10" (
    call "%~dp0RUN_UIA.cmd" explore.py --keyboard-only
    pause
)
if "%MENU_ITEM%"=="11" (
    call "%~dp0RUN_UIA.cmd" explore.py --clipboard-only
    pause
)
if "%MENU_ITEM%"=="12" (
    call "%~dp0RUN_UIA.cmd" explore.py --retry-only
    pause
)
if "%MENU_ITEM%"=="13" (
    setlocal
    set "PYTHONUTF8=1"
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" "%~dp0repair_clipboard.py"
    ) else (
        py -3.11 "%~dp0repair_clipboard.py"
    )
    endlocal
    pause
)
