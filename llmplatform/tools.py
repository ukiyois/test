from __future__ import annotations

import ast
import asyncio
import ipaddress
import json
import math
import operator
import re
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx


TOOL_CATALOG: dict[str, dict[str, Any]] = {
    "calculator": {
        "name": "计算器",
        "description": "计算安全的数字表达式，例如 (12 + 8) * 1.5。",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
            "additionalProperties": False,
        },
    },
    "search_files": {
        "name": "搜索上传文件",
        "description": "在本次工作流附带的文件中查找与关键词相关的文本片段。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "semantic_search": {
        "name": "语义搜索文件",
        "description": "用嵌入模型从本次上传文件中检索语义相关的片段。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "model": {"type": "string", "enum": ["bge-m3", "qwen3-embedding-4b"]},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "current_time": {
        "name": "获取当前时间",
        "description": "返回当前 UTC 时间。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "http_request": {
        "name": "HTTP 请求",
        "description": "向公开 HTTP/HTTPS API 发送 GET 或 POST 请求。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST"]},
                "headers": {"type": "object"},
                "json": {"type": "object"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}

# 合并自强化工具 schema，使 tool_schemas 可统一取用
from .selfimprove import (  # noqa: E402
    DANGEROUS_TOOLS,
    SELF_IMPROVE_TOOL_SCHEMAS,
    execute_self_improve_tool,
)

for _sid, _spec in SELF_IMPROVE_TOOL_SCHEMAS.items():
    TOOL_CATALOG[_sid] = {
        "name": _sid,
        "description": _spec["description"],
        "parameters": _spec["parameters"],
        "dangerous": _sid in DANGEROUS_TOOLS,
    }

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _calculate_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _calculate_node(node.left)
        right = _calculate_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 8:
            raise ValueError("指数不能超过 8。")
        value = _BINOPS[type(node.op)](left, right)
        if not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("计算结果超出允许范围。")
        return value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_calculate_node(node.operand))
    raise ValueError("只允许数字和 + - * / // % ** 括号运算。")


def calculate(expression: str) -> float:
    expression = expression.translate(str.maketrans({"×": "*", "÷": "/", "−": "-", "–": "-", "＋": "+", "／": "/"}))
    if len(expression) > 200:
        raise ValueError("表达式过长。")
    tree = ast.parse(expression, mode="eval")
    return _calculate_node(tree.body)


def infer_calculator_expression(text: str) -> str:
    """Recover a simple arithmetic expression when a small model omits tool args."""
    normalized = text.translate(str.maketrans({"×": "*", "÷": "/", "−": "-", "–": "-", "＋": "+", "／": "/"}))
    pattern = re.compile(r"(?<![\w.])[0-9(][0-9\s.+*/()%\-]*[0-9)](?![\w.])")
    for match in pattern.finditer(normalized):
        candidate = match.group(0).strip()
        if not any(operator in candidate for operator in "+-*/%"):
            continue
        try:
            ast.parse(candidate, mode="eval")
        except SyntaxError:
            continue
        return candidate
    return ""


def search_files(query: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    terms = [part.lower() for part in query.split() if part.strip()]
    hits: list[dict[str, str]] = []
    for file in files:
        text = str(file.get("text", ""))
        lowered = text.lower()
        if not terms:
            excerpt = text[:700]
            if excerpt:
                hits.append({"filename": file.get("filename", "文件"), "excerpt": excerpt})
            continue
        positions = [lowered.find(term) for term in terms]
        position = min((pos for pos in positions if pos >= 0), default=-1)
        if position >= 0:
            start, end = max(0, position - 220), min(len(text), position + 480)
            hits.append(
                {"filename": file.get("filename", "文件"), "excerpt": text[start:end]}
            )
        if len(hits) >= 8:
            break
    return {"query": query, "hits": hits, "count": len(hits)}


def _assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许有效的 HTTP 或 HTTPS 地址。")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("HTTP 工具不能访问本机地址。")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443)}
    except OSError as exc:
        raise ValueError(f"无法解析主机名：{exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("HTTP 工具不能访问本机、内网或保留地址。")


async def execute_tool(
    tool_id: str, arguments: dict[str, Any], files: list[dict[str, Any]]
) -> Any:
    if tool_id == "calculator":
        return {"result": calculate(str(arguments.get("expression", "")))}
    if tool_id == "search_files":
        return search_files(str(arguments.get("query", "")), files)
    if tool_id == "semantic_search":
        from .embeddings import embedding_runtime

        query = str(arguments.get("query", ""))
        model_id = str(arguments.get("model") or "bge-m3")
        chunks: list[dict[str, str]] = []
        for file in files:
            text = str(file.get("text", ""))
            step = 650
            overlap = 100
            for start in range(0, len(text), step - overlap):
                content = text[start : start + step].strip()
                if content:
                    chunks.append({"filename": file.get("filename", "文件"), "text": content})
                if len(chunks) >= 120:
                    break
            if len(chunks) >= 120:
                break
        if not chunks:
            return {"query": query, "hits": [], "count": 0}
        try:
            vectors, _ = await asyncio.to_thread(
                embedding_runtime.encode,
                model_id,
                [query] + [item["text"] for item in chunks],
            )
            query_vector = vectors[0]
            scored = []
            for index, vector in enumerate(vectors[1:]):
                score = sum(left * right for left, right in zip(query_vector, vector))
                scored.append(
                    {
                        "filename": chunks[index]["filename"],
                        "excerpt": chunks[index]["text"],
                        "score": round(float(score), 4),
                    }
                )
            scored.sort(key=lambda item: item["score"], reverse=True)
            return {"query": query, "hits": scored[:6], "count": min(6, len(scored))}
        except Exception as exc:
            fallback = search_files(query, files)
            fallback["mode"] = "keyword_fallback"
            fallback["embedding_error"] = str(exc)
            return fallback
    if tool_id == "current_time":
        return {"utc": datetime.now(timezone.utc).isoformat()}
    if tool_id == "http_request":
        url = str(arguments.get("url", ""))
        _assert_public_url(url)
        method = str(arguments.get("method", "GET")).upper()
        if method not in {"GET", "POST"}:
            raise ValueError("当前只支持 GET 和 POST。")
        headers = arguments.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValueError("headers 必须是对象。")
        body = arguments.get("json")
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=False, limits=httpx.Limits(max_connections=4)
        ) as client:
            response = await client.request(
                method,
                url,
                headers={str(k): str(v) for k, v in list(headers.items())[:30]},
                json=body if method == "POST" and isinstance(body, dict) else None,
            )
        text = response.text[:12000]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        return {"status": response.status_code, "body": parsed}
    if tool_id in SELF_IMPROVE_TOOL_SCHEMAS:
        return await execute_self_improve_tool(tool_id, arguments)
    raise ValueError(f"未注册的工具：{tool_id}")


def tool_schemas(tool_ids: list[str]) -> list[dict[str, Any]]:
    result = []
    for tool_id in tool_ids:
        spec = TOOL_CATALOG.get(tool_id)
        if not spec:
            continue
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool_id,
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                },
            }
        )
    return result
