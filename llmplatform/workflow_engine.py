from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import set_config_context
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from .diagnostics import exception_diagnostics
from .storage import DATA_DIR


CHECKPOINT_PATH = DATA_DIR / "harness-checkpoints.sqlite3"
MAX_GRAPH_STEPS = 100
MAX_BRANCH_STEPS = 100
MAX_WORKFLOW_ACTIONS = 2000


def _merge_visits(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged = dict(left or {})
    for node_id, increment in (right or {}).items():
        merged[node_id] = int(merged.get(node_id, 0)) + int(increment)
    return merged


def _sum_counter(left: int | None, right: int | None) -> int:
    return int(left or 0) + int(right or 0)


def _merge_branch_results(
    left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any] | None]
) -> dict[str, dict[str, Any]]:
    merged = {key: dict(value) for key, value in (left or {}).items()}
    for run_key, branches in (right or {}).items():
        if branches is None:
            merged.pop(run_key, None)
            continue
        current = dict(merged.get(run_key, {}))
        current.update(branches)
        merged[run_key] = current
    return merged


def _merge_join_signal(left: dict[str, Any] | None, right: dict[str, Any] | None):
    # All arrivals at one barrier publish the same signal. Replacing it also
    # allows a later loop iteration to publish a new fan-out invocation.
    return right or left


class WorkflowState(TypedDict, total=False):
    input: str
    file_ids: list[str]
    files: list[dict[str, Any]]
    variables: dict[str, Any]
    last_output: Any
    result: Any
    last_error: dict[str, Any] | None
    step_count: int
    node_visits: Annotated[dict[str, int], _merge_visits]
    approval: dict[str, Any] | None
    review_notes: list[dict[str, Any]]
    cancel_requested: bool
    _resume_node: str
    _error_route_from: str
    _branch_results: Annotated[dict[str, dict[str, Any] | None], _merge_branch_results]
    _join_signal: Annotated[dict[str, Any], _merge_join_signal]
    _execution_meter: Annotated[int, _sum_counter]
    _execution_reserved: Annotated[int, _sum_counter]
    _branch_join_results: dict[str, Any] | None
    _branch_context: dict[str, Any]


_checkpointer_context: Any = None
_checkpointer: AsyncSqliteSaver | None = None


async def initialize_workflow_engine() -> None:
    """Open a dedicated durable SQLite checkpointer for graph execution."""
    global _checkpointer_context, _checkpointer
    if _checkpointer is not None:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    context = AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_PATH))
    saver = await context.__aenter__()
    try:
        await saver.setup()
    except Exception:
        await context.__aexit__(None, None, None)
        raise
    _checkpointer_context = context
    _checkpointer = saver


async def close_workflow_engine() -> None:
    global _checkpointer_context, _checkpointer
    context = _checkpointer_context
    _checkpointer_context = None
    _checkpointer = None
    if context is not None:
        await context.__aexit__(None, None, None)


