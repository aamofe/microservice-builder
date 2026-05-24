"""
skills/generate_rest_controller/skill.py

v5 修复：
1. [问题1] dubbo 调用方法名不再照搬 endpoint.method_name。
   新增 _resolve_dubbo_methods()：从 context.interfaces 查 provider 接口的真实方法列表，
   按参数类型匹配为每个 endpoint 找到对应的 dubbo 方法，注入模板变量 dubbo_call。
   模板使用 ep.dubbo_call（而非 ep.method_name）生成 RPC 调用行。

2. [问题2] 前置 DTO 守卫：_ensure_dtos()
   扫描所有 endpoint 参数，发现自定义类型（非 Java 基础类型）且 dto 文件不存在时，
   直接内联调用 generate_dto.run()，不依赖 LLM 主动调用 generate_dto skill。
   这样即使 LLM 跳过了 generate_dto，编译也不会因缺少 DTO 而失败。
"""

from __future__ import annotations

from pathlib import Path

from project_context import ProjectContext
from tools.template_engine import get_engine
from tools.maven_helper import PomEditor
import logging

logger = logging.getLogger(__name__)

# 永远不需要生成 DTO 的类型
_BUILTIN_TYPES = {
    "void", "boolean", "byte", "short", "int", "long", "float", "double", "char",
    "String", "Object", "Boolean", "Byte", "Short", "Integer", "Long",
    "Float", "Double", "Character", "Void", "Number",
    # 集合类（泛型参数暂不处理）
    "List", "Map", "Set", "Collection",
}

_HTTP_METHOD_NORMALIZE = {
    "get": "Get", "post": "Post", "put": "Put",
    "delete": "Delete", "patch": "Patch",
    "GET": "Get", "POST": "Post", "PUT": "Put",
    "DELETE": "Delete", "PATCH": "Patch",
}


# ---------------------------------------------------------------------------
# 问题2修复：前置 DTO 守卫
# ---------------------------------------------------------------------------

def _ensure_dtos(module_name: str, endpoints: list, context: ProjectContext) -> list[str]:
    """
    扫描 endpoints 所有参数类型，发现自定义类型且 DTO 文件不存在时自动生成。
    返回本次自动生成的 DTO 类名列表（用于日志）。

    直接 import 并调用 generate_dto.run()，不走 skill_runner，
    避免循环依赖和 LLM 遗漏调用的双重问题。
    """
    import importlib
    try:
        generate_dto = importlib.import_module("skills.generate_dto.skill")
    except ImportError:
        logger.warning("_ensure_dtos: cannot import generate_dto skill, skipping auto-generation")
        return []

    module_info = context.modules.get(module_name)
    if not module_info:
        return []

    base_package = module_info.base_package
    dto_package = base_package + ".dto"
    module_path = context.module_path(module_name)
    src_root = module_path / "src" / "main" / "java"
    dto_dir = src_root / Path(*dto_package.split("."))

    auto_generated = []

    for ep in endpoints:
        for param in ep.get("params", []):
            raw_type = param.get("type", "").strip()
            # 去掉泛型部分，取主类名
            base_type = raw_type.split("<")[0].strip()

            if not base_type or base_type in _BUILTIN_TYPES or "." in base_type:
                continue

            # 已在 kv 表记录过
            fqn = f"{dto_package}.{base_type}"
            if context.get_kv(f"dto:{fqn}"):
                continue

            # 文件已存在（可能是上一次运行生成的）
            dto_file = dto_dir / f"{base_type}.java"
            if dto_file.exists():
                context.set_kv(f"dto:{fqn}", fqn)
                logger.info(f"_ensure_dtos: '{base_type}' file already exists, registered in kv")
                continue

            logger.info(f"_ensure_dtos: auto-generating missing DTO '{base_type}' for module '{module_name}'")
            result = generate_dto.run(
                {
                    "module_name": module_name,
                    "class_name": base_type,
                    "fields": [],   # 空字段——骨架够用，编译不报错
                },
                context,
            )
            if result.get("status") in ("success", "already_exists"):
                auto_generated.append(base_type)
                logger.info(f"_ensure_dtos: '{base_type}' generated/confirmed → {result.get('file', '')}")
            else:
                logger.error(f"_ensure_dtos: failed to generate '{base_type}': {result}")

    return auto_generated


