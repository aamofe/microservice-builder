"""
skills/generate_rest_controller/skill.py
Generates a Spring MVC REST controller that delegates to a Dubbo service.

Fix (v2):
- _normalize_endpoints(): 兼容 LLM 传 http_method/method、method_name/handler_name 等变体键名
- endpoints 里的 params 字段可缺失，归一化为 []
- 幂等：已存在的文件不覆盖（overwrite=False）
- 记录 change_history / controller_classes
"""

from __future__ import annotations

from pathlib import Path

from project_context import ProjectContext
from tools.template_engine import get_engine
import logging

logger = logging.getLogger(__name__)

# HTTP 方法名归一化映射（LLM 可能传大写/小写/全名）
_HTTP_METHOD_NORMALIZE = {
    "get": "Get", "post": "Post", "put": "Put",
    "delete": "Delete", "patch": "Patch",
    "GET": "Get", "POST": "Post", "PUT": "Put",
    "DELETE": "Delete", "PATCH": "Patch",
}


def _normalize_endpoints(endpoints: list) -> list[dict]:
    """
    规范化 endpoints 列表，兼容 LLM 传来的各种键名变体：
    - http_method / method  →  统一为 http_method（首字母大写，供模板 @{http_method}Mapping 用）
    - method_name / handler_name / name  →  统一为 method_name
    - params 缺失  →  补 []
    - params 里的元素若为字符串  →  {name: str, type: "String", source: "query"}
    """
    result = []
    for ep in endpoints:
        if not isinstance(ep, dict):
            logger.warning(f"generate_rest_controller: unexpected endpoint format, skipping: {ep!r}")
            continue

        # http_method
        raw_method = ep.get("http_method") or ep.get("method") or "Get"
        http_method = _HTTP_METHOD_NORMALIZE.get(raw_method, raw_method.capitalize())

        # method_name
        method_name = (
            ep.get("method_name")
            or ep.get("handler_name")
            or ep.get("name")
            or "handle"
        )

        # params
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

    # controller_name 兼容 class_name
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

    # 幂等检查
    if out_path.exists() and not overwrite:
        logger.info(f"Controller already exists, skipping: {out_path}")
        return {
            "status": "already_exists",
            "file": str(out_path),
            "controller_fqn": ctrl_fqn,
            "message": f"Controller '{controller_name}' already exists. Use overwrite=true to regenerate.",
        }

    imports = _build_imports(endpoints, dubbo_ref)

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


def _build_imports(endpoints: list, dubbo_ref: dict) -> list[str]:
    imports = []
    if dubbo_ref:
        iface = dubbo_ref.get("interface", "")
        if iface:
            imports.append(iface)
    for ep in endpoints:
        for p in ep.get("params", []):
            t = p.get("type", "")
            if "." in t:
                imports.append(t)
    return list(dict.fromkeys(imports))