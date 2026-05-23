"""
skills/create_spring_boot_module/skill.py

v4 变更：
- nacos_host 默认值从 "localhost" 改为 "nacos"
  理由：生成的 application.yml 会被打进 Docker 镜像，容器内必须用服务名 "nacos"
  访问 Nacos，而不是 localhost。本地直接运行可通过环境变量 NACOS_HOST=localhost 覆盖。
- port 继续强制忽略 LLM 传入值，由 context.next_available_port() 分配（v3 已有）
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from project_context import ModuleInfo, ProjectContext
from tools.template_engine import get_engine
from tools.maven_helper import PomEditor
import logging

logger = logging.getLogger(__name__)


def run(params: dict, context: ProjectContext) -> dict:
    module_name: str = params["module_name"]

    # ----------------------------------------------------------------
    # 幂等检查
    # ----------------------------------------------------------------
    if context.module_exists(module_name):
        existing = context.modules[module_name]
        logger.info(f"Module '{module_name}' already exists, skipping creation")
        return {
            "status": "already_exists",
            "module_name": module_name,
            "path": str(context.module_path(module_name)),
            "port": existing.port,
            "assigned_port": existing.port,
            "base_package": existing.base_package,
            "message": (
                f"Module '{module_name}' already exists (port={existing.port}, "
                f"pkg={existing.base_package}). Use incremental_modify to add methods."
            ),
        }

    # ----------------------------------------------------------------
    # 参数解析
    # ----------------------------------------------------------------
    group_id: str = params.get("group_id") or context.get_kv("group_id") or "com.example"
    version: str = params.get("version") or context.get_kv("version") or "1.0.0-SNAPSHOT"
    project_name: str = context.project_name

    base_package: str = params.get("base_package") or (
        group_id + "." + re.sub(r"[^a-zA-Z0-9]", "", module_name)
    )

    # ★ 强制忽略 LLM 传入的 port，统一由系统分配
    if "port" in params and params["port"] is not None:
        logger.warning(
            f"Ignoring LLM-provided port={params['port']} for module '{module_name}'. "
            f"Port is always auto-assigned by context.next_available_port()."
        )
    port: int = context.next_available_port()

    has_async: bool = bool(params.get("has_async", False))
    has_threadpool: bool = bool(params.get("has_threadpool", False))
    module_dependencies: list = params.get("module_dependencies", [])

    module_path = context.module_path(module_name)
    module_path.mkdir(parents=True, exist_ok=True)

    engine = get_engine()
    created_files = []

    # ----------------------------------------------------------------
    # module pom.xml
    # ----------------------------------------------------------------
    pom_ctx = {
        "group_id": group_id,
        "project_name": project_name,
        "version": version,
        "module_name": module_name,
        "has_threadpool": has_threadpool,
        "module_dependencies": module_dependencies,
    }
    pom_path = module_path / "pom.xml"
    if engine.write_rendered("spring-boot-module/pom.xml.j2", pom_ctx, pom_path):
        created_files.append(str(pom_path))

    # ----------------------------------------------------------------
    # Java source directories
    # ----------------------------------------------------------------
    pkg_path = module_path / "src" / "main" / "java" / Path(*base_package.split("."))
    pkg_path.mkdir(parents=True, exist_ok=True)
    (module_path / "src" / "test" / "java" / Path(*base_package.split("."))).mkdir(
        parents=True, exist_ok=True
    )
    resources_path = module_path / "src" / "main" / "resources"
    resources_path.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------
    # Application.java
    # ----------------------------------------------------------------
    class_name = "".join(w.capitalize() for w in re.split(r"[^a-zA-Z0-9]", module_name) if w)
    app_ctx = {
        "base_package": base_package,
        "class_name": class_name,
        "has_async": has_async or has_threadpool,
    }
    app_path = pkg_path / f"{class_name}Application.java"
    if engine.write_rendered("spring-boot-module/Application.java.j2", app_ctx, app_path):
        created_files.append(str(app_path))

    # ----------------------------------------------------------------
    # application.yml
    # ★ nacos_host 默认 "nacos"（容器服务名），本地开发可通过 NACOS_HOST=localhost 覆盖
    # ----------------------------------------------------------------
    nacos_host = os.getenv("NACOS_HOST", "nacos")
    nacos_port = int(os.getenv("NACOS_PORT", 8848))
    dubbo_port = port + 10000
    yml_ctx = {
        "port": port,
        "module_name": module_name,
        "nacos_host": nacos_host,
        "nacos_port": nacos_port,
        "dubbo_port": dubbo_port,
        "base_package": base_package,
    }
    yml_path = resources_path / "application.yml"
    if engine.write_rendered("spring-boot-module/application.yml.j2", yml_ctx, yml_path):
        created_files.append(str(yml_path))

    # ----------------------------------------------------------------
    # ThreadPoolConfig（可选）
    # ----------------------------------------------------------------
    if has_threadpool:
        config_pkg_path = pkg_path / "config"
        config_pkg_path.mkdir(exist_ok=True)
        tp_ctx = {
            "package": base_package + ".config",
            "module_name": module_name,
            "core_size": params.get("thread_core_size", 4),
            "max_size": params.get("thread_max_size", 20),
            "queue_capacity": params.get("thread_queue_capacity", 500),
        }
        tp_path = config_pkg_path / "ThreadPoolConfig.java"
        if engine.write_rendered("threadpool/ThreadPoolConfig.java.j2", tp_ctx, tp_path):
            created_files.append(str(tp_path))

    # ----------------------------------------------------------------
    # 更新父 pom.xml
    # ----------------------------------------------------------------
    parent_pom = context.project_root / "pom.xml"
    if parent_pom.exists():
        editor = PomEditor(parent_pom)
        if editor.add_module(module_name):
            editor.save()
    else:
        all_modules = list(context.modules.keys()) + [module_name]
        parent_ctx = {
            "group_id": group_id,
            "project_name": project_name,
            "version": version,
            "modules": all_modules,
        }
        if engine.write_rendered("spring-boot-module/pom.xml.j2", parent_ctx, parent_pom):
            created_files.append(str(parent_pom))

    # ----------------------------------------------------------------
    # 持久化状态
    # ----------------------------------------------------------------
    m = ModuleInfo(
        name=module_name,
        artifact_id=module_name,
        group_id=group_id,
        version=version,
        base_package=base_package,
        port=port,
        has_threadpool=has_threadpool,
        dependencies=module_dependencies,
    )
    context.save_module(m)
    context.set_kv("group_id", group_id)
    context.set_kv("version", version)

    context.log_change(
        module_name=module_name,
        file_path=".",
        change_type="create",
        description=f"Created module '{module_name}': port={port}, pkg={base_package}, "
                    f"threadpool={has_threadpool}, files={len(created_files)}",
    )

    return {
        "status": "success",
        "module_name": module_name,
        "path": str(module_path),
        "port": port,
        "assigned_port": port,
        "base_package": base_package,
        "created_files": created_files,
        "message": (
            f"Module '{module_name}' created at {module_path} ({len(created_files)} files). "
            f"Assigned port: {port}. Use this port in all subsequent references."
        ),
    }