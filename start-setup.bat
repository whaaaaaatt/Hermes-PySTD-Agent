@echo off
REM ============================================================================
REM HermesLite — Windows model provider setup wizard
REM ============================================================================
REM Usage:
REM   start-setup.bat                           Interactive setup
REM   start-setup.bat --provider openai         Quick OpenAI setup
REM   start-setup.bat --provider openrouter --model anthropic/claude-3.5-sonnet
REM   start-setup.bat --status                  Show provider status
REM
REM This wizard helps you configure:
REM   - Provider selection (9 providers + custom)
REM   - Base URL (custom endpoints, proxies, mirrors)
REM   - API key (checks both env vars AND config.json)
REM   - Model selection (fetches live list from /v1/models endpoint)
REM   - Context window size (inferred or configurable)
REM
REM You can change this anytime with: hermeslite setup model
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
if "%HERMESLITE_HOME%"=="" set "HERMESLITE_HOME=%USERPROFILE%\.hermes-lite"

REM --- Pre-flight checks ---
where %PYTHON% >nul 2>&1
if errorlevel 1 (
    echo [start-setup] Error: %PYTHON% not found in PATH 1>&2
    echo   Set PYTHON to a Python 3.10+ interpreter and retry. 1>&2
    echo   Example: set PYTHON=python3 1>&2
    exit /b 1
)

%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo [start-setup] Error: %PYTHON% --version failed 1>&2
    exit /b 1
)

REM --- Check Python version (need 3.10+) ---
%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if errorlevel 1 (
    echo [start-setup] Error: Python is older than 3.10 1>&2
    %PYTHON% --version 1>&2
    echo   Please install Python 3.10 or newer. 1>&2
    exit /b 1
)

REM --- Check if first run ---
if not exist "%HERMESLITE_HOME%" mkdir "%HERMESLITE_HOME%"

REM --- Display banner ---
echo.
echo   ========================================================
echo           HermesLite — Model Provider Setup
echo   ========================================================
echo.
echo   This wizard helps you configure which AI provider
echo   and model HermesLite will use for conversations.
echo.

REM --- Show environment info ---
if defined OPENAI_API_KEY (
    echo   [OK] OPENAI_API_KEY is set
)
if defined OPENROUTER_API_KEY (
    echo   [OK] OPENROUTER_API_KEY is set
)
if defined DEEPSEEK_API_KEY (
    echo   [OK] DEEPSEEK_API_KEY is set
)
echo.

REM --- Run the setup wizard ---
%PYTHON% -m hermeslite.setup_model %*
set "EXITCODE=%ERRORLEVEL%"

endlocal & exit /b %EXITCODE%
