"""Web server entry point + all API route handlers.

The ``start_server`` function is what the CLI's ``hermeslite web``
command calls. It builds the route table, configures auth, opens the
socket, and (optionally) opens the user's browser.

All routes live in the same module so a developer can find them
without spelunking through the package. There are about a dozen
endpoints — none of them are auth-protected when bound to loopback.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from .. import __release_date__, __version__
from ..agent.core import AIAgent, AgentTurnResult
from ..config import save_config
from ..cron import Scheduler as CronScheduler
from ..paths import get_hermes_home, get_state_db_path
from ..providers import (
    ChatMessage,
    OpenAICompatProvider,
    active_profile,
    load_providers,
)
from ..state import Message, StateStore
from ..tools import registry as tool_registry
from .auth import Auth
from .router import (
    HTTPError,
    RequestHandler,
    Router,
    SSEResponse,
    ThreadedHTTPServer,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static asset path
# ---------------------------------------------------------------------------

def _static_dir() -> Path:
    """Return the path to the bundled static assets.

    The ``web/static`` directory sits next to this file in the source
    tree; when installed as a package, ``__file__`` still points at the
    package directory.
    """
    return Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Server state — shared across requests
# ---------------------------------------------------------------------------

class ServerState:
    """Mutable per-server state: config, state store, auth, lock."""

    def __init__(self, cfg: Dict[str, Any], host: str, port: int, allow_public: bool):
        self.cfg = cfg
        # The bind host as passed on the CLI (used in startup banner).
        self.bind_host = host
        self.port = port
        # ``allow_public`` = ``--insecure``: operator has opted out of
        # the auth gate entirely. We never generate a token in that
        # case, and ``required()`` is hard-wired to False.
        self.allow_public = allow_public
        self.state = StateStore(get_state_db_path())
        # Auth: only auto-generate a token for non-loopback binds when
        # the operator didn't pass ``--insecure`` AND there's no token
        # already saved. ``--insecure`` is a strong "I really want zero
        # auth" signal — honour it, even on 0.0.0.0.
        existing = (cfg.get("web") or {}).get("auth_token") or ""
        if allow_public:
            self.auth = Auth(token="", disabled=True)
        elif existing:
            self.auth = Auth(token=existing)
        elif not _is_loopback(host):
            self.auth = Auth.generate()
            cfg.setdefault("web", {})["auth_token"] = self.auth.token
            try:
                save_config(cfg)
            except OSError as exc:
                logger.warning("web: could not persist auth token: %s", exc)
        else:
            self.auth = Auth(token="")
        # Track active agents per session to support the streaming UI
        # (each session has at most one in-flight turn).
        self._turn_locks: Dict[str, threading.Lock] = {}
        self._turn_locks_guard = threading.Lock()
        # Cron scheduler — started lazily when the first job is added.
        self._cron_scheduler: Optional[CronScheduler] = None
        self._cron_scheduler_lock = threading.Lock()


def _is_loopback(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1")


# ---------------------------------------------------------------------------
# Handlers — small wrappers that pull shared state off the handler.
# ---------------------------------------------------------------------------

def _server(handler: RequestHandler) -> ServerState:
    """Cast :attr:`RequestHandler.state` back to :class:`ServerState`."""
    return handler.state  # type: ignore[return-value]


# Auth gate. Implemented as a router-level wrapper that the install
# function weaves into each API handler.
def _check_auth(handler: RequestHandler) -> None:
    s = _server(handler)
    qs = handler.path.split("?", 1)[1] if "?" in handler.path else ""
    # Always check against the *bind* host, not the request's Host
    # header. The bind host is what the operator chose on the CLI;
    # the request Host is whatever the client typed and is untrustworthy.
    if not s.auth.check(handler.headers, qs, s.bind_host):
        raise HTTPError(401, "unauthorized")


# ---------------------------------------------------------------------------
# Environment variables (env.json)
# ---------------------------------------------------------------------------

def _env_json_path() -> Path:
    """Return the path to ``~/.hermes-lite/env.json``."""
    return get_hermes_home() / "env.json"


def _load_env_json() -> Dict[str, str]:
    """Load env.json from disk.  Returns an empty dict on missing file."""
    p = _env_json_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_env_json(data: Dict[str, str]) -> None:
    """Atomically write env.json (write-temp + rename)."""
    p = _env_json_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.rename(p)


# Keys that came from env.json (survives restarts).
_persistent_env_keys: set = set()


# ---------------------------------------------------------------------------
# Route definitions
# ---------------------------------------------------------------------------

def _build_router(state: ServerState) -> Router:
    r = Router()

    # -- meta ----------------------------------------------------------------

    @r.route("GET", r"/api/status")
    def status(handler, body, kw):
        _check_auth(handler)
        usage = state.state.total_usage()
        # Only expose the auth token to the dashboard if auth is
        # actually required (non-loopback bind with a generated token).
        # On loopback binds and ``--insecure`` mode we hide it so the
        # front-end doesn't try to send a token that nothing will check.
        token = state.auth.token if state.auth.required(state.bind_host) else ""
        return {
            "version": __version__,
            "release_date": __release_date__,
            "host": state.bind_host,
            "port": state.port,
            "auth_required": state.auth.required(state.bind_host),
            "auth_disabled": state.auth.disabled,
            "insecure_mode": state.allow_public,
            "auth_token": token,
            "model": (state.cfg.get("model") or {}).get("name", ""),
            "provider": (state.cfg.get("model") or {}).get("provider", ""),
            "providers": list(load_providers(state.cfg).keys()),
            "max_context_tokens": int((state.cfg.get("model") or {}).get("max_context_tokens") or 0),
            "usage": usage,
        }

    @r.route("GET", r"/api/config")
    def get_config(handler, body, kw):
        _check_auth(handler)
        return state.cfg

    @r.route("PUT", r"/api/config")
    def put_config(handler, body, kw):
        _check_auth(handler)
        new_cfg = handler.parse_json(body)
        # Shallow-merge into the existing config to preserve provider
        # entries that the dashboard doesn't know about.
        merged = _merge(state.cfg, new_cfg)
        state.cfg = merged
        try:
            save_config(merged)
        except OSError as exc:
            raise HTTPError(500, f"cannot save config: {exc}")
        return {"ok": True, "config": merged}

    # -- working directory ---------------------------------------------------

    @r.route("GET", r"/api/cwd")
    def get_cwd(handler, body, kw):
        """Return the current agent working directory."""
        _check_auth(handler)
        from ..agent.runtime_cwd import resolve_agent_cwd
        return {"cwd": str(resolve_agent_cwd())}

    @r.route("PUT", r"/api/cwd")
    def set_cwd(handler, body, kw):
        """Set the agent working directory (persists to config)."""
        _check_auth(handler)
        data = handler.parse_json(body)
        new_cwd = (data.get("cwd") or "").strip()
        if new_cwd:
            from pathlib import Path as _P
            p = _P(os.path.expanduser(new_cwd))
            if not p.is_dir():
                raise HTTPError(400, f"directory not found: {p}")
            new_cwd = str(p)
        os.environ["TERMINAL_CWD"] = new_cwd
        state.cfg.setdefault("terminal", {})["cwd"] = new_cwd
        try:
            save_config(state.cfg)
        except OSError as exc:
            raise HTTPError(500, f"cannot save config: {exc}")
        return {"ok": True, "cwd": new_cwd}

    # -- sessions ------------------------------------------------------------

    @r.route("GET", r"/api/sessions")
    def list_sessions(handler, body, kw):
        _check_auth(handler)
        try:
            limit = int(handler.path.split("limit=", 1)[1].split("&")[0]) if "limit=" in handler.path else 50
        except (ValueError, IndexError):
            limit = 50
        return [s.__dict__ for s in state.state.list_sessions(limit=limit)]

    @r.route("POST", r"/api/sessions")
    def create_session(handler, body, kw):
        _check_auth(handler)
        data = handler.parse_json(body) if body else {}
        s = state.state.create_session(
            title=data.get("title", ""),
            model=(state.cfg.get("model") or {}).get("name", ""),
            provider=(state.cfg.get("model") or {}).get("provider", ""),
            source="web",
        )
        return s.__dict__

    @r.route("GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_]+)")
    def get_session(handler, body, kw):
        _check_auth(handler)
        s = state.state.get_session(kw["sid"])
        if s is None:
            raise HTTPError(404, "no such session")
        return s.__dict__

    @r.route("DELETE", r"/api/sessions/(?P<sid>[A-Za-z0-9_]+)")
    def delete_session(handler, body, kw):
        _check_auth(handler)
        if not state.state.delete_session(kw["sid"]):
            raise HTTPError(404, "no such session")
        return {"ok": True}

    @r.route("GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_]+)/messages")
    def get_messages(handler, body, kw):
        _check_auth(handler)
        msgs = state.state.list_messages(kw["sid"])
        return [m.__dict__ for m in msgs]

    @r.route("DELETE", r"/api/sessions/(?P<sid>[A-Za-z0-9_]+)/messages/(?P<mid>[0-9]+)")
    def delete_message(handler, body, kw):
        _check_auth(handler)
        ok = state.state.delete_message(int(kw["mid"]))
        if not ok:
            raise HTTPError(404, "message not found")
        return {"ok": True}

    @r.route("POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_]+)/messages")
    def post_message(handler, body, kw):
        """Non-streaming message. Persists user+assistant turns.

        Same request body as ``/chat/stream`` (model / provider /
        options overrides). Returns the full result in one JSON
        response — useful for scripts and for the test suite.
        """
        _check_auth(handler)
        data = handler.parse_json(body)
        text = (data.get("content") or "").strip()
        if not text:
            raise HTTPError(400, "content is required")
        cfg = state.cfg
        model = data.get("model")
        provider_name = data.get("provider")
        options = data.get("options") or {}
        if model or provider_name or options:
            from copy import deepcopy
            cfg = deepcopy(cfg)
            if provider_name:
                cfg.setdefault("model", {})["provider"] = provider_name
            if model:
                cfg.setdefault("model", {})["name"] = model
            if options:
                cfg.setdefault("model", {}).setdefault("options", {}).update(options)
        profile = active_profile(cfg)
        opts = (cfg.get("model") or {}).get("options") or {}
        temperature = opts.get("temperature")
        max_tokens = opts.get("max_tokens")
        standard = {"temperature", "max_tokens"}
        extra = {k: v for k, v in opts.items() if k not in standard and v is not None}
        agent = AIAgent(
            cfg=cfg, profile=profile, registry=tool_registry,
            state=state.state, session_id=kw["sid"], stream=False,
            model=model,
            temperature=temperature, max_tokens=max_tokens, extra=extra,
        )
        result = agent.run_turn(text)
        return {
            "session_id": agent.session_id,
            "text": result.final_text,
            "iterations": result.iterations,
            "usage": result.usage,
        }

    @r.route("POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_]+)/chat/stream")
    def stream_chat(handler, body, kw):
        """SSE stream of one turn.

        Request body::

            {
              "content":  "...",        // required
              "model":    "...",        // optional, overrides cfg.model.name
              "provider": "...",        // optional, overrides cfg.model.provider
              "options":  {             // optional, generation params
                "temperature":      0.7,
                "max_tokens":       2048,
                "top_p":            0.9,
                "presence_penalty": 0.0,
                "reasoning_effort": "medium",
                "thinking":         true,
                ...
              }
            }

        The frame shape is::

            {"type": "delta", "text": "..."}      for streamed text
            {"type": "tool_call", ...}            for tool calls
            {"type": "tool_result", ...}          for tool results
            {"type": "done", "text": ..., "usage": {...}}  to close
        """
        _check_auth(handler)
        data = handler.parse_json(body)
        text = (data.get("content") or "").strip()
        attachments = data.get("attachments") or []
        if not text and not attachments:
            raise HTTPError(400, "content is required")
        # Handle slash commands before sending to the agent.
        if text.startswith("/"):
            result = _handle_slash_command(text, kw["sid"])
            if result["handled"]:
                cmd_result = result["result"]
                # Special signals the frontend understands.
                if cmd_result == "__NEW_SESSION__":
                    return SSEResponse(_sse_single({"type": "new_session"}))
                if cmd_result == "__CLEAR__":
                    return SSEResponse(_sse_single({"type": "clear"}))
                if cmd_result == "__EXIT__":
                    return SSEResponse(_sse_single({"type": "done", "text": "Bye."}))
                if cmd_result.startswith("__RETRY__:"):
                    original = cmd_result[len("__RETRY__:"):]
                    return SSEResponse(_sse_single({"type": "retry", "text": original}))
                if cmd_result.startswith("__BRANCH__:"):
                    new_id = cmd_result[len("__BRANCH__:"):]
                    return SSEResponse(_sse_single({"type": "branch", "session_id": new_id}))
                return SSEResponse(_sse_single({"type": "command_result", "text": cmd_result}))
        # Per-request overrides: model / provider / generation options.
        # We rebuild the cfg and profile for this single turn so the
        # rest of the system (history, usage) is unaffected.
        cfg = state.cfg
        model = data.get("model")
        provider_name = data.get("provider")
        options = data.get("options") or {}
        if model or provider_name:
            from copy import deepcopy
            cfg = deepcopy(cfg)
            if provider_name:
                cfg.setdefault("model", {})["provider"] = provider_name
            if model:
                cfg.setdefault("model", {})["name"] = model
        if options:
            from copy import deepcopy
            cfg = deepcopy(cfg)
            cfg.setdefault("model", {}).setdefault("options", {}).update(options)
        profile = active_profile(cfg)
        # Convert the (already-merged) options dict into the kwargs the
        # AIAgent expects. None values are dropped so the provider picks
        # its own default.
        opts = (cfg.get("model") or {}).get("options") or {}
        temperature = opts.get("temperature")
        max_tokens = opts.get("max_tokens")
        # Anything else is passed through as a provider-specific extra.
        standard = {"temperature", "max_tokens"}
        extra = {k: v for k, v in opts.items() if k not in standard and v is not None}
        agent = AIAgent(
            cfg=cfg, profile=profile, registry=tool_registry,
            state=state.state, session_id=kw["sid"], stream=True,
            model=model,
            temperature=temperature, max_tokens=max_tokens, extra=extra,
        )
        # Build the user message: plain string or multimodal content parts.
        if attachments:
            content_parts = [{"type": "text", "text": text}]
            for att in attachments:
                data_url = att.get("data_url") or att.get("data", "")
                if data_url:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    })
            return SSEResponse(_stream_one_turn(agent, content_parts))
        return SSEResponse(_stream_one_turn(agent, text))

    @r.route("POST", r"/api/sessions/(?P<sid>[A-Za-z0-9_]+)/cancel")
    def cancel_session(handler, body, kw):
        _check_auth(handler)
        # Best-effort: there's no clean way to abort a urllib request
        # mid-flight, but we can mark the session as "cancelled" so
        # the next request resets the lock.
        with state._turn_locks_guard:
            state._turn_locks.pop(kw["sid"], None)
        return {"ok": True}

    @r.route("GET", r"/api/sessions/(?P<sid>[A-Za-z0-9_]+)/usage")
    def session_usage(handler, body, kw):
        _check_auth(handler)
        last = state.state.last_turn_usage(kw["sid"])
        last["max_context_tokens"] = int(
            (state.cfg.get("model") or {}).get("max_context_tokens") or 0
        )
        return last

    @r.route("POST", r"/api/approve")
    def approve_command(handler, body, kw):
        _check_auth(handler)
        data = handler.parse_json(body)
        from ..tools.approval import resolve_web_approval
        resolve_web_approval(
            data.get("approval_id", ""),
            data.get("decision", "deny"),
        )
        return {"ok": True}

    @r.route("POST", r"/api/sudo")
    def sudo_password(handler, body, kw):
        _check_auth(handler)
        data = handler.parse_json(body)
        from ..tools.approval import resolve_web_sudo
        resolve_web_sudo(
            data.get("request_id", ""),
            data.get("action", "reject"),
            password=data.get("password", ""),
            message=data.get("message", ""),
        )
        return {"ok": True}

    # -- slash commands ----------------------------------------------------

    _SLASH_COMMANDS = [
        {"name": "/help",      "description": "Show available commands"},
        {"name": "/model",     "description": "Show or switch the active model (e.g. /model gpt-4)"},
        {"name": "/tools",     "description": "List registered tools"},
        {"name": "/skills",    "description": "List discovered skills"},
        {"name": "/memory",    "description": "Memory management (/memory add key=value, /memory del key)"},
        {"name": "/sessions",  "description": "List saved sessions"},
        {"name": "/status",    "description": "Show session and token usage info"},
        {"name": "/compress",  "description": "Compress the current session history"},
        {"name": "/export",    "description": "Export session to markdown (e.g. /export output.md)"},
        {"name": "/new",       "description": "Start a new session"},
        {"name": "/config",    "description": "Show current configuration"},
        {"name": "/usage",     "description": "Show token usage totals"},
        {"name": "/clear",     "description": "Clear the chat display"},
        {"name": "/quit",      "description": "Exit the session"},
        {"name": "/history",   "description": "Show recent messages in this session"},
        {"name": "/retry",     "description": "Re-send the last user message"},
        {"name": "/undo",      "description": "Remove the last assistant turn"},
        {"name": "/title",     "description": "Set session title (e.g. /title my chat)"},
        {"name": "/reload",    "description": "Re-scan skills and tools from disk"},
        {"name": "/debug",     "description": "Show system prompt and tool list"},
        {"name": "/reasoning", "description": "Show reasoning effort configuration"},
        {"name": "/yolo",      "description": "Toggle YOLO mode (skip all approvals)"},
        {"name": "/branch",    "description": "Branch current session into a new one"},
        {"name": "/snapshot",  "description": "Manage state snapshots (/snapshot list|create|restore)"},
        {"name": "/personality", "description": "Set predefined personality (e.g. /personality helpful)"},
        {"name": "/fast",      "description": "Toggle fast mode (priority processing)"},
    ]

    @r.route("GET", r"/api/commands")
    def list_commands(handler, body, kw):
        _check_auth(handler)
        return _SLASH_COMMANDS

    def _handle_slash_command(text: str, session_id: str) -> dict:
        """Handle a /command. Returns {"handled": True, "result": ...} or
        {"handled": False} if it should go to the agent."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            lines = ["Slash commands:"]
            for c in _SLASH_COMMANDS:
                lines.append(f"  {c['name']}  — {c['description']}")
            lines.append("")
            lines.append("Anything else is sent to the agent as a user message.")
            return {"handled": True, "result": "\n".join(lines)}

        if cmd == "/status":
            last = state.state.last_turn_usage(session_id)
            max_ctx = int((state.cfg.get("model") or {}).get("max_context_tokens") or 0)
            prompt_tok = last.get("prompt_tokens", 0)
            total_tok = last.get("total_tokens", 0)
            model = (state.cfg.get("model") or {}).get("name", "?")
            provider = (state.cfg.get("model") or {}).get("provider", "?")
            lines = [
                f"Model: {model}",
                f"Provider: {provider}",
                f"Session: {session_id}",
                f"Prompt tokens: {prompt_tok}",
                f"Total tokens: {total_tok}",
            ]
            if max_ctx:
                pct = min(100, round(prompt_tok / max_ctx * 100))
                lines.append(f"Context: {prompt_tok}/{max_ctx} ({pct}%)")
            return {"handled": True, "result": "\n".join(lines)}

        if cmd == "/model":
            if not rest:
                model = (state.cfg.get("model") or {}).get("name", "?")
                prov = (state.cfg.get("model") or {}).get("provider", "?")
                return {"handled": True, "result": f"Active model: {model}\nProvider: {prov}"}
            from ..config import save_config
            if " --provider " in rest:
                name, _, prov = rest.partition(" --provider ")
                name, prov = name.strip(), prov.strip()
            else:
                name, prov = rest, None
            state.cfg.setdefault("model", {})["name"] = name
            if prov:
                state.cfg["model"]["provider"] = prov
            save_config(state.cfg)
            return {"handled": True, "result": f"Model set to {name}" + (f" (provider: {prov})" if prov else "")}

        if cmd == "/tools":
            lines = [f"  {t.name} — {t.description[:60]}" for t in tool_registry.all()]
            return {"handled": True, "result": "Registered tools:\n" + "\n".join(lines)}

        if cmd == "/skills":
            from ..skills import discover_skills
            skills = discover_skills()
            if not skills:
                return {"handled": True, "result": "(no skills discovered)"}
            lines = [f"  {s.name} — {(s.description or '')[:60]}" for s in skills]
            return {"handled": True, "result": "Skills:\n" + "\n".join(lines)}

        if cmd == "/sessions":
            sessions = state.state.list_sessions(limit=20)
            if not sessions:
                return {"handled": True, "result": "(no sessions)"}
            lines = []
            for s in sessions:
                title = s.title or s.id[:8]
                lines.append(f"  {s.id[:12]}  {title}")
            return {"handled": True, "result": "Sessions:\n" + "\n".join(lines)}

        if cmd == "/compress":
            from ..agent.compress import compress_session
            from ..providers import active_profile as _ap
            before = len(state.state.list_messages(session_id))
            comp_cfg = state.cfg.get("compression") or {}
            threshold_pct = float(comp_cfg.get("threshold_percent") or 0.50)
            max_ctx = int((state.cfg.get("model") or {}).get("max_context_tokens") or 128_000)
            abs_threshold = max(int(max_ctx * threshold_pct), 64_000)
            compressed = compress_session(
                state.state, session_id,
                profile=_ap(state.cfg),
                model=(state.cfg.get("model") or {}).get("name"),
                threshold=abs_threshold,
                target=int(comp_cfg.get("target_recent") or 20),
                use_model_summary=bool(comp_cfg.get("use_model_summary")),
            )
            after = len(state.state.list_messages(session_id))
            status = "triggered" if compressed.triggered else "skipped (below threshold)"
            return {"handled": True, "result": f"Compress {status}: {before} -> {after} messages"}

        if cmd == "/export":
            filename = rest or "hermes-export.md"
            msgs = state.state.list_messages(session_id)
            if not msgs:
                return {"handled": True, "result": "Session is empty — nothing to export"}
            lines = ["# HermesLite Session Export", ""]
            lines.append(f"- Session: `{session_id}`")
            lines.append(f"- Messages: {len(msgs)}")
            lines.append("")
            for m in msgs:
                role = m.role.capitalize()
                if m.name:
                    role += f" ({m.name})"
                lines.append(f"## {role}")
                lines.append("")
                if m.content:
                    lines.append(m.content.strip())
                lines.append("")
                if m.tool_calls:
                    for tc in m.tool_calls:
                        fn = tc.get("function") or {}
                        lines.append(f"**Tool call:** `{fn.get('name', '?')}`")
                        args_str = fn.get("arguments", "")
                        if isinstance(args_str, str) and args_str:
                            lines.append(f"```json\n{args_str}\n```")
                        lines.append("")
            try:
                from pathlib import Path as _Path
                _Path(filename).write_text("\n".join(lines), encoding="utf-8")
                return {"handled": True, "result": f"Exported {len(msgs)} messages to {filename}"}
            except OSError as exc:
                return {"handled": True, "result": f"Export failed: {exc}"}

        if cmd == "/new":
            return {"handled": True, "result": "__NEW_SESSION__"}

        if cmd == "/config":
            import json as _json
            return {"handled": True, "result": _json.dumps(state.cfg, indent=2, ensure_ascii=False)}

        if cmd == "/usage":
            last = state.state.last_turn_usage(session_id)
            return {"handled": True, "result": f"Last turn — prompt: {last.get('prompt_tokens', 0)}, completion: {last.get('completion_tokens', 0)}, total: {last.get('total_tokens', 0)}"}

        if cmd == "/memory":
            if not rest:
                entries = state.state.memory_list(limit=20)
                if not entries:
                    return {"handled": True, "result": "(no memory entries)"}
                lines = [f"  {e['key']}: {(e['value'] or '')[:40]}" for e in entries]
                return {"handled": True, "result": "Memory:\n" + "\n".join(lines)}
            sub_parts = rest.split(maxsplit=1)
            sub = sub_parts[0].lower()
            if sub == "add":
                kv = sub_parts[1] if len(sub_parts) > 1 else ""
                if "=" not in kv:
                    return {"handled": True, "result": "Usage: /memory add key=value"}
                k, _, v = kv.partition("=")
                state.state.memory_set(k.strip(), v.strip(), "")
                return {"handled": True, "result": f"Memory set: {k.strip()}"}
            if sub == "del":
                key = sub_parts[1].strip() if len(sub_parts) > 1 else ""
                if not key:
                    return {"handled": True, "result": "Usage: /memory del <key>"}
                ok = state.state.memory_delete(key)
                return {"handled": True, "result": f"Deleted: {key}" if ok else f"Not found: {key}"}
            return {"handled": True, "result": "Usage: /memory [add key=value | del key]"}

        if cmd == "/history":
            msgs = state.state.list_messages(session_id)
            if not msgs:
                return {"handled": True, "result": "(empty session)"}
            lines = []
            for m in msgs[-30:]:
                preview = (m.content or "").splitlines()[0][:120] if m.content else ""
                lines.append(f"  [{m.role:9s}] {preview}")
            return {"handled": True, "result": "\n".join(lines)}

        if cmd == "/retry":
            msgs = state.state.list_messages(session_id)
            last_user = None
            for m in reversed(msgs):
                if m.role == "user":
                    last_user = m
                    break
            if last_user is None:
                return {"handled": True, "result": "No user message to retry"}
            with state.state.transaction() as c:
                c.execute("DELETE FROM messages WHERE id >= ?", (last_user.id,))
            return {"handled": True, "result": f"__RETRY__:{last_user.content}"}

        if cmd == "/undo":
            msgs = state.state.list_messages(session_id)
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].role == "assistant":
                    with state.state.transaction() as c:
                        c.execute("DELETE FROM messages WHERE id >= ?", (msgs[i].id,))
                    return {"handled": True, "result": "Removed last assistant turn"}
            return {"handled": True, "result": "No assistant message to undo"}

        if cmd == "/title":
            if not rest:
                return {"handled": True, "result": "Usage: /title <text>"}
            state.state.update_session(session_id, title=rest.strip())
            return {"handled": True, "result": f"Title set to: {rest.strip()}"}

        if cmd == "/reload":
            from ..skills import discover_skills
            skills = discover_skills()
            tools = tool_registry.all()
            return {"handled": True, "result": f"Reloaded {len(skills)} skills, {len(tools)} tools"}

        if cmd == "/debug":
            from ..providers import active_profile as _ap_dbg
            agent_dbg = AIAgent(
                cfg=state.cfg, profile=_ap_dbg(state.cfg), registry=tool_registry,
                state=state.state, session_id=session_id, stream=False,
            )
            sp = agent_dbg.system_prompt
            preview = sp[:1500] + ("\n... [truncated]" if len(sp) > 1500 else "")
            tool_names = [t.name for t in tool_registry.all()]
            return {"handled": True, "result": f"=== System Prompt ===\n{preview}\n\n=== Tools ({len(tool_names)}) ===\n" + ", ".join(tool_names)}

        if cmd == "/reasoning":
            effort = (state.cfg.get("model") or {}).get("reasoning_effort", "not set")
            return {"handled": True, "result": f"Reasoning effort: {effort}\nSet via config.model.reasoning_effort (model-dependent)"}

        if cmd == "/yolo":
            from ..config import save_config
            approvals = state.cfg.setdefault("approvals", {})
            current = approvals.get("yolo", False)
            approvals["yolo"] = not current
            save_config(state.cfg)
            return {"handled": True, "result": f"YOLO mode: {'ON' if not current else 'OFF'}"}

        if cmd == "/branch":
            try:
                new_id = state.state.branch_session(session_id, rest.strip())
                return {"handled": True, "result": f"__BRANCH__:{new_id}"}
            except ValueError as exc:
                return {"handled": True, "result": str(exc)}

        if cmd == "/snapshot":
            from ..snapshot import create_snapshot, list_snapshots, restore_snapshot
            parts = rest.split(maxsplit=1)
            sub = parts[0] if parts else "list"
            if sub == "create":
                label = parts[1].strip() if len(parts) > 1 else ""
                m = create_snapshot(label)
                return {"handled": True, "result": f"Snapshot created: {m['id']}"}
            elif sub == "restore":
                if len(parts) < 2:
                    return {"handled": True, "result": "Usage: /snapshot restore <id>"}
                ok = restore_snapshot(parts[1].strip())
                return {"handled": True, "result": f"Restored: {parts[1].strip()}" if ok else "Snapshot not found"}
            snaps = list_snapshots()
            if not snaps:
                return {"handled": True, "result": "(no snapshots)"}
            lines = [f"  {s['id']} — {s['file_count']} files" for s in snaps]
            return {"handled": True, "result": "Snapshots:\n" + "\n".join(lines)}

        if cmd == "/personality":
            from ..agent.prompt import PERSONALITIES, get_personality_instruction
            from ..config import save_config as _save_cfg
            if not rest:
                lines = ["Available personalities:"]
                for name, desc in PERSONALITIES.items():
                    lines.append(f"  {name:14s} — {desc}")
                current = (state.cfg.get("model") or {}).get("personality", "")
                lines.append(f"\nActive: {current or '(none)'}")
                return {"handled": True, "result": "\n".join(lines)}
            name = rest.strip().lower()
            if name == "none":
                state.cfg.setdefault("model", {})["personality"] = ""
                _save_cfg(state.cfg)
                return {"handled": True, "result": "Personality cleared"}
            instr = get_personality_instruction(name)
            if not instr:
                return {"handled": True, "result": f"Unknown personality: {name}. Use /personality to list."}
            state.cfg.setdefault("model", {})["personality"] = name
            _save_cfg(state.cfg)
            return {"handled": True, "result": f"Personality set to: {name}"}

        if cmd == "/fast":
            from ..config import save_config as _save_cfg
            model = state.cfg.setdefault("model", {})
            current = model.get("fast_mode", False)
            model["fast_mode"] = not current
            _save_cfg(state.cfg)
            return {"handled": True, "result": f"Fast mode: {'ON' if not current else 'OFF'}"}

        if cmd == "/quit":
            return {"handled": True, "result": "__EXIT__"}

        if cmd in ("/clear",):
            return {"handled": True, "result": "__CLEAR__"}

        return {"handled": False}

    # -- profiles (multi-agent) -------------------------------------------

    @r.route("POST", r"/api/upload")
    def upload_file(handler, body, kw):
        """Accept a base64-encoded file and return a data URL.

        Request body::

            {
                "data": "data:image/png;base64,iVBORw...",  // or raw base64
                "filename": "screenshot.png"                // optional
            }

        Returns ``{"data_url": "data:image/png;base64,...", "filename": "..."}``.
        """
        _check_auth(handler)
        data = handler.parse_json(body)
        raw = (data.get("data") or "").strip()
        if not raw:
            raise HTTPError(400, "data is required")
        filename = data.get("filename") or "uploaded"
        # Accept both "data:...;base64,..." and raw base64.
        if raw.startswith("data:"):
            data_url = raw
        else:
            # Try to sniff MIME from filename extension.
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            mime_map = {
                "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
                "pdf": "application/pdf", "txt": "text/plain",
                "py": "text/x-python", "js": "text/javascript",
                "json": "application/json", "md": "text/markdown",
                "csv": "text/csv", "html": "text/html",
            }
            mime = mime_map.get(ext, "application/octet-stream")
            data_url = f"data:{mime};base64,{raw}"
        return {"data_url": data_url, "filename": filename}

    @r.route("GET", r"/api/profiles")
    def list_profiles(handler, body, kw):
        _check_auth(handler)
        from ..profiles import list_profiles as _list, get_active_profile as _get_active
        active = _get_active()
        profiles = _list()
        return {
            "active": active,
            "profiles": [
                {
                    "name": p.name,
                    "path": p.path,
                    "is_default": p.is_default,
                    "model": p.model,
                    "provider": p.provider,
                    "has_state": p.has_state,
                    "skill_count": p.skill_count,
                }
                for p in profiles
            ],
        }

    @r.route("POST", r"/api/profiles")
    def create_profile(handler, body, kw):
        _check_auth(handler)
        from ..profiles import create_profile as _create
        data = handler.parse_json(body)
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPError(400, "name is required")
        try:
            p = _create(
                name,
                clone_from=data.get("clone_from"),
                model=data.get("model"),
                provider=data.get("provider"),
            )
        except ValueError as exc:
            raise HTTPError(400, str(exc))
        except FileExistsError as exc:
            raise HTTPError(409, str(exc))
        return {"name": p.name, "path": p.path}

    @r.route("GET", r"/api/profiles/active")
    def get_active_profile(handler, body, kw):
        _check_auth(handler)
        from ..profiles import get_active_profile as _get_active
        return {"active": _get_active()}

    @r.route("PUT", r"/api/profiles/active")
    def set_active_profile(handler, body, kw):
        _check_auth(handler)
        from ..profiles import set_active_profile as _set_active
        data = handler.parse_json(body)
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPError(400, "name is required")
        try:
            _set_active(name)
        except FileNotFoundError as exc:
            raise HTTPError(404, str(exc))
        except ValueError as exc:
            raise HTTPError(400, str(exc))
        return {"ok": True, "active": name}

    @r.route("DELETE", r"/api/profiles/(?P<name>[A-Za-z0-9_\-]+)")
    def delete_profile(handler, body, kw):
        _check_auth(handler)
        from ..profiles import delete_profile as _delete
        try:
            ok = _delete(kw["name"])
        except ValueError as exc:
            raise HTTPError(400, str(exc))
        if not ok:
            raise HTTPError(404, f"profile {kw['name']!r} not found")
        return {"ok": True}

    # -- tools / skills / memory / models ----------------------------------

    @r.route("GET", r"/api/tools")
    def list_tools(handler, body, kw):
        _check_auth(handler)
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in tool_registry.all()
        ]

    @r.route("GET", r"/api/skills")
    def list_skills(handler, body, kw):
        _check_auth(handler)
        from ..skills import discover_skills
        return [
            {"name": s.name, "description": s.description, "source": s.source}
            for s in discover_skills()
        ]

    @r.route("GET", r"/api/skills/(?P<name>[A-Za-z0-9_\-]+)")
    def get_skill(handler, body, kw):
        _check_auth(handler)
        from ..skills import discover_skills
        for s in discover_skills():
            if s.name == kw["name"]:
                return {"name": s.name, "description": s.description, "body": s.body, "source": s.source}
        raise HTTPError(404, "no such skill")

    @r.route("GET", r"/api/memory")
    def list_memory(handler, body, kw):
        _check_auth(handler)
        return state.state.memory_list(limit=200)

    @r.route("POST", r"/api/memory")
    def post_memory(handler, body, kw):
        _check_auth(handler)
        data = handler.parse_json(body)
        key = (data.get("key") or "").strip()
        if not key:
            raise HTTPError(400, "key is required")
        state.state.memory_set(key, data.get("value", ""), data.get("tags", ""))
        return {"ok": True}

    @r.route("DELETE", r"/api/memory/(?P<key>[^/]+)")
    def delete_memory(handler, body, kw):
        _check_auth(handler)
        # ``key`` may have been URL-encoded; the path parser kept it
        # as-is so we unquote here.
        from urllib.parse import unquote
        key = unquote(kw["key"])
        ok = state.state.memory_delete(key)
        if not ok:
            raise HTTPError(404, "no such key")
        return {"ok": True}

    @r.route("GET", r"/api/models")
    def list_models(handler, body, kw):
        _check_auth(handler)
        provider_name = (state.cfg.get("model") or {}).get("provider") or "openai"
        providers = load_providers(state.cfg)
        prof = providers.get(provider_name)
        if prof is None:
            return {"models": [], "source": "none"}
        client = OpenAICompatProvider(prof)
        models = client.fetch_models()
        if not models:
            models = prof.fallback_models
            return {"models": models, "source": "fallback"}
        return {"models": models, "source": "live"}

    # -- environment variables ------------------------------------------------

    @r.route("GET", r"/api/env")
    def get_env(handler, body, kw):
        _check_auth(handler)
        return {
            "vars": dict(os.environ),
            "persistent": sorted(_persistent_env_keys),
        }

    @r.route("PUT", r"/api/env")
    def put_env(handler, body, kw):
        _check_auth(handler)
        data = handler.parse_json(body)
        name = (data.get("name") or "").strip()
        value = data.get("value", "")
        persistent = bool(data.get("persistent"))
        if not name:
            raise HTTPError(400, "name is required")
        os.environ[name] = value
        if persistent:
            _persistent_env_keys.add(name)
            env_file = _load_env_json()
            env_file[name] = value
            _save_env_json(env_file)
        return {"ok": True}

    @r.route("DELETE", r"/api/env")
    def delete_env(handler, body, kw):
        _check_auth(handler)
        data = handler.parse_json(body)
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPError(400, "name is required")
        os.environ.pop(name, None)
        if name in _persistent_env_keys:
            _persistent_env_keys.discard(name)
            env_file = _load_env_json()
            env_file.pop(name, None)
            _save_env_json(env_file)
        return {"ok": True}

    # -- cron jobs ---------------------------------------------------------

    def _ensure_cron_scheduler() -> None:
        """Start the cron scheduler if not already running."""
        if state._cron_scheduler is not None and state._cron_scheduler.is_running:
            return
        with state._cron_scheduler_lock:
            if state._cron_scheduler is not None and state._cron_scheduler.is_running:
                return
            state._cron_scheduler = CronScheduler()
            state._cron_scheduler.start()

    @r.route("GET", r"/api/cron")
    def list_cron_jobs(handler, body, kw):
        _check_auth(handler)
        from ..cron import load_jobs
        jobs = load_jobs()
        return {"jobs": [j.to_dict() for j in jobs]}

    @r.route("POST", r"/api/cron")
    def create_cron_job(handler, body, kw):
        _check_auth(handler)
        from ..cron import create_job
        data = handler.parse_json(body)
        name = (data.get("name") or "").strip()
        schedule = (data.get("schedule") or "").strip()
        if not name:
            raise HTTPError(400, "name is required")
        if not schedule:
            raise HTTPError(400, "schedule is required")
        prompt = (data.get("prompt") or "").strip()
        command = (data.get("command") or "").strip() or None
        if not prompt and not command:
            raise HTTPError(400, "either prompt or command is required")
        try:
            job = create_job(
                name=name,
                schedule=schedule,
                prompt=prompt,
                command=command,
                model=data.get("model"),
                provider=data.get("provider"),
                workdir=data.get("workdir"),
                repeat_times=data.get("repeat_times"),
            )
        except ValueError as exc:
            raise HTTPError(400, str(exc))
        _ensure_cron_scheduler()
        return {"job": job.to_dict()}

    @r.route("GET", r"/api/cron/(?P<jid>[a-f0-9]{12})")
    def get_cron_job(handler, body, kw):
        _check_auth(handler)
        from ..cron import load_jobs
        jobs = load_jobs()
        for j in jobs:
            if j.id == kw["jid"]:
                return {"job": j.to_dict()}
        raise HTTPError(404, "no such job")

    @r.route("PATCH", r"/api/cron/(?P<jid>[a-f0-9]{12})")
    def update_cron_job(handler, body, kw):
        _check_auth(handler)
        from ..cron import update_job
        data = handler.parse_json(body)
        job = update_job(kw["jid"], data)
        if job is None:
            raise HTTPError(404, "no such job")
        return {"job": job.to_dict()}

    @r.route("DELETE", r"/api/cron/(?P<jid>[a-f0-9]{12})")
    def delete_cron_job(handler, body, kw):
        _check_auth(handler)
        from ..cron import remove_job
        if not remove_job(kw["jid"]):
            raise HTTPError(404, "no such job")
        return {"ok": True}

    @r.route("POST", r"/api/cron/(?P<jid>[a-f0-9]{12})/pause")
    def pause_cron_job(handler, body, kw):
        _check_auth(handler)
        from ..cron import pause_job
        job = pause_job(kw["jid"])
        if job is None:
            raise HTTPError(404, "no such job")
        return {"job": job.to_dict()}

    @r.route("POST", r"/api/cron/(?P<jid>[a-f0-9]{12})/resume")
    def resume_cron_job(handler, body, kw):
        _check_auth(handler)
        from ..cron import resume_job
        job = resume_job(kw["jid"])
        if job is None:
            raise HTTPError(404, "no such job")
        _ensure_cron_scheduler()
        return {"job": job.to_dict()}

    @r.route("POST", r"/api/cron/(?P<jid>[a-f0-9]{12})/run")
    def trigger_cron_job(handler, body, kw):
        _check_auth(handler)
        from ..cron import trigger_job
        job = trigger_job(kw["jid"])
        if job is None:
            raise HTTPError(404, "no such job")
        _ensure_cron_scheduler()
        return {"job": job.to_dict()}

    @r.route("GET", r"/api/cron/(?P<jid>[a-f0-9]{12})/output")
    def get_cron_output(handler, body, kw):
        _check_auth(handler)
        from ..cron import load_job_output
        output = load_job_output(kw["jid"])
        return {"output": output}

    @r.route("GET", r"/api/cron/(?P<jid>[a-f0-9]{12})/sessions")
    def get_cron_sessions(handler, body, kw):
        _check_auth(handler)
        import re as _re
        jid = kw["jid"]
        prefix = f"cron_{jid}_"
        sessions = state.state.list_sessions_by_prefix(prefix)
        result = []
        for s in sessions:
            msgs = state.state.list_messages(s.id)
            cleaned = []
            for m in msgs:
                if m.role not in ("user", "assistant"):
                    continue
                content = (m.content or "").strip()
                # Strip <system-reminder>...</system-reminder> blocks.
                content = _re.sub(r"<system-reminder>.*?</system-reminder>", "", content, flags=_re.DOTALL).strip()
                if not content:
                    continue
                cleaned.append({"role": m.role, "content": content})
            result.append({
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "messages": cleaned,
            })
        return {"sessions": result}

    return r


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def _sse_single(payload: dict) -> Generator[str, None, None]:
    """Yield a single SSE JSON payload."""
    yield json.dumps(payload, ensure_ascii=False)


