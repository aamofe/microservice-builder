"""
tools/docker_compose_builder.py
生成并增量更新 docker-compose.yml。

Fix (v3):
- 还原被覆盖丢失的 DockerComposeBuilder 类
- is_nacos_container_running() 结果直接写入 GlobalNacosState，
  确保 ensure_nacos() 能拿到真实状态
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
import yaml

from project_context import GlobalNacosState

logger = logging.getLogger(__name__)


class DockerComposeBuilder:

    NACOS_SERVICE = {
        "image": "nacos/nacos-server:v2.2.3",
        "container_name": "nacos",
        "environment": {
            "MODE": "standalone",
            "NACOS_AUTH_ENABLE": "false",
            "JVM_XMS": "256m",
            "JVM_XMX": "512m",
        },
        "ports": ["8848:8848", "9848:9848"],
        "healthcheck": {
            "test": ["CMD", "curl", "-f", "http://localhost:8848/nacos/"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 10,
            "start_period": "30s",
        },
        "networks": ["microservice-net"],
        "restart": "unless-stopped",
    }

    NETWORK_BRIDGE = {"microservice-net": {"driver": "bridge"}}
    NETWORK_EXTERNAL = {"microservice-net": {"external": True}}

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.compose_path = project_root / "docker-compose.yml"
        self._data: dict = self._load_existing()

    def _load_existing(self) -> dict:
        if self.compose_path.exists():
            try:
                with open(self.compose_path) as f:
                    data = yaml.safe_load(f) or {}
                    data.setdefault("version", "3.8")
                    data.setdefault("services", {})
                    data.setdefault("networks", self.NETWORK_BRIDGE)
                    return data
            except Exception as e:
                logger.warning(f"Failed to load existing compose file: {e}, starting fresh")
        return {
            "version": "3.8",
            "services": {},
            "networks": self.NETWORK_BRIDGE,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Nacos 单例处理
    # ──────────────────────────────────────────────────────────────────────────

    def ensure_nacos(self) -> str:
        """
        保证 Nacos 在某个 compose 文件中存在且只存在一次。
        返回: "existing" | "added"
        """
        # 先做真实探测，同步到 GlobalNacosState（防止状态文件缺失）
        nacos_actually_running = self.is_nacos_container_running()
        if nacos_actually_running and not GlobalNacosState.is_running():
            GlobalNacosState.mark_running(
                compose_file="external (detected via docker inspect)",
                network="microservice-net",
            )
            logger.info("ensure_nacos: live nacos container detected, GlobalNacosState synced")

        nacos_state = GlobalNacosState.get()

        if nacos_state.get("running"):
            logger.info("Nacos already running (GlobalNacosState), using external network")
            self._data["networks"] = self.NETWORK_EXTERNAL
            self._data["services"].pop("nacos", None)
            return "existing"
        else:
            logger.info("Adding nacos service to docker-compose (first time)")
            self._data["networks"] = self.NETWORK_BRIDGE
            self._data["services"]["nacos"] = self.NACOS_SERVICE
            return "added"

    # ──────────────────────────────────────────────────────────────────────────
    # 服务管理
    # ──────────────────────────────────────────────────────────────────────────

    def add_service(
        self,
        service_name: str,
        module_name: str,
        port: int,
        image_name: str = None,
        env_vars: dict = None,
        depends_on: list[str] = None,
        registry_mirror: str = None,
    ) -> str:
        """
        添加或更新一个微服务。
        返回: "added" | "updated" | "unchanged"
        """
        image = image_name or f"{module_name}:latest"
        if registry_mirror:
            image = f"{registry_mirror}/{image}"

        default_env = {
            "NACOS_HOST": "nacos",
            "NACOS_PORT": "8848",
            "SERVER_PORT": str(port),
            "SPRING_PROFILES_ACTIVE": "docker",
        }
        if env_vars:
            default_env.update(env_vars)

        nacos_state = GlobalNacosState.get()
        nacos_is_external = nacos_state.get("running", False)

        existing = self._data["services"].get(service_name)

        if existing:
            existing_env = existing.get("environment", {})
            merged_env = {**default_env, **existing_env}
            changed = (
                existing_env != merged_env
                or existing.get("ports") != [f"{port}:{port}"]
            )
            if not changed:
                logger.info(f"Service '{service_name}' unchanged, skipping")
                return "unchanged"
            existing["environment"] = merged_env
            existing["ports"] = [f"{port}:{port}"]
            if nacos_is_external:
                existing.pop("depends_on", None)
            else:
                existing["depends_on"] = {"nacos": {"condition": "service_healthy"}}
            logger.info(f"Updated existing service: {service_name}")
            return "updated"
        else:
            svc: dict = {
                "image": image,
                "build": {
                    "context": f"./{module_name}",
                    "dockerfile": "Dockerfile",
                },
                "container_name": module_name,
                "ports": [f"{port}:{port}"],
                "environment": default_env,
                "networks": ["microservice-net"],
                "restart": "on-failure",
            }
            if not nacos_is_external:
                svc["depends_on"] = {"nacos": {"condition": "service_healthy"}}

            self._data["services"][service_name] = svc
            logger.info(f"Added new service: {service_name}")
            return "added"

    def remove_service(self, service_name: str):
        self._data.get("services", {}).pop(service_name, None)

    # ──────────────────────────────────────────────────────────────────────────
    # 持久化
    # ──────────────────────────────────────────────────────────────────────────

    def save(self):
        self.compose_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.compose_path, "w") as f:
            yaml.dump(
                self._data,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        logger.info(f"docker-compose.yml saved: {self.compose_path}")

    def to_yaml(self) -> str:
        return yaml.dump(self._data, default_flow_style=False, sort_keys=False)

    # ──────────────────────────────────────────────────────────────────────────
    # Dockerfile（幂等）
    # ──────────────────────────────────────────────────────────────────────────

    def generate_dockerfile(self, module_path: Path, port: int, overwrite: bool = False) -> bool:
        dockerfile = module_path / "Dockerfile"
        if dockerfile.exists() and not overwrite:
            logger.info(f"Dockerfile already exists, skipping: {dockerfile}")
            return False
        content = f"""\
FROM eclipse-temurin:17-jre-alpine
WORKDIR /app
COPY target/*.jar app.jar
EXPOSE {port}
ENTRYPOINT ["java", "-jar", "app.jar"]
"""
        dockerfile.write_text(content)
        logger.info(f"Dockerfile written: {dockerfile}")
        return True

    # ──────────────────────────────────────────────────────────────────────────
    # 运行状态检测
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def is_nacos_container_running() -> bool:
        """
        通过 docker inspect 检查名为 'nacos' 的容器是否正在运行。
        不依赖 GlobalNacosState（用于初始化时的真实状态探测）。
        """
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", "nacos"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() == "true"
        except Exception:
            return False

    @staticmethod
    def is_nacos_network_exists() -> bool:
        """检查 docker 网络 microservice-net 是否已存在。"""
        try:
            result = subprocess.run(
                ["docker", "network", "ls", "--filter", "name=microservice-net", "--format", "{{.Name}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return "microservice-net" in result.stdout
        except Exception:
            return False