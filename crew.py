"""
crew.py  —  patch sections
替换内容：
  1. SKILL_REQUIRED_PARAMS  新增常量
  2. _validate_skill_params  新增函数
  3. _invoke_skill_with_limit  替换（区分参数错误 vs 运行时错误）
  4. SkillRunnerTool  替换（description 内联必填参数 + 前置校验）

其余 Agent / Task / build_crew / run_crew 代码不变。
"""

from __future__ import annotations

import json
import os
import logging
import time
from textwrap import dedent
from pathlib import Path

from crewai import Agent, Task, Crew, Process
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from project_context import ProjectContext, ContextRegistry, GlobalNacosState
from tools.skill_runner import run_skill, list_skills
from tools.validator import MavenValidator

logger = logging.getLogger(__name__)

CURRENT_PROJECT: str | None = None

# ──────────────────────────────────────────────────────────────────────────────
# ① 每个 skill 的必填参数 schema（新增）
#    key = skill_name, value = list of required param names
# ──────────────────────────────────────────────────────────────────────────────

SKILL_REQUIRED_PARAMS: dict[str, list[str]] = {
    # ★ port 从必填列表移除：skill 层强制忽略 LLM 传入的 port，由系统自动分配
    "create_spring_boot_module": ["module_name"],
    "generate_dubbo_interface":  ["module_name", "interface_name", "methods"],
    "generate_entity":           ["module_name", "class_name"],
    "generate_rest_controller":  ["module_name", "controller_name", "base_path", "endpoints"],
    "add_threadpool_config":     ["module_name"],
    "incremental_modify":        ["module_name", "operations"],
    "update_docker_compose":     [],
    "modify_dubbo_config":       ["module_name"],
    "update_nacos_config":       [],
    "generate_dto":              ["module_name", "class_name"],
}

# skill description 内联摘要，注入到 SkillRunnerTool.description（LLM 可见）
_SKILL_PARAM_HINTS = """
Required params per skill (ALL must be in the params JSON):
  create_spring_boot_module : module_name(str)
                              *** DO NOT pass port — it is auto-assigned by the system.
                              *** The response will include `assigned_port`; use that value
                              *** in all subsequent skill calls for this module.
  generate_dubbo_interface  : module_name(str), interface_name(str), methods(list[{name,return_type,params}])
  generate_entity           : module_name(str), class_name(str), fields(list[{name,type}])
  generate_rest_controller  : module_name(str), controller_name(str), base_path(str),
                              endpoints(list[{http_method,path,method_name,params}])
                              *** If this module calls a Dubbo service from another module,
                              *** you MUST pass dubbo_ref:
                              ***   dubbo_ref = {
                              ***     "interface": "<fully.qualified.InterfaceName>",
                              ***     "field_name": "<camelCase field name in controller>"
                              ***   }
                              *** This injects @DubboReference into the controller so the
                              *** consumer can discover the provider via Nacos.
                              *** Example: order-service calling user-service:
                              ***   dubbo_ref = {
                              ***     "interface": "com.example.userservice.api.UserService",
                              ***     "field_name": "userService"
                              ***   }
  add_threadpool_config     : module_name(str)
  incremental_modify        : module_name(str), target_class(str), operations(list)
  update_docker_compose     : (no required params)
  generate_dto              : module_name(str), class_name(str), fields(list[{name,type}])
"""
 


# ──────────────────────────────────────────────────────────────────────────────
# ② 参数校验函数（新增）
# ──────────────────────────────────────────────────────────────────────────────

