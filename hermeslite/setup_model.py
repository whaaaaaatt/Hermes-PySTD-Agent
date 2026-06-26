"""Enhanced model provider setup wizard for HermesLite.

Provides a human-friendly interactive flow to configure:
  - Provider selection (with descriptions and default URLs)
  - Base URL configuration (with current value display)
  - API key configuration (checks env AND config, paste or use existing)
  - Model selection (fetches live list from /v1/models endpoint)
  - Context window size (inferred from model or configurable)

Can be run standalone or via the CLI's ``setup model`` subcommand.
Supports non-interactive mode via ``--provider``, ``--model``, ``--api-key``.

This module is stdlib-only — no third-party dependencies.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import load_config, save_config
from .paths import get_config_path, get_hermes_home
from .providers.base import ProviderProfile, load_providers, resolve_api_key

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider catalog — human-readable descriptions + smart defaults
# ---------------------------------------------------------------------------

_PROVIDER_CATALOG: Dict[str, Dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "description": "GPT-4o, GPT-4o-mini, o1, o3 — the original OpenAI API",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "key_url": "https://platform.openai.com/api-keys",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o3-mini"],
        "default_model": "gpt-4o-mini",
        "needs_key": True,
    },
    "openrouter": {
        "name": "OpenRouter",
        "description": "300+ models via one API — Claude, Llama, Gemini, Mistral…",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "key_url": "https://openrouter.ai/keys",
        "models": [
            "anthropic/claude-3.5-sonnet",
            "anthropic/claude-3-opus",
            "meta-llama/llama-3.1-405b-instruct",
            "google/gemini-2.0-flash-exp",
            "mistralai/mistral-large",
            "deepseek/deepseek-chat",
        ],
        "default_model": "anthropic/claude-3.5-sonnet",
        "needs_key": True,
    },
    "deepseek": {
        "name": "DeepSeek",
        "description": "DeepSeek-V3, DeepSeek-R1 — cost-effective reasoning models",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "key_url": "https://platform.deepseek.com/api_keys",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "needs_key": True,
    },
    "ollama": {
        "name": "Ollama (local)",
        "description": "Run LLMs locally — API key optional (for auth proxies)",
        "base_url": "http://localhost:11434/v1",
        "api_key_env": "OLLAMA_API_KEY",
        "key_url": "",
        "models": ["llama3.1", "mistral", "codellama", "qwen2.5-coder"],
        "default_model": "llama3.1",
        "needs_key": False,  # optional: some setups use auth proxies
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "description": "Chinese LLM platform — Qwen, Yi, DeepSeek, GLM…",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "models": [
            "Qwen/Qwen2.5-72B-Instruct",
            "deepseek-ai/DeepSeek-V3",
            "THUDM/glm-4-9b-chat",
            "01-ai/Yi-1.5-34B-Chat",
        ],
        "default_model": "Qwen/Qwen2.5-72B-Instruct",
        "needs_key": True,
    },
    "zhipu": {
        "name": "Zhipu (GLM)",
        "description": "GLM-4, ChatGLM — official Zhipu AI API",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPUAI_API_KEY",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "models": ["glm-4-flash", "glm-4", "glm-4v"],
        "default_model": "glm-4-flash",
        "needs_key": True,
    },
    "moonshot": {
        "name": "Moonshot (Kimi)",
        "description": "Kimi K2 — long context and reasoning",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "default_model": "moonshot-v1-8k",
        "needs_key": True,
    },
    "volcengine": {
        "name": "Volcengine (Doubao)",
        "description": "ByteDance Doubao models — fast and affordable",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key_env": "ARK_API_KEY",
        "key_url": "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        "models": ["doubao-pro-32k", "doubao-lite-32k"],
        "default_model": "doubao-pro-32k",
        "needs_key": True,
    },
    "custom": {
        "name": "Custom (OpenAI-compatible)",
        "description": "Any OpenAI-compatible API — vLLM, LocalAI, LiteLLM, etc.",
        "base_url": "",
        "api_key_env": "",
        "key_url": "",
        "models": [],
        "default_model": "default",
        "needs_key": False,  # optional: depends on the endpoint
    },
}

# ---------------------------------------------------------------------------
# Common context window sizes (fallback when API doesn't provide)
# ---------------------------------------------------------------------------

_CONTEXT_WINDOW_MAP: Dict[str, int] = {
    # OpenAI
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4-turbo": 128000,
    "gpt-4": 8192,
    "gpt-3.5-turbo": 16385,
    "o1": 200000,
    "o1-mini": 128000,
    "o1-pro": 200000,
    "o3": 200000,
    "o3-mini": 200000,
    "o4-mini": 200000,
    # Anthropic (via OpenRouter)
    "claude-3.5-sonnet": 200000,
    "claude-3-opus": 200000,
    "claude-3-sonnet": 200000,
    "claude-3-haiku": 200000,
    # DeepSeek
    "deepseek-chat": 65536,
    "deepseek-reasoner": 65536,
    # Google (via OpenRouter)
    "gemini-2.0-flash": 1048576,
    "gemini-1.5-pro": 1048576,
    "gemini-1.5-flash": 1048576,
    # Meta (via OpenRouter)
    "llama-3.1-405b": 131072,
    "llama-3.1-70b": 131072,
    "llama-3.1-8b": 131072,
    # Moonshot
    "moonshot-v1-8k": 8192,
    "moonshot-v1-32k": 32768,
    "moonshot-v1-128k": 131072,
}


# ---------------------------------------------------------------------------
# ANSI color helpers (minimal, no dependency on tui/colors.py)
# ---------------------------------------------------------------------------

_COLORS_ENABLED = True


def _init_colors() -> None:
    global _COLORS_ENABLED
    if os.environ.get("NO_COLOR") or os.environ.get("HERMESLITE_NO_COLOR"):
        _COLORS_ENABLED = False
    elif hasattr(sys.stdout, "isatty") and not sys.stdout.isatty():
        _COLORS_ENABLED = False


def _c(code: str, text: str) -> str:
    if not _COLORS_ENABLED:
        return text
    return f"\033[{code}m{text}\033[0m"


def _bold(text: str) -> str:
    return _c("1", text)


def _cyan(text: str) -> str:
    return _c("36", text)


def _green(text: str) -> str:
    return _c("32", text)


def _yellow(text: str) -> str:
    return _c("33", text)


def _red(text: str) -> str:
    return _c("31", text)


def _gray(text: str) -> str:
    return _c("90", text)


def _bright_cyan(text: str) -> str:
    return _c("96", text)


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def _prompt(question: str, default: str = "") -> str:
    """Prompt for input with optional default."""
    if default:
        display = f"{question} [{default}]: "
    else:
        display = f"{question}: "
    try:
        value = input(_cyan(display))
        return value.strip() or default
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(1)


def _prompt_password(question: str) -> str:
    """Prompt for a password/API key (masked display)."""
    import getpass
    try:
        value = getpass.getpass(_cyan(f"{question}: "))
        return value.strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(1)


def _prompt_choice(question: str, choices: list, default: int = 0) -> int:
    """Prompt for a numbered choice."""
    print()
    for i, choice in enumerate(choices, 1):
        marker = "→" if i - 1 == default else " "
        if i - 1 == default:
            print(f"  {_green(marker)} {_bold(str(i))}. {choice}")
        else:
            print(f"  {marker} {i}. {choice}")
    print()

    while True:
        try:
            value = input(_cyan(f"  Select [1-{len(choices)}] ({default + 1}): ") or str(default + 1))
            if not value:
                return default
            idx = int(value) - 1
            if 0 <= idx < len(choices):
                return idx
            print(_red(f"  Please enter a number between 1 and {len(choices)}"))
        except ValueError:
            print(_red("  Please enter a number"))
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(1)


def _prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt for yes/no."""
    default_str = "Y/n" if default else "y/N"
    while True:
        try:
            value = input(_cyan(f"{question} [{default_str}]: ")).strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(1)
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print(_red("  Please enter 'y' or 'n'"))