# ---------------------------------------------------------------------------
# 问题1修复：dubbo 调用方法解析
# ---------------------------------------------------------------------------

def _resolve_dubbo_methods(
    endpoints: list,
    dubbo_ref: dict,
    context: ProjectContext,
) -> list[dict]:
    """
    为每个 endpoint 解析对应的 dubbo 调用方法名和参数列表。

    匹配规则（严格）：
      - 只有 endpoint 参数类型列表与接口方法参数类型列表完全一致时，才标记 matched=True
      - 其他情况一律 matched=False，模板生成 TODO 注释而不是错误代码

    dubbo_call 结构：
      {
        "matched": bool,        # True = 精确匹配，False = 无法匹配
        "method_name": str,     # 匹配到的 dubbo 方法名（matched=False 时为 None）
        "params": list[dict],   # dubbo 方法的参数列表（用于日志提示）
        "iface_methods": list,  # provider 接口所有方法（供模板生成 TODO 注释参考）
      }
    """
    if not dubbo_ref:
        return endpoints

    iface_fqn: str = dubbo_ref.get("interface", "")
    iface_info = context.interfaces.get(iface_fqn)

    iface_methods: list[dict] = iface_info.methods if iface_info else []

    if not iface_info:
        logger.warning(
            f"_resolve_dubbo_methods: interface '{iface_fqn}' not found in context, "
            f"available: {list(context.interfaces.keys())}"
        )

    def _match(ep: dict) -> dict:
        ep_types = [p.get("type", "") for p in ep.get("params", [])]

        # 唯一匹配条件：参数类型列表完全一致
        for m in iface_methods:
            m_types = [p.get("type", "") for p in m.get("params", [])]
            if m_types == ep_types:
                logger.info(
                    f"_resolve_dubbo_methods: '{ep['method_name']}' → exact match "
                    f"'{m['name']}' in '{iface_fqn}'"
                )
                return {
                    "matched": True,
                    "method_name": m["name"],
                    "params": m.get("params", []),
                    "iface_methods": iface_methods,
                }

        # 无法匹配：生成 TODO，不生成错误代码
        available = [f"{m['name']}({', '.join(p['type'] for p in m.get('params',[]))})"
                     for m in iface_methods]
        logger.warning(
            f"_resolve_dubbo_methods: '{ep['method_name']}' (types={ep_types}) "
            f"cannot match any method in '{iface_fqn}'. "
            f"Available: {available}. Generating TODO comment."
        )
        return {
            "matched": False,
            "method_name": None,
            "params": [],
            "iface_methods": iface_methods,
        }

    for ep in endpoints:
        ep["dubbo_call"] = _match(ep)

    return endpoints


# ---------------------------------------------------------------------------
# 原有辅助函数（保持不变）
# ---------------------------------------------------------------------------

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
    interface_fqn: str = dubbo_ref.get("interface", "")
    if not interface_fqn:
        return None

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
        logger.info(f"Injected dependency {group_id}:{provider_artifact} into {module_name}/pom.xml")
        context.log_change(
            module_name=module_name,
            file_path="pom.xml",
            change_type="modify_config",
            description=f"Auto-injected provider dependency: {provider_artifact}",
        )
    else:
        logger.info(f"Dependency {provider_artifact} already present in {module_name}/pom.xml")

    return provider_artifact


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


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

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

    # ★ [问题2修复] 前置 DTO 守卫：确保所有自定义参数类型都有对应的 DTO 文件
    auto_dtos = _ensure_dtos(module_name, endpoints, context)
    if auto_dtos:
        logger.info(f"Auto-generated DTOs before controller: {auto_dtos}")

    # ★ [问题1修复] 解析每个 endpoint 对应的真实 dubbo 方法
    if dubbo_ref:
        endpoints = _resolve_dubbo_methods(endpoints, dubbo_ref, context)

    # 自动注入 provider 依赖到 pom.xml
    injected_provider = None
    if dubbo_ref:
        injected_provider = _inject_provider_dependency(module_name, dubbo_ref, context)

    imports = _build_imports(endpoints, dubbo_ref, base_package)

    engine = get_engine()
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
        "auto_generated_dtos": auto_dtos,
    }