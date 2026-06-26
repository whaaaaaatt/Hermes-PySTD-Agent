"""Skill discovery and reload tools.

The agent can ask "what skills are loaded?" and (in a future build)
"reload skills from disk". The loader lives in ``hermeslite.skills``;
this module just exposes the information through the tool surface.
"""
from __future__ import annotations

import logging
from typing import Any, List

from ..skills.loader import Skill, discover_skills
from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


class SkillsListTool(Tool):
    name = "skills_list"
    description = (
        "List the skills currently discovered on disk. Returns one line "
        "per skill: `name | description | source_dir`."
    )
    parameters = {"type": "object", "properties": {}}

    def run(self, **_: Any) -> ToolResult:
        try:
            skills: List[Skill] = discover_skills()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        if not skills:
            return ToolResult.success("(no skills found)")
        out = "\n".join(f"{s.name} | {s.description} | {s.source}" for s in skills)
        return ToolResult.success(out)


class SkillsViewTool(Tool):
    name = "skills_view"
    description = "View a single skill's body (markdown after the frontmatter)."
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    def run(self, name: str, **_: Any) -> ToolResult:
        for s in discover_skills():
            if s.name == name:
                return ToolResult.success(s.body)
        return ToolResult.failure(f"no such skill: {name!r}")


registry.register(SkillsListTool())
registry.register(SkillsViewTool())
