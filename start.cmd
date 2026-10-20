@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
title AI Newsroom
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0launcher.py" %*
    goto :finished
)
where py >nul 2>&1
if not errorlevel 1 (
    py -3 "%~dp0launcher.py" %*
    goto :finished
)
where python >nul 2>&1
if not errorlevel 1 (
    python "%~dp0launcher.py" %*
    goto :finished
)
echo [ERROR] Python 3.11+ is not available. Keep this project and its .venv together.
pause
exit /b 1
:finished
if errorlevel 1 (
    echo [ERROR] Startup failed. Read the message above or data\server.log.
    pause
    exit /b 1
)
exit /b 0
