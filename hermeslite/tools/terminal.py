"""Terminal / shell tool.

Subprocess execution with timeout, sudo support, output truncation,
ANSI stripping, secret redaction, and exit code interpretation.

Sudo flow:
  1. Check $SUDO_PASSWORD env var
 2. Check session cache (thread-local)
 3. Prompt user (CLI: /dev/tty, Web: SSE modal)
 4. Transform sudo → sudo -S -p '' and pipe password via stdin

Timeout: configurable per-call, default 0 (no timeout). On timeout the
tool returns a failure result with a message the model can act on.

Reject: user can reject sudo with an optional explanation that is
forwarded to the model.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ANSI escape stripping
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"   # CSI
    r"|\][\s\S]*?(?:\x07|\x1b\\)"                # OSC
    r"|[PX^_][\s\S]*?(?:\x1b\\)"                 # DCS/SOS/PM/APC
    r"|[\x20-\x2f]+[\x30-\x7e]"                  # nF escapes
    r"|[\x30-\x7e]"                               # Fp/Fe/Fs
    r")"
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]" # 8-bit CSI
    r"|\x9d[\s\S]*?(?:\x07|\x9c)"                # 8-bit OSC
    r"|[\x80-\x9f]",                              # C1 controls
    re.DOTALL,
)
_HAS_ESC = re.compile(r"[\x1b\x80-\x9f]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    if not text or not _HAS_ESC.search(text):
        return text
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Secret redaction — catch common patterns that leak into command output
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    (re.compile(r'(?i)(api[_-]?key|token|secret|password|passwd|credential)\s*[=:]\s*\S+'), "secret"),
    (re.compile(r'(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*'), "auth token"),
    (re.compile(r'(?i)(AKIA|ASIA)[A-Z0-9]{16}'), "AWS access key"),
    (re.compile(r'gh[pousr]_[A-Za-z0-9_]{36,}'), "GitHub token"),
    (re.compile(r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----'), "private key"),
    (re.compile(r'(?i)(mysql|postgres|mongodb|redis)://[^:]+:\S+@'), "database password"),
]


def _redact_secrets(text: str) -> str:
    """Replace common secret patterns with a descriptive redaction marker.

    The marker tells the model what was redacted without revealing the
    value: ``[REDACTED: secret]``, ``[REDACTED: auth token]``, etc.
    """
    if not text:
        return text
    for pat, kind in _SECRET_PATTERNS:
        text = pat.sub(f"[REDACTED: {kind}]", text)
    return text


# ---------------------------------------------------------------------------
# Exit code interpretation — helpful notes for common tools
# ---------------------------------------------------------------------------

_EXIT_NOTES = {
    ("grep", 1): "No matches found (not an error)",
    ("rg", 1): "No matches found (not an error)",
    ("ag", 1): "No matches found (not an error)",
    ("ack", 1): "No matches found (not an error)",
    ("diff", 1): "Files differ (expected)",
    ("find", 1): "Some directories inaccessible",
    ("test", 1): "Condition false",
    ("[", 1): "Condition false",
    ("curl", 6): "Could not resolve host",
    ("curl", 7): "Failed to connect to host",
    ("curl", 22): "HTTP error (4xx/5xx)",
    ("curl", 28): "Request timed out",
    ("git", 1): "Non-zero exit (often normal)",
}


def _extract_base_command(command: str) -> str:
    """Extract the base command name from a shell command line."""
    cmd = command.strip()
    while cmd and "=" in cmd.split()[0]:
        cmd = " ".join(cmd.split()[1:])
    if not cmd:
        return ""
    first = cmd.split()[0]
    base = os.path.basename(first)
    if "|" in cmd:
        parts = cmd.split("|")
        last = parts[-1].strip()
        while last and "=" in last.split()[0]:
            last = " ".join(last.split()[1:])
        if last:
            base = os.path.basename(last.split()[0])
    return base


def _interpret_exit_code(command: str, exit_code: int) -> Optional[str]:
    """Return a helpful note for non-zero exit codes that aren't real errors."""
    if exit_code == 0:
        return None
    base = _extract_base_command(command)
    return _EXIT_NOTES.get((base, exit_code))


# ---------------------------------------------------------------------------
# Output truncation — 40% head + 60% tail
# ---------------------------------------------------------------------------


def _truncate_output(text: str, max_bytes: int) -> str:
    """Truncate output keeping both head and tail, with a notice."""
    if max_bytes <= 0 or len(text.encode("utf-8")) <= max_bytes:
        return text
    head_chars = int(max_bytes * 0.4)
    tail_chars = max_bytes - head_chars
    omitted = len(text) - head_chars - tail_chars
    notice = (
        f"\n\n... [OUTPUT TRUNCATED — {omitted} chars omitted "
        f"out of {len(text)} total] ...\n\n"
    )
    return text[:head_chars] + notice + text[-tail_chars:]


