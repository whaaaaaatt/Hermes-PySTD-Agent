"""Skill management tool — create, edit, patch, delete skills.

Ported from hermes-agent-ref/tools/skill_manager_tool.py (simplified).
No YAML dependency (hand-rolled frontmatter validation), no security
scanner, no category support, no write_file/remove_file actions.

Skills live in ~/.hermes-lite/skills/<name>/SKILL.md.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000

# Characters allowed in skill names (filesystem-safe, URL-friendly)
VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')


def _skills_dir() -> Path:
    """Return ~/.hermes-lite/skills/."""
    from ..paths import get_hermes_home
    return get_hermes_home() / "skills"


def _validate_name(name: str) -> Optional[str]:
    """Validate a skill name. Returns error message or None if valid."""
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def _validate_frontmatter(content: str) -> Optional[str]:
    """Validate that SKILL.md content has proper frontmatter with required fields.

    Returns error message or None if valid.
    """
    if not content.strip():
        return "Content cannot be empty."

    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    end_match = re.search(r'\n---\s*\n', content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    fm_text = content[3:end_match.start() + 3]

    # Hand-rolled YAML parse: extract name: and description: fields
    fm_dict: Dict[str, str] = {}
    for line in fm_text.split('\n'):
        line = line.strip()
        if ':' in line:
            key, _, val = line.partition(':')
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key in ('name', 'description'):
                fm_dict[key] = val

    if "name" not in fm_dict:
        return "Frontmatter must include 'name' field."
    if "description" not in fm_dict:
        return "Frontmatter must include 'description' field."
    if len(fm_dict.get("description", "")) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."

    body = content[end_match.end() + 3:].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    return None


def _find_skill(name: str) -> Optional[Path]:
    """Find a skill directory by name. Returns skill dir path or None."""
    skills_dir = _skills_dir()
    if not skills_dir.exists():
        return None
    for skill_md in skills_dir.rglob("SKILL.md"):
        if skill_md.parent.name == name:
            return skill_md.parent
    return None


def _atomic_write_text(file_path: Path, content: str) -> None:
    """Atomically write text content to a file."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".{file_path.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, file_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


class SkillManageTool(Tool):
    name = "skill_manage"
    description = (
        "Manage skills — your procedural memory for recurring tasks. "
        "Actions: create (new SKILL.md), edit (full rewrite), patch "
        "(find-and-replace), delete (remove skill directory). "
        "Skills live in ~/.hermes-lite/skills/<name>/SKILL.md."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "edit", "patch", "delete"],
                "description": "The action to perform.",
            },
            "name": {
                "type": "string",
                "description": (
                    "Skill name (lowercase, hyphens/underscores, max 64 chars). "
                    "Must match an existing skill for edit/patch/delete."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Full SKILL.md content (YAML frontmatter + markdown body). "
                    "Required for 'create' and 'edit'."
                ),
            },
            "old_string": {
                "type": "string",
                "description": "Text to find in SKILL.md (required for 'patch'). Must be unique.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text (required for 'patch').",
            },
        },
        "required": ["action", "name"],
    }

    def run(
        self,
        action: str,
        name: str,
        content: str = "",
        old_string: str = "",
        new_string: str = "",
        **_: Any,
    ) -> ToolResult:
        if action == "create":
            return self._create(name, content)
        if action == "edit":
            return self._edit(name, content)
        if action == "patch":
            return self._patch(name, old_string, new_string)
        if action == "delete":
            return self._delete(name)
        return ToolResult.failure(f"Unknown action '{action}'. Use: create, edit, patch, delete")

    def _create(self, name: str, content: str) -> ToolResult:
        err = _validate_name(name)
        if err:
            return ToolResult.failure(err)
        if not content:
            return ToolResult.failure("content is required for 'create'. Provide the full SKILL.md text.")
        err = _validate_frontmatter(content)
        if err:
            return ToolResult.failure(err)
        if len(content) > MAX_SKILL_CONTENT_CHARS:
            return ToolResult.failure(f"Content is {len(content):,} chars (limit: {MAX_SKILL_CONTENT_CHARS:,}).")
        existing = _find_skill(name)
        if existing:
            return ToolResult.failure(f"Skill '{name}' already exists at {existing}.")
        skill_dir = _skills_dir() / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        _atomic_write_text(skill_md, content)
        return ToolResult.success(f"Skill '{name}' created at {skill_md}")

    def _edit(self, name: str, content: str) -> ToolResult:
        if not content:
            return ToolResult.failure("content is required for 'edit'. Provide the full updated SKILL.md text.")
        err = _validate_frontmatter(content)
        if err:
            return ToolResult.failure(err)
        if len(content) > MAX_SKILL_CONTENT_CHARS:
            return ToolResult.failure(f"Content is {len(content):,} chars (limit: {MAX_SKILL_CONTENT_CHARS:,}).")
        existing = _find_skill(name)
        if not existing:
            return ToolResult.failure(f"Skill '{name}' not found. Use skills_list to see available skills.")
        skill_md = existing / "SKILL.md"
        _atomic_write_text(skill_md, content)
        return ToolResult.success(f"Skill '{name}' updated.")

    def _patch(self, name: str, old_string: str, new_string: str) -> ToolResult:
        if not old_string:
            return ToolResult.failure("old_string is required for 'patch'.")
        if new_string is None:
            return ToolResult.failure("new_string is required for 'patch'.")
        existing = _find_skill(name)
        if not existing:
            return ToolResult.failure(f"Skill '{name}' not found. Use skills_list to see available skills.")
        skill_md = existing / "SKILL.md"
        if not skill_md.exists():
            return ToolResult.failure(f"SKILL.md not found in skill '{name}'.")
        content = skill_md.read_text(encoding="utf-8")
        count = content.count(old_string)
        if count == 0:
            return ToolResult.failure("old_string not found in SKILL.md.")
        if count > 1:
            return ToolResult.failure(
                f"old_string appears {count} times — make it unique or use replace_all."
            )
        new_content = content.replace(old_string, new_string, 1)
        err = _validate_frontmatter(new_content)
        if err:
            return ToolResult.failure(f"Patch would break SKILL.md structure: {err}")
        _atomic_write_text(skill_md, new_content)
        return ToolResult.success(f"Patched SKILL.md in skill '{name}'.")

    def _delete(self, name: str) -> ToolResult:
        existing = _find_skill(name)
        if not existing:
            return ToolResult.failure(f"Skill '{name}' not found. Use skills_list to see available skills.")
        shutil.rmtree(existing)
        # Clean up empty parent directories
        parent = existing.parent
        skills_root = _skills_dir()
        if parent != skills_root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
        return ToolResult.success(f"Skill '{name}' deleted.")


registry.register(SkillManageTool())
