"""
tools/docker_compose_builder.py

v4 变更：
- generate_dockerfile: COPY 改为 *-exec.jar
  原因：pom.xml.j2 用 classifier=exec，fat jar 命名为 *-exec.jar，
  普通 *.jar 是原始 jar（供模块间依赖用），不含完整依赖无法直接运行。
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
        self._nacos_in_compose: bool = NACOS_CONTAINER_NAME in self._data.get("services", {})

    def _load_or_init(self) -> dict:
        if self.compose_path.exists():
            try:
                with open(self.compose_path) as f:
                    data = yaml.safe_load(f) or {}
                data.setdefault("services", {})
                data.pop("version", None)
                return data
            except Exception as e:
                logger.warning(f"Failed to load compose file: {e}, starting fresh")
        return {"services": {}}

    # ── Network ───────────────────────────────────────────────────────────────

    @staticmethod
    def _network_exists() -> bool:
        try:
            result = subprocess.run(
                ["docker", "network", "inspect", NETWORK_NAME],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _resolve_network(self) -> str:
        if self._network_exists():
            logger.info(f"Network '{NETWORK_NAME}' exists → external: true")
            self._data["networks"] = {NETWORK_NAME: {"external": True}}
            return "external"
        else:
            logger.info(f"Network '{NETWORK_NAME}' not found → driver: bridge")
            self._data["networks"] = {NETWORK_NAME: {"driver": "bridge"}}
            return "bridge"

    # ── Nacos ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _nacos_running() -> bool:
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", NACOS_CONTAINER_NAME],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip() == "true"
        except Exception:
            return False

    def ensure_nacos(self) -> str:
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

    # ── Business services ─────────────────────────────────────────────────────

    def add_service(self, service_name: str, module_name: str, port: int,
                    registry_mirror: str = None) -> str:
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

    # ── Dockerfile ────────────────────────────────────────────────────────────

    def generate_dockerfile(self, module_path: Path, port: int, overwrite: bool = False) -> bool:
        dockerfile = module_path / "Dockerfile"
        if dockerfile.exists() and not overwrite:
            return False
        dockerfile.write_text(
            f"FROM eclipse-temurin:17-jre-alpine\n"
            f"WORKDIR /app\n"
            # ★ 使用 *-exec.jar：pom.xml classifier=exec，fat jar 命名为 *-exec.jar
            # 普通 *.jar 是原始 jar（供模块间 Maven 依赖使用），不含完整依赖
            f"COPY target/*-exec.jar app.jar\n"
            f"EXPOSE {port}\n"
            f'ENTRYPOINT ["java", "-jar", "app.jar"]\n'
        )
        logger.info(f"Dockerfile written: {dockerfile}")
        return True

    # ── Save ──────────────────────────────────────────────────────────────────

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