"""Centralized logging setup (stdlib ``logging`` only).

We configure a single root logger with two handlers:
  - ``StderrHandler``: human-readable, level from config
  - ``RotatingFileHandler`` (5 MB × 3) at ``$HERMESLITE_HOME/agent.log``

No log shipping, no JSON formatting — this is a CLI tool, not a server.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

from .paths import get_hermes_home


_DEFAULT_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DEFAULT_DATEFMT = "%H:%M:%S"


_configured = False


def setup_logging(level: str = "INFO", log_file: Optional[Path] = None) -> None:
    """Idempotent root-logger configuration.

    Idempotent so callers can re-invoke with new level/file without stacking
    handlers. We tear down existing handlers owned by us on every call.
    """
    global _configured
    root = logging.getLogger()
    # Remove our previous handlers (keep third-party handlers alone).
    for h in list(root.handlers):
        if getattr(h, "_hermeslite_owned", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    root.setLevel(_parse_level(level))

    formatter = logging.Formatter(_DEFAULT_FMT, datefmt=_DEFAULT_DATEFMT)

    stderr = logging.StreamHandler(stream=sys.stderr)
    stderr.setFormatter(formatter)
    stderr._hermeslite_owned = True  # type: ignore[attr-defined]
    root.addHandler(stderr)

    log_path = log_file or (get_hermes_home() / "agent.log")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        rotating._hermeslite_owned = True  # type: ignore[attr-defined]
        root.addHandler(rotating)
    except OSError:
        # File logging is best-effort; the CLI must still work without it.
        pass

    _configured = True


def _parse_level(value: str) -> int:
    v = (value or "").strip().upper()
    return getattr(logging, v, logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