# ---------------------------------------------------------------------------
# Sudo support — password cache, command transform, stdin pipe
# ---------------------------------------------------------------------------

# Thread-local sudo password cache (session-scoped)
sudo_cache_tls = threading.local()

_SUDO_RE = re.compile(r'(?:^|[;&|`\n]|\|\||&&)\s*sudo\b')


def _get_cached_sudo_password() -> str:
    """Return cached sudo password for current thread, or empty."""
    return getattr(sudo_cache_tls, "password", "")


def _set_cached_sudo_password(password: str) -> None:
    """Cache sudo password for current thread."""
    sudo_cache_tls.password = password


def _has_sudo(command: str) -> bool:
    """Return True if the command contains a bare sudo invocation."""
    return bool(_SUDO_RE.search(command))


def _transform_sudo(command: str) -> str:
    """Rewrite bare 'sudo' to 'sudo -S -p ''' for stdin password pipe."""
    return re.sub(
        r'((?:^|[;&|`\n]|\|\||&&)\s*)sudo\b',
        r"\1sudo -S -p ''",
        command,
    )


def _pipe_stdin(proc: subprocess.Popen, data: str) -> None:
    """Write data to proc.stdin on a daemon thread to avoid pipe-buffer deadlocks.

    NOTE: We do NOT close stdin here — ``proc.communicate()`` closes it
    after reading stdout/stderr.  Closing early causes "I/O operation on
    closed file" when ``communicate()`` tries to flush.
    """
    def _write():
        try:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            target = getattr(proc.stdin, "buffer", proc.stdin)
            target.write(raw)
        except (BrokenPipeError, OSError):
            pass
    threading.Thread(target=_write, daemon=True).start()


def _prompt_sudo_password(emit_fn: Any = None, timeout: float = 120.0) -> dict:
    """Get sudo password from user. Tries Web SSE first, then CLI /dev/tty.

    Returns {"action": "password"|"reject"|"timeout", "password": str, "message": str}.
    """
    # Try Web UI prompt if emit_fn is available
    if emit_fn:
        from .approval import submit_web_sudo
        return submit_web_sudo(command="", emit_fn=emit_fn, timeout=timeout)

    # CLI prompt
    from .approval import prompt_sudo_cli
    return prompt_sudo_cli(timeout=timeout)


