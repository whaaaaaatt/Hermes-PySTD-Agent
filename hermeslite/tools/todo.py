"""Todo / task list tool (in-memory only — the list is per-process).

Persisting todos across processes is out of scope; the agent uses this
for short-lived planning within a single session.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


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
    description = "Add an item to the in-memory todo list. Returns the new id."
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "What to remember."},
        },
        "required": ["content"],
    }

    def run(self, content: str, **_: Any) -> ToolResult:
        tid = _store.add(content)
        return ToolResult.success(f"added #{tid}: {content}")


class TodoUpdateTool(Tool):
    name = "todo_update"
    description = "Update an item's status. Status is one of: pending, in_progress, done."
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
        return ToolResult.success(f"updated #{id} -> {status}")


class TodoListTool(Tool):
    name = "todo_list"
    description = "List all todos in the current session."
    parameters = {"type": "object", "properties": {}}

    def run(self, **_: Any) -> ToolResult:
        items = _store.list()
        if not items:
            return ToolResult.success("(no todos)")
        lines = [f"[{i['status']:11s}] #{i['id']}: {i['content']}" for i in items]
        return ToolResult.success("\n".join(lines))


registry.register(TodoAddTool())
registry.register(TodoUpdateTool())
registry.register(TodoListTool())
