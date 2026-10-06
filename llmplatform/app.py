from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .harness import (
    RunQueueFullError,
    _model_context_length,
    cancel_run,
    close_workflow_engine,
    create_run,
    ensure_default_workflow,
    get_run,
    get_workflow,
    list_runs,
    list_workflows,
    initialize_workflow_engine,
    publish_workflow,
    recover_runs,
    record_event,
    resume_run,
    save_workflow,
    start_run_task,
    subscribe_run,
    unpublish_workflow,
    unsubscribe_run,
    workflow_diagnostics,
)
from .embeddings import embedding_runtime
from .context import (
    COMPACTION_PREFIX,
    build_compaction_request,
    count_messages_tokens,
    find_compaction_cut,
    render_history_for_compaction,
)
from .buildinfo import get_build_id
from .diagnostics_export import build_diagnostics_bundle
from .inference import ModelRuntimeError, local_runtime
from .llamacpp import MODEL_ALIAS, SERVER_URL as GGUF_SERVER_URL, llama_runtime
from .models import EXCLUDED_MODELS, list_gguf_models, list_local_models
from .monitoring import (
    acknowledge_monitor_alert,
    get_monitoring_snapshot,
    record_system_sample,
    save_monitor_thresholds,
)
from .providers import (
    delete_provider,
    discover_models,
    list_providers,
    remote_model_records,
    remote_embeddings,
    resolve_remote_model,
    save_provider,
)
from .pricing import get_budgets, get_pricing, set_budget, set_pricing
from .secrets import delete_secret, get_secret, set_secret
from .service import abort_stream, complete_model, split_model_ref, stream_model, usage_for_local
from .storage import (
    DATA_DIR,
    UPLOAD_DIR,
    connect,
    json_dump,
    json_load,
    new_id,
    now_iso,
)
from .tools import DANGEROUS_TOOLS, TOOL_CATALOG, execute_tool, tool_schemas


MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_CONTEXT_PER_FILE = 12000
PLATFORM_KEY_REF = "platform-api-key"
GGUF_PROVIDER_ID = "provider_qwen38_gguf_local"
MODEL_RUNTIME_SWITCH_LOCK = asyncio.Lock()
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# 对话 agent 模式：可用的自强化工具与最大迭代轮数
CHAT_AGENT_TOOLS = [
    "read_file", "write_file", "apply_patch", "run_command", "restart_service",
    "list_files", "grep_code", "list_backups", "agent_plan",
    "calculator", "current_time", "search_files", "semantic_search",
]
CHAT_AGENT_MAX_ITERATIONS = 12
# 对话 agent 上下文压缩：超过模型窗口预算比例时自动 compact，防止 prompt 滚雪球撞 max_tokens
CHAT_AGENT_CONTEXT_BUDGET_RATIO = 0.75
CHAT_AGENT_COMPACTION_KEEP_RECENT = 6
# 待审批的自强化操作，token -> 挂起的 asyncio.Future
_PENDING_APPROVALS: dict[str, asyncio.Future] = {}

# 对话影子 run：监控统计只 JOIN runs 表，对话必须落 run 锚点，模型调用事件才能进仪表盘。
_CHAT_WORKFLOW_NAME = "对话"
_CHAT_WORKFLOW_DEF = {
    "name": _CHAT_WORKFLOW_NAME,
    "description": "聊天页对话的影子运行记录，用于统计 token 与成本。",
    "nodes": [
        {"id": "start", "type": "start", "label": "开始", "next": "end"},
        {"id": "end", "type": "end", "label": "结束"},
    ],
}


def _ensure_chat_workflow() -> str:
    """返回"对话"影子工作流的 id，不存在则创建。"""
    with connect() as db:
        row = db.execute(
            "SELECT id FROM workflows WHERE name=?", (_CHAT_WORKFLOW_NAME,)
        ).fetchone()
        if row:
            return row["id"]
    created = save_workflow(dict(_CHAT_WORKFLOW_DEF))
    return created["id"]


