from __future__ import annotations

from pathlib import Path
from typing import Any

from .storage import ROOT


MODEL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "qwen3.5-0.8b",
        "name": "Qwen3.5 · 0.8B",
        "path": "Decoder/Qwen3.5-0.8B",
        "kind": "chat",
        "family": "Qwen",
        "description": "轻量本地对话模型，适合作为默认模型。",
        "tags": ["本地", "轻量"],
        "context_length": 32768,
    },
    {
        "id": "qwen3.5-2b",
        "name": "Qwen3.5 · 2B",
        "path": "Decoder/Qwen3.5-2B",
        "kind": "chat",
        "family": "Qwen",
        "description": "本地对话模型；显存不足时自动将部分层放到内存。",
        "tags": ["本地", "CPU/GPU"],
        "context_length": 32768,
    },
    {
        "id": "qwen2.5-1.5b-instruct",
        "name": "Qwen2.5 · 1.5B Instruct",
        "path": "Decoder/Qwen2.5-1.5B-Instruct",
        "kind": "chat",
        "family": "Qwen",
        "description": "指令微调中文模型。",
        "tags": ["本地", "指令"],
        "context_length": 32768,
    },
    {
        "id": "gemma-3-1b-it",
        "name": "Gemma 3 · 1B IT",
        "path": "Decoder/Gemma-3-1B-it",
        "kind": "chat",
        "family": "Gemma",
        "description": "轻量指令模型。",
        "tags": ["本地", "轻量"],
        "context_length": 32768,
    },
    {
        "id": "gemma-4-e2b-it",
        "name": "Gemma 4 · E2B IT",
        "path": "Decoder/Gemma-4-E2B-it",
        "kind": "chat",
        "family": "Gemma",
        "description": "需要 CPU/GPU 混合加载或更高显存。",
        "tags": ["本地", "CPU/GPU"],
        "context_length": 131072,
    },
    {
        "id": "phi-4-mini-instruct",
        "name": "Phi-4 Mini Instruct",
        "path": "Decoder/Phi-4-mini-instruct",
        "kind": "chat",
        "family": "Phi",
        "description": "需要 CPU/GPU 混合加载或更高显存。",
        "tags": ["本地", "CPU/GPU"],
        "context_length": 131072,
    },
    {
        "id": "flan-t5-base",
        "name": "FLAN-T5 Base",
        "path": "Seq2Seq/FLAN-T5-base",
        "kind": "text2text",
        "family": "T5",
        "description": "文本转换模型，可从 Harness 的文本生成节点调用。",
        "tags": ["本地", "文本转换"],
    },
    {
        "id": "bge-m3",
        "name": "BGE-M3",
        "path": "Embedding/BGE-M3",
        "kind": "embedding",
        "family": "BGE",
        "description": "本地向量模型，可用于文件检索。",
        "tags": ["本地", "向量"],
    },
    {
        "id": "qwen3-embedding-4b",
        "name": "Qwen3 Embedding · 4B",
        "path": "Embedding/Qwen3-Embedding-4B",
        "kind": "embedding",
        "family": "Qwen",
        "description": "本地向量模型；资源不足时使用 CPU/GPU 混合加载。",
        "tags": ["本地", "向量", "CPU/GPU"],
    },
]


def _has_weights(path: Path) -> bool:
    patterns = (
        "*.safetensors",
        "*.safetensors.index.json",
        "pytorch_model*.bin",
        "model.onnx",
    )
    return any(any(path.glob(pattern)) for pattern in patterns)


def list_local_models() -> list[dict[str, Any]]:
    result = []
    for item in MODEL_DEFINITIONS:
        folder = ROOT / item["path"]
        exists = folder.is_dir()
        available = exists and _has_weights(folder)
        result.append(
            {
                **item,
                "source": "local",
                "available": available,
                "path": str(folder),
            }
        )
    return result


EXCLUDED_MODELS = [
    "GPT-2-small",
    "GPT-2-medium",
    "MobileLLM-R1-950M",
    "Qwen3-Embedding-0.6B",
    "DeepSeek-Coder-1.3B-Instruct",
    "Xmodel-2.5",
    "1.3B-100B-GLA-hybrid-3-1",
    "1.3B-100B-HGRN2-hybrid-3-1",
]


def list_gguf_models() -> list[dict[str, Any]]:
    """Qwen3.8-27B GGUF 作为一等公民本地模型，始终出现在模型列表。"""
    from .llamacpp import MODEL_ALIAS, MODEL_PATH, EXPECTED_MODEL_BYTES, llama_runtime

    try:
        status = llama_runtime.status()
    except Exception:
        status = {}
    size = MODEL_PATH.stat().st_size if MODEL_PATH.is_file() else 0
    installed = size >= EXPECTED_MODEL_BYTES * 0.98
    return [
        {
            "id": "gguf/qwen38-27b",
            "name": f"Qwen3.8 · 27B (GGUF Q3_K_M)",
            "path": str(MODEL_PATH),
            "kind": "chat",
            "family": "Qwen",
            "description": "大参数本地模型，经 llama.cpp 引擎运行，自动 GPU 装配。",
            "tags": ["本地", "GGUF", "大模型"],
            "source": "local",
            "engine": "llama.cpp",
            "available": installed,
            "context_length": 4096,
            "model_alias": MODEL_ALIAS,
            "gguf_status": status.get("status"),
            "gguf_running": status.get("running"),
            "gguf_ready": status.get("ready"),
        }
    ]

