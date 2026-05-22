"""
skills/incremental_modify/skill.py
增量修改已有 Java 文件（添加方法 / 字段 / 注解）。

v2 核心变更：
- 每次操作前查询 context.has_method() 去重（跨 session 有效）
- 成功写入后用 context.record_method() + context.log_change() 持久化
- 支持 target_type: "interface" | "impl" | "controller" | "entity" | "auto"
  → "auto" 时从 context 自动解析 FQN 对应的文件路径
- diff 展示更友好，支持 rich 格式
"""

from __future__ import annotations

import re
from pathlib import Path

from project_context import ProjectContext, MethodRecord
from tools.java_parser_wrapper import JavaFileEditor, MethodDef, FieldDef
import logging

logger = logging.getLogger(__name__)


def run(params: dict, context: ProjectContext) -> dict:
    """
    params:
        module_name: str
        target_class: str           # 简名如 "UserService" 或全限定名
        target_type: str            # "interface"|"impl"|"controller"|"entity"|"auto" (默认 auto)
        relative_java_path: str     # 显式指定路径（优先于 target_class 解析）
        operations: list[dict]
            type: "add_method" | "add_field" | "add_annotation" | "add_implements"
            payload: dict
        auto_approve: bool          # 默认 False（生产环境建议 False）
        interface_sync: bool        # add_method 时同时修改接口和实现类（默认 True）
    """
    module_name: str = params["module_name"]
    operations: list = params.get("operations", [])
    auto_approve: bool = bool(params.get("auto_approve", False))
    interface_sync: bool = bool(params.get("interface_sync", True))

    if not operations:
        return {"status": "no_op", "message": "No operations specified."}

    module_path = context.module_path(module_name)
    module_info = context.modules.get(module_name)
    if not module_info:
        return {"status": "error", "message": f"Module '{module_name}' not found in context."}

    src_root = module_path / "src" / "main" / "java"

    # ----------------------------------------------------------------
    # 解析目标文件路径
    # ----------------------------------------------------------------
    java_file, class_fqn = _resolve_target(params, context, module_info, src_root)
    if java_file is None:
        return {"status": "error", "message": class_fqn}   # class_fqn 此时存放的是错误信息

    if not java_file.exists():
        return {"status": "error", "message": f"Target file not found: {java_file}"}

    editor = JavaFileEditor(java_file)
    original_source = editor.source
    applied = []

    # ----------------------------------------------------------------
    # 执行操作
    # ----------------------------------------------------------------
    for op in operations:
        op_type = op.get("type")
        payload = op.get("payload", {})
        result = _apply_operation(op_type, payload, editor, class_fqn, context)
        applied.append(result)

    # ----------------------------------------------------------------
    # 差异检测
    # ----------------------------------------------------------------
    diff_text = editor.diff(original_source)

    if not diff_text.strip():
        return {
            "status": "no_change",
            "message": "No changes produced (all methods may already exist).",
            "operations": applied,
        }

    # ----------------------------------------------------------------
    # 展示 diff，等待用户确认
    # ----------------------------------------------------------------
    if not auto_approve:
        _print_diff(java_file, diff_text)
        answer = input("Apply these changes? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            return {
                "status": "rejected",
                "message": "User rejected the changes.",
                "diff": diff_text,
                "operations": applied,
            }

    # ----------------------------------------------------------------
    # 写入文件
    # ----------------------------------------------------------------
    editor.save()

    # ----------------------------------------------------------------
    # interface_sync: 若目标是接口，同步给 impl 也加一遍方法签名
    # ----------------------------------------------------------------
    sync_results = []
    if interface_sync and params.get("target_type", "auto") in ("interface", "auto"):
        iface_info = context.interfaces.get(class_fqn)
        if iface_info:
            sync_results = _sync_to_impl(
                iface_info.impl_fqn, operations, context, module_info, src_root, auto_approve
            )

    # ----------------------------------------------------------------
    # 持久化方法记录 + 变更历史
    # ----------------------------------------------------------------
    newly_added = []
    for op in operations:
        if op.get("type") == "add_method":
            pl = op.get("payload", {})
            name = pl.get("name", "")
            if name and not context.has_method(class_fqn, name):
                context.record_method(MethodRecord(
                    class_fqn=class_fqn,
                    method_name=name,
                    return_type=pl.get("return_type", "void"),
                    params=pl.get("params", []),
                    source="generated",
                ))
                newly_added.append(name)

    # 同步更新 InterfaceInfo 中的 methods 列表
    _update_interface_methods_in_context(class_fqn, operations, context)

    context.log_change(
        module_name=module_name,
        file_path=str(java_file.relative_to(module_path)),
        change_type="add_method",
        description=f"Incremental modify: {[a['result'] for a in applied]}",
        diff_summary=diff_text[:500],
    )

    return {
        "status": "success",
        "file": str(java_file),
        "class_fqn": class_fqn,
        "operations": applied,
        "newly_recorded_methods": newly_added,
        "impl_sync": sync_results,
        "diff": diff_text,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_target(params: dict, context: ProjectContext,
                    module_info, src_root: Path) -> tuple[Path | None, str]:
    """
    返回 (java_file_path, class_fqn)。
    出错时返回 (None, error_message)。
    """
    # 1. 优先使用显式路径
    if params.get("relative_java_path"):
        rel = params["relative_java_path"]
        java_file = src_root / rel
        # 从路径推导 fqn
        fqn = rel.replace("/", ".").replace("\\", ".").removesuffix(".java")
        return java_file, fqn

    # 2. 根据 target_class 解析
    target_class: str = params.get("target_class", "")
    target_type: str = params.get("target_type", "auto")

    if not target_class:
        return None, "Must provide either 'relative_java_path' or 'target_class'."

    # 如果是全限定名
    if "." in target_class:
        class_fqn = target_class
        rel_path = class_fqn.replace(".", "/") + ".java"
        return src_root / rel_path, class_fqn

    # 简名 → 从 context 推导
    base_pkg = module_info.base_package

    if target_type == "interface":
        # 查 context interfaces
        info = context.get_interface_by_name(target_class, module_info.name)
        if info:
            rel = info.interface_fqn.replace(".", "/") + ".java"
            return src_root / rel, info.interface_fqn
        # fallback：约定路径
        fqn = f"{base_pkg}.api.{target_class}"
        return src_root / (fqn.replace(".", "/") + ".java"), fqn

    elif target_type == "impl":
        info = context.get_interface_by_name(target_class.removesuffix("Impl"), module_info.name)
        if info:
            rel = info.impl_fqn.replace(".", "/") + ".java"
            return src_root / rel, info.impl_fqn
        impl_name = target_class if target_class.endswith("Impl") else target_class + "Impl"
        fqn = f"{base_pkg}.service.{impl_name}"
        return src_root / (fqn.replace(".", "/") + ".java"), fqn

    elif target_type == "controller":
        fqn = f"{base_pkg}.controller.{target_class}"
        return src_root / (fqn.replace(".", "/") + ".java"), fqn

    elif target_type == "entity":
        fqn = f"{base_pkg}.entity.{target_class}"
        return src_root / (fqn.replace(".", "/") + ".java"), fqn

    else:
        # auto: 按优先级尝试
        for sub in ("api", "service", "controller", "entity"):
            candidate_fqn = f"{base_pkg}.{sub}.{target_class}"
            candidate_path = src_root / (candidate_fqn.replace(".", "/") + ".java")
            if candidate_path.exists():
                return candidate_path, candidate_fqn
        # 最后回退到 api package
        fqn = f"{base_pkg}.api.{target_class}"
        return src_root / (fqn.replace(".", "/") + ".java"), fqn


def _apply_operation(op_type: str, payload: dict, editor: JavaFileEditor,
                     class_fqn: str, context: ProjectContext) -> dict:
    """执行单个操作，返回操作结果 dict。"""
    if op_type == "add_method":
        method_name = payload.get("name", "")
        # 跨 session 去重：先查 DB，再查文件
        if context.has_method(class_fqn, method_name):
            return {"type": op_type, "name": method_name, "result": "skipped (recorded in DB)"}
        if editor.has_method(method_name):
            # 文件里有但 DB 没记 → 补录
            context.record_method(MethodRecord(
                class_fqn=class_fqn,
                method_name=method_name,
                return_type=payload.get("return_type", "void"),
                params=payload.get("params", []),
                source="scanned",
            ))
            return {"type": op_type, "name": method_name, "result": "skipped (found in file, recorded)"}

        m = MethodDef(
            name=method_name,
            return_type=payload.get("return_type", "void"),
            params=[(p["type"], p["name"]) for p in payload.get("params", [])],
            body=payload.get("body", "// TODO: implement"),
            annotations=payload.get("annotations"),
            access=payload.get("access", "public"),
            throws=payload.get("throws"),
        )
        editor.add_method(m, imports=payload.get("imports", []))
        return {"type": op_type, "name": method_name, "result": "added"}

    elif op_type == "add_field":
        f = FieldDef(
            name=payload["name"],
            field_type=payload["type"],
            annotations=payload.get("annotations"),
            access=payload.get("access", "private"),
            initial_value=payload.get("initial_value"),
        )
        editor.add_field(f, imports=payload.get("imports", []))
        return {"type": op_type, "name": payload["name"], "result": "added"}

    elif op_type == "add_annotation":
        ann = payload.get("annotation", "")
        if editor.has_annotation(ann):
            return {"type": op_type, "annotation": ann, "result": "skipped (already present)"}
        editor.add_class_annotation(ann)
        return {"type": op_type, "annotation": ann, "result": "added"}

    elif op_type == "add_implements":
        iface = payload.get("interface", "")
        editor.add_interface_to_implements(iface)
        return {"type": op_type, "interface": iface, "result": "added"}

    else:
        return {"type": op_type, "result": f"unknown op type: {op_type}"}


def _sync_to_impl(impl_fqn: str, operations: list, context: ProjectContext,
                  module_info, src_root: Path, auto_approve: bool) -> list:
    """将接口的 add_method 操作同步给 impl 类（加 @Override 和 TODO body）。"""
    impl_path = src_root / (impl_fqn.replace(".", "/") + ".java")
    if not impl_path.exists():
        return [{"result": f"impl file not found: {impl_path}"}]

    impl_editor = JavaFileEditor(impl_path)
    original = impl_editor.source
    sync_applied = []

    for op in operations:
        if op.get("type") != "add_method":
            continue
        payload = op.get("payload", {})
        method_name = payload.get("name", "")

        if context.has_method(impl_fqn, method_name) or impl_editor.has_method(method_name):
            sync_applied.append({"method": method_name, "result": "skipped"})
            continue

        annotations = ["@Override"] + (payload.get("annotations") or [])
        m = MethodDef(
            name=method_name,
            return_type=payload.get("return_type", "void"),
            params=[(p["type"], p["name"]) for p in payload.get("params", [])],
            body=payload.get("body", "// TODO: implement"),
            annotations=annotations,
            access=payload.get("access", "public"),
            throws=payload.get("throws"),
        )
        impl_editor.add_method(m, imports=payload.get("imports", []))
        sync_applied.append({"method": method_name, "result": "added to impl"})

    diff = impl_editor.diff(original)
    if diff.strip():
        if not auto_approve:
            _print_diff(impl_path, diff, label="[IMPL SYNC]")
            ans = input("Apply impl sync? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                return [{"result": "impl sync rejected"}]
        impl_editor.save()
        for item in sync_applied:
            if item.get("result") == "added to impl":
                pl = next((o["payload"] for o in operations
                           if o.get("type") == "add_method" and o["payload"].get("name") == item["method"]), {})
                context.record_method(MethodRecord(
                    class_fqn=impl_fqn,
                    method_name=item["method"],
                    return_type=pl.get("return_type", "void"),
                    params=pl.get("params", []),
                    source="generated",
                ))

    return sync_applied


def _update_interface_methods_in_context(class_fqn: str, operations: list, context: ProjectContext):
    """将新增方法追加到 InterfaceInfo.methods 列表并持久化。"""
    info = context.interfaces.get(class_fqn)
    if not info:
        return
    existing_names = {m["name"] for m in info.methods}
    for op in operations:
        if op.get("type") == "add_method":
            pl = op.get("payload", {})
            name = pl.get("name", "")
            if name and name not in existing_names:
                info.methods.append({
                    "name": name,
                    "return_type": pl.get("return_type", "void"),
                    "params": pl.get("params", []),
                })
                existing_names.add(name)
    context.save_interface(info)


def _print_diff(java_file: Path, diff_text: str, label: str = ""):
    prefix = f"{label} " if label else ""
    print(f"\n{'='*60}")
    print(f"{prefix}Proposed changes to: {java_file}")
    print("=" * 60)
    try:
        import colorama
        colorama.init(autoreset=True)
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                print(colorama.Fore.GREEN + line)
            elif line.startswith("-") and not line.startswith("---"):
                print(colorama.Fore.RED + line)
            else:
                print(line)
    except ImportError:
        print(diff_text)
    print("=" * 60)