#!/usr/bin/env bash
# =============================================================================
# HermesLite — Linux/macOS/WSL model provider setup wizard
# =============================================================================
# Usage:
#   ./start-setup.sh                           Interactive setup
#   ./start-setup.sh --provider openai         Quick OpenAI setup
#   ./start-setup.sh --provider openrouter --model anthropic/claude-3.5-sonnet
#   ./start-setup.sh --status                  Show provider status
#
# This wizard helps you configure:
#   - Provider selection (9 providers + custom)
#   - Base URL (custom endpoints, proxies, mirrors)
#   - API key (checks both env vars AND config.json)
#   - Model selection (fetches live list from /v1/models endpoint)
#   - Context window size (inferred or configurable)
#
# You can change this anytime with: hermeslite setup model
#
# Environment:
#   PYTHON             path to a Python 3.10+ interpreter (default: python3)
#   HERMESLITE_HOME    where to store config / state / logs
#                       (default: ~/.hermes-lite)
# =============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"
HERMESLITE_HOME="${HERMESLITE_HOME:-$HOME/.hermes-lite}"
export HERMESLITE_HOME

# --- Color output helpers ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}$*${NC}"; }
ok()    { echo -e "${GREEN}$*${NC}"; }
warn()  { echo -e "${YELLOW}$*${NC}"; }
err()   { echo -e "${RED}$*${NC}" >&2; }

# --- Pre-flight checks ---
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    err "[start-setup] Error: $PYTHON not found in PATH"
    err "  Set PYTHON to a Python 3.10+ interpreter and retry."
    err "  Example: PYTHON=python3 ./start-setup.sh"
    exit 1
fi

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    err "[start-setup] Error: $PYTHON is older than Python 3.10"
    err "  Current version: $("$PYTHON" --version 2>&1)"
    err "  Please install Python 3.10 or newer."
    exit 1
fi

# --- Check if first run ---
if [ ! -f "$HERMESLITE_HOME/config.json" ]; then
    warn "[start-setup] First run detected — creating $HERMESLITE_HOME"
    mkdir -p "$HERMESLITE_HOME"
fi

# --- Display banner ---
echo
info "╔════════════════════════════════════════════════════════════╗"
info "║          HermesLite — Model Provider Setup                ║"
info "╚════════════════════════════════════════════════════════════╝"
echo
info "  This wizard helps you configure which AI provider"
info "  and model HermesLite will use for conversations."
echo

# --- Show environment info ---
if [ -n "${OPENAI_API_KEY:-}" ]; then
    ok "  ✓ OPENAI_API_KEY is set"
fi
if [ -n "${OPENROUTER_API_KEY:-}" ]; then
    ok "  ✓ OPENROUTER_API_KEY is set"
fi
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
    ok "  ✓ DEEPSEEK_API_KEY is set"
fi
echo

# --- Convert POSIX path to Windows path on Git Bash / Cygwin ---
SETUP_PY_SCRIPT="$HERE/hermeslite/setup_model.py"
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    # Ensure we're in a Windows-friendly directory for Python
    cd "$(cygpath -w "$HERE")"
fi

# --- Run the setup wizard ---
exec "$PYTHON" -m hermeslite.setup_model "$@"
