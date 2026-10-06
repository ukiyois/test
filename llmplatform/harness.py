from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any

from jsonschema import Draft202012Validator

from .diagnostics import exception_diagnostics
from .context import (
    COMPACTION_PREFIX,
    build_compaction_request,
    count_messages_tokens,
    find_compaction_cut,
    prune_tool_result,
    render_history_for_compaction,
    resolve_tokenizer,
)
from .service import complete_model, extract_local_tool_call, split_model_ref
from .storage import connect, json_dump, json_load, new_id, now_iso
from .tools import TOOL_CATALOG, execute_tool, infer_calculator_expression, tool_schemas
from .workflow_engine import (
    close_workflow_engine,
    initialize_workflow_engine,
    invoke_workflow,
)


MAX_NODES = 60
MAX_STEPS_PER_RUN = 100
MAX_AGENT_ITERATIONS = 8
MAX_SUBWORKFLOW_DEPTH = 5
# Agent 上下文管理：上下文预算（占模型 context_length 的比例）与压缩触发阈值
AGENT_CONTEXT_BUDGET_RATIO = 0.75
AGENT_COMPACTION_KEEP_RECENT = 6
AGENT_DEFAULT_CONTEXT_LENGTH = 8192
MAX_ACTIVE_ROOT_RUNS = 2
MAX_QUEUED_ROOT_RUNS = 100
RUN_TASKS: dict[str, asyncio.Task[Any]] = {}
RUN_SEMAPHORE: asyncio.Semaphore | None = None
_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_TEMPLATE_PATH_PART = re.compile(r"(?:\.([^.[\]]+))|\[(\d+|\"[^\"]*\"|'[^']*')\]")
_BUILTIN_TEMPLATE_NAMES = {"input", "last_output", "result", "error", "last_error", "files", "variables"}


class RunQueueFullError(RuntimeError):
    """Raised when the bounded Harness queue has reached its configured limit."""


class SubworkflowApprovalRequired(RuntimeError):
    """Suspend a parent run while a child workflow is waiting for review."""

    suspend_workflow = True

    def __init__(self, parent_node_id: str, pending: dict[str, Any]):
        self.parent_node_id = parent_node_id
        self.pending = pending
        super().__init__(pending.get("message") or "子工作流等待人工审核。")


DEFAULT_WORKFLOW = {
    "name": "文件研究员",
    "description": "搜索本轮上传文件，调用工具并整理一份有依据的回答。",
    "model": "local/qwen3.5-0.8b",
    "nodes": [
        {"id": "start", "type": "start", "label": "开始", "next": "agent"},
        {
            "id": "agent",
            "type": "agent",
            "label": "研究与工具调用",
            "model": "local/qwen3.5-0.8b",
            "system_prompt": "你是文件研究助手。优先搜索上传文件并引用文件名；需要计算时使用计算器。资料不足时明确说明。",
            "tools": ["semantic_search", "search_files", "calculator", "current_time"],
            "max_iterations": 5,
            "max_tokens": 512,
            "retries": 1,
            "timeout": 600,
            "next": "approval",
        },
        {
            "id": "approval",
            "type": "approval",
            "label": "人工审核",
            "message": "检查 Agent 的结果后再结束本次运行。",
            "on_approve": "end",
            "on_reject": "agent",
        },
        {"id": "end", "type": "end", "label": "结束"},
    ],
}


def _has_external_schema_ref(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str) and not item.startswith("#"):
                return True
            if _has_external_schema_ref(item):
                return True
    elif isinstance(value, list):
        return any(_has_external_schema_ref(item) for item in value)
    return False


def _workflow_template_reference_issues(nodes: list[Any]) -> list[str]:
    """Find template references that cannot be resolved from this workflow shape."""
    roots = _BUILTIN_TEMPLATE_NAMES | {
        str(node.get("id")) for node in nodes if isinstance(node, dict) and node.get("id")
    }
    issues: list[str] = []

    def node_name(node: dict[str, Any]) -> str:
        return str(node.get("label") or node.get("id") or "未命名节点")

    def check_reference(node: dict[str, Any], expression: str) -> None:
        expression = expression.strip()
        root = _template_root_for_reference(expression, roots)
        if root is None:
            issues.append(
                f"节点“{node_name(node)}”引用了未知模板变量 `{{{{{expression}}}}}`。"
            )
        elif _path_parts(expression[len(root):]) is None:
            issues.append(
                f"节点“{node_name(node)}”中的模板字段路径 `{{{{{expression}}}}}` 格式无效。"
            )

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        template_values: list[Any] = []
        if node_type == "tool":
            template_values.append(node.get("arguments", {}))
        elif node_type == "transform":
            template_values.append(node.get("template", ""))
        elif node_type == "subworkflow":
            template_values.append(node.get("input_template", "{{last_output}}"))
        elif node_type in {"condition", "loop"}:
            template_values.append(node.get("value", ""))
            variable = str(node.get("variable") or "last_output").strip()
            if variable not in roots:
                issues.append(f"节点“{node_name(node)}”选择了未知的条件变量“{variable}”。")
            else:
                variable_path = str(node.get("variable_path") or "").strip()
                suffix = "" if not variable_path else (
                    variable_path if variable_path.startswith("[") else "." + variable_path
                )
                if _path_parts(suffix) is None:
                    issues.append(f"节点“{node_name(node)}”的字段路径“{variable_path}”格式无效。")
        elif node_type == "parallel":
            branches = node.get("branches", [])
            if not isinstance(branches, list):
                branches = []
            for branch in branches:
                if not isinstance(branch, dict):
                    continue
                if branch.get("type") == "tool":
                    template_values.append(branch.get("arguments", {}))
                elif branch.get("type") == "transform":
                    template_values.append(branch.get("template", ""))
        for expression in _iter_template_expressions(template_values):
            check_reference(node, expression)
    return list(dict.fromkeys(issues))