def _sudo_nopasswd_works() -> bool:
    """Return True if sudo currently works without password."""
    try:
        probe = subprocess.run(
            ["sudo", "-n", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return probe.returncode == 0
    except Exception:
        return False


def _get_sudo_password(emit_fn: Any = None, timeout: float = 120.0) -> dict:
    """Resolve sudo password. Returns the same dict shape as _prompt_sudo_password.

    Resolution order:
      1. $SUDO_PASSWORD env var
      2. Thread-local cache  (if cached, skip nopasswd probe — the user
         explicitly provided a password earlier, so we always use it)
      3. Probe sudo -n (nopasswd configured)
      4. Prompt user
    """
    # 1. Env var
    env_pw = os.environ.get("SUDO_PASSWORD", "")
    if env_pw:
        _set_cached_sudo_password(env_pw)
        return {"action": "password", "password": env_pw, "message": ""}

    # 2. Cache — once a password is cached, always use it.  This avoids
    #    the nopasswd probe overriding a previously-entered password
    #    (which caused "first time works, subsequent times fail" when
    #    the sudo ticket expired between calls).
    cached = _get_cached_sudo_password()
    if cached:
        return {"action": "password", "password": cached, "message": ""}

    # 3. Sudo nopasswd (only when no password has been cached yet)
    if _sudo_nopasswd_works():
        return {"action": "password", "password": "", "message": ""}

    # 4. Prompt user
    result = _prompt_sudo_password(emit_fn=emit_fn, timeout=timeout)
    if result["action"] == "password" and result["password"]:
        _set_cached_sudo_password(result["password"])
    return result


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

_DEFAULT_MAX_OUTPUT = 50_000
_MAX_RETRIES = 3


class TerminalTool(Tool):
    name = "terminal"
    description = (
        "Run a shell command. `command` is a string; the tool uses the "
        "system shell (`bash -c` on POSIX, `cmd /c` on Windows). Set "
        "`cwd` to run in a different directory. `timeout` defaults to 0 "
        "(no timeout); set to a positive value for a per-command timeout "
        "in seconds. `max_output` caps stdout+stderr at 50_000 bytes. "
        "Returns (exit_code, stdout+stderr). ANSI escapes are stripped "
        "and secrets are redacted in the output. Sudo commands are "
        "handled transparently — the user is prompted for a password "
        "when needed (cached for the session)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command line."},
            "cwd": {"type": "string", "description": "Working directory. Default cwd."},
            "timeout": {"type": "integer", "description": "Timeout in seconds. 0 = no timeout (default)."},
            "max_output": {"type": "integer", "description": "Output cap in bytes. Default 50000."},
        },
        "required": ["command"],
    }

    def run(
        self,
        command: str,
        cwd: str = "",
        timeout: int = 0,
        max_output: int = _DEFAULT_MAX_OUTPUT,
        **_: Any,
    ) -> ToolResult:
        if not command.strip():
            return ToolResult.failure("empty command")

        # Approval check for dangerous commands.
        from .approval import check_terminal_command
        emit_fn = getattr(self, '_emit_fn', None)
        approvals_enabled = True
        cfg_val = None
        try:
            from ..config import load_config
            loaded_cfg = load_config()
            approvals_enabled = loaded_cfg.get("approvals", {}).get("enabled", True)
            cfg_val = loaded_cfg
        except Exception:  # noqa: BLE001
            pass
        check = check_terminal_command(
            command,
            emit_fn=emit_fn,
            approvals_enabled=approvals_enabled,
            cfg=cfg_val,
        )
        if not check["approved"]:
            return ToolResult.failure(check["message"])

        workdir = os.path.expanduser(cwd) if cwd else None
        if not workdir:
            from ..agent.runtime_cwd import resolve_agent_cwd
            workdir = str(resolve_agent_cwd())
        if workdir and not Path(workdir).is_dir():
            return ToolResult.failure(f"cwd not found: {workdir}")

        # --- Sudo handling ---
        sudo_stdin: Optional[str] = None
        if _has_sudo(command):
            sudo_result = _get_sudo_password(emit_fn=emit_fn, timeout=float(timeout) if timeout > 0 else 120.0)
            if sudo_result["action"] == "password":
                command = _transform_sudo(command)
                if sudo_result["password"]:
                    sudo_stdin = sudo_result["password"] + "\n"
                # If password is empty (nopasswd), run without stdin pipe.
            elif sudo_result["action"] == "reject":
                reason = sudo_result.get("message", "")
                return ToolResult.failure(
                    f"Sudo rejected by user"
                    + (f": {reason}" if reason else "")
                    + ". The command was not executed."
                )
            elif sudo_result["action"] == "timeout":
                return ToolResult.failure(
                    "Sudo prompt timed out — user did not respond. "
                    "The command was not executed. You can retry or ask "
                    "the user to configure $SUDO_PASSWORD to avoid prompts."
                )

        # --- Build argv ---
        is_windows = os.name == "nt"
        if is_windows:
            argv = ["cmd", "/c", command]
        else:
            argv = ["bash", "-c", command]

        # --- Execute with optional stdin pipe ---
        effective_timeout = timeout if timeout > 0 else None

        if sudo_stdin is not None:
            # Use Popen to pipe password to stdin
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=workdir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                _pipe_stdin(proc, sudo_stdin)
                try:
                    stdout, _ = proc.communicate(timeout=effective_timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    return ToolResult.failure(
                        f"timeout after {timeout}s — command was killed"
                    )
                out = stdout or ""
                returncode = proc.returncode
            except FileNotFoundError as exc:
                return ToolResult.failure(f"shell not found: {exc}")
            except OSError as exc:
                return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        else:
            # No sudo stdin — use subprocess.run (simpler)
            last_error = None
            for attempt in range(_MAX_RETRIES):
                try:
                    proc = subprocess.run(
                        argv,
                        cwd=workdir,
                        capture_output=True,
                        text=True,
                        timeout=effective_timeout,
                        check=False,
                    )
                    break
                except subprocess.TimeoutExpired as exc:
                    captured = len((exc.stdout or b'') + (exc.stderr or b''))
                    return ToolResult.failure(
                        f"timeout after {timeout}s (captured {captured} bytes)"
                    )
                except FileNotFoundError as exc:
                    return ToolResult.failure(f"shell not found: {exc}")
                except OSError as exc:
                    last_error = exc
                    if attempt < _MAX_RETRIES - 1:
                        wait = 2 ** (attempt + 1)
                        logger.warning(
                            "terminal: transient error (attempt %d/%d), retrying in %ds: %s",
                            attempt + 1, _MAX_RETRIES, wait, exc,
                        )
                        time.sleep(wait)
                        continue
                    return ToolResult.failure(f"{type(exc).__name__}: {exc}")
            else:
                return ToolResult.failure(
                    f"{type(last_error).__name__}: {last_error} (after {_MAX_RETRIES} retries)"
                )
            out = (proc.stdout or "") + (proc.stderr or "")
            returncode = proc.returncode

        # --- Post-process output ---
        out = _strip_ansi(out)
        out = _redact_secrets(out)
        out = _truncate_output(out, max_output)

        exit_note = _interpret_exit_code(command, returncode)

        body = f"[exit {returncode}]\n{out}"
        if exit_note:
            body += f"\n({exit_note})"

        if returncode == 0:
            return ToolResult.success(body)
        return ToolResult(data=body, ok=False, error=f"exit {returncode}")


registry.register(TerminalTool())
