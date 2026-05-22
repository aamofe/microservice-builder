"""
skills/generate_rest_controller/skill.py
Generates a Spring MVC REST controller that delegates to a Dubbo service.

Fix (v3):
- _build_imports(): 短类名（无 '.'）的参数类型按约定推断为 <base_package>.dto.<Type>
  解决 UserDto / LoginDto 等 DTO 无法 import 的编译错误
- source="body" 的参数正确生成 @RequestBody（原 v2 已支持，此版本保持不变）
- 其余逻辑与 v2 完全一致
"""

from __future__ import annotations

from pathlib import Path

from project_context import ProjectContext
from tools.template_engine import get_engine
import logging

logger = logging.getLogger(__name__)

_HTTP_METHOD_NORMALIZE = {
    "get": "Get", "post": "Post", "put": "Put",
    "delete": "Delete", "patch": "Patch",
    "GET": "Get", "POST": "Post", "PUT": "Put",
    "DELETE": "Delete", "PATCH": "Patch",
}


def _normalize_endpoints(endpoints: list) -> list[dict]:
    result = []
    for ep in endpoints:
        if not isinstance(ep, dict):
            logger.warning(f"generate_rest_controller: unexpected endpoint format, skipping: {ep!r}")
            continue

        raw_method = ep.get("http_method") or ep.get("method") or "Get"
        http_method = _HTTP_METHOD_NORMALIZE.get(raw_method, raw_method.capitalize())

        method_name = (
            ep.get("method_name")
            or ep.get("handler_name")
            or ep.get("name")
            or "handle"
        )

        raw_params = ep.get("params") or []
        params = []
        for p in raw_params:
            if isinstance(p, str):
                params.append({"name": p, "type": "String", "source": "query"})
            elif isinstance(p, dict):
                params.append({
                    "name": p.get("name", "param"),
                    "type": p.get("type", "String"),
                    "source": p.get("source", "query"),
                })

        result.append({
            "http_method": http_method,
            "path": ep.get("path", ""),
            "method_name": method_name,
            "response_type": ep.get("response_type", "Object"),
            "description": ep.get("description", ""),
            "params": params,
        })
    return result


def run(params: dict, context: ProjectContext) -> dict:
    module_name: str = params.get("module_name", "")
    if not module_name:
        return {"status": "error", "message": "Missing required param: module_name"}

    controller_name: str = (
        params.get("controller_name")
        or params.get("class_name")
        or ""
    )
    if not controller_name:
        return {"status": "error", "message": "Missing required param: controller_name"}

    base_path: str = params.get("base_path", "/api/v1")
    endpoints: list = _normalize_endpoints(params.get("endpoints", []))
    dubbo_ref: dict = params.get("dubbo_ref")
    overwrite: bool = bool(params.get("overwrite", False))

    module_info = context.modules.get(module_name)
    if not module_info:
        return {"status": "error", "message": f"Module '{module_name}' not found."}

    base_package = module_info.base_package
    ctrl_package = base_package + ".controller"
    ctrl_fqn = f"{ctrl_package}.{controller_name}"

    engine = get_engine()
    module_path = context.module_path(module_name)
    src_root = module_path / "src" / "main" / "java"
    ctrl_dir = src_root / Path(*ctrl_package.split("."))
    ctrl_dir.mkdir(parents=True, exist_ok=True)

    out_path = ctrl_dir / f"{controller_name}.java"

    if out_path.exists() and not overwrite:
        logger.info(f"Controller already exists, skipping: {out_path}")
        return {
            "status": "already_exists",
            "file": str(out_path),
            "controller_fqn": ctrl_fqn,
            "message": f"Controller '{controller_name}' already exists. Use overwrite=true to regenerate.",
        }

    # ★ v3: pass base_package so _build_imports can resolve short DTO names
    imports = _build_imports(endpoints, dubbo_ref, base_package)

    ctx = {
        "package": ctrl_package,
        "class_name": controller_name,
        "base_path": base_path,
        "endpoints": endpoints,
        "dubbo_ref": dubbo_ref,
        "dubbo_version": params.get("dubbo_version", "1.0.0"),
        "dubbo_group": params.get("dubbo_group", "default"),
        "imports": imports,
    }

    engine.write_rendered("rest-controller/RestController.java.j2", ctx, out_path, overwrite=overwrite)

    module_info.has_rest_controller = True
    if ctrl_fqn not in (module_info.controller_classes or []):
        module_info.controller_classes = (module_info.controller_classes or []) + [ctrl_fqn]
    context.save_module(module_info)

    context.log_change(
        module_name=module_name,
        file_path=str(out_path.relative_to(module_path)),
        change_type="create",
        description=f"Generated controller '{controller_name}' with {len(endpoints)} endpoints",
    )

    return {
        "status": "success",
        "file": str(out_path),
        "controller_fqn": ctrl_fqn,
    }


def _build_imports(endpoints: list, dubbo_ref: dict, base_package: str = "") -> list[str]:
    """
    Collect all import statements needed by the controller.

    Rules:
    - dubbo_ref.interface: always import as-is (it's a FQN)
    - param types that already contain '.': import as-is (caller passed FQN)
    - param types without '.': assume they live in <base_package>.dto
      e.g. "UserDto" → "com.example.userservice.dto.UserDto"
    - Primitive / well-known types that never need import are skipped.
    """
    NO_IMPORT = {
        "void", "String", "Integer", "Long", "Double", "Float",
        "Boolean", "Byte", "Short", "Character", "Object",
        "int", "long", "double", "float", "boolean", "byte", "short", "char",
    }

    imports = []

    if dubbo_ref:
        iface = dubbo_ref.get("interface", "")
        if iface:
            imports.append(iface)

    for ep in endpoints:
        for p in ep.get("params", []):
            t = p.get("type", "").strip()
            if not t or t in NO_IMPORT:
                continue
            if "." in t:
                # Already a FQN
                imports.append(t)
            else:
                # Short name — resolve to <base_package>.dto.<Type>
                if base_package:
                    imports.append(f"{base_package}.dto.{t}")
                    logger.debug(f"_build_imports: resolved '{t}' → '{base_package}.dto.{t}'")
                else:
                    logger.warning(
                        f"_build_imports: cannot resolve short type '{t}' without base_package"
                    )

    return list(dict.fromkeys(imports))  # deduplicate, preserve order