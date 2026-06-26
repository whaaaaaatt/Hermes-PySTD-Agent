"""Cron job management tool — lets the model create and manage scheduled jobs.

Single action-based tool that dispatches to :mod:`hermeslite.cron` functions.
Jobs can run shell commands or agent prompts. The cron agent has access to all
tools including ``skills_list`` and ``skills_view`` for dynamic skill loading.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..cron import (
    create_job,
    load_jobs,
    pause_job,
    remove_job,
    resume_job,
    trigger_job,
    update_job,
)
from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job ID resolution
# ---------------------------------------------------------------------------

def _resolve_job_id(job_id: str) -> Optional[str]:
    """Resolve a job reference to a canonical 12-char hex ID.

    Tries exact ID match first, then case-insensitive name search.
    Returns the canonical ID or ``None`` if not found.
    """
    jobs = load_jobs()
    # Exact ID match.
    for j in jobs:
        if j.id == job_id:
            return j.id
    # Case-insensitive name match.
    matches = [j for j in jobs if j.name.lower() == job_id.lower()]
    if len(matches) == 1:
        return matches[0].id
    return None


def _resolve_job_id_strict(job_id: str) -> ToolResult:
    """Resolve job ID and return a ToolResult.failure if ambiguous or missing."""
    jobs = load_jobs()
    # Exact ID match.
    for j in jobs:
        if j.id == job_id:
            return ToolResult.success(j.id)
    # Case-insensitive name match.
    matches = [j for j in jobs if j.name.lower() == job_id.lower()]
    if len(matches) == 0:
        return ToolResult.failure(
            f"Job '{job_id}' not found. Use cron(action='list') to see available jobs."
        )
    if len(matches) > 1:
        ids = ", ".join(f"{m.id} ({m.name})" for m in matches)
        return ToolResult.failure(
            f"Ambiguous name '{job_id}' matches multiple jobs: {ids}. "
            "Use the exact job ID instead."
        )
    return ToolResult.success(matches[0].id)


# ---------------------------------------------------------------------------
# Job formatting
# ---------------------------------------------------------------------------

def _format_job(job: Any) -> Dict[str, Any]:
    """Format a Job dataclass into a compact dict for the model."""
    result: Dict[str, Any] = {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "state": job.state,
    }
    if job.prompt:
        preview = job.prompt[:120] + "..." if len(job.prompt) > 120 else job.prompt
        result["prompt_preview"] = preview
    if job.command:
        result["command"] = job.command
    result["schedule"] = job.schedule_display or "?"
    repeat_times = job.repeat.get("times") if job.repeat else None
    repeat_done = job.repeat.get("completed", 0) if job.repeat else 0
    if repeat_times is None:
        result["repeat"] = "forever"
    else:
        result["repeat"] = f"{repeat_done}/{repeat_times}"
    if job.model:
        result["model"] = job.model
    if job.provider:
        result["provider"] = job.provider
    if job.workdir:
        result["workdir"] = job.workdir
    if job.next_run_at:
        result["next_run_at"] = job.next_run_at
    if job.last_run_at:
        result["last_run_at"] = job.last_run_at
    if job.last_status:
        result["last_status"] = job.last_status
    if job.last_error:
        result["last_error"] = job.last_error
    return result


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

_CRON_DESCRIPTION = """\
Manage scheduled cron jobs. Skills are available via skills_list/skills_view.

Actions:
  create  — Schedule a new job. Requires 'schedule' and either 'prompt' or 'command'.
            Schedule formats: '30m' (one-shot), 'every 2h' (recurring),
            '0 9 * * *' (cron expr), '2026-06-01T09:00' (ISO one-shot).
  list    — Show all jobs.
  update  — Change a job's fields. Requires 'job_id' + at least one field.
  remove  — Delete a job and its output. Requires 'job_id'.
  pause   — Pause a job. Requires 'job_id'.
  resume  — Resume a paused job. Requires 'job_id'.
  run     — Trigger immediate execution. Requires 'job_id'.

