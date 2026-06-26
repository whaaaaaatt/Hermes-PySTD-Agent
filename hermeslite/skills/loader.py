"""Skill loader — reads ``SKILL.md`` files from disk.

A skill is a directory containing a ``SKILL.md`` with a YAML-ish
frontmatter block delimited by ``---`` and a markdown body. The
frontmatter holds metadata; the body is what the agent sees in its
system prompt.

We parse the frontmatter with a tiny hand-rolled scanner — the format
is constrained (a few string fields plus a list) so we don't need a
full YAML library. If the file is malformed we return a Skill with
``name`` set to the directory name and an empty body, plus a log
warning; the loader is best-effort.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n(.*)\Z", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str = ""
    version: str = ""
    body: str = ""
    source: str = ""
    # Extra frontmatter fields preserved for callers that want them.
    extra: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(raw: str) -> Tuple[Dict[str, str], str]:
    """Split ``raw`` into ``(frontmatter_dict, body)``.

    Frontmatter is everything between the first ``---`` line and the
    second. We support the small subset of YAML the original project
    uses: ``key: value`` lines and ``key: [a, b]`` lists. Anything
    fancier (block scalars, multiline strings) is left as raw text in
    the value.
    """
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    block, body = m.group(1), m.group(2)
    out: Dict[str, str] = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Strip wrapping quotes if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        out[key] = value
    return out, body


# ---------------------------------------------------------------------------
# File + directory discovery
# ---------------------------------------------------------------------------

def load_skill_file(path: Path) -> Skill:
    """Read a single ``SKILL.md`` and return a :class:`Skill`.

    The ``name`` falls back to the parent directory name when the
    frontmatter is missing. We never raise — malformed skills surface
    as warnings and an empty body.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("skills: cannot read %s: %s", path, exc)
        return Skill(name=path.parent.name, source=str(path.parent))

    fm, body = _parse_frontmatter(raw)
    name = fm.get("name") or path.parent.name
    description = fm.get("description", "")
    version = fm.get("version", "")
    extra = {k: v for k, v in fm.items() if k not in ("name", "description", "version")}
    return Skill(
        name=name,
        description=description,
        version=version,
        body=body.strip(),
        source=str(path.parent),
        extra=extra,
    )


def discover_skills(roots: Optional[List[Path]] = None) -> List[Skill]:
    """Find every ``SKILL.md`` under the given roots and load it.

    Default roots: ``$HERMESLITE_HOME/skills`` plus any extra dirs from
    ``config["skills"]["dirs"]``. Duplicate skill names (last wins) are
    de-duplicated. Skills listed in ``config["skills"]["disabled"]`` are
    excluded from the result.
    """
    from ..config import load_config
    from ..paths import get_skills_dir

    cfg = load_config()
    skills_cfg = cfg.get("skills") or {}
    disabled = set(skills_cfg.get("disabled") or [])

    if roots is None:
        roots = [get_skills_dir()]
        for d in skills_cfg.get("dirs") or []:
            roots.append(Path(os.path.expanduser(d)))

    seen: Dict[str, Skill] = {}
    for root in roots:
        try:
            if not root.exists():
                continue
            for skill_md in root.rglob("SKILL.md"):
                try:
                    skill = load_skill_file(skill_md)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("skills: failed to load %s: %s", skill_md, exc)
                    continue
                if skill.name not in disabled:
                    seen[skill.name] = skill
        except OSError as exc:
            logger.warning("skills: cannot scan %s: %s", root, exc)
    return list(seen.values())


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_skills_prompt_section(skills: Optional[List[Skill]] = None) -> str:
    """Format the skills as a compact system-prompt index.

    Only skill names and descriptions are included — NOT the full body.
    The model should use ``skills_view(name)`` to load the full content
    of a skill when it needs the detailed instructions.  This keeps the
    system prompt short while still giving the model awareness of what
    skills are available.
    """
    if skills is None:
        skills = discover_skills()
    if not skills:
        return ""
    lines = [
        "# Available Skills",
        "",
        "You can invoke any skill listed below with the `skills_view` tool.",
        "Only load a skill when it is relevant to the current task.",
        "",
    ]
    for s in skills:
        desc = (s.description or "(no description)").splitlines()[0]
        lines.append(f"- **{s.name}** — {desc}")
    return "\n".join(lines)
