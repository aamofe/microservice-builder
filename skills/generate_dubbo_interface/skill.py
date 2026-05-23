"""
skills/generate_dubbo_interface/skill.py

v3 变更：
- _collect_imports() 新增自定义类型解析：
  短类名（无 '.'，非基础类型）按约定推断为 <base_package>.dto.<Type>
  解决 UserDto / LoginDto 等 DTO 在 interface 和 impl 里缺少 import 的编译错误
"""

from __future__ import annotations

import re
from pathlib import Path

from project_context import InterfaceInfo, ProjectContext
from tools.template_engine import get_engine
import logging

logger = logging.getLogger(__name__)

# 永远不需要 import 的类型
_NO_IMPORT = {
    "void", "boolean", "byte", "short", "int", "long", "float", "double", "char",
    "String", "Object", "Boolean", "Byte", "Short", "Integer", "Long",
    "Float", "Double", "Character", "Void",
}

# 标准库类型映射
_STD_IMPORT_MAP = {
    "List":              "java.util.List",
    "Map":               "java.util.Map",
    "Set":               "java.util.Set",
    "Optional":          "java.util.Optional",
    "LocalDate":         "java.time.LocalDate",
    "LocalDateTime":     "java.time.LocalDateTime",
    "BigDecimal":        "java.math.BigDecimal",
    "CompletableFuture": "java.util.concurrent.CompletableFuture",
}


def _normalize_methods(methods: list) -> list[dict]:
    normalized = []
    for m in methods:
        if isinstance(m, str):
            normalized.append({"name": m.strip(), "return_type": "void", "params": []})
        elif isinstance(m, dict):
            entry = {
                "name": m.get("name", "unknown"),
                "return_type": m.get("return_type", "void"),
                "params": [],
            }
            for p in m.get("params", []):
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


def _collect_imports(methods: list, base_package: str = "") -> list[str]:
    """
    收集 interface / impl 所需的所有 import。

    规则：
    - 标准库短名（List、Map 等）→ 查 _STD_IMPORT_MAP
    - 自定义短类名（无 '.'，非基础类型）→ <base_package>.dto.<Type>
    - 已含 '.' 的 FQN → 直接使用
    - 基础类型 / String / Object 等 → 跳过
    """
    needed: dict[str, str] = {}  # simple_name → fqn，用于去重

    def _add(type_str: str):
        # 提取尖括号前的主类名，如 List<UserDto> → List, UserDto
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", type_str)
        for token in tokens:
            if token in _NO_IMPORT or token in needed:
                continue
            if token in _STD_IMPORT_MAP:
                needed[token] = _STD_IMPORT_MAP[token]
            elif "." not in token:
                # 自定义类型，推断为 dto 包
                if base_package:
                    fqn = f"{base_package}.dto.{token}"
                    needed[token] = fqn
                    logger.debug(f"_collect_imports: resolved '{token}' → '{fqn}'")
                else:
                    logger.warning(f"_collect_imports: cannot resolve '{token}' without base_package")
            else:
                # 已经是 FQN
                needed[token] = token

    for m in methods:
        _add(m.get("return_type", ""))
        for p in m.get("params", []):
            _add(p.get("type", ""))

    return sorted(needed.values())


def run(params: dict, context: ProjectContext) -> dict:
    module_name: str = params["module_name"]
    interface_name: str = params["interface_name"]
    methods: list = params.get("methods", [])
    version: str = params.get("version", "1.0.0")
    group: str = params.get("group", "default")
    has_threadpool: bool = params.get("has_threadpool", False)

    methods = _normalize_methods(methods)
    if not methods:
        logger.warning("generate_dubbo_interface: no valid methods after normalization")

    module_info = context.modules.get(module_name)
    if not module_info:
        return {
            "status": "error",
            "message": f"Module '{module_name}' not found in context. Create it first.",
        }

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

    # ★ 传入 base_package，让 _collect_imports 能解析自定义 DTO 类型
    imports = _collect_imports(methods, base_package)

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