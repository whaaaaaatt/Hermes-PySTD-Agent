"""Todo / task list tool — aligned with hermes-agent-ref.

Single ``todo`` tool with read/write modes:
  - Omit ``todos`` → read current list
  - Provide ``todos`` array → write (replace or merge)

State is per-agent (stored on ``AIAgent._todo_store``).  The global
``_store`` singleton is only a fallback for tool tests.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)

_VALID_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})


class TodoStore:
    """Ordered in-memory todo list.  Position = priority."""

    def __init__(self) -> None:
        self._items: List[Dict[str, str]] = []
        self._lock = threading.Lock()

    def write(self, todos: List[Dict[str, str]], merge: bool = False) -> List[Dict[str, str]]:
        """Write the todo list.

        ``merge=False`` (default): replace the entire list.
        ``merge=True``: update existing items by id, append new ones.
        """
        with self._lock:
            if not merge:
                self._items = [self._normalise(t) for t in todos]
            else:
                by_id = {i["id"]: i for i in self._items}
                for t in todos:
                    item = self._normalise(t)
                    existing = by_id.get(item["id"])
                    if existing:
                        existing["content"] = item["content"]
                        existing["status"] = item["status"]
                    else:
                        self._items.append(item)
                        by_id[item["id"]] = item
        return self.read()

    def read(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(i) for i in self._items]

    def has_items(self) -> bool:
        with self._lock:
            return bool(self._items)

    def format_for_injection(self) -> Optional[str]:
        """Return a user-message string to re-inject active todos after
        context compression.  Only includes pending / in_progress items."""
        with self._lock:
            active = [i for i in self._items if i["status"] in ("pending", "in_progress")]
        if not active:
            return None
        lines = ["[Your active task list was preserved across context compression]"]
        for i in active:
            tag = "[>]" if i["status"] == "in_progress" else "[ ]"
            lines.append(f"  {tag} {i['id']}: {i['content']}")
        return "\n".join(lines)

    @staticmethod
    def _normalise(t: Dict[str, Any]) -> Dict[str, str]:
        status = t.get("status", "pending")
        if status not in _VALID_STATUSES:
            status = "pending"
        return {
            "id": str(t.get("id", "")),
            "content": str(t.get("content", "")),
            "status": status,
        }


def _format_result(items: List[Dict[str, str]]) -> str:
    """Format the full list + summary as JSON (matches ref output shape)."""
    counts: Dict[str, int] = {}
    for i in items:
        s = i["status"]
        counts[s] = counts.get(s, 0) + 1
    return json.dumps({
        "todos": items,
        "summary": {
            "total": len(items),
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "completed": counts.get("completed", 0),
            "cancelled": counts.get("cancelled", 0),
        },
    }, ensure_ascii=False, indent=2)


# Module-level default store (used when the agent doesn't inject one).
_default_store = TodoStore()


class TodoTool(Tool):
    """Single unified todo tool — read/write via optional ``todos`` param."""

    name = "todo"
    description = (
        "Manage your task list for the current session. Use for complex tasks "
        "with 3+ steps or when the user provides multiple tasks. "
        "Call with no parameters to read the current list.\n\n"
        "Writing:\n"
        "- Provide 'todos' array to create/update items\n"
        "- merge=false (default): replace the entire list with a fresh plan\n"
        "- merge=true: update existing items by id, add any new ones\n\n"
        "Each item: {id: string, content: string, "
        "status: pending|in_progress|completed|cancelled}\n"
        "List order is priority. Only ONE item in_progress at a time.\n"
        "Mark items completed immediately when done. If something fails, "
        "cancel it and add a revised item.\n\n"
        "Always returns the full current list."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Task items to write. Omit to read current list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Unique item identifier (you choose the id)."},
                        "content": {"type": "string", "description": "Task description."},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                            "description": "Item status.",
                        },
                    },
                    "required": ["id", "content", "status"],
                },
            },
            "merge": {
                "type": "boolean",
                "description": (
                    "true: update existing items by id, add new ones. "
                    "false (default): replace the entire list."
                ),
                "default": False,
            },
        },
        "required": [],
    }

    def run(self, todos: Any = None, merge: bool = False, **_: Any) -> ToolResult:
        # Resolve the store — prefer agent-level, fallback to module default.
        store: TodoStore = getattr(self, "_todo_store", _default_store)

        if todos is None:
            # Read mode.
            items = store.read()
            return ToolResult.success(_format_result(items))

        # Write mode — validate input.
        if not isinstance(todos, list):
            return ToolResult.failure("todos must be an array of {id, content, status} objects")

        # Validate and deduplicate (keep last occurrence per id).
        cleaned: List[Dict[str, str]] = []
        seen: set = set()
        for t in todos:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id", ""))
            if not tid:
                continue
            if tid in seen:
                # Deduplicate: remove earlier occurrence.
                cleaned = [c for c in cleaned if c["id"] != tid]
            seen.add(tid)
            cleaned.append(TodoStore._normalise(t))

        if not cleaned:
            return ToolResult.failure("no valid items in todos array (each needs id, content, status)")

        items = store.write(cleaned, merge=merge)
        return ToolResult.success(_format_result(items))


registry.register(TodoTool())
