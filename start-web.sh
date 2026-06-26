#!/usr/bin/env bash
# =============================================================================
# HermesLite — Linux/macOS/WSL launcher for the web management UI
# =============================================================================
# Usage:
#   ./start-web.sh                           default (127.0.0.1:9119, no auth)
#   ./start-web.sh --port 9000               different port
#   ./start-web.sh --host 0.0.0.0 --insecure expose on LAN, no auth
#   ./start-web.sh --host 0.0.0.0             expose on LAN with auto-token
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

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "[start-web] Error: $PYTHON not found in PATH" >&2
    echo "  Set PYTHON to a Python 3.10+ interpreter and retry." >&2
    exit 1
fi

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "[start-web] Error: $PYTHON --version is older than 3.10" >&2
    "$PYTHON" --version >&2 || true
    exit 1
fi

echo "[start-web] cwd      = $HERE"
echo "[start-web] python   = $("$PYTHON" --version 2>&1)"
echo "[start-web] home     = $HERMESLITE_HOME"
echo "[start-web] argv     = $*"
echo

# Convert the POSIX path to a Windows path when running under Git Bash
# on Windows. The launcher's own argv has already been pre-processed by
# Git Bash (e.g. ``--port 9000``), so the values themselves are fine —
# only the *script* path needs converting for the Python interpreter.
START_PY_SCRIPT="$HERE/start.py"
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    START_PY_SCRIPT="$(cygpath -w "$START_PY_SCRIPT")"
fi

exec "$PYTHON" "$START_PY_SCRIPT" "$@"