def _open_chat_run(conversation_id: str, model: str, message: str, agent_mode: bool) -> str:
    """为一次对话创建影子 run（queued→running），返回 run_id。"""
    workflow_id = _ensure_chat_workflow()
    run_id = new_id("run_")
    now = now_iso()
    preview = message.strip().replace("\n", " ")[:60] or "（空）"
    tag = "助手" if agent_mode else "对话"
    with connect() as db:
        db.execute(
            """
            INSERT INTO runs(
                id,workflow_id,workflow_version,workflow_snapshot,status,input,state,
                current_node,pending,error,started_at,finished_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                workflow_id,
                1,
                json_dump(_CHAT_WORKFLOW_DEF),
                "running",
                json_dump({"input": f"[{tag}] {preview}", "conversation_id": conversation_id}),
                json_dump({"conversation_id": conversation_id, "model": model, "agent_mode": agent_mode}),
                "end",
                None,
                None,
                now,
                None,
                now,
                now,
            ),
        )
    return run_id


def _close_chat_run(run_id: str, ok: bool, error: str | None = None) -> None:
    """对话结束：标记影子 run 完成/失败。"""
    now = now_iso()
    with connect() as db:
        db.execute(
            "UPDATE runs SET status=?,error=?,finished_at=?,updated_at=? WHERE id=?",
            ("completed" if ok else "failed", error, now, now, run_id),
        )


def _record_chat_model_call(
    run_id: str,
    model: str,
    latency_ms: float,
    usage: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    """把一次对话模型调用写进 run_events，供监控聚合 token 与成本。"""
    source, _ = split_model_ref(model)
    payload: dict[str, Any] = {
        "model": model,
        "provider": source,
        "operation": "chat",
        "latency_ms": round(latency_ms, 2),
    }
    if usage:
        payload["prompt_tokens"] = usage.get("prompt_tokens")
        payload["completion_tokens"] = usage.get("completion_tokens")
        payload["total_tokens"] = usage.get("total_tokens")
    if error is not None:
        payload["error"] = error[:500]
        record_event(run_id, "end", "model_call_failed", payload)
    else:
        record_event(run_id, "end", "model_call_completed", payload)


# auto 审批下仍需人工确认的命令关键词（删库/强制推送/格式化等破坏性操作）
_AUTO_REJECT_COMMAND_KEYWORDS = (
    "rm -rf", "del /f", "rmdir /s", "format", "drop table", "drop database",
    "git push --force", "git push -f", "git reset --hard", "shutdown",
)


def _auto_approve(tool: str, args: dict[str, Any]) -> bool:
    """AI 审核：项目内的写/补丁/常规构建命令自动批准；破坏性操作仍转人工。

    沙箱本身已拦截项目外路径与受保护目录，这里只对"虽在沙箱内但语义危险"
    的操作保持谨慎。
    """
    if tool in {"write_file", "apply_patch"}:
        # 项目内代码/配置写入，沙箱已校验路径，自动批准
        return True
    if tool == "restart_service":
        return True
    if tool == "run_command":
        command = str(args.get("command") or "").strip().lower()
        if any(k in command for k in _AUTO_REJECT_COMMAND_KEYWORDS):
            return False  # 破坏性命令转人工
        return True  # 常规探查/构建命令自动批准
    return False


@asynccontextmanager
async def lifespan(_: FastAPI):
    from .storage import init_db

    backend = local_runtime.prepare_backend()
    if not backend["ready"]:
        logging.getLogger("llmplatform").warning(
            "Local inference runtime could not be initialized: %s", backend["error"]
        )
    local_runtime.set_external_server(llama_runtime)
    init_db()
    await initialize_workflow_engine()
    if (await asyncio.to_thread(llama_runtime.status))["running"]:
        await asyncio.to_thread(local_runtime.set_external_backend, "llama.cpp")
    ensure_default_workflow()
    await recover_runs()
    runtime_status = await _runtime_status_async()
    await asyncio.to_thread(record_system_sample, runtime_status)
    monitor_task = asyncio.create_task(_monitor_system())
    try:
        yield
    finally:
        monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await monitor_task
        await asyncio.to_thread(local_runtime.unload)
        await close_workflow_engine()


async def _monitor_system() -> None:
    while True:
        try:
            runtime_status = await _runtime_status_async()
            await asyncio.to_thread(record_system_sample, runtime_status)
        except Exception:
            logging.getLogger("llmplatform.monitoring").exception(
                "Could not collect a system metrics sample"
            )
        await asyncio.sleep(5)


def _runtime_status() -> dict[str, Any]:
    status = local_runtime.status()
    status["gguf_runtime"] = llama_runtime.status()
    active = status.get("active_model")
    status["model_id"] = active
    status["loaded"] = bool(active) and (
        bool(status.get("gguf_active")) or bool(status.get("placement"))
    )
    return status


async def _runtime_status_async() -> dict[str, Any]:
    """Collect process/runtime telemetry away from FastAPI's event loop."""
    return await asyncio.to_thread(_runtime_status)


app = FastAPI(
    title="LLMmodol · Local AI Studio",
    description="本地模型、远程模型 API、对话和 Agent 工作流平台。",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount(
    "/app-assets",
    StaticFiles(directory=WEB_DIR / "app-assets", check_dir=False),
    name="studio-app-assets",
)


class ProviderBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str | None = Field(default=None, max_length=2000)
    model_ids: list[str] | None = None


class ConversationBody(BaseModel):
    title: str | None = Field(default=None, max_length=100)
    model: str = "local/qwen3.5-0.8b"


class ChatBody(BaseModel):
    conversation_id: str
    model: str
    message: str = Field(min_length=1, max_length=100000)
    file_ids: list[str] = Field(default_factory=list)
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=0.9, ge=0, le=1)
    agent_mode: bool = False  # 助手模式：模型可调用自强化工具改代码并热更新
    approval_mode: str = "manual"  # 审批门：manual 手动 / auto AI 审核 / full 完全访问


class ChatApprovalBody(BaseModel):
    approval_token: str
    approved: bool


class WorkflowBody(BaseModel):
    definition: dict[str, Any]


class RunBody(BaseModel):
    input: str = Field(min_length=1, max_length=100000)
    file_ids: list[str] = Field(default_factory=list)


class AgentRunBody(RunBody):
    pass


class ResumeBody(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=5000)


class ChatCompletionBody(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=0.9, ge=0, le=1)
    stop: str | list[str] | None = None
    stream_options: dict[str, Any] | None = None


class EmbeddingBody(BaseModel):
    model: str
    input: str | list[str]


class MonitorThresholdBody(BaseModel):
    values: dict[str, Any] | None = None
    reset: bool = False


def _platform_key() -> str | None:
    return get_secret(PLATFORM_KEY_REF)


async def _check_api_auth(request: Request) -> None:
    configured = _platform_key()
    if not configured:
        return
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied, f"Bearer {configured}"):
        raise HTTPException(status_code=401, detail="API Key 无效。")


def _available_models() -> list[dict[str, Any]]:
    local = list_local_models() + list_gguf_models()
    remote = remote_model_records()
    return local + remote


def _get_file_records(file_ids: list[str]) -> list[dict[str, Any]]:
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    with connect() as db:
        rows = db.execute(
            f"SELECT id,filename,extracted_text FROM files WHERE id IN ({placeholders})",
            tuple(file_ids),
        ).fetchall()
    by_id = {row["id"]: row for row in rows}
    missing = [item for item in file_ids if item not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"找不到文件：{missing[0]}")
    return [
        {
            "id": item,
            "filename": by_id[item]["filename"],
            "text": by_id[item]["extracted_text"],
        }
        for item in file_ids
    ]


