"""Tool package — built-in tools live in sibling modules.

Importing this package runs each tool module's ``@registry.register`` so
the global :data:`registry` (a :class:`ToolRegistry` instance) is
populated. Callers should do ``import hermeslite.tools`` once at
startup to ensure all built-ins are registered before constructing
the agent.
"""
from __future__ import annotations

# Re-export the singleton registry so callers can write
#     from hermeslite.tools import registry
# and get a ToolRegistry instance, not the registry module.
from .registry import registry, Tool, ToolResult, ToolRegistry

# Importing submodules triggers their ``@registry.register`` decorators.
from . import file  # noqa: F401
from . import terminal  # noqa: F401
from . import http  # noqa: F401
from . import python_exec  # noqa: F401
from . import todo  # noqa: F401
from . import memory_tool  # noqa: F401
from . import list_dir  # noqa: F401
from . import search  # noqa: F401
from . import skills_tool  # noqa: F401
from . import data  # noqa: F401
from . import extra  # noqa: F401
from . import skill_manage_tool  # noqa: F401
from . import delegate  # noqa: F401
from . import cron_tool  # noqa: F401

__all__ = ["registry", "Tool", "ToolResult", "ToolRegistry"]
