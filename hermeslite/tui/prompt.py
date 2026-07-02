"""Interactive REPL with ANSI styling, history, and slash commands.

The REPL is *intentionally* minimal:

  - reads lines via ``builtins.input()`` (or ``readline`` on POSIX when
    available)
  - history is persisted to ``$HERMESLITE_HOME/history`` so up-arrow
    works across launches
  - slash commands (``/help``, ``/quit``, ``/model``, ``/clear``,
    ``/tools``, ``/skills``, ``/compress``) are handled inline before
    sending to the agent
  - the agent's streaming response is printed directly to stdout
  - ``Ctrl-C`` cancels the current turn but does NOT exit the REPL;
    ``Ctrl-D`` (EOF) exits

We deliberately do NOT depend on ``prompt_toolkit`` or ``rich`` — this
implementation is about 200 lines and handles everything a CLI needs.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..agent.core import AIAgent
from ..paths import get_history_path
from . import colors as C
from .formatting import _short_repr

logger = logging.getLogger(__name__)


_SLASH_COMMANDS = (
    "/help", "/quit", "/exit", "/q",
    "/clear", "/new", "/history", "/retry", "/undo",
    "/title", "/model", "/compress", "/reload", "/config",
    "/tools", "/skills", "/sessions", "/status",
    "/memory", "/usage", "/reasoning", "/verbose",
    "/copy", "/redraw", "/debug", "/export",
    "/yolo", "/branch", "/snapshot",
    "/personality", "/fast",
)


class Repl:
    """Interactive REPL.

    Construct with an :class:`AIAgent` factory — every turn is a fresh
    agent instance because the AIAgent caches its session, but the
    underlying StateStore and ToolRegistry are shared.
    """

    def __init__(
        self,
        *,
        agent_factory: Callable[[Optional[str]], AIAgent],
        history_size: int = 1000,
        prompt_template: str = "{user}@{model} ❯ ",
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self._make_agent = agent_factory
        self._history_size = history_size
        self._prompt_template = prompt_template
        self._on_event = on_event or (lambda k, p: None)
        self._active_session_id: Optional[str] = None
        self._active_agent: Optional[AIAgent] = None  # for Ctrl-C interrupt
        self._output_lock = threading.Lock()           # serialise stdout from threads
        self._readline_ready = False
        self._last_ctrl_c: float = 0  # timestamp of last Ctrl-C

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def run(self) -> int:
        self._setup_readline()
        self._print_welcome()
        exit_code = 0
        while True:
            try:
                line = self._read()
            except EOFError:
                # Ctrl-D: clean exit.
                print()
                break
            except KeyboardInterrupt:
                now = time.monotonic()
                if now - self._last_ctrl_c < 1.5:
                    print()
                    break
                self._last_ctrl_c = now
                # If an agent turn is running, interrupt it.
                if self._active_agent is not None:
                    self._active_agent.interrupt("Cancelled by user")
                    print(C.yellow("\n  [interrupted — type a new message]"))
                else:
                    print(C.dim("  (press Ctrl-C again to exit)"))
                continue
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("/"):
                # Bare "/" shows the interactive command menu.
                if line.strip() == "/":
                    result = self._slash_menu_select()
                    if result is None:
                        continue
                    if result:
                        should_quit = self._handle_slash(result)
                        if should_quit:
                            break
                    continue
                should_quit = self._handle_slash(line)
                if should_quit:
                    break
                continue
            try:
                self._dispatch_turn(line)
            except KeyboardInterrupt:
                now = time.monotonic()
                if now - self._last_ctrl_c < 1.5:
                    print()
                    break
                self._last_ctrl_c = now
                print(C.yellow("\n[cancelled]"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("turn failed")
                print(C.red(f"error: {type(exc).__name__}: {exc}"))
        self._teardown_readline()
        return exit_code

    # ------------------------------------------------------------------
    # Readline
    # ------------------------------------------------------------------

    def _setup_readline(self) -> None:
        """Configure readline for history + simple completion."""
        try:
            import readline  # type: ignore
        except ImportError:
            # Windows without pyreadline: skip history; input() still works.
            return
        try:
            hist_path = get_history_path()
            hist_path.parent.mkdir(parents=True, exist_ok=True)
            readline.read_history_file(str(hist_path))
            readline.set_history_length(self._history_size)
        except OSError as exc:
            logger.debug("readline: cannot load history: %s", exc)
        try:
            readline.set_completer(self._completer)
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass
        self._readline_ready = True

    def _teardown_readline(self) -> None:
        if not self._readline_ready:
            return
        try:
            import readline  # type: ignore
            readline.set_history_length(self._history_size)
            readline.write_history_file(str(get_history_path()))
        except (ImportError, OSError):
            pass

    def _completer(self, text: str, state: int):
        """Tab completion for slash commands and subcommands."""
        try:
            import readline  # type: ignore
        except ImportError:
            return None
        # Match slash commands by prefix.
        options = [c for c in _SLASH_COMMANDS if c.startswith(text)]
        if state < len(options):
            # Add trailing space for commands that have no subcommands,
            # keep without space for commands that take arguments (so the
            # user can continue typing).
            cmd = options[state]
            _takes_arg = cmd in (
                "/model", "/title", "/config", "/memory", "/export",
                "/branch", "/snapshot", "/personality",
            )
            return cmd if _takes_arg else cmd + " "
        return None

    def _read(self) -> str:
        prompt = self._format_prompt()
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            raise

    def _format_prompt(self) -> str:
        # Pull the active model's name to put in the prompt.
        try:
            agent = self._make_agent(self._active_session_id)
        except Exception:
            return self._prompt_template.format(user="?", model="?") + " "
        model = agent.model or "?"
        # Truncate long model names.
        if len(model) > 28:
            model = "…" + model[-27:]
        return self._prompt_template.format(user="you", model=model)

    # ------------------------------------------------------------------
    # Interactive slash menu
    # ------------------------------------------------------------------

    _SLASH_MENU_ITEMS = [
        ("/help",       "show this help"),
        ("/quit",       "exit HermesLite"),
        ("/new",        "start a new session"),
        ("/history",    "list messages in this session"),
        ("/retry",      "re-send the last user message"),
        ("/undo",       "delete the last assistant turn"),
        ("/title",      "set the current session's title"),
        ("/model",      "show or switch the active model"),
        ("/compress",   "compress the current session's history"),
        ("/reload",     "re-scan skills and tools from disk"),
        ("/config",     "show full config (or a dotted key)"),
        ("/tools",      "list registered tools"),
        ("/skills",     "list discovered skills"),
        ("/memory",     "memory show/add/del/search"),
        ("/sessions",   "list saved sessions"),
        ("/status",     "show session + usage info"),
        ("/usage",      "show token usage totals"),
        ("/reasoning",  "show reasoning effort configuration"),
        ("/verbose",    "show verbose mode info"),
        ("/copy",       "copy the last assistant reply to clipboard"),
        ("/redraw",     "redraw the screen"),
        ("/debug",      "show the system prompt + tool list"),
        ("/export",     "export session to markdown file"),
        ("/clear",      "clear the screen"),
        ("/yolo",       "toggle YOLO mode (skip all approvals)"),
        ("/branch",     "branch current session into a new one"),
        ("/snapshot",   "manage state snapshots"),
        ("/personality","set predefined personality"),
        ("/fast",       "toggle fast mode (priority processing)"),
    ]

    def _slash_menu_select(self) -> Optional[str]:
        """Show an interactive numbered command menu and return the chosen
        command string (e.g. ``"/compress"``), or *None* to cancel."""
        items = self._SLASH_MENU_ITEMS
        # Display the menu.
        print(C.bold("  Commands:"))
        cols = 2
        per_col = (len(items) + cols - 1) // cols
        for row in range(per_col):
            parts: List[str] = []
            for col in range(cols):
                idx = row + col * per_col
                if idx < len(items):
                    cmd, desc = items[idx]
                    parts.append(f"  {C.cyan(str(idx + 1).rjust(2))}. {cmd:<16s}{C.dim(desc)}")
            print("".join(parts))
        print()

        # Read user selection (disable readline history for this input).
        try:
            import readline  # type: ignore
            old_hist = readline.get_history_length()
            old_comp = readline.get_completer()
            readline.set_completer(None)
        except (ImportError, Exception):
            old_hist = None
            old_comp = None

        try:
            raw = input(C.dim("  select [number/name/q] ❯ ")).strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""

        if old_comp is not None:
            try:
                readline.set_completer(old_comp)
            except Exception:
                pass

        if not raw or raw.lower() in ("q", "quit", "exit"):
            return None

        # Try as a number.
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(items):
                return items[n - 1][0]
            print(C.yellow(f"  invalid number: {n}"))
            return None

        # Try as a command name (with or without leading '/').
        name = raw if raw.startswith("/") else "/" + raw
        for cmd, _desc in items:
            if cmd == name:
                return cmd
            if cmd.startswith(name):
                return cmd

        print(C.yellow(f"  unknown command: {raw}"))
        return None

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    def _handle_slash(self, line: str) -> bool:
        """Handle a slash command. Return True if the REPL should exit."""
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if cmd in ("/quit", "/exit", "/q"):
            return True
        if cmd == "/help":
            self._print_help()
        elif cmd == "/clear":
            self._clear_screen()
        elif cmd == "/new":
            self._active_session_id = None
            print(C.green("✓ new session"))
        elif cmd == "/history":
            self._cmd_history()
        elif cmd == "/retry":
            self._cmd_retry()
        elif cmd == "/undo":
            self._cmd_undo()
        elif cmd == "/reload":
            self._cmd_reload()
        elif cmd == "/model":
            self._cmd_model(rest)
        elif cmd == "/tools":
            self._cmd_tools()
        elif cmd == "/skills":
            self._cmd_skills()
        elif cmd == "/memory":
            self._cmd_memory(rest)
        elif cmd == "/sessions":
            self._cmd_sessions()
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/title":
            self._cmd_title(rest)
        elif cmd == "/compress":
            self._cmd_compress()
        elif cmd == "/config":
            self._cmd_config_show(rest)
        elif cmd == "/usage":
            self._cmd_usage()
        elif cmd == "/reasoning":
            print(C.yellow("reasoning effort: configured via config.model.reasoning_effort (model-dependent)"))
        elif cmd == "/verbose":
            print(C.yellow("verbose mode: use --debug at startup to enable DEBUG-level logs"))
        elif cmd == "/copy":
            self._cmd_copy()
        elif cmd == "/redraw":
            self._clear_screen()
        elif cmd == "/debug":
            self._cmd_debug()
        elif cmd == "/export":
            self._cmd_export(rest)
        elif cmd == "/yolo":
            self._cmd_yolo()
        elif cmd == "/branch":
            self._cmd_branch(rest)
        elif cmd == "/snapshot":
            self._cmd_snapshot(rest)
        elif cmd == "/personality":
            self._cmd_personality(rest)
        elif cmd == "/fast":
            self._cmd_fast()
        else:
            print(C.yellow(f"unknown command: {cmd} (try /help)"))
        return False

    def _print_help(self) -> None:
        print(C.bold("Slash commands:"))
        print(f"  {C.cyan('/help')}                       show this help")
        print(f"  {C.cyan('/quit')}, {C.cyan('/exit')}                exit HermesLite")
        print(f"  {C.cyan('/new')}                        start a new session")
        print(f"  {C.cyan('/history')}                    list messages in this session")
        print(f"  {C.cyan('/retry')}                      re-send the last user message")
        print(f"  {C.cyan('/undo')}                       delete the last assistant turn")
        print(f"  {C.cyan('/title <text>')}               set the current session's title")
        print(f"  {C.cyan('/model [name] [--provider P]')}  show or switch the active model")
        print(f"  {C.cyan('/compress')}                   compress the current session's history")
        print(f"  {C.cyan('/reload')}                     re-scan skills and tools from disk")
        print(f"  {C.cyan('/config [key]')}               show full config (or a dotted key)")
        print(f"  {C.cyan('/tools')}                      list registered tools")
        print(f"  {C.cyan('/skills')}                     list discovered skills")
        print(f"  {C.cyan('/memory <subcmd>')}            memory show/add/del/search")
        print(f"  {C.cyan('/sessions')}                   list saved sessions")
        print(f"  {C.cyan('/status')}                     show session + usage info")
        print(f"  {C.cyan('/usage')}                      show token usage totals")
        print(f"  {C.cyan('/reasoning')}                  show reasoning effort configuration")
        print(f"  {C.cyan('/verbose')}                    show verbose mode info")
        print(f"  {C.cyan('/copy')}                       copy the last assistant reply to the clipboard")
        print(f"  {C.cyan('/redraw')}                     redraw the screen")
        print(f"  {C.cyan('/debug')}                      show the system prompt + tool list")
        print(f"  {C.cyan('/export [file]')}               export session to markdown file (default: hermes-export.md)")
        print(f"  {C.cyan('/clear')}                      clear the screen")
        print(f"  {C.cyan('/yolo')}                       toggle YOLO mode (skip all approvals)")
        print(f"  {C.cyan('/branch [label]')}              branch current session into a new one")
        print(f"  {C.cyan('/snapshot [list|create|restore]')}  manage state snapshots")
        print(f"  {C.cyan('/personality [name]')}          set predefined personality")
        print(f"  {C.cyan('/fast')}                       toggle fast mode (priority processing)")
        print()
        print(C.dim("Type / followed by Tab to see completions."))
        print(C.dim("Anything else is sent to the agent as a user message."))
        print(C.dim("Ctrl-C cancels the current turn; Ctrl-D exits."))

    def _clear_screen(self) -> None:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

    def _cmd_model(self, rest: str) -> None:
        from ..config import load_config, save_config
        cfg = load_config()
        if not rest.strip():
            model = (cfg.get("model") or {}).get("name", "?")
            prov = (cfg.get("model") or {}).get("provider", "?")
            print(f"active model: {C.bright_cyan(model)}  provider: {C.cyan(prov)}")
            return
        # Parse "name --provider X" or just "name"
        if " --provider " in rest:
            name, _, prov = rest.partition(" --provider ")
            name = name.strip()
            prov = prov.strip()
        else:
            name, prov = rest.strip(), None
        cfg.setdefault("model", {})["name"] = name
        if prov:
            cfg["model"]["provider"] = prov
        save_config(cfg)
        # Drop the cached session so the next turn picks up the new model.
        self._active_session_id = None
        print(C.green(f"✓ model set to {name}" + (f" (provider: {prov})" if prov else "")))

    def _cmd_tools(self) -> None:
        from ..tools import registry
        for t in registry.all():
            print(f"  {C.bright_cyan(t.name):24s}  {t.description.splitlines()[0] if t.description else ''}")

    def _cmd_skills(self) -> None:
        from ..skills import discover_skills
        skills = discover_skills()
        if not skills:
            print(C.dim("(no skills found)"))
            return
        for s in skills:
            print(f"  {C.bright_cyan(s.name):24s}  {s.description}")

    def _cmd_memory(self, rest: str) -> None:
        # Delegate to the memory tool implementations.
        from ..tools.registry import registry
        sub = rest.strip()
        if not sub:
            print(registry.call("memory_list", limit=20).to_message())
            return
        parts = sub.split(maxsplit=1)
        op = parts[0]
        if op == "show" and len(parts) > 1:
            print(registry.call("memory_read", key=parts[1]).to_message())
        elif op == "add" and len(parts) > 1:
            kv = parts[1]
            if "=" in kv:
                k, v = kv.split("=", 1)
                print(registry.call("memory_write", key=k.strip(), value=v.strip()).to_message())
            else:
                print(C.yellow("usage: /memory add key=value"))
        elif op == "del" and len(parts) > 1:
            print(registry.call("memory_delete", key=parts[1]).to_message())
        else:
            print(C.yellow("usage: /memory show <key> | add k=v | del <key>"))

    def _cmd_sessions(self) -> None:
        from ..paths import get_state_db_path
        from ..state import StateStore
        state = StateStore(get_state_db_path())
        for s in state.list_sessions(limit=20):
            print(f"  {s.id[:8]}  {C.bright_cyan(s.title or '(untitled)'):40s}  {s.updated_at:.0f}  {s.model}")

    def _cmd_status(self) -> None:
        from ..paths import get_state_db_path
        from ..state import StateStore
        state = StateStore(get_state_db_path())
        sid = self._active_session_id or "(none)"
        print(f"session: {sid}")
        if self._active_session_id:
            u = state.session_usage(self._active_session_id)
            print(f"usage:   prompt={u['prompt_tokens']} completion={u['completion_tokens']} total={u['total_tokens']}")

    def _cmd_title(self, rest: str) -> None:
        if not self._active_session_id:
            print(C.yellow("no active session yet — send a message first"))
            return
        from ..paths import get_state_db_path
        from ..state import StateStore
        state = StateStore(get_state_db_path())
        state.update_session(self._active_session_id, title=rest.strip())
        print(C.green("✓ title updated"))

    # ------------------------------------------------------------------
    # New commands
    # ------------------------------------------------------------------

    def _cmd_history(self) -> None:
        if not self._active_session_id:
            print(C.yellow("no active session"))
            return
        from ..paths import get_state_db_path
        from ..state import StateStore
        state = StateStore(get_state_db_path())
        msgs = state.list_messages(self._active_session_id)
        if not msgs:
            print(C.dim("(empty)"))
            return
        for m in msgs[-30:]:
            role = m.role
            preview = (m.content or "").splitlines()[0][:120] if m.content else ""
            print(f"  [{C.bright_cyan(role):9s}] {preview}")

    def _cmd_retry(self) -> None:
        if not self._active_session_id:
            print(C.yellow("no active session"))
            return
        from ..paths import get_state_db_path
        from ..state import StateStore
        state = StateStore(get_state_db_path())
        msgs = state.list_messages(self._active_session_id)
        # Find the last user message.
        last_user = None
        for m in reversed(msgs):
            if m.role == "user":
                last_user = m
                break
        if last_user is None:
            print(C.yellow("no user message to retry"))
            return
        # Delete the user message and everything after it.
        with state.transaction() as c:
            c.execute("DELETE FROM messages WHERE id >= ?", (last_user.id,))
        print(C.green(f"✓ re-sending: {last_user.content[:80]!r}"))
        self._dispatch_turn(last_user.content)

    def _cmd_undo(self) -> None:
        if not self._active_session_id:
            print(C.yellow("no active session"))
            return
        from ..paths import get_state_db_path
        from ..state import StateStore
        state = StateStore(get_state_db_path())
        msgs = state.list_messages(self._active_session_id)
        # Drop the last assistant message and any tool results that belong to it.
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].role == "assistant":
                # Delete from this index to the end.
                with state.transaction() as c:
                    c.execute("DELETE FROM messages WHERE id >= ?", (msgs[i].id,))
                print(C.green("✓ removed last assistant turn"))
                return
        print(C.yellow("no assistant message to undo"))

    def _cmd_reload(self) -> None:
        from ..skills import discover_skills
        from ..tools import registry
        skills = discover_skills()
        tools = registry.all()
        print(C.green(f"✓ reloaded {len(skills)} skills, {len(tools)} tools"))

    def _cmd_compress(self) -> None:
        if not self._active_session_id:
            print(C.yellow("no active session"))
            return
        from ..config import load_config
        from ..agent.compress import compress_session
        from ..paths import get_state_db_path
        from ..state import StateStore
        from ..providers import active_profile
        cfg = load_config()
        comp_cfg = cfg.get("compression") or {}
        state = StateStore(get_state_db_path())
        profile = active_profile(cfg)
        agent = self._make_agent(self._active_session_id)
        threshold_pct = float(comp_cfg.get("threshold_percent") or 0.50)
        max_ctx = int((cfg.get("model") or {}).get("max_context_tokens") or 128_000)
        abs_threshold = max(int(max_ctx * threshold_pct), 64_000)
        result = compress_session(
            state, self._active_session_id,
            profile=profile, model=agent.model,
            threshold=abs_threshold,
            target=int(comp_cfg.get("target_recent") or 20),
            use_model_summary=bool(comp_cfg.get("use_model_summary")),
        )
        if result.triggered:
            print(C.green(
                f"✓ compressed: {result.tokens_before}→{result.tokens_after} tokens, "
                f"{result.messages_before}→{result.messages_after} messages "
                f"(method: {result.method})"
            ))
        else:
            print(C.yellow(f"no compression needed ({result.tokens_before} tokens)"))

    def _cmd_config_show(self, rest: str) -> None:
        import json as _json
        from ..config import get_value, load_config
        cfg = load_config()
        if rest.strip():
            v = get_value(cfg, rest.strip())
            if v is None:
                print(C.yellow(f"(unset) {rest}"))
            elif isinstance(v, (dict, list)):
                print(_json.dumps(v, indent=2, ensure_ascii=False))
            else:
                print(v)
        else:
            print(_json.dumps(cfg, indent=2, ensure_ascii=False))

    def _cmd_usage(self) -> None:
        from ..paths import get_state_db_path
        from ..state import StateStore
        state = StateStore(get_state_db_path())
        u = state.total_usage()
        print(f"total:  prompt={u['prompt_tokens']}  completion={u['completion_tokens']}  total={u['total_tokens']}")
        if self._active_session_id:
            su = state.session_usage(self._active_session_id)
            print(f"this session: prompt={su['prompt_tokens']}  completion={su['completion_tokens']}  total={su['total_tokens']}")

    def _cmd_copy(self) -> None:
        """Copy the last assistant message to the OS clipboard."""
        if not self._active_session_id:
            print(C.yellow("no active session"))
            return
        from ..paths import get_state_db_path
        from ..state import StateStore
        state = StateStore(get_state_db_path())
        msgs = state.list_messages(self._active_session_id)
        last_asst = None
        for m in reversed(msgs):
            if m.role == "assistant" and m.content:
                last_asst = m.content
                break
        if not last_asst:
            print(C.yellow("no assistant message to copy"))
            return
        try:
            import subprocess
            if sys.platform == "win32":
                p = subprocess.run(["clip"], input=last_asst.encode("utf-8"), check=True)
            elif sys.platform == "darwin":
                p = subprocess.run(["pbcopy"], input=last_asst.encode("utf-8"), check=True)
            else:
                p = subprocess.run(["xclip", "-selection", "clipboard"], input=last_asst.encode("utf-8"), check=False)
                if p.returncode != 0:
                    p = subprocess.run(["xsel", "--clipboard", "--input"], input=last_asst.encode("utf-8"), check=False)
            print(C.green(f"✓ copied {len(last_asst)} chars to clipboard"))
        except (FileNotFoundError, OSError) as exc:
            print(C.yellow(f"clipboard unavailable: {exc}"))

    def _cmd_debug(self) -> None:
        try:
            agent = self._make_agent(self._active_session_id)
            sp = agent.system_prompt
            print(C.bold("=== system prompt ==="))
            print(sp[:1500] + ("\n... [truncated]" if len(sp) > 1500 else ""))
            print()
            print(C.bold(f"=== {len(agent.tools)} tools ==="))
            for t in agent.tools:
                print(f"  {t.name}")
        except Exception as exc:  # noqa: BLE001
            print(C.red(f"debug: {exc}"))

    def _cmd_export(self, rest: str) -> None:
        """Export the current session to a markdown file."""
        if not self._active_session_id:
            print(C.yellow("no active session to export"))
            return
        filename = rest.strip() or "hermes-export.md"
        msgs = self._state.list_messages(self._active_session_id)
        if not msgs:
            print(C.yellow("session is empty — nothing to export"))
            return
        lines = [f"# HermesLite Session Export", ""]
        lines.append(f"- Session: `{self._active_session_id}`")
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
            from pathlib import Path
            Path(filename).write_text("\n".join(lines), encoding="utf-8")
            print(C.green(f"exported {len(msgs)} messages to {filename}"))
        except OSError as exc:
            print(C.red(f"export failed: {exc}"))

    def _cmd_yolo(self) -> None:
        """Toggle YOLO mode (skip all approval prompts)."""
        from ..config import load_config, save_config
        cfg = load_config()
        approvals = cfg.setdefault("approvals", {})
        current = approvals.get("yolo", False)
        approvals["yolo"] = not current
        save_config(cfg)
        state_str = "ON" if not current else "OFF"
        suffix = " — all approvals auto-allowed" if not current else ""
        print(C.green(f"YOLO mode: {state_str}{suffix}"))

    def _cmd_branch(self, rest: str) -> None:
        """Branch the current session into a new one."""
        if not self._active_session_id:
            print(C.yellow("no active session"))
            return
        from ..paths import get_state_db_path
        from ..state import StateStore
        state = StateStore(get_state_db_path())
        try:
            new_id = state.branch_session(self._active_session_id, rest.strip())
            self._active_session_id = new_id
            print(C.green(f"branched to new session: {new_id}"))
        except ValueError as exc:
            print(C.red(str(exc)))

    def _cmd_snapshot(self, rest: str) -> None:
        """Manage state snapshots."""
        from ..snapshot import create_snapshot, list_snapshots, restore_snapshot
        parts = rest.split(maxsplit=1)
        sub = parts[0] if parts else "list"

        if sub == "create":
            label = parts[1].strip() if len(parts) > 1 else ""
            m = create_snapshot(label)
            print(C.green(f"snapshot created: {m['id']} ({m['file_count']} files, {m['total_size']} bytes)"))
        elif sub == "restore":
            if len(parts) < 2:
                print(C.yellow("usage: /snapshot restore <id>"))
                return
            sid = parts[1].strip()
            ok = restore_snapshot(sid)
            if ok:
                print(C.green(f"restored snapshot {sid}"))
            else:
                print(C.red(f"snapshot not found: {sid}"))
        elif sub == "list":
            snaps = list_snapshots()
            if not snaps:
                print(C.dim("(no snapshots)"))
                return
            for s in snaps:
                label_str = f" ({s['label']})" if s.get("label") else ""
                print(f"  {s['id']}{label_str}  — {s['file_count']} files, {s['total_size']} bytes")
        else:
            print(C.yellow("usage: /snapshot [list|create [label]|restore <id>]"))

    def _cmd_personality(self, rest: str) -> None:
        """Set or list predefined personalities."""
        from ..agent.prompt import PERSONALITIES, get_personality_instruction
        from ..config import load_config, save_config

        if not rest.strip():
            print(C.bold("Available personalities:"))
            for name, desc in PERSONALITIES.items():
                print(f"  {C.cyan(name):14s} — {desc}")
            cfg = load_config()
            current = (cfg.get("model") or {}).get("personality", "")
            if current:
                print(f"\nActive: {C.green(current)}")
            else:
                print(f"\nActive: {C.dim('(none)')}")
            return

        name = rest.strip().lower()
        if name == "none":
            cfg = load_config()
            cfg.setdefault("model", {})["personality"] = ""
            save_config(cfg)
            print(C.green("personality cleared"))
            return

        instr = get_personality_instruction(name)
        if not instr:
            print(C.yellow(f"unknown personality: {name}. Use /personality to list."))
            return
        cfg = load_config()
        cfg.setdefault("model", {})["personality"] = name
        save_config(cfg)
        print(C.green(f"personality set to: {name}"))
        # Reset session so next turn uses the new personality
        self._active_session_id = None

    def _cmd_fast(self) -> None:
        """Toggle fast mode (provider-specific priority processing)."""
        from ..config import load_config, save_config
        cfg = load_config()
        model = cfg.setdefault("model", {})
        current = model.get("fast_mode", False)
        model["fast_mode"] = not current
        save_config(cfg)
        state_str = "ON" if not current else "OFF"
        print(C.green(f"fast mode: {state_str}"))

    # ------------------------------------------------------------------
    # Turn dispatch
    # ------------------------------------------------------------------

    def _dispatch_turn(self, user_message: str) -> None:
        agent = self._make_agent(self._active_session_id)
        self._active_session_id = agent.session_id
        self._active_agent = agent
        agent.on_event = self._on_event
        # Stream events to the terminal inline.
        _thinking_active = False  # track whether we're inside a thinking block
        lock = self._output_lock

        def _end_thinking() -> None:
            nonlocal _thinking_active
            if _thinking_active:
                with lock:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                _thinking_active = False

        def event_sink(kind: str, payload: Dict[str, Any]) -> None:
            nonlocal _thinking_active
            if kind == "assistant_text_delta":
                _end_thinking()
                with lock:
                    sys.stdout.write(payload.get("text", ""))
                    sys.stdout.flush()
            elif kind == "assistant_text_done":
                _end_thinking()
                if payload.get("text"):
                    with lock:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                self._on_event(kind, payload)
            elif kind == "thinking_content":
                text = payload.get("text", "")
                if text:
                    with lock:
                        if not _thinking_active:
                            sys.stdout.write(C.dim("  \u2728 thinking...\n"))
                            sys.stdout.flush()
                            _thinking_active = True
                        sys.stdout.write(C.dim(text))
                        sys.stdout.flush()
            elif kind == "tool_call":
                _end_thinking()
                name = payload.get("name")
                args = payload.get("args") or {}
                with lock:
                    sys.stdout.write(C.gray(f"\n  \u2192 {name}({_short_repr(args)})\n"))
                    sys.stdout.flush()
            elif kind == "tool_result":
                _end_thinking()
                ok = payload.get("ok")
                marker = C.green("\u2713") if ok else C.red("\u2717")
                data = payload.get("data")
                error = payload.get("error")
                display = repr(data) if data is not None else ("ERROR: " + error if error else "")
                if len(display) > 80:
                    display = display[:80] + "\u2026"
                with lock:
                    sys.stdout.write(C.gray(f"  {marker} {display}\n"))
                    sys.stdout.flush()
            elif kind == "sudo_request":
                _end_thinking()
                # CLI: prompt the user via /dev/tty, then resolve the
                # pending web sudo event so the agent thread unblocks.
                from ..tools.approval import prompt_sudo_cli, resolve_web_sudo
                request_id = payload.get("request_id", "")
                command = payload.get("command", "")
                result = prompt_sudo_cli(timeout=120.0)
                resolve_web_sudo(
                    request_id,
                    result.get("action", "reject"),
                    password=result.get("password", ""),
                    message=result.get("message", ""),
                )
            elif kind == "approval_request":
                _end_thinking()
                # CLI: prompt the user, then resolve the pending web
                # approval event so the agent thread unblocks.
                from ..tools.approval import prompt_approval_cli, resolve_web_approval
                approval_id = payload.get("approval_id", "")
                command = payload.get("command", "")
                description = payload.get("description", "")
                decision = prompt_approval_cli(command, description=description)
                resolve_web_approval(approval_id, decision)
            else:
                _end_thinking()
                self._on_event(kind, payload)
        agent.on_event = event_sink

        # Run the agent in a daemon thread so the main thread stays
        # responsive to Ctrl-C (KeyboardInterrupt).
        turn_done = threading.Event()
        result_holder: List = [None]
        error_holder: List = [None]

        def _run() -> None:
            try:
                result_holder[0] = agent.run_turn(user_message)
            except BaseException as exc:
                error_holder[0] = exc
            finally:
                turn_done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        # Main thread polls — short timeout allows timely KeyboardInterrupt.
        try:
            while not turn_done.is_set():
                turn_done.wait(timeout=0.1)
        except KeyboardInterrupt:
            agent.interrupt("Cancelled by user")
            # Wait for agent thread to finish (it will exit quickly after
            # interrupt sets the event and closes the stream).
            turn_done.wait(timeout=5.0)
            print(C.yellow("\n  [interrupted — type a new message]"))
            return
        finally:
            self._active_agent = None

        # Re-raise agent-thread exceptions on the main thread.
        exc = error_holder[0]
        if exc is not None:
            if isinstance(exc, KeyboardInterrupt):
                print(C.yellow("\n  [interrupted]"))
                return
            raise exc

        result = result_holder[0]
        if result is None:
            return

        # Auto-title from the first user message if the session is still
        # untitled — mirrors the web frontend's behaviour so that CLI
        # sessions show a meaningful name in the web UI session list.
        if self._active_session_id:
            from ..paths import get_state_db_path
            from ..state import StateStore
            _st = StateStore(get_state_db_path())
            _sess = _st.get_session(self._active_session_id)
            if _sess and not _sess.title:
                title = (user_message or "").strip()[:40]
                if title:
                    _st.update_session(self._active_session_id, title=title)
        # Print a usage footer with optional context window progress bar.
        u = result.usage
        if u.get("total_tokens"):
            max_ctx = u.get("max_context_tokens") or 0
            prompt_tok = u.get("prompt_tokens", 0)
            comp_tok = u.get("completion_tokens", 0)
            total_tok = u.get("total_tokens", 0)
            if max_ctx > 0:
                pct = min(100, round(prompt_tok / max_ctx * 100))
                filled = round(pct / 10)
                bar = "\u2588" * filled + "\u2591" * (10 - filled)
                style = (
                    C.green if pct < 50 else
                    C.yellow if pct < 80 else
                    C.red
                )
                ctx_info = f"{prompt_tok}/{max_ctx} [{style(bar)}] {pct}%"
            else:
                ctx_info = f"{prompt_tok}↓ {comp_tok}↑ {total_tok} tok"
            with lock:
                sys.stdout.write(C.dim(f"  [{result.iterations} iter · {ctx_info}]\n"))
                sys.stdout.flush()

    # ------------------------------------------------------------------
    # Welcome
    # ------------------------------------------------------------------

    def _print_welcome(self) -> None:
        bar = C.gray(C.banner(" HermesLite ", "─", 56))
        print(bar)
        print(C.bold("  Zero-dependency multi-model agent"))
        print()
        print(C.dim("  Type a message and press Enter. /help for commands. Ctrl-D to exit."))
        print()