def _validate_skill_params(skill_name: str, params: dict) -> str | None:
    """
    校验 params 是否包含 skill 的所有必填字段。
    返回 None 表示通过，否则返回带修复提示的错误字符串。
    """
    required = SKILL_REQUIRED_PARAMS.get(skill_name)
    if required is None:
        # 未知 skill，跳过校验（skill_runner 会处理 not found）
        return None

    missing = [k for k in required if k not in params or params[k] is None]
    if not missing:
        return None

    # 生成友好的错误提示，帮助 LLM 自我修正
    hints = {
        "module_name":       "the exact module name you just created, e.g. 'user-service'",
        "interface_name":    "the Java interface class name, e.g. 'UserService'",
        "class_name":        "the Java class name, e.g. 'User'",
        "controller_name":   "the controller class name, e.g. 'UserController'",
        "methods":           "list of {name, return_type, params:[{name,type}]}",
        "fields":            "list of {name, type} — do NOT include 'id', it is auto-generated",
        "endpoints":         "list of {http_method, path, method_name, params:[{name,type,source}]}",
        "operations":        "list of {type:'add_method', payload:{name, return_type, params}}",
        "base_path":         "URL base path, e.g. '/api/v1/users'",
    }
    hint_lines = "\n".join(
        f"  - {k}: {hints.get(k, 'required string')}" for k in missing
    )
    return (
        f"Skill '{skill_name}' is missing required params: {missing}.\n"
        f"Fix by re-calling with these fields added to params JSON:\n{hint_lines}\n"
        f"Full required params for {skill_name}: {SKILL_REQUIRED_PARAMS[skill_name]}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# ③ 带重试上限的 skill 调用（替换原版）
#    核心改动：区分 missing_param 错误（不重试）vs 运行时错误（重试）
# ──────────────────────────────────────────────────────────────────────────────

_MAX_SKILL_RETRIES = 3


def _invoke_skill_with_limit(skill_name: str, params: dict, ctx: ProjectContext) -> dict:
    """
    调用 run_skill，最多重试 _MAX_SKILL_RETRIES 次（每次间隔 1s）。

    不重试的情况（直接返回 final_error）：
    - 参数缺失 / 参数错误（重试结果完全一样，没有意义）
    - status == already_exists（幂等，不是错误）

    重试的情况：
    - 运行时异常（网络、IO、模板渲染等）
    - status == error 且不是参数问题
    """
    last_err = ""
    for attempt in range(1, _MAX_SKILL_RETRIES + 1):
        try:
            result = run_skill(skill_name, params, ctx)

            # already_exists 不是错误，直接返回
            if isinstance(result, dict) and result.get("status") == "already_exists":
                return result

            if isinstance(result, dict) and result.get("status") == "error":
                msg = result.get("message", "unknown error")

                # 判断是否是参数错误 → 不重试，直接返回带提示的 final_error
                param_error_keywords = (
                    "missing required param", "keyerror", "not found in context",
                    "must provide", "missing param",
                )
                if any(kw in msg.lower() for kw in param_error_keywords):
                    logger.error(
                        f"Skill '{skill_name}' param error (no retry): {msg}"
                    )
                    return {
                        "status": "final_error",
                        "error_type": "param_error",
                        "message": (
                            f"Skill '{skill_name}' failed due to missing/wrong params: {msg}. "
                            "Re-call this skill with the correct params. DO NOT skip."
                        ),
                    }

                last_err = msg
                logger.warning(
                    f"Skill '{skill_name}' attempt {attempt}/{_MAX_SKILL_RETRIES} failed: {last_err}"
                )
                if attempt < _MAX_SKILL_RETRIES:
                    time.sleep(1)
                continue

            return result

        except Exception as e:
            last_err = str(e)
            logger.warning(
                f"Skill '{skill_name}' attempt {attempt}/{_MAX_SKILL_RETRIES} exception: {e}"
            )
            if attempt < _MAX_SKILL_RETRIES:
                time.sleep(1)

    msg = (
        f"Skill '{skill_name}' failed after {_MAX_SKILL_RETRIES} attempts. "
        f"Last error: {last_err}. "
        "DO NOT retry this skill. Report the failure and move to the next task."
    )
    logger.error(msg)
    return {"status": "final_error", "message": msg}


# ──────────────────────────────────────────────────────────────────────────────
# 其余辅助函数（不变）
# ──────────────────────────────────────────────────────────────────────────────

def _validate_project(project_name: str) -> str | None:
    if CURRENT_PROJECT and project_name != CURRENT_PROJECT:
        return (
            f"project_name '{project_name}' does not match the current project '{CURRENT_PROJECT}'. "
            f"You MUST use project_name='{CURRENT_PROJECT}' in all tool calls."
        )
    return None


def _get_ctx(project_name: str) -> ProjectContext:
    return ContextRegistry.get_or_create(
        project_name,
        projects_root=os.getenv("PROJECTS_ROOT", "./output"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# ④ SkillRunnerTool（替换原版）
#    核心改动：description 内联必填参数 + 执行前前置校验
# ──────────────────────────────────────────────────────────────────────────────

class SkillInput(BaseModel):
    skill_name: str = Field(description="Skill name to invoke")
    params: str = Field(description="JSON string of parameters")
    project_name: str = Field(description="Project name — MUST match the current project")


class SkillRunnerTool(BaseTool):
    name: str = "run_skill"
    description: str = (
        "Invoke a microservice generation skill. "
        "Available skills: " + ", ".join(list_skills()) + ".\n"
        + _SKILL_PARAM_HINTS +
        "RULES:\n"
        "- project_name MUST equal the project you were given at task start.\n"
        "- If skill returns status=final_error with error_type=param_error: "
          "re-call immediately with the correct params (do NOT skip).\n"
        "- If skill returns status=final_error (runtime): STOP and report.\n"
        "- If skill returns status=already_exists: skip, move on."
    )
    args_schema: type[BaseModel] = SkillInput

    def _run(self, skill_name: str, params: str, project_name: str) -> str:
        # 1. project_name 白名单校验
        err = _validate_project(project_name)
        if err:
            return json.dumps({"status": "error", "message": err})

        # 2. 解析 params JSON
        try:
            p = json.loads(params)
        except json.JSONDecodeError as e:
            return json.dumps({"status": "error", "message": f"Invalid JSON params: {e}"})

        # ★ 自动重定向：LLM 用 generate_entity 生成 DTO 时强制转为 generate_dto
        _DTO_SUFFIXES = ("DTO", "Dto", "Request", "Response", "Vo", "VO", "Form")
        if skill_name == "generate_entity":
            class_name = p.get("class_name") or p.get("entity_name") or p.get("name") or ""
            if any(class_name.endswith(s) for s in _DTO_SUFFIXES):
                logger.info(
                    f"Auto-redirect: generate_entity(class_name='{class_name}') "
                    f"→ generate_dto (DTO suffix detected)"
                )
                skill_name = "generate_dto"

        # 3. ★ 前置参数校验（新增）
        param_err = _validate_skill_params(skill_name, p)
        if param_err:
            return json.dumps({
                "status": "error",
                "error_type": "param_error",
                "message": param_err,
            })

        # 4. 带重试上限的 skill 调用
        ctx = _get_ctx(project_name)
        result = _invoke_skill_with_limit(skill_name, p, ctx)
        return json.dumps(result, indent=2, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────────────
# ProjectStateTool / ValidateTool（不变，保持原样）
# ──────────────────────────────────────────────────────────────────────────────

class ProjectStateInput(BaseModel):
    project_name: str = Field(description="Project name — MUST match the current project")
    class_fqn: str = Field(default="", description="Optional: FQN of a class to inspect its methods")


class ProjectStateTool(BaseTool):
    name: str = "get_project_state"
    description: str = (
        "Get the full current state of a project: all modules, interfaces, entities, "
        "existing methods per class, and Nacos/Docker status. "
        "Optionally pass class_fqn to get the method list of a specific class. "
        "Call this ONCE at the start of your task — do not call it repeatedly in a loop."
    )
    args_schema: type[BaseModel] = ProjectStateInput

    def _run(self, project_name: str, class_fqn: str = "") -> str:
        err = _validate_project(project_name)
        if err:
            return json.dumps({"status": "error", "message": err})
        try:
            ctx = _get_ctx(project_name)
            summary = ctx.to_rich_summary()
            if class_fqn:
                methods = ctx.get_all_method_names(class_fqn)
                summary["queried_class"] = {"fqn": class_fqn, "known_methods": methods}
            return json.dumps(summary, indent=2, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})


class ValidateInput(BaseModel):
    project_name: str = Field(description="Project name — MUST match the current project")
    module_name: str = Field(default="", description="Specific module (optional)")


class ValidateTool(BaseTool):
    name: str = "validate_project"
    description: str = "Run mvn compile on the project. Call ONCE — do not loop on failures."
    args_schema: type[BaseModel] = ValidateInput

    def _run(self, project_name: str, module_name: str = "") -> str:
        err = _validate_project(project_name)
        if err:
            return json.dumps({"status": "error", "message": err})
        try:
            ctx = _get_ctx(project_name)
            validator = MavenValidator(ctx.project_root)
            result = validator.compile(module=module_name or None)
            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})


TOOLS = [SkillRunnerTool(), ProjectStateTool(), ValidateTool()]


# ──────────────────────────────────────────────────────────────────────────────
# Agent definitions（不变，保持原样）
# ──────────────────────────────────────────────────────────────────────────────

def build_agents(llm_config: dict = None) -> dict[str, Agent]:
    shared = dict(
        tools=TOOLS,
        verbose=True,
        allow_delegation=False,
        max_iter=20,
        **(llm_config or {}),
    )

    requirement_analyst = Agent(
        role="Requirement Analyst",
        goal=(
            "Parse the user's natural language request into a structured specification. "
            "Distinguish NEW (new module/interface) vs EXISTING (incremental modification). "
            "Call get_project_state ONCE, then output the JSON spec. Stop."
        ),
        backstory=dedent(f"""\
            You are a senior software architect. Rules:
            1. Call get_project_state(project_name='{CURRENT_PROJECT}') EXACTLY ONCE.
            2. Read the result and classify each requirement as create_new or add_to_existing.
            3. Output the JSON spec immediately. Do NOT call get_project_state again.
            4. If get_project_state returns an error, output the spec based on the request alone.
        """),
        **shared,
    )

    architect = Agent(
        role="Architect",
        goal=(
            "Design module structure, port assignments, and Dubbo topology. "
            "Call get_project_state ONCE, then output the architecture JSON. Stop."
        ),
        backstory=dedent(f"""\
            You are a Java platform architect. Rules:
            1. Call get_project_state(project_name='{CURRENT_PROJECT}') ONCE to check ports.
            2. Assign new ports starting from max_existing_port + 1.
            3. Port assignment: start from 8051, increment by 1 for each new module. NEVER use 8080, 8081, 8082.
            4. Output the architecture JSON immediately. Do NOT loop.
            5. project_name in ALL tool calls MUST be '{CURRENT_PROJECT}'.

        """),
        **shared,
    )

    code_generator = Agent(
        role="Code Generator",
        goal=(
            "Generate code for NEW modules/interfaces/entities only. "
            "For each new item, call the appropriate skill once. "
            "If a skill returns already_exists or final_error (runtime): move on. "
            "If a skill returns final_error with error_type=param_error: re-call with correct params."
        ),
        backstory=dedent(f"""\
            You are a code generation engine. Rules:
            1. project_name in ALL skill calls MUST be '{CURRENT_PROJECT}'.
            2. Call skills in this EXACT order for EVERY module:
               create_spring_boot_module
               → generate_dubbo_interface   (if module exposes a Dubbo service)
               → generate_entity            (if module has JPA entities)
               → generate_dto               (MANDATORY for EVERY DTO type referenced anywhere)
               → generate_rest_controller
               → add_threadpool_config      (only if has_threadpool=true)
 
            3. DTO RULE — NON-NEGOTIABLE:
               Before calling generate_rest_controller for ANY module, scan ALL endpoint
               param types AND all Dubbo interface method param types.
               For EVERY type that is not a Java primitive or String
               (e.g. UserDTO, OrderDTO, LoginRequest, CreateOrderRequest):
                 - Call generate_dto for that type in the module that OWNS it.
                 - UserDTO lives in user-service, OrderDTO lives in order-service.
               Do NOT skip generate_dto even if you think the type is simple.
               Missing DTO = compilation failure.
 
            4. CONSUMER RULE — NON-NEGOTIABLE:
               If a module calls another module's Dubbo service, pass dubbo_ref to
               generate_rest_controller:
                 dubbo_ref = {{
                   "interface": "<interface_fqn from generate_dubbo_interface result>",
                   "field_name": "<camelCase field name>"
                 }}
               The skill will automatically add the provider as a Maven dependency.
               Never skip dubbo_ref for a consumer module.
 
            5. Other param rules:
               - DO NOT pass port to create_spring_boot_module. Read assigned_port from result.
               - generate_entity: key is 'class_name' (not entity_name), no 'id' in fields.
               - generate_dubbo_interface: methods = list of
                 {{name, return_type, params:[{{name,type}}]}}.
               - generate_rest_controller: key is 'controller_name'.
 
            6. Error handling:
               - param_error → fix and re-call once, then move on.
               - already_exists → skip.
               - final_error (runtime) → record and move to next item.
        """),
        **shared,
    )

    incremental_modifier = Agent(
        role="Incremental Modifier",
        goal=(
            "Add new methods/fields to existing Java files. "
            "Query existing methods ONCE, then call incremental_modify ONCE per target class."
        ),
        backstory=dedent(f"""\
            You specialize in safely evolving existing codebases. Rules:
            1. Call get_project_state(project_name='{CURRENT_PROJECT}', class_fqn=<FQN>) ONCE per class.
            2. Diff requested methods against existing — only add the missing ones.
            3. Call run_skill incremental_modify ONCE with only the missing operations.
            4. If all methods already exist: output "All methods already present" and STOP.
            5. If no incremental changes are needed: output "No incremental modifications required" and STOP.
            6. project_name MUST be '{CURRENT_PROJECT}'.
        """),
        **shared,
    )

    configuration_manager = Agent(
        role="Configuration Manager",
        goal=(
            "Update docker-compose.yml. "
            "Call update_docker_compose ONCE. Output the run commands. Stop."
        ),
        backstory=dedent(f"""\
            You are a DevOps expert. Rules:
            1. Call run_skill update_docker_compose ONCE with project_name='{CURRENT_PROJECT}'.
            2. The skill handles Nacos reuse automatically — do NOT check nacos status separately.
            3. Output the docker-compose up command for the user.
            4. Do NOT call get_project_state or update_docker_compose more than once.
        """),
        **shared,
    )

    validator_agent = Agent(
        role="Validator",
        goal="Run mvn compile ONCE. Report result. Output final delivery summary. Stop.",
        backstory=dedent(f"""\
            You are the quality gate. Rules:
            1. Call validate_project(project_name='{CURRENT_PROJECT}') ONCE.
            2. If build fails: list the [ERROR] lines and suggest fixes. Do NOT re-run.
            3. Output the complete delivery report and STOP.
        """),
        **shared,
    )

    return {
        "requirement_analyst": requirement_analyst,
        "architect": architect,
        "code_generator": code_generator,
        "incremental_modifier": incremental_modifier,
        "configuration_manager": configuration_manager,
        "validator": validator_agent,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Task builders / Crew factory / run_crew（不变，保持原样）
# ──────────────────────────────────────────────────────────────────────────────

def _get_state_snapshot(project_name: str) -> str:
    try:
        ctx = ContextRegistry.get_or_create(
            project_name,
            projects_root=os.getenv("PROJECTS_ROOT", "./output"),
        )
        return json.dumps(ctx.to_rich_summary(), indent=2, ensure_ascii=False)
    except Exception as e:
        return f"(state unavailable: {e})"


def build_tasks(agents: dict[str, Agent], user_request: str, project_name: str) -> list[Task]:
    state_snapshot = _get_state_snapshot(project_name)

    t1 = Task(
        description=dedent(f"""\
            分析以下用户需求，输出结构化规格说明。

            USER REQUEST: {user_request}
            PROJECT: {project_name}

            ⚠️  project_name = "{project_name}" — 所有工具调用必须使用此值，不得自行修改。

            当前项目状态快照（已有模块/接口/方法）:
            {state_snapshot}

            步骤（严格按顺序，最多调用工具1次）:
            1. 调用 get_project_state(project_name="{project_name}") — 只调用一次
            2. 对照状态，将每个需求分类: create_new / add_to_existing / mixed
            3. 立即输出 JSON 规格（不再调用任何工具）:
            {{
              "request_type": "create_new|add_to_existing|mixed",
              "new_modules": [...],
              "modify_modules": [...],
              "new_interfaces": [...],
              "new_entities": [...],
              "new_controllers": [...],
              "notes": "..."
            }}
        """),
        expected_output="JSON specification with create_new and add_to_existing sections.",
        agent=agents["requirement_analyst"],
    )

    t2 = Task(
        description=dedent(f"""\
            审查需求分析结果，确定最终架构（project: {project_name}）。

            ⚠️  project_name = "{project_name}" — 所有工具调用必须使用此值。

            规则（最多调用工具1次）:
            1. 调用 get_project_state(project_name="{project_name}") — 只调用一次
            2. 从现有最大端口 +1 开始分配新端口。也就是从 8051 开始分配端口，每个新模块递增1。不得使用 8080/8081/8082。
            3. 立即输出架构 JSON（不再调用任何工具）:
            {{
              "group_id": "com.example",
              "version": "1.0.0-SNAPSHOT",
              "new_modules": [...],
              "incremental_changes": [...]
            }}
        """),
        expected_output="Finalized architecture JSON.",
        agent=agents["architect"],
        context=[t1],
    )

    t3 = Task(
        description=dedent(f"""\
            为 project '{project_name}' 生成【新建】部分的代码。
 
            ⚠️  project_name = "{project_name}" — 所有 skill 调用必须使用此值。
            ⚠️  参数错误（error_type=param_error）：修正后立即重调，不要跳过。
            ⚠️  already_exists：跳过，不要重试。
            ⚠️  运行时错误（final_error 无 error_type）：记录后继续下一项。
 
            【DTO 强制规则】
            在调用 generate_rest_controller 之前，必须检查：
            - 该模块所有 endpoint 的 param type
            - 该模块 Dubbo interface 所有 method 的 param type
            对每一个非 Java 基础类型、非 String 的类型（如 OrderDTO、UserDTO、
            LoginRequest、CreateOrderRequest 等），必须先调用 generate_dto 生成该类。
            DTO 所在模块 = 使用该类型的模块（OrderDTO 在 order-service，UserDTO 在 user-service）。
            跳过 generate_dto 会导致编译失败，这是强制要求。
 
            【Consumer 强制规则】
            如果某模块需要调用其他模块的 Dubbo 服务，调用 generate_rest_controller 时
            必须传入 dubbo_ref：
              dubbo_ref = {{
                "interface": "<provider 的 interface_fqn>",
                "field_name": "<controller 中的 camelCase 字段名>"
              }}
            skill 会自动将 provider 注入 consumer 的 pom.xml，无需额外操作。
 
            技能调用顺序（每个模块严格按序执行）:
              1. create_spring_boot_module（不传 port，从返回值读 assigned_port）
              2. generate_dubbo_interface（如有 Dubbo 服务）
              3. generate_entity（如有 JPA 实体）
              4. generate_dto（所有自定义类型，逐个调用，不可跳过）
              5. generate_rest_controller（consumer 模块必须带 dubbo_ref）
              6. add_threadpool_config（仅 has_threadpool=true）
 
            所有调用包含 project_name='{project_name}'。
        """),
        expected_output="List of created files or skip reasons per module.",
        agent=agents["code_generator"],
        context=[t1, t2],
    )

    t4 = Task(
        description=dedent(f"""\
            为 project '{project_name}' 执行【增量修改】。

            ⚠️  project_name = "{project_name}"。
            ⚠️  每个目标类只调用一次 get_project_state + 一次 incremental_modify。不要循环。

            工作流（每个需要修改的类）:
            1. get_project_state(project_name="{project_name}", class_fqn="<目标类FQN>")
               → 获取 known_methods
            2. 过滤出 requested_methods - known_methods = methods_to_add
            3. 若 methods_to_add 为空 → 输出 "All methods already present" 并跳过
            4. 否则调用 run_skill incremental_modify 一次，只包含 methods_to_add

            如果本次请求是纯新建（无增量），直接输出:
            "No incremental modifications required."
        """),
        expected_output="Report of incremental changes (added/skipped per method).",
        agent=agents["incremental_modifier"],
        context=[t1, t2, t3],
    )

    t5 = Task(
        description=dedent(f"""\
            更新 project '{project_name}' 的 docker-compose.yml。

            ⚠️  project_name = "{project_name}"。
            ⚠️  只调用一次 update_docker_compose，不要重复调用。

            步骤:
            1. 调用 run_skill(skill_name="update_docker_compose", params="{{}}", project_name="{project_name}")
            2. 确认返回中的 nacos_action（"existing"=复用 / "added"=新建）
            3. 输出启动命令（cd output/{project_name} && docker-compose up -d）
        """),
        expected_output="docker-compose.yml updated. Nacos reuse status confirmed. Run commands.",
        agent=agents["configuration_manager"],
        context=[t3],
    )

    t6 = Task(
        description=dedent(f"""\
            验证 project '{project_name}' 并输出最终报告。

            ⚠️  project_name = "{project_name}"。
            ⚠️  只调用一次 validate_project，不要因失败而重试。

            步骤:
            1. 调用 validate_project(project_name="{project_name}")
            2. 失败时分析 [ERROR] 行并给出建议（不重新编译）
            3. 输出完整交付报告:
               ✅ 新建模块: ...
               ✅ 增量修改: ...
               ✅ Docker: nacos 状态 + 所有服务
               📋 下一步:
                  cd output/{project_name}
                  docker-compose up -d
                  # Nacos: http://localhost:8848/nacos (nacos/nacos)
        """),
        expected_output="Build result + complete delivery report with run instructions.",
        agent=agents["validator"],
        context=[t3, t4, t5],
    )

    return [t1, t2, t3, t4, t5, t6]


def build_crew(user_request: str, project_name: str, llm_config: dict = None) -> Crew:
    agents = build_agents(llm_config)
    tasks = build_tasks(agents, user_request, project_name)
    return Crew(
        agents=list(agents.values()),
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
        memory=False,
    )


def run_crew(user_request: str, project_name: str, llm_config: dict = None) -> str:
    global CURRENT_PROJECT
    CURRENT_PROJECT = project_name
    try:
        crew = build_crew(user_request, project_name, llm_config)
        result = crew.kickoff()
        return str(result)
    finally:
        ContextRegistry.close_all()
        CURRENT_PROJECT = None