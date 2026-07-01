"""Todo / task list tool (in-memory only — the list is per-process).

Persisting todos across processes is out of scope; the agent uses this
for short-lived planning within a single session.

Design notes (aligned with hermes-agent-ref):

- Every mutation (add / update / remove) returns the **full list** plus
  summary counts so the model always knows the current state.
- Only ONE item should be ``in_progress`` at a time — finish it before
  starting the next.
- After planning, **execute immediately** — do not loop on todo updates.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


def _format_list(items: List[Dict[str, Any]]) -> str:
    """Format the full todo list with summary counts."""
    if not items:
        return "(no todos)\n\nSummary: 0 total, 0 pending, 0 in_progress, 0 done"
    lines = []
    for i in items:
        tag = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}.get(i["status"], "[ ]")
        lines.append(f"{tag} #{i['id']}: {i['content']}")
    counts = {"pending": 0, "in_progress": 0, "done": 0}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    summary = f"Summary: {len(items)} total, {counts['pending']} pending, {counts['in_progress']} in_progress, {counts['done']} done"
    return "\n".join(lines) + "\n\n" + summary


class _TodoStore:
    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def add(self, content: str) -> str:
        tid = uuid.uuid4().hex[:8]
        with self._lock:
            self._items[tid] = {"id": tid, "content": content, "status": "pending"}
        return tid

    def update(self, tid: str, status: str) -> bool:
        if status not in ("pending", "in_progress", "done"):
            return False
        with self._lock:
            item = self._items.get(tid)
            if not item:
                return False
            item["status"] = status
            return True

    def remove(self, tid: str) -> bool:
        with self._lock:
            return self._items.pop(tid, None) is not None

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._items.values())


_store = _TodoStore()


class TodoAddTool(Tool):
    name = "todo_add"
    description = (
        "Add a task to the planning list. Returns the full list with status "
        "counts. IMPORTANT: After planning, execute the tasks immediately "
        "using terminal / file tools — do NOT keep adding more todos."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "What to do."},
        },
        "required": ["content"],
    }

    def run(self, content: str, **_: Any) -> ToolResult:
        tid = _store.add(content)
        items = _store.list()
        return ToolResult.success(f"Added #{tid}: {content}\n\n{_format_list(items)}")


class TodoUpdateTool(Tool):
    name = "todo_update"
    description = (
        "Update a task's status. Only ONE task should be in_progress at a "
        "time. Mark tasks done immediately after completing them. Returns "
        "the full list with status counts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Todo id from todo_add."},
            "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
        },
        "required": ["id", "status"],
    }

    def run(self, id: str, status: str, **_: Any) -> ToolResult:
        ok = _store.update(id, status)
        if not ok:
            return ToolResult.failure(f"could not update todo {id} (unknown id or bad status)")
        items = _store.list()
        return ToolResult.success(f"Updated #{id} -> {status}\n\n{_format_list(items)}")


class TodoRemoveTool(Tool):
    name = "todo_remove"
    description = "Remove a task from the list. Returns the full list with status counts."
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Todo id to remove."},
        },
        "required": ["id"],
    }

    def run(self, id: str, **_: Any) -> ToolResult:
        ok = _store.remove(id)
        if not ok:
            return ToolResult.failure(f"could not remove todo {id} (unknown id)")
        items = _store.list()
        return ToolResult.success(f"Removed #{id}\n\n{_format_list(items)}")


class TodoListTool(Tool):
    name = "todo_list"
    description = "List all tasks with their current status."
    parameters = {"type": "object", "properties": {}}

    def run(self, **_: Any) -> ToolResult:
        items = _store.list()
        return ToolResult.success(_format_list(items))


registry.register(TodoAddTool())
registry.register(TodoUpdateTool())
registry.register(TodoRemoveTool())
registry.register(TodoListTool())
