"""Cron-style job scheduler with JSON persistence (stdlib only).

A scheduler that accepts crontab-style expressions, interval strings,
duration strings, and ISO timestamps. Jobs are persisted to
``~/.hermes-lite/cron/jobs.json`` and execution outputs are saved to
``~/.hermes-lite/cron/output/{job_id}/{timestamp}.md``.

Schedule formats
----------------

  - **Cron**: ``0 9 * * *`` — standard 5-field crontab expression
  - **Interval**: ``every 30m``, ``every 2h`` — recurring at fixed intervals
  - **Duration**: ``30m``, ``2h``, ``1d`` — one-shot, fires after the given delay
  - **ISO timestamp**: ``2026-02-03T14:00`` — one-shot, fires at the given time

Components
----------

  - :class:`Job` — a single scheduled task with full lifecycle metadata
  - :class:`CronSpec` — parsed 5-field crontab expression
  - :class:`Scheduler` — background thread that dispatches due jobs
  - :func:`parse_schedule` — parse any supported schedule format
  - :func:`compute_next_run` — compute next fire time from a schedule
  - :func:`load_jobs` / :func:`save_jobs` — JSON persistence
  - :func:`create_job` / :func:`update_job` / :func:`remove_job` — CRUD
  - :func:`run_job` — execute a job (shell command or agent prompt)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _cron_dir() -> Path:
    """Return ``~/.hermes-lite/cron/``, creating if needed."""
    from .paths import get_hermes_home
    d = get_hermes_home() / "cron"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jobs_path() -> Path:
    return _cron_dir() / "jobs.json"


def _output_dir() -> Path:
    d = _cron_dir() / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Job data model
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Job:
    """A single scheduled task with full lifecycle metadata."""

    id: str = ""
    name: str = ""
    prompt: str = ""
    schedule: Dict[str, Any] = field(default_factory=dict)
    schedule_display: str = ""
    enabled: bool = True
    state: str = "scheduled"  # scheduled | paused | completed | error | running
    model: Optional[str] = None
    provider: Optional[str] = None
    command: Optional[str] = None  # shell command (alternative to prompt)
    repeat: Dict[str, Any] = field(default_factory=lambda: {"times": None, "completed": 0})
    workdir: Optional[str] = None
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None  # "ok" | "error"
    last_error: Optional[str] = None
    output: Optional[str] = None  # truncated recent output
    created_at: str = ""
    paused_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Job":
        # Accept only known fields to avoid unexpected kwargs.
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)([mhd])$")
_INTERVAL_RE = re.compile(r"^every\s+(\d+)([mhd])$", re.IGNORECASE)
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})?$")

_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}


def parse_schedule(expr: str) -> Dict[str, Any]:
    """Parse a schedule expression into a structured dict.

    Supported formats:

    - ``"30m"``, ``"2h"``, ``"1d"`` — one-shot duration
    - ``"every 30m"``, ``"every 2h"`` — recurring interval
    - ``"0 9 * * *"`` — 5-field crontab expression
    - ``"2026-02-03T14:00"`` — ISO timestamp (one-shot)

    Raises ``ValueError`` on unrecognised input.
    """
    expr = expr.strip()

    # Duration: "30m", "2h", "1d"
    m = _DURATION_RE.match(expr)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        minutes = n * _UNIT_MINUTES[unit]
        run_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        return {
            "kind": "once",
            "run_at": run_at.isoformat(),
            "display": expr,
        }

    # Interval: "every 30m", "every 2h"
    m = _INTERVAL_RE.match(expr)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        minutes = n * _UNIT_MINUTES[unit]
        return {
            "kind": "interval",
            "minutes": minutes,
            "display": expr,
        }

    # ISO timestamp: "2026-02-03T14:00" or "2026-02-03T14:00:00Z"
    if _ISO_RE.match(expr):
        return {
            "kind": "once",
            "run_at": _normalise_iso(expr),
            "display": expr,
        }

    # Cron expression: try parsing as 5-field crontab
    parts = expr.split()
    if len(parts) == 5:
        try:
            spec = CronSpec.parse(expr)
            return {
                "kind": "cron",
                "expr": expr,
                "display": expr,
            }
        except ValueError:
            pass

    raise ValueError(
        f"unrecognised schedule: {expr!r} "
        "(expected cron expr, 'every Nm/h/d', 'Nm/h/d', or ISO timestamp)"
    )


def _normalise_iso(s: str) -> str:
    """Ensure an ISO timestamp has timezone info (assume UTC if naive)."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # If no timezone offset, assume UTC.
    if "+" not in s[10:] and "-" not in s[10:]:
        s += "+00:00"
    return s


