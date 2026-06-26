"""Tool-risk detection and approval flows.

Ported from hermes-agent-ref/tools/approval.py (simplified). Provides:
- HARDLINE_PATTERNS: unconditional blocklist (rm -rf /, fork bombs, etc.)
- DANGEROUS_PATTERNS: patterns requiring user approval (rm -rf, chmod 777, etc.)
- SENSITIVE_PATHS: system paths blocked for file write tools
- CLI approval prompt + per-session session-scoped approval memory
- Web UI approval via SSE approval_request event + POST /api/approve

No third-party dependencies.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hardline patterns — unconditional block
# ---------------------------------------------------------------------------

HARDLINE_PATTERNS: List[Tuple[str, str]] = [
    (r'\brm\s+(-[^\s]*\s+)*(/|/\*|/ \*)(\s|$)', "recursive delete of root filesystem"),
    (r'\brm\s+(-[^\s]*\s+)*(/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*)(\s|$)', "recursive delete of system directory"),
    (r'\brm\s+(-[^\s]*\s+)*(~|\$HOME)(/?|/\*)?(\s|$)', "recursive delete of home directory"),
    (r'\bmkfs(\.[a-z0-9]+)?\b', "format filesystem (mkfs)"),
    (r'\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*', "dd to raw block device"),
    (r'>\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b', "redirect to raw block device"),
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    (r'\bkill\s+(-[^\s]+\s+)*-1\b', "kill all processes"),
    (r'(?:^|[;&|\n`]|(?:\$)\()\s*(?:sudo\s+(?:-[^\s]+\s+)*)?\s*(?:env\s+(?:\w+=\S*\s+)*)?\s*(?:(?:exec|nohup|setsid|time)\s+)*\s*(shutdown|reboot|halt|poweroff)\b', "system shutdown/reboot"),
    (r'(?:^|[;&|\n`]|(?:\$)\()\s*(?:sudo\s+(?:-[^\s]+\s+)*)?\s*(?:env\s+(?:\w+=\S*\s+)*)?\s*(?:(?:exec|nohup|setsid|time)\s+)*\s*init\s+[06]\b', "init 0/6 (shutdown/reboot)"),
    (r'(?:^|[;&|\n`]|(?:\$)\()\s*(?:sudo\s+(?:-[^\s]+\s+)*)?\s*(?:env\s+(?:\w+=\S*\s+)*)?\s*(?:(?:exec|nohup|setsid|time)\s+)*\s*systemctl\s+(poweroff|reboot|halt|kexec)\b', "systemctl poweroff/reboot"),
    (r'(?:^|[;&|\n`]|(?:\$)\()\s*(?:sudo\s+(?:-[^\s]+\s+)*)?\s*(?:env\s+(?:\w+=\S*\s+)*)?\s*(?:(?:exec|nohup|setsid|time)\s+)*\s*telinit\s+[06]\b', "telinit 0/6 (shutdown/reboot)"),
]

# Sudo stdin guard
_SUDO_STDIN_RE = re.compile(
    r'(?:^|[;&|`\n]|&&|\|\||\$\()\s*sudo\s+-S\b',
    re.IGNORECASE,
)

_RE_FLAGS = re.IGNORECASE | re.DOTALL
HARDLINE_COMPILED = [
    (re.compile(p, _RE_FLAGS), d) for p, d in HARDLINE_PATTERNS
]


# ---------------------------------------------------------------------------
# Dangerous patterns — require approval
# ---------------------------------------------------------------------------

DANGEROUS_PATTERNS: List[Tuple[str, str]] = [
    (r'\brm\s+(-[^\s]*\s+)*/', "delete in root path"),
    (r'\brm\s+-[^\s]*r', "recursive delete"),
    (r'\brm\s+--recursive\b', "recursive delete (long flag)"),
    (r'\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b', "world/other-writable permissions"),
    (r'\bchmod\s+--recursive\b.*(777|666|o\+[rwx]*w|a\+[rwx]*w)', "recursive world/other-writable"),
    (r'\bchown\s+(-[^\s]*)?R\s+root', "recursive chown to root"),
    (r'\bchown\s+--recursive\b.*root', "recursive chown to root (long flag)"),
    (r'\bmkfs\b', "format filesystem"),
    (r'\bdd\s+.*if=', "disk copy"),
    (r'>\s*/dev/sd', "write to block device"),
    (r'\bDROP\s+(TABLE|DATABASE)\b', "SQL DROP"),
    (r'\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)', "SQL DELETE without WHERE"),
    (r'\bTRUNCATE\s+(TABLE)?\s*\w', "SQL TRUNCATE"),
    (r'\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b', "stop/restart system service"),
    (r'\bkill\s+-9\s+-1\b', "kill all processes"),
    (r'\bpkill\s+-9\b', "force kill processes"),
    (r'\bkillall\s+(-[^\s]*\s+)*-(9|KILL|SIGKILL)\b', "force kill processes (killall -KILL)"),
    (r'\bkillall\s+(-[^\s]*\s+)*-s\s+(KILL|SIGKILL|9)\b', "force kill processes (killall -s KILL)"),
    (r'\bkillall\s+(-[^\s]*\s+)*-r\b', "kill processes by regex (killall -r)"),
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    (r'\b(bash|sh|zsh|ksh)\s+-[^\s]*c(\s+|$)', "shell command via -c/-lc flag"),
    (r'\b(python[23]?|perl|ruby|node)\s+-[ec]\s+', "script execution via -e/-c flag"),
    (r'\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)', "pipe remote content to shell"),
    (r'\bxargs\s+.*\brm\b', "xargs with rm"),
    (r'\bfind\b.*-exec(?:dir)?\s+(/\S*/)?rm\b', "find -exec/-execdir rm"),
    (r'\bfind\b.*-delete\b', "find -delete"),
    # Hermes-specific: gateway lifecycle protection
    (r'\bhermes\s+gateway\s+(stop|restart)\b', "stop/restart hermes gateway"),
    (r'\bhermes\s+update\b', "hermes update (restarts gateway)"),
    # Docker lifecycle
    (r'\bdocker\s+compose\s+(restart|stop|kill|down)\b', "docker compose restart/stop/kill/down"),
    (r'\bdocker\s+(restart|stop|kill)\b', "docker restart/stop/kill"),
]

DANGEROUS_COMPILED = [
    (re.compile(p, _RE_FLAGS), d) for p, d in DANGEROUS_PATTERNS
]


# ---------------------------------------------------------------------------
# Sensitive paths — file tools should refuse to write without approval
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PREFIXES = (
    "/etc/", "/boot/", "/usr/lib/systemd/",
    "/private/etc/", "/private/var/",
)
_SENSITIVE_EXACT_PATHS = {"/var/run/docker.sock", "/run/docker.sock"}


def check_sensitive_path(filepath: str) -> Optional[str]:
    """Check if a filepath targets a sensitive system location.

    Returns an error message if blocked, or None if safe.
    """
    try:
        normalized = os.path.expanduser(filepath)
        resolved = os.path.realpath(normalized)
    except (OSError, ValueError):
        return None
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if resolved.startswith(prefix):
            return f"BLOCKED: {filepath} resolves to sensitive system path ({prefix})"
    if resolved in _SENSITIVE_EXACT_PATHS:
        return f"BLOCKED: {filepath} resolves to sensitive system path ({resolved})"
    return None


# ---------------------------------------------------------------------------
# Detection functions
# ---------------------------------------------------------------------------

def _normalize_command_for_detection(command: str) -> str:
    """Strip ANSI escapes, normalize whitespace for regex matching."""
    command = re.sub(r'\x1b\[[0-9;]*m', '', command)
    command = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', command)
    return command.strip()


def detect_hardline(command: str) -> Tuple[bool, Optional[str]]:
    """Check if a command matches the unconditional hardline blocklist.

    Returns (is_hardline, description) or (False, None).
    """
    normalized = _normalize_command_for_detection(command).lower()
    # Sudo stdin guard
    if "SUDO_PASSWORD" not in os.environ:
        if _SUDO_STDIN_RE.search(normalized):
            return True, "sudo password guessing via stdin (sudo -S)"
    for pattern_re, description in HARDLINE_COMPILED:
        if pattern_re.search(normalized):
            return True, description
    return False, None


def detect_dangerous(command: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """Check if a command matches dangerous patterns requiring approval.

    Returns (is_dangerous, pattern_key, description) or (False, None, None).
    """
    normalized = _normalize_command_for_detection(command).lower()
    for pattern_re, description in DANGEROUS_COMPILED:
        if pattern_re.search(normalized):
            return True, description, description
    return False, None, None


# ---------------------------------------------------------------------------
# Per-session approval memory
# ---------------------------------------------------------------------------

class ApprovalState:
    """Thread-safe per-session approval state.

    Remembers approved patterns for the session so the user doesn't
    have to approve the same dangerous command twice.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._approved: Dict[str, Dict[str, float]] = {}

    def is_approved(self, session_key: str, pattern_key: str) -> bool:
        with self._lock:
            return pattern_key in self._approved.get(session_key, {})

    def approve_session(self, session_key: str, pattern_key: str) -> None:
        with self._lock:
            self._approved.setdefault(session_key, {})[pattern_key] = time.time()

    def clear_session(self, session_key: str) -> None:
        with self._lock:
            self._approved.pop(session_key, None)