def _build_graph(
    definition: dict[str, Any],
    run_id: str,
    execute_node: Callable[..., Any],
    condition: Callable[..., bool],
    record_event: Callable[..., None],
    save_run: Callable[..., None],
):
    if _checkpointer is None:
        raise RuntimeError("Harness checkpoint 存储尚未初始化。")

    nodes = definition["nodes"]
    by_id = {node["id"]: node for node in nodes}
    start_id = next(node["id"] for node in nodes if node["type"] == "start")
    internal_id = {node["id"]: f"workflow_node_{index}" for index, node in enumerate(nodes)}
    fanin_for = {
        node["id"]: node
        for node in nodes
        if node["type"] == "fanin"
    }
    fanout_semaphores: dict[str, asyncio.Semaphore] = {}
    branch_step_id = "harness_branch_step"
    branch_approval_id = "harness_branch_approval"
    branch_arrival_id = "harness_branch_arrival"
    join_check_id = "harness_fanout_join_check"
    builder = StateGraph(WorkflowState)

    async def dispatch_fanout(state: WorkflowState, current: dict[str, Any]) -> Command:
        current_id = current["id"]
        branches = current["branches"]
        step_count = int(state.get("step_count", 0)) + 1
        if step_count > MAX_GRAPH_STEPS:
            raise RuntimeError("工作流超过 100 个执行步骤，已停止以保护运行资源。")
        meter = int(state.get("_execution_meter", 0))
        reserved = int(state.get("_execution_reserved", 0))
        remaining = MAX_WORKFLOW_ACTIONS - meter - reserved - 1
        per_branch_budget = min(MAX_BRANCH_STEPS, remaining // len(branches))
        if per_branch_budget < 1:
            raise RuntimeError("工作流并行执行预算已用完，未启动新的分支。")

        visits = dict(state.get("node_visits", {}))
        visit_number = int(visits.get(current_id, 0)) + 1
        run_key = f"{current_id}@{visit_number}"
        paired_fanin = fanin_for[current["fanin_id"]]
        variables = dict(state.get("variables", {}))
        variables[current_id] = {
            "status": "running",
            "run_key": run_key,
            "branch_count": len(branches),
        }
        root_state = {
            key: state.get(key)
            for key in ("input", "file_ids", "files", "last_error", "result")
            if key in state
        }
        root_state.update({
            "variables": variables,
            "last_output": state.get("last_output"),
            "step_count": step_count,
            "node_visits": _merge_visits(visits, {current_id: 1}),
        })
        record_event(
            run_id,
            current_id,
            "node_started",
            {"type": "fanout", "label": current.get("label") or current_id},
        )
        persisted = {**state, **root_state, "_execution_meter": meter + 1}
        persisted["node_visits"] = root_state["node_visits"]
        save_run(persisted, current_id, "running")
        record_event(
            run_id,
            current_id,
            "fanout_started",
            {
                "fanin_id": paired_fanin["id"],
                "branches": len(branches),
                "max_concurrency": current.get("max_concurrency", len(branches)),
                "branch_step_budget": per_branch_budget,
            },
        )

        fanout_semaphores.setdefault(
            run_key,
            asyncio.Semaphore(max(1, min(int(current.get("max_concurrency", len(branches))), len(branches)))),
        )
        # A resumed graph rebuilds this semaphore lazily from the pinned definition.
        destinations = []
        for branch in branches:
            context = {
                "run_key": run_key,
                "fanout_id": current_id,
                "fanin_id": paired_fanin["id"],
                "branch_id": branch["id"],
                "branch_label": branch.get("label") or branch["id"],
                "node_id": branch["next"],
                "state": {
                    **root_state,
                    "variables": dict(root_state.get("variables", {})),
                    "node_visits": dict(root_state.get("node_visits", {})),
                },
                "steps": 0,
                "step_budget": per_branch_budget,
                "reserved_budget": per_branch_budget,
                "expected_branch_ids": [item["id"] for item in branches],
                "status": "running",
            }
            destinations.append(Send(branch_step_id, {"_branch_context": context}))
            record_event(
                run_id,
                current_id,
                "fanout_branch_started",
                {"branch_id": branch["id"], "label": context["branch_label"], "entry": branch["next"]},
            )
        return Command(
            update={
                "variables": variables,
                "step_count": step_count,
                "node_visits": {current_id: 1},
                "_execution_meter": 1,
                "_execution_reserved": per_branch_budget * len(branches),
            },
            goto=destinations,
        )

    async def _run_branch_step(task_state: WorkflowState) -> Command:
        context = dict(task_state.get("_branch_context") or {})
        branch_id = context.get("branch_id", "?")
        fanout_id = context.get("fanout_id", "?")
        run_key = context.get("run_key", "?")
        local_state = dict(context.get("state") or {})
        current_id = str(context.get("node_id") or "")

        def arrive(status: str, *, output: Any = None, error: str | None = None) -> Command:
            context["status"] = status
            context["output"] = output
            context["error"] = error
            context["state"] = local_state
            return Command(
                update={},
                goto=Send(branch_arrival_id, {"_branch_context": context}),
            )

        if current_id == context.get("fanin_id"):
            context["status"] = "completed"
            context["output"] = local_state.get("last_output")
            context["state"] = local_state
            return Command(goto=Send(branch_arrival_id, {"_branch_context": context}))

        current = by_id.get(current_id)
        if current is None or current["type"] in {"start", "end", "fanout", "fanin"}:
            reason = f"分支 {branch_id} 进入不允许的节点：{current_id or '空目标'}"
            return arrive("failed", output=local_state.get("last_output"), error=reason)
        resuming_same_node = bool(context.pop("_resume_same_node", False))
        if (
            not resuming_same_node
            and int(context.get("steps", 0)) >= int(context.get("step_budget", MAX_BRANCH_STEPS))
        ):
            reason = f"分支超过分配的 {context.get('step_budget')} 个节点执行预算。"
            return arrive("failed", output=local_state.get("last_output"), error=reason)

        visits = dict(local_state.get("node_visits", {}))
        if not resuming_same_node:
            visits[current_id] = int(visits.get(current_id, 0)) + 1
            local_state["node_visits"] = visits
            context["steps"] = int(context.get("steps", 0)) + 1
            record_event(
                run_id,
                current_id,
                "node_started",
                {
                    "type": current["type"],
                    "label": current.get("label") or current_id,
                    "branch_id": branch_id,
                    "fanout_id": fanout_id,
                },
            )

        if current["type"] == "approval":
            context["state"] = local_state
            context["_approval_payload"] = {
                "type": "branch_approval",
                "node_id": current_id,
                "label": current.get("label") or current_id,
                "message": current.get("message", "等待人工审核。"),
                "output": local_state.get("last_output"),
                "branch_id": branch_id,
                "branch_label": context.get("branch_label") or branch_id,
                "fanout_id": fanout_id,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
            return Command(
                goto=Send(branch_approval_id, {"_branch_context": context}),
            )
        started = asyncio.get_running_loop().time()
        retries = max(0, min(int(current.get("retries", 0) or 0), 5))
        timeout = max(1, min(int(current.get("timeout", 600) or 600), 3600))
        semaphore = fanout_semaphores.get(run_key)
        if semaphore is None:
            fanout = by_id[fanout_id]
            concurrency = max(
                1,
                min(
                    int(fanout.get("max_concurrency", len(fanout["branches"]))),
                    len(fanout["branches"]),
                ),
            )
            semaphore = fanout_semaphores.setdefault(
                run_key, asyncio.Semaphore(concurrency)
            )
        output: Any = None
        final_error: Exception | None = None
        suspended_subworkflow: dict[str, Any] | None = None
        async with semaphore:
            for attempt in range(retries + 1):
                try:
                    output = await asyncio.wait_for(
                        execute_node(run_id, current, dict(local_state), definition),
                        timeout=timeout,
                    )
                    final_error = None
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    pending = getattr(exc, "pending", None)
                    if getattr(exc, "suspend_workflow", False) and isinstance(pending, dict):
                        suspended_subworkflow = {
                            **pending,
                            "type": "branch_subworkflow_approval",
                            "node_id": current_id,
                            "branch_id": branch_id,
                            "branch_label": context.get("branch_label") or branch_id,
                            "fanout_id": fanout_id,
                            "requested_at": pending.get("requested_at") or datetime.now(timezone.utc).isoformat(),
                        }
                        break
                    final_error = exc
                    record_event(
                        run_id,
                        current_id,
                        "node_attempt_failed",
                        {
                            "attempt": attempt + 1,
                            "max_attempts": retries + 1,
                            "error": str(exc)[:1000],
                            "diagnostic": exception_diagnostics(exc),
                            "branch_id": branch_id,
                        },
                    )
                    if attempt < retries:
                        await asyncio.sleep(min(2**attempt, 8))

        if suspended_subworkflow is not None:
            context["state"] = local_state
            context["_resume_same_node"] = True
            context["_approval_payload"] = suspended_subworkflow
            return Command(
                goto=Send(branch_approval_id, {"_branch_context": context}),
            )

        if final_error is not None:
            error_target = current.get("error_next")
            error_message = str(final_error).strip() or type(final_error).__name__
            error_info = {
                "node_id": current_id,
                "node_type": current["type"],
                "type": type(final_error).__name__,
                "message": error_message[:1000],
                "diagnostic": exception_diagnostics(final_error),
                "attempts": retries + 1,
            }
            local_state["variables"] = dict(local_state.get("variables", {}))
            local_state["variables"][current_id] = error_info
            local_state["last_output"] = error_info
            local_state["last_error"] = error_info
            if error_target:
                context["node_id"] = error_target
                context["state"] = local_state
                record_event(run_id, current_id, "node_error_routed", {"target": error_target, "error": error_info, "branch_id": branch_id})
                return Command(
                    update={"_execution_meter": 1, "node_visits": {current_id: 1}},
                    goto=Send(branch_step_id, {"_branch_context": context}),
                )
            context["state"] = local_state
            context["node_id"] = ""
            context["status"] = "failed"
            context["output"] = error_info
            context["error"] = error_info["message"]
            return Command(
                update={"_execution_meter": 1, "node_visits": {current_id: 1}},
                goto=Send(branch_arrival_id, {"_branch_context": context}),
            )

        local_state["variables"] = dict(local_state.get("variables", {}))
        local_state["variables"][current_id] = output
        local_state["last_output"] = output
        local_state["last_error"] = None
        elapsed = round((asyncio.get_running_loop().time() - started) * 1000, 2)
        record_event(
            run_id,
            current_id,
            "node_completed",
            {"output": output, "duration_ms": elapsed, "branch_id": branch_id, "fanout_id": fanout_id},
        )

        if current["type"] == "condition":
            matched = output.get("matched") if isinstance(output, dict) else None
            if not isinstance(matched, bool):
                matched = condition(current, local_state)
            next_id = current["true_next"] if matched else current["false_next"]
        elif current["type"] == "loop":
            limit = max(1, min(int(current.get("max_iterations", 5) or 5), 50))
            next_id = current["done_next"] if visits[current_id] > limit or not condition(current, local_state) else current["body_next"]
        else:
            next_id = current["next"]
        context["node_id"] = next_id
        context["state"] = local_state
        if next_id == context.get("fanin_id"):
            context["status"] = "completed"
            context["output"] = local_state.get("last_output")
        destination = branch_arrival_id if next_id == context.get("fanin_id") else branch_step_id
        return Command(
            update={"_execution_meter": 1, "node_visits": {current_id: 1}},
            goto=Send(destination, {"_branch_context": context}),
        )

    async def _run_branch_approval(task_state: WorkflowState) -> Command:
        context = dict(task_state.get("_branch_context") or {})
        payload = dict(context.pop("_approval_payload", {}) or {})
        node_id = str(payload.get("node_id") or "")
        node = by_id.get(node_id)
        if not node:
            raise RuntimeError("审批中断引用了不存在的工作流节点。")

        # Keep the interrupt as the first side effect in this dedicated graph
        # task. LangGraph replays this node on resume, so its interrupt order is
        # stable even when the parent branch resumes a subworkflow.
        response = interrupt(payload)
        branch_id = str(context.get("branch_id") or "?")
        fanout_id = str(context.get("fanout_id") or "?")
        local_state = dict(context.get("state") or {})

        if payload.get("type") == "branch_subworkflow_approval":
            context["node_id"] = node_id
            context["state"] = local_state
            context["_resume_same_node"] = True
            return Command(goto=Send(branch_step_id, {"_branch_context": context}))

        decision = response if isinstance(response, dict) else {}
        approved = bool(decision.get("approved"))
        note = str(decision.get("note") or "")[:5000]
        result = {"approved": approved, "note": note}
        variables = dict(local_state.get("variables", {}))
        variables[node_id] = result
        local_state["variables"] = variables
        local_state["last_output"] = result
        local_state["last_error"] = None
        local_state.setdefault("review_notes", []).append(
            {"node_id": node_id, "approved": approved, "note": note, "created_at": datetime.now(timezone.utc).isoformat()}
        )
        next_id = node.get("on_approve" if approved else "on_reject")
        context["node_id"] = next_id
        context["state"] = local_state
        context["output"] = result
        record_event(
            run_id,
            node_id,
            "node_completed",
            {"output": result, "duration_ms": None, "branch_id": branch_id, "fanout_id": fanout_id},
        )
        if next_id == context.get("fanin_id"):
            context["status"] = "completed"
            context["output"] = result
            destination = branch_arrival_id
        else:
            destination = branch_step_id
        return Command(
            update={"_execution_meter": 1, "node_visits": {node_id: 1}},
            goto=Send(destination, {"_branch_context": context}),
        )

    async def with_interrupt_context(action: Callable[[WorkflowState], Any], state: WorkflowState, config: RunnableConfig):
        # Python 3.10 does not propagate contextvars into newly-created asyncio
        # tasks. interrupt() reads LangGraph's runnable context, so explicitly
        # copy the graph config into its child task on that runtime.
        with set_config_context(config) as context:
            task = context.run(asyncio.create_task, action(state))
        return await task

    async def run_branch_step(task_state: WorkflowState, config: RunnableConfig) -> Command:
        return await with_interrupt_context(_run_branch_step, task_state, config)

    async def run_branch_approval(task_state: WorkflowState, config: RunnableConfig) -> Command:
        return await with_interrupt_context(_run_branch_approval, task_state, config)

    async def receive_branch(task_state: WorkflowState) -> dict[str, Any]:
        context = dict(task_state.get("_branch_context") or {})
        run_key = str(context.get("run_key") or "")
        branch_id = str(context.get("branch_id") or "")
        result = {
            "status": context.get("status", "completed"),
            "output": context.get("output", (context.get("state") or {}).get("last_output")),
            "error": context.get("error"),
            "steps": int(context.get("steps", 0)),
            "variables": dict((context.get("state") or {}).get("variables", {})),
        }
        event_type = "fanout_branch_completed" if result["status"] == "completed" else "fanout_branch_failed"
        record_event(
            run_id,
            context.get("fanout_id"),
            event_type,
            {"branch_id": branch_id, "label": context.get("branch_label"), "output": result["output"], "error": result["error"], "steps": result["steps"]},
        )
        return {
            "_branch_results": {run_key: {branch_id: result}},
            "_join_signal": {
                "run_key": run_key,
                "fanout_id": context.get("fanout_id"),
                "fanin_id": context.get("fanin_id"),
                "expected": context.get("expected_branch_ids"),
            },
            "_execution_reserved": -int(context.get("reserved_budget", 0)),
        }

    async def check_fanout_join(state: WorkflowState) -> Command:
        signal = dict(state.get("_join_signal") or {})
        run_key = str(signal.get("run_key") or "")
        expected = list(signal.get("expected") or [])
        completed = dict((state.get("_branch_results") or {}).get(run_key, {}))
        if not run_key or any(branch_id not in completed for branch_id in expected):
            return Command(goto=END)

        fanout_id = str(signal["fanout_id"])
        fanin_id = str(signal["fanin_id"])
        results = {branch_id: completed[branch_id] for branch_id in expected}
        variables = dict(state.get("variables", {}))
        variables[fanout_id] = results
        updated = dict(state)
        updated.pop("_branch_results", None)
        updated["variables"] = variables
        updated["last_output"] = results
        updated["_branch_join_results"] = results
        record_event(
            run_id,
            fanout_id,
            "fanout_completed",
            {"fanin_id": fanin_id, "branches": results},
        )
        record_event(run_id, fanout_id, "node_completed", {"output": results, "duration_ms": None})
        fanout_semaphores.pop(run_key, None)
        return Command(
            update={"_branch_results": {run_key: None}},
            goto=Send(internal_id[fanin_id], updated),
        )

    for node in nodes:
        node_id = node["id"]
        graph_node = internal_id[node_id]

        if node["type"] == "fanout":
            async def execute_graph_fanout(
                state: WorkflowState, current: dict[str, Any] = node
            ) -> Command:
                return await dispatch_fanout(state, current)

            builder.add_node(graph_node, execute_graph_fanout)
            continue

        async def execute_graph_node(state: WorkflowState, current: dict[str, Any] = node) -> dict[str, Any]:
            current_id = current["id"]
            label = current.get("label") or current_id
            record_event(
                run_id,
                current_id,
                "node_started",
                {"type": current["type"], "label": label},
            )
            save_run(state, current_id, "running")
            started = asyncio.get_running_loop().time()
            step_count = int(state.get("step_count", 0)) + 1
            if step_count > MAX_GRAPH_STEPS:
                raise RuntimeError("工作流超过 100 个执行步骤，已停止以保护运行资源。")
            if (
                int(state.get("_execution_meter", 0))
                + int(state.get("_execution_reserved", 0))
                + 1
                > MAX_WORKFLOW_ACTIONS
            ):
                raise RuntimeError(f"工作流超过 {MAX_WORKFLOW_ACTIONS} 个执行动作，已停止以保护运行资源。")

            if current["type"] == "approval":
                raise RuntimeError(
                    "审批节点只能通过 Harness 的持久化暂停与恢复接口执行。"
                )

            retries = max(0, min(int(current.get("retries", 0) or 0), 5))
            timeout = max(1, min(int(current.get("timeout", 600) or 600), 3600))
            output: Any = None
            for attempt in range(retries + 1):
                try:
                    output = await asyncio.wait_for(
                        execute_node(run_id, current, dict(state), definition),
                        timeout=timeout,
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if getattr(exc, "suspend_workflow", False):
                        # Approval waits are durable control-flow pauses, not node
                        # failures: do not retry them or route them to error_next.
                        raise
                    record_event(
                        run_id,
                        current_id,
                        "node_attempt_failed",
                        {
                            "attempt": attempt + 1,
                            "max_attempts": retries + 1,
                            "error": str(exc)[:1000],
                            "diagnostic": exception_diagnostics(exc),
                        },
                    )
                    if attempt >= retries:
                        error_target = current.get("error_next")
                        if error_target:
                            error_message = str(exc).strip() or type(exc).__name__
                            error_info = {
                                "node_id": current_id,
                                "node_type": current["type"],
                                "type": type(exc).__name__,
                                "message": error_message[:1000],
                                "diagnostic": exception_diagnostics(exc),
                                "attempts": attempt + 1,
                            }
                            variables = dict(state.get("variables", {}))
                            variables[current_id] = error_info
                            updated = {
                                "variables": variables,
                                "last_output": error_info,
                                "last_error": error_info,
                                "step_count": step_count,
                                "_execution_meter": 1,
                                "_error_route_from": current_id,
                            }
                            if current["type"] == "fanin":
                                updated["_branch_join_results"] = None
                            persisted = {**state, **updated}
                            persisted["node_visits"] = _merge_visits(
                                dict(state.get("node_visits", {})), {current_id: 1}
                            )
                            save_run(persisted, current_id, "running")
                            record_event(
                                run_id,
                                current_id,
                                "node_error_routed",
                                {
                                    "target": error_target,
                                    "attempts": attempt + 1,
                                    "error": error_info,
                                    "duration_ms": round(
                                        (asyncio.get_running_loop().time() - started) * 1000,
                                        2,
                                    ),
                                },
                            )
                            updated["node_visits"] = {current_id: 1}
                            return updated
                        raise
                    await asyncio.sleep(min(2**attempt, 8))

            variables = dict(state.get("variables", {}))
            variables[current_id] = output
            updated = {
                "variables": variables,
                "last_output": output,
                "step_count": step_count,
                "_execution_meter": 1,
                "_error_route_from": "",
            }
            if current["type"] == "fanin":
                updated["_branch_join_results"] = None
            if current["type"] == "end":
                updated["result"] = output
            persisted = {**state, **updated}
            persisted["node_visits"] = _merge_visits(
                dict(state.get("node_visits", {})), {current_id: 1}
            )
            save_run(persisted, current_id, "running")
            elapsed = round((asyncio.get_running_loop().time() - started) * 1000, 2)
            record_event(
                run_id,
                current_id,
                "node_completed",
                {"output": output, "duration_ms": elapsed},
            )
            updated["node_visits"] = {current_id: 1}
            return updated

        builder.add_node(graph_node, execute_graph_node)

    builder.add_node(branch_step_id, run_branch_step)
    builder.add_node(branch_approval_id, run_branch_approval)
    builder.add_node(branch_arrival_id, receive_branch)
    builder.add_node(join_check_id, check_fanout_join)
    builder.add_edge(branch_arrival_id, join_check_id)

    entry_targets = {node_id: internal_id[node_id] for node_id in internal_id}

    def choose_entry(state: WorkflowState) -> str:
        candidate = state.get("_resume_node")
        return candidate if candidate in internal_id else start_id

    builder.add_conditional_edges(START, choose_entry, entry_targets)

    def possible_targets(node: dict[str, Any], state: WorkflowState) -> list[str]:
        node_type = node["type"]
        if node_type == "condition":
            targets = [node["true_next"], node["false_next"]]
        elif node_type == "approval":
            targets = [node["on_approve"], node["on_reject"]]
        elif node_type == "loop":
            targets = [node["body_next"], node["done_next"]]
        elif node_type == "end":
            targets = []
        else:
            targets = [node["next"]]
        if state.get("_error_route_from") == node["id"] and node.get("error_next"):
            return [node["error_next"]]
        if node_type == "condition":
            node_output = (state.get("variables") or {}).get(node["id"])
            matched = node_output.get("matched") if isinstance(node_output, dict) else None
            if not isinstance(matched, bool):
                matched = condition(node, dict(state))
            return [node["true_next"] if matched else node["false_next"]]
        if node_type == "loop":
            visits = int((state.get("node_visits") or {}).get(node["id"], 0))
            limit = max(1, min(int(node.get("max_iterations", 5) or 5), 50))
            if visits > limit or not condition(node, dict(state)):
                return [node["done_next"]]
            return [node["body_next"]]
        return targets[:1]

    for node in nodes:
        source = internal_id[node["id"]]
        node_type = node["type"]
        if node_type == "fanout":
            # This node creates durable LangGraph Send tasks for each branch.
            continue
        if node_type == "end":
            builder.add_edge(source, END)
        elif node_type == "approval":
            builder.add_conditional_edges(
                source,
                lambda state: bool((state.get("approval") or {}).get("approved")),
                {
                    True: internal_id[node["on_approve"]],
                    False: internal_id[node["on_reject"]],
                },
            )
        else:
            normal_targets = (
                [node["true_next"], node["false_next"]]
                if node_type == "condition" else
                [node["body_next"], node["done_next"]]
                if node_type == "loop" else
                [node["next"]]
            )
            target_ids = list(dict.fromkeys(
                normal_targets + ([node["error_next"]] if node.get("error_next") else [])
            ))

            def choose_route(state: WorkflowState, current: dict[str, Any] = node) -> str:
                targets = possible_targets(current, state)
                return targets[0]

            builder.add_conditional_edges(
                source,
                choose_route,
                {target: internal_id[target] for target in target_ids},
            )

    return builder.compile(checkpointer=_checkpointer)


async def invoke_workflow(
    run_id: str,
    definition: dict[str, Any],
    initial_state: dict[str, Any],
    execute_node: Callable[..., Any],
    condition: Callable[..., bool],
    record_event: Callable[..., None],
    save_run: Callable[..., None],
) -> dict[str, Any]:
    """Run or resume a version-pinned workflow using its durable run ID."""
    graph = _build_graph(
        definition, run_id, execute_node, condition, record_event, save_run
    )
    config = {"configurable": {"thread_id": run_id}}
    snapshot = await graph.aget_state(config)
    has_checkpoint = bool(snapshot.values or snapshot.next or snapshot.tasks)
    resume_payload = initial_state.pop("_resume_payload", None)
    resume_approval_node = initial_state.pop("_resume_approval_node", None)
    resume_interrupt = initial_state.pop("_resume_interrupt", None)
    nodes = {node["id"]: node for node in definition["nodes"]}
    internal_id = {
        node["id"]: f"workflow_node_{index}"
        for index, node in enumerate(definition["nodes"])
    }
    approval_graph_nodes = [
        internal_id[node_id]
        for node_id, node in nodes.items()
        if node["type"] == "approval"
    ]

    if resume_interrupt is not None:
        interrupt_id = str(resume_interrupt.get("interrupt_id") or "")
        pending_ids = {
            str(pending_interrupt.id)
            for task in snapshot.tasks
            if task.result is None and task.error is None
            for pending_interrupt in getattr(task, "interrupts", ())
        }
        if not has_checkpoint or not interrupt_id or interrupt_id not in pending_ids:
            raise RuntimeError("审批中断已不在当前图 checkpoint 中；拒绝把决定应用到其他分支。")
        await graph.ainvoke(
            Command(resume={interrupt_id: resume_interrupt.get("value")}),
            config=config,
            interrupt_before=approval_graph_nodes,
        )
    elif (
        resume_payload is not None
        and has_checkpoint
        and resume_approval_node in nodes
        and internal_id[resume_approval_node] in snapshot.next
    ):
        decision = {
            "approved": bool(resume_payload.get("approved")),
            "note": str(resume_payload.get("note") or "")[:5000],
        }
        values = {
            "approval": decision,
            "variables": dict(initial_state.get("variables", {})),
            "review_notes": list(initial_state.get("review_notes", [])),
            "step_count": int(initial_state.get("step_count", 0)),
        }
        await graph.aupdate_state(
            config,
            values,
            as_node=internal_id[resume_approval_node],
        )
        record_event(
            run_id,
            resume_approval_node,
            "node_completed",
            {"output": decision, "duration_ms": None},
        )
        await graph.ainvoke(
            None,
            config=config,
            # The resolved approval is bypassed by a checkpoint state update.
            # Keep every approval as a breakpoint so a reject/revise loop can
            # pause at that same node again during this invocation.
            interrupt_before=approval_graph_nodes,
        )
    elif has_checkpoint and snapshot.next:
        await graph.ainvoke(None, config=config, interrupt_before=approval_graph_nodes)
    else:
        await graph.ainvoke(
            initial_state,
            config=config,
            interrupt_before=approval_graph_nodes,
        )

    latest = await graph.aget_state(config)
    result = dict(latest.values or initial_state)
    branch_interrupts = []
    for task in latest.tasks:
        if task.result is not None or task.error is not None:
            continue
        for pending_interrupt in getattr(task, "interrupts", ()):
            payload = getattr(pending_interrupt, "value", None)
            if not isinstance(payload, dict) or payload.get("type") not in {
                "branch_approval",
                "branch_subworkflow_approval",
            }:
                continue
            branch_interrupts.append({
                **payload,
                "interrupt_id": pending_interrupt.id,
                "task_id": task.id,
            })
    branch_interrupts.sort(
        key=lambda item: (
            str(item.get("fanout_id") or ""),
            str(item.get("branch_id") or ""),
            str(item.get("node_id") or ""),
            str(item.get("interrupt_id") or ""),
        )
    )
    if branch_interrupts:
        branch_interrupts[0]["pending_branch_approvals"] = len(branch_interrupts)
        result["__interrupt__"] = branch_interrupts
        return result

    for graph_node in latest.next:
        node_id = next(
            (candidate for candidate, internal in internal_id.items() if internal == graph_node),
            None,
        )
        node = nodes.get(node_id or "")
        if node and node["type"] == "approval":
            payload = {
                "type": "approval",
                "node_id": node_id,
                "label": node.get("label") or node_id,
                "message": node.get("message", "等待人工审核。"),
                "output": result.get("last_output"),
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
            record_event(
                run_id,
                node_id,
                "node_started",
                {"type": "approval", "label": payload["label"]},
            )
            result["__interrupt__"] = [payload]
            break
    return result
