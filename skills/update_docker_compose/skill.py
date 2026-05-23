"""
skills/update_docker_compose/skill.py

v4 变更：
- mvn clean package → mvn install（通过 MavenValidator.install()）
  原因：consumer 模块（order-service）依赖 provider jar（user-service），
  mvn package 不把 jar 装入本地仓库，导致 consumer 编译失败。
  mvn install 先按 reactor 顺序构建所有模块并 install，再打包，
  保证模块间依赖在本地仓库中可被解析。
"""

from __future__ import annotations

import os
import subprocess
import logging

from project_context import ProjectContext
from tools.docker_compose_builder import DockerComposeBuilder
from tools.validator import MavenValidator

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

    # ── Step 4: mvn install（reactor 整体构建 + 装入本地仓库）──────────
    # 必须用 install 而非 package：consumer 模块依赖 provider jar，
    # install 保证 provider jar 进入本地仓库后 consumer 才能编译通过。
    validator = MavenValidator(context.project_root)
    mvn_result = validator.install(skip_tests=True)
    if not mvn_result["success"]:
        logger.error(f"mvn install failed:\n{mvn_result['output']}")
        return {
            "status": "error",
            "message": f"mvn install failed: {mvn_result['output'][-800:]}",
        }
    logger.info("mvn install succeeded")

    # ── Step 5: docker compose up（--remove-orphans 处理旧容器）────────
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