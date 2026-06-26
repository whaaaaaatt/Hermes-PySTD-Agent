"""First-run / install / uninstall helpers for HermesLite.

These are the analogues of upstream ``hermes setup`` / ``hermes update`` /
``hermes uninstall``. They live outside the agent loop because they
manage the *install itself* — config, state, skills — rather than
running a model.

Everything in this module is stdlib-only.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_CONFIG, load_config, save_config, set_value
from .paths import get_hermes_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# init / setup
# ---------------------------------------------------------------------------

def init_home(force: bool = False) -> Path:
    """Ensure ``$HERMESLITE_HOME`` exists and contains a default config.

    Returns the home path. If the home is missing the standard files
    (``config.json``, ``state.db``, ``skills/``, ``agent.log``), we
    create them. ``force`` rewrites the default config even if one
    already exists.
    """
    home = get_hermes_home()
    config_path = home / "config.json"
    if config_path.exists() and not force:
        logger.info("init: home already initialised at %s", home)
        return home
    # Make sure the directory tree exists.
    (home / "skills").mkdir(parents=True, exist_ok=True)
    # Write default config.
    save_config(DEFAULT_CONFIG, config_path)
    logger.info("init: wrote default config to %s", config_path)
    return home


def setup_wizard() -> Dict[str, Any]:
    """Interactive first-run setup. Returns the updated config dict.

    We ask the minimum number of questions needed to make the agent
    usable: which provider, the model name, and (for hosted
    providers) confirmation that the API key is set in the
    environment. Everything else stays at defaults and can be
    tuned later via ``hermeslite config set`` or the web UI.
    """
    cfg = load_config()

    print()
    print("Welcome to HermesLite — first-run setup.")
    print()
    print("Pick a provider:")
    providers = list((cfg.get("providers") or {}).keys())
    for i, name in enumerate(providers, 1):
        prof = (cfg.get("providers") or {}).get(name) or {}
        url = prof.get("base_url", "?")
        print(f"  {i}. {name:14s}  {url}")
    print(f"  {len(providers) + 1}. custom (enter a new base_url)")
    print()
    choice_raw = input(f"provider [{providers[0] if providers else 'openai'}]: ").strip()
    if not choice_raw:
        provider_name = providers[0] if providers else "openai"
    else:
        try:
            idx = int(choice_raw) - 1
        except ValueError:
            provider_name = choice_raw
            idx = -1
        if 0 <= idx < len(providers):
            provider_name = providers[idx]
        elif idx == len(providers):
            provider_name = input("custom provider name: ").strip() or "custom"
            base_url = input("base_url (e.g. https://api.openai.com/v1): ").strip()
            if base_url:
                cfg.setdefault("providers", {}).setdefault(provider_name, {})
                cfg["providers"][provider_name]["base_url"] = base_url
        else:
            provider_name = providers[0] if providers else "openai"

    cfg.setdefault("model", {})["provider"] = provider_name

    # API key check.
    prof = (cfg.get("providers") or {}).get(provider_name) or {}
    api_key = (prof or {}).get("api_key") or ""
    api_key_env = (prof or {}).get("api_key_env") or ""
    if api_key:
        masked = api_key[:4] + "…" + api_key[-3:] if len(api_key) > 8 else "***"
        print(f"  OK    api_key in config.json  ({masked})")
    elif api_key_env:
        has = bool(os.environ.get(api_key_env))
        marker = "OK" if has else "MISSING"
        print(f"  {marker}  env var {api_key_env} {'set' if has else 'not set'}")
        if not has and provider_name not in ("ollama",):
            print(f"     (export {api_key_env}=... before running chat)")
            # Offer to paste the key directly into config.
            print()
            print("  Or paste the API key here to store it in config.json instead")
            print("  (less secure than an env var, but works in this session).")
            pasted = input("  api key (empty to skip): ").strip()
            if pasted:
                prof["api_key"] = pasted
                prof.pop("api_key_env", None)
                save_config(cfg)
                print("  saved to config.json")
    else:
        # No key configured at all. Ask for one.
        print(f"  no API key configured for {provider_name}")
        print("  paste one here to store in config.json, or leave empty and set the env var later")
        pasted = input("  api key (empty to skip): ").strip()
        if pasted:
            prof["api_key"] = pasted
            save_config(cfg)
            print("  saved to config.json")

    # Model name.
    default_model = _default_model_for(provider_name)
    model = input(f"model name [{default_model}]: ").strip() or default_model
    cfg["model"]["name"] = model

    save_config(cfg)
    print()
    print(f"Setup complete. Config saved to {get_hermes_home() / 'config.json'}")
    print(f"Try:  hermeslite chat")
    return cfg


def _default_model_for(provider: str) -> str:
    """Pick a sensible default model name per provider."""
    return {
        "openai": "gpt-4o-mini",
        "openrouter": "anthropic/claude-3.5-sonnet",
        "deepseek": "deepseek-chat",
        "ollama": "llama3.1",
    }.get(provider, "gpt-4o-mini")


# ---------------------------------------------------------------------------
# status / reset / uninstall
# ---------------------------------------------------------------------------

def home_status() -> Dict[str, Any]:
    """Return a snapshot of what's installed in the home directory."""
    home = get_hermes_home()
    items: List[Dict[str, Any]] = []
    for child in sorted(home.iterdir()):
        try:
            stat = child.stat()
        except OSError:
            continue
        items.append({
            "name": child.name,
            "is_dir": child.is_dir(),
            "size": stat.st_size,
        })
    return {
        "home": str(home),
        "exists": home.exists(),
        "items": items,
    }


