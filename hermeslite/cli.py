"""HermesLite CLI entry point.

Subcommands:
  - (default) chat            start the interactive REPL
  - chat                      same as default, but explicit
  - web                       start the management web server
  - serve                     alias for `web`
  - config <show|get|set|path>    config inspection
  - models [provider] [--refresh]  list available models
  - tools list                         list registered tools
  - skills list                        list discovered skills
  - sessions list|show|delete         manage sessions
  - memory list|show|add|del           manage persistent memory
  - version                            print version + exit
  - doctor                             diagnostics

All commands share ``--config PATH`` (override the config file) and
``--debug`` (verbose logging).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __release_date__, __version__
from .agent.core import AIAgent
from .config import DEFAULT_CONFIG, get_value, load_config, save_config, set_value
from .logging_util import setup_logging
from .paths import get_config_path, get_hermes_home, get_state_db_path
from .providers import (
    OpenAICompatProvider,
    active_profile,
    load_providers,
    resolve_api_key,
)
from .state import StateStore
from .tui import colors as C
from .tui import colors_enabled, configure


logger = logging.getLogger("hermeslite")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermeslite",
        description="Zero-dependency multi-model agent platform.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=True,
    )
    p.add_argument("--config", help=f"Config file path (default: {get_config_path()})")
    p.add_argument("--profile", help="Config profile name (looks up ~/.hermes-lite/<name>.json)")
    p.add_argument("--debug", action="store_true", help="Verbose logging (DEBUG level)")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override logging level")

    sub = p.add_subparsers(dest="cmd", title="commands", metavar="<command>")

    # chat (default)
    chat = sub.add_parser("chat", help="Start an interactive REPL (default if no command given)")
    chat.add_argument("--model", help="Override the model name for this session")
    chat.add_argument("--provider", help="Override the provider name for this session")
    chat.add_argument("--session", help="Resume the given session id")
    chat.add_argument("--no-stream", action="store_true", help="Disable streaming output")

    # web
    web = sub.add_parser("web", help="Start the management web server")
    web.add_argument("--host", help="Bind address (default: 127.0.0.1 or config.web.host)")
    web.add_argument("--port", type=int, help="Bind port (default: 9119 or config.web.port)")
    web.add_argument("--no-browser", action="store_true", help="Don't try to open the browser automatically")
    web.add_argument("--insecure", action="store_true", help="Allow binding to 0.0.0.0 without a token (not recommended)")

    sub.add_parser("serve", help="Alias for `web`")

    # config
    cfg_p = sub.add_parser("config", help="Inspect or modify config")
    cfg_sub = cfg_p.add_subparsers(dest="config_cmd", required=True)
    cfg_sub.add_parser("show", help="Print the effective config as JSON")
    cfg_sub.add_parser("path", help="Print the config file path")
    g = cfg_sub.add_parser("get", help="Read a value by dotted key")
    g.add_argument("key")
    s = cfg_sub.add_parser("set", help="Set a value by dotted key")
    s.add_argument("key")
    s.add_argument("value")
    w = cfg_sub.add_parser("wizard", help="Run the interactive first-run setup wizard")

    # models
    mod = sub.add_parser("models", help="List available models")
    mod.add_argument("provider", nargs="?", help="Provider name (default: active provider)")
    mod.add_argument("--refresh", action="store_true", help="Skip the cache, refetch the live catalog")

    # tools
    tools_p = sub.add_parser("tools", help="List registered tools")
    tools_sub = tools_p.add_subparsers(dest="tools_cmd")
    tools_sub.add_parser("list", help="List registered tools")

    # skills
    skills_p = sub.add_parser("skills", help="List discovered skills")
    skills_sub = skills_p.add_subparsers(dest="skills_cmd")
    skills_sub.add_parser("list", help="List discovered skills")

    # sessions
    sess = sub.add_parser("sessions", help="Manage sessions")
    sess_sub = sess.add_subparsers(dest="sessions_cmd", required=True)
    sess_sub.add_parser("list", help="List recent sessions")
    show = sess_sub.add_parser("show", help="Print a session's messages")
    show.add_argument("session_id")
    dele = sess_sub.add_parser("delete", help="Delete a session and all its messages")
    dele.add_argument("session_id")

    # memory
    mem = sub.add_parser("memory", help="Manage persistent memory")
    mem_sub = mem.add_subparsers(dest="memory_cmd", required=True)
    mem_sub.add_parser("list", help="List memory entries")
    show = mem_sub.add_parser("show", help="Show a single key")
    show.add_argument("key")
    add = mem_sub.add_parser("add", help="Add or update a key")
    add.add_argument("key")
    add.add_argument("value")
    dele = mem_sub.add_parser("del", help="Delete a key")
    dele.add_argument("key")
    search = mem_sub.add_parser("search", help="Search memory by substring")
    search.add_argument("query")

    # cron
    crn = sub.add_parser("cron", help="Manage scheduled jobs")
    crn_sub = crn.add_subparsers(dest="cron_cmd", required=True)
    crn_sub.add_parser("list", help="List all jobs")
    add = crn_sub.add_parser("add", help="Add a job")
    add.add_argument("name")
    add.add_argument("schedule", help="Schedule: cron expr, 'every 2h', '30m', or ISO timestamp")
    add.add_argument("command", help="Shell command to run")
    rm = crn_sub.add_parser("remove", help="Remove a job by id")
    rm.add_argument("job_id")
    en = crn_sub.add_parser("enable", help="Enable a job")
    en.add_argument("job_id")
    dis = crn_sub.add_parser("disable", help="Disable a job")
    dis.add_argument("job_id")
    run = crn_sub.add_parser("run-once", help="Run a job immediately")
    run.add_argument("job_id")
    crn_sub.add_parser("start", help="Start the scheduler (foreground)")
    crn_sub.add_parser("stop", help="Stop the scheduler")

    # logs
    lg = sub.add_parser("logs", help="View or tail the agent log")
    lg.add_argument("--tail", type=int, default=40, help="Number of lines (default 40)")
    lg.add_argument("--level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Filter by level")

    # dump
    dp = sub.add_parser("dump", help="Export a session to a JSON file")
    dp.add_argument("session_id")
    dp.add_argument("output", nargs="?", default="-", help="Output path; '-' = stdout")

    # profile
    pr = sub.add_parser("profile", help="Manage named configuration profiles")
    pr_sub = pr.add_subparsers(dest="profile_cmd", required=True)
    pr_sub.add_parser("list", help="List profiles found in $HERMESLITE_HOME")
    pr_sub.add_parser("path", help="Show profile directory")
    use = pr_sub.add_parser("use", help="Set the default profile for future invocations")
    use.add_argument("name")

    # redact
    rd = sub.add_parser("redact", help="Print text with secrets redacted")
    rd.add_argument("text", nargs="?", help="Text to redact; if empty, read stdin")
    rd.add_argument("--from-file", help="Read input from this file instead")

    # setup / init / install / uninstall / reset
    setup = sub.add_parser("setup", help="Interactive first-run setup (alias of 'init')")
    setup.add_argument("--yes", action="store_true", help="Accept defaults without prompting")
    init = sub.add_parser("init", help="Initialise $HERMESLITE_HOME with a default config")
    init.add_argument("--force", action="store_true", help="Overwrite an existing config")
    install = sub.add_parser("install", help="Re-install built-in skills into $HERMESLITE_HOME/skills/")
    install.add_argument("--force", action="store_true", help="Overwrite existing skill files")
    un = sub.add_parser("uninstall", help="Delete $HERMESLITE_HOME entirely")
    un.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    rs = sub.add_parser("reset", help="Delete sessions/memory but keep config")
    rs.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    st = sub.add_parser("where", help="Print the home directory path")
    sub.add_parser("doctor", help="Run diagnostics")
    sub.add_parser("version", help="Print the version and exit")

    # completion
    comp = sub.add_parser("completion", help="Generate shell completion script")
    comp.add_argument("shell", choices=["bash", "zsh", "fish"], help="Shell type")

    # status
    sub.add_parser("status", help="Show comprehensive system status")

    return p


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return _dispatch(args)


def _dispatch(args: argparse.Namespace) -> int:
    # Common setup.
    if args.no_color:
        configure(False)
    else:
        # Default color detection uses config.tui.color.
        pass

    # Resolve config early so setup_logging can read the level.
    cfg = _resolve_config(args)
    color_mode = (cfg.get("tui") or {}).get("color") or "auto"
    configure(colors_enabled(color_mode) and not args.no_color)

    log_level = args.log_level or (cfg.get("logging") or {}).get("level") or "INFO"
    if args.debug:
        log_level = "DEBUG"
    setup_logging(log_level)

    cmd = args.cmd or "chat"
    if cmd in ("chat",):
        return _cmd_chat(args, cfg)
    if cmd in ("web", "serve"):
        return _cmd_web(args, cfg)
    if cmd == "config":
        return _cmd_config(args, cfg)
    if cmd == "models":
        return _cmd_models(args, cfg)
    if cmd == "tools":
        return _cmd_tools(args, cfg)
    if cmd == "skills":
        return _cmd_skills(args, cfg)
    if cmd == "sessions":
        return _cmd_sessions(args, cfg)
    if cmd == "memory":
        return _cmd_memory(args, cfg)
    if cmd == "cron":
        return _cmd_cron(args, cfg)
    if cmd == "logs":
        return _cmd_logs(args, cfg)
    if cmd == "dump":
        return _cmd_dump(args, cfg)
    if cmd == "profile":
        return _cmd_profile(args, cfg)
    if cmd == "redact":
        return _cmd_redact(args, cfg)
    if cmd in ("setup",):
        return _cmd_setup(args, cfg)
    if cmd == "init":
        return _cmd_init(args, cfg)
    if cmd == "install":
        return _cmd_install(args, cfg)
    if cmd == "uninstall":
        return _cmd_uninstall(args, cfg)
    if cmd == "reset":
        return _cmd_reset(args, cfg)
    if cmd == "where":
        return _cmd_where(args, cfg)
    if cmd == "doctor":
        return _cmd_doctor(args, cfg)
    if cmd == "version":
        return _cmd_version(args, cfg)
    if cmd == "completion":
        return _cmd_completion(args, cfg)
    if cmd == "status":
        return _cmd_status(args, cfg)
    print(C.red(f"unknown command: {cmd}"))
    return 2


def _resolve_config(args: argparse.Namespace) -> Dict[str, Any]:
    if args.config:
        cfg = load_config(Path(args.config))
    elif args.profile:
        profile_path = get_hermes_home() / f"{args.profile}.json"
        cfg = load_config(profile_path)
    else:
        cfg = load_config()
    return cfg


# ---------------------------------------------------------------------------
# Subcommand: chat
# ---------------------------------------------------------------------------

def _cmd_chat(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .tui.prompt import Repl

    if args.model or args.provider:
        m = cfg.setdefault("model", {})
        if args.model:
            m["name"] = args.model
        if args.provider:
            m["provider"] = args.provider

    # Setup the agent (registers tools + skills).
    from .skills import install_builtin_skills
    install_builtin_skills()

    # Bridge terminal.cwd → $TERMINAL_CWD.
    terminal_cwd = (cfg.get("terminal") or {}).get("cwd", "")
    if terminal_cwd:
        os.environ["TERMINAL_CWD"] = os.path.expanduser(terminal_cwd)

    profile = active_profile(cfg)
    if not profile.base_url:
        print(C.red(f"provider {profile.name!r} has no base_url — run `hermeslite config` to set one"))
        return 1

    # The agent factory closes over a single StateStore and a session
    # id (so successive turns in one REPL session keep appending to the
    # same row). The web frontend uses a different factory.
    from .paths import get_state_db_path
    from .tools import registry as tool_registry
    state = StateStore(get_state_db_path())

    session_id = args.session

    def factory(sid: Optional[str]) -> AIAgent:
        comp_cfg = cfg.get("compression") or {}
        opts = (cfg.get("model") or {}).get("options") or {}
        standard = {"temperature", "max_tokens"}
        extra = {k: v for k, v in opts.items() if k not in standard and v is not None}
        return AIAgent(
            cfg=cfg, profile=profile, registry=tool_registry, state=state,
            session_id=sid or session_id,
            stream=not args.no_stream,
            compress_threshold=float(comp_cfg.get("threshold_percent") or 0),
            compress_target=int(comp_cfg.get("target_recent") or 20),
            extra=extra or None,
        )

    repl = Repl(agent_factory=factory, history_size=(cfg.get("tui") or {}).get("history_size") or 1000)
    return repl.run()


# ---------------------------------------------------------------------------
# Subcommand: web
# ---------------------------------------------------------------------------

def _cmd_web(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .web.server import start_server
    host = args.host or (cfg.get("web") or {}).get("host") or "127.0.0.1"
    port = int(args.port or (cfg.get("web") or {}).get("port") or 9119)
    open_browser = not args.no_browser
    return start_server(
        cfg=cfg, host=host, port=port, open_browser=open_browser,
        allow_public=args.insecure,
    )


# ---------------------------------------------------------------------------
# Subcommand: config
# ---------------------------------------------------------------------------

def _cmd_config(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    sub = args.config_cmd
    if sub == "show":
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return 0
    if sub == "path":
        print(get_config_path())
        return 0
    if sub == "get":
        val = get_value(cfg, args.key, default=None)
        if isinstance(val, (dict, list)):
            print(json.dumps(val, indent=2, ensure_ascii=False))
        elif val is None:
            print(C.yellow(f"(unset) {args.key}"))
        else:
            print(val)
        return 0
    if sub == "set":
        try:
            set_value(cfg, args.key, args.value)
        except ValueError as exc:
            print(C.red(f"error: {exc}"))
            return 1
        # Persist.
        if args.config:
            save_config(cfg, Path(args.config))
        else:
            save_config(cfg)
        print(C.green(f"✓ set {args.key}"))
        return 0
    if sub == "wizard":
        return _run_setup_wizard(cfg)
    return 1


def _run_setup_wizard(cfg: Dict[str, Any]) -> int:
    """Minimal first-run setup: choose provider + model."""
    print(C.bold("HermesLite first-run setup"))
    print()
    print("Select a provider:")
    providers = list((cfg.get("providers") or {}).keys())
    for i, name in enumerate(providers, 1):
        prof = (cfg.get("providers") or {}).get(name) or {}
        url = prof.get("base_url", "?")
        print(f"  {C.cyan(str(i))}. {name}  {C.gray(url)}")
    print(f"  {C.cyan(str(len(providers) + 1))}. custom (enter base_url)")
    choice = input(C.bright_cyan("provider [1]: ") or "1").strip()
    try:
        idx = int(choice) - 1
    except ValueError:
        idx = 0
    if 0 <= idx < len(providers):
        provider_name = providers[idx]
    else:
        provider_name = input("provider name: ").strip() or "openai"
        base_url = input("base_url (e.g. https://api.openai.com/v1): ").strip()
        cfg.setdefault("providers", {}).setdefault(provider_name, {})
        cfg["providers"][provider_name]["base_url"] = base_url
    cfg.setdefault("model", {})["provider"] = provider_name
    model = input(f"model name (default: gpt-4o-mini): ").strip() or "gpt-4o-mini"
    cfg["model"]["name"] = model
    save_config(cfg)
    print(C.green("✓ saved. you can edit further with `hermeslite config set <key> <value>`"))
    return 0


# ---------------------------------------------------------------------------
# Subcommand: models
# ---------------------------------------------------------------------------

def _cmd_models(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    provider_name = args.provider or (cfg.get("model") or {}).get("provider") or "openai"
    providers = load_providers(cfg)
    profile = providers.get(provider_name)
    if profile is None:
        print(C.red(f"unknown provider: {provider_name!r}"))
        return 1
    client = OpenAICompatProvider(profile)
    models = None if args.refresh else None  # we don't cache; the flag is just informational
    try:
        models = client.fetch_models()
    except Exception as exc:  # noqa: BLE001
        print(C.yellow(f"live fetch failed: {exc}"))
    if not models:
        models = profile.fallback_models
        if not models:
            print(C.yellow("(no model list available — set a model with `hermeslite config set model.name <name>`)"))
            return 0
    print(f"  models for {C.bright_cyan(provider_name)}:")
    for m in models:
        print(f"    - {m}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: tools / skills
# ---------------------------------------------------------------------------

def _cmd_tools(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .tools import registry
    enabled = (cfg.get("tools") or {}).get("enabled") or ["*"]
    disabled = (cfg.get("tools") or {}).get("disabled") or []
    visible = set(t.name for t in registry.filter(enabled, disabled))
    for t in registry.all():
        marker = C.green("●") if t.name in visible else C.gray("○")
        print(f"  {marker} {C.bright_cyan(t.name):24s}  {t.description.splitlines()[0] if t.description else ''}")
    return 0


def _cmd_skills(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .skills import discover_skills, install_builtin_skills
    install_builtin_skills()
    skills = discover_skills()
    if not skills:
        print(C.dim("(no skills found)"))
        return 0
    for s in skills:
        print(f"  {C.bright_cyan(s.name):24s}  {s.description}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: sessions / memory
# ---------------------------------------------------------------------------

def _cmd_sessions(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    state = StateStore(get_state_db_path())
    sub = args.sessions_cmd
    if sub == "list":
        for s in state.list_sessions(limit=30):
            print(f"  {s.id[:8]}  {C.bright_cyan(s.title or '(untitled)'):40s}  {s.model}  {s.provider}")
    elif sub == "show":
        s = state.get_session(args.session_id)
        if s is None:
            print(C.red(f"no such session: {args.session_id}"))
            return 1
        print(f"  {C.bold(s.title or '(untitled)')}  {s.id}")
        print(f"  model: {s.model}  provider: {s.provider}  source: {s.source}")
        print()
        for m in state.list_messages(s.id):
            role_color = {
                "user": C.bright_cyan, "assistant": C.green,
                "system": C.gray, "tool": C.yellow,
            }.get(m.role, lambda x: x)
            ts = m.created_at
            print(f"  {C.gray(f'[{m.role}]')} {role_color(_short(m.content, 200))}")
    elif sub == "delete":
        if state.delete_session(args.session_id):
            print(C.green(f"✓ deleted {args.session_id}"))
        else:
            print(C.red(f"no such session: {args.session_id}"))
            return 1
    return 0


def _cmd_memory(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    state = StateStore(get_state_db_path())
    sub = args.memory_cmd
    if sub == "list":
        for r in state.memory_list(limit=50):
            print(f"  {C.bright_cyan(r['key']):24s}  {r['value']}")
    elif sub == "show":
        v = state.memory_get(args.key)
        if v is None:
            print(C.red(f"no such key: {args.key!r}"))
            return 1
        print(v)
    elif sub == "add":
        state.memory_set(args.key, args.value)
        print(C.green(f"✓ wrote {args.key!r}"))
    elif sub == "del":
        if state.memory_delete(args.key):
            print(C.green(f"✓ deleted {args.key!r}"))
        else:
            print(C.red(f"no such key: {args.key!r}"))
            return 1
    elif sub == "search":
        for r in state.memory_search(args.query):
            print(f"  {C.bright_cyan(r['key']):24s}  {r['value']}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: cron
# ---------------------------------------------------------------------------

def _cmd_cron(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .cron import (
        CronSpec, Scheduler, load_jobs, create_job, update_job,
        remove_job, pause_job, resume_job, trigger_job, run_job,
        mark_job_run, parse_schedule, save_job_output,
    )

    if args.cron_cmd == "start":
        sched = Scheduler()
        print("hermeslite cron: scheduler started")
        print("  Ctrl-C to stop")
        try:
            sched.start()
            import time as _t
            while True:
                _t.sleep(60)
        except KeyboardInterrupt:
            sched.stop()
        return 0

    if args.cron_cmd == "stop":
        print("cron: not a daemon. The scheduler only runs while `cron start` is in the foreground.")
        return 0

    if args.cron_cmd == "list":
        jobs = load_jobs()
        if not jobs:
            print("(no cron jobs)")
            return 0
        for j in jobs:
            if j.state == "completed":
                mark = C.gray("○")
            elif j.enabled:
                mark = C.green("●")
            else:
                mark = C.yellow("◌")
            sched_str = j.schedule_display or ""
            next_str = j.next_run_at[:16] if j.next_run_at else "—"
            last_str = j.last_run_at[:16] if j.last_run_at else "—"
            status = j.state or ("scheduled" if j.enabled else "paused")
            print(f"  {mark} {j.id}  {j.name:24s}  {sched_str:20s}  {status:10s}  next: {next_str}  last: {last_str}")
        return 0

    if args.cron_cmd == "add":
        try:
            sched_dict = parse_schedule(args.schedule)
        except ValueError as exc:
            print(C.red(f"bad schedule: {exc}"))
            return 1
        job = create_job(
            name=args.name,
            schedule=args.schedule,
            command=args.command,
        )
        print(C.green(f"✓ added {job.name}: {args.schedule} → {args.command}"))
        print(f"  id: {job.id}")
        return 0

    if args.cron_cmd == "remove":
        if remove_job(args.job_id):
            print(C.green(f"✓ removed {args.job_id}"))
        else:
            print(C.red(f"no such job: {args.job_id}"))
            return 1
        return 0

    if args.cron_cmd == "enable":
        job = update_job(args.job_id, {"enabled": True, "state": "scheduled"})
        if job:
            print(C.green(f"✓ enabled {args.job_id}"))
        else:
            print(C.red(f"no such job: {args.job_id}"))
            return 1
        return 0

    if args.cron_cmd == "disable":
        job = update_job(args.job_id, {"enabled": False})
        if job:
            print(C.green(f"✓ disabled {args.job_id}"))
        else:
            print(C.red(f"no such job: {args.job_id}"))
            return 1
        return 0

    if args.cron_cmd == "run-once":
        jobs = load_jobs()
        job = None
        for j in jobs:
            if j.id == args.job_id:
                job = j
                break
        if job is None:
            print(C.red(f"no such job: {args.job_id}"))
            return 1
        print(f"running {job.name} ({job.id})...")
        try:
            output = run_job(job)
            mark_job_run(job.id, success=True, output=output)
            print(C.green("✓ completed"))
            if output:
                print(output)
        except Exception as exc:
            mark_job_run(job.id, success=False, error=str(exc))
            print(C.red(f"✗ failed: {exc}"))
            return 1
        return 0
    return 1


# ---------------------------------------------------------------------------
# Subcommand: logs / dump / profile / redact
# ---------------------------------------------------------------------------

def _cmd_logs(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .paths import get_hermes_home
    log = get_hermes_home() / "agent.log"
    if not log.exists():
        print(C.yellow("(no log file yet)"))
        return 0
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        print(C.red(f"cannot read log: {exc}"))
        return 1
    if args.level:
        level = args.level
        lines = [ln for ln in lines if f" {level:7s} " in ln]
    for line in lines[-args.tail:]:
        print(line)
    return 0


def _cmd_dump(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    state = StateStore(get_state_db_path())
    sess = state.get_session(args.session_id)
    if sess is None:
        print(C.red(f"no such session: {args.session_id}"))
        return 1
    msgs = state.list_messages(sess.id)
    payload = {
        "session": sess.__dict__,
        "messages": [m.__dict__ for m in msgs],
        "usage": state.session_usage(sess.id),
    }
    out = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    if args.output == "-":
        print(out)
    else:
        try:
            Path(args.output).write_text(out, encoding="utf-8")
            print(C.green(f"✓ wrote {args.output}"))
        except OSError as exc:
            print(C.red(f"cannot write: {exc}"))
            return 1
    return 0


def _cmd_profile(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .paths import get_hermes_home
    home = get_hermes_home()
    if args.profile_cmd == "list":
        for p in sorted(home.glob("*.json")):
            mark = C.green("●") if p.name == "config.json" else C.gray("○")
            print(f"  {mark} {p.stem}")
    elif args.profile_cmd == "path":
        print(home)
    elif args.profile_cmd == "use":
        # Persist by writing a sentinel file the CLI reads at startup.
        (home / ".default_profile").write_text(args.name, encoding="utf-8")
        print(C.green(f"✓ default profile set to {args.name!r}"))
    return 0


def _cmd_redact(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    """Replace API-key / token-looking substrings with ****.

    Useful when piping log output somewhere it shouldn't be public.
    """
    import re as _re
    text = ""
    if args.from_file:
        try:
            text = Path(args.from_file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(C.red(f"cannot read: {exc}"))
            return 1
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read()
    pattern = _re.compile(
        r"(?i)(?:api[_-]?key|token|secret|password|authorization)[\"'\s:=]+([A-Za-z0-9._\-/+=]{8,})"
    )
    out = pattern.sub(lambda m: m.group(0).replace(m.group(1), "***REDACTED***"), text)
    print(out)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: setup / init / install / uninstall / reset / where
# ---------------------------------------------------------------------------

def _cmd_setup(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .setup import init_home, setup_wizard
    # Always ensure the home directory exists.
    home = init_home(force=False)
    print(f"home: {home}")
    if getattr(args, "yes", False):
        # Non-interactive: just create the default config.
        print("non-interactive: wrote default config (no questions asked).")
        return 0
    setup_wizard()
    # Re-load the freshly saved config.
    from .config import load_config
    from .skills import install_builtin_skills
    cfg = load_config()
    install_builtin_skills()
    return 0


def _cmd_init(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    """Non-interactive home initialisation (the safer cousin of ``setup``)."""
    from .setup import init_home
    home = init_home(force=args.force)
    print(f"home: {home}")
    if args.force:
        print("init: rewrote default config (--force)")
    else:
        print("init: config.json present; left untouched (use --force to rewrite)")
    return 0


def _cmd_install(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .setup import update_skills
    n = update_skills(force=args.force)
    if args.force:
        print(f"✓ reinstalled {n} built-in skill(s) (force)")
    else:
        print(f"✓ installed {n} new built-in skill(s) (existing files were kept)")
    return 0


def _cmd_uninstall(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .setup import uninstall
    ok = uninstall(yes=args.yes)
    return 0 if ok else 1


def _cmd_reset(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .setup import reset
    ok = reset(yes=args.yes)
    return 0 if ok else 1


def _cmd_where(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .paths import get_hermes_home
    print(get_hermes_home())
    return 0


# ---------------------------------------------------------------------------
# Subcommand: doctor / version
# ---------------------------------------------------------------------------

def _cmd_doctor(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .skills import install_builtin_skills
    install_builtin_skills()
    print(C.bold("HermesLite doctor"))
    print(f"  version:    {__version__} ({__release_date__})")
    print(f"  python:     {sys.version.split()[0]}")
    print(f"  platform:   {sys.platform}")
    print(f"  home:       {get_hermes_home()}")
    print(f"  config:     {get_config_path()}")
    print(f"  state db:   {get_state_db_path()}")
    print()
    print(C.bold("providers:"))
    for name, prof in load_providers(cfg).items():
        key = resolve_api_key(prof)
        if key:
            masked = key[:4] + "…" + key[-3:] if len(key) > 8 else "***"
            print(f"  {C.green('●')} {name:14s}  {prof.base_url:40s}  key: {masked}")
        else:
            print(f"  {C.yellow('●')} {name:14s}  {prof.base_url:40s}  {C.yellow('(no api key)')}")
    print()
    print(C.bold("tools:"))
    from .tools import registry
    print(f"  {len(registry.all())} registered")
    return 0


def _cmd_version(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    print(f"hermeslite {__version__} ({__release_date__})")
    print(f"python {sys.version.split()[0]}  platform {sys.platform}")
    return 0


def _cmd_completion(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from .completion import get_completion
    print(get_completion(args.shell))
    return 0


def _cmd_status(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    """Show comprehensive system status."""
    print(f"HermesLite v{__version__}")
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    print(f"Home: {get_hermes_home()}")
    print(f"Config: {get_config_path()}")
    print(f"State DB: {get_state_db_path()}")
    print()

    from .providers import load_providers
    providers = load_providers(cfg)
    active = cfg.get("model", {}).get("provider", "?")
    print(f"Active provider: {active}")
    for name, prof in providers.items():
        key_status = "set" if prof.api_key else "missing"
        marker = "●" if name == active else "○"
        print(f"  {marker} {name} ({key_status})")
    print()

    from .skills import discover_skills
    from .tools import registry
    tools = registry.all()
    skills = discover_skills()
    print(f"Tools: {len(tools)} registered")
    print(f"Skills: {len(skills)} discovered")
    print()

    from .state import StateStore
    state = StateStore(get_state_db_path())
    sessions = state.list_sessions(limit=1000)
    print(f"Sessions: {len(sessions)}")
    usage = state.total_usage()
    print(f"Total tokens: {usage['total_tokens']} (prompt={usage['prompt_tokens']}, completion={usage['completion_tokens']})")
    yolo = cfg.get("approvals", {}).get("yolo", False)
    print(f"YOLO mode: {'ON' if yolo else 'OFF'}")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _short(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
