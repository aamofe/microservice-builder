"""
tools/nacos_client.py
Wraps Nacos OpenAPI to push/pull configuration and check service registration.
"""

from __future__ import annotations

import logging
import os
import requests
from typing import Optional

logger = logging.getLogger(__name__)


class NacosClient:
    def __init__(
        self,
        host: str = None,
        port: int = None,
        username: str = None,
        password: str = None,
        namespace: str = "public",
    ):
        self.host = host or os.getenv("NACOS_HOST", "localhost")
        self.port = int(port or os.getenv("NACOS_PORT", 8848))
        self.username = username or os.getenv("NACOS_USERNAME", "nacos")
        self.password = password or os.getenv("NACOS_PASSWORD", "nacos")
        self.namespace = namespace or os.getenv("NACOS_NAMESPACE", "public")
        self.base_url = f"http://{self.host}:{self.port}/nacos/v1"
        self._token: Optional[str] = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _login(self):
        resp = requests.post(
            f"{self.base_url}/auth/login",
            params={"username": self.username, "password": self.password},
            timeout=5,
        )
        resp.raise_for_status()
        self._token = resp.json().get("accessToken", "")

    def _headers(self) -> dict:
        if not self._token:
            try:
                self._login()
            except Exception:
                logger.warning("Nacos login failed; proceeding without token")
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    # ------------------------------------------------------------------
    # Config management
    # ------------------------------------------------------------------

    def publish_config(self, data_id: str, group: str, content: str, config_type: str = "yaml") -> bool:
        """Push a configuration item to Nacos."""
        try:
            resp = requests.post(
                f"{self.base_url}/cs/configs",
                headers=self._headers(),
                params={
                    "dataId": data_id,
                    "group": group,
                    "tenant": self.namespace,
                    "type": config_type,
                    "content": content,
                },
                timeout=5,
            )
            resp.raise_for_status()
            logger.info(f"Published config {data_id}@{group}")
            return True
        except Exception as e:
            logger.error(f"Nacos publish failed: {e}")
            return False

    def get_config(self, data_id: str, group: str) -> Optional[str]:
        try:
            resp = requests.get(
                f"{self.base_url}/cs/configs",
                headers=self._headers(),
                params={"dataId": data_id, "group": group, "tenant": self.namespace},
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.text
            return None
        except Exception as e:
            logger.error(f"Nacos get config failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Service discovery
    # ------------------------------------------------------------------

    def list_services(self) -> list[str]:
        try:
            resp = requests.get(
                f"{self.base_url}/ns/service/list",
                headers=self._headers(),
                params={"pageNo": 1, "pageSize": 100, "namespaceId": self.namespace},
                timeout=5,
            )
            resp.raise_for_status()
            return resp.json().get("doms", [])
        except Exception as e:
            logger.error(f"Nacos list services failed: {e}")
            return []

    def is_service_registered(self, service_name: str) -> bool:
        return service_name in self.list_services()

    def health_check(self) -> bool:
        try:
            resp = requests.get(f"http://{self.host}:{self.port}/nacos/", timeout=3)
            return resp.status_code < 400
        except Exception:
            return False