def validate_workflow(
    definition: dict[str, Any], _snapshot_depth: int = 0
) -> dict[str, Any]:
    if _snapshot_depth > MAX_SUBWORKFLOW_DEPTH:
        raise ValueError(f"子工作流版本快照不能超过 {MAX_SUBWORKFLOW_DEPTH} 层嵌套。")
    if not isinstance(definition, dict):
        raise ValueError("工作流内容必须是对象。")
    nodes = definition.get("nodes")
    if not isinstance(nodes, list) or not nodes or len(nodes) > MAX_NODES:
        raise ValueError(f"工作流需要 1 到 {MAX_NODES} 个节点。")
    allowed_types = {"start", "agent", "llm", "tool", "condition", "loop", "parallel", "fanout", "fanin", "subworkflow", "approval", "transform", "end"}
    ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("节点必须是对象。")
        node_id = str(node.get("id", "")).strip()
        node_type = str(node.get("type", "")).strip()
        if not node_id or len(node_id) > 80 or node_id in ids:
            raise ValueError("节点 ID 不能为空、不能重复，且不能超过 80 字符。")
        if node_type not in allowed_types:
            raise ValueError(f"不支持的节点类型：{node_type}")
        ids.add(node_id)
    starts = [node for node in nodes if node["type"] == "start"]
    ends = [node for node in nodes if node["type"] == "end"]
    if len(starts) != 1:
        raise ValueError("工作流必须且只能有一个开始节点。")
    if len(ends) != 1:
        raise ValueError("工作流必须且只能有一个结束节点。")
    route_keys = {
        "start": ("next",),
        "agent": ("next",),
        "llm": ("next",),
        "tool": ("next",),
        "transform": ("next",),
        "condition": ("true_next", "false_next"),
        "loop": ("body_next", "done_next"),
        "parallel": ("next",),
        "fanout": (),
        "fanin": ("next",),
        "subworkflow": ("next",),
        "approval": ("on_approve", "on_reject"),
        "end": (),
    }
    error_route_types = {"agent", "llm", "tool", "transform", "condition", "loop", "parallel", "fanin", "subworkflow"}
    graph: dict[str, list[str]] = {}
    for node in nodes:
        required_routes = route_keys[node["type"]]
        for key in required_routes:
            target = str(node.get(key) or "").strip()
            if not target:
                raise ValueError(f"节点 {node['id']} 缺少必需连接：{key}。")
            if target not in ids:
                raise ValueError(f"节点 {node['id']} 的 {key} 指向不存在的节点 {target}。")
        graph[node["id"]] = [str(node[key]) for key in required_routes]
        if node["type"] == "fanout":
            branches = node.get("branches")
            if not isinstance(branches, list) or not 2 <= len(branches) <= 8:
                raise ValueError(f"Fan-out 节点 {node['id']} 需要配置 2 到 8 个分支入口。")
            if not str(node.get("fanin_id") or "").strip():
                raise ValueError(f"Fan-out 节点 {node['id']} 尚未选择对应的 Fan-in 汇合节点。")
            branch_ids: set[str] = set()
            branch_targets: list[str] = []
            for branch in branches:
                if not isinstance(branch, dict):
                    raise ValueError(f"Fan-out 节点 {node['id']} 的每个分支必须是对象。")
                branch_id = str(branch.get("id") or "").strip()
                target = str(branch.get("next") or "").strip()
                if not branch_id or len(branch_id) > 80 or branch_id in branch_ids:
                    raise ValueError(f"Fan-out 节点 {node['id']} 的分支 ID 不能为空、不能重复且不能超过 80 字符。")
                branch_ids.add(branch_id)
                if not target or target not in ids:
                    raise ValueError(f"Fan-out 分支 {branch_id} 必须连接到一个存在的节点。")
                branch_targets.append(target)
            graph[node["id"]] = branch_targets
            try:
                concurrency = int(node.get("max_concurrency", len(branches)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Fan-out 节点 {node['id']} 的并发数必须是整数。") from exc
            if not 1 <= concurrency <= len(branches):
                raise ValueError(f"Fan-out 节点 {node['id']} 的并发数必须在 1 到分支数之间。")
        if node["type"] == "fanin" and node.get("failure_policy", "fail") not in {"fail", "continue"}:
            raise ValueError(f"Fan-in 节点 {node['id']} 的失败策略只能是 fail 或 continue。")
        error_target = node.get("error_next")
        if error_target:
            if node["type"] not in error_route_types:
                raise ValueError(f"节点 {node['id']} 不支持错误出口。")
            error_target = str(error_target).strip()
            if not error_target or error_target == node["id"]:
                raise ValueError(f"节点 {node['id']} 的错误出口必须连接到其他节点。")
            if error_target not in ids:
                raise ValueError(f"节点 {node['id']} 的 error_next 指向不存在的节点 {error_target}。")
            graph[node["id"]].append(error_target)
        for key in ("next", "true_next", "false_next", "body_next", "done_next", "on_approve", "on_reject", "error_next"):
            target = node.get(key)
            if target and target not in ids:
                raise ValueError(f"节点 {node['id']} 的 {key} 指向不存在的节点 {target}。")
        if node["type"] == "tool" and node.get("tool") not in TOOL_CATALOG:
            raise ValueError(f"工具节点 {node['id']} 引用了未知工具。")
        if node["type"] == "agent":
            tools = node.get("tools", [])
            if not isinstance(tools, list) or any(tool not in TOOL_CATALOG for tool in tools):
                raise ValueError(f"Agent 节点 {node['id']} 包含未注册工具。")
        if node["type"] == "condition" and node.get("operator", "contains") not in {
            "contains", "equals", "not_equals", "is_empty", "is_not_empty"
        }:
            raise ValueError(f"条件节点 {node['id']} 使用了未知比较方式。")
        if node["type"] == "loop":
            if node.get("operator", "is_not_empty") not in {
                "contains", "equals", "not_equals", "is_empty", "is_not_empty"
            }:
                raise ValueError(f"循环节点 {node['id']} 使用了未知停止条件。")
            try:
                max_iterations = int(node.get("max_iterations", 5))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"循环节点 {node['id']} 的最大轮数必须是整数。") from exc
            if not 1 <= max_iterations <= 50:
                raise ValueError(f"循环节点 {node['id']} 的最大轮数必须在 1 到 50 之间。")
        if node["type"] == "parallel":
            branches = node.get("branches")
            if not isinstance(branches, list) or not 2 <= len(branches) <= 8:
                raise ValueError(f"并行节点 {node['id']} 需要配置 2 到 8 个并行分支。")
            branch_ids: set[str] = set()
            for branch in branches:
                if not isinstance(branch, dict):
                    raise ValueError(f"并行节点 {node['id']} 的每个分支必须是对象。")
                branch_id = str(branch.get("id", "")).strip()
                branch_type = str(branch.get("type", "")).strip()
                if not branch_id or branch_id in branch_ids:
                    raise ValueError(f"并行节点 {node['id']} 的分支 ID 不能为空或重复。")
                branch_ids.add(branch_id)
                if branch_type not in {"agent", "llm", "tool", "transform"}:
                    raise ValueError(f"并行分支 {branch_id} 只支持 Agent、模型、工具或文本整理。")
                try:
                    retries = int(branch.get("retries", 0) or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"并行分支 {branch_id} 的失败重试次数必须是整数。") from exc
                if not 0 <= retries <= 5:
                    raise ValueError(f"并行分支 {branch_id} 的失败重试次数必须在 0 到 5 之间。")
                try:
                    timeout_value = branch.get("timeout", 600)
                    timeout = int(600 if timeout_value is None else timeout_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"并行分支 {branch_id} 的超时秒数必须是整数。") from exc
                if not 1 <= timeout <= 3600:
                    raise ValueError(f"并行分支 {branch_id} 的超时秒数必须在 1 到 3600 之间。")
                if branch_type == "tool" and branch.get("tool") not in TOOL_CATALOG:
                    raise ValueError(f"并行分支 {branch_id} 引用了未知工具。")
                if branch_type == "tool" and not isinstance(branch.get("arguments", {}), dict):
                    raise ValueError(f"并行分支 {branch_id} 的工具参数必须是对象。")
                if branch_type == "agent":
                    branch_tools = branch.get("tools", [])
                    if not isinstance(branch_tools, list) or any(tool not in TOOL_CATALOG for tool in branch_tools):
                        raise ValueError(f"并行分支 {branch_id} 包含未注册工具。")
                if branch_type in {"agent", "llm"} and branch.get("output_schema") is not None:
                    schema = branch["output_schema"]
                    if not isinstance(schema, dict):
                        raise ValueError(f"并行分支 {branch_id} 的 JSON Schema 必须是对象。")
                    if len(json.dumps(schema, ensure_ascii=False)) > 16000:
                        raise ValueError(f"并行分支 {branch_id} 的 JSON Schema 不能超过 16 KB。")
                    if _has_external_schema_ref(schema):
                        raise ValueError(f"并行分支 {branch_id} 的 JSON Schema 只允许本地 # 引用。")
                    try:
                        Draft202012Validator.check_schema(schema)
                    except Exception as exc:
                        raise ValueError(f"并行分支 {branch_id} 的 JSON Schema 无效：{exc.message if hasattr(exc, 'message') else exc}") from exc
            try:
                concurrency = int(node.get("max_concurrency", len(branches)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"并行节点 {node['id']} 的并发数必须是整数。") from exc
            if not 1 <= concurrency <= min(8, len(branches)):
                raise ValueError(f"并行节点 {node['id']} 的并发数必须在 1 到分支数之间。")
        if node["type"] == "subworkflow":
            if not str(node.get("workflow_id") or "").strip():
                raise ValueError(f"子工作流节点 {node['id']} 尚未选择已发布 Agent。")
            snapshot = node.get("workflow_snapshot")
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("nodes"), list):
                raise ValueError(f"子工作流节点 {node['id']} 必须固定一个已发布 Agent 的版本快照。")
            if snapshot.get("id") and snapshot["id"] != node["workflow_id"]:
                raise ValueError(f"子工作流节点 {node['id']} 的版本快照与 Agent ID 不匹配。")
            validate_workflow(snapshot, _snapshot_depth + 1)
            try:
                if int(node.get("workflow_version") or 0) < 1:
                    raise ValueError("版本必须大于 0")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"子工作流节点 {node['id']} 缺少有效的已发布版本号。") from exc
        if node_type in {"llm", "agent"} and node.get("output_schema") is not None:
            schema = node["output_schema"]
            if not isinstance(schema, dict):
                raise ValueError(f"节点 {node['id']} 的 JSON Schema 必须是对象。")
            if len(json.dumps(schema, ensure_ascii=False)) > 16000:
                raise ValueError(f"节点 {node['id']} 的 JSON Schema 不能超过 16 KB。")
            if _has_external_schema_ref(schema):
                raise ValueError(f"节点 {node['id']} 的 JSON Schema 只允许本地 # 引用。")
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:
                raise ValueError(f"节点 {node['id']} 的 JSON Schema 无效：{exc.message if hasattr(exc, 'message') else exc}") from exc

    by_id = {node["id"]: node for node in nodes}
    fanouts = [node for node in nodes if node["type"] == "fanout"]
    fanins = [node for node in nodes if node["type"] == "fanin"]
    fanin_owners: dict[str, str] = {}
    for fanout in fanouts:
        fanin_id = str(fanout.get("fanin_id") or "").strip()
        fanin = by_id.get(fanin_id)
        if not fanin or fanin["type"] != "fanin":
            raise ValueError(f"Fan-out 节点 {fanout['id']} 必须连接到一个 Fan-in 节点。")
        if fanin.get("fanout_id") != fanout["id"]:
            raise ValueError(f"Fan-in 节点 {fanin_id} 必须反向指定 Fan-out 节点 {fanout['id']}。")
        if fanin_id in fanin_owners:
            raise ValueError(f"Fan-in 节点 {fanin_id} 只能汇合一个 Fan-out。")
        fanin_owners[fanin_id] = fanout["id"]
    for fanin in fanins:
        if fanin_owners.get(fanin["id"]) != fanin.get("fanout_id"):
            raise ValueError(f"Fan-in 节点 {fanin['id']} 尚未与唯一的 Fan-out 节点正确配对。")

    branch_owners: dict[str, tuple[str, str]] = {}
    for fanout in fanouts:
        paired_fanin = str(fanout["fanin_id"])
        for branch in fanout["branches"]:
            branch_id = str(branch["id"])
            region: set[str] = set()
            pending_nodes = [str(branch["next"])]
            while pending_nodes:
                current_id = pending_nodes.pop()
                if current_id == paired_fanin or current_id in region:
                    continue
                current = by_id[current_id]
                if current["type"] in {"start", "end", "fanout", "fanin"}:
                    raise ValueError(
                        f"Fan-out 分支 {branch_id} 不能嵌套开始、结束、Fan-out 或 Fan-in 节点，必须通过审批、子工作流及普通执行节点到达其配对的 Fan-in {paired_fanin}。"
                    )
                region.add(current_id)
                pending_nodes.extend(graph[current_id])

            for current_id in region:
                owner = branch_owners.get(current_id)
                if owner and owner != (fanout["id"], branch_id):
                    raise ValueError(f"工作流节点 {current_id} 被多个 Fan-out 分支共用，分支状态无法隔离。")
                branch_owners[current_id] = (fanout["id"], branch_id)

            can_reach_join = {paired_fanin}
            changed = True
            while changed:
                changed = False
                for current_id in region - can_reach_join:
                    if any(target in can_reach_join for target in graph[current_id]):
                        can_reach_join.add(current_id)
                        changed = True
            dead_branch_nodes = region - can_reach_join
            if dead_branch_nodes:
                raise ValueError(
                    f"Fan-out 分支 {branch_id} 中这些节点没有路径到达汇合点 {paired_fanin}："
                    + "、".join(sorted(dead_branch_nodes))
                )

    # The post-join workflow executes once. Branch-local nodes must not also
    # be reachable from the main path, or concurrent writes would be ambiguous.
    main_reachable: set[str] = set()
    main_pending = [starts[0]["id"]]
    while main_pending:
        current_id = main_pending.pop()
        if current_id in main_reachable:
            continue
        main_reachable.add(current_id)
        current = by_id[current_id]
        if current["type"] == "fanout":
            main_pending.append(str(current["fanin_id"]))
        else:
            main_pending.extend(graph[current_id])
    shared_with_main = set(branch_owners) & main_reachable
    if shared_with_main:
        raise ValueError("Fan-out 分支节点也可从主流程直接到达，需保持分支内部路径独立：" + "、".join(sorted(shared_with_main)))

    # Catch visually disconnected nodes before they become opaque runtime failures.
    reachable: set[str] = set()
    queue = [starts[0]["id"]]
    while queue:
        current = queue.pop()
        if current in reachable:
            continue
        reachable.add(current)
        queue.extend(graph[current])
    unreachable = ids - reachable
    if unreachable:
        raise ValueError("以下节点从开始节点无法到达：" + "、".join(sorted(unreachable)))

    reverse: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for source, targets in graph.items():
        for target in targets:
            reverse[target].append(source)
    can_finish: set[str] = set()
    queue = [ends[0]["id"]]
    while queue:
        current = queue.pop()
        if current in can_finish:
            continue
        can_finish.add(current)
        queue.extend(reverse[current])
    dead_ends = ids - can_finish
    if dead_ends:
        raise ValueError("以下节点没有任何可达结束路径：" + "、".join(sorted(dead_ends)))
    template_issues = _workflow_template_reference_issues(nodes)
    if template_issues:
        raise ValueError(template_issues[0])
    return definition