# ---------------------------------------------------------------------------
# CronSpec — parsed 5-field expression
# ---------------------------------------------------------------------------

_FIELD_RE = re.compile(r"^(\d+)-(\d+)(?:/(\d+))?$")
_STEP_RE = re.compile(r"^\*/(\d+)$")


@dataclass(frozen=True)
class CronSpec:
    """A parsed cron expression. Match against :class:`datetime` objects."""

    minute: frozenset
    hour: frozenset
    day: frozenset
    month: frozenset
    weekday: frozenset  # 0=Monday, 6=Sunday (Python convention)

    @classmethod
    def parse(cls, expr: str) -> "CronSpec":
        """Parse a 5-field crontab expression. Raises ``ValueError`` on bad input."""
        parts = expr.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"expected 5 fields (m h dom mon dow), got {len(parts)}: {expr!r}"
            )
        minute = _parse_field(parts[0], 0, 59, "minute")
        hour = _parse_field(parts[1], 0, 23, "hour")
        day = _parse_field(parts[2], 1, 31, "day")
        month = _parse_field(parts[3], 1, 12, "month")
        raw_weekday = _parse_field(parts[4], 0, 7, "weekday")
        weekday = frozenset(((d - 1) % 7) for d in raw_weekday)
        return cls(
            minute=minute, hour=hour, day=day, month=month, weekday=weekday,
        )

    def matches(self, dt: datetime) -> bool:
        return (
            dt.minute in self.minute
            and dt.hour in self.hour
            and dt.day in self.day
            and dt.month in self.month
            and dt.weekday() in self.weekday
        )


def _parse_field(token: str, lo: int, hi: int, name: str) -> frozenset:
    out: set = set()
    for piece in token.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if piece == "*":
            out.update(range(lo, hi + 1))
            continue
        m = _STEP_RE.match(piece)
        if m:
            step = int(m.group(1))
            if step < 1:
                raise ValueError(f"{name}: step must be >= 1, got {step}")
            out.update(range(lo, hi + 1, step))
            continue
        if "-" in piece:
            m = _FIELD_RE.match(piece)
            if not m:
                raise ValueError(f"{name}: bad range {piece!r}")
            a, b, step = int(m.group(1)), int(m.group(2)), m.group(3)
            if not (lo <= a <= hi and lo <= b <= hi and a <= b):
                raise ValueError(f"{name}: range out of bounds {piece!r}")
            if step:
                out.update(range(a, b + 1, int(step)))
            else:
                out.update(range(a, b + 1))
            continue
        try:
            v = int(piece)
        except ValueError as exc:
            raise ValueError(f"{name}: bad token {piece!r}") from exc
        if not (lo <= v <= hi):
            raise ValueError(f"{name}: value {v} out of range [{lo}, {hi}]")
        out.add(v)
    return frozenset(out)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_jobs_lock = threading.Lock()


def load_jobs() -> List[Job]:
    """Load jobs from ``jobs.json``. Returns empty list on missing/corrupt file."""
    path = _jobs_path()
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("cron: cannot read %s: %s", path, exc)
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("cron: corrupt %s: %s", path, exc)
        return []
    if isinstance(data, list):
        # Tolerate bare list format.
        return [Job.from_dict(j) for j in data if isinstance(j, dict)]
    if isinstance(data, dict):
        return [Job.from_dict(j) for j in data.get("jobs", []) if isinstance(j, dict)]
    return []


