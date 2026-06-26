"""Multi-agent profile management.

A profile is a named, isolated workspace under ``~/.hermes-lite/profiles/<name>/``
with its own ``config.json``, ``state.db``, and ``skills/`` directory. The
default profile lives at ``~/.hermes-lite/`` itself (no migration needed).

Active profile is tracked via ``~/.hermes-lite/active_profile`` (plain text file).
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .paths import get_hermes_home

logger = logging.getLogger(__name__)

_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_RESERVED_NAMES = frozenset({"default", "hermes", "test", "tmp", "root", "sudo"})
_PROFILES_DIR = "profiles"
_ACTIVE_PROFILE_FILE = "active_profile"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ProfileInfo:
    """Summary view of a profile."""
    name: str
    path: str
    is_default: bool
    model: Optional[str] = None
    provider: Optional[str] = None
    has_state: bool = False
    skill_count: int = 0


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

def validate_profile_name(name: str) -> str:
    """Validate and normalise a profile name. Raises ValueError on bad names."""
    name = name.strip().lower()
    if not name:
        raise ValueError("profile name cannot be empty")
    if not _PROFILE_NAME_RE.match(name):
        raise ValueError(
            f"invalid profile name {name!r}: must be lowercase alphanumeric "
            "with hyphens/underscores, 1-32 chars"
        )
    if name in _RESERVED_NAMES:
        raise ValueError(f"profile name {name!r} is reserved")
    return name


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _profiles_root() -> Path:
    """Return the ``profiles/`` directory under the default home."""
    root = get_hermes_home() / _PROFILES_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _active_profile_path() -> Path:
    return get_hermes_home() / _ACTIVE_PROFILE_FILE


def get_profile_home(name: str) -> Path:
    """Return the home directory for a named profile."""
    if name == "default":
        return get_hermes_home()
    return _profiles_root() / name


def resolve_profile_home(name: Optional[str] = None) -> Path:
    """Resolve a profile name to its home directory.

    If ``name`` is None, returns the active profile's home.
    If ``name`` is "default", returns the root home.
    """
    if name is None:
        name = get_active_profile()
    if name == "default":
        return get_hermes_home()
    home = _profiles_root() / name
    if not home.is_dir():
        raise FileNotFoundError(f"profile {name!r} not found at {home}")
    return home


# ---------------------------------------------------------------------------
# Active profile
# ---------------------------------------------------------------------------

def get_active_profile() -> str:
    """Read the active profile name from the pointer file.

    Returns ``"default"`` if the file doesn't exist or is empty.
    """
    try:
        text = _active_profile_path().read_text(encoding="utf-8").strip()
        return text if text else "default"
    except FileNotFoundError:
        return "default"


def set_active_profile(name: str) -> None:
    """Write the active profile name to the pointer file.

    Use ``"default"`` to clear the pointer (reverts to root home).
    """
    name = name.strip().lower()
    if name == "default":
        # Remove the pointer file — get_active_profile() returns "default" when absent.
        try:
            _active_profile_path().unlink()
        except FileNotFoundError:
            pass
        logger.info("active profile reset to default")
        return
    name = validate_profile_name(name)
    home = get_profile_home(name)
    if not home.is_dir():
        raise FileNotFoundError(f"profile {name!r} does not exist")
    _active_profile_path().write_text(name + "\n", encoding="utf-8")
    logger.info("active profile set to %s", name)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_profiles() -> List[ProfileInfo]:
    """List all profiles (default + named)."""
    profiles: List[ProfileInfo] = []

    # Default profile.
    default_home = get_hermes_home()
    default_cfg = _read_config(default_home)
    default_model = (default_cfg.get("model") or {}).get("name")
    default_provider = (default_cfg.get("model") or {}).get("provider")
    default_has_state = (default_home / "state.db").exists()
    default_skills = len(list((default_home / "skills").glob("*/SKILL.md"))) if (default_home / "skills").is_dir() else 0
    profiles.append(ProfileInfo(
        name="default",
        path=str(default_home),
        is_default=True,
        model=default_model,
        provider=default_provider,
        has_state=default_has_state,
        skill_count=default_skills,
    ))

    # Named profiles.
    root = _profiles_root()
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            cfg = _read_config(d)
            model = (cfg.get("model") or {}).get("name")
            provider = (cfg.get("model") or {}).get("provider")
            has_state = (d / "state.db").exists()
            skills = len(list((d / "skills").glob("*/SKILL.md"))) if (d / "skills").is_dir() else 0
            profiles.append(ProfileInfo(
                name=d.name,
                path=str(d),
                is_default=False,
                model=model,
                provider=provider,
                has_state=has_state,
                skill_count=skills,
            ))
    return profiles


def create_profile(
    name: str,
    *,
    clone_from: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> ProfileInfo:
    """Create a new profile directory with its own config and state.

    If ``clone_from`` is given, copies config.json from that profile.
    Otherwise starts with defaults. A fresh state.db and skills/ dir
    are always created.
    """
    name = validate_profile_name(name)
    home = get_profile_home(name)
    if home.is_dir():
        raise FileExistsError(f"profile {name!r} already exists at {home}")

    home.mkdir(parents=True, exist_ok=True)
    (home / "skills").mkdir(exist_ok=True)

    # Bootstrap config.
    if clone_from:
        src = get_profile_home(clone_from)
        src_cfg_path = src / "config.json"
        if src_cfg_path.exists():
            shutil.copy2(src_cfg_path, home / "config.json")
        else:
            _write_default_config(home)
    else:
        _write_default_config(home)

    # Apply model/provider overrides if given.
    if model or provider:
        cfg = _read_config(home)
        if model:
            cfg.setdefault("model", {})["name"] = model
        if provider:
            cfg.setdefault("model", {})["provider"] = provider
        _write_config(home, cfg)

    # Create fresh state.db.
    _init_state_db(home)

    logger.info("created profile %s at %s", name, home)
    return ProfileInfo(
        name=name,
        path=str(home),
        is_default=False,
        model=(model or (cfg.get("model") or {}).get("name") if (home / "config.json").exists() else None),
        provider=(provider or (cfg.get("model") or {}).get("provider") if (home / "config.json").exists() else None),
        has_state=True,
        skill_count=0,
    )


def delete_profile(name: str) -> bool:
    """Delete a named profile. Cannot delete the default profile."""
    name = validate_profile_name(name)
    if name == "default":
        raise ValueError("cannot delete the default profile")

    home = get_profile_home(name)
    if not home.is_dir():
        return False

    # If this is the active profile, switch to default first.
    if get_active_profile() == name:
        set_active_profile("default")

    shutil.rmtree(home)
    logger.info("deleted profile %s at %s", name, home)
    return True


def get_profile_config(name: str) -> dict:
    """Read the config for a profile."""
    home = get_profile_home(name)
    return _read_config(home)


def save_profile_config(name: str, cfg: dict) -> None:
    """Save config for a profile."""
    home = get_profile_home(name)
    _write_config(home, cfg)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_config(home: Path) -> dict:
    """Read config.json from a profile home, returning {} on error."""
    import json
    cfg_path = home / "config.json"
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _write_config(home: Path, cfg: dict) -> None:
    """Atomic write of config.json to a profile home."""
    import json
    cfg_path = home / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(cfg_path.parent), suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        import os
        os.replace(tmp, str(cfg_path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_default_config(home: Path) -> None:
    """Write the default config.json to a profile home."""
    from .config import DEFAULT_CONFIG
    from copy import deepcopy
    _write_config(home, deepcopy(DEFAULT_CONFIG))


def _init_state_db(home: Path) -> None:
    """Create a fresh state.db in a profile home."""
    import sqlite3
    db_path = home / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                model TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                parent_id TEXT,
                source TEXT DEFAULT 'cli',
                created_at REAL,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT DEFAULT '',
                tool_calls TEXT,
                tool_call_id TEXT,
                name TEXT,
                reasoning_content TEXT,
                created_at REAL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS memory (
                key TEXT UNIQUE NOT NULL,
                value TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                created_at REAL,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                model TEXT DEFAULT '',
                created_at REAL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS skills_state (
                name TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.commit()
    finally:
        conn.close()
