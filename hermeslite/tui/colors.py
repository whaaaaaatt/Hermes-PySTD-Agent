"""ANSI colors and small TUI helpers (stdlib only)."""
from __future__ import annotations

import os
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def colors_enabled(mode: str = "auto") -> bool:
    """Return True iff ANSI escape sequences should be emitted.

    ``mode`` is "auto" (default), "on", or "off". "auto" returns False
    when stdout isn't a TTY, when ``NO_COLOR`` is set in the environment
    (https://no-color.org/), or when the platform's TERM is "dumb".
    """
    if mode == "on":
        return True
    if mode == "off":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Style helpers
# ---------------------------------------------------------------------------

class Style:
    """A tiny helper that wraps an ANSI sequence and no-ops when colors are off.

    Use ``Style("32")`` for green, ``Style("1;33")`` for bold yellow.
    The text() method returns the styled string; the file write helpers
    in :mod:`tui.prompt` use ``print`` directly.
    """

    __slots__ = ("_code", "_enabled")

    def __init__(self, code: str, enabled: bool = True):
        self._code = code
        self._enabled = enabled

    def _wrap(self, text: str) -> str:
        if not self._enabled:
            return text
        return f"\033[{self._code}m{text}\033[0m"

    def __call__(self, text: str) -> str:
        return self._wrap(text)

    def apply(self, enabled: bool) -> "Style":
        return Style(self._code, enabled)


# Pre-built styles. They are enabled/disabled globally by ``configure``.
_RESET = "\033[0m"


def _mk(code: str) -> Style:
    return Style(code)


bold: Style = _mk("1")
dim: Style = _mk("2")
red: Style = _mk("31")
green: Style = _mk("32")
yellow: Style = _mk("33")
blue: Style = _mk("34")
magenta: Style = _mk("35")
cyan: Style = _mk("36")
gray: Style = _mk("90")
bright_green: Style = _mk("92")
bright_cyan: Style = _mk("96")


def configure(enabled: bool) -> None:
    """Globally enable or disable the pre-built styles."""
    global bold, dim, red, green, yellow, blue, magenta, cyan, gray, bright_green, bright_cyan
    bold = bold.apply(enabled)
    dim = dim.apply(enabled)
    red = red.apply(enabled)
    green = green.apply(enabled)
    yellow = yellow.apply(enabled)
    blue = blue.apply(enabled)
    magenta = magenta.apply(enabled)
    cyan = cyan.apply(enabled)
    gray = gray.apply(enabled)
    bright_green = bright_green.apply(enabled)
    bright_cyan = bright_cyan.apply(enabled)


# ---------------------------------------------------------------------------
# Box-drawing
# ---------------------------------------------------------------------------

def banner(text: str, char: str = "─", width: int = 60) -> str:
    """Render a centered banner like ``────────── text ──────────``."""
    pad = max(0, (width - len(text) - 2) // 2)
    return f"{char * pad} {text} {char * pad}"


def clear_line() -> str:
    return "\033[2K\r"


def move_up(n: int = 1) -> str:
    return f"\033[{n}A"
