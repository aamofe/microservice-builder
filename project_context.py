"""
project_context.py
Per-project state manager: SQLite persistence + in-memory cache.

修复 (v3):
- ContextRegistry.get_or_create() 新增 allowed_project 白名单参数：
  若传入的 project_name 与 allowed_project 不符，抛 ValueError 而非静默创建孤儿项目。
  （crew.py 的工具层捕获此 ValueError 并返回 error dict，不会触发 re-planning）
- ProjectContext.__init__ 打印明确的 project_root 日志，便于排查路径问题
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局 Nacos 状态文件 —— 放在 PROJECTS_ROOT 下，所有项目共享
# ---------------------------------------------------------------------------

GLOBAL_STATE_DIR = Path(os.getenv("PROJECTS_ROOT", "./output"))
GLOBAL_STATE_FILE = GLOBAL_STATE_DIR / ".global_state.json"


def _load_global_state() -> dict:
    if GLOBAL_STATE_FILE.exists():
        try:
            return json.loads(GLOBAL_STATE_FILE.read_text())
        except Exception:
            pass
    return {"nacos": {"running": False, "compose_file": None, "network": "microservice-net"}}


def _save_global_state(state: dict):
    GLOBAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    GLOBAL_STATE_FILE.write_text(json.dumps(state, indent=2))

class GlobalNacosState:
    @classmethod
    def mark_running(cls, compose_file: str, network: str = "microservice-net"):
        state = _load_global_state()
        state["nacos"] = {
            "running": True,
            "compose_file": compose_file,
            "network": network,
            "marked_at": datetime.now().isoformat(),
        }
        _save_global_state(state)
        logger.info(f"GlobalNacosState: marked running, compose={compose_file}")

    @classmethod
    def get(cls) -> dict:
        """
        读取状态。若磁盘记录 running=false，做一次 docker inspect 兜底：
        - 容器实际运行 → 同步写入磁盘，返回 running=true
        - 容器确实未运行 → 返回 running=false
        """
        state = _load_global_state()
        nacos = state.get("nacos", {"running": False, "compose_file": None})

        if not nacos.get("running"):
            # 兜底探测：防止因 Ctrl+C / 进程崩溃导致状态文件从未写入
            try:
                import subprocess
                result = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Running}}", "nacos"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.stdout.strip() == "true":
                    cls.mark_running(
                        compose_file="external (detected via docker inspect)",
                        network="microservice-net",
                    )
                    logger.info("GlobalNacosState.get: live docker inspect found running nacos, state synced")
                    return _load_global_state().get("nacos", nacos)
            except Exception:
                pass

        return nacos

    @classmethod
    def is_running(cls) -> bool:
        return cls.get().get("running", False)

    @classmethod
    def reset(cls):
        state = _load_global_state()
        state["nacos"] = {"running": False, "compose_file": None, "network": "microservice-net"}
        _save_global_state(state)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MethodRecord:
    class_fqn: str
    method_name: str
    return_type: str
    params: list[dict]
    source: str = "generated"


@dataclass
class ChangeHistory:
    timestamp: str
    module_name: str
    file_path: str
    change_type: str
    description: str
    diff_summary: str = ""


@dataclass
class ModuleInfo:
    name: str
    artifact_id: str
    group_id: str
    version: str
    base_package: str
    port: int
    has_dubbo_provider: bool = False
    has_dubbo_consumer: bool = False
    has_rest_controller: bool = False
    has_threadpool: bool = False
    dependencies: list[str] = field(default_factory=list)
    interface_fqns: list[str] = field(default_factory=list)
    entity_fqns: list[str] = field(default_factory=list)
    controller_classes: list[str] = field(default_factory=list)


@dataclass
class InterfaceInfo:
    module_name: str
    interface_fqn: str
    impl_fqn: str
    methods: list[dict]


@dataclass
class EntityInfo:
    module_name: str
    fqn: str
    fields: list[dict]


# ---------------------------------------------------------------------------
# ProjectContext
# ---------------------------------------------------------------------------

class ProjectContext:
    DB_FILE = ".project_state.db"

    def __init__(self, project_name: str, projects_root: str = "./output"):
        self.project_name = project_name
        self.project_root = Path(projects_root) / project_name
        self.project_root.mkdir(parents=True, exist_ok=True)

        # 明确打印 project_root，方便排查路径问题
        logger.info(f"ProjectContext: project_name='{project_name}', root='{self.project_root.resolve()}'")

        self.db_path = self.project_root / self.DB_FILE
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_db()

        self.modules: dict[str, ModuleInfo] = {}
        self.interfaces: dict[str, InterfaceInfo] = {}
        self.entities: dict[str, EntityInfo] = {}

        self._load_from_db()

    # ------------------------------------------------------------------
    # DB bootstrap
    # ------------------------------------------------------------------

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS modules (
                name TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS interfaces (
                fqn TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entities (
                fqn TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS method_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_fqn TEXT NOT NULL,
                method_name TEXT NOT NULL,
                data TEXT NOT NULL,
                UNIQUE(class_fqn, method_name)
            );
            CREATE TABLE IF NOT EXISTS change_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                module_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                change_type TEXT NOT NULL,
                description TEXT NOT NULL,
                diff_summary TEXT DEFAULT ''
            );
        """)
        self._conn.commit()

    def _load_from_db(self):
        cur = self._conn.cursor()
        for row in cur.execute("SELECT name, data FROM modules"):
            m = ModuleInfo(**json.loads(row[1]))
            self.modules[m.name] = m
        for row in cur.execute("SELECT fqn, data FROM interfaces"):
            i = InterfaceInfo(**json.loads(row[1]))
            self.interfaces[i.interface_fqn] = i
        for row in cur.execute("SELECT fqn, data FROM entities"):
            e = EntityInfo(**json.loads(row[1]))
            self.entities[e.fqn] = e

    # ------------------------------------------------------------------
    # Module / Interface / Entity persistence
    # ------------------------------------------------------------------

    def save_module(self, m: ModuleInfo):
        self.modules[m.name] = m
        self._conn.execute(
            "INSERT OR REPLACE INTO modules (name, data) VALUES (?,?)",
            (m.name, json.dumps(asdict(m)))
        )
        self._conn.commit()

    def save_interface(self, i: InterfaceInfo):
        self.interfaces[i.interface_fqn] = i
        self._conn.execute(
            "INSERT OR REPLACE INTO interfaces (fqn, data) VALUES (?,?)",
            (i.interface_fqn, json.dumps(asdict(i)))
        )
        self._conn.commit()
        m = self.modules.get(i.module_name)
        if m and i.interface_fqn not in m.interface_fqns:
            m.interface_fqns.append(i.interface_fqn)
            self.save_module(m)
        for method in i.methods:
            self.record_method(MethodRecord(
                class_fqn=i.interface_fqn,
                method_name=method["name"],
                return_type=method.get("return_type", "void"),
                params=method.get("params", []),
                source="generated",
            ))
            self.record_method(MethodRecord(
                class_fqn=i.impl_fqn,
                method_name=method["name"],
                return_type=method.get("return_type", "void"),
                params=method.get("params", []),
                source="generated",
            ))

    def save_entity(self, e: EntityInfo):
        self.entities[e.fqn] = e
        self._conn.execute(
            "INSERT OR REPLACE INTO entities (fqn, data) VALUES (?,?)",
            (e.fqn, json.dumps(asdict(e)))
        )
        self._conn.commit()
        m = self.modules.get(e.module_name)
        if m and e.fqn not in m.entity_fqns:
            m.entity_fqns.append(e.fqn)
            self.save_module(m)

    def set_kv(self, key: str, value: str):
        self._conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
            (key, value)
        )
        self._conn.commit()

    def get_kv(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    # ------------------------------------------------------------------
    # Method records
    # ------------------------------------------------------------------

    def record_method(self, rec: MethodRecord):
        self._conn.execute(
            "INSERT OR REPLACE INTO method_records (class_fqn, method_name, data) VALUES (?,?,?)",
            (rec.class_fqn, rec.method_name, json.dumps(asdict(rec)))
        )
        self._conn.commit()

    def has_method(self, class_fqn: str, method_name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM method_records WHERE class_fqn=? AND method_name=?",
            (class_fqn, method_name)
        ).fetchone()
        return row is not None

    def get_methods(self, class_fqn: str) -> list[MethodRecord]:
        rows = self._conn.execute(
            "SELECT data FROM method_records WHERE class_fqn=?",
            (class_fqn,)
        ).fetchall()
        return [MethodRecord(**json.loads(r[0])) for r in rows]

    def get_all_method_names(self, class_fqn: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT method_name FROM method_records WHERE class_fqn=?",
            (class_fqn,)
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Change history
    # ------------------------------------------------------------------

    def log_change(self, module_name: str, file_path: str,
                   change_type: str, description: str, diff_summary: str = ""):
        self._conn.execute(
            """INSERT INTO change_history
               (timestamp, module_name, file_path, change_type, description, diff_summary)
               VALUES (?,?,?,?,?,?)""",
            (datetime.now().isoformat(), module_name, file_path,
             change_type, description, diff_summary[:500])
        )
        self._conn.commit()

    def get_recent_changes(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            """SELECT timestamp, module_name, file_path, change_type, description
               FROM change_history ORDER BY id DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [
            {"timestamp": r[0], "module": r[1], "file": r[2], "type": r[3], "desc": r[4]}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Idempotency helpers
    # ------------------------------------------------------------------

    def module_exists(self, module_name: str) -> bool:
        return module_name in self.modules

    def interface_exists(self, interface_fqn: str) -> bool:
        return interface_fqn in self.interfaces

    def entity_exists(self, fqn: str) -> bool:
        return fqn in self.entities

    def get_interface_by_name(self, interface_name: str, module_name: str = None) -> Optional[InterfaceInfo]:
        for fqn, info in self.interfaces.items():
            if fqn.endswith("." + interface_name):
                if module_name is None or info.module_name == module_name:
                    return info
        return None

    # ------------------------------------------------------------------
    # File-system scanner
    # ------------------------------------------------------------------

    def scan_existing_project(self):
        logger.info(f"Scanning project at {self.project_root}")
        if not self.project_root.exists():
            return
        for item in self.project_root.iterdir():
            if item.is_dir() and (item / "pom.xml").exists() and not item.name.startswith("."):
                self._scan_module(item)

    def _scan_module(self, module_path: Path):
        name = module_path.name
        if name in self.modules:
            self._scan_java_methods(module_path, self.modules[name])
            return

        pom = module_path / "pom.xml"
        if not pom.exists():
            return

        import xml.etree.ElementTree as ET
        try:
            tree = ET.parse(str(pom))
            root_el = tree.getroot()
            ns = {"m": "http://maven.apache.org/POM/4.0.0"}

            def text(tag):
                el = root_el.find(f"m:{tag}", ns)
                return el.text.strip() if el is not None and el.text else ""

            group_id = text("groupId") or self.get_kv("group_id") or "com.example"
            artifact_id = text("artifactId") or name
            version = text("version") or "1.0.0-SNAPSHOT"
        except Exception:
            group_id = self.get_kv("group_id") or "com.example"
            artifact_id = name
            version = "1.0.0-SNAPSHOT"

        import re
        base_package = group_id.replace("-", "") + "." + re.sub(r"[^a-zA-Z0-9]", "", name)
        port = self._read_port_from_yml(module_path) or self.next_available_port()

        m = ModuleInfo(
            name=name,
            artifact_id=artifact_id,
            group_id=group_id,
            version=version,
            base_package=base_package,
            port=port,
        )
        self.save_module(m)
        self._scan_java_methods(module_path, m)
        logger.info(f"  Discovered module: {name} (port={port})")

    def _read_port_from_yml(self, module_path: Path) -> Optional[int]:
        yml = module_path / "src" / "main" / "resources" / "application.yml"
        if not yml.exists():
            return None
        try:
            import yaml
            data = yaml.safe_load(yml.read_text()) or {}
            return int(data.get("server", {}).get("port", 0)) or None
        except Exception:
            return None

    def _scan_java_methods(self, module_path: Path, module_info: ModuleInfo):
        import re
        src_root = module_path / "src" / "main" / "java"
        if not src_root.exists():
            return

        method_pattern = re.compile(
            r"(?:public|protected|private)\s+(?:static\s+)?(\w[\w<>,\s\[\]]*?)\s+(\w+)\s*\("
        )
        pkg_pattern = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
        class_pattern = re.compile(r"(?:class|interface)\s+(\w+)")

        for java_file in src_root.rglob("*.java"):
            try:
                source = java_file.read_text(encoding="utf-8")
                pkg_match = pkg_pattern.search(source)
                class_match = class_pattern.search(source)
                if not pkg_match or not class_match:
                    continue
                fqn = pkg_match.group(1) + "." + class_match.group(1)
                for m in method_pattern.finditer(source):
                    return_type, method_name = m.group(1).strip(), m.group(2).strip()
                    if method_name in ("if", "for", "while", "return", "new"):
                        continue
                    if not self.has_method(fqn, method_name):
                        self.record_method(MethodRecord(
                            class_fqn=fqn,
                            method_name=method_name,
                            return_type=return_type,
                            params=[],
                            source="scanned",
                        ))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def module_path(self, module_name: str) -> Path:
        return self.project_root / module_name

    def next_available_port(self, start: int = 8051) -> int:
        used = {m.port for m in self.modules.values()}
        p = start
        while p in used:
            p += 1
        return p

    def to_summary(self) -> str:
        lines = [
            f"Project: {self.project_name}",
            f"Root: {self.project_root}",
            f"Modules ({len(self.modules)}):",
        ]
        for m in self.modules.values():
            lines.append(f"  - {m.name}  port={m.port}  pkg={m.base_package}")
            if m.interface_fqns:
                lines.append(f"    interfaces: {', '.join(m.interface_fqns)}")
            if m.entity_fqns:
                lines.append(f"    entities: {', '.join(m.entity_fqns)}")
        lines.append(f"Interfaces ({len(self.interfaces)}):")
        for fqn, info in self.interfaces.items():
            method_names = [me["name"] for me in info.methods]
            lines.append(f"  - {fqn}  methods={method_names}")
        lines.append(f"Entities ({len(self.entities)}):")
        for fqn in self.entities:
            lines.append(f"  - {fqn}")
        nacos = GlobalNacosState.get()
        lines.append(f"Nacos: running={nacos.get('running')}  compose={nacos.get('compose_file')}")
        return "\n".join(lines)

    def to_rich_summary(self) -> dict:
        modules_detail = {}
        for name, m in self.modules.items():
            interfaces_detail = {}
            for fqn in m.interface_fqns:
                info = self.interfaces.get(fqn)
                if info:
                    interfaces_detail[fqn] = {
                        "impl_fqn": info.impl_fqn,
                        "existing_methods": [me["name"] for me in info.methods],
                        "methods_full": info.methods,
                    }
            modules_detail[name] = {
                "port": m.port,
                "base_package": m.base_package,
                "has_dubbo_provider": m.has_dubbo_provider,
                "has_rest_controller": m.has_rest_controller,
                "has_threadpool": m.has_threadpool,
                "dependencies": m.dependencies,
                "interfaces": interfaces_detail,
                "entities": m.entity_fqns,
                "controllers": m.controller_classes,
                "path_exists": self.module_path(name).exists(),
            }
        return {
            "project_name": self.project_name,
            "project_root": str(self.project_root),
            "modules": modules_detail,
            "nacos": GlobalNacosState.get(),
            "recent_changes": self.get_recent_changes(10),
        }

    def close(self):
        self._conn.close()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ContextRegistry:
    _instances: dict[str, ProjectContext] = {}

    @classmethod
    def get_or_create(
        cls,
        project_name: str,
        projects_root: str = "./output",
        allowed_project: str | None = None,
    ) -> ProjectContext:
        """
        获取或创建 ProjectContext。

        allowed_project: 若传入，要求 project_name == allowed_project。
        不匹配时抛 ValueError（调用方应捕获并返回 error dict，不应传播为异常触发 re-planning）。
        """
        if allowed_project and project_name != allowed_project:
            raise ValueError(
                f"Blocked: project_name='{project_name}' != allowed='{allowed_project}'. "
                f"Use project_name='{allowed_project}'."
            )

        if project_name not in cls._instances:
            ctx = ProjectContext(project_name, projects_root)
            ctx.scan_existing_project()
            cls._instances[project_name] = ctx
        return cls._instances[project_name]

    @classmethod
    def close_all(cls):
        for ctx in cls._instances.values():
            try:
                ctx.close()
            except Exception:
                pass
        cls._instances.clear()