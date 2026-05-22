"""
skills/update_docker_compose/skill.py
生成/更新 docker-compose.yml，执行 mvn package，启动容器。

v3 流程（与 DockerComposeBuilder v3 对应）：
  Step 1 (A+B): ensure_nacos()  — 解析网络 + 决定 nacos 放置
  Step 2 (C):   add_service()   — 为每个模块添加/更新 service
  Step 3:       save()          — 写入 docker-compose.yml
  Step 4:       mvn clean package
  Step 5:       docker compose up -d --build --remove-orphans
                ↑ 用 --remove-orphans 替代先 down 再 up，
                  避免把外部 nacos 容器误 down 掉
"""

from __future__ import annotations

import os
import subprocess
import logging

from project_context import ProjectContext
from tools.docker_compose_builder import DockerComposeBuilder

logger = logging.getLogger(__name__)


def run(params: dict, context: ProjectContext) -> dict:
    registry_mirror: str = params.get("registry_mirror") or os.getenv("DOCKER_REGISTRY_MIRROR", "")
    force_dockerfile: bool = bool(params.get("force_dockerfile", False))

    builder = DockerComposeBuilder(context.project_root)

    # ── Step 1: 网络 + nacos ──────────────────────────────────────────
    nacos_action = builder.ensure_nacos()

    # ── Step 2: 业务服务 ──────────────────────────────────────────────
    results = {}
    for module_name, module_info in context.modules.items():
        action = builder.add_service(
            service_name=module_name,
            module_name=module_name,
            port=module_info.port,
            registry_mirror=registry_mirror,
        )
        results[module_name] = action
        builder.generate_dockerfile(
            context.module_path(module_name),
            module_info.port,
            overwrite=force_dockerfile,
        )
        if action in ("added", "updated"):
            context.log_change(
                module_name=module_name,
                file_path="docker-compose.yml",
                change_type="modify_config",
                description=f"docker-compose service {action}: {module_name} port={module_info.port}",
            )

    # ── Step 3: 保存 ──────────────────────────────────────────────────
    builder.save()

    # ── Step 4: mvn clean package ─────────────────────────────────────
    mvn = subprocess.run(
        ["mvn", "clean", "package", "-DskipTests", "-q", "--no-transfer-progress"],
        cwd=str(context.project_root),
        capture_output=True, text=True,
    )
    if mvn.returncode != 0:
        logger.error(f"mvn package failed:\n{mvn.stderr}")
        return {"status": "error", "message": f"mvn package failed: {mvn.stderr[-800:]}"}
    logger.info("mvn clean package succeeded")

    # ── Step 5: docker compose up（--remove-orphans 处理旧容器）────────
    #   不做 compose down，避免影响外部 nacos 容器
    up = subprocess.run(
        ["docker", "compose", "up", "-d", "--build", "--remove-orphans"],
        cwd=str(context.project_root),
        capture_output=True, text=True,
    )
    if up.returncode != 0:
        logger.error(f"docker compose up failed:\n{up.stderr}")
        return {"status": "error", "message": f"docker compose up failed: {up.stderr[-800:]}"}
    logger.info("docker compose up -d --build --remove-orphans succeeded")

    return {
        "status": "success",
        "compose_file": str(context.project_root / "docker-compose.yml"),
        "nacos_action": nacos_action,
        "services": results,
        "deployed": True,
        "message": (
            f"Nacos: {'reused (external)' if nacos_action == 'existing' else 'started (in compose)'}. "
            f"Services deployed: {', '.join(results.keys())}. "
            f"Access Nacos: http://localhost:8848/nacos (nacos/nacos)"
        ),
    }