def _stream_one_turn(agent: AIAgent, user_message) -> Generator[str, None, None]:
    """Drive one agent turn and yield SSE-formatted JSON lines.

    ``user_message`` may be a plain string or a list of content parts
    (multimodal format with images).

    The :class:`SSEResponse` wrapper writes each yielded value as
    ``data: <value>\n\n``. We yield JSON strings (one per event) so
    the frontend can ``JSON.parse(event.data)``.
    """
    def emit(kind: str, payload: Dict[str, Any]) -> str:
        return json.dumps({"type": kind, **payload}, ensure_ascii=False)

    def event_sink(kind: str, payload: Dict[str, Any]) -> None:
        try:
            yield emit(kind, payload)
        except GeneratorExit:
            raise
    # The agent's on_event runs in-process; we want each event to be
    # forwarded to the SSE stream. The agent's interface is sync, so
    # we use a queue.
    import queue
    q: "queue.Queue[str]" = queue.Queue()
    done = threading.Event()
    error_holder: List[BaseException] = []

    def sink(kind: str, payload: Dict[str, Any]) -> None:
        q.put(emit(kind, payload))

    def runner() -> None:
        try:
            agent.on_event = sink
            # Emit the system prompt only on the first turn of a session.
            # On resumption the prompt is already stored in the DB and the
            # frontend does not need to display it again.
            try:
                stored = agent.state.get_session_system_prompt(agent.session_id)
                sys_prompt = agent.system_prompt
                if sys_prompt and not stored:
                    q.put(emit("system_prompt", {"text": sys_prompt}))
            except Exception:  # noqa: BLE001
                pass
            result = agent.run_turn(user_message)
            q.put(emit("done", {
                "text": result.final_text,
                "iterations": result.iterations,
                "usage": result.usage,
            }))
        except BaseException as exc:  # noqa: BLE001
            error_holder.append(exc)
            q.put(emit("error", {"detail": f"{type(exc).__name__}: {exc}"}))
        finally:
            done.set()

    threading.Thread(target=runner, daemon=True).start()

    # Yield events as they arrive. We don't honour per-request
    # cancellation here — the client can simply stop reading and the
    # underlying TCP close will tear the agent thread down.
    while True:
        try:
            evt = q.get(timeout=15.0)
        except queue.Empty:
            # Keep-alive comment to defeat proxy timeouts. The router
            # wraps each payload as `data: ...\n\n`, but comments
            # start with `:` and need their own format. We send a
            # literal "ping" data line.
            yield json.dumps({"type": "ping"})
            continue
        yield evt
        try:
            parsed = json.loads(evt)
        except json.JSONDecodeError:
            parsed = {}
        if parsed.get("type") in ("done", "error"):
            break
        if done.is_set() and q.empty():
            break
    if error_holder:
        # Re-raise on caller so the framework can log it. We yield a
        # final error frame first.
        logger.error("stream_one_turn: %s", error_holder[0])


