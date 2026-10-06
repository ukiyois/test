from __future__ import annotations

import threading
from typing import Any

from .models import list_local_models


class EmbeddingRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model: Any = None
        self._active_id: str | None = None

    def encode(self, model_id: str, inputs: list[str]) -> tuple[list[list[float]], int]:
        with self._lock:
            if self._active_id != model_id or self._model is None:
                self._model = None
                self._active_id = None
                try:
                    import torch
                    from sentence_transformers import SentenceTransformer
                except Exception as exc:
                    raise RuntimeError(f"嵌入模型依赖不可用：{exc}") from exc
                item = next(
                    (
                        entry
                        for entry in list_local_models()
                        if entry["id"] == model_id
                        and entry["kind"] == "embedding"
                        and entry["available"]
                    ),
                    None,
                )
                if not item:
                    raise RuntimeError(f"嵌入模型不存在或权重缺失：{model_id}")
                # Embedding inference stays on CPU so it cannot starve the chat model's
                # GPU allocation; callers can use a remote embedding provider instead.
                self._model = SentenceTransformer(
                    item["path"],
                    device="cpu",
                    trust_remote_code=False,
                    local_files_only=True,
                    model_kwargs={"torch_dtype": torch.float32},
                )
                self._active_id = model_id

            vectors = self._model.encode(
                inputs,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
                batch_size=4,
            )
            result = vectors.tolist()
            tokens = 0
            tokenizer = getattr(self._model, "tokenizer", None)
            if tokenizer:
                for text in inputs:
                    tokens += len(tokenizer.encode(text, add_special_tokens=False))
            return result, tokens


embedding_runtime = EmbeddingRuntime()

