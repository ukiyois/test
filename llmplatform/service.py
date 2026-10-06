from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .inference import GenerationHandle, local_runtime
from .providers import remote_complete, remote_stream, resolve_remote_model


def split_model_ref(model_ref: str) -> tuple[str, str]:
    if model_ref.startswith("local/gguf/"):
        # local/gguf/xxx → GGUF 引擎（llama.cpp），model_id 形如 gguf/xxx
        return "local", model_ref.split("/", 1)[1]
    if model_ref.startswith("local/"):
        return "local", model_ref.split("/", 1)[1]
    if model_ref.startswith("gguf/"):
        return "local", model_ref
    if model_ref.startswith("remote/"):
        return "remote", model_ref
    return "local", model_ref


# Live local generation handles, keyed by model id. Lets the API layer abort an
# in-flight stream when the client disconnects or presses stop.
_ACTIVE_STREAMS: dict[str, GenerationHandle] = {}
_ACTIVE_LOCK = asyncio.Lock()


async def abort_stream(model_ref: str) -> bool:
    kind, model_id = split_model_ref(model_ref)
    if kind != "local":
        return False
    async with _ACTIVE_LOCK:
        handle = _ACTIVE_STREAMS.pop(model_id, None)
    if handle is None:
        return False
    handle.request_abort()
    return True


def _normalize_stops(stop: str | list[str] | None) -> list[str]:
    if not stop:
        return []
    items = [stop] if isinstance(stop, str) else list(stop)
    return [item for item in items if isinstance(item, str) and item][:4]


async def usage_for_local(
    messages: list[dict[str, Any]], completion_text: str
) -> dict[str, int] | None:
    """Best-effort token accounting for a local generation."""
    try:
        prompt_tokens, completion_tokens = await asyncio.gather(
            asyncio.to_thread(local_runtime.count_prompt_tokens, messages),
            asyncio.to_thread(local_runtime.count_text_tokens, completion_text),
        )
    except Exception:
        return None
    if prompt_tokens is None or completion_tokens is None:
        return None
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


async def stream_model(
    model_ref: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 512,
    temperature: float = 0.7,
    top_p: float = 0.9,
    flush_chars: int = 16,
    stop: str | list[str] | None = None,
):
    kind, model_id = split_model_ref(model_ref)
    stops = _normalize_stops(stop)
    if kind == "remote":
        provider, remote_id = resolve_remote_model(model_id)
        async for piece in remote_stream(
            provider,
            remote_id,
            messages,
            max_tokens,
            temperature,
            top_p,
            stop=stops or None,
        ):
            yield piece
        return

    handle = local_runtime.start_stream(
        model_id,
        messages,
        max_new_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
    )
    async with _ACTIVE_LOCK:
        _ACTIVE_STREAMS[model_id] = handle
    stop_tail = max((len(item) for item in stops), default=0)
    pending = ""  # withheld suffix that might be the start of a stop sequence
    finish_reason: str | None = None
    try:
        buffer: list[str] = []
        while True:
            item = await asyncio.to_thread(handle.output.get)
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            buffer.append(str(item))
            # Coalesce several token chunks into one SSE frame to cut the
            # per-token thread hop + serialization overhead.
            if sum(len(part) for part in buffer) >= flush_chars:
                chunk = pending + "".join(buffer)
                buffer.clear()
                if stops:
                    earliest = min(
                        (chunk.find(seq) for seq in stops if chunk.find(seq) >= 0),
                        default=-1,
                    )
                    if earliest >= 0:
                        finish_reason = "stop"
                        head = chunk[:earliest]
                        if head:
                            yield head
                        pending = ""
                        buffer.clear()
                        break
                    # withhold the tail that could grow into a stop sequence
                    pending = chunk[-stop_tail:] if stop_tail else ""
                    emit = chunk[: len(chunk) - stop_tail] if stop_tail else chunk
                    if emit:
                        yield emit
                else:
                    yield chunk
            if finish_reason is not None:
                break
        if finish_reason is None:
            tail = pending + "".join(buffer)
            buffer.clear()
            pending = ""
            if stops:
                earliest = min(
                    (tail.find(seq) for seq in stops if tail.find(seq) >= 0),
                    default=-1,
                )
                if earliest >= 0:
                    finish_reason = "stop"
                    tail = tail[:earliest]
            if tail:
                yield tail
            if finish_reason is None:
                if handle.aborted:
                    finish_reason = "stop"
                elif handle.max_tokens and handle.token_count >= handle.max_tokens:
                    finish_reason = "length"
                else:
                    finish_reason = "stop"
        yield {"finish_reason": finish_reason}
    finally:
        async with _ACTIVE_LOCK:
            _ACTIVE_STREAMS.pop(model_id, None)


async def complete_model(
    model_ref: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 512,
    temperature: float = 0.2,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    kind, model_id = split_model_ref(model_ref)
    if kind == "remote":
        provider, remote_id = resolve_remote_model(model_id)
        result = await remote_complete(
            provider,
            remote_id,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tool_schemas,
        )
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result
    text = await asyncio.to_thread(
        local_runtime.generate_text,
        model_id,
        messages,
        max_tokens,
        temperature,
    )
    try:
        prompt_tokens, completion_tokens = await asyncio.gather(
            asyncio.to_thread(local_runtime.count_prompt_tokens, messages),
            asyncio.to_thread(local_runtime.count_text_tokens, text),
        )
    except Exception:
        # Usage measurement is best-effort and must never fail a completed model call.
        prompt_tokens = completion_tokens = None
    usage = None
    if prompt_tokens is not None and completion_tokens is not None:
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return {
        "content": text,
        "tool_calls": [],
        "usage": usage,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def extract_local_tool_call(text: str, allowed: set[str]) -> dict[str, Any] | None:
    value = text.strip()
    fence = chr(96) * 3
    if value.startswith(fence):
        value = value.strip(chr(96))
        if value.lower().startswith("json"):
            value = value[4:].strip()
    first = value.find("{")
    last = value.rfind("}")
    if first < 0 or last <= first:
        return None
    try:
        parsed = json.loads(value[first : last + 1])
    except json.JSONDecodeError:
        return None
    tool_id = parsed.get("tool") or parsed.get("name")
    if tool_id not in allowed:
        return None
    args = parsed.get("arguments", parsed.get("args", {}))
    if not isinstance(args, dict):
        return None
    return {"name": tool_id, "arguments": args}