def workflow_diagnostics(definition: dict[str, Any]) -> dict[str, Any]:
    """Return editor-friendly validation details without mutating the draft."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        validate_workflow(definition)
    except (TypeError, ValueError) as exc:
        errors.append(str(exc))
    nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
    if not isinstance(nodes, list):
        nodes = []
    node_count = len(nodes)
    edge_count = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type in {"condition", "approval"}:
            edge_count += sum(bool(node.get(key)) for key in ("true_next", "false_next", "on_approve", "on_reject"))
        elif node_type == "loop":
            edge_count += sum(bool(node.get(key)) for key in ("body_next", "done_next"))
        elif node_type == "fanout":
            branches = node.get("branches", [])
            if isinstance(branches, list):
                edge_count += sum(bool(branch.get("next")) for branch in branches if isinstance(branch, dict))
        elif node_type != "end":
            edge_count += bool(node.get("next"))
        edge_count += bool(node.get("error_next"))
        if node_type == "agent" and not node.get("tools"):
            warnings.append(f"Agent 节点“{node.get('label') or node.get('id')}”没有可用工具，只能直接生成文本。")
        if node_type == "tool" and node.get("tool") == "http_request":
            try:
                retries = int(node.get("retries", 0) or 0)
            except (TypeError, ValueError):
                retries = 0
            if retries > 0:
                warnings.append(f"HTTP 节点“{node.get('label') or node.get('id')}”启用了重试；POST 请求失败后可能会重复提交。")
    errors.extend(_workflow_template_reference_issues(nodes))
    if node_count > 40:
        warnings.append("流程超过 40 个节点，建议拆分为可复用的子流程，方便调试和维护。")
    return {
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "node_count": node_count,
        "connection_count": edge_count,
    }


def ensure_default_workflow() -> None:
    with connect() as db:
        row = db.execute("SELECT id FROM workflows LIMIT 1").fetchone()
        if row:
            return
        workflow_id = new_id("flow_")
        now = now_iso()
        definition = {**DEFAULT_WORKFLOW, "id": workflow_id}
        db.execute(
            "INSERT INTO workflows(id,name,description,definition,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                workflow_id,
                definition["name"],
                definition["description"],
                json_dump(definition),
                1,
                now,
                now,
            ),
        )


def list_workflows() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM workflows ORDER BY updated_at DESC").fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "definition": json_load(row["definition"], {}),
            "version": row["version"],
            "published_version": row["published_version"],
            "published_definition": json_load(row["published_definition"], {}),
            "published_at": row["published_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def get_workflow(workflow_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "definition": json_load(row["definition"], {}),
        "version": row["version"],
        "published_version": row["published_version"],
        "published_definition": json_load(row["published_definition"], {}),
        "published_at": row["published_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_workflow(
    definition: dict[str, Any], workflow_id: str | None = None
) -> dict[str, Any]:
    definition = validate_workflow(definition)
    workflow_id = workflow_id or new_id("flow_")
    definition = {**definition, "id": workflow_id}
    current = get_workflow(workflow_id)
    now = now_iso()
    version = int(current.get("version", 1)) + 1 if current else 1
    with connect() as db:
        db.execute(
            """
            INSERT INTO workflows(id,name,description,definition,version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, description=excluded.description,
              definition=excluded.definition, version=excluded.version,
              updated_at=excluded.updated_at
            """,
            (
                workflow_id,
                str(definition.get("name") or "未命名工作流").strip(),
                str(definition.get("description") or ""),
                json_dump(definition),
                version,
                current["created_at"] if current else now,
                now,
            ),
        )
    return get_workflow(workflow_id) or {}


def publish_workflow(workflow_id: str) -> dict[str, Any] | None:
    workflow = get_workflow(workflow_id)
    if not workflow:
        return None
    validate_workflow(workflow["definition"])
    now = now_iso()
    with connect() as db:
        db.execute(
            "UPDATE workflows SET published_version=?, published_definition=?, published_at=? WHERE id=?",
            (int(workflow["version"]), json_dump(workflow["definition"]), now, workflow_id),
        )
    return get_workflow(workflow_id)


def unpublish_workflow(workflow_id: str) -> dict[str, Any] | None:
    workflow = get_workflow(workflow_id)
    if not workflow:
        return None
    with connect() as db:
        db.execute(
            "UPDATE workflows SET published_version=0, published_definition='{}', published_at=NULL WHERE id=?",
            (workflow_id,),
        )
    return get_workflow(workflow_id)


def create_run(
    workflow_id: str,
    user_input: str,
    file_ids: list[str],
    definition_snapshot: dict[str, Any] | None = None,
    workflow_version: int | None = None,
    execution_context: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
    parent_node_id: str | None = None,
    parent_invocation: int | None = None,
) -> str:
    workflow = get_workflow(workflow_id)
    if not workflow:
        raise ValueError("工作流不存在。")
    definition = definition_snapshot or workflow["definition"]
    validate_workflow(definition)
    nodes = definition["nodes"]
    start = next(node for node in nodes if node["type"] == "start")
    state = {
        "input": user_input,
        "file_ids": file_ids,
        "files": _load_run_files(file_ids),
        "variables": {},
        "last_output": user_input,
        "result": None,
        "step_count": 0,
        "approval": None,
        "_subworkflow_depth": 0,
        "_subworkflow_stack": [workflow_id],
    }
    if execution_context:
        state.update(execution_context)
    run_id = new_id("run_")
    now = now_iso()
    with connect() as db:
        if parent_run_id is None:
            queued = db.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE status='queued' AND parent_run_id IS NULL"
            ).fetchone()["count"]
            if int(queued) >= MAX_QUEUED_ROOT_RUNS:
                raise RunQueueFullError(
                    f"Harness 队列已满（最多等待 {MAX_QUEUED_ROOT_RUNS} 个任务），请稍后重试。"
                )
        db.execute(
            """
            INSERT INTO runs(
                id,workflow_id,parent_run_id,parent_node_id,parent_invocation,workflow_version,workflow_snapshot,status,input,state,
                current_node,pending,error,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                workflow_id,
                parent_run_id,
                parent_node_id,
                parent_invocation,
                int(workflow_version if workflow_version is not None else workflow.get("version", 1)),
                json_dump(definition),
                "queued",
                json_dump({"input": user_input, "file_ids": file_ids}),
                json_dump(state),
                start["id"],
                None,
                None,
                now,
                now,
            ),
        )
    record_event(run_id, start["id"], "run_queued", {"input": user_input})
    return run_id


