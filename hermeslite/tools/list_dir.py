"""List directory and search files (glob + grep)."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, List

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


def _resolve(path: str) -> Path:
    p = Path(os.path.expanduser(path or "."))
    if not p.is_absolute():
        from ..agent.runtime_cwd import resolve_agent_cwd
        p = resolve_agent_cwd() / p
    return p


class ListDirTool(Tool):
    name = "list_dir"
    description = (
        "List the contents of `path` (default: current working directory). "
        "Returns one entry per line with a trailing '/' for directories. "
        "Hidden files are included by default; pass `include_hidden=false` to skip them."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list. Default cwd."},
            "include_hidden": {"type": "boolean", "description": "Include dotfiles. Default true."},
            "max_entries": {"type": "integer", "description": "Cap on entries. Default 500."},
        },
    }

    def run(self, path: str = ".", include_hidden: bool = True, max_entries: int = 500, **_: Any) -> ToolResult:
        try:
            p = _resolve(path)
            if not p.exists():
                return ToolResult.failure(f"path not found: {p}")
            if not p.is_dir():
                return ToolResult.failure(f"not a directory: {p}")
            entries: List[str] = []
            for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
                if not include_hidden and entry.name.startswith("."):
                    continue
                suffix = "/" if entry.is_dir() else ""
                entries.append(entry.name + suffix)
                if len(entries) >= max_entries:
                    entries.append(f"... [truncated at {max_entries} entries]")
                    break
            return ToolResult.success("\n".join(entries) if entries else "(empty directory)")
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")


class SearchFilesTool(Tool):
    name = "search_files"
    description = (
        "Search for `query` in files under `path`. Recurses into subdirectories. "
        "Returns matching lines as `path:lineno: text`. Use `glob` to filter files "
        "(e.g. `*.py`); defaults to common text extensions. `context` is the number "
        "of surrounding lines to include (default 0)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Substring to search for."},
            "path": {"type": "string", "description": "Root directory. Default cwd."},
            "glob": {"type": "string", "description": "File pattern, e.g. '*.py'. Default text files."},
            "context": {"type": "integer", "description": "Lines of context around each match. Default 0."},
            "max_results": {"type": "integer", "description": "Cap on matches. Default 100."},
        },
        "required": ["query"],
    }

    DEFAULT_GLOBS = ("*.py", "*.md", "*.txt", "*.json", "*.yaml", "*.yml", "*.js", "*.ts", "*.html", "*.css", "*.sh")

    def run(
        self,
        query: str,
        path: str = ".",
        glob: str = "",
        context: int = 0,
        max_results: int = 100,
        **_: Any,
    ) -> ToolResult:
        try:
            root = _resolve(path)
            if not root.is_dir():
                return ToolResult.failure(f"not a directory: {root}")
            patterns = (glob,) if glob else self.DEFAULT_GLOBS
            results = []
            for pat in patterns:
                for fp in root.rglob(pat):
                    if not fp.is_file():
                        continue
                    try:
                        with fp.open("r", encoding="utf-8", errors="replace") as f:
                            lines = f.readlines()
                    except OSError:
                        continue
                    for i, line in enumerate(lines, 1):
                        if query in line:
                            if context > 0:
                                lo = max(1, i - context)
                                hi = min(len(lines), i + context)
                                for j in range(lo, hi + 1):
                                    results.append(f"{fp}:{j}: {lines[j-1].rstrip()}")
                                results.append("--")
                            else:
                                results.append(f"{fp}:{i}: {line.rstrip()}")
                            if len(results) >= max_results:
                                results.append(f"... [truncated at {max_results} matches]")
                                return ToolResult.success("\n".join(results))
            return ToolResult.success("\n".join(results) if results else "(no matches)")
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")


registry.register(ListDirTool())
registry.register(SearchFilesTool())