# ---------------------------------------------------------------------------
# Server start
# ---------------------------------------------------------------------------

def start_server(
    *,
    cfg: Dict[str, Any],
    host: str,
    port: int,
    open_browser: bool,
    allow_public: bool,
) -> int:
    """Start the management web server. Blocks until the user hits Ctrl-C.

    Returns the process exit code (always 0 on a clean shutdown).
    """
    # Make sure the home directory is initialised. If the user has
    # never run ``hermeslite setup``, do the bare minimum (create the
    # directory + write a default config) so the server can start.
    from ..setup import init_home
    init_home(force=False)

    # Load persisted environment variables (env.json) into os.environ
    # so that resolve_api_key(), _get_sudo_password(), etc. pick them up
    # immediately.  os.environ is process-global and thread-safe (GIL).
    saved_env = _load_env_json()
    for k, v in saved_env.items():
        os.environ[k] = v
    _persistent_env_keys.update(saved_env.keys())
    if saved_env:
        logger.info("loaded %d env var(s) from env.json", len(saved_env))

    # Bridge terminal.cwd → $TERMINAL_CWD so that resolve_agent_cwd()
    # picks up the configured working directory.
    terminal_cwd = (cfg.get("terminal") or {}).get("cwd", "")
    if terminal_cwd:
        os.environ["TERMINAL_CWD"] = os.path.expanduser(terminal_cwd)
        logger.info("terminal.cwd → %s", os.environ["TERMINAL_CWD"])

    state = ServerState(cfg=cfg, host=host, port=port, allow_public=allow_public)
    router = _build_router(state)

    # Start the cron scheduler if there are persisted jobs.
    from ..cron import load_jobs as _load_cron_jobs
    _existing_jobs = _load_cron_jobs()
    if _existing_jobs:
        state._cron_scheduler = CronScheduler()
        state._cron_scheduler.start()
        logger.info("cron: scheduler started with %d job(s)", len(_existing_jobs))

    # Subclass RequestHandler so each request sees our router + state.
    handler_cls = type(
        "H",
        (RequestHandler,),
        {"router": router, "state": state, "static_dir": str(_static_dir())},
    )

    try:
        server = ThreadedHTTPServer((host, port), handler_cls)
    except OSError as exc:
        # Port-in-use is the most common reason. Surface it cleanly.
        print(f"error: cannot bind {host}:{port} — {exc}")
        print("hint: pass --port to use a different port")
        return 1

    url = f"http://{host}:{port}/"
    if state.auth.disabled:
        print(f"hermeslite web: listening on {url}  (--insecure: NO AUTH)")
        print("  warning: anyone able to reach this port can drive the agent.")
    elif state.auth.required(host):
        print(f"hermeslite web: listening on {url}")
        print(f"  auth token: {state.auth.token}")
        print("  (token persisted to config.web.auth_token)")
    else:
        print(f"hermeslite web: listening on {url}  (no auth — loopback bind)")
    if open_browser and _is_loopback(host):
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("webbrowser.open failed: %s", exc)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into ``base`` (copy, not in-place)."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out
