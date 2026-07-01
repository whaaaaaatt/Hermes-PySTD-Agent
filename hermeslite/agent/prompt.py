"""System-prompt construction.

Combines a default identity block, a list of available tools, a list of
discovered skills, and any extra instructions the user passed at session
start. The output is a single string the model sees at the top of the
conversation.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..skills.loader import Skill, build_skills_prompt_section
from ..tools.registry import Tool


# ---------------------------------------------------------------------------

DEFAULT_IDENTITY = (
    "You are HermesLite, a careful AI agent running locally on the user's machine. "
    "You can call tools to read and edit files, run shell commands, execute Python, "
    "and search the web. Be concise. When a tool call is appropriate, make it; "
    "otherwise answer directly. Always double-check file paths and command syntax "
    "before invoking tools. Prefer small, reversible actions over large irreversible ones."
    "\n\n"
    "Proactively use the `memory` tool to save: user preferences, corrections, "
    "environment facts, project conventions, and anything that will matter in future "
    "sessions. Do not save temporary task state or trivial/obvious information."
    "\n\n"
    "Tool usage rules:\n"
    "- The `todo_*` tools are for PLANNING only. After creating a plan, "
    "execute it immediately using terminal / file tools in the SAME turn. "
    "Do NOT loop on todo updates — add items, then start doing them.\n"
    "- Only ONE todo item should be in_progress at a time.\n"
    "- Mark items done immediately after completing them.\n"
    "- Do not end your turn with a promise of future action — execute it now."
)


# Predefined personalities (referenced by /personality command)
PERSONALITIES: Dict[str, str] = {
    "helpful":     "You are a helpful, friendly assistant.",
    "concise":     "Be extremely concise. Short answers only.",
    "technical":   "Be highly technical and precise.",
    "creative":    "Be creative and imaginative.",
    "teacher":     "Explain concepts clearly like a patient teacher.",
    "kawaii":      "Be cute and adorable, use occasional kaomoji.",
    "pirate":      "Talk like a pirate, matey!",
    "shakespeare": "Speak in Shakespearean English.",
    "philosopher": "Be thoughtful and philosophical.",
}


def get_personality_instruction(name: str) -> str:
    """Return the personality instruction for the given name, or empty string."""
    return PERSONALITIES.get(name.lower(), "")


# Provider-specific operational guidance appended when the active provider
# matches the key. These are short hints the model benefits from knowing.
_PROVIDER_GUIDANCE: Dict[str, str] = {
    "gemini": (
        "You are running on Google Gemini. When using tools, always emit the full "
        "JSON for tool_calls — Gemini does not support partial/ incremental tool "
        "call arguments. If a tool call fails, retry with corrected arguments."
    ),
    "openai": (
        "You are running on an OpenAI model. You may receive thinking tokens if "
        "reasoning is enabled. Be precise with tool call JSON — OpenAI validates "
        "schema strictly."
    ),
    "anthropic": (
        "You are running on Anthropic Claude. Use the provided tools directly "
        "rather than describing what you would do. Claude works best with clear, "
        "specific tool invocations."
    ),
}


# ---------------------------------------------------------------------------

def build_tools_prompt_section(tools: List[Tool]) -> str:
    """Format the tool list for inclusion in the system prompt.

    We do NOT inline the full JSON schema — the model receives the schema
    in the API call's ``tools`` field. The prompt section just gives a
    one-line summary per tool so the model can plan.
    """
    if not tools:
        return ""
    lines = ["# Available Tools", ""]
    lines.append("Each tool has a JSON schema sent with every request.")
    lines.append("Use them by emitting a `tool_calls` block in your response.")
    lines.append("")
    for t in tools:
        lines.append(f"- **{t.name}** — {t.description.splitlines()[0] if t.description else '(no description)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def build_system_prompt(
    cfg: Dict[str, Any],
    tools: List[Tool],
    skills: Optional[List[Skill]] = None,
    extra_instructions: str = "",
    cwd: Optional[str] = None,
    model_override: Optional[str] = None,
) -> str:
    """Compose the full system prompt from the building blocks above."""
    parts: List[str] = [DEFAULT_IDENTITY, ""]
    model_name = model_override or cfg.get("model", {}).get("name", "unknown")
    now = datetime.datetime.now()
    parts.append(f"Date: {now.strftime('%Y-%m-%d')} (weekday: {now.strftime('%A')})")
    parts.append(f"Model: {model_name}")
    parts.append(f"Provider: {cfg.get('model', {}).get('provider', 'unknown')}")
    parts.append(f"Working directory: {cwd or _resolve_cwd()}")
    parts.append(f"Platform: {os.name} ({sys_platform()})")
    parts.append("")

    tools_section = build_tools_prompt_section(tools)
    if tools_section:
        parts.append(tools_section)
        parts.append("")

    # Provider-specific guidance.
    provider_name = (cfg.get("model") or {}).get("provider", "").lower()
    guidance = _PROVIDER_GUIDANCE.get(provider_name)
    if guidance:
        parts.append(guidance)
        parts.append("")

    # Personality instruction (set via /personality command).
    personality = (cfg.get("model") or {}).get("personality", "")
    if personality:
        instr = get_personality_instruction(personality)
        if instr:
            parts.append(f"Personality: {instr}")
            parts.append("")

    skills_section = build_skills_prompt_section(skills)
    if skills_section:
        parts.append(skills_section)
        parts.append("")

    if extra_instructions and extra_instructions.strip():
        parts.append("# Additional Instructions")
        parts.append("")
        parts.append(extra_instructions.strip())
        parts.append("")

    # Scan for context files in the working directory.
    ctx_section = _build_context_files_prompt(cwd)
    if ctx_section:
        parts.append(ctx_section)
        parts.append("")

    # User persona from ~/.hermes-lite/USER.md
    persona = _load_user_persona()
    if persona:
        parts.append("# User Profile")
        parts.append("")
        parts.append(persona)
        parts.append("")

    return "\n".join(parts).strip()


def sys_platform() -> str:
    import sys
    return sys.platform


def _resolve_cwd() -> str:
    """Resolve the working directory using the agent runtime_cwd module."""
    try:
        from .runtime_cwd import resolve_agent_cwd
        return str(resolve_agent_cwd())
    except Exception:  # noqa: BLE001
        return os.getcwd()


# Context-file names we look for in the working directory.
_CONTEXT_FILE_NAMES = (
    "AGENTS.md", "CLAUDE.md", ".cursorrules", ".hermes.md",
    "CONVENTIONS.md", "CODING_GUIDELINES.md", "GLOSSARY.md",
)


def _build_context_files_prompt(cwd: Optional[str] = None) -> str:
    """Scan the working directory for context files and build a prompt section.

    Returns a formatted string with the contents of any discovered files,
    or an empty string if none are found. Files are capped at 4 KB each
    to avoid bloating the prompt.
    """
    if cwd:
        base = Path(cwd)
    else:
        try:
            from .runtime_cwd import resolve_agent_cwd
            base = resolve_agent_cwd()
        except Exception:  # noqa: BLE001
            base = Path.cwd()
    found: List[str] = []
    for name in _CONTEXT_FILE_NAMES:
        p = base / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        found.append(f"## {name}\n\n{text.strip()}")
    if not found:
        return ""
    return "# Project Context Files\n\n" + "\n\n".join(found)


def _load_user_persona() -> str:
    """Load user persona from ~/.hermes-lite/USER.md if it exists.

    Returns the file content (capped at 2 KB) or an empty string.
    """
    try:
        from ..paths import get_hermes_home
        persona_file = get_hermes_home() / "USER.md"
        if persona_file.is_file():
            return persona_file.read_text(encoding="utf-8", errors="replace")[:2048].strip()
    except Exception:  # noqa: BLE001
        pass
    return ""
