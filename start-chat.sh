#!/usr/bin/env bash
# =============================================================================
# HermesLite — Linux/macOS/WSL launcher for the interactive REPL
# =============================================================================
# Examples:
#   ./start-chat.sh
#   ./start-chat.sh --model gpt-4o --provider openai
#   ./start-chat.sh --no-stream
# =============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"
HERMESLITE_HOME="${HERMESLITE_HOME:-$HOME/.hermes-lite}"
export HERMESLITE_HOME

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "[start-chat] Error: $PYTHON not found in PATH" >&2
    exit 1
fi

# Convert POSIX path to a Windows path on Git Bash / Cygwin, because
# `python -m hermeslite.cli` uses the CWD to find the package — a
# `/d/...` path won't resolve.
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    cd "$(cygpath -w "$HERE")"
fi

exec "$PYTHON" -m hermeslite.cli chat "$@"