# ---------------------------------------------------------------------------
# API key resolution — checks both env and config
# ---------------------------------------------------------------------------

def _resolve_api_key(provider_name: str, cfg: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Resolve API key from env var and/or config.

    Returns (key, source) where source is one of:
      - "env"      : found in environment variable
      - "config"   : found in config.json (api_key literal)
      - "none"     : not found anywhere
    """
    catalog = _PROVIDER_CATALOG.get(provider_name, {})
    env_var_name = catalog.get("api_key_env", "")

    # 1. Check environment variable first
    if env_var_name:
        env_key = os.environ.get(env_var_name)
        if env_key:
            return env_key, "env"

    # 2. Check config.json (api_key literal)
    config_key = (cfg.get("providers") or {}).get(provider_name, {}).get("api_key", "")
    if config_key:
        return config_key, "config"

    return None, "none"


def _mask_key(key: str) -> str:
    """Mask an API key for display."""
    if len(key) > 8:
        return key[:4] + "…" + key[-3:]
    return "***"


# ---------------------------------------------------------------------------
# Model fetching from /v1/models endpoint
# ---------------------------------------------------------------------------

def _fetch_models_from_endpoint(
    base_url: str,
    api_key: Optional[str] = None,
    timeout: float = 5.0,
) -> Optional[List[str]]:
    """Fetch model list from OpenAI-compatible /v1/models endpoint.

    Returns list of model IDs, or None if the request fails.
    """
    if not base_url:
        return None

    # Normalize URL
    normalized = base_url.strip().rstrip("/")

    # Build candidates: try the URL as-is, and with/without /v1 suffix
    candidates = []
    if normalized.endswith("/v1"):
        candidates.append(normalized)
        candidates.append(normalized[:-3].rstrip("/"))
    else:
        candidates.append(normalized)
        candidates.append(normalized + "/v1")

    # Build headers
    headers = {"User-Agent": "HermesLite/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    for candidate in candidates:
        url = candidate.rstrip("/") + "/models"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
                models = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
                if models:
                    return sorted(models)
        except Exception:
            continue

    return None


def _fetch_model_details(
    base_url: str,
    api_key: Optional[str] = None,
    model_id: str = "",
    timeout: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """Fetch detailed model info from /v1/models/{model_id} endpoint.

    Returns dict with model details (context_length, etc.), or None.
    """
    if not base_url or not model_id:
        return None

    normalized = base_url.strip().rstrip("/")
    url = f"{normalized}/models/{model_id}"

    headers = {"User-Agent": "HermesLite/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _infer_context_window(model_id: str) -> int:
    """Infer context window size from model ID using known mappings."""
    model_lower = model_id.lower()

    # Direct match
    if model_lower in _CONTEXT_WINDOW_MAP:
        return _CONTEXT_WINDOW_MAP[model_lower]

    # Partial match (e.g., "gpt-4o-2024-08-06" matches "gpt-4o")
    for key, tokens in _CONTEXT_WINDOW_MAP.items():
        if key in model_lower:
            return tokens

    # Default for unknown models
    return 128000


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def _show_current_config(cfg: Dict[str, Any]) -> None:
    """Display the current model/provider configuration."""
    print()
    print(_bold("Current configuration:"))
    print()

    model_cfg = cfg.get("model") or {}
    provider_name = model_cfg.get("provider") or "(not set)"
    model_name = model_cfg.get("name") or "(not set)"
    max_ctx = model_cfg.get("max_context_tokens", 0)

    print(f"  {_bold('Provider:'):<16} {_cyan(provider_name)}")
    print(f"  {_bold('Model:'):<16} {_cyan(model_name)}")
    if max_ctx:
        print(f"  {_bold('Context:'):<16} {max_ctx:,} tokens")
    print()

    # Show provider details
    providers = load_providers(cfg)
    if provider_name in providers:
        prof = providers[provider_name]
        key, source = _resolve_api_key(provider_name, cfg)
        if key:
            source_label = f" ({source})"
            print(f"  {_green('●')} API key: {_mask_key(key)}{_gray(source_label)}")
        else:
            print(f"  {_yellow('○')} API key: {_yellow('(not found)')}")
        print(f"  Base URL: {prof.base_url}")
    print()


# ---------------------------------------------------------------------------
# Main wizard flow
# ---------------------------------------------------------------------------

def setup_model(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    non_interactive: bool = False,
) -> Dict[str, Any]:
    """Run the model provider setup wizard.

    Args:
        provider: Provider name (skip interactive selection if provided)
        model: Model name (skip interactive model prompt if provided)
        api_key: API key value (skip interactive key prompt if provided)
        base_url: Custom base URL (skip interactive URL prompt if provided)
        non_interactive: If True, don't ask questions; use provided args or defaults

    Returns:
        The updated config dict (also saved to disk).
    """
    _init_colors()
    cfg = load_config()

    print()
    print(_bold("=" * 60))
    print(_bold("  HermesLite — Model Provider Setup"))
    print(_bold("=" * 60))
    print()
    print(_gray("  Configure which AI provider and model to use."))
    print(_gray("  You can change this later with: hermeslite setup model"))
    print()

    # Show current config
    _show_current_config(cfg)

    # --- Step 1: Provider selection ---
    provider_names = list(_PROVIDER_CATALOG.keys())
    provider_labels = []
    for name in provider_names:
        info = _PROVIDER_CATALOG[name]
        label = f"{info['name']:24s}  {info['description']}"
        provider_labels.append(label)

    if provider and provider in _PROVIDER_CATALOG:
        print(_green(f"  Using provider: {provider}"))
        provider_name = provider
    elif non_interactive:
        provider_name = (cfg.get("model") or {}).get("provider") or "openai"
        print(_green(f"  Non-interactive mode: using provider: {provider_name}"))
    else:
        print(_bold("Step 1: Choose a provider"))
        print()
        idx = _prompt_choice("Provider:", provider_labels)
        provider_name = provider_names[idx]

    catalog = _PROVIDER_CATALOG.get(provider_name, _PROVIDER_CATALOG["custom"])

    # Handle custom provider — ask for name only, base URL comes in Step 1.5
    if provider_name == "custom" and not non_interactive:
        custom_name = _prompt("  Provider name", "custom")
        provider_name = custom_name
        catalog = _PROVIDER_CATALOG["custom"].copy()

    # --- Step 1.5: Base URL (unified for all providers) ---
    current_base_url = (cfg.get("providers") or {}).get(provider_name, {}).get("base_url", "")
    default_base_url = catalog.get("base_url", "")
    display_url = current_base_url or default_base_url or "(not set)"

    if not non_interactive:
        print()
        print(_bold("Step 1.5: Base URL"))
        print()
        print(_gray(f"  Current:  {display_url}"))
        print(_gray("  Leave empty to keep current, or enter a new URL."))
        if provider_name == "custom":
            print(_gray("  Example:  http://localhost:8000/v1"))
        else:
            print(_gray("  Useful for: proxies, mirrors, local servers."))
        print()
        new_url = _prompt("  Base URL")
        if new_url and new_url != display_url:
            cfg.setdefault("providers", {}).setdefault(provider_name, {})
            cfg["providers"][provider_name]["base_url"] = new_url
            display_url = new_url
            print(_green(f"  ✓ Base URL updated to: {new_url}"))
        elif base_url and base_url != display_url:
            # CLI arg override
            cfg.setdefault("providers", {}).setdefault(provider_name, {})
            cfg["providers"][provider_name]["base_url"] = base_url
            display_url = base_url
            print(_green(f"  ✓ Base URL set to: {base_url}"))
    elif base_url:
        # Non-interactive: use CLI arg if provided
        cfg.setdefault("providers", {}).setdefault(provider_name, {})
        cfg["providers"][provider_name]["base_url"] = base_url
        display_url = base_url

    # --- Step 2: API key (checks both env AND config) ---
    print()
    print(_bold("Step 2: API Key"))
    print()

    existing_key, key_source = _resolve_api_key(provider_name, cfg)

    if catalog.get("needs_key"):
        # Provider requires API key
        if existing_key:
            source_label = "environment variable" if key_source == "env" else "config.json"
            print(_green(f"  ✓ API key found in {source_label} ({_mask_key(existing_key)})"))
            if key_source == "env":
                env_name = catalog.get("api_key_env", "?")
                print(_gray(f"    Variable: {env_name}"))
            print()
            if not non_interactive:
                if _prompt_yes_no("  Use this key?", True):
                    pass  # keep using existing
                else:
                    print(_gray("  Paste a new key to store in config.json (or empty to keep existing):"))
                    pasted = _prompt_password("  API key")
                    if pasted:
                        _set_provider_key(cfg, provider_name, pasted)
                        print(_green("  ✓ Saved to config.json"))
        else:
            print(_yellow(f"  No API key found for {provider_name}"))
            env_name = catalog.get("api_key_env", "")
            if env_name:
                print(_gray(f"  Expected env var: {env_name}"))
            if catalog.get("key_url"):
                print(_gray(f"  Get your key at: {catalog['key_url']}"))
            print()
            if not non_interactive:
                print(_gray("  Paste your API key below to store in config.json:"))
                print(_gray("  (or leave empty to set the env var yourself)"))
                pasted = _prompt_password("  API key")
                if pasted:
                    _set_provider_key(cfg, provider_name, pasted)
                    print(_green("  ✓ Saved to config.json"))
                else:
                    if env_name:
                        print()
                        print(_gray(f"  Set it with: export {env_name}=your-key-here"))
            elif api_key:
                # CLI arg
                _set_provider_key(cfg, provider_name, api_key)
                print(_green(f"  ✓ API key saved to config.json"))
            else:
                if env_name:
                    print(_yellow(f"  Set {env_name} before using chat"))
    else:
        # Provider doesn't require API key, but allow optional configuration
        # (e.g., Ollama with auth proxy, custom endpoints with auth)
        print(_gray("  API key is optional for this provider."))
        if existing_key:
            source_label = "environment variable" if key_source == "env" else "config.json"
            print(_green(f"  ✓ API key found in {source_label} ({_mask_key(existing_key)})"))
        print()
        if not non_interactive:
            if _prompt_yes_no("  Configure an API key?", False):
                print(_gray("  Paste your API key (or empty to skip):"))
                pasted = _prompt_password("  API key")
                if pasted:
                    _set_provider_key(cfg, provider_name, pasted)
                    print(_green("  ✓ Saved to config.json"))
                elif existing_key:
                    print(_gray("  Keeping existing key"))
            else:
                print(_gray("  Skipping API key configuration"))
        elif api_key:
            # CLI arg provided
            _set_provider_key(cfg, provider_name, api_key)
            print(_green(f"  ✓ API key saved to config.json"))

    # Re-resolve key after potential save
    final_key, _ = _resolve_api_key(provider_name, cfg)

    # --- Step 3: Model selection (with live fetch) ---
    print()
    print(_bold("Step 3: Default Model"))
    print()

    default_model = catalog.get("default_model", "default")
    fallback_models = catalog.get("models", [])

    # Try to fetch live models from the endpoint
    live_models = None
    if not non_interactive:
        print(_gray("  Fetching available models from endpoint..."))
        live_models = _fetch_models_from_endpoint(display_url, final_key)
        if live_models:
            print(_green(f"  ✓ Found {len(live_models)} models"))
        else:
            print(_yellow("  Could not fetch models (using built-in list)"))
    elif final_key or not catalog.get("needs_key"):
        # Non-interactive: still try to fetch for completeness
        live_models = _fetch_models_from_endpoint(display_url, final_key)

    # Merge: live models first, then fallback (deduped)
    all_models = []
    seen = set()
    if live_models:
        for m in live_models:
            if m not in seen:
                all_models.append(m)
                seen.add(m)
    for m in fallback_models:
        if m not in seen:
            all_models.append(m)
            seen.add(m)

    if model:
        selected_model = model
        print(_green(f"  Using model: {model}"))
    elif non_interactive:
        selected_model = (cfg.get("model") or {}).get("name") or default_model
        print(_green(f"  Non-interactive mode: using model: {selected_model}"))
    else:
        if all_models:
            print(_gray("  Available models:"))
            # Show max 30 models + custom option
            display_models = all_models[:30]
            model_labels = display_models + ["[custom]"]
            if len(all_models) > 30:
                model_labels.insert(30, f"[... and {len(all_models) - 30} more]")
            idx = _prompt_choice("Model:", model_labels)
            if idx < len(display_models):
                selected_model = display_models[idx]
            elif idx == len(display_models) and len(all_models) > 30:
                # "[... and N more]" was selected, prompt for custom
                selected_model = _prompt("  Model name", default_model)
            else:
                selected_model = _prompt("  Model name", default_model)
        else:
            selected_model = _prompt("  Model name", default_model)

    # --- Step 3.5: Context window size ---
    print()
    print(_bold("Step 3.5: Context Window"))
    print()

    # Try to infer from model name
    inferred_ctx = _infer_context_window(selected_model)
    current_max_ctx = (cfg.get("model") or {}).get("max_context_tokens", 128000)

    if not non_interactive:
        print(_gray(f"  Inferred context window: {inferred_ctx:,} tokens"))
        print(_gray(f"  (based on model name, or 128K default)"))
        print()
        max_ctx_input = input(_cyan(f"  Max context tokens: ") or str(inferred_ctx))
        if not max_ctx_input.strip():
            max_ctx = inferred_ctx
        else:
            try:
                max_ctx = int(max_ctx_input)
            except ValueError:
                max_ctx = inferred_ctx
    else:
        max_ctx = current_max_ctx if current_max_ctx else inferred_ctx

    # --- Step 4: Advanced options (optional) ---
    if not non_interactive:
        print()
        print(_bold("Step 4: Advanced Options (optional)"))
        print()

        current_temp = (cfg.get("model", {}).get("options") or {}).get("temperature")
        temp_input = _prompt(f"  Temperature (0.0-2.0, null=provider default)", str(current_temp) if current_temp is not None else "null")
        if temp_input.lower() in ("null", "none", ""):
            temperature = None
        else:
            try:
                temperature = float(temp_input)
            except ValueError:
                temperature = current_temp

        # Thinking mode
        current_thinking = (cfg.get("model", {}).get("options") or {}).get("thinking")
        print()
        print(_gray("  Thinking mode enables the model's reasoning/thinking tokens."))
        print(_gray("  Useful for complex reasoning tasks. Not all models support this."))
        print()
        thinking_input = _prompt("  Enable thinking mode? (yes/no)", "yes" if current_thinking else "no")
        thinking = thinking_input.lower() in ("yes", "y", "true", "1")

        # Reasoning effort (only if thinking is enabled)
        reasoning_effort = None
        if thinking:
            current_effort = (cfg.get("model", {}).get("options") or {}).get("reasoning_effort")
            print()
            print(_gray("  Reasoning effort controls how much compute the model uses for thinking."))
            print(_gray("  Options: low, medium, high (null = provider default)"))
            print()
            effort_input = _prompt("  Reasoning effort (low/medium/high/null)", str(current_effort) if current_effort else "null")
            if effort_input.lower() in ("null", "none", ""):
                reasoning_effort = None
            elif effort_input.lower() in ("low", "medium", "high"):
                reasoning_effort = effort_input.lower()
            else:
                reasoning_effort = current_effort
    else:
        temperature = (cfg.get("model", {}).get("options") or {}).get("temperature")
        thinking = (cfg.get("model", {}).get("options") or {}).get("thinking")
        reasoning_effort = (cfg.get("model", {}).get("options") or {}).get("reasoning_effort")

    # --- Save config ---
    cfg.setdefault("model", {})["provider"] = provider_name
    cfg["model"]["name"] = selected_model
    cfg["model"]["max_context_tokens"] = max_ctx

    # Save options
    options = cfg.setdefault("model", {}).setdefault("options", {})
    if temperature is not None:
        options["temperature"] = temperature
    else:
        options.pop("temperature", None)

    if thinking is not None:
        options["thinking"] = thinking
    else:
        options.pop("thinking", None)

    if reasoning_effort is not None:
        options["reasoning_effort"] = reasoning_effort
    else:
        options.pop("reasoning_effort", None)

    # Ensure the provider entry exists
    if provider_name not in (cfg.get("providers") or {}):
        cfg.setdefault("providers", {})[provider_name] = {
            "base_url": catalog.get("base_url", ""),
        }

    save_config(cfg)

    # --- Summary ---
    print()
    print(_bold("=" * 60))
    print(_green("  ✓ Configuration saved!"))
    print(_bold("=" * 60))
    print()
    print(f"  Config file:  {get_config_path()}")
    print(f"  Home:         {get_hermes_home()}")
    print()
    print(_bold("  Provider:     ") + _cyan(provider_name))
    print(_bold("  Base URL:     ") + _cyan(display_url))
    print(_bold("  Model:        ") + _cyan(selected_model))
    print(_bold("  Max context:  ") + _cyan(f"{max_ctx:,} tokens"))
    print()

    # Next steps
    print(_bold("  Next steps:"))
    print(f"    {_green('hermeslite chat')}            Start chatting")
    print(f"    {_green('hermeslite models')}          List available models")
    print(f"    {_green('hermeslite config set')}      Modify any setting")
    print(f"    {_green('hermeslite doctor')}         Check configuration")
    print()

    return cfg


def _set_provider_key(cfg: Dict[str, Any], provider_name: str, api_key: str) -> None:
    """Set the API key for a provider in the config."""
    cfg.setdefault("providers", {}).setdefault(provider_name, {})
    cfg["providers"][provider_name]["api_key"] = api_key
    # Remove api_key_env since we're storing the literal key
    cfg["providers"][provider_name].pop("api_key_env", None)


# ---------------------------------------------------------------------------
# Status check utility
# ---------------------------------------------------------------------------

def check_provider_status() -> int:
    """Print a quick status of all configured providers. Returns 0 on success."""
    _init_colors()
    cfg = load_config()
    providers = load_providers(cfg)

    print()
    print(_bold("Provider status:"))
    print()

    if not providers:
        print(_yellow("  No providers configured. Run: hermeslite setup model"))
        return 1

    active = (cfg.get("model") or {}).get("provider") or ""

    for name, prof in providers.items():
        marker = _green("●") if name == active else _gray("○")
        key, source = _resolve_api_key(name, cfg)
        if key:
            source_label = f" ({source})"
            key_info = _green(f"key: {_mask_key(key)}{_gray(source_label)}")
        else:
            key_info = _yellow("no key")
        print(f"  {marker} {name:16s} {prof.base_url:40s}  {key_info}")

    print()
    print(_gray(f"  Active model: {active} / {(cfg.get('model') or {}).get('name', '?')}"))
    print()
    return 0


# ---------------------------------------------------------------------------
# CLI entry point (for standalone use)
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI entry point: ``python -m hermeslite.setup_model [options]``."""
    import argparse

    p = argparse.ArgumentParser(
        description="HermesLite model provider setup wizard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python -m hermeslite.setup_model                   Interactive setup\n"
               "  python -m hermeslite.setup_model --provider openai Quick OpenAI setup\n"
               "  python -m hermeslite.setup_model --status          Show provider status\n",
    )
    p.add_argument("--provider", help="Provider name (skip selection)")
    p.add_argument("--model", help="Model name (skip model prompt)")
    p.add_argument("--api-key", help="API key (skip key prompt)")
    p.add_argument("--base-url", help="Custom base URL")
    p.add_argument("--non-interactive", "-y", action="store_true",
                   help="Non-interactive mode: use provided args or defaults")
    p.add_argument("--status", action="store_true",
                   help="Show provider status and exit")

    args = p.parse_args()

    if args.status:
        return check_provider_status()

    setup_model(
        provider=args.provider,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        non_interactive=args.non_interactive,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
