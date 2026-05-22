"""
tools/docker_compose_builder.py
生成并增量更新 docker-compose.yml。

v3 重构要点：
- 三步走，职责分明：
    Step A: 网络（_resolve_network）
      → microservice-net 存在 → external: true
      → 不存在            → driver: bridge（compose 自动创建）
    Step B: nacos（ensure_nacos）
      → 容器真实在跑 → 从 services 里移除，不重复启动
      → 不在跑       → 加入 services，depends_on 用 service_healthy
    Step C: 业务服务（add_service）
      → depends_on nacos 仅在 nacos 被本 compose 管理时才加
- 去掉 GlobalNacosState 依赖，单一事实来源 = docker inspect
- 移除过时的 version 字段
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)

NACOS_CONTAINER_NAME = "nacos"
NETWORK_NAME = "microservice-net"

NACOS_SERVICE = {
    "image": "nacos/nacos-server:v2.3.0",
    "container_name": NACOS_CONTAINER_NAME,
    "environment": {
        "MODE": "standalone",
        "NACOS_AUTH_ENABLE": "false",
        "JVM_XMS": "256m",
        "JVM_XMX": "512m",
    },
    "ports": ["8848:8848", "9848:9848"],
    "healthcheck": {
        "test": ["CMD", "curl", "-f", "http://localhost:8848/nacos/actuator/health"],
        "interval": "10s",
        "timeout": "5s",
        "retries": 10,
        "start_period": "30s",
    },
    "networks": [NETWORK_NAME],
    "restart": "unless-stopped",
}


class DockerComposeBuilder:

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.compose_path = project_root / "docker-compose.yml"
        self._data: dict = self._load_or_init()
        # Will be set by ensure_nacos(); add_service() reads it
        self._nacos_in_compose: bool = NACOS_CONTAINER_NAME in self._data.get("services", {})

    def _load_or_init(self) -> dict:
        if self.compose_path.exists():
            try:
                with open(self.compose_path) as f:
                    data = yaml.safe_load(f) or {}
                data.setdefault("services", {})
                # Strip obsolete version field
                data.pop("version", None)
                return data
            except Exception as e:
                logger.warning(f"Failed to load compose file: {e}, starting fresh")
        return {"services": {}}

    # ──────────────────────────────────────────────────────────────────────────
    # Step A: Network
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _network_exists() -> bool:
        """Check whether microservice-net already exists in Docker."""
        try:
            result = subprocess.run(
                ["docker", "network", "inspect", NETWORK_NAME],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _resolve_network(self) -> str:
        """
        Decide network declaration for this compose file.
        Returns "external" or "bridge".
        - external: network already exists, just reference it
        - bridge:   network doesn't exist yet, let compose create it
        """
        if self._network_exists():
            logger.info(f"Network '{NETWORK_NAME}' exists → external: true")
            self._data["networks"] = {
                NETWORK_NAME: {"external": True}
            }
            return "external"
        else:
            logger.info(f"Network '{NETWORK_NAME}' not found → driver: bridge (compose will create)")
            self._data["networks"] = {
                NETWORK_NAME: {"driver": "bridge"}
            }
            return "bridge"

    # ──────────────────────────────────────────────────────────────────────────
    # Step B: Nacos
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _nacos_running() -> bool:
        """True if the nacos container is currently running."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", NACOS_CONTAINER_NAME],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip() == "true"
        except Exception:
            return False

    def ensure_nacos(self) -> str:
        """
        Step A + B combined (call once).

        1. Resolve network declaration.
        2. Decide nacos placement:
           - Running → remove from services (reuse), nacos_action="existing"
           - Not running → add to services,         nacos_action="added"

        Updates self._nacos_in_compose accordingly (consumed by add_service).
        Returns: "existing" | "added"
        """
        self._resolve_network()

        if self._nacos_running():
            logger.info("Nacos container running → reusing, removing from compose services")
            self._data["services"].pop(NACOS_CONTAINER_NAME, None)
            self._nacos_in_compose = False
            return "existing"
        else:
            logger.info("Nacos not running → adding to compose services")
            self._data["services"][NACOS_CONTAINER_NAME] = NACOS_SERVICE
            self._nacos_in_compose = True
            return "added"

    # ──────────────────────────────────────────────────────────────────────────
    # Step C: Business services
    # ──────────────────────────────────────────────────────────────────────────

    def add_service(self, service_name: str, module_name: str, port: int,
                    registry_mirror: str = None) -> str:
        """
        Add or update a business microservice.
        depends_on nacos only when nacos is managed by this compose file.
        Returns: "added" | "updated" | "unchanged"
        """
        image = (
            f"{registry_mirror}/{module_name}:latest"
            if registry_mirror
            else f"{module_name}:latest"
        )

        desired: dict = {
            "image": image,
            "build": {"context": f"./{module_name}", "dockerfile": "Dockerfile"},
            "container_name": module_name,
            "ports": [f"{port}:{port}"],
            "environment": {
                "NACOS_HOST": NACOS_CONTAINER_NAME,
                "NACOS_PORT": "8848",
                "SERVER_PORT": str(port),
                "SPRING_PROFILES_ACTIVE": "docker",
            },
            "networks": [NETWORK_NAME],
            "restart": "on-failure",
        }
        if self._nacos_in_compose:
            desired["depends_on"] = {
                NACOS_CONTAINER_NAME: {"condition": "service_healthy"}
            }

        existing = self._data["services"].get(service_name)
        if existing == desired:
            return "unchanged"

        self._data["services"][service_name] = desired
        return "updated" if existing else "added"

    # ──────────────────────────────────────────────────────────────────────────
    # Dockerfile
    # ──────────────────────────────────────────────────────────────────────────

    def generate_dockerfile(self, module_path: Path, port: int, overwrite: bool = False) -> bool:
        dockerfile = module_path / "Dockerfile"
        if dockerfile.exists() and not overwrite:
            return False
        dockerfile.write_text(
            f"FROM eclipse-temurin:17-jre-alpine\n"
            f"WORKDIR /app\n"
            f"COPY target/*.jar app.jar\n"
            f"EXPOSE {port}\n"
            f'ENTRYPOINT ["java", "-jar", "app.jar"]\n'
        )
        logger.info(f"Dockerfile written: {dockerfile}")
        return True

    # ──────────────────────────────────────────────────────────────────────────
    # Persist
    # ──────────────────────────────────────────────────────────────────────────

    def save(self):
        self.compose_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.compose_path, "w") as f:
            yaml.dump(
                self._data, f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        logger.info(f"docker-compose.yml saved: {self.compose_path}")