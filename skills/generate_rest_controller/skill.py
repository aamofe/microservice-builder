"""
skills/generate_rest_controller/skill.py

v4 变更：
- 当 dubbo_ref 存在时，自动将 provider 模块注入 consumer 的 pom.xml。
  推断逻辑：dubbo_ref.interface FQN → base_package 前缀匹配 context.modules
  → 找到 provider artifactId → 写入 <dependency>。
  不依赖 LLM 传 module_dependencies，硬保证编译期依赖存在。
"""

from __future__ import annotations

from pathlib import Path

from project_context import ProjectContext
from tools.template_engine import get_engine
from tools.maven_helper import PomEditor
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


def _inject_provider_dependency(
    module_name: str,
    dubbo_ref: dict,
    context: ProjectContext,
) -> str | None:
    """
    当 consumer 的 controller 引用了 provider 的接口时，
    自动将 provider 模块写入 consumer 的 pom.xml。

    推断逻辑：
      dubbo_ref["interface"] = "com.example.userservice.api.UserService"
      遍历 context.modules，找 base_package 是该 FQN 前缀的模块
      → provider_artifact = module_info.artifact_id（通常等于 module_name）

    返回注入的 artifactId，或 None（未找到 / 已存在）。
    """
    interface_fqn: str = dubbo_ref.get("interface", "")
    if not interface_fqn:
        return None

    # 找 provider 模块：base_package 是 interface_fqn 的前缀
    provider_artifact: str | None = None
    for mod_name, mod_info in context.modules.items():
        if mod_name == module_name:
            continue
        if interface_fqn.startswith(mod_info.base_package + "."):
            provider_artifact = mod_info.artifact_id or mod_name
            logger.info(
                f"_inject_provider_dependency: '{module_name}' → '{provider_artifact}' "
                f"(matched base_package '{mod_info.base_package}')"
            )
            break

    if not provider_artifact:
        logger.warning(
            f"_inject_provider_dependency: cannot resolve provider for '{interface_fqn}' "
            f"among modules {list(context.modules.keys())}"
        )
        return None

    consumer_pom = context.module_path(module_name) / "pom.xml"
    if not consumer_pom.exists():
        logger.warning(f"_inject_provider_dependency: pom not found at {consumer_pom}")
        return None

    group_id = context.modules[module_name].group_id
    version_ref = "${project.version}"

    editor = PomEditor(consumer_pom)
    added = editor.add_dependency(
        group_id=group_id,
        artifact_id=provider_artifact,
        version=version_ref,
    )
    if added:
        editor.save()
        logger.info(
            f"Injected dependency {group_id}:{provider_artifact} into {module_name}/pom.xml"
        )
        context.log_change(
            module_name=module_name,
            file_path="pom.xml",
            change_type="modify_config",
            description=f"Auto-injected provider dependency: {provider_artifact}",
        )
    else:
        logger.info(
            f"Dependency {provider_artifact} already present in {module_name}/pom.xml"
        )

    return provider_artifact


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

    # ★ 自动注入 provider 依赖到 pom.xml（不依赖 LLM 传 module_dependencies）
    injected_provider = None
    if dubbo_ref:
        injected_provider = _inject_provider_dependency(module_name, dubbo_ref, context)

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
        "injected_provider_dependency": injected_provider,
    }


def _build_imports(endpoints: list, dubbo_ref: dict, base_package: str = "") -> list[str]:
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
                imports.append(t)
            else:
                if base_package:
                    imports.append(f"{base_package}.dto.{t}")
                else:
                    logger.warning(
                        f"_build_imports: cannot resolve short type '{t}' without base_package"
                    )

    return list(dict.fromkeys(imports))