"""Helpers for the REPL — small string / formatting utilities."""
from __future__ import annotations

from typing import Any


def _short_repr(obj: Any, max_len: int = 80) -> str:
    """Like ``repr`` but with a hard cap on length."""
    r = repr(obj)
    if len(r) > max_len:
        return r[:max_len] + "…"
    return r
