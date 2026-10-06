from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import shutil
import statistics
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

import psutil

from .harness import MAX_ACTIVE_ROOT_RUNS, MAX_QUEUED_ROOT_RUNS
from .pricing import compute_cost, get_budgets
from .storage import connect, json_dump, json_load, new_id, now_iso
from .workflow_engine import CHECKPOINT_PATH


DEFAULT_MONITOR_THRESHOLDS: dict[str, int | float] = {
    "cpu_warning_percent": 90,
    "memory_warning_percent": 88,
    "memory_critical_percent": 95,
    "vram_warning_percent": 88,
    "vram_critical_percent": 95,
    "gpu_temp_warning_c": 85,
    "sample_stale_warning_seconds": 20,
    "queue_wait_warning_seconds": 60,
    "queue_warning_percent": 80,
    "run_stale_warning_seconds": 900,
    "failure_warning_percent": 20,
    "failure_critical_percent": 50,
    "failure_minimum_runs": 5,
    "resource_consecutive_samples": 3,
}
_INTEGER_THRESHOLDS = {
    "gpu_temp_warning_c", "sample_stale_warning_seconds", "queue_wait_warning_seconds",
    "run_stale_warning_seconds", "failure_minimum_runs", "resource_consecutive_samples",
}


def get_monitor_thresholds() -> dict[str, int | float]:
    with connect() as db:
        rows = db.execute(
            "SELECT key,value FROM platform_settings WHERE key LIKE 'monitor_threshold.%'"
        ).fetchall()
    values: dict[str, int | float] = dict(DEFAULT_MONITOR_THRESHOLDS)
    for row in rows:
        key = row["key"].removeprefix("monitor_threshold.")
        if key not in DEFAULT_MONITOR_THRESHOLDS:
            continue
        try:
            parsed = float(row["value"])
            if math.isfinite(parsed):
                values[key] = int(parsed) if key in _INTEGER_THRESHOLDS else parsed
        except (TypeError, ValueError):
            continue
    return values


def save_monitor_thresholds(
    updates: dict[str, Any], *, reset: bool = False
) -> dict[str, int | float]:
    if reset:
        if updates:
            raise ValueError("恢复默认阈值时不能同时提交其他设置。")
        with connect() as db:
            db.execute("DELETE FROM platform_settings WHERE key LIKE 'monitor_threshold.%'")
        return dict(DEFAULT_MONITOR_THRESHOLDS)
    unknown = set(updates) - set(DEFAULT_MONITOR_THRESHOLDS)
    if unknown:
        raise ValueError("包含未知监控阈值：" + ", ".join(sorted(unknown)))
    values = get_monitor_thresholds()
    for key, raw in updates.items():
        if isinstance(raw, bool):
            raise ValueError(f"{key} 必须是数字。")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是数字。") from exc
        if not math.isfinite(number):
            raise ValueError(f"{key} 必须是有效数字。")
        if key in _INTEGER_THRESHOLDS and not number.is_integer():
            raise ValueError(f"{key} 必须是整数。")
        values[key] = int(number) if key in _INTEGER_THRESHOLDS else number
    ranges: dict[str, tuple[float, float]] = {
        "cpu_warning_percent": (50, 100),
        "memory_warning_percent": (50, 99),
        "memory_critical_percent": (51, 100),
        "vram_warning_percent": (50, 99),
        "vram_critical_percent": (51, 100),
        "gpu_temp_warning_c": (50, 110),
        "sample_stale_warning_seconds": (10, 300),
        "queue_wait_warning_seconds": (1, 3600),
        "queue_warning_percent": (10, 99),
        "run_stale_warning_seconds": (60, 7200),
        "failure_warning_percent": (1, 99),
        "failure_critical_percent": (2, 100),
        "failure_minimum_runs": (1, 100),
        "resource_consecutive_samples": (1, 12),
    }
    for key, (minimum, maximum) in ranges.items():
        if not minimum <= float(values[key]) <= maximum:
            raise ValueError(f"{key} 必须在 {minimum:g} 到 {maximum:g} 之间。")
    if values["memory_critical_percent"] <= values["memory_warning_percent"]:
        raise ValueError("系统内存严重阈值必须高于关注阈值。")
    if values["vram_critical_percent"] <= values["vram_warning_percent"]:
        raise ValueError("GPU 显存严重阈值必须高于关注阈值。")
    if values["failure_critical_percent"] <= values["failure_warning_percent"]:
        raise ValueError("失败率严重阈值必须高于关注阈值。")
    now = now_iso()
    with connect() as db:
        for key, value in values.items():
            db.execute(
                "INSERT INTO platform_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (f"monitor_threshold.{key}", str(value), now),
            )
    return values


