"""Memory tools backed by ``StateStore``.

These let the agent persist and recall key/value facts across sessions.
The same SQLite database stores sessions + memory; queries are isolated
by table.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..paths import get_state_db_path
from ..state import StateStore
from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


def _store() -> StateStore:
    # Construct lazily so a write failure during boot is delayed until
    # the first call. The StateStore's __init__ opens the DB; if that
    # fails we want to surface the error from inside the tool, not at
    # import time.
    return StateStore(get_state_db_path())


class MemoryReadTool(Tool):
    name = "memory_read"
    description = (
        "Read a single key from persistent memory. Returns the value, "
        "or an error if the key doesn't exist."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
        },
        "required": ["key"],
    }

    def run(self, key: str, **_: Any) -> ToolResult:
        try:
            val = _store().memory_get(key)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        if val is None:
            return ToolResult.failure(f"key not found: {key!r}")
        return ToolResult.success(val)


class MemoryWriteTool(Tool):
    name = "memory_write"
    description = (
        "Write a value to persistent memory under `key` (overwrites if it "
        "exists). `tags` is an optional comma-separated list used by "
        "memory_search."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "value": {"type": "string"},
            "tags": {"type": "string", "description": "Optional CSV of tags."},
        },
        "required": ["key", "value"],
    }

    def run(self, key: str, value: str, tags: str = "", **_: Any) -> ToolResult:
        try:
            _store().memory_set(key, value, tags)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        return ToolResult.success(f"wrote {key!r} ({len(value)} chars)")


class MemorySearchTool(Tool):
    name = "memory_search"
    description = (
        "Search memory by substring (case-insensitive). Supports multi-word "
        "queries (AND logic — all words must match). Returns matching key/value pairs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (space-separated words are ANDed)."},
            "limit": {"type": "integer", "description": "Max results. Default 20."},
        },
        "required": ["query"],
    }

    def run(self, query: str, limit: int = 20, **_: Any) -> ToolResult:
        try:
            rows = _store().memory_search(query, limit=limit)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        if not rows:
            return ToolResult.success(f"(no matches for '{query}')")
        lines = [f"Found {len(rows)} match(es):", ""]
        for r in rows:
            key = r["key"]
            tags = r.get("tags") or ""
            value = r["value"] or ""
            # Truncate long values for readability but show more than before.
            display_value = value[:500] + "..." if len(value) > 500 else value
            tag_str = f" [{tags}]" if tags else ""
            lines.append(f"  {key}{tag_str}")
            lines.append(f"    {display_value}")
            lines.append("")
        return ToolResult.success("\n".join(lines).strip())


class MemoryListTool(Tool):
    name = "memory_list"
    description = "List recent memory entries (most recent first). Default 20."
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max results. Default 20."},
        },
    }

    def run(self, limit: int = 20, **_: Any) -> ToolResult:
        try:
            rows = _store().memory_list(limit=limit)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        if not rows:
            return ToolResult.success("(empty)")
        out = "\n".join(
            f"{r['key']}  [{r['tags'] or '-'}]  =  {r['value']}" for r in rows
        )
        return ToolResult.success(out)


class MemoryDeleteTool(Tool):
    name = "memory_delete"
    description = "Delete a key from persistent memory."
    parameters = {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    }

    def run(self, key: str, **_: Any) -> ToolResult:
        try:
            ok = _store().memory_delete(key)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        if not ok:
            return ToolResult.failure(f"key not found: {key!r}")
        return ToolResult.success(f"deleted {key!r}")


registry.register(MemoryReadTool())
registry.register(MemoryWriteTool())
registry.register(MemorySearchTool())
registry.register(MemoryListTool())
registry.register(MemoryDeleteTool())


class MemoryCountTool(Tool):
    name = "memory_count"
    description = "Return the total number of entries in persistent memory."
    parameters = {
        "type": "object",
        "properties": {},
    }

    def run(self, **_: Any) -> ToolResult:
        try:
            cnt = _store().memory_count()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        return ToolResult.success(f"{cnt} entries in memory")


registry.register(MemoryCountTool())


class MemoryTool(Tool):
    """Unified memory tool with add/replace/remove/search actions.

    Combines the functionality of memory_write, memory_delete, and
    memory_search into a single tool with an 'action' parameter. This
    is the tool the model should use for proactive memory management.
    """
    name = "memory"
    description = (
        "Save/replace/remove/search entries in persistent memory. Use this proactively "
        "to remember: user preferences, corrections, environment facts, project "
        "conventions, and anything that will matter in future sessions. "
        "Do not save temporary task state or trivial/obvious information."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove", "search"],
                "description": (
                    "add = create or overwrite a key. "
                    "replace = update an existing key (fails if not found). "
                    "remove = delete a key (fails if not found). "
                    "search = search memory by substring query."
                ),
            },
            "key": {"type": "string", "description": "Memory key (for add/replace/remove)."},
            "value": {"type": "string", "description": "Value to store (for add/replace)."},
            "query": {"type": "string", "description": "Search query (for search action)."},
        },
        "required": ["action"],
    }

    def run(self, action: str, key: str = "", value: str = "", query: str = "", **_: Any) -> ToolResult:
        try:
            store = _store()
            if action == "add":
                if not key:
                    return ToolResult.failure("key is required for add action")
                store.memory_set(key, value)
                return ToolResult.success(f"Saved '{key}' ({len(value)} chars).")
            if action == "replace":
                if not key:
                    return ToolResult.failure("key is required for replace action")
                existing = store.memory_get(key)
                if existing is None:
                    return ToolResult.failure(f"Key '{key}' not found. Use action='add' to create it.")
                store.memory_set(key, value)
                return ToolResult.success(f"Replaced '{key}' ({len(value)} chars).")
            if action == "remove":
                if not key:
                    return ToolResult.failure("key is required for remove action")
                ok = store.memory_delete(key)
                if not ok:
                    return ToolResult.failure(f"Key '{key}' not found.")
                return ToolResult.success(f"Deleted '{key}'.")
            if action == "search":
                if not query:
                    return ToolResult.failure("query is required for search action")
                rows = store.memory_search(query, limit=20)
                if not rows:
                    return ToolResult.success(f"(no matches for '{query}')")
                lines = [f"Found {len(rows)} match(es):", ""]
                for r in rows:
                    k = r["key"]
                    tags = r.get("tags") or ""
                    val = r["value"] or ""
                    display_val = val[:500] + "..." if len(val) > 500 else val
                    tag_str = f" [{tags}]" if tags else ""
                    lines.append(f"  {k}{tag_str}")
                    lines.append(f"    {display_val}")
                    lines.append("")
                return ToolResult.success("\n".join(lines).strip())
            return ToolResult.failure(f"Unknown action '{action}'. Use: add, replace, remove, search")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")


registry.register(MemoryTool())
