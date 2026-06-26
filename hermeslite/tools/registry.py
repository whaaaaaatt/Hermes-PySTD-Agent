"""Tool registry: registry.register(...) decorator + Tool entry dataclass.

Mirrors the upstream ``tools/registry.py`` pattern but in ~150 lines. The
exposed surface:

    from tools.registry import registry, Tool

    @registry.register
    class MyTool(Tool):
        name = "my_tool"
        description = "..."
        parameters = {...}  # JSON schema
        def run(self, **kwargs): return ToolResult(...)

    registry.call("my_tool", arg1=...)  # unified dispatch
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool result
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Uniform result wrapper for all tools.

    ``ok`` is True for success. On failure, ``error`` carries a short
    message; ``data`` may still contain a partial result (e.g. an
    exception class name) but should not be relied on.
    """
    ok: bool
    data: Any = None
    error: str = ""

    @classmethod
    def success(cls, data: Any = None) -> "ToolResult":
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, error: str, data: Any = None) -> "ToolResult":
        return cls(ok=False, error=error, data=data)

    def to_message(self) -> str:
        """Format for inclusion in a tool message back to the model."""
        if self.ok:
            if isinstance(self.data, str):
                return self.data
            return repr(self.data)
        return f"ERROR: {self.error}"


# ---------------------------------------------------------------------------
# Tool base
# ---------------------------------------------------------------------------

class Tool:
    """Subclass and decorate with ``@registry.register`` to expose.

    Subclasses set class-level attributes ``name``, ``description``,
    ``parameters`` (JSON-schema dict), and implement ``run(**kwargs)``.
    ``run`` may be sync only — async-style is not needed for our scale.
    """

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    def run(self, **kwargs) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    # -- register / unregister -----------------------------------------------

    def register(self, tool: Tool) -> Tool:
        """Register an instance (returned for use as a decorator)."""
        if not isinstance(tool, Tool):
            # Allow decoration of classes too: ``@registry.register``
            # applied to a class is the same as instantiating it.
            if inspect.isclass(tool) and issubclass(tool, Tool):
                tool = tool()
            else:
                raise TypeError(
                    f"registry.register expects a Tool instance or subclass, "
                    f"got {type(tool).__name__}"
                )
        if not tool.name:
            raise ValueError(f"tool {type(tool).__name__} has no name")
        if tool.name in self._tools:
            logger.debug("registry: replacing existing tool %s", tool.name)
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> Optional[Tool]:
        return self._tools.pop(name, None)

    # -- lookup --------------------------------------------------------------

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def all(self) -> List[Tool]:
        return [self._tools[n] for n in self.names()]

    # -- call ----------------------------------------------------------------

    def call(self, tool_name: str, *args, **kwargs) -> ToolResult:
        """Invoke a registered tool by name.

        Catches every exception so the agent loop never crashes on a tool
        bug. The original exception's repr is preserved in ``error``.

        The first positional argument is the tool's registry name. We
        use ``tool_name`` (not ``name``) as the parameter so the model
        can pass ``name=`` as a real tool argument without conflict.
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult.failure(f"unknown tool: {tool_name!r}")
        try:
            return tool.run(*args, **kwargs)
        except TypeError as exc:
            # Likely the caller passed a kwarg the tool's signature
            # doesn't accept. Surface a helpful message.
            msg = str(exc)
            if "unexpected keyword argument" in msg or "missing" in msg or "required positional argument" in msg:
                return ToolResult.failure(
                    f"bad arguments for {tool_name}: {exc}. Pass params as kwargs matching the tool's schema."
                )
            logger.exception("tool %s raised", tool_name)
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - intentional catch-all
            logger.exception("tool %s raised", tool_name)
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")

    # -- filter helpers ------------------------------------------------------

    def filter(self, enabled: List[str], disabled: List[str]) -> List[Tool]:
        """Apply the config's enable/disable lists to the registry.

        ``enabled = ["*"]`` means "all"; otherwise it's an explicit allow
        list. ``disabled`` is always an explicit deny list applied second.
        """
        if "*" in enabled:
            allowed = set(self.names())
        else:
            allowed = set(enabled)
        denied = set(disabled or [])
        return [t for t in self.all() if t.name in allowed and t.name not in denied]


# Module-level singleton — code says ``@registry.register`` without
# worrying about import order.
registry = ToolRegistry()