def _load_run_files(file_ids: list[str]) -> list[dict[str, Any]]:
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    with connect() as db:
        rows = db.execute(
            f"SELECT id,filename,extracted_text FROM files WHERE id IN ({placeholders})",
            tuple(file_ids),
        ).fetchall()
    return [
        {"id": row["id"], "filename": row["filename"], "text": row["extracted_text"]}
        for row in rows
    ]


def record_event(
    run_id: str, node_id: str | None, event_type: str, payload: Any
) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO run_events(run_id,node_id,event_type,payload,created_at) VALUES(?,?,?,?,?)",
            (run_id, node_id, event_type, json_dump(payload), now_iso()),
        )
    _publish(
        run_id,
        "event",
        {
            "node_id": node_id,
            "type": event_type,
            "payload": payload,
            "created_at": now_iso(),
        },
    )


# --- Live run streaming -------------------------------------------------
# Subscribers are asyncio queues bound to the FastAPI event loop; record_event
# and _save_run run in worker threads, so publishing hops through
# loop.call_soon_threadsafe. Queues are bounded and drops are allowed: the
# persisted run_events table remains the source of truth.
_RUN_SUBSCRIBERS: dict[str, set[asyncio.Queue[Any]]] = {}
_SUBSCRIBER_LOCK = threading.Lock()


def subscribe_run(run_id: str) -> tuple[asyncio.Queue[Any], asyncio.AbstractEventLoop]:
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=500)
    loop = asyncio.get_running_loop()
    with _SUBSCRIBER_LOCK:
        _RUN_SUBSCRIBERS.setdefault(run_id, set()).add(queue)
    return queue, loop


