"""Skill package — exposes the loader and the ``Skill`` dataclass."""
from .loader import Skill, discover_skills, load_skill_file
from .builtins import SKILLS, install_builtin_skills

__all__ = ["Skill", "discover_skills", "load_skill_file", "SKILLS", "install_builtin_skills"]
