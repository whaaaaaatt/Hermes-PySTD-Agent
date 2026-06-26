"""Provider base class — declarative config container.

Mirrors ``providers/base.py`` from the original project but is much
smaller: one class, one method. We don't have a plugin registry for
providers — adding a new one means adding a row to ``DEFAULT_CONFIG`` in
``hermeslite/config.py``. The base class is here so the openai-compat
client can hang behavior off the same object.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ProviderProfile:
    """All provider-specific knobs in one struct.

    API-key resolution order (see :func:`resolve_api_key`):

      1. ``api_key`` literal in config — the key value is used directly.
         Useful for personal setups where the user is OK with a key in
         ``~/.hermes-lite/config.json``. Take care of file permissions.
      2. ``api_key_env`` — the name of an environment variable to read
         at request time. The key never lands on disk.
      3. Neither — no Authorization header is sent. This is the right
         shape for local services like Ollama / vLLM.
    """

    name: str
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    # Optional override for the request timeout, in seconds.
    request_timeout: float = 60.0
    # Default headers attached to every request.
    default_headers: Dict[str, str] = field(default_factory=dict)
    # Human-readable label used in the CLI's ``models`` / ``config show``.
    display_name: str = ""
    # Optional fallback list of model ids; used when ``/models`` fetch
    # fails so the user still sees *something* in the picker.
    fallback_models: List[str] = field(default_factory=list)

    def get_hostname(self) -> str:
        """Extract the hostname from ``base_url`` (for logging)."""
        from urllib.parse import urlparse
        try:
            return urlparse(self.base_url).hostname or ""
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Loader + API-key resolution
# ---------------------------------------------------------------------------

def load_providers(cfg: Dict[str, Any]) -> Dict[str, ProviderProfile]:
    """Build a ``{name: ProviderProfile}`` map from the config dict.

    Reads ``cfg["providers"]`` (a mapping of name → settings) and returns
    profiles. Unknown fields in the settings dict are ignored.
    """
    out: Dict[str, ProviderProfile] = {}
    providers_cfg = cfg.get("providers") or {}
    for name, settings in providers_cfg.items():
        if not isinstance(settings, dict):
            continue
        out[name] = ProviderProfile(
            name=name,
            base_url=(settings.get("base_url") or "").rstrip("/"),
            api_key=settings.get("api_key") or "",
            api_key_env=settings.get("api_key_env") or "",
            request_timeout=float(settings.get("request_timeout") or 60.0),
            default_headers=dict(settings.get("default_headers") or {}),
            display_name=settings.get("display_name") or name,
            fallback_models=list(settings.get("fallback_models") or []),
        )
    return out


def resolve_api_key(profile: ProviderProfile) -> Optional[str]:
    """Return the API key for ``profile``, or ``None`` if no key is set.

    Resolution order:
      1. ``profile.api_key`` (literal value from config)
      2. ``$profile.api_key_env`` (read from the environment)
      3. ``None`` — caller treats this as "send no Authorization header".

    Local endpoints like Ollama / vLLM don't need a real key, so this
    returns ``None`` rather than raising.
    """
    import os
    if profile.api_key:
        return profile.api_key
    if not profile.api_key_env:
        return None
    return os.environ.get(profile.api_key_env) or None


def active_profile(cfg: Dict[str, Any]) -> ProviderProfile:
    """Return the profile corresponding to ``cfg["model"]["provider"]``."""
    providers = load_providers(cfg)
    name = (cfg.get("model") or {}).get("provider") or "openai"
    if name not in providers:
        # Bootstrap: the user's config references an unknown provider.
        # Build a minimal profile so the call doesn't crash; the user
        # will see a clear error when the actual request fails.
        return ProviderProfile(name=name, base_url="", api_key_env="")
    return providers[name]