def _extract_file(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    text_suffixes = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log", ".py", ".html"}
    if suffix in text_suffixes:
        return data.decode("utf-8-sig", errors="replace")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            raise ValueError(f"PDF 文本提取失败：{exc}") from exc
    if suffix == ".docx":
        try:
            from docx import Document

            document = Document(io.BytesIO(data))
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        except Exception as exc:
            raise ValueError(f"DOCX 文本提取失败：{exc}") from exc
    raise ValueError("暂支持 TXT、Markdown、CSV、JSON、YAML、LOG、PY、HTML、PDF 和 DOCX。")


def _serialize_message(row: Any) -> dict[str, Any]:
    try:
        reasoning = row["reasoning"] or ""
    except (KeyError, IndexError):
        reasoning = ""
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "reasoning": reasoning,
        "model": row["model"],
        "file_ids": json_load(row["file_ids"], []),
        "created_at": row["created_at"],
    }


APP_PAGE = WEB_DIR / "app-assets" / "index.html"


def _spa_page() -> FileResponse:
    if not APP_PAGE.exists():
        raise HTTPException(status_code=503, detail="前端尚未构建，请先在 studio/ 目录执行 npm run build。")
    return FileResponse(APP_PAGE, headers={"Cache-Control": "no-store"})


@app.get("/")
async def home():
    return _spa_page()


@app.exception_handler(404)
async def spa_fallback(request, exc):
    """SPA history 路由回退：页面路由返回 index.html，API 路径保持 JSON 404。"""
    path = request.url.path
    if (
        request.method == "GET"
        and "text/html" in request.headers.get("accept", "")
        and not path.startswith(("/api/", "/v1/", "/app-assets", "/docs", "/redoc", "/openapi"))
    ):
        return _spa_page()
    return Response(
        content=json.dumps({"detail": getattr(exc, "detail", "资源不存在。")}, ensure_ascii=False),
        status_code=404,
        media_type="application/json",
    )


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "build_id": get_build_id(),
        "models": len([item for item in list_local_models() if item["available"]]),
        "runtime": await _runtime_status_async(),
    }


@app.get("/api/models")
async def models():
    return {
        "data": _available_models(),
        "excluded": EXCLUDED_MODELS,
        "runtime": await _runtime_status_async(),
    }


@app.post("/api/models/{model_id}/load")
async def load_model(model_id: str):
    if model_id.startswith("local/"):
        model_id = model_id.split("/", 1)[1]
    # GGUF 模型走 llama.cpp 引擎（自动 GPU 装配），不经 Transformers。
    if model_id.startswith("gguf/") or model_id.startswith("local/gguf/"):
        async with MODEL_RUNTIME_SWITCH_LOCK:
            await asyncio.to_thread(local_runtime.set_external_backend, "llama.cpp")
            try:
                await asyncio.to_thread(llama_runtime.start)
            except (RuntimeError, ValueError, OSError) as exc:
                await asyncio.to_thread(llama_runtime.stop)
                await asyncio.to_thread(local_runtime.set_external_backend, None)
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await _runtime_status_async()
    try:
        async with MODEL_RUNTIME_SWITCH_LOCK:
            if (await asyncio.to_thread(llama_runtime.status))["running"]:
                await asyncio.to_thread(llama_runtime.stop)
                await asyncio.to_thread(delete_provider, GGUF_PROVIDER_ID)
            await asyncio.to_thread(local_runtime.set_external_backend, None)
            await asyncio.to_thread(local_runtime.load, model_id)
    except AttributeError:
        raise HTTPException(status_code=501, detail="模型加载接口初始化失败。")
    except ModelRuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _runtime_status_async()


@app.post("/api/models/unload")
async def unload_model():
    await asyncio.to_thread(local_runtime.unload)
    return await _runtime_status_async()


@app.get("/api/runtime/gguf")
async def gguf_runtime_status():
    return await asyncio.to_thread(llama_runtime.status)


@app.post("/api/runtime/gguf/start")
async def start_gguf_runtime():
    async with MODEL_RUNTIME_SWITCH_LOCK:
        await asyncio.to_thread(local_runtime.set_external_backend, "llama.cpp")
        try:
            status = await asyncio.to_thread(llama_runtime.start)
            await asyncio.to_thread(
                save_provider,
                "Qwen3.8-27B · 本地 GGUF",
                GGUF_SERVER_URL + "/v1",
                "llmmod-local-runtime",
                GGUF_PROVIDER_ID,
                [MODEL_ALIAS],
            )
        except (RuntimeError, ValueError, OSError) as exc:
            await asyncio.to_thread(llama_runtime.stop)
            await asyncio.to_thread(local_runtime.set_external_backend, None)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            await asyncio.to_thread(llama_runtime.stop)
            await asyncio.to_thread(local_runtime.set_external_backend, None)
            raise
    return status


@app.post("/api/runtime/gguf/stop")
async def stop_gguf_runtime():
    async with MODEL_RUNTIME_SWITCH_LOCK:
        status = await asyncio.to_thread(llama_runtime.stop)
        await asyncio.to_thread(local_runtime.set_external_backend, None)
        await asyncio.to_thread(delete_provider, GGUF_PROVIDER_ID)
    return status


@app.get("/api/providers")
async def providers():
    return {"data": list_providers()}


