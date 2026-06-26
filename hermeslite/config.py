"""Configuration loader and writer (JSON, stdlib only).

Replaces hermes-agent's PyYAML-based config with a tiny JSON store. Provides:
- ``load_config(path=None)``: read + merge over ``DEFAULT_CONFIG`` + atomic write
  of the default config on first run
- ``save_config(cfg, path=None)``: atomic write (tmp + ``os.replace``)
- ``deep_merge(base, override)``: recursive dict merge, lists replaced wholesale
- ``get_config_path()``: the user-level config path

We deliberately do NOT use pydantic — this is the entire validation layer
and it's three functions. Keep it that way.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import get_config_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "model": {
        "provider": "openai",
        "name": "gpt-4o-mini",
        # Optional generation parameters. ``null`` means "let the
        # provider decide". Recognised keys:
        #   - temperature        (float, 0.0–2.0)
        #   - max_tokens         (int)
        #   - top_p              (float)
        #   - presence_penalty   (float)
        #   - frequency_penalty  (float)
        #   - reasoning_effort   (str: "low" | "medium" | "high")
        #   - thinking           (bool; convenience flag — auto-converted
        #                          to chat_template_kwargs.enable_thinking
        #                          by the OpenAI-compat provider)
        #   - chat_template_kwargs (dict; passed through verbatim — use
        #                          this for vLLM / Agnes / other providers
        #                          that need nested template kwargs)
        #   - response_format    ({"type": "json_object"} or similar)
        #   - any other provider-specific key (passed through verbatim)
        "options": {
            "temperature": None,
            "max_tokens": None,
            "top_p": None,
            "presence_penalty": None,
            "frequency_penalty": None,
            "reasoning_effort": None,
            "thinking": None,
            "chat_template_kwargs": None,
        },
        # Maximum context window size in tokens. Used for the TUI/Web
        # progress bar and compression threshold. Set to 0 to disable
        # the progress bar display.
        "max_context_tokens": 128000,
    },
    "providers": {
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
        },
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com/v1",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "ollama": {
            "base_url": "http://localhost:11434/v1",
            "api_key_env": "OLLAMA_API_KEY",
        },
    },
    "tools": {
        # Tool names enabled for the agent. "*" = all registered; otherwise list.
        "enabled": ["*"],
        # Explicit disable list (applied after "enabled").
        "disabled": [],
        "python_exec": {"timeout": 30, "max_output": 20000},
        "terminal": {"timeout": 60, "max_output": 20000},
    },
    "skills": {
        "dirs": ["~/.hermes-lite/skills"],
        "enabled": ["*"],
        "disabled": [],          # skill names to exclude (applied after enabled)
    },
    "memory": {
        "enabled": True,
        "max_entries": 1000,
    },
    "compression": {
        "threshold_percent": 0.50,  # fraction of max_context_tokens; 0 disables
        "target_recent": 20,        # messages to keep after compression
        "use_model_summary": False, # set true to call the model for summaries
    },
    "cron": {
        "log_file": "cron.jsonl",
        "max_concurrent": 3,
        "job_timeout": 120,
    },
    "approvals": {
        "enabled": True,
        "hardline_always_block": True,
        "yolo": False,  # skip all approval prompts (auto-allow dangerous commands)
    },
    "web": {
        "host": "127.0.0.1",
        "port": 9119,
        # Auto-generated on first bind-to-non-loopback and persisted here.
        "auth_token": "",
    },
    "tui": {
        "color": "auto",   # "auto" | "on" | "off"
        "history_size": 1000,
        "prompt": "❯ ",
    },
    "delegation": {
        "max_concurrent_children": 3,
        "child_timeout_seconds": 600,
        "max_iterations": 25,
    },
    "terminal": {
        # Agent working directory.  Empty = ``$HERMESLITE_HOME/workspace/``.
        # Set to an absolute path to pin the agent to a specific project.
        "cwd": "",
        "timeout": 60,
        "max_output": 20000,
    },
    "debug": {
        # When enabled the backend emits llm_request / llm_response SSE
        # events and the frontend shows a debug panel (bottom-right).
        "enabled": True,
    },
    "logging": {
        "level": "INFO",
    },
}


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into ``base`` and return the result.

    Semantics:
      - dict + dict → recurse
      - everything else → override wins
      - lists are replaced wholesale (not concatenated)
    """
    out = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """Read a JSON file. Returns ``None`` on missing/empty/invalid; logs once.

    We do NOT raise on parse errors — a corrupt config should not crash the
    CLI. The default config is a complete enough fallback that almost every
    feature still works. We log a warning so the user can fix it.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("config: cannot read %s: %s", path, exc)
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("config: invalid JSON in %s: %s — using defaults", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("config: %s did not contain a JSON object — using defaults", path)
        return None
    return data


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically: tmp file in same dir + ``os.replace``.

    ``os.replace`` is atomic on POSIX and Windows (same volume). We put the
    tmp file in the same directory so the rename stays on a single
    filesystem — otherwise ``os.replace`` would fall back to copy+delete
    on some platforms and lose atomicity.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        # Clean up the tmp file on failure so we don't litter the dir.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load config from ``path`` (default: ``$HERMESLITE_HOME/config.json``).

    Behavior:
      1. If the file does not exist, write the default config there and return it.
      2. If the file exists but is invalid JSON, log a warning and return defaults
         (the broken file is left in place — we never silently overwrite user data).
      3. Otherwise return ``deep_merge(DEFAULT_CONFIG, file_data)``.
    """
    cfg_path = Path(path) if path else get_config_path()
    file_data = _read_json(cfg_path)
    if file_data is None:
        if not cfg_path.exists():
            try:
                _atomic_write_json(cfg_path, DEFAULT_CONFIG)
                logger.info("config: wrote default config to %s", cfg_path)
            except OSError as exc:
                logger.warning("config: cannot write default to %s: %s", cfg_path, exc)
        return deepcopy(DEFAULT_CONFIG)
    return deep_merge(DEFAULT_CONFIG, file_data)


