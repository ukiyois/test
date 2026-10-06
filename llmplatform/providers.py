from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from .secrets import get_secret
from .storage import connect, json_dump, json_load, new_id, now_iso


def normalize_base_url(url: str) -> str:
    value = url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if value.lower().endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.rstrip("/")


def _provider_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "model_ids": json_load(row["model_ids"], []),
        "api_key_set": bool(row.get("secret_ref")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_providers() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM providers ORDER BY created_at").fetchall()
    return [_provider_public(dict(row)) for row in rows]


def get_provider(provider_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
    return dict(row) if row else None


def save_provider(
    name: str,
    base_url: str,
    api_key: str | None,
    provider_id: str | None = None,
    model_ids: list[str] | None = None,
) -> dict[str, Any]:
    provider_id = provider_id or new_id("provider_")
    now = now_iso()
    existing = get_provider(provider_id)
    secret_ref = f"provider:{provider_id}"
    normalized = normalize_base_url(base_url)
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("API URL 必须以 http:// 或 https:// 开头。")
    if api_key:
        from .secrets import set_secret

        set_secret(secret_ref, api_key.strip())
    elif not existing:
        secret_ref = ""
    else:
        secret_ref = existing["secret_ref"]

    old_ids = json_load(existing["model_ids"], []) if existing else []
    ids = model_ids if model_ids is not None else old_ids
    with connect() as db:
        db.execute(
            """
            INSERT INTO providers(id,name,base_url,secret_ref,model_ids,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, base_url=excluded.base_url,
              secret_ref=excluded.secret_ref, model_ids=excluded.model_ids,
              updated_at=excluded.updated_at
            """,
            (
                provider_id,
                name.strip(),
                normalized,
                secret_ref,
                json_dump(ids),
                existing["created_at"] if existing else now,
                now,
            ),
        )
    return _provider_public(get_provider(provider_id) or {})


def delete_provider(provider_id: str) -> bool:
    provider = get_provider(provider_id)
    if not provider:
        return False
    if provider.get("secret_ref"):
        from .secrets import delete_secret

        delete_secret(provider["secret_ref"])
    with connect() as db:
        db.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
    return True


def make_client(provider: dict[str, Any]) -> AsyncOpenAI:
    reference = provider.get("secret_ref")
    secret = get_secret(reference) if reference else None
    if not secret:
        raise RuntimeError("此服务商还没有保存 API Key。")
    return AsyncOpenAI(
        api_key=secret,
        base_url=provider["base_url"],
        timeout=60,
        max_retries=1,
    )


async def discover_models(provider_id: str) -> list[str]:
    provider = get_provider(provider_id)
    if not provider:
        raise ValueError("服务商不存在。")
    client = make_client(provider)
    response = await client.models.list()
    ids = sorted({item.id for item in response.data})
    with connect() as db:
        db.execute(
            "UPDATE providers SET model_ids=?, updated_at=? WHERE id=?",
            (json_dump(ids), now_iso(), provider_id),
        )
    return ids


# 远程模型上下文窗口（token），供 agent 预算与前端上下文油表使用
_REMOTE_CONTEXT_LENGTH = {
    "deepseek-flash": 131072,
    "deepseek-v4-pro": 131072,
    "deepseek-chat": 131072,
    "deepseek-reasoner": 131072,
    "kimi-for-coding": 262144,
    "kimi-for-coding-highspeed": 262144,
    "k3": 1048576,
    "k3-256k": 262144,
}


def remote_model_records() -> list[dict[str, Any]]:
    result = []
    for provider in list_providers():
        for model_id in provider["model_ids"]:
            result.append(
                {
                    "id": f"remote/{provider['id']}/{model_id}",
                    "name": f"{provider['name']} · {model_id}",
                    "kind": "chat",
                    "source": "remote",
                    "provider_id": provider["id"],
                    "remote_model_id": model_id,
                    "available": provider["api_key_set"],
                    "base_url": provider["base_url"],
                    "context_length": _REMOTE_CONTEXT_LENGTH.get(model_id, 131072),
                }
            )
    return result


def resolve_remote_model(model_ref: str) -> tuple[dict[str, Any], str]:
    if not model_ref.startswith("remote/"):
        raise ValueError("不是远程模型 ID。")
    _, provider_id, remote_id = model_ref.split("/", 2)
    provider = get_provider(provider_id)
    if not provider:
        raise ValueError("远程模型服务商不存在。")
    model_id = remote_id
    if model_id not in json_load(provider["model_ids"], []):
        raise ValueError("此模型还未发现或不在服务商模型列表中。")
    return provider, model_id


def _sampling_overrides(provider: dict[str, Any], model_id: str) -> dict[str, Any]:
    """按服务商/模型返回采样参数覆盖，处理锁定采样值的端点。

    Kimi Code（api.kimi.com/coding）的 kimi-for-coding/k3 等模型锁定
    temperature=1 且 top_p=0.95，传其它值会被 400 拒绝。对这类端点直接
    下发其固定值；其余服务商不覆盖，沿用调用方参数。
    """
    base = str(provider.get("base_url", "")).lower()
    if "api.kimi.com/coding" in base or "api.kimi.ai/coding" in base:
        return {"temperature": 1, "top_p": 0.95}
    return {}


async def remote_stream(
    provider: dict[str, Any],
    model_id: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    top_p: float = 0.9,
    stop: list[str] | None = None,
):
    """流式调用远程模型。

    借鉴 deepseek-harness 的类型化 block 流：正文与推理（reasoning_content）
    是两类内容块，分别以 dict piece 透出，由上层分发成独立 SSE 事件。
    yield 形态：
      str                                → 正文增量
      {"reasoning": str}                 → 推理链增量
      {"finish_reason": str}             → 结束原因
      {"usage": {...}}                   → token 用量（流末，若服务商提供）
    """
    client = make_client(provider)
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max(1, min(int(max_tokens), 8192)),
        "temperature": max(0.0, min(float(temperature), 2.0)),
        "top_p": max(0.01, min(float(top_p), 1.0)),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    kwargs.update(_sampling_overrides(provider, model_id))
    if stop:
        kwargs["stop"] = stop
    response = await client.chat.completions.create(**kwargs)
    finish_emitted = False
    async for chunk in response:
        # 末尾 usage 帧没有 choices
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            try:
                yield {
                    "usage": {
                        "prompt_tokens": int(usage.prompt_tokens),
                        "completion_tokens": int(usage.completion_tokens),
                        "total_tokens": int(usage.total_tokens),
                    }
                }
            except Exception:
                pass
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            yield {"reasoning": reasoning}
        if delta.content:
            yield delta.content
        if choice.finish_reason and not finish_emitted:
            finish_emitted = True
            yield {"finish_reason": str(choice.finish_reason)}


async def remote_complete(
    provider: dict[str, Any],
    model_id: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 512,
    temperature: float = 0.2,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    client = make_client(provider)
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max(1, min(int(max_tokens), 8192)),
        "temperature": max(0.0, min(float(temperature), 2.0)),
    }
    kwargs.update(_sampling_overrides(provider, model_id))
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    response = await client.chat.completions.create(**kwargs)
    message = response.choices[0].message
    usage = response.usage
    return {
        "content": message.content or "",
        "reasoning": getattr(message, "reasoning_content", None) or "",
        "tool_calls": [
            {
                "id": item.id,
                "name": item.function.name,
                "arguments": item.function.arguments,
            }
            for item in (message.tool_calls or [])
        ],
        "usage": {
            "prompt_tokens": int(usage.prompt_tokens),
            "completion_tokens": int(usage.completion_tokens),
            "total_tokens": int(usage.total_tokens),
        } if usage else None,
        "raw": message,
    }


async def remote_embeddings(
    provider: dict[str, Any], model_id: str, inputs: list[str]
) -> list[list[float]]:
    client = make_client(provider)
    response = await client.embeddings.create(model=model_id, input=inputs)
    return [item.embedding for item in response.data]
