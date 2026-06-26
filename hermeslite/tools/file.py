"""File-system tools: read_file / write_file / edit_file.

These tools are deliberately *untyped* with respect to what the agent
can do — any path is fair game. A more restricted build would resolve
paths against an allow-list; that's out of scope here.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


def _resolve(path: str) -> Path:
    p = Path(os.path.expanduser(path or "."))
    if not p.is_absolute():
        from ..agent.runtime_cwd import resolve_agent_cwd
        p = resolve_agent_cwd() / p
    return p


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read the contents of a file at `path`. If `start_line` and "
        "`end_line` are given, returns the slice in 1-indexed inclusive "
        "form. Output is truncated to `max_bytes` (default 50_000)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or ~-expanded file path."},
            "start_line": {"type": "integer", "description": "First line (1-indexed). Optional."},
            "end_line": {"type": "integer", "description": "Last line (inclusive). Optional."},
            "max_bytes": {"type": "integer", "description": "Soft cap on output bytes. Default 50000."},
        },
        "required": ["path"],
    }

    def run(self, path: str, start_line: int = 0, end_line: int = 0, max_bytes: int = 50_000, **_: Any) -> ToolResult:
        try:
            p = _resolve(path)
            if not p.exists():
                return ToolResult.failure(f"file not found: {p}")
            if not p.is_file():
                return ToolResult.failure(f"not a regular file: {p}")
            if start_line or end_line:
                with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
                    lines = f.readlines()
                lo = max(1, int(start_line or 1)) - 1
                hi = int(end_line or len(lines))
                chunk = "".join(lines[lo:hi])
                return ToolResult.success(_truncate(chunk, max_bytes))
            with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
                data = f.read()
            return ToolResult.success(_truncate(data, max_bytes))
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write `content` to the file at `path`, creating parent directories "
        "as needed. Overwrites existing content. ``append``=true appends "
        "instead of overwriting."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean", "description": "Append instead of overwriting. Default false."},
        },
        "required": ["path", "content"],
    }

    def run(self, path: str, content: str, append: bool = False, **_: Any) -> ToolResult:
        try:
            p = _resolve(path)
            # Block writes to sensitive system paths.
            from .approval import check_sensitive_path
            err = check_sensitive_path(str(p))
            if err:
                return ToolResult.failure(err)
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            # ``newline=""`` keeps the file byte-for-byte identical to
            # the content we received. Without it, Windows translates
            # ``\n`` → ``\r\n`` and roundtrips through read_file fail.
            with p.open(mode, encoding="utf-8", newline="") as f:
                f.write(content)
            return ToolResult.success(f"wrote {len(content)} bytes to {p}")
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace the unique occurrence of `old_text` with `new_text` in "
        "the file at `path`. Fails if `old_text` is not found or appears "
        "more than once (use the more specific text or call write_file)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string", "description": "Exact text to replace (must be unique)."},
            "new_text": {"type": "string", "description": "Replacement text."},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def run(self, path: str, old_text: str, new_text: str, **_: Any) -> ToolResult:
        try:
            p = _resolve(path)
            # Block edits to sensitive system paths.
            from .approval import check_sensitive_path
            err = check_sensitive_path(str(p))
            if err:
                return ToolResult.failure(err)
            if not p.exists():
                return ToolResult.failure(f"file not found: {p}")
            with open(p, encoding="utf-8", errors="replace", newline="") as f:
                src = f.read()
            count = src.count(old_text)
            if count == 0:
                return ToolResult.failure("old_text not found in file")
            if count > 1:
                return ToolResult.failure(
                    f"old_text appears {count} times — make it unique or use write_file"
                )
            dst = src.replace(old_text, new_text, 1)
            p.write_text(dst, encoding="utf-8", newline="")
            return ToolResult.success(f"edited {p} ({len(old_text)} -> {len(new_text)} chars)")
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")


def _truncate(s: str, max_bytes: int) -> str:
    if max_bytes <= 0 or len(s.encode("utf-8")) <= max_bytes:
        return s
    # Truncate on a character boundary, not a byte boundary, to keep the
    # output decodable.
    out = s.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace")
    return out + f"\n\n... [truncated, {len(s) - len(out)} more chars]"


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

registry.register(ReadFileTool())
registry.register(WriteFileTool())
registry.register(EditFileTool())
