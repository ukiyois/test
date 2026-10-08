@echo off
rem ============================================================
rem LLMmodol Local AI Studio - start script (CMD)
rem Usage: start.bat            start and open browser
rem        start.bat nobrowser  start without opening browser
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PORT=7860"
set "PIDFILE=data\server.pid"
set "LOGFILE=data\server.log"
set "ERRFILE=data\server-error.log"

rem ---- check python ----
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    exit /b 1
)

rem ---- compute current build id ----
set "BUILD="
for /f "delims=" %%B in ('python scripts\_svc.py build 2^>nul') do set "BUILD=%%B"
if not defined BUILD (
    echo [ERROR] cannot compute platform build id.
    exit /b 1
)

if not exist data mkdir data

rem ---- already running current build: open browser and exit ----
set "RUNBUILD="
for /f "delims=" %%H in ('python scripts\_svc.py health 2^>nul') do set "RUNBUILD=%%H"
if /i "%RUNBUILD%"=="%BUILD%" (
    echo Platform already running ^(build %BUILD%^).
    if /i not "%~1"=="nobrowser" start "" "http://127.0.0.1:%PORT%"
    exit /b 0
)

rem ---- port occupied: only kill if it is our uvicorn ----
set "OLDPID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do set "OLDPID=%%P"
if defined OLDPID (
    python scripts\_svc.py check !OLDPID! >nul 2>&1
    if not errorlevel 1 (
        echo Stopping old platform process ^(PID !OLDPID!^)...
        taskkill /PID !OLDPID! /F >nul 2>&1
        set /a WAITN=0
        :waitport
        ping 127.0.0.1 -n 2 >nul
        netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
        if not errorlevel 1 (
            set /a WAITN+=1
            if !WAITN! LSS 20 goto waitport
        )
    ) else (
        echo [ERROR] port %PORT% is used by another program; not stopping it.
        exit /b 1
    )
)

rem ---- start uvicorn in background ----
echo Starting platform ^(build %BUILD%^)...
start "LLMmodol" /min /b python -m uvicorn llmplatform.app:app --host 127.0.0.1 --port %PORT% 1>"%LOGFILE%" 2>"%ERRFILE%"

rem ---- wait until ready ----
set /a TRY=0
:waitready
set /a TRY+=1
if !TRY! GTR 30 goto notready
ping 127.0.0.1 -n 2 >nul
set "READYBUILD="
for /f "delims=" %%H in ('python scripts\_svc.py health 2^>nul') do set "READYBUILD=%%H"
if /i "!READYBUILD!"=="%BUILD%" goto ready
goto waitready

:ready
rem ---- record server PID ----
set "NEWPID="
for /f "delims=" %%P in ('python scripts\_svc.py pid 2^>nul') do set "NEWPID=%%P"
if defined NEWPID (
    >"%PIDFILE%" echo !NEWPID!
    echo Platform ready: http://127.0.0.1:%PORT%  ^(PID !NEWPID!^)
) else (
    echo Platform ready: http://127.0.0.1:%PORT%
)
if /i not "%~1"=="nobrowser" start "" "http://127.0.0.1:%PORT%"
exit /b 0

:notready
echo [ERROR] platform failed to start. See data\server.log
exit /b 1