def save_jobs(jobs: List[Job]) -> None:
    """Atomically write jobs to ``jobs.json``."""
    path = _jobs_path()
    payload = {
        "jobs": [j.to_dict() for j in jobs],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".jobs.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_modify_save(fn: Callable[[List[Job]], List[Job]]) -> List[Job]:
    """Thread-safe load → modify → save cycle. Returns the updated list."""
    with _jobs_lock:
        jobs = load_jobs()
        jobs = fn(jobs)
        save_jobs(jobs)
        return jobs


# ---------------------------------------------------------------------------
# Compute next run
# ---------------------------------------------------------------------------

def compute_next_run(
    schedule: Dict[str, Any],
    last_run_at: Optional[str] = None,
) -> Optional[str]:
    """Compute the next fire time for a schedule. Returns ISO timestamp or None."""
    kind = schedule.get("kind", "cron")

    if kind == "once":
        run_at = schedule.get("run_at")
        if not run_at:
            return None
        # Already in the past? Return None.
        try:
            dt = datetime.fromisoformat(run_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt <= datetime.now(timezone.utc):
                return None
        except (ValueError, TypeError):
            return None
        return run_at

    if kind == "interval":
        minutes = schedule.get("minutes", 60)
        base = datetime.now(timezone.utc)
        if last_run_at:
            try:
                base = datetime.fromisoformat(last_run_at)
                if base.tzinfo is None:
                    base = base.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                base = datetime.now(timezone.utc)
        next_dt = base + timedelta(minutes=minutes)
        return next_dt.isoformat()

    if kind == "cron":
        expr = schedule.get("expr", "")
        if not expr:
            return None
        try:
            spec = CronSpec.parse(expr)
        except ValueError:
            return None
        # Interpret cron expressions in local time so "0 5 * * *" means
        # 05:00 in the user's timezone, not UTC.
        local_tz = datetime.now().astimezone().tzinfo
        after_local = datetime.now(local_tz)
        if last_run_at:
            try:
                after = datetime.fromisoformat(last_run_at)
                if after.tzinfo is None:
                    after = after.replace(tzinfo=timezone.utc)
                after_local = after.astimezone(local_tz)
            except (ValueError, TypeError):
                after_local = datetime.now(local_tz)
        next_local = _next_cron_match(spec, after_local)
        if next_local is None:
            return None
        # Convert to UTC for storage so get_due_jobs() comparisons work.
        return next_local.astimezone(timezone.utc).isoformat()

    return None


def _next_cron_match(spec: CronSpec, after: datetime) -> Optional[datetime]:
    """Find the next datetime > after that matches the cron spec."""
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    candidate = start
    for _ in range(60 * 24 * 366):  # safety cap: ~1 year of minutes
        if spec.matches(candidate):
            return candidate
        candidate += timedelta(minutes=1)
    return None


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------

def create_job(
    *,
    name: str,
    schedule: str,
    prompt: str = "",
    command: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    workdir: Optional[str] = None,
    repeat_times: Optional[int] = None,
) -> Job:
    """Create a new job, persist it, and return it."""
    sched = parse_schedule(schedule)
    now_iso = datetime.now(timezone.utc).isoformat()
    job = Job(
        id=_new_id(),
        name=name,
        prompt=prompt,
        schedule=sched,
        schedule_display=schedule,
        model=model,
        provider=provider,
        command=command,
        repeat={"times": repeat_times, "completed": 0},
        workdir=workdir,
        next_run_at=compute_next_run(sched),
        created_at=now_iso,
    )
    _load_modify_save(lambda jobs: jobs + [job])
    return job


def update_job(job_id: str, updates: Dict[str, Any]) -> Optional[Job]:
    """Update a job's fields. Re-computes next_run_at if schedule changed."""
    immutable = {"id", "created_at"}
    result: List[Job] = []
    updated: Optional[Job] = None

    def _apply(jobs: List[Job]) -> List[Job]:
        nonlocal updated
        for j in jobs:
            if j.id == job_id:
                for k, v in updates.items():
                    if k in immutable:
                        continue
                    if k == "schedule" and isinstance(v, str):
                        j.schedule = parse_schedule(v)
                        j.schedule_display = v
                    elif k == "repeat_times":
                        j.repeat = dict(j.repeat)
                        j.repeat["times"] = v
                    else:
                        setattr(j, k, v)
                # Recompute next_run_at if schedule or state changed.
                if "schedule" in updates or "enabled" in updates:
                    if j.enabled and j.state not in ("completed", "error"):
                        j.next_run_at = compute_next_run(j.schedule, j.last_run_at)
                    else:
                        j.next_run_at = None
                updated = j
            result.append(j)
        return result

    _load_modify_save(_apply)
    return updated


def remove_job(job_id: str) -> bool:
    """Remove a job and its output directory. Returns True if found."""
    removed = [False]

    def _apply(jobs: List[Job]) -> List[Job]:
        nonlocal removed
        new_jobs = []
        for j in jobs:
            if j.id == job_id:
                removed[0] = True
                # Clean up output directory.
                out = _output_dir() / job_id
                if out.is_dir():
                    shutil.rmtree(out, ignore_errors=True)
            else:
                new_jobs.append(j)
        return new_jobs

    _load_modify_save(_apply)
    return removed[0]


def pause_job(job_id: str, reason: str = "") -> Optional[Job]:
    """Pause a job."""
    return update_job(job_id, {
        "enabled": False,
        "state": "paused",
        "paused_at": datetime.now(timezone.utc).isoformat(),
    })


def resume_job(job_id: str) -> Optional[Job]:
    """Resume a paused job."""
    return update_job(job_id, {
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
    })


def trigger_job(job_id: str) -> Optional[Job]:
    """Set next_run_at to now so the job fires on the next tick."""
    return update_job(job_id, {
        "next_run_at": datetime.now(timezone.utc).isoformat(),
    })


def mark_job_run(
    job_id: str, success: bool, error: Optional[str] = None,
    output: Optional[str] = None,
) -> Optional[Job]:
    """Mark a job as having been run. Advances next_run_at for recurring jobs."""
    result: List[Job] = []
    updated: Optional[Job] = None

    def _apply(jobs: List[Job]) -> List[Job]:
        nonlocal updated
        for j in jobs:
            if j.id == job_id:
                now_iso = datetime.now(timezone.utc).isoformat()
                j.last_run_at = now_iso
                j.last_status = "ok" if success else "error"
                j.last_error = error
                j.state = "scheduled" if j.enabled else j.state
                if output is not None:
                    # Truncate to last 4000 chars to keep jobs.json small.
                    j.output = output[-4000:] if len(output) > 4000 else output
                # Advance repeat counter.
                j.repeat = dict(j.repeat)
                if j.repeat["times"] is not None:
                    j.repeat["completed"] = j.repeat.get("completed", 0) + 1
                    if j.repeat["completed"] >= j.repeat["times"]:
                        j.state = "completed"
                        j.next_run_at = None
                # Compute next run for recurring jobs.
                if j.state not in ("completed",):
                    j.next_run_at = compute_next_run(j.schedule, j.last_run_at)
                if j.next_run_at is None and j.state not in ("completed", "paused"):
                    j.state = "error"
                    j.last_error = j.last_error or "could not compute next_run_at"
                updated = j
            result.append(j)
        return result

    _load_modify_save(_apply)
    return updated


def get_due_jobs(now: Optional[datetime] = None) -> List[Job]:
    """Return enabled jobs whose next_run_at <= now."""
    now = now or datetime.now(timezone.utc)
    jobs = load_jobs()
    due = []
    for j in jobs:
        if not j.enabled or j.state in ("completed", "running"):
            continue
        if not j.next_run_at:
            continue
        try:
            nrt = datetime.fromisoformat(j.next_run_at)
            if nrt.tzinfo is None:
                nrt = nrt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if nrt <= now:
            due.append(j)
    return due


# ---------------------------------------------------------------------------
# Output storage
# ---------------------------------------------------------------------------

def save_job_output(job_id: str, output: str) -> Path:
    """Save execution output to ``output/{job_id}/{timestamp}.md``."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = _output_dir() / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ts}.md"
    path.write_text(output, encoding="utf-8")
    # Also clean up old outputs: keep last 20.
    _prune_outputs(out_dir, keep=20)
    return path


def _prune_outputs(out_dir: Path, keep: int = 20) -> None:
    """Remove old output files, keeping the most recent ``keep``."""
    if not out_dir.is_dir():
        return
    files = sorted(out_dir.glob("*.md"), key=lambda p: p.name, reverse=True)
    for f in files[keep:]:
        try:
            f.unlink()
        except OSError:
            pass


def load_job_output(job_id: str) -> str:
    """Load the most recent output for a job. Returns empty string if none."""
    out_dir = _output_dir() / job_id
    if not out_dir.is_dir():
        return ""
    files = sorted(out_dir.glob("*.md"), key=lambda p: p.name, reverse=True)
    if not files:
        return ""
    try:
        return files[0].read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------

def run_job(job: Job, timeout: float = 120.0) -> str:
    """Execute a job and return its output text.

    Two modes:

    - **Shell command** (``job.command`` is set): run via ``subprocess.run``
    - **Agent prompt** (``job.prompt`` is set): run via ``AIAgent.run_turn``

    Returns the combined stdout/stderr or agent output.
    """
    job_id = job.id
    try:
        if job.command:
            output = _run_shell(job.command, job.workdir, timeout)
        elif job.prompt:
            output = _run_agent(job)
        else:
            output = "(no command or prompt specified)"
        save_job_output(job_id, output)
        return output
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.exception("cron: job %s failed", job.name or job_id)
        save_job_output(job_id, f"ERROR: {error_msg}")
        raise


def _run_shell(command: str, workdir: Optional[str], timeout: float) -> str:
    """Execute a shell command and return combined output."""
    cwd = workdir or None
    result = subprocess.run(
        command, shell=True, cwd=cwd,
        capture_output=True, text=True, timeout=timeout,
    )
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"STDERR:\n{result.stderr}")
    if result.returncode != 0:
        parts.append(f"EXIT CODE: {result.returncode}")
    return "\n".join(parts) if parts else "(no output)"


def _run_agent(job: Job) -> str:
    """Run a job through the AIAgent and return the output."""
    from .agent.core import AIAgent
    from .config import load_config
    from .providers import active_profile, load_providers
    from .tools import registry as tool_registry
    from .state import StateStore
    from .paths import get_state_db_path

    cfg = load_config()
    # Apply per-job overrides.
    if job.model:
        cfg.setdefault("model", {})["name"] = job.model
    if job.provider:
        cfg.setdefault("model", {})["provider"] = job.provider

    profile = active_profile(cfg)
    state = StateStore(get_state_db_path())
    run_ts = int(time.time())
    session_id = f"cron_{job.id}_{run_ts}"

    agent = AIAgent(
        cfg=cfg, profile=profile, registry=tool_registry,
        state=state, session_id=session_id, stream=False,
        model=job.model,
    )
    # Name the session with job name + run time for display.
    run_dt = datetime.fromtimestamp(run_ts)
    title = f"{job.name or job.id} @ {run_dt.strftime('%Y-%m-%d %H:%M')}"
    state.update_session(session_id, title=title)

    result = agent.run_turn(job.prompt)
    return result.final_text or "(no response)"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """Background cron scheduler. Runs a daemon thread that checks for due
    jobs every 60 seconds and executes them.
    """

    def __init__(
        self,
        run_job_fn: Optional[Callable[[Job], str]] = None,
        max_concurrent: int = 3,
    ):
        self._run_job_fn = run_job_fn or run_job
        self._max_concurrent = max_concurrent
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running_ids: set = set()
        self._running_lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler daemon thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="cron-scheduler", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- run loop -----------------------------------------------------------

    def _run(self) -> None:
        last_minute = -1
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            sleep_for = 60 - now.second - now.microsecond / 1_000_000
            if sleep_for <= 0:
                sleep_for = 0.5
            if self._stop.wait(sleep_for):
                return
            now = datetime.now(timezone.utc)
            tick = (now.year, now.month, now.day, now.hour, now.minute)
            if tick == last_minute:
                continue
            last_minute = tick
            self._dispatch(now)

    def _dispatch(self, now: datetime) -> None:
        due = get_due_jobs(now)
        for job in due:
            with self._running_lock:
                if job.id in self._running_ids:
                    continue
                if len(self._running_ids) >= self._max_concurrent:
                    break
                self._running_ids.add(job.id)
            # Run in a separate thread so the scheduler isn't blocked.
            t = threading.Thread(
                target=self._execute, args=(job,), daemon=True,
            )
            t.start()

    def _execute(self, job: Job) -> None:
        try:
            # Mark as running.
            update_job(job.id, {"state": "running"})
            output = self._run_job_fn(job)
            mark_job_run(job.id, success=True, output=output)
            logger.info("cron: job %s completed", job.name or job.id)
        except Exception as exc:
            mark_job_run(
                job.id, success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.exception("cron: job %s failed", job.name or job.id)
        finally:
            with self._running_lock:
                self._running_ids.discard(job.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def next_fire(spec: CronSpec, after: Optional[datetime] = None, max_minutes: int = 60 * 24 * 366) -> Optional[datetime]:
    """Return the next datetime >= after that matches the spec.

    ``after`` defaults to ``datetime.now()``. ``max_minutes`` is a
    safety cap. Returns ``None`` if no match within the window.
    """
    start = (after or datetime.now()).replace(second=0, microsecond=0)
    candidate = start
    for _ in range(max_minutes):
        candidate = candidate.replace(second=0, microsecond=0)
        if candidate <= start and not spec.matches(candidate):
            candidate += timedelta(minutes=1)
            continue
        if spec.matches(candidate):
            return candidate
        candidate += timedelta(minutes=1)
    return None
