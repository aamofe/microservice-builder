"""
tools/validator.py
Runs mvn compile (and optionally docker-compose up --dry-run) to validate generated code.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class MavenValidator:
    def __init__(self, project_root: Path):
        self.project_root = project_root

    def compile(self, module: str = None, skip_tests: bool = True) -> dict:
        """
        Run mvn compile (or mvn -pl <module> compile) and return
        {"success": bool, "output": str, "errors": list[str]}
        """
        cmd = ["mvn"]
        if module:
            cmd += ["-pl", module, "-am"]
        cmd.append("compile")
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
                timeout=120,
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
        """Run docker-compose config to validate the compose file."""
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