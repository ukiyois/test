"""Agent 上下文管理：真实 token 计量 / tool-result 裁剪 / auto-compaction。

借鉴 deepseek-harness compaction 子系统与 Codex auto-compact，但按正式标准实现：
  - 真实 tokenizer 计量，不做字符数近似（self-improvement 循环需要精确反馈）。
  - surface/log 分离：裁剪只作用于发给模型的 messages 副本，已持久化的
    tool_completed 事件保留全量原文，运行记录不受影响。
  - tool-result 确定性裁剪：head/tail 保留 + 中间省略，无需 LLM。
  - auto-compaction：上下文逼近预算时，用模型生成 handoff 摘要压缩早期迭代，
    保持 tool_call 与 tool_result 配对边界（否则远程 API 因孤儿 tool_result 报错）。

tokenizer 解析策略（按模型来源）：
  - local/xxx   → 已加载的 local_runtime 精确 tokenizer；
                  未加载时按模型目录惰性加载 AutoTokenizer（不加载权重）。
  - gguf/xxx    → llama.cpp 后端，/tokenize 端点不可用，回退到对应基座 tokenizer。
  - remote/...  → 远程模型无法本地分词，按其声明的词表族使用最接近的本地
                  tokenizer 作代理（DeepSeek/Kimi 均为 BPE 系，误差可控）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("llmplatform.context")

# tool_result 单条进入上下文前的字符预算（超出即裁剪）
TOOL_RESULT_CHAR_BUDGET = 6000
# 裁剪后头/尾各保留的字符数
_HEAD_CHARS = 2400
_TAIL_CHARS = 1600

# 远程模型词表族 → 本地代理 tokenizer 的 HF 路径（惰性加载，失败回退 None）
_REMOTE_TOKENIZER_PROXY = {
    "deepseek": "deepseek-ai/DeepSeek-V3",
    "kimi": "moonshotai/Kimi-K2-Instruct",
    "moonshot": "moonshotai/Kimi-K2-Instruct",
}

# 惰性加载的 tokenizer 缓存，key 为模型标识
_tokenizer_cache: dict[str, Any] = {}
_tokenizer_load_failed: set[str] = set()


def _load_hf_tokenizer(path: str) -> Any:
    """按 HF 路径/本地目录惰性加载 tokenizer（不加载模型权重）。"""
    if path in _tokenizer_cache:
        return _tokenizer_cache[path]
    if path in _tokenizer_load_failed:
        return None
    try:
        from transformers import AutoTokenizer

        tok = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
        _tokenizer_cache[path] = tok
        return tok
    except Exception as exc:  # noqa: BLE001 - 离线/无权重时优雅降级
        logger.warning("tokenizer 加载失败 %s: %s", path, exc)
        _tokenizer_load_failed.add(path)
        return None


def _runtime_tokenizer() -> Any:
    """已加载到推理运行时的 tokenizer（精确，零额外开销）。"""
    try:
        from .inference import local_runtime

        return getattr(local_runtime, "_tokenizer", None)
    except Exception:  # noqa: BLE001
        return None


def resolve_tokenizer(model_ref: str, model_path: str | None = None) -> Any:
    """为给定模型引用解析最合适的 tokenizer。

    优先级：已加载运行时 tokenizer > 模型本地目录 > 远程代理词表 > None。
    """
    runtime_tok = _runtime_tokenizer()
    if runtime_tok is not None:
        return runtime_tok
    if model_path:
        tok = _load_hf_tokenizer(model_path)
        if tok is not None:
            return tok
    if model_ref.startswith("remote/"):
        lowered = model_ref.lower()
        for family, proxy in _REMOTE_TOKENIZER_PROXY.items():
            if family in lowered:
                return _load_hf_tokenizer(proxy)
    return None


def count_tokens(text: str, tokenizer: Any) -> int:
    """用真实 tokenizer 精确计数；无 tokenizer 时按 UTF-8 字节保守折算。"""
    if not text:
        return 0
    if tokenizer is not None:
        try:
            return int(len(tokenizer(str(text), add_special_tokens=False)["input_ids"]))
        except Exception:  # noqa: BLE001
            pass
    # 无 tokenizer 的兜底：UTF-8 字节 / 3（多语言混合的保守上界）
    return max(1, len(str(text).encode("utf-8")) // 3)


def count_message_tokens(message: dict[str, Any], tokenizer: Any) -> int:
    """计量单条消息，含 tool_calls 参数与多模态文本段。"""
    total = 0
    content = message.get("content")
    if isinstance(content, str):
        total += count_tokens(content, tokenizer)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                total += count_tokens(str(part.get("text", "")), tokenizer)
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        total += count_tokens(str(fn.get("arguments", "")), tokenizer)
        total += count_tokens(str(fn.get("name", "")), tokenizer)
    return total


def count_messages_tokens(messages: list[dict[str, Any]], tokenizer: Any) -> int:
    """计量整段 messages 的 token 占用（真实分词求和）。"""
    return sum(count_message_tokens(m, tokenizer) for m in messages)


def prune_tool_result(text: str, budget: int = TOOL_RESULT_CHAR_BUDGET) -> str:
    """确定性 head/tail 裁剪：保留开头与结尾，中间省略并标注。

    仅作用于发给模型的副本，不影响已持久化的事件原文。
    """
    if not text or len(text) <= budget:
        return text
    head = text[:_HEAD_CHARS]
    tail = text[-_TAIL_CHARS:]
    omitted = len(text) - len(head) - len(tail)
    return (
        f"{head}\n"
        f"[…中间 {omitted} 字符已裁剪，完整结果见运行记录…]\n"
        f"{tail}"
    )


def _is_tool_pair_boundary(messages: list[dict[str, Any]], cut: int) -> bool:
    """检查在 cut 处截断是否劈开 tool_call/tool_result 配对。"""
    pending: set[str] = set()
    for m in messages[:cut]:
        if m.get("role") == "assistant":
            for call in m.get("tool_calls") or []:
                pending.add(call.get("id"))
        elif m.get("role") == "tool":
            pending.discard(m.get("tool_call_id"))
    if cut < len(messages) and messages[cut].get("role") == "tool":
        return messages[cut].get("tool_call_id") not in pending
    return not pending


def find_compaction_cut(messages: list[dict[str, Any]], keep_recent: int) -> int:
    """找到安全的压缩切点：保留最近 keep_recent 条，且不在 tool 配对中间。

    返回切点索引，messages[:cut] 将被压缩为摘要。找不到安全切点时返回 0。
    """
    n = len(messages)
    if n <= keep_recent:
        return 0
    cut = n - keep_recent
    while cut > 1 and not _is_tool_pair_boundary(messages, cut):
        cut -= 1
    if cut <= 1:
        return 0
    return cut


COMPACTION_SYSTEM = (
    "你是上下文压缩器。另一个语言模型正在解决一个多步任务，"
    "其对话历史即将超出上下文窗口。请把早期历史压缩成一份交接摘要，"
    "供模型继续完成任务。"
)

COMPACTION_PROMPT = """请阅读以下对话历史，生成一份结构化的交接摘要，包含四个部分：

