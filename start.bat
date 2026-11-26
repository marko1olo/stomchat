@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "STOMCHAT_CONSOLE_LOG=1"
set "PYTHON_CMD=python"
set "SUPERVISOR_LOG=%~dp0bot_supervisor.log"
where python >nul 2>nul || set "PYTHON_CMD=py -3"

if /I "%STOMCHAT_DRY_RUN%"=="1" (
    echo %DATE% %TIME% - start.bat syntax OK.
    exit /b 0
)

:loop
echo %DATE% %TIME% - Starting stomat bot...
>> "%SUPERVISOR_LOG%" echo %DATE% %TIME% - Starting stomat bot...
%PYTHON_CMD% -X utf8 -u "%~dp0main.py"
set "BOT_EXIT=%ERRORLEVEL%"
if "%BOT_EXIT%"=="75" (
    echo %DATE% %TIME% - Another bot instance is already running (code 75). Supervisor exiting cleanly.
    >> "%SUPERVISOR_LOG%" echo %DATE% %TIME% - Another bot instance is already running (code 75). Supervisor exiting cleanly.
    exit /b 0
)
echo %DATE% %TIME% - Bot stopped with code %BOT_EXIT%. Restart in 5 seconds...
>> "%SUPERVISOR_LOG%" echo %DATE% %TIME% - Bot stopped with code %BOT_EXIT%. Restart in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
