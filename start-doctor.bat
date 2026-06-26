@echo off
REM ============================================================================
REM HermesLite — Windows diagnostic launcher
REM ============================================================================
REM Runs `hermeslite doctor`: prints version, paths, provider status, tool
REM count, and verifies the home directory is writable.
REM ============================================================================

setlocal ENABLEEXTENSIONS
set "HERE=%~dp0"
cd /d "%HERE%"

if "%PYTHON%"=="" set "PYTHON=python"
if "%HERMESLITE_HOME%"=="" set "HERMESLITE_HOME=%USERPROFILE%\.hermes-lite"

where %PYTHON% >nul 2>&1
if errorlevel 1 (
    echo [start-doctor] Error: %PYTHON% not found in PATH 1>&2
    exit /b 1
)

%PYTHON% -m hermeslite.cli doctor %*
set "EXITCODE=%ERRORLEVEL%"
endlocal & exit /b %EXITCODE%
