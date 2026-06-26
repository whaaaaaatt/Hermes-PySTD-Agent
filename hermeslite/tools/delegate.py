"""Sub-agent delegation tool.

Spawns child AIAgent instances with isolated context, restricted toolsets,
and their own session. Supports single-task and batch (parallel) modes.
The parent blocks until all children complete.

Each child gets:
  - A fresh conversation (no parent history)
  - Its own session_id
  - A restricted toolset (delegate_task and memory are always stripped)
  - A focused system prompt built from the delegated goal + context

The parent's context only sees the delegation call and the summary result,
never the child's intermediate tool calls or reasoning.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DELEGATE_BLOCKED_TOOLS = frozenset([
    "delegate_task",  # no recursive delegation
    "memory",         # no writes to shared memory
])

DEFAULT_MAX_CONCURRENT = 3
DEFAULT_CHILD_TIMEOUT = 600.0  # seconds
DEFAULT_MAX_ITERATIONS = 25


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_delegation_config() -> Dict[str, Any]:
    """Read delegation config, falling back to defaults."""
    try:
        from ..config import load_config
        cfg = load_config()
        return cfg.get("delegation") or {}
    except Exception:
        return {}


def _get_max_concurrent() -> int:
    cfg = _load_delegation_config()
    val = cfg.get("max_concurrent_children")
    if val is not None:
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            pass
    return DEFAULT_MAX_CONCURRENT


def _get_child_timeout() -> float:
    cfg = _load_delegation_config()
    val = cfg.get("child_timeout_seconds")
    if val is not None:
        try:
            return max(30.0, float(val))
        except (TypeError, ValueError):
            pass
    return DEFAULT_CHILD_TIMEOUT


def _get_max_iterations() -> int:
    cfg = _load_delegation_config()
    val = cfg.get("max_iterations")
    if val is not None:
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            pass
    return DEFAULT_MAX_ITERATIONS


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
) -> str:
    """Build a focused system prompt for a child agent."""
    parts = [
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    parts.append(
        "\nComplete this task using the tools available to you. "
        "When finished, provide a clear, concise summary of:\n"
        "- What you did\n"
        "- What you found or accomplished\n"
        "- Any files you created or modified\n"
        "- Any issues encountered\n\n"
        "Be thorough but concise -- your response is returned to the "
        "parent agent as a summary."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Child agent builder
# ---------------------------------------------------------------------------

def _build_child_agent(
    goal: str,
    context: Optional[str],
    parent_agent: Any,
    child_session_id: str,
    on_event: Optional[Any] = None,
) -> Any:
    """Build a child AIAgent on the main thread (thread-safe construction).

    Returns the constructed child agent without running it.
    """
    from ..agent.core import AIAgent

    child_prompt = _build_child_system_prompt(goal, context)

    # Filter out blocked tools from the parent's tool list.
    parent_tools = parent_agent.tools
    child_tools = [
        t for t in parent_tools if t.name not in DELEGATE_BLOCKED_TOOLS
    ]

    # Build a minimal cfg for the child that reflects the filtered tools.
    # The child inherits the parent's config but with a tool restriction.
    child_cfg = dict(parent_agent.cfg)
    child_cfg = {
        **child_cfg,
        "tools": {
            "enabled": [t.name for t in child_tools],
            "disabled": [],
        },
    }

    max_iter = _get_max_iterations()

    child = AIAgent(
        cfg=child_cfg,
        profile=parent_agent.profile,
        registry=parent_agent.registry,
        state=parent_agent.state,
        session_id=child_session_id,
        model=parent_agent.model,
        system_prompt=child_prompt,
        max_iterations=max_iter,
        stream=False,
        on_event=on_event,
    )

    return child


# ---------------------------------------------------------------------------
# Single child runner
# ---------------------------------------------------------------------------

def _run_single_child(
    child: Any,
    goal: str,
    child_id: str,
    parent_agent: Any,
    timeout: float,
) -> Dict[str, Any]:
    """Run a pre-built child agent in the current thread.

    Returns a structured result dict.
    """
    start = time.monotonic()

    # Emit subagent start event to parent.
    parent_agent._emit("subagent_event", {
        "subagent_id": child_id,
        "goal": goal,
        "event_kind": "subagent.start",
        "payload": {"goal": goal},
    })

    try:
        result = child.run_turn(goal)
        duration = round(time.monotonic() - start, 2)

        summary = result.final_text or ""

        # Emit completion event.
        parent_agent._emit("subagent_event", {
            "subagent_id": child_id,
            "goal": goal,
            "event_kind": "subagent.complete",
            "payload": {
                "status": "completed",
                "summary": summary[:500],
                "duration_seconds": duration,
            },
        })

        return {
            "status": "completed",
            "summary": summary,
            "duration_seconds": duration,
            "iterations": result.iterations,
            "usage": result.usage,
        }

    except Exception as exc:
        duration = round(time.monotonic() - start, 2)
        logger.exception("subagent %s failed", child_id)

        parent_agent._emit("subagent_event", {
            "subagent_id": child_id,
            "goal": goal,
            "event_kind": "subagent.complete",
            "payload": {
                "status": "failed",
                "error": str(exc),
                "duration_seconds": duration,
            },
        })

        return {
            "status": "failed",
            "error": str(exc),
            "duration_seconds": duration,
        }


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

_DELEGATE_DESCRIPTION = (
    "Spawn one or more subagents to work on tasks in isolated contexts. "
    "Each subagent gets its own conversation and toolset. "
    "Only the final summary is returned -- intermediate tool results "
    "never enter your context window.\n\n"
    "TWO MODES (one of 'goal' or 'tasks' is required):\n"
    "1. Single task: provide 'goal' (+ optional context)\n"
    "2. Batch (parallel): provide 'tasks' array with up to {max_children} "
    "items concurrently.\n\n"
    "WHEN TO USE delegate_task:\n"
    "- Reasoning-heavy subtasks (debugging, code review, research)\n"
    "- Tasks that would flood your context with intermediate data\n"
    "- Parallel independent workstreams\n\n"
    "WHEN NOT TO USE:\n"
    "- Single tool call -- just call the tool directly\n"
    "- Tasks needing user interaction -- subagents cannot use clarify\n\n"
    "IMPORTANT:\n"
    "- Subagents have NO memory of your conversation. Pass all relevant "
    "info via the 'context' field.\n"
    "- Subagents CANNOT call: delegate_task, memory.\n"
    "- Each subagent gets its own session (separate conversation history).\n"
    "- Results are always returned as an array, one entry per task."
)


@registry.register
class DelegateTaskTool(Tool):
    """Spawn child agents to handle delegated tasks in parallel."""

    name = "delegate_task"
    description = _DELEGATE_DESCRIPTION.format(
        max_children=DEFAULT_MAX_CONCURRENT
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "What the subagent should accomplish. Be specific and "
                    "self-contained -- the subagent knows nothing about your "
                    "conversation history."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Background information the subagent needs: file paths, "
                    "error messages, project structure, constraints."
                ),
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "Task goal",
                        },
                        "context": {
                            "type": "string",
                            "description": "Task-specific context",
                        },
                    },
                    "required": ["goal"],
                },
                "description": (
                    "Batch mode: tasks to run in parallel (up to {max_children}). "
                    "Each gets its own subagent. When provided, top-level "
                    "goal/context are ignored."
                ).format(max_children=DEFAULT_MAX_CONCURRENT),
            },
        },
        "required": [],
    }

    def run(
        self,
        goal: Optional[str] = None,
        context: Optional[str] = None,
        tasks: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> ToolResult:
        # The parent agent is passed via the registry call kwargs.
        parent_agent = kwargs.get("parent_agent")
        if parent_agent is None:
            return ToolResult.failure(
                "delegate_task requires a parent agent context."
            )

        # Validate inputs.
        max_children = _get_max_concurrent()

        if tasks and isinstance(tasks, list):
            if len(tasks) > max_children:
                return ToolResult.failure(
                    f"Too many tasks: {len(tasks)} provided, but "
                    f"max_concurrent_children is {max_children}."
                )
            task_list = tasks
        elif goal and isinstance(goal, str) and goal.strip():
            task_list = [{"goal": goal, "context": context}]
        else:
            return ToolResult.failure(
                "Provide either 'goal' (single task) or 'tasks' (batch)."
            )

        if not task_list:
            return ToolResult.failure("No tasks provided.")

        # Validate each task has a goal.
        for i, task in enumerate(task_list):
            if not isinstance(task, dict):
                return ToolResult.failure(
                    f"Task {i} must be an object, got {type(task).__name__}."
                )
            if not task.get("goal", "").strip():
                return ToolResult.failure(f"Task {i} is missing a 'goal'.")

        timeout = _get_child_timeout()
        n_tasks = len(task_list)
        results: List[Dict[str, Any]] = []

        if n_tasks == 1:
            # Single task -- run directly (no thread pool overhead).
            t = task_list[0]
            child_id = f"sa-{uuid.uuid4().hex[:8]}"
            child_session_id = f"delegate-{uuid.uuid4().hex[:12]}"

            child = _build_child_agent(
                goal=t["goal"],
                context=t.get("context"),
                parent_agent=parent_agent,
                child_session_id=child_session_id,
            )

            result = _run_single_child(
                child=child,
                goal=t["goal"],
                child_id=child_id,
                parent_agent=parent_agent,
                timeout=timeout,
            )
            results.append(result)
        else:
            # Batch -- run in parallel.
            with ThreadPoolExecutor(max_workers=max_children) as executor:
                futures = {}
                for i, t in enumerate(task_list):
                    child_id = f"sa-{i}-{uuid.uuid4().hex[:8]}"
                    child_session_id = f"delegate-{uuid.uuid4().hex[:12]}"

                    child = _build_child_agent(
                        goal=t["goal"],
                        context=t.get("context"),
                        parent_agent=parent_agent,
                        child_session_id=child_session_id,
                    )

                    future = executor.submit(
                        _run_single_child,
                        child=child,
                        goal=t["goal"],
                        child_id=child_id,
                        parent_agent=parent_agent,
                        timeout=timeout,
                    )
                    futures[future] = i

                # Collect results.
                from concurrent.futures import wait as cf_wait, FIRST_COMPLETED
                pending = set(futures.keys())
                while pending:
                    done, pending = cf_wait(
                        pending, timeout=0.5, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        try:
                            entry = future.result()
                        except Exception as exc:
                            idx = futures[future]
                            entry = {
                                "status": "error",
                                "error": str(exc),
                                "duration_seconds": 0,
                            }
                        results.append(entry)

        total_duration = sum(r.get("duration_seconds", 0) for r in results)

        return ToolResult.success(json.dumps({
            "results": results,
            "total_duration_seconds": round(total_duration, 2),
        }, ensure_ascii=False))