1. **当前进展**：已完成哪些步骤、关键中间结果、做出的重要决策。
2. **关键上下文**：约束条件、用户偏好、重要数据/文件路径/标识符。
3. **待办事项**：接下来要做的具体步骤（按顺序）。
4. **关键数据**：继续任务所必需的精确数值、代码片段、错误信息、引用。

要求：保留所有对后续步骤有实质影响的信息，省略客套与重复。用简洁的中文。

对话历史：
{history}"""

COMPACTION_PREFIX = (
    "[上下文已压缩] 另一个语言模型已开始处理此任务并产出了以下交接摘要，"
    "请基于它继续：\n\n"
)


def build_compaction_request(history_text: str) -> list[dict[str, str]]:
    """构造发给模型的压缩请求消息序列。"""
    return [
        {"role": "system", "content": COMPACTION_SYSTEM},
        {"role": "user", "content": COMPACTION_PROMPT.format(history=history_text)},
    ]


def render_history_for_compaction(messages: list[dict[str, Any]]) -> str:
    """把待压缩的消息区间渲染成纯文本，供摘要模型阅读。

    渲染时也裁剪超长 tool 输出，避免摘要请求本身爆上下文。
    """
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict)
            )
        text = str(content or "")
        if role == "tool":
            text = prune_tool_result(text, 2000)
        if m.get("tool_calls"):
            calls = ", ".join(
                (c.get("function") or {}).get("name", "?") for c in m["tool_calls"]
            )
            lines.append(f"[{role} → 调用工具: {calls}]")
        if text.strip():
            lines.append(f"[{role}] {text}")
    return "\n\n".join(lines)
