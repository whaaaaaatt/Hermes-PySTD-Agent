@echo off
REM ============================================================================
REM HermesLite — Windows chat REPL launcher
REM ============================================================================
REM Opens the interactive terminal session. Config is loaded from
REM HERMESLITE_HOME (default: %USERPROFILE%\.hermes-lite).
REM
REM Examples:
REM   start-chat.bat
REM   start-chat.bat --model gpt-4o --provider openai
REM   start-chat.bat --no-stream
REM ============================================================================

setlocal ENABLEEXTENSIONS
set "HERE=%~dp0"
cd /d "%HERE%"

if "%PYTHON%"=="" set "PYTHON=python"
if "%HERMESLITE_HOME%"=="" set "HERMESLITE_HOME=%USERPROFILE%\.hermes-lite"

where %PYTHON% >nul 2>&1
if errorlevel 1 (
    echo [start-chat] Error: %PYTHON% not found in PATH 1>&2
    exit /b 1
)

%PYTHON% -m hermeslite.cli chat %*
set "EXITCODE=%ERRORLEVEL%"
endlocal & exit /b %EXITCODE%
