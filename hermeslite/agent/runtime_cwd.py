"""Agent working directory resolution.

Single source of truth for where the agent does its work — reads/writes
files, runs commands, discovers context files (AGENTS.md, etc.).

Resolution priority:
  1. ``$TERMINAL_CWD`` — set from ``config.terminal.cwd`` at startup,
     or overridden per-request via the ``PUT /api/cwd`` endpoint.
  2. ``$HERMESLITE_HOME/workspace/`` — default workspace under the
     HermesLite home directory.
  3. ``os.getcwd()`` — process launch directory (last resort).

This mirrors the reference implementation's ``agent/runtime_cwd.py``
without coupling to any third-party dependency.
"""
from __future__ import annotations

import os
from pathlib import Path


def resolve_agent_cwd() -> Path:
    """Resolve the agent's working directory.

    Returns the first available directory from the priority list above.
    The ``workspace/`` fallback is created on first access.
    """
    # Priority 1: explicit override via env var.
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p

    # Priority 2: $HERMESLITE_HOME/workspace/
    try:
        from ..paths import get_hermes_home
        ws = get_hermes_home() / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        return ws
    except Exception:  # noqa: BLE001
        pass

    # Priority 3: process cwd.
    return Path(os.getcwd())