_approval_state = ApprovalState()


# ---------------------------------------------------------------------------
# Web approval — SSE + blocking Event
# ---------------------------------------------------------------------------

_pending_approvals: Dict[str, threading.Event] = {}
_approval_results: Dict[str, str] = {}


def submit_web_approval(command: str, description: str, emit_fn: Optional[Callable] = None) -> dict:
    """Request approval via Web UI (SSE). Blocks until resolved.

    Returns {"approved": bool, "message": str}.
    """
    import uuid
    approval_id = uuid.uuid4().hex
    event = threading.Event()
    _pending_approvals[approval_id] = event

    if emit_fn:
        emit_fn("approval_request", {
            "approval_id": approval_id,
            "command": command,
            "description": description,
        })

    resolved = event.wait(timeout=120.0)
    result = _approval_results.pop(approval_id, "deny")
    _pending_approvals.pop(approval_id, None)

    if not resolved:
        return {"approved": False, "message": "Approval timed out (120s)"}
    if result == "allow":
        return {"approved": True, "message": "Approved by user"}
    return {"approved": False, "message": "Denied by user"}


def resolve_web_approval(approval_id: str, decision: str) -> None:
    """Resolve a pending web approval. Called by POST /api/approve."""
    event = _pending_approvals.get(approval_id)
    if event:
        _approval_results[approval_id] = decision
        event.set()