def _gpu_metrics() -> dict[str, Any]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"name": None, "util_percent": None, "temp_c": None,
                "vram_used_bytes": None, "vram_total_bytes": None}
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        row = next(csv.reader(io.StringIO(result.stdout)))
        return {
            "name": row[0].strip(),
            "util_percent": float(row[1].strip()),
            "temp_c": float(row[2].strip()),
            "vram_used_bytes": int(float(row[3].strip()) * 1024 * 1024),
            "vram_total_bytes": int(float(row[4].strip()) * 1024 * 1024),
        }
    except (OSError, subprocess.SubprocessError, StopIteration, ValueError, IndexError):
        return {"name": None, "util_percent": None, "temp_c": None,
                "vram_used_bytes": None, "vram_total_bytes": None}


def record_system_sample(runtime_status: dict[str, Any] | None = None) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    try:
        process_rss = psutil.Process().memory_info().rss
    except (psutil.Error, OSError):
        process_rss = None
    gpu = _gpu_metrics()
    gguf = (runtime_status or {}).get("gguf_runtime", {})
    runtime_metrics = gguf.get("metrics", {}) if isinstance(gguf, dict) else {}
    sample = {
        "sampled_at": now_iso(),
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "memory_used_bytes": memory.used,
        "memory_total_bytes": memory.total,
        "process_rss_bytes": process_rss,
        "gpu_name": gpu["name"],
        "gpu_util_percent": gpu["util_percent"],
        "gpu_temp_c": gpu["temp_c"],
        "vram_used_bytes": gpu["vram_used_bytes"],
        "vram_total_bytes": gpu["vram_total_bytes"],
        "model_process_rss_bytes": gguf.get("process_rss_bytes") if isinstance(gguf, dict) else None,
        "model_process_cpu_percent": gguf.get("process_cpu_percent") if isinstance(gguf, dict) else None,
        "prompt_tokens_per_second": runtime_metrics.get("prompt_tokens_per_second"),
        "generation_tokens_per_second": runtime_metrics.get("generation_tokens_per_second"),
        "requests_processing": runtime_metrics.get("requests_processing"),
        "requests_deferred": runtime_metrics.get("requests_deferred"),
        "runtime_status": runtime_status or {},
    }
    thresholds = get_monitor_thresholds()
    with connect() as db:
        db.execute(
            """
            INSERT INTO system_metrics(
                sampled_at,cpu_percent,memory_used_bytes,memory_total_bytes,
                process_rss_bytes,gpu_name,gpu_util_percent,gpu_temp_c,
                vram_used_bytes,vram_total_bytes,model_process_rss_bytes,
                model_process_cpu_percent,prompt_tokens_per_second,
                generation_tokens_per_second,requests_processing,requests_deferred,
                runtime_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sample["sampled_at"], sample["cpu_percent"],
                sample["memory_used_bytes"], sample["memory_total_bytes"],
                sample["process_rss_bytes"], sample["gpu_name"],
                sample["gpu_util_percent"], sample["gpu_temp_c"],
                sample["vram_used_bytes"], sample["vram_total_bytes"],
                sample["model_process_rss_bytes"], sample["model_process_cpu_percent"],
                sample["prompt_tokens_per_second"], sample["generation_tokens_per_second"],
                sample["requests_processing"], sample["requests_deferred"],
                json_dump(sample["runtime_status"]),
            ),
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        db.execute("DELETE FROM system_metrics WHERE sampled_at < ?", (cutoff,))
        recent_rows = db.execute(
            "SELECT * FROM system_metrics ORDER BY id DESC LIMIT ?",
            (int(thresholds["resource_consecutive_samples"]),),
        ).fetchall()
    recent_samples = [_sample_dict(row) for row in reversed(recent_rows)]
    current_alerts = _monitor_alerts(recent_samples[-1], _run_summary(thresholds), recent_samples, thresholds)
    _sync_alert_events(current_alerts, sample["sampled_at"])
    return sample


def _sample_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["runtime_status"] = json_load(item.get("runtime_status"), {})
    return item


def _duration_ms(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _checkpoint_bytes() -> int:
    total = 0
    for path in (CHECKPOINT_PATH, CHECKPOINT_PATH.with_name(CHECKPOINT_PATH.name + "-wal"),
                 CHECKPOINT_PATH.with_name(CHECKPOINT_PATH.name + "-shm")):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def _run_summary(thresholds: dict[str, int | float] | None = None) -> dict[str, Any]:
    thresholds = thresholds or dict(DEFAULT_MONITOR_THRESHOLDS)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with connect() as db:
        rows = db.execute("SELECT status,COUNT(*) AS count FROM runs GROUP BY status").fetchall()
        root_rows = db.execute(
            "SELECT status,COUNT(*) AS count FROM runs WHERE parent_run_id IS NULL GROUP BY status"
        ).fetchall()
        live_run_rows = db.execute(
            """
            SELECT r.id,r.workflow_id,w.name AS workflow_name,r.status,r.current_node,
                   r.error,r.state,r.workflow_snapshot,r.created_at,r.started_at,r.updated_at,
                   (SELECT COUNT(*) FROM runs child WHERE child.parent_run_id=r.id) AS child_run_count,
                   (SELECT event_type FROM run_events e WHERE e.run_id=r.id ORDER BY e.id DESC LIMIT 1) AS last_event_type
            FROM runs r LEFT JOIN workflows w ON w.id=r.workflow_id
            WHERE r.parent_run_id IS NULL
              AND r.status IN ('queued','running','cancel_requested','waiting_approval','interrupted')
            ORDER BY CASE r.status
                WHEN 'running' THEN 0
                WHEN 'cancel_requested' THEN 1
                WHEN 'waiting_approval' THEN 2
                WHEN 'interrupted' THEN 3
                ELSE 4 END,
                CASE WHEN r.status='queued' THEN r.created_at ELSE COALESCE(r.updated_at,r.created_at) END ASC
            LIMIT 30
            """
        ).fetchall()
        recent = db.execute(
            """
            SELECT r.id,r.workflow_id,w.name AS workflow_name,r.parent_run_id,
                   r.parent_node_id,r.current_node,r.status,r.started_at,r.finished_at,
                   r.created_at,r.updated_at,r.error
            FROM runs r LEFT JOIN workflows w ON w.id=r.workflow_id
            WHERE r.created_at >= ? ORDER BY r.created_at DESC LIMIT 2000
            """,
            (since,),
        ).fetchall()
        node_events = db.execute(
            """
            SELECT e.run_id,e.node_id,e.event_type,e.payload,r.workflow_id,w.name AS workflow_name
            FROM run_events e
            JOIN runs r ON r.id=e.run_id
            LEFT JOIN workflows w ON w.id=r.workflow_id
            WHERE r.created_at >= ? AND e.event_type='node_completed'
            ORDER BY e.id DESC LIMIT 2000
            """,
            (since,),
        ).fetchall()
        event_rows = db.execute(
            """
            SELECT e.event_type,e.payload,e.node_id,e.run_id
            FROM run_events e JOIN runs r ON r.id=e.run_id
            WHERE r.created_at >= ? ORDER BY e.id DESC LIMIT 30000
            """,
            (since,),
        ).fetchall()
    statuses = {row["status"]: row["count"] for row in rows}
    root_statuses = {row["status"]: row["count"] for row in root_rows}
    live_runs = []
    stalled_runs = []
    now = datetime.now(timezone.utc)
    for row in live_run_rows:
        state = json_load(row["state"], {})
        snapshot = json_load(row["workflow_snapshot"], {})
        node_id = row["current_node"]
        node = next(
            (item for item in snapshot.get("nodes", []) if item.get("id") == node_id),
            {},
        )
        status = row["status"]
        age_source = (
            row["created_at"] if status == "queued" else
            row["updated_at"] if status in {"waiting_approval", "interrupted"} else
            row["started_at"] or row["created_at"]
        )
        try:
            age_seconds = max(0.0, (now - datetime.fromisoformat(age_source)).total_seconds())
        except (TypeError, ValueError):
            age_seconds = None
        try:
            progress_age_seconds = max(
                0.0, (now - datetime.fromisoformat(row["updated_at"])).total_seconds()
            )
        except (TypeError, ValueError):
            progress_age_seconds = None
        try:
            node_timeout_seconds = max(1, min(int(node.get("timeout", 600) or 600), 3600))
        except (TypeError, ValueError):
            node_timeout_seconds = 600
        stale_after_seconds = max(
            int(thresholds["run_stale_warning_seconds"]), node_timeout_seconds + 30
        )
        no_progress = (
            status in {"running", "cancel_requested"}
            and progress_age_seconds is not None
            and progress_age_seconds >= stale_after_seconds
        )
        live_run = {
            "run_id": row["id"],
            "workflow_id": row["workflow_id"],
            "workflow_name": row["workflow_name"] or "已删除的工作流",
            "status": status,
            "current_node": node_id,
            "node_label": node.get("label") or node_id or "等待调度",
            "step_count": int(state.get("step_count", 0) or 0),
            "child_run_count": int(row["child_run_count"] or 0),
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "progress_age_seconds": round(progress_age_seconds, 1) if progress_age_seconds is not None else None,
            "stale_after_seconds": stale_after_seconds,
            "no_progress": no_progress,
            "last_event_type": row["last_event_type"],
            "error": str(row["error"] or "")[:240],
            "updated_at": row["updated_at"],
        }
        live_runs.append(live_run)
        if no_progress:
            stalled_runs.append(live_run)
    recent_statuses: dict[str, int] = {}
    for row in recent:
        recent_statuses[row["status"]] = recent_statuses.get(row["status"], 0) + 1
    durations = [
        value for row in recent
        if (value := _duration_ms(row["started_at"], row["finished_at"])) is not None
    ]
    node_durations: list[float] = []
    by_node: dict[tuple[str, str, str], list[float]] = {}
    for row in node_events:
        payload = json_load(row["payload"], {})
        value = payload.get("duration_ms") if isinstance(payload, dict) else None
        if isinstance(value, (int, float)):
            duration = float(value)
            node_durations.append(duration)
            key = (row["workflow_name"] or "已删除的工作流", row["workflow_id"], row["node_id"] or "unknown")
            by_node.setdefault(key, []).append(duration)
    terminal_recent = recent_statuses.get("completed", 0) + recent_statuses.get("failed", 0)
    retries = 0
    tool_failures = 0
    resumes = 0
    approvals = 0
    model_calls = 0
    model_failures = 0
    model_usage: dict[str, dict[str, Any]] = {}
    for row in event_rows:
        event_type = row["event_type"]
        retries += event_type == "node_attempt_failed"
        tool_failures += event_type == "tool_failed"
        resumes += event_type == "run_resumed"
        approvals += event_type == "approval_requested"
        if event_type == "model_call_failed":
            model_failures += 1
        if event_type in {"model_call_completed", "model_call_failed"}:
            payload = json_load(row["payload"], {})
            model_name = str(payload.get("model") or "未知模型")
            item = model_usage.setdefault(model_name, {
                "model": model_name,
                "provider": payload.get("provider") or "unknown",
                "calls": 0,
                "failed": 0,
                "known_token_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "cost_priced": False,
                "latency_ms": [],
            })
            if event_type == "model_call_completed":
                model_calls += 1
                item["calls"] += 1
                prompt_tokens = payload.get("prompt_tokens")
                completion_tokens = payload.get("completion_tokens")
                if isinstance(prompt_tokens, (int, float)) and isinstance(completion_tokens, (int, float)):
                    item["known_token_calls"] += 1
                    item["prompt_tokens"] += int(prompt_tokens)
                    item["completion_tokens"] += int(completion_tokens)
                    item["total_tokens"] += int(payload.get("total_tokens") or prompt_tokens + completion_tokens)
                    cost = compute_cost(model_name, int(prompt_tokens), int(completion_tokens))
                    if cost is not None:
                        item["cost_usd"] = round(item["cost_usd"] + cost, 6)
                        item["cost_priced"] = True
            else:
                item["failed"] += 1
            latency = payload.get("latency_ms")
            if isinstance(latency, (int, float)):
                item["latency_ms"].append(float(latency))

    workflow_runs: dict[str, dict[str, Any]] = {}
    for row in recent:
        key = row["workflow_id"]
        item = workflow_runs.setdefault(key, {
            "workflow_id": key,
            "name": row["workflow_name"] or "已删除的工作流",
            "runs": 0,
            "completed": 0,
            "failed": 0,
            "durations": [],
        })
        item["runs"] += 1
        item["completed"] += row["status"] == "completed"
        item["failed"] += row["status"] == "failed"
        duration = _duration_ms(row["started_at"], row["finished_at"])
        if duration is not None:
            item["durations"].append(duration)

    by_workflow = []
    for item in workflow_runs.values():
        duration_values = item.pop("durations")
        item["median_elapsed_ms"] = round(statistics.median(duration_values), 1) if duration_values else None
        item["success_rate_percent"] = round(item["completed"] * 100 / (item["completed"] + item["failed"]), 1) if item["completed"] + item["failed"] else None
        by_workflow.append(item)
    by_workflow.sort(key=lambda item: (item["runs"], item["name"]), reverse=True)

    slow_nodes = [
        {
            "workflow_name": name,
            "workflow_id": workflow_id,
            "node_id": node_id,
            "samples": len(values),
            "average_ms": round(statistics.mean(values), 1),
            "p95_ms": round(_percentile(values, 0.95), 1),
        }
        for (name, workflow_id, node_id), values in by_node.items()
    ]
    slow_nodes.sort(key=lambda item: item["p95_ms"], reverse=True)
    slow_nodes = slow_nodes[:8]

    model_usage_rows = []
    for item in model_usage.values():
        latencies = item.pop("latency_ms")
        item["average_latency_ms"] = round(statistics.mean(latencies), 1) if latencies else None
        model_usage_rows.append(item)
    model_usage_rows.sort(key=lambda item: (item["total_tokens"], item["calls"], item["model"]), reverse=True)

    now = datetime.now(timezone.utc)
    queued_ages = []
    running_ages = []
    for row in recent:
        if row["status"] not in {"queued", "running", "cancel_requested"}:
            continue
        try:
            age = max(0.0, (now - datetime.fromisoformat(row["created_at"] if row["status"] == "queued" else row["started_at"] or row["created_at"])).total_seconds())
        except (TypeError, ValueError):
            continue
        if row["status"] == "queued":
            queued_ages.append(age)
        else:
            running_ages.append(age)

    recent_failures = [
        {
            "run_id": row["id"],
            "workflow_id": row["workflow_id"],
            "workflow_name": row["workflow_name"] or "已删除的工作流",
            "created_at": row["created_at"],
            "elapsed_ms": _duration_ms(row["started_at"], row["finished_at"]),
            "error": str(row["error"] or "运行失败")[:500],
        }
        for row in recent if row["status"] == "failed"
    ][:8]
    recent_activity = []
    for row in recent[:50]:
        status = row["status"]
        end_time = row["finished_at"]
        if status in {"running", "cancel_requested"}:
            end_time = now.isoformat()
        start_time = row["started_at"] or (row["created_at"] if status == "queued" else None)
        activity_age_source = (
            row["created_at"] if status == "queued" else
            row["updated_at"] if status in {"waiting_approval", "interrupted"} else
            row["started_at"] or row["created_at"]
        ) if status in {"queued", "running", "cancel_requested", "waiting_approval", "interrupted"} else None
        try:
            age_seconds = max(0.0, (now - datetime.fromisoformat(activity_age_source)).total_seconds())
        except (TypeError, ValueError):
            age_seconds = None
        recent_activity.append({
            "run_id": row["id"],
            "workflow_id": row["workflow_id"],
            "workflow_name": row["workflow_name"] or "已删除的工作流",
            "parent_run_id": row["parent_run_id"],
            "parent_node_id": row["parent_node_id"],
            "current_node": row["current_node"],
            "status": status,
            "created_at": row["created_at"],
            "elapsed_ms": _duration_ms(start_time, end_time),
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "error": str(row["error"] or "")[:240],
        })
    return {
        "by_status": statuses,
        "total": sum(statuses.values()),
        "active": sum(statuses.get(name, 0) for name in ("queued", "running", "cancel_requested")),
        "root_active": sum(root_statuses.get(name, 0) for name in ("queued", "running", "cancel_requested")),
        "root_running": root_statuses.get("running", 0) + root_statuses.get("cancel_requested", 0),
        "root_queued": root_statuses.get("queued", 0),
        "root_concurrency_limit": MAX_ACTIVE_ROOT_RUNS,
        "root_queue_limit": MAX_QUEUED_ROOT_RUNS,
        "waiting_approval": statuses.get("waiting_approval", 0),
        "root_waiting_approval": root_statuses.get("waiting_approval", 0),
        "live_runs": live_runs,
        "stalled_runs": stalled_runs,
        "last_24h": len(recent),
        "terminal_last_24h": terminal_recent,
        "success_rate_percent": round(recent_statuses.get("completed", 0) * 100 / terminal_recent, 1) if terminal_recent else None,
        "success_rate_window": "24h",
        "median_elapsed_ms": round(statistics.median(durations), 1) if durations else None,
        "p95_elapsed_ms": round(_percentile(durations, 0.95), 1) if durations else None,
        "average_node_ms": round(statistics.mean(node_durations), 1) if node_durations else None,
        "node_samples": len(node_durations),
        "failed_last_24h": recent_statuses.get("failed", 0),
        "retries_last_24h": retries,
        "tool_failures_last_24h": tool_failures,
        "approvals_requested_last_24h": approvals,
        "resumes_last_24h": resumes,
        "model_calls_last_24h": model_calls,
        "model_call_failures_last_24h": model_failures,
        "prompt_tokens_last_24h": sum(item["prompt_tokens"] for item in model_usage_rows),
        "completion_tokens_last_24h": sum(item["completion_tokens"] for item in model_usage_rows),
        "known_token_calls_last_24h": sum(item["known_token_calls"] for item in model_usage_rows),
        "cost_usd_last_24h": round(sum(item["cost_usd"] for item in model_usage_rows), 6),
        "local_tokens_last_24h": sum(
            item["total_tokens"] for item in model_usage_rows
            if str(item["provider"]).lower() in {"local", "gguf", "llamacpp"}
        ),
        "remote_tokens_last_24h": sum(
            item["total_tokens"] for item in model_usage_rows
            if str(item["provider"]).lower() not in {"local", "gguf", "llamacpp"}
        ),
        "budgets": get_budgets(),
        "models_usage_last_24h": model_usage_rows[:8],
        "oldest_queued_seconds": round(max(queued_ages), 1) if queued_ages else None,
        "longest_running_seconds": round(max(running_ages), 1) if running_ages else None,
        "recent_failures": recent_failures,
        "recent_activity": recent_activity,
        "slow_nodes": slow_nodes,
        "by_workflow": by_workflow[:8],
        "checkpoint_storage_bytes": _checkpoint_bytes(),
    }


def _monitor_alerts(
    latest: dict[str, Any] | None,
    runs: dict[str, Any],
    history: list[dict[str, Any]],
    thresholds: dict[str, int | float] | None = None,
) -> list[dict[str, str]]:
    thresholds = thresholds or dict(DEFAULT_MONITOR_THRESHOLDS)
    alerts: list[dict[str, str]] = []
    if not latest:
        alerts.append({"level": "warning", "title": "尚无资源采样", "detail": "等待监控采集器写入第一条系统遥测。"})
    else:
        sampled_at = latest.get("sampled_at")
        try:
            sample_age = (datetime.now(timezone.utc) - datetime.fromisoformat(sampled_at)).total_seconds()
            stale_limit = int(thresholds["sample_stale_warning_seconds"])
            if sample_age > stale_limit:
                alerts.append({"level": "warning", "title": "资源采样延迟", "detail": f"最近采样已是 {int(sample_age)} 秒前。"})
        except (TypeError, ValueError):
            pass
        recent = history[-int(thresholds["resource_consecutive_samples"]):]
        cpu_values = [sample.get("cpu_percent") for sample in recent]
        cpu = latest.get("cpu_percent")
        sample_count = int(thresholds["resource_consecutive_samples"])
        cpu_limit = float(thresholds["cpu_warning_percent"])
        if len(cpu_values) == sample_count and all(isinstance(value, (int, float)) and value >= cpu_limit for value in cpu_values):
            alerts.append({"level": "warning", "title": "CPU 持续高负载", "detail": f"连续 {sample_count} 次采样达到 {cpu_limit:.0f}% 以上，最近一次为 {cpu:.0f}%。"})
        for label, used_key, total_key in (
            ("系统内存", "memory_used_bytes", "memory_total_bytes"),
            ("GPU 显存", "vram_used_bytes", "vram_total_bytes"),
        ):
            used, total = latest.get(used_key), latest.get(total_key)
            if isinstance(used, (int, float)) and isinstance(total, (int, float)) and total > 0:
                ratio = used / total * 100
                recent_ratios = []
                for sample in recent:
                    sample_used, sample_total = sample.get(used_key), sample.get(total_key)
                    if isinstance(sample_used, (int, float)) and isinstance(sample_total, (int, float)) and sample_total > 0:
                        recent_ratios.append(sample_used / sample_total * 100)
                warning_limit = float(thresholds[f"{('memory' if used_key == 'memory_used_bytes' else 'vram')}_warning_percent"])
                critical_limit = float(thresholds[f"{('memory' if used_key == 'memory_used_bytes' else 'vram')}_critical_percent"])
                sustained = len(recent_ratios) == sample_count and min(recent_ratios) >= warning_limit
                if sustained and ratio >= critical_limit:
                    alerts.append({"level": "critical", "title": f"{label}持续接近耗尽", "detail": f"连续 {sample_count} 次采样高于 {warning_limit:.0f}%，当前已使用 {ratio:.0f}%，新模型或并发任务可能无法启动。"})
                elif sustained:
                    alerts.append({"level": "warning", "title": f"{label}余量持续偏低", "detail": f"连续 {sample_count} 次采样高于 {warning_limit:.0f}%，当前已使用 {ratio:.0f}%，建议减少上下文或卸载模型。"})
        temperature = latest.get("gpu_temp_c")
        temp_limit = int(thresholds["gpu_temp_warning_c"])
        if isinstance(temperature, (int, float)) and temperature >= temp_limit:
            alerts.append({"level": "warning", "title": "GPU 温度偏高", "detail": f"当前温度 {temperature:.0f}°C。"})

    queued_age = runs.get("oldest_queued_seconds")
    if isinstance(queued_age, (int, float)) and queued_age >= int(thresholds["queue_wait_warning_seconds"]):
        alerts.append({"level": "warning", "title": "Harness 队列等待", "detail": f"最早的任务已排队 {int(queued_age)} 秒。"})
    queued = int(runs.get("root_queued", 0) or 0)
    queue_limit = int(runs.get("root_queue_limit", 0) or 0)
    if queue_limit > 0 and queued >= queue_limit:
        alerts.append({"level": "critical", "title": "Harness 队列已满", "detail": f"顶层队列达到 {queued}/{queue_limit}；新运行请求会暂时返回 429。"})
    elif queue_limit > 0 and queued >= max(1, math.ceil(queue_limit * float(thresholds["queue_warning_percent"]) / 100)):
        alerts.append({"level": "warning", "title": "Harness 队列接近上限", "detail": f"顶层队列已使用 {queued}/{queue_limit} 个等待位置。"})
    stalled_runs = runs.get("stalled_runs", [])
    if stalled_runs:
        stalled = stalled_runs[0]
        seconds = int(stalled.get("progress_age_seconds") or 0)
        limit = int(stalled.get("stale_after_seconds") or thresholds["run_stale_warning_seconds"])
        alerts.append({
            "level": "warning",
            "title": "Harness 运行长时间无进展",
            "detail": (
                f"{len(stalled_runs)} 个运行超过当前节点超时与无进展阈值仍未更新 checkpoint；"
                f"例如 {str(stalled.get('workflow_name') or '工作流')[:80]} · "
                f"{str(stalled.get('node_label') or stalled.get('current_node') or '未知节点')[:80]}，"
                f"已 {seconds} 秒无进展（阈值 {limit} 秒）。"
            ),
        })
    terminal = int(runs.get("terminal_last_24h", 0))
    failed = int(runs.get("failed_last_24h", 0))
    failure_percent = failed / terminal * 100 if terminal else 0
    minimum_runs = int(thresholds["failure_minimum_runs"])
    warning_failure = float(thresholds["failure_warning_percent"])
    critical_failure = float(thresholds["failure_critical_percent"])
    if terminal >= minimum_runs and failure_percent >= critical_failure:
        alerts.append({"level": "critical", "title": "Harness 失败率严重", "detail": f"最近 24 小时失败 {failed}/{terminal} 次。"})
    elif terminal >= minimum_runs and failure_percent >= warning_failure:
        alerts.append({"level": "warning", "title": "Harness 失败率升高", "detail": f"最近 24 小时失败 {failed}/{terminal} 次。"})
    if not alerts:
        alerts.append({"level": "ok", "title": "资源与队列阈值正常", "detail": "系统采样、推理资源与 Harness 队列均未触发配置阈值。"})
    return alerts[:10]


def _sync_alert_events(
    alerts: list[dict[str, str]], observed_at: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active = [item for item in alerts if item.get("level") != "ok"]
    now = observed_at
    fingerprints = {
        hashlib.sha256(f"{item['level']}|{item['title']}".encode("utf-8")).hexdigest(): item
        for item in active
    }
    with connect() as db:
        open_rows = db.execute(
            "SELECT id,fingerprint FROM monitor_alerts WHERE status IN ('active','acknowledged')"
        ).fetchall()
        for row in open_rows:
            if row["fingerprint"] not in fingerprints:
                db.execute(
                    "UPDATE monitor_alerts SET status='resolved',resolved_at=? WHERE id=?",
                    (now, row["id"]),
                )
        for fingerprint, item in fingerprints.items():
            existing = db.execute(
                "SELECT id FROM monitor_alerts WHERE fingerprint=? AND status IN ('active','acknowledged')",
                (fingerprint,),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE monitor_alerts SET level=?,title=?,detail=?,last_seen=?,occurrences=occurrences+1 WHERE id=?",
                    (item["level"], item["title"], item["detail"], now, existing["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO monitor_alerts(id,fingerprint,level,title,detail,status,occurrences,first_seen,last_seen) VALUES(?,?,?,?,?,'active',1,?,?)",
                    (new_id("alert_"), fingerprint, item["level"], item["title"], item["detail"], now, now),
                )
        current_rows = db.execute(
            "SELECT * FROM monitor_alerts WHERE status IN ('active','acknowledged') ORDER BY last_seen DESC"
        ).fetchall()
        history_rows = db.execute(
            "SELECT * FROM monitor_alerts ORDER BY last_seen DESC LIMIT 24"
        ).fetchall()
    return [dict(row) for row in current_rows], [dict(row) for row in history_rows]


def _load_alert_events() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with connect() as db:
        current_rows = db.execute(
            "SELECT * FROM monitor_alerts WHERE status IN ('active','acknowledged') ORDER BY last_seen DESC"
        ).fetchall()
        history_rows = db.execute(
            "SELECT * FROM monitor_alerts ORDER BY last_seen DESC LIMIT 24"
        ).fetchall()
    return [dict(row) for row in current_rows], [dict(row) for row in history_rows]


def acknowledge_monitor_alert(alert_id: str) -> dict[str, Any] | None:
    now = now_iso()
    with connect() as db:
        changed = db.execute(
            "UPDATE monitor_alerts SET status='acknowledged',acknowledged_at=? WHERE id=? AND status='active'",
            (now, alert_id),
        ).rowcount
        if not changed:
            return None
        row = db.execute("SELECT * FROM monitor_alerts WHERE id=?", (alert_id,)).fetchone()
    return dict(row) if row else None


def get_monitoring_snapshot(window_minutes: int = 60) -> dict[str, Any]:
    window_minutes = max(5, min(int(window_minutes), 24 * 60))
    thresholds = get_monitor_thresholds()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM system_metrics WHERE sampled_at >= ? ORDER BY id DESC LIMIT 20000",
            (cutoff,),
        ).fetchall()
    history = [_sample_dict(row) for row in reversed(rows)]
    recent_samples = history[-int(thresholds["resource_consecutive_samples"]):]
    if len(history) > 720:
        step = (len(history) - 1) / 719
        history = [history[round(index * step)] for index in range(720)]
    latest = history[-1] if history else None
    runs = _run_summary(thresholds)
    dynamic_alerts = _monitor_alerts(latest, runs, recent_samples, thresholds)
    active_alerts, alert_history = _load_alert_events()
    current_by_fingerprint = {item["fingerprint"]: item for item in active_alerts}
    exposed_alerts = []
    for item in dynamic_alerts:
        if item.get("level") == "ok":
            exposed_alerts.append(item)
            continue
        fingerprint = hashlib.sha256(f"{item['level']}|{item['title']}".encode("utf-8")).hexdigest()
        exposed_alerts.append({**item, **current_by_fingerprint.get(fingerprint, {})})
    return {
        "generated_at": now_iso(),
        "window_minutes": window_minutes,
        "thresholds": thresholds,
        "latest": latest,
        "history": history,
        "runs": runs,
        "alerts": exposed_alerts,
        "alert_history": alert_history,
        "unacknowledged_alerts": sum(item["status"] == "active" for item in active_alerts),
    }
