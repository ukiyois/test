from __future__ import annotations

import platform
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .buildinfo import get_build_id
from .monitoring import get_monitoring_snapshot
from .storage import connect, json_load


_RESOURCE_FIELDS = (
    "sampled_at",
    "cpu_percent",
    "memory_used_bytes",
    "memory_total_bytes",
    "process_rss_bytes",
    "gpu_name",
    "gpu_util_percent",
    "gpu_temp_c",
    "vram_used_bytes",
    "vram_total_bytes",
    "model_process_rss_bytes",
    "model_process_cpu_percent",
    "prompt_tokens_per_second",
    "generation_tokens_per_second",
    "requests_processing",
    "requests_deferred",
)
_FAILURE_EVENTS = {
    "node_attempt_failed",
    "node_error_routed",
    "model_call_failed",
    "tool_failed",
    "parallel_branch_failed",
    "fanout_branch_failed",
    "subworkflow_failed",
    "run_failed",
}
_NUMERIC_EVENT_FIELDS = (
    "attempt",
    "max_attempts",
    "duration_ms",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
)


def _safe_text(value: Any, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b", "[REDACTED_KEY]", text)
    text = re.sub(r"(?i)\b(?:gh[pousr]|xox[baprs])-[A-Za-z0-9-]{12,}\b", "[REDACTED_KEY]", text)
    text = re.sub(r"\b(?:hf_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{30,})\b", "[REDACTED_KEY]", text)
    text = re.sub(r"(?i)(api[_-]?key|authorization)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)
    return text[:limit] or None


def _safe_location(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    module = _safe_text(value.get("module"), 120)
    if module:
        module = module.replace("\\", "/").rsplit("/", 1)[-1]
    line = value.get("line")
    return {
        "module": module,
        "function": _safe_text(value.get("function"), 120),
        "line": line if isinstance(line, int) and line >= 0 else None,
    }


def _safe_diagnostic(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    stack = []
    for frame in value.get("stack", [])[:12] if isinstance(value.get("stack"), list) else []:
        location = _safe_location(frame)
        if location:
            stack.append(location)
    causes = value.get("causes", [])
    return {
        "exception_type": _safe_text(value.get("exception_type"), 120),
        "location": _safe_location(value.get("location")),
        "stack": stack,
        "causes": [text for item in causes[:5] if (text := _safe_text(item, 120))]
        if isinstance(causes, list)
        else [],
    }


def _safe_failure_event(row: Any) -> dict[str, Any]:
    payload = json_load(row["payload"], {})
    if not isinstance(payload, dict):
        payload = {}
    error = payload.get("error")
    diagnostic = _safe_diagnostic(payload.get("diagnostic"))
    if not diagnostic and isinstance(error, dict):
        diagnostic = _safe_diagnostic(error.get("diagnostic"))
    error_info = payload.get("error_info")
    if not diagnostic and isinstance(error_info, dict):
        diagnostic = _safe_diagnostic(error_info.get("diagnostic"))

    event: dict[str, Any] = {
        "event_type": row["event_type"],
        "node_id": _safe_text(row["node_id"], 120),
        "created_at": row["created_at"],
        "diagnostic": diagnostic,
    }
    for key in _NUMERIC_EVENT_FIELDS:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            event[key] = value
    for key in ("model", "provider", "tool", "branch_id"):
        value = _safe_text(payload.get(key))
        if value:
            event[key] = value
    if isinstance(error, dict):
        error_type = _safe_text(error.get("type"), 120)
        if error_type:
            event["error_type"] = error_type
    elif diagnostic and diagnostic.get("exception_type"):
        event["error_type"] = diagnostic["exception_type"]
    return event


def _safe_runtime(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    gguf = value.get("gguf_runtime")
    if not isinstance(gguf, dict):
        gguf = {}
    metrics = gguf.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    safe_gguf = {
        key: gguf.get(key)
        for key in (
            "engine_installed",
            "model_installed",
            "model_alias",
            "model_size_bytes",
            "status",
            "running",
            "ready",
            "loading",
            "process_rss_bytes",
            "process_cpu_percent",
            "context_size",
            "gpu_policy",
        )
        if isinstance(gguf.get(key), (str, int, float, bool)) or gguf.get(key) is None
    }
    for key, item in tuple(safe_gguf.items()):
        if isinstance(item, str):
            safe_gguf[key] = _safe_text(item)
    safe_gguf["metrics"] = {
        key: metrics.get(key)
        for key in (
            "prompt_tokens_per_second",
            "generation_tokens_per_second",
            "requests_processing",
            "requests_deferred",
        )
        if isinstance(metrics.get(key), (int, float))
    }
    return {
        "active_model": _safe_text(value.get("active_model")),
        "force_cpu": bool(value.get("force_cpu")),
        "busy": bool(value.get("busy")),
        "external_backend": _safe_text(value.get("external_backend")),
        "gguf_runtime": safe_gguf,
    }


def _resource_sample(sample: Any) -> dict[str, Any]:
    if not isinstance(sample, dict):
        return {}
    return {key: sample.get(key) for key in _RESOURCE_FIELDS if key in sample}


def build_diagnostics_bundle() -> dict[str, Any]:
    """Build a bounded, privacy-filtered support bundle from local telemetry."""
    snapshot = get_monitoring_snapshot(24 * 60)
    history = snapshot.get("history") or []
    if len(history) > 288:
        step = (len(history) - 1) / 287
        history = [history[round(index * step)] for index in range(288)]

    recent_failures = (snapshot.get("runs") or {}).get("recent_failures") or []
    failed_run_ids = [str(item.get("run_id") or "") for item in recent_failures[:8] if item.get("run_id")]
    events_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if failed_run_ids:
        placeholders = ",".join("?" for _ in failed_run_ids)
        with connect() as db:
            rows = db.execute(
                "SELECT run_id,node_id,event_type,payload,created_at FROM run_events "
                f"WHERE run_id IN ({placeholders}) ORDER BY id",
                failed_run_ids,
            ).fetchall()
        for row in rows:
            if row["event_type"] in _FAILURE_EVENTS and len(events_by_run[row["run_id"]]) < 100:
                events_by_run[row["run_id"]].append(_safe_failure_event(row))

    failures = []
    for item in recent_failures[:8]:
        run_id = str(item.get("run_id") or "")
        failures.append(
            {
                "run_id": run_id,
                "workflow_id": _safe_text(item.get("workflow_id"), 120),
                "created_at": item.get("created_at"),
                "elapsed_ms": item.get("elapsed_ms"),
                "failure_events": events_by_run.get(run_id, []),
            }
        )

    runs = snapshot.get("runs") or {}
    run_fields = (
        "by_status",
        "total",
        "active",
        "root_active",
        "root_running",
        "root_queued",
        "root_concurrency_limit",
        "root_queue_limit",
        "waiting_approval",
        "root_waiting_approval",
        "last_24h",
        "terminal_last_24h",
        "success_rate_percent",
        "success_rate_window",
        "median_elapsed_ms",
        "p95_elapsed_ms",
        "average_node_ms",
        "node_samples",
        "failed_last_24h",
        "retries_last_24h",
        "tool_failures_last_24h",
        "approvals_requested_last_24h",
        "resumes_last_24h",
        "model_calls_last_24h",
        "model_call_failures_last_24h",
        "prompt_tokens_last_24h",
        "completion_tokens_last_24h",
        "known_token_calls_last_24h",
        "oldest_queued_seconds",
        "longest_running_seconds",
        "checkpoint_storage_bytes",
    )
    models = []
    for item in runs.get("models_usage_last_24h", [])[:12]:
        model_item = {
            key: item.get(key)
            for key in ("calls", "failed", "known_token_calls", "prompt_tokens", "completion_tokens", "average_latency_ms")
            if key in item
        }
        for key in ("model", "provider"):
            if safe_value := _safe_text(item.get(key)):
                model_item[key] = safe_value
        models.append(model_item)
    alert_fields = ("level", "title", "first_seen", "last_seen", "occurrences", "status")
    alerts = [
        {key: item.get(key) for key in alert_fields if key in item}
        for item in snapshot.get("alerts", [])[:24]
    ]
    runtime_status = (snapshot.get("latest") or {}).get("runtime_status")
    generated_at = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "privacy": {
            "included": "硬件采样、Harness 聚合、失败事件类型与异常定位帧。",
            "excluded": "任务输入、文件内容、提示词、模型输出、工具参数、API 密钥、服务路径和原始日志。",
        },
        "platform": {
            "application": "LLMmodol",
            "build_id": get_build_id(),
            "python_version": ".".join(str(part) for part in sys.version_info[:3]),
            "os": platform.system(),
            "os_release": platform.release(),
        },
        "runtime": _safe_runtime(runtime_status),
        "resources": {
            "window_minutes": 24 * 60,
            "latest": _resource_sample(snapshot.get("latest")),
            "samples": [_resource_sample(item) for item in history],
        },
        "harness": {
            "metrics": {key: runs.get(key) for key in run_fields if key in runs},
            "model_usage": models,
            "alerts": alerts,
            "recent_failures": failures,
        },
    }
