"""
skills/update_nacos_config/skill.py
Pushes per-module application.yml content to Nacos config center.
"""

from __future__ import annotations

import yaml
from pathlib import Path

from project_context import ProjectContext
from tools.nacos_client import NacosClient
import logging

logger = logging.getLogger(__name__)


def run(params: dict, context: ProjectContext) -> dict:
    module_name: str = params.get("module_name")
    group: str = params.get("group", "DEFAULT_GROUP")
    extra_config: dict = params.get("extra_config", {})

    client = NacosClient()

    if not client.health_check():
        return {
            "status": "warning",
            "message": "Nacos is not reachable; config not pushed. Start Nacos and retry.",
        }

    results = {}
    modules_to_push = (
        [context.modules[module_name]] if module_name else list(context.modules.values())
    )

    for m in modules_to_push:
        app_yml = context.module_path(m.name) / "src" / "main" / "resources" / "application.yml"
        if app_yml.exists():
            with open(app_yml) as f:
                config_data = yaml.safe_load(f) or {}
        else:
            config_data = {}

        config_data.update(extra_config)
        content = yaml.dump(config_data, default_flow_style=False)
        data_id = f"{m.name}.yaml"

        ok = client.publish_config(data_id=data_id, group=group, content=content)
        results[m.name] = "pushed" if ok else "failed"
        logger.info(f"Nacos config push {m.name}: {'ok' if ok else 'failed'}")

    return {
        "status": "success",
        "results": results,
    }