def reset(yes: bool = False) -> bool:
    """Delete state.db + history but keep the config.

    Returns True on success, False on user cancel / failure.
    """
    home = get_hermes_home()
    if not yes:
        print(f"This will delete sessions, messages, memory, and usage from")
        print(f"  {home}")
        print("The config file and installed skills are kept.")
        ans = input("Type 'reset' to confirm: ").strip()
        if ans != "reset":
            print("cancelled.")
            return False
    targets = ["state.db", "state.db-wal", "state.db-shm", "history", "cron.jsonl"]
    removed = 0
    for name in targets:
        p = home / name
        if p.exists():
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                print(f"  could not remove {p}: {exc}")
    # Also remove the cron directory (jobs + output).
    cron_dir = home / "cron"
    if cron_dir.is_dir():
        import shutil
        try:
            shutil.rmtree(cron_dir)
            removed += 1
        except OSError as exc:
            print(f"  could not remove {cron_dir}: {exc}")
    print(f"reset: removed {removed} file(s) from {home}")
    return True


def uninstall(yes: bool = False) -> bool:
    """Delete the entire home directory.

    Requires the user to type ``uninstall`` at the prompt (or pass
    ``--yes``). The Python package itself is NOT removed — this only
    deletes the user-level state directory.
    """
    home = get_hermes_home()
    if not home.exists():
        print(f"{home} does not exist; nothing to uninstall.")
        return True
    if not yes:
        print(f"This will DELETE the directory")
        print(f"  {home}")
        print("All sessions, memory, and config will be lost.")
        ans = input("Type 'uninstall' to confirm: ").strip()
        if ans != "uninstall":
            print("cancelled.")
            return False
    try:
        shutil.rmtree(home)
        print(f"uninstalled: {home}")
        return True
    except OSError as exc:
        print(f"uninstall failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# update (re-install built-in skills)
# ---------------------------------------------------------------------------

def update_skills(force: bool = False) -> int:
    """Re-copy built-in skills into the user's skills directory.

    Without ``--force`` we leave user-edited files alone. With
    ``--force`` we overwrite them — useful after upgrading to a
    new HermesLite release that ships an updated built-in skill.
    """
    from .skills.builtins import install_builtin_skills
    return install_builtin_skills(force=force)
