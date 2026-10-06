"""远程 API 成本核算：定价注册表 + 成本引擎 + 预算读取。

本地模型成本按用户要求记 0。远程模型按每千 token 单价计费，
单价可在 platform_settings（key 前缀 `pricing.`）覆盖，默认用内置公开价。
价格单位：美元 / 1000 token。
"""
from __future__ import annotations

import json
from typing import Any

from .storage import connect, now_iso

# 内置默认定价（美元/千 token）：{模型关键字: (prompt价, completion价)}
# 关键字按小写子串匹配模型 id，命中即用。
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "deepseek-flash": (0.00014, 0.00028),
    "deepseek-chat": (0.00027, 0.00110),
    "deepseek-reasoner": (0.00055, 0.00219),
    "deepseek-v4-pro": (0.00055, 0.00219),
    "kimi-for-coding-highspeed": (0.00100, 0.00400),
    "kimi-for-coding": (0.00100, 0.00400),
    "k3-256k": (0.00060, 0.00250),
    "k3": (0.00060, 0.00250),
}
_PRICING_PREFIX = "pricing."
_BUDGET_PREFIX = "budget."


def _match_default(model_name: str) -> tuple[float, float] | None:
    lowered = model_name.lower()
    # 长关键字优先，避免 "kimi-for-coding" 被 "kimi" 抢先
    for key in sorted(_DEFAULT_PRICING, key=len, reverse=True):
        if key in lowered:
            return _DEFAULT_PRICING[key]
    return None


def get_pricing() -> dict[str, dict[str, float]]:
    """返回所有已配置定价：{模型关键字: {prompt, completion}}（美元/千 token）。"""
    with connect() as db:
        rows = db.execute(
            "SELECT key,value FROM platform_settings WHERE key LIKE ?",
            (_PRICING_PREFIX + "%",),
        ).fetchall()
    custom: dict[str, dict[str, float]] = {}
    for row in rows:
        model_key = row["key"][len(_PRICING_PREFIX):]
        try:
            data = json.loads(row["value"])
            custom[model_key] = {
                "prompt": float(data.get("prompt", 0.0)),
                "completion": float(data.get("completion", 0.0)),
            }
        except (ValueError, TypeError, AttributeError):
            continue
    # 默认价与自定义价合并（自定义覆盖默认）
    merged: dict[str, dict[str, float]] = {
        key: {"prompt": p, "completion": c} for key, (p, c) in _DEFAULT_PRICING.items()
    }
    merged.update(custom)
    return merged


def set_pricing(model_key: str, prompt: float, completion: float) -> None:
    """写入/覆盖某模型关键字的单价。"""
    with connect() as db:
        db.execute(
            "INSERT INTO platform_settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (
                _PRICING_PREFIX + model_key,
                json.dumps({"prompt": float(prompt), "completion": float(completion)}),
                now_iso(),
            ),
        )


def lookup_price(model_name: str) -> tuple[float, float] | None:
    """查某模型的 (prompt, completion) 单价，未配置返回 None。"""
    lowered = model_name.lower()
    pricing = get_pricing()
    for key in sorted(pricing, key=len, reverse=True):
        if key in lowered:
            entry = pricing[key]
            return (entry["prompt"], entry["completion"])
    return None


def compute_cost(
    model_name: str, prompt_tokens: int, completion_tokens: int
) -> float | None:
    """按单价计算成本（美元）。本地模型/未配置定价返回 0.0/None。

    返回 None 表示"远程但未配置定价"；0.0 表示本地免费或零成本。
    """
    if not prompt_tokens and not completion_tokens:
        return 0.0
    price = lookup_price(model_name)
    if price is None:
        return None
    prompt_price, completion_price = price
    return round(
        (prompt_tokens / 1000.0) * prompt_price
        + (completion_tokens / 1000.0) * completion_price,
        6,
    )


def get_budgets() -> dict[str, float]:
    """读取预算配置：{daily_usd, monthly_usd}，未配置返回空。"""
    with connect() as db:
        rows = db.execute(
            "SELECT key,value FROM platform_settings WHERE key LIKE ?",
            (_BUDGET_PREFIX + "%",),
        ).fetchall()
    budgets: dict[str, float] = {}
    for row in rows:
        key = row["key"][len(_BUDGET_PREFIX):]
        try:
            budgets[key] = float(row["value"])
        except (TypeError, ValueError):
            continue
    return budgets


def set_budget(period: str, amount_usd: float) -> None:
    """设置预算（period: daily_usd / monthly_usd）。"""
    with connect() as db:
        db.execute(
            "INSERT INTO platform_settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (_BUDGET_PREFIX + period, str(float(amount_usd)), now_iso()),
        )
