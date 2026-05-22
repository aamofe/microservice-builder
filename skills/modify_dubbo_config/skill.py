"""
skills/modify_dubbo_config/skill.py
Updates Dubbo-specific configuration in an existing module's application.yml.
"""

from __future__ import annotations

import yaml
from pathlib import Path

from project_context import ProjectContext
import logging

logger = logging.getLogger(__name__)


def run(params: dict, context: ProjectContext) -> dict:
    module_name: str = params["module_name"]
    dubbo_overrides: dict = params.get("dubbo_overrides", {})
    # e.g. {"dubbo.protocol.threads": 200, "dubbo.consumer.timeout": 3000}

    module_info = context.modules.get(module_name)
    if not module_info:
        return {"status": "error", "message": f"Module '{module_name}' not found."}

    yml_path = (
        context.module_path(module_name)
        / "src" / "main" / "resources" / "application.yml"
    )
    if not yml_path.exists():
        return {"status": "error", "message": f"application.yml not found at {yml_path}"}

    with open(yml_path) as f:
        config = yaml.safe_load(f) or {}

    # Apply dot-notation overrides
    for key, value in dubbo_overrides.items():
        parts = key.split(".")
        node = config
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    with open(yml_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    logger.info(f"Updated dubbo config in {yml_path}")

    return {
        "status": "success",
        "file": str(yml_path),
        "applied": dubbo_overrides,
    }