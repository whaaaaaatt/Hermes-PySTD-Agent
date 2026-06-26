#!/usr/bin/env bash
# =============================================================================
# HermesLite — diagnostic launcher
# =============================================================================
# Runs `hermeslite doctor`: version, paths, provider status, tool count, etc.
# =============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"
HERMESLITE_HOME="${HERMESLITE_HOME:-$HOME/.hermes-lite}"
export HERMESLITE_HOME

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "[start-doctor] Error: $PYTHON not found in PATH" >&2
    exit 1
fi

# Convert POSIX path to a Windows path on Git Bash / Cygwin, because
# `python -m hermeslite.cli` uses the CWD to find the package.
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    cd "$(cygpath -w "$HERE")"
fi

exec "$PYTHON" -m hermeslite.cli doctor "$@"