def save_config(cfg: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Atomically write ``cfg`` to the config path.

    Lists and dicts in ``cfg`` are not validated here — we trust the caller
    to construct a sane config. The default config in this module is the
    schema's reference.
    """
    cfg_path = Path(path) if path else get_config_path()
    _atomic_write_json(cfg_path, cfg)


def get_value(cfg: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """Look up ``dotted_key`` (e.g. ``"model.name"``) in a nested dict.

    Returns ``default`` if any segment is missing. Used by ``config get``
    and the CLI for quick reads.
    """
    node: Any = cfg
    for segment in dotted_key.split("."):
        if not isinstance(node, dict) or segment not in node:
            return default
        node = node[segment]
    return node


def set_value(cfg: Dict[str, Any], dotted_key: str, value: Any) -> None:
    """Set ``dotted_key`` to ``value`` in-place, creating intermediate dicts.

    Raises ``ValueError`` on empty key or a non-leaf collision (i.e. trying
    to descend into a list/scalar).
    """
    if not dotted_key:
        raise ValueError("dotted_key must be non-empty")
    segments = dotted_key.split(".")
    node = cfg
    for seg in segments[:-1]:
        nxt = node.get(seg)
        if nxt is None:
            nxt = {}
            node[seg] = nxt
        elif not isinstance(nxt, dict):
            raise ValueError(
                f"cannot descend into non-dict at '{seg}' (have {type(nxt).__name__})"
            )
        node = nxt
    # Coerce scalar strings for known types (int/float/bool/null) so the
    # CLI's `config set foo.bar 42` works without a JSON parser.
    node[segments[-1]] = _coerce_scalar(value)


def _coerce_scalar(value: Any) -> Any:
    """Coerce a string value to int / float / bool / None when possible.

    JSON literals are matched: ``true``/``false``/``null`` (case-insensitive)
    and numeric forms. Anything else stays a string. We do not coerce lists
    or dicts — those callers pass through the CLI as raw JSON.
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~"):
        return None
    # Try int / float — but only if the whole string is the literal number.
    try:
        if s.lstrip("-+").isdigit():
            return int(s)
    except (TypeError, ValueError):
        pass
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    return value
