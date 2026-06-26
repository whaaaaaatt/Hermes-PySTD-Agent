"""Path resolution for HermesLite.

Default home is ``~/.hermes-lite/`` — distinct from the upstream
``hermes-agent`` which uses ``~/.hermes/``, so the two can coexist
on the same machine without stepping on each other's state.
"""
from __future__ import annotations

import os
from pathlib import Path


_ENV_HOME = "HERMESLITE_HOME"
_APP_DIR_NAME = ".hermes-lite"


def get_hermes_home() -> Path:
    """Return the HermesLite home directory, creating it if needed.

    Resolution order:
      1. ``$HERMESLITE_HOME`` (env var, if set and non-empty)
      2. ``~/.hermes-lite/`` (default user-level directory)

    The directory is created on first access. We deliberately do NOT raise
    on creation failure here — callers that need a writable home (config
    load, sqlite open) will surface the error with full context. Path
    resolution itself must stay cheap and non-throwing.
    """
    env = os.environ.get(_ENV_HOME, "").strip()
    if env:
        home = Path(env).expanduser()
    else:
        home = Path.home() / _APP_DIR_NAME
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Caller will hit the same error on first write — let it surface
        # with the actual operation context (e.g. "cannot open state.db").
        pass
    return home


def get_config_path() -> Path:
    """Default config file path: ``$HERMESLITE_HOME/config.json``."""
    return get_hermes_home() / "config.json"


def get_state_db_path() -> Path:
    """Default SQLite state file path: ``$HERMESLITE_HOME/state.db``."""
    return get_hermes_home() / "state.db"


def get_skills_dir() -> Path:
    """Default skills directory: ``$HERMESLITE_HOME/skills/``."""
    d = get_hermes_home() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_history_path() -> Path:
    """Default readline history file: ``$HERMESLITE_HOME/history``."""
    return get_hermes_home() / "history"


def get_workspace_dir() -> Path:
    """Default workspace directory: ``$HERMESLITE_HOME/workspace/``.

    This is where the agent does its work by default — separate from the
    server code and from any user project directory.  Created on first
    access.
    """
    d = get_hermes_home() / "workspace"
    d.mkdir(parents=True, exist_ok=True)
    return d