# ---------------------------------------------------------------------------
# CLI approval prompt
# ---------------------------------------------------------------------------

def prompt_approval_cli(command: str, description: str) -> str:
    """Interactive CLI approval prompt. Returns "allow" or "deny"."""
    print(f"\n\033[33m⚠ DANGEROUS COMMAND:\033[0m {description}")
    print(f"\033[90mCommand:\033[0m {command}")
    try:
        answer = input("Allow this command? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "deny"
    return "allow" if answer in ("y", "yes") else "deny"


# ---------------------------------------------------------------------------
# Terminal command check — main entry point for terminal tool
# ---------------------------------------------------------------------------

def check_terminal_command(
    command: str,
    session_key: str = "",
    emit_fn: Optional[Callable] = None,
    approvals_enabled: bool = True,
    cfg: Optional[Dict[str, Any]] = None,
) -> dict:
    """Check a terminal command against hardline + dangerous patterns.

    Returns {"approved": bool, "message": str}.
    """
    if not approvals_enabled:
        return {"approved": True, "message": ""}

    # Hardline: unconditional block (always enforced, even in YOLO mode)
    is_hardline, desc = detect_hardline(command)
    if is_hardline:
        return {
            "approved": False,
            "message": (
                f"BLOCKED (hardline): {desc}. "
                "This command is on the unconditional blocklist and cannot "
                "be executed via the agent."
            ),
        }

    # YOLO mode: skip dangerous-pattern approvals
    if cfg and cfg.get("approvals", {}).get("yolo"):
        return {"approved": True, "message": "YOLO mode — auto-approved"}

    # Dangerous: require approval
    is_dangerous, pattern_key, desc = detect_dangerous(command)
    if not is_dangerous:
        return {"approved": True, "message": ""}

    # Check session-scoped approval memory
    if session_key and _approval_state.is_approved(session_key, pattern_key or desc or ""):
        return {"approved": True, "message": f"Previously approved: {desc}"}

    # Try web approval first (if emit_fn is available)
    if emit_fn:
        result = submit_web_approval(command, desc or "dangerous command", emit_fn)
        if result["approved"] and session_key and pattern_key:
            _approval_state.approve_session(session_key, pattern_key)
        return result

    # Fall back to CLI prompt
    decision = prompt_approval_cli(command, desc or "dangerous command")
    if decision == "allow":
        if session_key and pattern_key:
            _approval_state.approve_session(session_key, pattern_key)
        return {"approved": True, "message": "Approved by user (CLI)"}
    return {"approved": False, "message": "Denied by user (CLI)"}


# ---------------------------------------------------------------------------
# Sudo password prompt — Web UI (SSE) + CLI (/dev/tty)
# ---------------------------------------------------------------------------

_pending_sudo: Dict[str, threading.Event] = {}
_sudo_results: Dict[str, Dict[str, str]] = {}


def submit_web_sudo(
    command: str,
    emit_fn: Optional[Callable] = None,
    timeout: float = 120.0,
) -> dict:
    """Request sudo password via Web UI (SSE). Blocks until resolved.

    Returns {"action": "password"|"reject"|"timeout", "password": str, "message": str}.
    """
    import uuid
    request_id = uuid.uuid4().hex
    event = threading.Event()
    _pending_sudo[request_id] = event

    if emit_fn:
        emit_fn("sudo_request", {
            "request_id": request_id,
            "command": command,
        })

    resolved = event.wait(timeout=timeout)
    result = _sudo_results.pop(request_id, {"action": "timeout"})
    _pending_sudo.pop(request_id, None)

    if not resolved:
        return {"action": "timeout", "password": "", "message": "Sudo prompt timed out — user did not respond"}
    if result["action"] == "password":
        return {"action": "password", "password": result["password"], "message": ""}
    if result["action"] == "reject":
        reason = result.get("message", "")
        msg = "User rejected sudo" + (f": {reason}" if reason else "")
        return {"action": "reject", "password": "", "message": msg}
    return {"action": "timeout", "password": "", "message": "Sudo prompt timed out"}


def resolve_web_sudo(request_id: str, action: str, password: str = "", message: str = "") -> None:
    """Resolve a pending web sudo prompt. Called by POST /api/sudo."""
    event = _pending_sudo.get(request_id)
    if event:
        _sudo_results[request_id] = {"action": action, "password": password, "message": message}
        event.set()


def prompt_sudo_cli(timeout: float = 120.0) -> dict:
    """Interactive CLI sudo password prompt with timeout.

    Returns {"action": "password"|"reject"|"timeout", "password": str, "message": str}.
    """
    import sys
    import platform

    result: Dict[str, Any] = {"action": "timeout", "password": "", "message": ""}

    def _read_password():
        """Read password with echo disabled."""
        tty_fd = None
        old_attrs = None
        try:
            if platform.system() == "Windows":
                import msvcrt
                chars: list[str] = []
                while True:
                    c = msvcrt.getwch()
                    if c in ("\r", "\n"):
                        break
                    if c == "\x03":
                        raise KeyboardInterrupt
                    chars.append(c)
                result["password"] = "".join(chars)
            else:
                import termios
                tty_fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
                old_attrs = termios.tcgetattr(tty_fd)
                new_attrs = termios.tcgetattr(tty_fd)
                new_attrs[3] = new_attrs[3] & ~termios.ECHO
                termios.tcsetattr(tty_fd, termios.TCSAFLUSH, new_attrs)
                chars_bytes: list[bytes] = []
                while True:
                    b = os.read(tty_fd, 1)
                    if not b or b in (b"\n", b"\r"):
                        break
                    chars_bytes.append(b)
                result["password"] = b"".join(chars_bytes).decode("utf-8", errors="replace")
            result["action"] = "password"
        except (EOFError, KeyboardInterrupt, OSError):
            result["action"] = "reject"
            result["message"] = "Cancelled"
        except Exception:
            result["action"] = "reject"
            result["message"] = "Read error"
        finally:
            if tty_fd is not None and old_attrs is not None:
                try:
                    import termios as _termios
                    _termios.tcsetattr(tty_fd, _termios.TCSAFLUSH, old_attrs)
                except Exception:
                    pass
            if tty_fd is not None:
                try:
                    os.close(tty_fd)
                except Exception:
                    pass

    try:
        print()
        print("┌" + "─" * 58 + "┐")
        print("│  SUDO PASSWORD REQUIRED" + " " * 34 + "│")
        print("├" + "─" * 58 + "┤")
        print("│  Enter password below (input is hidden), or:            │")
        print("│    • Press Enter to reject (command will not run)       │")
        print(f"│    • Wait {int(timeout)}s to auto-reject" + " " * (28 - len(str(int(timeout)))) + "│")
        print("└" + "─" * 58 + "┘")
        print()
        print("  Password (hidden): ", end="", flush=True)

        t = threading.Thread(target=_read_password, daemon=True)
        t.start()
        t.join(timeout=timeout)

        print()  # newline after hidden input

        if result["action"] == "password":
            if result["password"]:
                print("  Password received (cached for this session)")
            else:
                # Empty password = user pressed Enter = reject
                result["action"] = "reject"
                result["message"] = "User rejected sudo (empty password)"
                print("  Rejected — continuing without sudo")
        elif result["action"] == "timeout":
            result["message"] = "Sudo prompt timed out — user did not respond"
            print(f"  Timed out after {int(timeout)}s — continuing without sudo")
        else:
            print("  Rejected — continuing without sudo")
        print()
        sys.stdout.flush()
    except (EOFError, KeyboardInterrupt):
        print()
        print("  Rejected — continuing without sudo")
        print()
        sys.stdout.flush()
        result["action"] = "reject"
        result["message"] = "Cancelled by user"
    except Exception as e:
        print(f"\n  [sudo prompt error: {e}] — continuing without sudo\n")
        sys.stdout.flush()
        result["action"] = "reject"
        result["message"] = f"Prompt error: {e}"

    return result