@app.post("/api/providers")
async def create_provider(body: ProviderBody):
    try:
        provider = save_provider(
            body.name, body.base_url, body.api_key, model_ids=body.model_ids
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return provider


@app.put("/api/providers/{provider_id}")
async def update_provider(provider_id: str, body: ProviderBody):
    try:
        provider = save_provider(
            body.name,
            body.base_url,
            body.api_key,
            provider_id=provider_id,
            model_ids=body.model_ids,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return provider


@app.delete("/api/providers/{provider_id}")
async def remove_provider(provider_id: str):
    try:
        removed = delete_provider(provider_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="服务商不存在。")
    return {"deleted": True}


@app.post("/api/providers/{provider_id}/discover")
async def provider_discover(provider_id: str):
    try:
        found = await discover_models(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"读取模型列表失败：{exc}") from exc
    return {"model_ids": found}


@app.post("/api/providers/{provider_id}/test")
async def provider_test(provider_id: str):
    try:
        found = await discover_models(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"接口连接失败：{exc}") from exc
    return {"ok": True, "model_count": len(found), "model_ids": found}


@app.get("/api/system")
async def system_info():
    try:
        key_set = bool(_platform_key())
    except RuntimeError:
        key_set = False
    return {
        "title": "LLMmodol AI Studio",
        "api_base_url": "http://127.0.0.1:7860/v1",
        "openapi_url": "http://127.0.0.1:7860/docs",
        "api_key_configured": key_set,
        "runtime": await _runtime_status_async(),
    }


@app.get("/api/monitoring")
async def monitoring(window_minutes: int = 60):
    return await asyncio.to_thread(get_monitoring_snapshot, window_minutes)


@app.get("/api/monitoring/diagnostics")
async def monitoring_diagnostics():
    bundle = await asyncio.to_thread(build_diagnostics_bundle)
    filename = f"llmmodol-diagnostics-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json"
    return Response(
        content=json.dumps(bundle, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.put("/api/monitoring/thresholds")
async def update_monitor_thresholds(body: MonitorThresholdBody):
    try:
        thresholds = await asyncio.to_thread(
            save_monitor_thresholds, body.values or {}, reset=body.reset
        )
        return {"thresholds": thresholds}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/monitoring/alerts/{alert_id}/ack")
async def acknowledge_alert(alert_id: str):
    result = await asyncio.to_thread(acknowledge_monitor_alert, alert_id)
    if result is None:
        raise HTTPException(status_code=404, detail="告警不存在、已恢复或已经确认。")
    return result


@app.post("/api/system/api-key")
async def rotate_platform_key():
    value = "llmp_" + secrets.token_urlsafe(36)
    try:
        set_secret(PLATFORM_KEY_REF, value)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"api_key": value}


@app.delete("/api/system/api-key")
async def delete_platform_key():
    try:
        delete_secret(PLATFORM_KEY_REF)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"deleted": True}


@app.get("/api/conversations")
async def conversations():
    with connect() as db:
        rows = db.execute(
            "SELECT id,title,model,created_at,updated_at FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
    return {"data": [dict(row) for row in rows]}


@app.post("/api/conversations")
async def create_conversation(body: ConversationBody):
    conversation_id = new_id("chat_")
    now = now_iso()
    title = body.title or "新对话"
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,model,created_at,updated_at) VALUES(?,?,?,?,?)",
            (conversation_id, title, body.model, now, now),
        )
    return {
        "id": conversation_id,
        "title": title,
        "model": body.model,
        "created_at": now,
        "updated_at": now,
    }


@app.get("/api/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: str):
    with connect() as db:
        exists = db.execute(
            "SELECT id FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="对话不存在。")
        rows = db.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
    return {"data": [_serialize_message(row) for row in rows]}


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    with connect() as db:
        cursor = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="对话不存在。")
    return {"deleted": True}


@app.post("/api/files")
async def upload_file(file: UploadFile = File(...)):
    filename = Path(file.filename or "upload.txt").name[:240]
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="单个文件不能超过 15 MB。")
    try:
        extracted = _extract_file(filename, payload)
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    file_id = new_id("file_")
    safe_name = file_id + Path(filename).suffix.lower()
    target = UPLOAD_DIR / safe_name
    target.write_bytes(payload)
    with connect() as db:
        db.execute(
            """
            INSERT INTO files(id,filename,media_type,size,path,extracted_text,created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                file_id,
                filename,
                file.content_type or "application/octet-stream",
                len(payload),
                str(target),
                extracted,
                now_iso(),
            ),
        )
    return {
        "id": file_id,
        "filename": filename,
        "size": len(payload),
        "characters": len(extracted),
        "preview": extracted[:600],
    }


@app.get("/api/files/{file_id}")
async def file_info(file_id: str):
    with connect() as db:
        row = db.execute(
            "SELECT id,filename,media_type,size,extracted_text,created_at FROM files WHERE id=?",
            (file_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在。")
    return {
        "id": row["id"],
        "filename": row["filename"],
        "media_type": row["media_type"],
        "size": row["size"],
        "characters": len(row["extracted_text"]),
        "preview": row["extracted_text"][:1200],
        "created_at": row["created_at"],
    }


def _conversation_prompt(
    conversation_id: str, current_message: str, file_records: list[dict[str, Any]]
) -> list[dict[str, str]]:
    with connect() as db:
        rows = db.execute(
            "SELECT role,content FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT 24",
            (conversation_id,),
        ).fetchall()
    history = [
        {"role": row["role"], "content": row["content"]}
        for row in reversed(rows)
        if not (row["role"] == "assistant" and not row["content"])
    ]
    if not history or history[-1]["role"] != "user":
        history.append({"role": "user", "content": current_message})
    elif history[-1]["content"] != current_message:
        history.append({"role": "user", "content": current_message})
    if file_records:
        file_text = "\n\n".join(
            "文件：" + item["filename"] + "\n" + item["text"][:MAX_CONTEXT_PER_FILE]
            for item in file_records
        )
        history[-1]["content"] += "\n\n[本轮上传文件内容]\n" + file_text
    return history


CHAT_AGENT_SYSTEM = """你是 LLMmodol 平台的自强化编程助手，运行在项目代码库内，【已配备真实工具，不是只能聊天】。

## 你可用的工具（必须通过 tool_calls 调用，不要只在文字里描述）
- list_files：列出项目内目录/文件（自动跳过 .git/node_modules 等噪音目录），低成本探查结构
- grep_code：在项目源码中搜索关键词，返回文件/行号/片段，比 search_files 更适合查代码
- read_file：读取项目任意文件（带行号）
- write_file：整文件写入（危险，需审批）
- apply_patch：对文件做 search/replace 局部修改（危险，需审批）
- run_command：在项目根执行任意命令（ls、find、cat、python、npx vite build 等），用于探查/构建/验证（危险，需审批）
- restart_service：重启后端/前端服务使改动生效（危险，需审批）
- list_backups / agent_plan：列出补丁备份 / 获取推荐工作流
- search_files / semantic_search：按关键词或语义检索项目与上传文件
- calculator / current_time：计算与时间

## 强制行为准则
1. 涉及"读取/查看/修改/项目代码/文件/终端/架构"等需求时，【必须先调用工具】，禁止凭空回答"我不能"或只给建议。
2. 改代码前先用 read_file 或 search_files 读懂现状，不要猜测结构。
3. 优先用 apply_patch 做最小局部修改；仅整文件重写时才用 write_file。
4. 后端 Python 改动后调用 restart_service 生效；前端改动后先 run_command 执行 `npx vite build` 再提示用户刷新。
5. 写/删/执行类操作，系统会自动弹出审批请求，你只需正常发起 tool_call，由用户决定批准与否。
6. 项目根即当前工作区，路径用相对项目根（如 llmplatform/app.py、studio/src/views/ChatView.tsx）。
7. 每完成一步简要汇报结果；修改完成后总结改了哪些文件、如何验证。

## 重要
当用户问"你能不能修改这个项目"类问题时，正确做法是直接用 read_file/search_files 探查相关代码，然后用工具实际修改——而不是解释你"理论上能"。用你的工具证明。
"""


async def _chat_agent_loop(
    body: "ChatBody",
    messages: list[dict[str, Any]],
    files: list[dict[str, Any]],
    emit,
) -> tuple[str, str, dict[str, Any] | None]:
    """对话 agent 循环：模型工具调用 → 审批 → 执行 → 续写，直至产出最终回复。

    emit(event, data) 用于向前端推送工具调用/审批/进度事件。
    返回 (final_content, reasoning, usage)。
    """
    working = [{"role": "system", "content": CHAT_AGENT_SYSTEM}] + list(messages)
    schemas = tool_schemas(CHAT_AGENT_TOOLS)
    reasoning_parts: list[str] = []
    last_usage: dict[str, Any] | None = None

    def _accumulate(total: dict[str, Any] | None, piece: dict[str, Any]) -> dict[str, Any]:
        """agent 多轮调用：累加各轮 token，避免只记最后一轮导致成本少算。"""
        if total is None:
            total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = piece.get(key)
            if isinstance(value, (int, float)):
                total[key] = int(total.get(key, 0)) + int(value)
        return total

    # 上下文预算：超过模型窗口 75% 时自动 compact，防止 prompt 滚雪球撞 max_tokens
    context_length = _model_context_length(body.model)
    context_budget = int(context_length * CHAT_AGENT_CONTEXT_BUDGET_RATIO)
    last_prompt_tokens = 0

    for _ in range(CHAT_AGENT_MAX_ITERATIONS):
        # 压力检测：优先用上一轮模型返回的真实 prompt_tokens，否则用 tokenizer 计量
        pressure = last_prompt_tokens or count_messages_tokens(body.model, working)
        if pressure > context_budget:
            cut = find_compaction_cut(working, CHAT_AGENT_COMPACTION_KEEP_RECENT)
            if cut > 0:
                history_text = render_history_for_compaction(working[:cut])
                summary_messages = build_compaction_request(history_text)
                try:
                    compact_result = await complete_model(
                        body.model,
                        summary_messages,
                        max_tokens=1024,
                        temperature=0.0,
                    )
                    summary = str(compact_result.get("content", "")).strip()
                except Exception:  # noqa: BLE001 - 压缩失败不中断 agent
                    summary = ""
                if summary:
                    head = working[0] if working and working[0].get("role") == "system" else None
                    compacted: list[dict[str, Any]] = []
                    if head is not None:
                        compacted.append(head)
                    compacted.append({"role": "user", "content": COMPACTION_PREFIX + summary})
                    compacted.extend(working[cut:])
                    working = compacted
                    emit("context_compacted", {
                        "dropped_messages": cut,
                        "kept_messages": len(working) - (1 if head else 0) - 1,
                        "summary_chars": len(summary),
                    })
                    last_prompt_tokens = 0  # 重置，让下一轮重新计量

        result = await complete_model(
            body.model,
            working,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            tool_schemas=schemas,
        )
        if isinstance(result.get("usage"), dict):
            last_usage = _accumulate(last_usage, result["usage"])
            pt = result["usage"].get("prompt_tokens")
            if isinstance(pt, (int, float)):
                last_prompt_tokens = int(pt)
        if result.get("reasoning"):
            reasoning_parts.append(str(result["reasoning"]))
        tool_calls = result.get("tool_calls") or []
        content = str(result.get("content") or "")

        # 无工具调用：得到最终回复
        if not tool_calls:
            return content, "".join(reasoning_parts), last_usage

        # 记录 assistant 的工具调用回合（remote_complete 返回扁平 {id,name,arguments}）
        working.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": call.get("id") or f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": call.get("name"),
                            "arguments": call.get("arguments")
                            if isinstance(call.get("arguments"), str)
                            else json_dump(call.get("arguments") or {}),
                        },
                    }
                    for index, call in enumerate(tool_calls)
                ],
            }
        )

        for index, call in enumerate(tool_calls):
            call_id = call.get("id") or f"call_{index}"
            name = call.get("name")
            raw_args = call.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            emit("tool_call", {"tool": name, "arguments": args})

            # 危险操作：三级审批门（manual 手动 / auto AI 审核 / full 完全访问）
            if name in DANGEROUS_TOOLS:
                approved: bool
                if body.approval_mode == "full":
                    # 完全访问：直接放行（沙箱仍拦截项目外路径）
                    approved = True
                    emit("approval_resolved", {"tool": name, "approved": True, "mode": "full"})
                elif body.approval_mode == "auto" and _auto_approve(name, args):
                    # AI 审核通过：项目内安全操作自动批准
                    approved = True
                    emit("approval_resolved", {"tool": name, "approved": True, "mode": "auto"})
                else:
                    # 手动审批（或 auto 审核未通过转人工）
                    token = new_id("appr_")
                    loop = asyncio.get_running_loop()
                    future: asyncio.Future = loop.create_future()
                    _PENDING_APPROVALS[token] = future
                    emit(
                        "approval_required",
                        {"approval_token": token, "tool": name, "arguments": args},
                    )
                    try:
                        approved = await asyncio.wait_for(future, timeout=600)
                    except asyncio.TimeoutError:
                        approved = False
                    finally:
                        _PENDING_APPROVALS.pop(token, None)
                    emit("approval_resolved", {"tool": name, "approved": approved, "mode": "manual"})
                if not approved:
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json_dump(
                                {"denied": True, "note": "用户拒绝了该操作。"}
                            ),
                        }
                    )
                    continue

            try:
                result_data = await execute_tool(name, args, files)
                payload = result_data if isinstance(result_data, dict) else {"result": result_data}
                emit("tool_result", {"tool": name, "ok": True, "result": payload})
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json_dump(payload)[:6000],
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 工具失败回报给模型
                emit("tool_result", {"tool": name, "ok": False, "error": str(exc)})
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json_dump({"error": str(exc)}),
                    }
                )

    # 迭代耗尽：让模型给阶段性总结
    working.append(
        {"role": "user", "content": "已达最大操作轮数，请总结目前进展与后续步骤。"}
    )
    result = await complete_model(
        body.model, working, max_tokens=body.max_tokens, temperature=body.temperature
    )
    if isinstance(result.get("usage"), dict):
        last_usage = _accumulate(last_usage, result["usage"])
    return (
        str(result.get("content") or "（已达到操作轮数上限）"),
        "".join(reasoning_parts),
        last_usage,
    )


@app.post("/api/chat/approve")
async def chat_approve(body: ChatApprovalBody):
    future = _PENDING_APPROVALS.get(body.approval_token)
    if future is None or future.done():
        raise HTTPException(status_code=404, detail="审批已失效或不存在。")
    future.set_result(bool(body.approved))
    return {"ok": True, "approved": bool(body.approved)}


class PricingBody(BaseModel):
    model_key: str
    prompt: float = Field(ge=0)
    completion: float = Field(ge=0)


class BudgetBody(BaseModel):
    period: str  # daily_usd / monthly_usd
    amount_usd: float = Field(ge=0)


@app.get("/api/pricing")
def api_get_pricing():
    return {"pricing": get_pricing(), "budgets": get_budgets()}


@app.post("/api/pricing")
def api_set_pricing(body: PricingBody):
    set_pricing(body.model_key, body.prompt, body.completion)
    return {"ok": True}


@app.post("/api/budget")
def api_set_budget(body: BudgetBody):
    if body.period not in {"daily_usd", "monthly_usd"}:
        raise HTTPException(status_code=400, detail="period 必须是 daily_usd 或 monthly_usd。")
    set_budget(body.period, body.amount_usd)
    return {"ok": True}


@app.post("/api/chat/stream")
async def chat_stream(body: ChatBody):
    with connect() as db:
        conversation = db.execute(
            "SELECT * FROM conversations WHERE id=?", (body.conversation_id,)
        ).fetchone()
    if not conversation:
        raise HTTPException(status_code=404, detail="对话不存在。")
    files = _get_file_records(body.file_ids)
    now = now_iso()
    user_message_id = new_id("msg_")
    assistant_message_id = new_id("msg_")
    title = conversation["title"]
    with connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
            (body.conversation_id,),
        ).fetchone()[0]
        if count == 0 and title == "新对话":
            title = body.message.strip().replace("\n", " ")[:36] or "新对话"
        db.execute(
            "INSERT INTO messages(id,conversation_id,role,content,model,file_ids,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                user_message_id,
                body.conversation_id,
                "user",
                body.message,
                body.model,
                json_dump(body.file_ids),
                now,
            ),
        )
        db.execute(
            "INSERT INTO messages(id,conversation_id,role,content,model,file_ids,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                assistant_message_id,
                body.conversation_id,
                "assistant",
                "",
                body.model,
                "[]",
                now_iso(),
            ),
        )
        db.execute(
            "UPDATE conversations SET title=?,model=?,updated_at=? WHERE id=?",
            (title, body.model, now_iso(), body.conversation_id),
        )

    messages = _conversation_prompt(body.conversation_id, body.message, files)
    # 影子 run：让这次对话的 token/成本进监控统计
    chat_run_id = _open_chat_run(body.conversation_id, body.model, body.message, body.agent_mode)

    async def stream():
        collected: list[str] = []
        thinking: list[str] = []
        finish_reason = "stop"
        usage: dict[str, Any] | None = None
        started = time.monotonic()
        yield "event: start\ndata: " + json_dump(
            {"message_id": assistant_message_id, "conversation_id": body.conversation_id}
        ) + "\n\n"
        try:
            if body.agent_mode:
                # 助手模式：进入工具调用循环，模型可读写代码并热更新
                def emit(event: str, data: dict[str, Any]) -> None:
                    events.append((event, data))

                events: list[tuple[str, dict[str, Any]]] = []

                async def _drain():
                    while events:
                        ev, data = events.pop(0)
                        yield "event: " + ev + "\ndata: " + json_dump(data) + "\n\n"

                # 逐步执行 agent 循环，交替冲刷事件队列
                loop = asyncio.get_running_loop()
                agent_task = loop.create_task(
                    _chat_agent_loop(body, messages, files, emit)
                )
                while not agent_task.done():
                    async for chunk in _drain():
                        yield chunk
                    await asyncio.sleep(0.05)
                async for chunk in _drain():
                    yield chunk
                final, thinking_text, usage = agent_task.result()
                finish_reason = "stop"
            else:
                async for piece in stream_model(
                    body.model, messages, body.max_tokens, body.temperature, body.top_p
                ):
                    if isinstance(piece, dict):
                        # 类型化 piece：推理增量 / 结束原因 / 用量
                        if "reasoning" in piece:
                            text = str(piece["reasoning"])
                            thinking.append(text)
                            yield "event: reasoning\ndata: " + json_dump({"text": text}) + "\n\n"
                        elif "usage" in piece:
                            usage = piece["usage"]
                        else:
                            finish_reason = str(piece.get("finish_reason") or "stop")
                        continue
                    collected.append(str(piece))
                    yield "event: delta\ndata: " + json_dump({"text": piece}) + "\n\n"
                final = "".join(collected)
                thinking_text = "".join(thinking)
            with connect() as db:
                db.execute(
                    "UPDATE messages SET content=?, reasoning=? WHERE id=?",
                    (final, thinking_text, assistant_message_id),
                )
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if usage is None:
                # 本地模型：估算用量；远程模型流末已带真实用量
                try:
                    usage = await usage_for_local(messages, final)
                except Exception:
                    usage = None
            # 影子 run：记录模型调用并关闭，token/成本进监控
            _record_chat_model_call(chat_run_id, body.model, float(elapsed_ms), usage)
            _close_chat_run(chat_run_id, ok=True)
            yield "event: done\ndata: " + json_dump(
                {
                    "message_id": assistant_message_id,
                    "content": final,
                    "reasoning": thinking_text,
                    "finish_reason": finish_reason,
                    "elapsed_ms": elapsed_ms,
                    "usage": usage,
                    "agent_mode": body.agent_mode,
                }
            ) + "\n\n"
        except Exception as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            _record_chat_model_call(chat_run_id, body.model, float(elapsed_ms), None, error=str(exc))
            _close_chat_run(chat_run_id, ok=False, error=str(exc)[:500])
            with connect() as db:
                db.execute(
                    "UPDATE messages SET content=? WHERE id=?",
                    ("[调用失败] " + str(exc), assistant_message_id),
                )
            yield "event: error\ndata: " + json_dump({"message": str(exc)}) + "\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ChatAbortRequest(BaseModel):
    model: str


@app.post("/api/chat/abort")
async def chat_abort(body: ChatAbortRequest):
    return {"aborted": await abort_stream(body.model)}


@app.get("/api/workflows")
async def workflows():
    return {"data": list_workflows(), "tools": TOOL_CATALOG}


@app.post("/api/workflows")
async def create_workflow(body: WorkflowBody):
    try:
        return save_workflow(body.definition)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/workflows/{workflow_id}")
async def update_workflow(workflow_id: str, body: WorkflowBody):
    try:
        return save_workflow(body.definition, workflow_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/workflows/validate")
async def validate_workflow_draft(body: WorkflowBody):
    return workflow_diagnostics(body.definition)


@app.post("/api/workflows/{workflow_id}/publish")
async def workflow_publish(workflow_id: str):
    try:
        workflow = publish_workflow(workflow_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not workflow:
        raise HTTPException(status_code=404, detail="工作流不存在。")
    return workflow


@app.post("/api/workflows/{workflow_id}/unpublish")
async def workflow_unpublish(workflow_id: str):
    workflow = unpublish_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="工作流不存在。")
    return workflow


@app.post("/api/workflows/{workflow_id}/runs")
async def run_workflow(workflow_id: str, body: RunBody):
    _get_file_records(body.file_ids)
    try:
        run_id = create_run(workflow_id, body.input, body.file_ids)
    except RunQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    start_run_task(run_id)
    return {"run_id": run_id, "status": "queued"}


@app.post("/api/deployed-agents/{workflow_id}/runs")
async def run_deployed_agent_preview(workflow_id: str, body: AgentRunBody):
    """Run the immutable published snapshot from the local Studio preview."""
    workflow = get_workflow(workflow_id)
    if not workflow or int(workflow.get("published_version", 0)) < 1:
        raise HTTPException(status_code=404, detail="已发布的 Agent 不存在。")
    _get_file_records(body.file_ids)
    try:
        run_id = create_run(
            workflow_id,
            body.input,
            body.file_ids,
            definition_snapshot=workflow["published_definition"],
            workflow_version=workflow["published_version"],
        )
    except RunQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    start_run_task(run_id)
    return {
        "run_id": run_id,
        "status": "queued",
        "workflow_version": workflow["published_version"],
    }


@app.get("/api/runs")
async def runs(limit: int = 50):
    return {"data": list_runs(limit)}


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行记录不存在。")
    return run


@app.get("/api/runs/{run_id}/events")
async def run_events_stream(run_id: str):
    """Live SSE feed of a run's status changes and node events."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行记录不存在。")
    queue, _ = subscribe_run(run_id)

    async def stream():
        try:
            # Kick off with the current snapshot so late joiners get state.
            yield "event: snapshot\ndata: " + json_dump(
                {
                    "status": run["status"],
                    "current_node": run["current_node"],
                    "pending": run["pending"],
                    "error": run["error"],
                    "events": run["events"],
                }
            ) + "\n\n"
            terminal = {"completed", "failed", "cancelled"}
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                kind = message.pop("kind", "event")
                yield f"event: {kind}\ndata: " + json_dump(message) + "\n\n"
                if kind == "status" and message.get("status") in terminal:
                    break
        finally:
            unsubscribe_run(run_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/runs/{run_id}/resume")
async def run_resume(run_id: str, body: ResumeBody):
    try:
        await resume_run(run_id, body.approved, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_id": run_id, "status": "queued"}


@app.post("/api/runs/{run_id}/cancel")
async def run_cancel(run_id: str):
    cancelled = await cancel_run(run_id)
    if not cancelled:
        run = get_run(run_id)
        parent = get_run(run["parent_run_id"]) if run and run.get("parent_run_id") else None
        if parent and parent["status"] in {"queued", "running", "waiting_approval"}:
            raise HTTPException(status_code=409, detail="此子运行由父工作流统一管理，请取消父运行。")
        raise HTTPException(status_code=409, detail="此运行当前不能取消。")
    updated = get_run(run_id)
    return {"run_id": run_id, "status": updated["status"] if updated else "cancel_requested"}


@app.get("/v1/models")
async def openai_models(request: Request):
    await _check_api_auth(request)
    records = [
        item
        for item in _available_models()
        if item.get("available") and item["kind"] in {"chat", "text2text"}
    ]
    return {
        "object": "list",
        "data": [
            {
                "id": item["id"] if item["source"] == "remote" else "local/" + item["id"],
                "object": "model",
                "owned_by": item["source"],
            }
            for item in records
        ],
    }


@app.get("/v1/agents")
async def deployed_agents(request: Request):
    """List published workflow agents without exposing their private definitions."""
    await _check_api_auth(request)
    agents = [
        {
            "id": item["id"],
            "name": item["name"],
            "description": item["description"],
            "version": item["published_version"],
            "created_at": item["published_at"],
            "runs_url": f"/v1/agents/{item['id']}/runs",
        }
        for item in list_workflows()
        if int(item.get("published_version", 0)) > 0
    ]
    return {"object": "list", "data": agents}


@app.post("/v1/agents/{workflow_id}/runs")
async def create_deployed_agent_run(
    workflow_id: str, body: AgentRunBody, request: Request
):
    """Start an asynchronous run against the immutable published workflow snapshot."""
    await _check_api_auth(request)
    workflow = get_workflow(workflow_id)
    if not workflow or int(workflow.get("published_version", 0)) < 1:
        raise HTTPException(status_code=404, detail="已发布的 Agent 不存在。")
    _get_file_records(body.file_ids)
    try:
        run_id = create_run(
            workflow_id,
            body.input,
            body.file_ids,
            definition_snapshot=workflow["published_definition"],
            workflow_version=workflow["published_version"],
        )
    except RunQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    start_run_task(run_id)
    return {
        "id": run_id,
        "object": "agent.run",
        "agent_id": workflow_id,
        "agent_version": workflow["published_version"],
        "status": "queued",
        "status_url": f"/v1/agent-runs/{run_id}",
    }


@app.get("/v1/agent-runs/{run_id}")
async def deployed_agent_run(run_id: str, request: Request):
    """Read a deployed Agent run without returning its stored prompt definition."""
    await _check_api_auth(request)
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent 运行记录不存在。")
    return {
        "id": run["id"],
        "object": "agent.run",
        "agent_id": run["workflow_id"],
        "agent_version": run["workflow_version"],
        "status": run["status"],
        "current_node": run["current_node"],
        "output": run["state"].get("result") if run["status"] == "completed" else None,
        "error": run["error"],
        "pending": run["pending"],
        "events": run["events"],
    }


@app.post("/v1/files")
async def upload_file_for_api(request: Request, file: UploadFile = File(...)):
    await _check_api_auth(request)
    return await upload_file(file)


def _openai_chunk(
    model: str,
    completion_id: str,
    created: int,
    content: str | None = None,
    finish: str | None = None,
    role: str | None = None,
):
    delta: dict[str, str] = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish,
            }
        ],
    }


@app.post("/v1/chat/completions")
async def openai_chat(body: ChatCompletionBody, request: Request):
    await _check_api_auth(request)
    if not body.messages:
        raise HTTPException(status_code=422, detail="messages 不能为空。")
    include_usage = bool((body.stream_options or {}).get("include_usage"))

    def split_piece(piece: Any) -> tuple[str | None, str | None]:
        """Separate a text chunk from the trailing finish_reason sentinel."""
        if isinstance(piece, dict):
            return None, str(piece.get("finish_reason") or "stop")
        return str(piece), None

    if not body.stream:
        chunks: list[str] = []
        finish_reason = "stop"
        try:
            async for piece in stream_model(
                body.model,
                body.messages,
                body.max_tokens,
                body.temperature,
                body.top_p,
                stop=body.stop,
            ):
                text, reason = split_piece(piece)
                if reason:
                    finish_reason = reason
                if text:
                    chunks.append(text)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        content = "".join(chunks)
        usage = None
        if split_model_ref(body.model)[0] == "local":
            usage = await usage_for_local(body.messages, content)
        return {
            "id": "chatcmpl-" + secrets.token_hex(12),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }

    async def stream():
        completion_id = "chatcmpl-" + secrets.token_hex(12)
        created = int(time.time())
        collected: list[str] = []
        finish_reason = "stop"
        try:
            yield "data: " + json.dumps(
                _openai_chunk(
                    body.model, completion_id, created, role="assistant"
                ),
                ensure_ascii=False,
            ) + "\n\n"
            async for piece in stream_model(
                body.model,
                body.messages,
                body.max_tokens,
                body.temperature,
                body.top_p,
                stop=body.stop,
            ):
                text, reason = split_piece(piece)
                if reason:
                    finish_reason = reason
                if text:
                    collected.append(text)
                    yield "data: " + json.dumps(
                        _openai_chunk(
                            body.model, completion_id, created, content=text
                        ),
                        ensure_ascii=False,
                    ) + "\n\n"
            yield "data: " + json.dumps(
                _openai_chunk(
                    body.model, completion_id, created, finish=finish_reason
                ),
                ensure_ascii=False,
            ) + "\n\n"
            if include_usage:
                usage = None
                if split_model_ref(body.model)[0] == "local":
                    usage = await usage_for_local(body.messages, "".join(collected))
                frame = _openai_chunk(body.model, completion_id, created)
                frame["choices"] = []
                frame["usage"] = usage
                yield "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            error = {"error": {"message": str(exc), "type": "model_error"}}
            yield "data: " + json.dumps(error, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ChatAbortBody(BaseModel):
    model: str


@app.post("/v1/chat/abort")
async def openai_chat_abort(body: ChatAbortBody, request: Request):
    await _check_api_auth(request)
    return {"aborted": await abort_stream(body.model)}


@app.post("/v1/embeddings")
async def openai_embeddings(body: EmbeddingBody, request: Request):
    await _check_api_auth(request)
    inputs = [body.input] if isinstance(body.input, str) else body.input
    if not inputs or any(not isinstance(item, str) for item in inputs):
        raise HTTPException(status_code=422, detail="input 必须是文本或文本数组。")
    if any(len(item) > 100000 for item in inputs):
        raise HTTPException(status_code=413, detail="单段嵌入文本不能超过 100000 字符。")
    if body.model.startswith("remote/"):
        try:
            provider, remote_id = resolve_remote_model(body.model)
            vectors = await remote_embeddings(provider, remote_id, inputs)
            tokens = 0
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    else:
        model_id = body.model.split("/", 1)[1] if body.model.startswith("local/") else body.model
        try:
            vectors, tokens = await asyncio.to_thread(
                embedding_runtime.encode, model_id, inputs
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "model": body.model,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
