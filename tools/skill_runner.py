"""
tools/skill_runner.py
Dispatcher that imports and invokes skill modules by name.
Each skill exposes a run(params: dict, context: ProjectContext) -> dict interface.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

from project_context import ProjectContext

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent.parent / "skills"


def run_skill(skill_name: str, params: dict, context: ProjectContext) -> dict:
    """
    Dynamically import skills/<skill_name>/skill.py and call skill.run(params, context).
    Returns the skill's result dict.
    """
    module_path = f"skills.{skill_name}.skill"
    try:
        mod = importlib.import_module(module_path)
        result = mod.run(params, context)
        logger.info(f"Skill '{skill_name}' completed: {result.get('status', '?')}")
        return result
    except ModuleNotFoundError as e:
        logger.error(f"Skill '{skill_name}' not found: {e}")
        return {"status": "error", "message": f"Skill not found: {skill_name}"}
    except Exception as e:
        logger.exception(f"Skill '{skill_name}' raised an exception")
        return {"status": "error", "message": str(e)}


def list_skills() -> list[str]:
    return [d.name for d in SKILLS_DIR.iterdir() if d.is_dir() and (d / "skill.py").exists()]