def unsubscribe_run(run_id: str, queue: asyncio.Queue[Any]) -> None:
    with _SUBSCRIBER_LOCK:
        subscribers = _RUN_SUBSCRIBERS.get(run_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            _RUN_SUBSCRIBERS.pop(run_id, None)


def _publish(run_id: str, kind: str, data: dict[str, Any]) -> None:
    with _SUBSCRIBER_LOCK:
        subscribers = list(_RUN_SUBSCRIBERS.get(run_id, ()))
    if not subscribers:
        return
    message = {"kind": kind, "run_id": run_id, **data}
    for queue in subscribers:
        try:
            loop = queue.get_loop()
        except RuntimeError:
            continue

        def _deliver(target: asyncio.Queue[Any] = queue) -> None:
            try:
                target.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer: drop oldest to stay fresh.
                try:
                    target.get_nowait()
                    target.put_nowait(message)
                except Exception:
                    pass

        try:
            loop.call_soon_threadsafe(_deliver)
        except RuntimeError:
            pass


def get_run(run_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        events = db.execute(
            "SELECT * FROM run_events WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        children = db.execute(
            "SELECT id,workflow_id,parent_node_id,parent_invocation,workflow_version,status,current_node,error,created_at,finished_at FROM runs WHERE parent_run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
    result = dict(row)
    result["input"] = json_load(result["input"], {})
    result["state"] = json_load(result["state"], {})
    result["pending"] = json_load(result["pending"], None)
    result["workflow_snapshot"] = json_load(result.get("workflow_snapshot"), {})
    result["children"] = [dict(child) for child in children]
    if not result["workflow_snapshot"]:
        # Compatibility for runs created before immutable snapshots existed.
        workflow = get_workflow(result["workflow_id"])
        if workflow:
            result["workflow_snapshot"] = workflow["definition"]
            result["workflow_version"] = workflow["version"]
    result["events"] = [
        {
            "id": item["id"],
            "node_id": item["node_id"],
            "type": item["event_type"],
            "payload": json_load(item["payload"], {}),
            "created_at": item["created_at"],
        }
        for item in events
    ]
    return result


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT id,workflow_id,parent_run_id,parent_node_id,parent_invocation,workflow_version,status,input,current_node,error,started_at,finished_at,created_at,updated_at FROM runs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [
        {
            **dict(row),
            "input": json_load(row["input"], {}),
        }
        for row in rows
    ]


def _save_run(
    run_id: str,
    state: dict[str, Any],
    current_node: str | None,
    status: str = "running",
    pending: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    now = now_iso()
    finished = status in {"completed", "failed", "cancelled"}
    with connect() as db:
        db.execute(
            """
            UPDATE runs SET state=?,current_node=?,status=?,pending=?,error=?,updated_at=?,
                started_at=CASE WHEN ?='running' THEN COALESCE(started_at,?) ELSE started_at END,
                finished_at=CASE WHEN ?='queued' THEN NULL WHEN ? THEN COALESCE(finished_at,?) ELSE finished_at END
            WHERE id=?
            """,
            (
                json_dump(state),
                current_node,
                status,
                json_dump(pending) if pending else None,
                error,
                now,
                status,
                now,
                status,
                finished,
                now,
                run_id,
            ),
        )
    _publish(
        run_id,
        "status",
        {
            "status": status,
            "current_node": current_node,
            "pending": pending,
            "error": error,
        },
    )


def _get_run_semaphore() -> asyncio.Semaphore:
    global RUN_SEMAPHORE
    if RUN_SEMAPHORE is None:
        RUN_SEMAPHORE = asyncio.Semaphore(MAX_ACTIVE_ROOT_RUNS)
    return RUN_SEMAPHORE


def _finish_queued_cancellation(run_id: str) -> None:
    run = get_run(run_id)
    if not run or run["status"] not in {"queued", "cancel_requested"}:
        return
    _save_run(
        run_id,
        run["state"],
        run["current_node"],
        status="cancelled",
        error=None,
    )
    record_event(run_id, run["current_node"], "run_cancelled", {})


async def _run_worker(run_id: str, bypass_concurrency: bool) -> None:
    semaphore = None if bypass_concurrency else _get_run_semaphore()
    acquired = False
    try:
        if semaphore is not None:
            await semaphore.acquire()
            acquired = True
        run = get_run(run_id)
        if not run:
            return
        if run["status"] == "cancel_requested":
            _finish_queued_cancellation(run_id)
            return
        if run["status"] != "queued":
            return
        await execute_run(run_id)
    except asyncio.CancelledError:
        # A worker waiting for capacity has not entered execute_run, so it
        # must finish the persisted queue row itself when cancelled.
        _finish_queued_cancellation(run_id)
    finally:
        if acquired and semaphore is not None:
            semaphore.release()
        if RUN_TASKS.get(run_id) is asyncio.current_task():
            RUN_TASKS.pop(run_id, None)


def start_run_task(run_id: str, *, bypass_concurrency: bool = False) -> None:
    current = RUN_TASKS.get(run_id)
    if current and not current.done():
        return
    RUN_TASKS[run_id] = asyncio.create_task(_run_worker(run_id, bypass_concurrency))


async def recover_runs() -> None:
    with connect() as db:
        rows = db.execute(
            """
            SELECT id,current_node,state,status FROM runs
            WHERE status IN ('running','cancel_requested')
               OR (status='queued' AND parent_run_id IS NOT NULL)
            """
        ).fetchall()
    for row in rows:
        state = json_load(row["state"], {})
        _save_run(
            row["id"],
            state,
            row["current_node"],
            status="interrupted",
            error="服务在运行期间重启；检查执行记录后可继续。",
        )
        record_event(row["id"], row["current_node"], "run_interrupted", {})
    # Queued jobs are safe to start again: no node has executed, and their
    # immutable workflow snapshot is already stored with the run.
    with connect() as db:
        queued_roots = db.execute(
            "SELECT id FROM runs WHERE status='queued' AND parent_run_id IS NULL ORDER BY created_at,id"
        ).fetchall()
    for row in queued_roots:
        start_run_task(row["id"])


async def resume_run(
    run_id: str,
    approved: bool,
    note: str = "",
    *,
    bypass_concurrency: bool = False,
) -> None:
    run = get_run(run_id)
    if not run or run["status"] not in {"waiting_approval", "interrupted", "failed"}:
        raise ValueError("此运行当前不能恢复。")
    definition = run.get("workflow_snapshot") or {}
    if not definition:
        workflow = get_workflow(run["workflow_id"])
        definition = workflow["definition"] if workflow else {}
    nodes = {node["id"]: node for node in definition.get("nodes", [])}
    state = run["state"]
    pending = run["pending"]
    current = run["current_node"]
    if pending and pending.get("type") == "approval":
        node = nodes.get(pending["node_id"])
        if not node:
            raise ValueError("运行快照中找不到待审核节点，无法继续恢复。")
        state["approval"] = {"approved": approved, "note": note}
        state.setdefault("variables", {})[node["id"]] = state["approval"]
        state.setdefault("review_notes", []).append(
            {"node_id": node["id"], "approved": approved, "note": note, "created_at": now_iso()}
        )
        state["step_count"] = int(state.get("step_count", 0)) + 1
        current = node.get("on_approve" if approved else "on_reject")
        state["_resume_node"] = current
        state["_resume_payload"] = {"approved": approved, "note": note}
        state["_resume_approval_node"] = node["id"]
        record_event(
            run_id,
            node["id"],
            "approval_resolved",
            {"approved": approved, "note": note},
        )
    elif pending and pending.get("type") in {
        "branch_approval",
        "branch_subworkflow_approval",
    }:
        node_id = str(pending.get("node_id") or "")
        node = nodes.get(node_id)
        interrupt_id = str(pending.get("interrupt_id") or "")
        if not interrupt_id:
            raise ValueError("分支审批缺少 checkpoint 中断 ID，无法安全恢复。")
        if pending["type"] == "branch_approval":
            if not node or node.get("type") != "approval":
                raise ValueError("运行快照中找不到分支审批节点。")
        else:
            child_id = str(pending.get("child_run_id") or "")
            child = get_run(child_id) if child_id else None
            if not node or node.get("type") != "subworkflow":
                raise ValueError("运行快照中找不到分支子工作流节点。")
            if not child or child.get("parent_run_id") != run_id or child.get("parent_node_id") != node_id:
                raise ValueError("分支审批引用的子运行与父运行不匹配。")
            if child["status"] != "waiting_approval" or not child.get("pending"):
                raise ValueError("子运行已不在等待审批状态；请刷新运行记录后重试。")
            await resume_run(child_id, approved, note, bypass_concurrency=True)
        decision = {"approved": bool(approved), "note": str(note or "")[:5000]}
        state["_resume_interrupt"] = {
            "interrupt_id": interrupt_id,
            "value": decision,
        }
        record_event(
            run_id,
            node_id,
            "approval_resolved",
            {
                **decision,
                "branch_id": pending.get("branch_id"),
                "fanout_id": pending.get("fanout_id"),
                "child_run_id": pending.get("child_run_id"),
            },
        )
    elif pending and pending.get("type") == "subworkflow_approval":
        node_id = str(pending.get("node_id") or "")
        node = nodes.get(node_id)
        child_id = str(pending.get("child_run_id") or "")
        child = get_run(child_id) if child_id else None
        if not node or node.get("type") != "subworkflow" or node_id != current:
            raise ValueError("运行快照中找不到等待审批的子工作流节点。")
        if not child or child.get("parent_run_id") != run_id or child.get("parent_node_id") != node_id:
            raise ValueError("待审批的子运行与当前父运行不匹配。")
        if child["status"] != "waiting_approval" or not child.get("pending"):
            raise ValueError("子运行已不在等待审批状态；请刷新运行记录后重试。")
        # Resolve the innermost pending approval first. For nested subworkflows,
        # resume_run delegates recursively until it reaches the actual approval node.
        await resume_run(child_id, approved, note, bypass_concurrency=True)
        state["_resume_node"] = node_id
        record_event(
            run_id,
            node_id,
            "approval_resolved",
            {
                "approved": approved,
                "note": note,
                "child_run_id": child_id,
                "child_node_id": pending.get("child_node_id"),
            },
        )
    else:
        state["_resume_node"] = current or next(
            node["id"] for node in definition.get("nodes", []) if node["type"] == "start"
        )
    _save_run(run_id, state, current, status="queued", pending=None, error=None)
    record_event(run_id, current, "run_resumed", {"approved": approved})
    start_run_task(run_id, bypass_concurrency=bypass_concurrency)


async def cancel_run(run_id: str, *, _from_parent: bool = False) -> bool:
    run = get_run(run_id)
    if not run or run["status"] not in {"queued", "running", "waiting_approval"}:
        return False
    if run.get("parent_run_id") and not _from_parent:
        parent = get_run(run["parent_run_id"])
        if parent and parent["status"] in {"queued", "running", "waiting_approval"}:
            return False

    if run["status"] == "waiting_approval":
        # A paused graph has no worker to receive cancellation. Close its child
        # runs explicitly, then make this checkpoint terminal.
        for child in run.get("children", []):
            if child["status"] in {"queued", "running", "waiting_approval"}:
                await cancel_run(child["id"], _from_parent=True)
        fresh = get_run(run_id) or run
        state = fresh["state"]
        state["cancel_requested"] = True
        _save_run(run_id, state, fresh["current_node"], status="cancelled", pending=None, error=None)
        record_event(run_id, fresh["current_node"], "run_cancelled", {})
        return True

    state = run["state"]
    state["cancel_requested"] = True
    _save_run(
        run_id,
        state,
        run["current_node"],
        status="cancel_requested",
        error=None,
    )
    record_event(run_id, run["current_node"], "cancel_requested", {})
    task = RUN_TASKS.get(run_id)
    if task and not task.done():
        task.cancel()
    else:
        _finish_queued_cancellation(run_id)
    for child in run.get("children", []):
        if child["status"] in {"queued", "running", "waiting_approval"}:
            await cancel_run(child["id"], _from_parent=True)
    return True


def _iter_template_expressions(value: Any):
    if isinstance(value, str):
        yield from _TEMPLATE_PATTERN.findall(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_template_expressions(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_template_expressions(item)


def _template_root_for_reference(expression: str, roots: set[str]) -> str | None:
    for root in sorted(roots, key=len, reverse=True):
        if expression == root or expression.startswith(root + ".") or expression.startswith(root + "["):
            return root
    return None


def _template_roots(state: dict[str, Any]) -> dict[str, Any]:
    variables = state.get("variables")
    if not isinstance(variables, dict):
        variables = {}
    files = [
        {"filename": item.get("filename", ""), "text": str(item.get("text") or "")[:8000]}
        for item in state.get("files", [])
        if isinstance(item, dict)
    ]
    roots = {
        "input": state.get("input", ""),
        "last_output": state.get("last_output", ""),
        "result": state.get("result", ""),
        "error": state.get("last_error", ""),
        "last_error": state.get("last_error", ""),
        "files": files,
        "variables": variables,
    }
    for name, item in variables.items():
        key = str(name)
        if key not in roots:
            roots[key] = item
    return roots


def _path_parts(suffix: str) -> list[str | int] | None:
    parts: list[str | int] = []
    position = 0
    while position < len(suffix):
        match = _TEMPLATE_PATH_PART.match(suffix, position)
        if not match:
            return None
        if match.group(1) is not None:
            part: str | int = match.group(1).strip()
            if not part:
                return None
        else:
            bracket = match.group(2)
            if bracket[0] in "'\"":
                part = bracket[1:-1]
            else:
                part = int(bracket)
        parts.append(part)
        position = match.end()
    return parts


def _resolve_template_expression(expression: str, state: dict[str, Any]) -> tuple[bool, Any]:
    roots = _template_roots(state)
    root_name = _template_root_for_reference(expression, set(roots))
    if root_name is None:
        return False, None
    suffix = expression[len(root_name):]
    parts = _path_parts(suffix)
    if parts is None:
        return False, None
    value = roots[root_name]
    for part in parts:
        if isinstance(value, dict):
            key = str(part)
            if key not in value:
                return False, None
            value = value[key]
        elif isinstance(value, (list, tuple)) and isinstance(part, int):
            if not 0 <= part < len(value):
                return False, None
            value = value[part]
        else:
            return False, None
    return True, value


def _render(value: Any, state: dict[str, Any], *, preserve_types: bool = False, strict: bool = False) -> Any:
    if isinstance(value, dict):
        return {key: _render(item, state, preserve_types=preserve_types, strict=strict) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, state, preserve_types=preserve_types, strict=strict) for item in value]
    if not isinstance(value, str):
        return value

    def template_value(item: Any) -> str:
        if isinstance(item, (dict, list, bool)) or item is None:
            return json.dumps(item, ensure_ascii=False)
        return str(item)

    full_reference = _TEMPLATE_PATTERN.fullmatch(value)
    if preserve_types and full_reference:
        expression = full_reference.group(1).strip()
        found, result = _resolve_template_expression(expression, state)
        if found:
            return result
        if strict:
            raise ValueError("模板变量 {{" + expression + "}} 无法解析。")

    def substitute(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        found, result = _resolve_template_expression(expression, state)
        if not found:
            if strict:
                raise ValueError("模板变量 {{" + expression + "}} 无法解析。")
            return match.group(0)
        return template_value(result)

    return _TEMPLATE_PATTERN.sub(substitute, value)


async def _execute_parallel(
    run_id: str,
    node: dict[str, Any],
    state: dict[str, Any],
    workflow: dict[str, Any],
) -> dict[str, Any]:
    branches = node["branches"]
    semaphore = asyncio.Semaphore(max(1, min(int(node.get("max_concurrency", len(branches))), len(branches))))

    async def run_branch(branch: dict[str, Any]) -> tuple[str, Any]:
        branch_id = str(branch["id"])
        trace_id = f"{node['id']}.{branch_id}"
        branch_node = {**branch, "id": trace_id, "label": branch.get("label") or branch_id}
        retries = max(0, min(int(branch.get("retries", 0) or 0), 5))
        timeout = max(1, min(int(branch.get("timeout", 600)), 3600))
        record_event(run_id, node["id"], "parallel_branch_started", {"branch_id": branch_id, "type": branch["type"], "max_attempts": retries + 1})
        started = time.monotonic()
        for attempt in range(retries + 1):
            try:
                async with semaphore:
                    result = await asyncio.wait_for(
                        _execute_node(run_id, branch_node, dict(state), workflow),
                        timeout=timeout,
                    )
                record_event(run_id, node["id"], "parallel_branch_completed", {"branch_id": branch_id, "output": result, "attempts": attempt + 1, "duration_ms": int((time.monotonic() - started) * 1000)})
                return branch_id, result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = f"执行超过 {timeout} 秒。" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else (str(exc).strip() or type(exc).__name__)
                record_event(
                    run_id,
                    trace_id,
                    "node_attempt_failed",
                    {
                        "branch_id": branch_id,
                        "attempt": attempt + 1,
                        "max_attempts": retries + 1,
                        "error": message[:1000],
                        "diagnostic": exception_diagnostics(exc),
                    },
                )
                if attempt >= retries:
                    record_event(run_id, node["id"], "parallel_branch_failed", {"branch_id": branch_id, "attempts": attempt + 1, "error": message[:1000], "duration_ms": int((time.monotonic() - started) * 1000)})
                    raise RuntimeError(f"并行分支 {branch_id} 重试 {retries} 次后失败：{message}") from exc

    results = await asyncio.gather(*(run_branch(branch) for branch in branches), return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        messages = [str(error) for error in failures[:3]]
        extra = f"（另有 {len(failures) - 3} 个分支失败）" if len(failures) > 3 else ""
        raise RuntimeError("并行分支失败：" + "；".join(messages) + extra)
    return {branch_id: result for branch_id, result in results}


async def _execute_subworkflow(
    run_id: str,
    node: dict[str, Any],
    state: dict[str, Any],
    workflow: dict[str, Any],
) -> Any:
    workflow_id = str(node.get("workflow_id") or "")
    snapshot = node.get("workflow_snapshot")
    selected = get_workflow(workflow_id)
    if not selected:
        raise ValueError(f"子工作流 {workflow_id or '(未选择)'} 不存在。")
    if snapshot is None:
        if not selected.get("published_version"):
            raise ValueError(f"子工作流“{selected['name']}”尚未发布。")
        snapshot = selected.get("published_definition") or {}
        version = int(selected["published_version"])
    else:
        version = int(node.get("workflow_version") or selected.get("published_version") or 1)
    validate_workflow(snapshot)
    stack = list(state.get("_subworkflow_stack") or [])
    parent_workflow_id = str(workflow.get("id") or "")
    if parent_workflow_id and parent_workflow_id not in stack:
        stack.append(parent_workflow_id)
    if workflow_id in stack:
        raise ValueError("检测到子工作流递归调用：" + " → ".join(stack + [workflow_id]))
    depth = int(state.get("_subworkflow_depth", 0))
    if depth >= MAX_SUBWORKFLOW_DEPTH:
        raise ValueError(f"子工作流嵌套超过 {MAX_SUBWORKFLOW_DEPTH} 层，已停止以保护运行资源。")

    child_input = _render(node.get("input_template", "{{last_output}}"), state, strict=True)
    if isinstance(child_input, (dict, list)):
        child_input = json.dumps(child_input, ensure_ascii=False)
    else:
        child_input = str(child_input or "")
    invocation = int((state.get("node_visits") or {}).get(node["id"], 0))
    parent = get_run(run_id)
    prior = next(
        (
            child
            for child in reversed((parent or {}).get("children", []))
            if child.get("parent_node_id") == node["id"]
            and child.get("parent_invocation") == invocation
            and child.get("workflow_id") == workflow_id
            and int(child.get("workflow_version") or 1) == version
        ),
        None,
    )
    child_id = prior["id"] if prior else None
    if prior and prior["status"] == "completed":
        child = get_run(child_id)
        result = child["state"].get("result", child["state"].get("last_output")) if child else None
        record_event(
            run_id,
            node["id"],
            "subworkflow_reused",
            {"child_run_id": child_id, "workflow_id": workflow_id, "workflow_version": version},
        )
        return result

    if not child_id or prior["status"] == "cancelled":
        child_id = create_run(
            workflow_id,
            child_input,
            list(state.get("file_ids", [])),
            definition_snapshot=snapshot,
            workflow_version=version,
            execution_context={
                "_subworkflow_depth": depth + 1,
                "_subworkflow_stack": stack + [workflow_id],
            },
            parent_run_id=run_id,
            parent_node_id=node["id"],
            parent_invocation=invocation,
        )
    record_event(
        run_id,
        node["id"],
        "subworkflow_started",
        {
            "child_run_id": child_id,
            "workflow_id": workflow_id,
            "workflow_name": selected["name"],
            "workflow_version": version,
            "resumed": bool(prior),
        },
    )
    if prior and prior["status"] in {"failed", "interrupted"}:
        await resume_run(
            child_id,
            False,
            "由父工作流恢复子 Agent。",
            bypass_concurrency=True,
        )
    elif prior and prior["status"] == "running":
        task = RUN_TASKS.get(child_id)
        if task and not task.done():
            pass
        else:
            stale_child = get_run(child_id)
            if stale_child:
                _save_run(
                    child_id,
                    stale_child["state"],
                    stale_child["current_node"],
                    status="interrupted",
                    error="子 Agent 运行器已退出；父工作流正在从 checkpoint 恢复。",
                )
                record_event(child_id, stale_child["current_node"], "run_interrupted", {})
            await resume_run(
                child_id,
                False,
                "由父工作流恢复子 Agent。",
                bypass_concurrency=True,
            )
    else:
        start_run_task(child_id, bypass_concurrency=True)
    try:
        while True:
            child = get_run(child_id)
            if not child:
                raise RuntimeError(f"子工作流运行记录 {child_id} 丢失。")
            if child["status"] == "completed":
                result = child["state"].get("result", child["state"].get("last_output"))
                record_event(run_id, node["id"], "subworkflow_completed", {"child_run_id": child_id, "output": result})
                return result
            if child["status"] == "waiting_approval":
                child_pending = child.get("pending") or {}
                pending = {
                    "type": "subworkflow_approval",
                    "node_id": node["id"],
                    "label": node.get("label") or node["id"],
                    "message": child_pending.get("message") or "子 Agent 需要人工审核后才能继续。",
                    "output": child_pending.get("output"),
                    "child_run_id": child_id,
                    "child_workflow_id": child_pending.get("approval_workflow_id") or child_pending.get("child_workflow_id") or child.get("workflow_id"),
                    "child_workflow_version": child_pending.get("approval_workflow_version") or child_pending.get("child_workflow_version") or child.get("workflow_version"),
                    "child_node_id": child_pending.get("approval_node_id") or child_pending.get("child_node_id") or child_pending.get("node_id"),
                    "child_node_label": child_pending.get("approval_node_label") or child_pending.get("child_node_label") or child_pending.get("label"),
                    "approval_run_id": child_pending.get("approval_run_id") or child_pending.get("child_run_id") or child_id,
                    "requested_at": child_pending.get("requested_at") or now_iso(),
                }
                raise SubworkflowApprovalRequired(node["id"], pending)
            if child["status"] in {"failed", "cancelled", "interrupted"}:
                raise RuntimeError(f"子工作流运行 {child_id} {child['status']}：{child.get('error') or '无错误详情'}")
            await asyncio.sleep(0.25)
    except asyncio.CancelledError:
        await cancel_run(child_id, _from_parent=True)
        record_event(run_id, node["id"], "subworkflow_cancelled", {"child_run_id": child_id})
        raise
    except SubworkflowApprovalRequired as exc:
        raise
    except Exception as exc:
        record_event(run_id, node["id"], "subworkflow_failed", {"child_run_id": child_id, "error": str(exc)})
        raise


def _redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    result = dict(arguments)
    headers = result.get("headers")
    if isinstance(headers, dict):
        protected = {}
        for key, value in headers.items():
            if any(
                marker in str(key).lower()
                for marker in ("authorization", "api-key", "apikey", "token", "secret", "password")
            ):
                protected[str(key)] = "[REDACTED]"
            else:
                protected[str(key)] = value
        result["headers"] = protected
    return result


def _condition(node: dict[str, Any], state: dict[str, Any]) -> bool:
    variable = str(node.get("variable") or "last_output")
    variable_path = str(node.get("variable_path") or "").strip()
    if variable_path:
        variable += variable_path if variable_path.startswith("[") else "." + variable_path
    found, value = _resolve_template_expression(variable, state)
    if not found:
        raise ValueError(f"条件变量 `{{{{{variable}}}}}` 在当前运行状态中无法解析。")
    expected = _render(
        node.get("value", ""),
        state,
        preserve_types=True,
        strict=True,
    )
    op = node.get("operator", "contains")
    left, right = str(value).lower(), str(expected).lower()
    if op == "contains":
        return right in left
    if op == "not_contains":
        return right not in left
    if op == "equals":
        return left == right
    if op == "not_equals":
        return left != right
    if op == "starts_with":
        return left.startswith(right)
    if op == "ends_with":
        return left.endswith(right)
    if op == "regex":
        return re.search(str(expected), str(value), re.DOTALL) is not None
    if op in {"gt", "gte", "lt", "lte"}:
        try:
            number = float(value)
            threshold = float(expected)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"数值比较操作符 {op} 需要可解析为数字的两侧，收到 {value!r} 与 {expected!r}。"
            ) from exc
        if op == "gt":
            return number > threshold
        if op == "gte":
            return number >= threshold
        if op == "lt":
            return number < threshold
        return number <= threshold
    if op == "is_empty":
        return not bool(value)
    if op == "is_not_empty":
        return bool(value)
    raise ValueError(f"未知条件操作符：{op}")


def _structured_output_instruction(schema: dict[str, Any] | None) -> str:
    if not schema:
        return ""
    return (
        "\n\n输出格式要求：只输出符合以下 JSON Schema 的有效 JSON，不要添加 Markdown 代码围栏、"
        "解释文字或前后缀。\nJSON Schema：\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
    )


def _parse_structured_output(content: Any, schema: dict[str, Any] | None) -> Any:
    text = str(content or "").strip()
    if not schema:
        return text
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
        elif lines:
            lines = lines[1:]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型输出不是有效 JSON：{exc.msg}（第 {exc.lineno} 行）") from exc
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        suffix = f"（另有 {len(errors) - 1} 个错误）" if len(errors) > 1 else ""
        raise ValueError(f"模型输出不符合 JSON Schema，{location}：{error.message}{suffix}")
    return value


def _next_node(node: dict[str, Any], state: dict[str, Any]) -> str | None:
    node_type = node["type"]
    if node_type == "condition":
        return node.get("true_next" if _condition(node, state) else "false_next")
    return node.get("next")


async def _call_harness_model(
    run_id: str,
    node_id: str,
    node_type: str,
    model_ref: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    tool_schemas: list[dict[str, Any]] | None = None,
    iteration: int | None = None,
) -> dict[str, Any]:
    source, _ = split_model_ref(model_ref)
    started = time.monotonic()
    try:
        result = await complete_model(
            model_ref,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_schemas=tool_schemas,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        record_event(
            run_id,
            node_id,
            "model_call_failed",
            {
                "model": model_ref,
                "provider": source,
                "operation": node_type,
                "iteration": iteration,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "error": str(exc)[:500],
                "diagnostic": exception_diagnostics(exc),
            },
        )
        raise
    usage = result.get("usage") or {}
    record_event(
        run_id,
        node_id,
        "model_call_completed",
        {
            "model": model_ref,
            "provider": source,
            "operation": node_type,
            "iteration": iteration,
            "latency_ms": result.get("latency_ms") or round((time.monotonic() - started) * 1000, 2),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
    )
    return result


def _model_context_length(model_ref: str) -> int:
    """解析模型的真实上下文窗口。查不到时回退默认值。"""
    try:
        from .models import list_gguf_models, list_local_models
        from .providers import remote_model_records

        for rec in list_local_models() + list_gguf_models() + remote_model_records():
            if rec.get("id") == model_ref and rec.get("context_length"):
                return int(rec["context_length"])
    except Exception:  # noqa: BLE001
        pass
    return AGENT_DEFAULT_CONTEXT_LENGTH


async def _compact_agent_context(
    run_id: str,
    node_id: str,
    model_ref: str,
    messages: list[dict[str, Any]],
    context_budget: int,
) -> list[dict[str, Any]]:
    """把早期 messages 压缩为一条 handoff 摘要，保留最近若干条与 tool 配对边界。

    借鉴 Codex auto-compact：用当前模型生成结构化交接摘要，替换被压缩区间。
    压缩失败（无安全切点/摘要模型报错）时原样返回，不中断任务。
    """
    cut = find_compaction_cut(messages, AGENT_COMPACTION_KEEP_RECENT)
    if cut <= 0:
        return messages
    history_text = render_history_for_compaction(messages[:cut])
    summary_messages = build_compaction_request(history_text)
    started = time.monotonic()
    try:
        result = await complete_model(
            model_ref,
            summary_messages,
            max_tokens=1024,
            temperature=0.0,
        )
        summary = str(result.get("content", "")).strip()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 压缩失败不应中断 agent
        record_event(
            run_id,
            node_id,
            "context_compaction_failed",
            {"error": str(exc)[:300], "dropped_messages": cut},
        )
        return messages
    if not summary:
        return messages
    record_event(
        run_id,
        node_id,
        "context_compacted",
        {
            "dropped_messages": cut,
            "kept_messages": len(messages) - cut,
            "summary_chars": len(summary),
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        },
    )
    # 保留首条 system，用摘要替换被压缩区间
    head = messages[0] if messages and messages[0].get("role") == "system" else None
    compacted: list[dict[str, Any]] = []
    if head is not None:
        compacted.append(head)
    compacted.append({"role": "user", "content": COMPACTION_PREFIX + summary})
    compacted.extend(messages[cut:])
    return compacted


async def _agent_call(
    run_id: str,
    node: dict[str, Any],
    state: dict[str, Any],
    model_ref: str,
) -> Any:
    selected_tools = list(dict.fromkeys(node.get("tools", [])))
    schemas = tool_schemas(selected_tools)
    allowed = set(selected_tools)
    prompt_parts = ["用户任务：\n" + str(state.get("input", ""))]
    last_output = state.get("last_output")
    if last_output and last_output != state.get("input"):
        prompt_parts.append("上一步结果：\n" + str(last_output))
    approval = state.get("approval")
    if approval and not approval.get("approved"):
        reviewer_note = str(approval.get("note") or "")
        prompt_parts.append(
            "审核未通过，请按照审核意见修订结果后再提交。\n审核意见：\n"
            + (reviewer_note or "审核人没有填写补充意见。")
        )
    if state.get("files"):
        prompt_parts.append(
            "本次上传文件：\n"
            + json.dumps(
                [
                    {"filename": item["filename"], "text": item["text"][:10000]}
                    for item in state.get("files", [])
                ],
                ensure_ascii=False,
            )
        )
    prompt = "\n\n".join(prompt_parts)
    schema = node.get("output_schema")
    system = str(node.get("system_prompt") or "你是一个能够调用工具完成任务的智能体。")
    system += _structured_output_instruction(schema)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    source, _ = split_model_ref(model_ref)
    iterations = max(1, min(int(node.get("max_iterations", 5)), MAX_AGENT_ITERATIONS))

    # 上下文预算：模型真实 context_length × 预算比例；tokenizer 用于精确计量
    context_length = int(node.get("context_length") or _model_context_length(model_ref))
    context_budget = int(context_length * AGENT_CONTEXT_BUDGET_RATIO)
    tokenizer = resolve_tokenizer(model_ref)
    # 上一次模型调用返回的真实 prompt_tokens（优先于本地计量）
    last_prompt_tokens: int | None = None

    for index in range(iterations):
        # 上下文压力检测：优先用模型真实预填充 token，否则用 tokenizer 精确计量
        used = (
            last_prompt_tokens
            if last_prompt_tokens is not None
            else count_messages_tokens(messages, tokenizer)
        )
        if used > context_budget:
            messages = await _compact_agent_context(
                run_id, node["id"], model_ref, messages, context_budget
            )

        if source == "local" and selected_tools:
            tool_menu = [
                {
                    "name": tool_id,
                    "description": TOOL_CATALOG[tool_id]["description"],
                    "parameters": TOOL_CATALOG[tool_id]["parameters"],
                }
                for tool_id in selected_tools
            ]
            local_system = (
                system
                + "\n\n可以通过工具执行任务。需要调用工具时，只输出一个 JSON 对象："
                + '{"tool":"工具ID","arguments":{"参数":"值"}}。'
                + "\n工具定义："
                + json.dumps(tool_menu, ensure_ascii=False)
                + "\n工具调用必须填写参数定义中的全部 required 字段。例如计算任务必须给 calculator 提供 expression。"
                + "数学表达式请使用数字和标准运算符 + - * /，不要把乘号写成中文符号。"
                + "\n不需要工具时直接用普通文本回答。不要伪造工具结果。"
            )
            call_messages = [dict(messages[0], content=local_system), *messages[1:]]
        else:
            call_messages = messages

        result = await _call_harness_model(
            run_id,
            node["id"],
            "agent",
            model_ref,
            call_messages,
            int(node.get("max_tokens", 512)),
            float(node.get("temperature", 0.2)),
            schemas if source == "remote" else None,
            index + 1,
        )
        tool_calls = result.get("tool_calls", [])
        # 捕获真实预填充 token，作为下一轮上下文压力的权威判据
        _usage = result.get("usage") or {}
        if isinstance(_usage.get("prompt_tokens"), int):
            last_prompt_tokens = _usage["prompt_tokens"]
        if source == "local" and selected_tools:
            local_call = extract_local_tool_call(result.get("content", ""), allowed)
            tool_calls = [local_call] if local_call else []
        if not tool_calls:
            return _parse_structured_output(result.get("content", ""), schema)

        assistant_calls = []
        argument_errors: dict[str, str] = {}
        for call_index, call in enumerate(tool_calls):
            tool_name = call.get("name")
            if tool_name not in allowed:
                continue
            arguments = call.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            argument_error = None
            if source == "local":
                if tool_name == "calculator" and not str(arguments.get("expression") or "").strip():
                    inferred = infer_calculator_expression(str(state.get("input", "")))
                    if inferred:
                        arguments["expression"] = inferred
                if tool_name in {"search_files", "semantic_search"} and not str(arguments.get("query") or "").strip():
                    arguments["query"] = str(state.get("input", "")).strip()
                required = TOOL_CATALOG[tool_name].get("parameters", {}).get("required", [])
                missing = [key for key in required if not str(arguments.get(key) or "").strip()]
                if missing:
                    argument_error = "缺少必需参数：" + "、".join(missing)
            call_id = call.get("id") or f"call_{index}_{call_index}"
            assistant_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            )
            if argument_error:
                argument_errors[call_id] = argument_error
        if not assistant_calls:
            return _parse_structured_output(result.get("content", ""), schema)

        messages.append(
            {
                "role": "assistant",
                "content": result.get("content") or None,
                "tool_calls": assistant_calls,
            }
        )
        for call in assistant_calls:
            function = call["function"]
            arguments = json.loads(function["arguments"])
            if call["id"] in argument_errors:
                error_message = argument_errors[call["id"]]
                record_event(
                    run_id,
                    node["id"],
                    "tool_failed",
                    {
                        "tool": function["name"],
                        "arguments": _redact_arguments(arguments),
                        "error": error_message,
                        "diagnostic": {"exception_type": "InvalidToolArguments"},
                    },
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps({"error": error_message}, ensure_ascii=False),
                    }
                )
                continue
            record_event(
                run_id,
                node["id"],
                "tool_started",
                {
                    "tool": function["name"],
                    "arguments": _redact_arguments(arguments),
                    "iteration": index + 1,
                },
            )
            started = time.monotonic()
            try:
                tool_result = await execute_tool(
                    function["name"], arguments, state.get("files", [])
                )
                record_event(
                    run_id,
                    node["id"],
                    "tool_completed",
                    {
                        "tool": function["name"],
                        "result": tool_result,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    },
                )
            except Exception as exc:
                tool_result = {"error": str(exc)}
                record_event(
                    run_id,
                    node["id"],
                    "tool_failed",
                    {
                        "tool": function["name"],
                        "error": str(exc),
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "diagnostic": exception_diagnostics(exc),
                    },
                )
            tool_result_text = json.dumps(tool_result, ensure_ascii=False)
            pruned_text = prune_tool_result(tool_result_text)
            if len(pruned_text) < len(tool_result_text):
                record_event(
                    run_id,
                    node["id"],
                    "tool_result_pruned",
                    {
                        "tool": function["name"],
                        "original_chars": len(tool_result_text),
                        "pruned_chars": len(pruned_text),
                    },
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": pruned_text,
                }
            )
    messages.append(
        {"role": "system", "content": "工具调用次数已用完，请基于现有结果回答，并说明缺少的信息。"}
    )
    final = await _call_harness_model(
        run_id,
        node["id"],
        "agent_final",
        model_ref,
        messages,
        int(node.get("max_tokens", 512)),
        0.2,
    )
    return _parse_structured_output(final.get("content", ""), schema)


async def _execute_node(
    run_id: str, node: dict[str, Any], state: dict[str, Any], workflow: dict[str, Any]
) -> Any:
    node_type = node["type"]
    if node_type == "start":
        return state.get("input", "")
    if node_type == "transform":
        return _render(node.get("template", "{{last_output}}"), state, preserve_types=True, strict=True)
    if node_type == "condition":
        return {"matched": _condition(node, state)}
    if node_type == "loop":
        return state.get("last_output", state.get("input", ""))
    if node_type == "parallel":
        return await _execute_parallel(run_id, node, state, workflow)
    if node_type == "fanin":
        results = state.get("_branch_join_results")
        if not isinstance(results, dict):
            raise RuntimeError("Fan-in 未收到对应 Fan-out 的分支结果。")
        failed = [branch_id for branch_id, result in results.items() if result.get("status") != "completed"]
        if failed and node.get("failure_policy", "fail") == "fail":
            details = {branch_id: results[branch_id].get("error") for branch_id in failed}
            raise RuntimeError("并行分支失败：" + json.dumps(details, ensure_ascii=False))
        return results
    if node_type == "subworkflow":
        return await _execute_subworkflow(run_id, node, state, workflow)
    if node_type == "tool":
        arguments = _render(node.get("arguments", {}), state, preserve_types=True, strict=True)
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是对象。")
        record_event(
            run_id,
            node["id"],
            "tool_started",
            {"tool": node["tool"], "arguments": _redact_arguments(arguments)},
        )
        started = time.monotonic()
        try:
            result = await execute_tool(node["tool"], arguments, state.get("files", []))
        except Exception as exc:
            record_event(
                run_id,
                node["id"],
                "tool_failed",
                {
                    "tool": node["tool"],
                    "error": str(exc),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "diagnostic": exception_diagnostics(exc),
                },
            )
            raise
        record_event(
            run_id,
            node["id"],
            "tool_completed",
            {
                "tool": node["tool"],
                "result": result,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return result
    if node_type in {"llm", "agent"}:
        model_ref = str(node.get("model") or workflow.get("model") or "local/qwen3.5-0.8b")
        system = str(node.get("system_prompt") or "")
        user_text = str(state.get("input", ""))
        if state.get("last_output") and state["last_output"] != user_text:
            user_text += "\n\n上一步结果：\n" + str(state["last_output"])
        if node_type == "agent":
            return await _agent_call(run_id, node, state, model_ref)
        messages = []
        system += _structured_output_instruction(node.get("output_schema"))
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_text})
        result = await _call_harness_model(
            run_id,
            node["id"],
            "llm",
            model_ref,
            messages,
            int(node.get("max_tokens", 512)),
            float(node.get("temperature", 0.2)),
        )
        return _parse_structured_output(result.get("content", ""), node.get("output_schema"))
    if node_type == "approval":
        return {"approval_required": True}
    if node_type == "end":
        return state.get("last_output", state.get("input", ""))
    raise ValueError(f"未知节点类型：{node_type}")


async def execute_run(run_id: str) -> None:
    run = get_run(run_id)
    if not run:
        return
    definition = run.get("workflow_snapshot") or {}
    if not definition:
        workflow = get_workflow(run["workflow_id"])
        definition = workflow["definition"] if workflow else {}
    if not definition:
        _save_run(run_id, run["state"], None, status="failed", error="工作流已删除。")
        return
    state = run["state"]
    current = run["current_node"]
    _save_run(run_id, state, current, status="running", error=None)
    record_event(run_id, current, "run_started", {})

    def persist_graph_state(
        updated_state: dict[str, Any],
        node_id: str | None,
        status: str,
        pending: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        _save_run(run_id, updated_state, node_id, status, pending, error)

    try:
        if state.get("cancel_requested"):
            _save_run(run_id, state, current, status="cancelled")
            record_event(run_id, current, "run_cancelled", {})
            return
        state = await invoke_workflow(
            run_id,
            definition,
            state,
            _execute_node,
            _condition,
            record_event,
            persist_graph_state,
        )
        interruptions = state.pop("__interrupt__", [])
        if interruptions:
            interrupt_value = getattr(interruptions[0], "value", interruptions[0])
            pending = interrupt_value if isinstance(interrupt_value, dict) else {
                "type": "approval", "node_id": current, "message": str(interrupt_value)
            }
            current = pending.get("node_id", current)
            _save_run(
                run_id,
                state,
                current,
                status="waiting_approval",
                pending=pending,
            )
            record_event(run_id, current, "approval_requested", pending)
            return
        output = state.get("result", state.get("last_output"))
        _save_run(run_id, state, None, status="completed")
        record_event(run_id, current, "run_completed", {"result": output})
    except asyncio.CancelledError:
        fresh = get_run(run_id)
        latest_state = fresh["state"] if fresh else state
        _save_run(run_id, latest_state, current, status="cancelled")
        record_event(run_id, current, "run_cancelled", {})
    except SubworkflowApprovalRequired as exc:
        fresh = get_run(run_id)
        latest_state = fresh["state"] if fresh else state
        _save_run(
            run_id,
            latest_state,
            exc.parent_node_id,
            status="waiting_approval",
            pending=exc.pending,
        )
        record_event(run_id, exc.parent_node_id, "subworkflow_approval_waiting", exc.pending)
    except Exception as exc:
        logging.getLogger("llmplatform.harness").exception(
            "Workflow run %s failed at node %s", run_id, current
        )
        fresh = get_run(run_id)
        latest_state = fresh["state"] if fresh else state
        latest_node = fresh["current_node"] if fresh else current
        _save_run(run_id, latest_state, latest_node, status="failed", error=str(exc))
        record_event(
            run_id,
            current,
            "run_failed",
            {"error": str(exc), "diagnostic": exception_diagnostics(exc)},
        )
    finally:
        RUN_TASKS.pop(run_id, None)
