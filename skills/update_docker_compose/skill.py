"""
skills/update_docker_compose/skill.py
生成或增量更新 docker-compose.yml。

v2 核心逻辑：
1. 先通过 docker inspect 检测 nacos 容器是否真实运行 → 同步到 GlobalNacosState
2. ensure_nacos() 根据全局状态决定：新增 nacos service 还是 external 网络复用
3. 每个 module 的 service 幂等添加（已存在只更新 env/ports）
4. save() 后若是首次添加 nacos，标记 GlobalNacosState.mark_running()
5. 记录变更到 context.log_change()
"""

from __future__ import annotations

import os
from pathlib import Path

from project_context import ProjectContext, GlobalNacosState
from tools.docker_compose_builder import DockerComposeBuilder
import logging

logger = logging.getLogger(__name__)


def run(params: dict, context: ProjectContext) -> dict:
    registry_mirror: str = params.get("registry_mirror") or os.getenv("DOCKER_REGISTRY_MIRROR", "")
    force_dockerfile: bool = bool(params.get("force_dockerfile", False))

    builder = DockerComposeBuilder(context.project_root)

    # ----------------------------------------------------------------
    # Step 1: 真实检测 nacos 运行状态，同步到全局 state
    # ----------------------------------------------------------------
    nacos_actually_running = DockerComposeBuilder.is_nacos_container_running()
    if nacos_actually_running and not GlobalNacosState.is_running():
        # docker 里确实有 nacos 容器，但全局 state 未记录 → 补充标记
        GlobalNacosState.mark_running(
            compose_file="external (detected via docker inspect)",
            network="microservice-net",
        )
        logger.info("Detected running nacos container, marked in GlobalNacosState")

    # ----------------------------------------------------------------
    # Step 2: 确保 nacos 配置存在（新增 or 复用）
    # ----------------------------------------------------------------
    nacos_action = builder.ensure_nacos()   # "added" | "existing"

    # ----------------------------------------------------------------
    # Step 3: 为每个 module 添加/更新 service
    # ----------------------------------------------------------------
    results = {}
    for module_name, module_info in context.modules.items():
        action = builder.add_service(
            service_name=module_name,
            module_name=module_name,
            port=module_info.port,
            registry_mirror=registry_mirror,
        )
        results[module_name] = action

        # 生成 Dockerfile（幂等）
        module_path = context.module_path(module_name)
        dockerfile_written = builder.generate_dockerfile(
            module_path, module_info.port, overwrite=force_dockerfile
        )

        if action in ("added", "updated"):
            context.log_change(
                module_name=module_name,
                file_path="docker-compose.yml",
                change_type="modify_config",
                description=f"docker-compose service {action}: {module_name} port={module_info.port}",
            )

    # ----------------------------------------------------------------
    # Step 4: 保存
    # ----------------------------------------------------------------
    builder.save()

    # Step 5: 如果本次新增了 nacos service，标记全局 state
    if nacos_action == "added":
        GlobalNacosState.mark_running(
            compose_file=str(context.project_root / "docker-compose.yml"),
            network="microservice-net",
        )

    compose_path = str(context.project_root / "docker-compose.yml")

    return {
        "status": "success",
        "compose_file": compose_path,
        "nacos_action": nacos_action,          # "added" | "existing"
        "services": results,                    # {module_name: "added"|"updated"|"unchanged"}
        "message": _build_message(nacos_action, results, context.project_root),
    }


def _build_message(nacos_action: str, services: dict, project_root: Path) -> str:
    lines = []
    if nacos_action == "existing":
        lines.append("✓ Nacos: 复用已运行的 nacos 容器（未重复声明）")
    else:
        lines.append("✓ Nacos: 已加入 docker-compose（首次）")

    for svc, action in services.items():
        symbol = {"added": "+", "updated": "~", "unchanged": "="}[action]
        lines.append(f"  [{symbol}] {svc} ({action})")

    lines.append("")
    lines.append(f"docker-compose.yml: {project_root / 'docker-compose.yml'}")
    lines.append("")
    lines.append("启动命令:")
    if nacos_action == "existing":
        lines.append(f"  cd {project_root}")
        lines.append("  docker-compose up -d   # nacos 已在运行，只启动业务服务")
    else:
        lines.append(f"  cd {project_root}")
        lines.append("  docker-compose up -d   # 含 nacos + 所有服务")
    lines.append("  # Nacos 控制台: http://localhost:8848/nacos  (nacos/nacos)")
    return "\n".join(lines)