To use skills in a cron job, include in the prompt:
  "Use skills_list to discover available skills, then skills_view(name='...') \
to load relevant ones before starting work."

Never guess job IDs — always list first to find the correct ID or name."""


class CronTool(Tool):
    name = "cron"
    description = _CRON_DESCRIPTION
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "REQUIRED. One of: create, list, update, remove, pause, resume, run."
                ),
            },
            "job_id": {
                "type": "string",
                "description": (
                    "Job ID (12-char hex) or job name. "
                    "Required for update/remove/pause/resume/run."
                ),
            },
            "name": {
                "type": "string",
                "description": "Human-friendly name for the job.",
            },
            "schedule": {
                "type": "string",
                "description": (
                    "REQUIRED for create. Formats: '30m', 'every 2h', "
                    "'0 9 * * *', '2026-06-01T09:00'."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Self-contained prompt for the agent to execute. "
                    "Use skills_list/skills_view to load skills if needed."
                ),
            },
            "command": {
                "type": "string",
                "description": (
                    "Shell command to run (alternative to prompt). "
                    "Mutually exclusive with prompt."
                ),
            },
            "repeat": {
                "type": "integer",
                "description": (
                    "Max repeat count. Omit for infinite (recurring) or 1 (one-shot)."
                ),
            },
            "model": {
                "type": "string",
                "description": "Per-job model override.",
            },
            "provider": {
                "type": "string",
                "description": "Per-job provider override.",
            },
            "workdir": {
                "type": "string",
                "description": "Working directory for the job.",
            },
        },
        "required": ["action"],
    }

    def run(
        self,
        action: str = "",
        job_id: str = "",
        name: str = "",
        schedule: str = "",
        prompt: str = "",
        command: str = "",
        repeat: Optional[int] = None,
        model: str = "",
        provider: str = "",
        workdir: str = "",
        **_: Any,
    ) -> ToolResult:
        action = (action or "").strip().lower()

        if action == "create":
            return self._create(schedule, prompt, command, name, repeat, model, provider, workdir)
        if action == "list":
            return self._list()
        if action == "update":
            return self._update(job_id, name, schedule, prompt, command, repeat, model, provider, workdir)
        if action == "remove":
            return self._remove(job_id)
        if action == "pause":
            return self._pause(job_id)
        if action == "resume":
            return self._resume(job_id)
        if action in ("run", "trigger"):
            return self._run(job_id)
        return ToolResult.failure(
            f"Unknown action '{action}'. "
            "Valid actions: create, list, update, remove, pause, resume, run."
        )

    # -- action handlers ----------------------------------------------------

    def _create(
        self,
        schedule: str,
        prompt: str,
        command: str,
        name: str,
        repeat: Optional[int],
        model: str,
        provider: str,
        workdir: str,
    ) -> ToolResult:
        if not schedule:
            return ToolResult.failure("'schedule' is required for create.")
        if not prompt and not command:
            return ToolResult.failure("Either 'prompt' or 'command' is required for create.")
        if prompt and command:
            return ToolResult.failure("Provide either 'prompt' or 'command', not both.")

        try:
            job = create_job(
                name=name or (prompt[:40] if prompt else command[:40]),
                schedule=schedule,
                prompt=prompt,
                command=command or None,
                model=model or None,
                provider=provider or None,
                workdir=workdir or None,
                repeat_times=repeat,
            )
            return ToolResult.success(json.dumps({
                "success": True,
                "job_id": job.id,
                "name": job.name,
                "schedule": job.schedule_display,
                "next_run_at": job.next_run_at,
                "message": f"Cron job '{job.name}' created.",
            }, indent=2))
        except ValueError as exc:
            return ToolResult.failure(str(exc))

    def _list(self) -> ToolResult:
        jobs = load_jobs()
        if not jobs:
            return ToolResult.success(json.dumps({
                "success": True,
                "count": 0,
                "jobs": [],
                "message": "No cron jobs.",
            }, indent=2))
        formatted = [_format_job(j) for j in jobs]
        return ToolResult.success(json.dumps({
            "success": True,
            "count": len(formatted),
            "jobs": formatted,
        }, indent=2))

    def _update(
        self,
        job_id: str,
        name: str,
        schedule: str,
        prompt: str,
        command: str,
        repeat: Optional[int],
        model: str,
        provider: str,
        workdir: str,
    ) -> ToolResult:
        if not job_id:
            return ToolResult.failure("'job_id' is required for update.")

        resolved = _resolve_job_id_strict(job_id)
        if not resolved.ok:
            return resolved
        canonical_id = resolved.data

        updates: Dict[str, Any] = {}
        if name:
            updates["name"] = name
        if schedule:
            updates["schedule"] = schedule
        if prompt:
            updates["prompt"] = prompt
        if command:
            updates["command"] = command
        if repeat is not None:
            updates["repeat_times"] = repeat if repeat > 0 else None
        if model:
            updates["model"] = model
        if provider:
            updates["provider"] = provider
        if workdir:
            updates["workdir"] = workdir

        if not updates:
            return ToolResult.failure("No fields to update. Provide at least one of: name, schedule, prompt, command, repeat, model, provider, workdir.")

        updated = update_job(canonical_id, updates)
        if updated is None:
            return ToolResult.failure(f"Job '{job_id}' not found.")
        return ToolResult.success(json.dumps({
            "success": True,
            "job": _format_job(updated),
            "message": f"Cron job '{updated.name}' updated.",
        }, indent=2))

    def _remove(self, job_id: str) -> ToolResult:
        if not job_id:
            return ToolResult.failure("'job_id' is required for remove.")

        resolved = _resolve_job_id_strict(job_id)
        if not resolved.ok:
            return resolved
        canonical_id = resolved.data

        # Get job info before removing for the response message.
        jobs = load_jobs()
        job_name = ""
        for j in jobs:
            if j.id == canonical_id:
                job_name = j.name
                break

        removed = remove_job(canonical_id)
        if not removed:
            return ToolResult.failure(f"Failed to remove job '{job_id}'.")
        return ToolResult.success(json.dumps({
            "success": True,
            "message": f"Cron job '{job_name}' removed.",
        }, indent=2))

    def _pause(self, job_id: str) -> ToolResult:
        if not job_id:
            return ToolResult.failure("'job_id' is required for pause.")

        resolved = _resolve_job_id_strict(job_id)
        if not resolved.ok:
            return resolved
        canonical_id = resolved.data

        updated = pause_job(canonical_id)
        if updated is None:
            return ToolResult.failure(f"Job '{job_id}' not found.")
        return ToolResult.success(json.dumps({
            "success": True,
            "job": _format_job(updated),
            "message": f"Cron job '{updated.name}' paused.",
        }, indent=2))

    def _resume(self, job_id: str) -> ToolResult:
        if not job_id:
            return ToolResult.failure("'job_id' is required for resume.")

        resolved = _resolve_job_id_strict(job_id)
        if not resolved.ok:
            return resolved
        canonical_id = resolved.data

        updated = resume_job(canonical_id)
        if updated is None:
            return ToolResult.failure(f"Job '{job_id}' not found.")
        return ToolResult.success(json.dumps({
            "success": True,
            "job": _format_job(updated),
            "message": f"Cron job '{updated.name}' resumed.",
        }, indent=2))

    def _run(self, job_id: str) -> ToolResult:
        if not job_id:
            return ToolResult.failure("'job_id' is required for run.")

        resolved = _resolve_job_id_strict(job_id)
        if not resolved.ok:
            return resolved
        canonical_id = resolved.data

        updated = trigger_job(canonical_id)
        if updated is None:
            return ToolResult.failure(f"Job '{job_id}' not found.")
        return ToolResult.success(json.dumps({
            "success": True,
            "job": _format_job(updated),
            "message": f"Cron job '{updated.name}' triggered for immediate execution.",
        }, indent=2))


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

registry.register(CronTool())
