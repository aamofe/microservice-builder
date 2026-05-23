"""
tools/validator.py

v2 变更：
- compile() 全局模式改为从父 pom 整体构建（去掉 -pl/-am），
  让 Maven reactor 按依赖顺序编译所有模块。
  原来 -pl order-service -am 在 user-service 未 install 时会报
  "Could not find artifact"，因为 -am 只 make 依赖但不 install。
- 单模块模式保留但改为 -pl module --also-make，配合父 pom reactor
  可以正确解析模块间依赖（需要先整体编译过一次）。
- 新增 install() 方法供 update_docker_compose skill 调用（替代 package）：
  mvn install -DskipTests 会把所有模块 jar 装入本地仓库，
  使后续单模块编译/打包能正确解析兄弟模块依赖。
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class MavenValidator:
    def __init__(self, project_root: Path):
        self.project_root = project_root

    def compile(self, module: str = None, skip_tests: bool = True) -> dict:
        """
        从父 pom 目录整体 compile（reactor 模式），保证模块间依赖顺序正确。
        module 参数保留但不再用 -pl，改为整体构建后只报告该模块的错误。
        """
        cmd = ["mvn", "compile"]
        if skip_tests:
            cmd.append("-DskipTests")
        cmd += ["-q", "--no-transfer-progress"]

        logger.info(f"Running: {' '.join(cmd)} in {self.project_root}")
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=300,
            )
            success = result.returncode == 0
            output = result.stdout + result.stderr
            errors = self._parse_errors(output)
            if not success:
                logger.error(f"mvn compile failed:\n{output}")
            return {"success": success, "output": output, "errors": errors}
        except FileNotFoundError:
            logger.warning("mvn not found on PATH; skipping compile validation")
            return {"success": None, "output": "mvn not found", "errors": []}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "timeout", "errors": ["Build timed out"]}

    def install(self, skip_tests: bool = True) -> dict:
        """
        从父 pom 整体 mvn install，将所有模块 jar 装入本地仓库。
        consumer 模块编译时需要 provider jar 已在本地仓库中。
        update_docker_compose skill 应调用此方法而非 mvn package。
        """
        cmd = ["mvn", "install"]
        if skip_tests:
            cmd.append("-DskipTests")
        cmd += ["-q", "--no-transfer-progress"]

        logger.info(f"Running: {' '.join(cmd)} in {self.project_root}")
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=300,
            )
            success = result.returncode == 0
            output = result.stdout + result.stderr
            errors = self._parse_errors(output)
            if not success:
                logger.error(f"mvn install failed:\n{output}")
            return {"success": success, "output": output, "errors": errors}
        except FileNotFoundError:
            return {"success": None, "output": "mvn not found", "errors": []}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "timeout", "errors": ["Build timed out"]}

    def _parse_errors(self, output: str) -> list[str]:
        errors = []
        for line in output.splitlines():
            if re.search(r"\[ERROR\]", line):
                errors.append(line.strip())
        return errors

    def check_pom_exists(self) -> bool:
        return (self.project_root / "pom.xml").exists()


class DockerValidator:
    def __init__(self, project_root: Path):
        self.project_root = project_root

    def config_check(self) -> dict:
        cmd = ["docker-compose", "config"]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=15,
            )
            return {
                "success": result.returncode == 0,
                "output": result.stdout + result.stderr,
            }
        except FileNotFoundError:
            return {"success": None, "output": "docker-compose not found"}