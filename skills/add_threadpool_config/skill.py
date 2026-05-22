"""
skills/add_threadpool_config/skill.py
Adds or updates ThreadPoolConfig in an existing module.
"""

from __future__ import annotations

from pathlib import Path

from project_context import ProjectContext
from tools.template_engine import get_engine
import logging

logger = logging.getLogger(__name__)


def run(params: dict, context: ProjectContext) -> dict:
    module_name: str = params["module_name"]
    core_size: int = int(params.get("core_size", 4))
    max_size: int = int(params.get("max_size", 20))
    queue_capacity: int = int(params.get("queue_capacity", 500))

    module_info = context.modules.get(module_name)
    if not module_info:
        return {"status": "error", "message": f"Module '{module_name}' not found."}

    base_package = module_info.base_package
    config_package = base_package + ".config"

    module_path = context.module_path(module_name)
    src_root = module_path / "src" / "main" / "java"
    config_dir = src_root / Path(*config_package.split("."))
    config_dir.mkdir(parents=True, exist_ok=True)

    engine = get_engine()
    tp_ctx = {
        "package": config_package,
        "module_name": module_name,
        "core_size": core_size,
        "max_size": max_size,
        "queue_capacity": queue_capacity,
    }

    out_path = config_dir / "ThreadPoolConfig.java"
    overwrite = params.get("overwrite", False)
    written = engine.write_rendered(
        "threadpool/ThreadPoolConfig.java.j2",
        tp_ctx,
        out_path,
        overwrite=overwrite,
    )

    module_info.has_threadpool = True
    context.save_module(module_info)

    return {
        "status": "success" if written else "skipped",
        "file": str(out_path),
        "message": "ThreadPoolConfig written" if written else "File already exists; use overwrite=True",
    }