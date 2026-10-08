@echo off
rem ============================================================
rem LLMmodol Local AI Studio - stop script (CMD)
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PIDFILE=data\server.pid"

if not exist "%PIDFILE%" (
    echo No saved server process.
    exit /b 0
)

set "SERVERID="
set /p SERVERID=<"%PIDFILE%"
set "SERVERID=%SERVERID: =%"

if not defined SERVERID goto cleanup

rem ---- only stop if the process is our uvicorn ----
python scripts\_svc.py check %SERVERID% >nul 2>&1
if errorlevel 1 (
    echo Platform server process already exited.
    goto cleanup
)

rem ---- gracefully stop GGUF engine first (ignore failures) ----
python scripts\_svc.py stop >nul 2>&1

taskkill /PID %SERVERID% /F >nul 2>&1
echo Platform server stopped.

:cleanup
del /f /q "%PIDFILE%" >nul 2>&1
exit /b 0
