"""
skills/generate_dubbo_interface/skill.py
Generates a Dubbo service interface + implementation class for a given module.

Fix (v2):
- _normalize_methods(): LLM 有时将 methods 传为字符串列表 ["login","register"]
  或混合格式，统一转换为 {name, return_type, params} 字典格式，防止 AttributeError
"""

from __future__ import annotations

import re
from pathlib import Path

from project_context import InterfaceInfo, ProjectContext
from tools.template_engine import get_engine
import logging

logger = logging.getLogger(__name__)


def _normalize_methods(methods: list) -> list[dict]:
    """
    将 LLM 可能传来的各种格式统一为标准字典格式:
      {name: str, return_type: str, params: list[{name: str, type: str}]}

    支持的输入格式:
      - str:  "login"  →  {name: "login", return_type: "void", params: []}
      - dict: 已是标准格式，补全缺失字段
    """
    normalized = []
    for m in methods:
        if isinstance(m, str):
            # LLM 只传了方法名字符串
            normalized.append({
                "name": m.strip(),
                "return_type": "void",
                "params": [],
            })
        elif isinstance(m, dict):
            # 补全可能缺失的字段
            entry = {
                "name": m.get("name", "unknown"),
                "return_type": m.get("return_type", "void"),
                "params": [],
            }
            # params 本身也可能是字符串列表，做同样防御
            raw_params = m.get("params", [])
            for p in raw_params:
                if isinstance(p, str):
                    entry["params"].append({"name": p, "type": "Object"})
                elif isinstance(p, dict):
                    entry["params"].append({
                        "name": p.get("name", "arg"),
                        "type": p.get("type", "Object"),
                    })
            normalized.append(entry)
        else:
            logger.warning(f"Unexpected method format, skipping: {m!r}")
    return normalized


def run(params: dict, context: ProjectContext) -> dict:
    module_name: str = params["module_name"]
    interface_name: str = params["interface_name"]   # e.g. "UserService"
    methods: list = params.get("methods", [])        # [{name, return_type, params:[{name,type}]}]
    version: str = params.get("version", "1.0.0")
    group: str = params.get("group", "default")
    has_threadpool: bool = params.get("has_threadpool", False)

    # ── 关键修复：规范化 methods，防止 LLM 传字符串导致 AttributeError ──
    methods = _normalize_methods(methods)
    if not methods:
        logger.warning(f"generate_dubbo_interface: no valid methods after normalization")

    module_info = context.modules.get(module_name)
    if not module_info:
        return {"status": "error", "message": f"Module '{module_name}' not found in context. Create it first."}

    base_package = module_info.base_package
    api_package = base_package + ".api"
    impl_package = base_package + ".service"
    impl_class = interface_name + "Impl"

    engine = get_engine()
    module_path = context.module_path(module_name)

    src_root = module_path / "src" / "main" / "java"

    def pkg_to_path(pkg: str) -> Path:
        return src_root / Path(*pkg.split("."))

    api_dir = pkg_to_path(api_package)
    api_dir.mkdir(parents=True, exist_ok=True)
    impl_dir = pkg_to_path(impl_package)
    impl_dir.mkdir(parents=True, exist_ok=True)

    imports = _collect_imports(methods)

    # --- Interface ---
    iface_ctx = {
        "package": api_package,
        "interface_name": interface_name,
        "methods": methods,
        "imports": imports,
    }
    engine.write_rendered(
        "dubbo-interface/DubboService.java.j2",
        iface_ctx,
        api_dir / f"{interface_name}.java",
    )

    # --- Implementation ---
    impl_ctx = {
        "impl_package": impl_package,
        "class_name": impl_class,
        "interface_name": interface_name,
        "methods": methods,
        "imports": imports + [f"{api_package}.{interface_name}"],
        "version": version,
        "group": group,
        "has_async": has_threadpool,
        "has_threadpool": has_threadpool,
    }
    engine.write_rendered(
        "dubbo-interface/DubboServiceImpl.java.j2",
        impl_ctx,
        impl_dir / f"{impl_class}.java",
    )

    interface_fqn = f"{api_package}.{interface_name}"
    impl_fqn = f"{impl_package}.{impl_class}"
    info = InterfaceInfo(
        module_name=module_name,
        interface_fqn=interface_fqn,
        impl_fqn=impl_fqn,
        methods=methods,
    )
    context.save_interface(info)

    module_info.has_dubbo_provider = True
    context.save_module(module_info)

    return {
        "status": "success",
        "interface_fqn": interface_fqn,
        "impl_fqn": impl_fqn,
        "files": [
            str(api_dir / f"{interface_name}.java"),
            str(impl_dir / f"{impl_class}.java"),
        ],
    }


def _collect_imports(methods: list) -> list[str]:
    """Scan method signatures for types that may need imports.
    methods 此时已经过 _normalize_methods()，保证每项都是 dict。
    """
    type_import_map = {
        "List": "java.util.List",
        "Map": "java.util.Map",
        "Set": "java.util.Set",
        "Optional": "java.util.Optional",
        "LocalDate": "java.time.LocalDate",
        "LocalDateTime": "java.time.LocalDateTime",
        "BigDecimal": "java.math.BigDecimal",
        "CompletableFuture": "java.util.concurrent.CompletableFuture",
    }
    needed = set()
    for m in methods:
        for token in re.findall(r"[A-Za-z]+", m.get("return_type", "")):
            if token in type_import_map:
                needed.add(type_import_map[token])
        for p in m.get("params", []):
            for token in re.findall(r"[A-Za-z]+", p.get("type", "")):
                if token in type_import_map:
                    needed.add(type_import_map[token])
    return sorted(needed)