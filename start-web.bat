@echo off
REM ============================================================================
REM HermesLite — Windows launcher
REM ============================================================================
REM Usage:
REM   start-web.bat                           default (127.0.0.1:9119, no auth)
REM   start-web.bat --port 9000               different port
REM   start-web.bat --host 0.0.0.0 --insecure expose on LAN with no auth
REM   start-web.bat --host 0.0.0.0             expose on LAN with auto-token
REM
REM   start-chat.bat                          open the interactive REPL
REM   start-doctor.bat                        run diagnostics
REM
REM Environment:
REM   PYTHON             path to a Python interpreter (default: python on PATH)
REM   HERMESLITE_HOME    where to store config / state / logs
REM                       (default: %USERPROFILE%\.hermes-lite)
REM ============================================================================

setlocal ENABLEEXTENSIONS

set "HERE=%~dp0"
cd /d "%HERE%"

if "%PYTHON%"=="" set "PYTHON=python"

where %PYTHON% >nul 2>&1
if errorlevel 1 (
    echo [start-web] Error: %PYTHON% not found in PATH 1>&2
    echo   Set PYTHON to a Python 3.10+ interpreter and retry. 1>&2
    exit /b 1
)

REM Verify it really runs (PYTHON may be set to a path that ``where`` resolves
REM but the interpreter itself is broken on some Windows installs).
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo [start-web] Error: %PYTHON% --version failed 1>&2
    exit /b 1
)

if "%HERMESLITE_HOME%"=="" set "HERMESLITE_HOME=%USERPROFILE%\.hermes-lite"

echo [start-web] cwd      = %HERE%
echo [start-web] python   = %PYTHON%
echo [start-web] home     = %HERMESLITE_HOME%
echo [start-web] argv     = %*
echo.

%PYTHON% "%HERE%start.py" %*
set "EXITCODE=%ERRORLEVEL%"

endlocal & exit /b %EXITCODE%
