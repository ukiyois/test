from __future__ import annotations

import gc
import queue
import threading
from pathlib import Path
from typing import Any, Iterable

from .models import list_local_models
from .storage import OFFLOAD_DIR


class ModelRuntimeError(RuntimeError):
    pass


class GenerationHandle:
    """Handle for an in-flight local generation; allows cooperative abort."""

    def __init__(self) -> None:
        self.output: queue.Queue[Any] = queue.Queue()
        self._abort = threading.Event()
        self._streamer: Any = None
        self.token_count = 0
        self.max_tokens = 0

    def request_abort(self) -> None:
        self._abort.set()
        streamer = self._streamer
        if streamer is not None:
            try:
                streamer.end()
            except Exception:
                pass

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()


class LocalModelRuntime:
    """Serializes model changes and inference; Accelerate dispatches layers to CPU/GPU."""

    def __init__(self) -> None:
        self._operation_lock = threading.Lock()
        self._model: Any = None
        self._tokenizer: Any = None
        self._active_id: str | None = None
        self._placement: dict[str, Any] = {}
        self._force_cpu = False
        self._last_error: str | None = None
        self._backend_ready: bool | None = None
        self._backend_error: str | None = None
        self._external_backend: str | None = None
        self._external_server: Any = None  # 具有 chat_stream/chat_complete 的外部引擎（llama.cpp）

    def set_external_server(self, server: Any) -> None:
        """注册外部推理引擎（如 llama.cpp），GGUF 模型请求将被路由到它。"""
        self._external_server = server

    def is_gguf_model(self, model_id: str) -> bool:
        return model_id.startswith("gguf/")

    def _ensure_gguf_ready(self) -> Any:
        server = self._external_server
        if server is None:
            raise ModelRuntimeError("GGUF 引擎尚未注册。")
        status = server.status()
        if not status.get("ready"):
            raise ModelRuntimeError("GGUF 引擎未就绪，请先在「模型」页启动 Qwen3.8-27B 引擎。")
        return server

    def prepare_backend(self) -> dict[str, Any]:
        """Import the ML runtime on the application thread before worker threads use it."""
        if self._backend_ready:
            return {"ready": True, "error": None}
        try:
            import importlib.metadata as package_metadata
            import logging

            import torch  # noqa: F401

            original_packages_distributions = package_metadata.packages_distributions

            def packages_distributions_without_broken_metadata():
                try:
                    return original_packages_distributions()
                except KeyError as exc:
                    if exc.args != ("Name",):
                        raise
                    # Transformers 5 inspects every installed distribution during
                    # import. Some unrelated local dist-info folders have no Name
                    # field; skip those malformed records while preserving the rest.
                    result: dict[str, list[str]] = {}
                    skipped = 0
                    for distribution in package_metadata.distributions():
                        name = distribution.metadata.get("Name")
                        if not name:
                            skipped += 1
                            continue
                        for package in (distribution.read_text("top_level.txt") or "").split():
                            result.setdefault(package, []).append(name)
                    logging.getLogger("llmplatform").warning(
                        "Skipped %s installed distributions with missing Name metadata",
                        skipped,
                    )
                    return result

            package_metadata.packages_distributions = packages_distributions_without_broken_metadata
            try:
                from transformers import TextIteratorStreamer  # noqa: F401
            finally:
                package_metadata.packages_distributions = original_packages_distributions
        except Exception as exc:
            self._backend_ready = False
            self._backend_error = str(exc)
            self._last_error = f"本地推理依赖初始化失败：{exc}"
            return {"ready": False, "error": self._backend_error}
        self._backend_ready = True
        self._backend_error = None
        return {"ready": True, "error": None}

    def status(self) -> dict[str, Any]:
        placement = dict(self._placement)
        gguf_active = bool(
            self._external_server and self._external_backend == "llama.cpp"
        )
        if gguf_active:
            try:
                gguf_status = self._external_server.status()
                placement = {
                    "mode": "GGUF · llama.cpp",
                    "engine": "llama.cpp",
                    "model": gguf_status.get("model_alias"),
                    "placement": gguf_status.get("placement"),
                    "context_size": gguf_status.get("context_size"),
                    "running": gguf_status.get("running"),
                    "ready": gguf_status.get("ready"),
                    "metrics": gguf_status.get("metrics"),
                }
            except Exception:
                pass
        return {
            "active_model": self._active_id or ("gguf/" + (placement.get("model") or "") if gguf_active else self._active_id),
            "placement": placement,
            "force_cpu": self._force_cpu,
            "busy": self._operation_lock.locked(),
            "last_error": self._last_error,
            "external_backend": self._external_backend,
            "gguf_active": gguf_active,
        }

    def set_external_backend(self, backend: str | None) -> dict[str, Any]:
        """Reserve shared local compute for a separately managed model server."""
        with self._operation_lock:
            if backend:
                self._unload_locked()
            self._external_backend = backend
            return self.status()

    def _get_model_info(self, model_id: str) -> dict[str, Any]:
        for item in list_local_models():
            if item["id"] == model_id:
                if not item["available"]:
                    raise ModelRuntimeError(f"模型 {model_id} 的权重文件缺失。")
                if item["kind"] not in {"chat", "text2text"}:
                    raise ModelRuntimeError(f"{model_id} 不是文本生成模型。")
                return item
        raise ModelRuntimeError(f"未知的本地模型：{model_id}")

    @staticmethod
    def _memory_limits(torch: Any) -> dict[Any, str]:
        import psutil

        limits: dict[Any, str] = {}
        if torch.cuda.is_available():
            free_bytes, _ = torch.cuda.mem_get_info(0)
            # 精确按字节预算：仅保留 1.25 GiB 给桌面/CUDA kernel/KV cache，
            # 避免整 GiB 取整把边界模型误挤出 GPU。
            gpu_bytes = max(1024**3, free_bytes - int(1.25 * 1024**3))
            limits[0] = f"{gpu_bytes}B"
        available_cpu = psutil.virtual_memory().available
        cpu_bytes = max(2 * 1024**3, available_cpu - 3 * 1024**3)
        limits["cpu"] = f"{cpu_bytes}B"
        return limits

    def _load_locked(self, model_id: str, force_cpu: bool = False) -> None:
        if self._external_backend:
            raise ModelRuntimeError(
                f"本地推理资源当前由 {self._external_backend} 占用；请先停止该运行时再加载 Transformers 模型。"
            )
        if self._active_id == model_id and self._model is not None and self._force_cpu == force_cpu:
            return

        self._unload_locked()
        item = self._get_model_info(model_id)
        model_path = Path(item["path"])
        try:
            import torch
            from transformers import AutoConfig, AutoTokenizer
        except Exception as exc:
            detail = self._backend_error or str(exc)
            raise ModelRuntimeError(f"本地推理依赖不可用：{detail}") from exc

        try:
            config = AutoConfig.from_pretrained(
                model_path, local_files_only=True, trust_remote_code=False
            )
            try:
                tokenizer = AutoTokenizer.from_pretrained(
                    model_path, local_files_only=True, trust_remote_code=False
                )
            except Exception:
                from transformers import AutoProcessor

                processor = AutoProcessor.from_pretrained(
                    model_path, local_files_only=True, trust_remote_code=False
                )
                tokenizer = processor.tokenizer
        except Exception as exc:
            raise ModelRuntimeError(f"读取模型配置或分词器失败：{exc}") from exc

        if force_cpu or not torch.cuda.is_available():
            dtype = torch.float32
            loading = {"device_map": {"": "cpu"}}
        else:
            major, _ = torch.cuda.get_device_capability(0)
            dtype = torch.bfloat16 if major >= 8 else torch.float16
            loading = {
                "device_map": "auto",
                "max_memory": self._memory_limits(torch),
                "offload_folder": str(OFFLOAD_DIR),
            }

        # Prefer the fastest available attention backend. flash-attn is only
        # used when it is actually importable; SDPA covers everything else.
        attn_implementation = None
        if not force_cpu and torch.cuda.is_available():
            try:
                import flash_attn  # noqa: F401

                attn_implementation = "flash_attention_2"
            except Exception:
                attn_implementation = "sdpa"
        if attn_implementation:
            loading["attn_implementation"] = attn_implementation

        model_classes: list[Any] = []
        if getattr(config, "is_encoder_decoder", False):
            from transformers import AutoModelForSeq2SeqLM

            model_classes.append(AutoModelForSeq2SeqLM)
        else:
            from transformers import AutoModelForCausalLM

            model_classes.append(AutoModelForCausalLM)
            try:
                from transformers import AutoModelForImageTextToText

                model_classes.append(AutoModelForImageTextToText)
            except ImportError:
                pass
            try:
                from transformers import AutoModelForConditionalGeneration

                model_classes.append(AutoModelForConditionalGeneration)
            except ImportError:
                pass

        errors: list[str] = []
        model = None
        attempts = [(dtype, loading, force_cpu or not torch.cuda.is_available())]
        if not force_cpu and torch.cuda.is_available():
            # Automatic layer placement is the normal path. If that still cannot
            # allocate the model, retry once entirely on system memory.
            attempts.append((torch.float32, {"device_map": {"": "cpu"}}, True))
        for attempt_dtype, attempt_loading, cpu_only in attempts:
            for model_class in model_classes:
                try:
                    model = model_class.from_pretrained(
                        model_path,
                        local_files_only=True,
                        trust_remote_code=False,
                        dtype=attempt_dtype,
                        low_cpu_mem_usage=True,
                        **attempt_loading,
                    )
                    dtype = attempt_dtype
                    force_cpu = cpu_only
                    break
                except Exception as exc:
                    errors.append(f"{model_class.__name__}: {exc}")
                    gc.collect()
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
            if model is not None:
                break

        if model is None:
            message = "；".join(errors[-4:])
            raise ModelRuntimeError(f"模型加载失败（已尝试自动 CPU/GPU 分配及 CPU 回退）：{message}")

        model.eval()
        self._model = model
        self._tokenizer = tokenizer
        self._active_id = model_id
        self._force_cpu = force_cpu or not torch.cuda.is_available()
        self._placement = {
            "mode": "CPU" if self._force_cpu else "自动 CPU/GPU 分配",
            "device_map": getattr(model, "hf_device_map", {}),
            "dtype": str(dtype),
            "layers": self._layer_summary(getattr(model, "hf_device_map", {})),
        }
        self._last_error = None

    @staticmethod
    def _layer_summary(device_map: dict[str, Any]) -> dict[str, int]:
        """统计层在各设备上的分布，用于前端「动力分配表」。"""
        counts: dict[str, int] = {"gpu": 0, "cpu": 0, "disk": 0}
        for target in device_map.values():
            key = str(target).lower()
            if key == "disk":
                counts["disk"] += 1
            elif key == "cpu":
                counts["cpu"] += 1
            else:
                counts["gpu"] += 1
        counts["total"] = counts["gpu"] + counts["cpu"] + counts["disk"]
        return counts

    def _unload_locked(self) -> None:
        self._model = None
        self._tokenizer = None
        self._active_id = None
        self._placement = {}
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def unload(self) -> None:
        with self._operation_lock:
            self._unload_locked()

    def load(self, model_id: str) -> dict[str, Any]:
        with self._operation_lock:
            self._load_locked(model_id)
        return self.status()

    @staticmethod
    def _format_prompt(tokenizer: Any, messages: list[dict[str, Any]]) -> str:
        normalized = [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in messages
        ]
        if getattr(tokenizer, "chat_template", None):
            return tokenizer.apply_chat_template(
                normalized, tokenize=False, add_generation_prompt=True
            )
        return "\n".join(f"{m['role'].upper()}: {m['content']}" for m in normalized) + "\nASSISTANT:"

    def count_prompt_tokens(self, messages: list[dict[str, Any]]) -> int | None:
        """Count the exact formatted prompt with the currently loaded tokenizer."""
        with self._operation_lock:
            if self._tokenizer is None:
                return None
            prompt = self._format_prompt(self._tokenizer, messages)
            return len(self._tokenizer(prompt)["input_ids"])

    def count_text_tokens(self, text: str) -> int | None:
        """Count generated text tokens without inserting another special token."""
        with self._operation_lock:
            if self._tokenizer is None:
                return None
            return len(self._tokenizer(str(text), add_special_tokens=False)["input_ids"])

    def _generate_locked(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        max_new_tokens: int,
        temperature: float,
        top_p: float,
        output: queue.Queue[Any],
        handle: GenerationHandle | None = None,
    ) -> None:
        import torch
        from transformers import TextIteratorStreamer

        self._load_locked(model_id)
        prompt = self._format_prompt(self._tokenizer, messages)
        encoded = self._tokenizer(prompt, return_tensors="pt")
        input_embedding = self._model.get_input_embeddings()
        input_device = input_embedding.weight.device
        if input_device.type == "meta":
            embedding_key = next(
                (
                    key
                    for key in getattr(self._model, "hf_device_map", {})
                    if "embed" in key.lower()
                ),
                None,
            )
            mapped = (
                self._model.hf_device_map.get(embedding_key)
                if embedding_key
                else None
            )
            if isinstance(mapped, int):
                input_device = torch.device(f"cuda:{mapped}")
            elif isinstance(mapped, str) and mapped not in {"disk", "meta"}:
                input_device = torch.device(mapped)
            else:
                input_device = torch.device("cpu")
        encoded = {key: value.to(input_device) for key, value in encoded.items()}
        streamer = TextIteratorStreamer(
            self._tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=300
        )
        if handle is not None:
            handle._streamer = streamer
            handle.max_tokens = max(1, min(int(max_new_tokens), 8192))
        generation: dict[str, Any] = {
            **encoded,
            "streamer": streamer,
            "max_new_tokens": max(1, min(int(max_new_tokens), 8192)),
            "do_sample": temperature > 0,
            "pad_token_id": self._tokenizer.pad_token_id
            or self._tokenizer.eos_token_id,
            "eos_token_id": self._tokenizer.eos_token_id,
        }
        if temperature > 0:
            generation["temperature"] = max(0.01, min(float(temperature), 2.0))
            generation["top_p"] = max(0.01, min(float(top_p), 1.0))

        errors: list[BaseException] = []

        def produce() -> None:
            try:
                with torch.inference_mode():
                    self._model.generate(**generation)
            except BaseException as exc:
                errors.append(exc)
                streamer.on_finalized_text("", stream_end=True)

        worker = threading.Thread(target=produce, name="local-model-generate", daemon=True)
        worker.start()
        try:
            for text in streamer:
                if handle is not None and handle.aborted:
                    streamer.end()
                    break
                if text:
                    if handle is not None:
                        handle.token_count += 1
                    output.put(text)
            worker.join()
            if errors:
                raise errors[0]
        finally:
            if worker.is_alive():
                worker.join(timeout=2)

    def start_stream(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float = 0.9,
    ) -> GenerationHandle:
        handle = GenerationHandle()

        if self.is_gguf_model(model_id):
            # GGUF：把 llama.cpp 的异步流桥接进 GenerationHandle 队列，
            # 让 service.stream_model 的现有逻辑（聚合/中止/finish）完全复用。
            import asyncio as _asyncio

            payload = {
                "messages": messages,
                "max_tokens": max_new_tokens,
                "temperature": temperature,
                "top_p": top_p,
            }

            async def pump() -> None:
                server = self._ensure_gguf_ready()
                count = 0
                async for piece in server.chat_stream(payload):
                    if handle.aborted:
                        break
                    count += 1
                    handle.output.put(piece)
                handle.token_count = count

            def run_gguf() -> None:
                try:
                    _asyncio.run(pump())
                except BaseException as exc:
                    self._last_error = str(exc)
                    handle.output.put(exc)
                finally:
                    handle.output.put(None)

            threading.Thread(target=run_gguf, name=f"model-{model_id}", daemon=True).start()
            return handle

        def run() -> None:
            try:
                with self._operation_lock:
                    self._generate_locked(
                        model_id,
                        messages,
                        max_new_tokens,
                        temperature,
                        top_p,
                        handle.output,
                        handle,
                    )
            except BaseException as exc:
                self._last_error = str(exc)
                handle.output.put(exc)
            finally:
                handle.output.put(None)

        threading.Thread(target=run, name=f"model-{model_id}", daemon=True).start()
        return handle

    def generate_text(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        max_new_tokens: int = 512,
        temperature: float = 0.2,
        top_p: float = 0.9,
    ) -> str:
        if self.is_gguf_model(model_id):
            import asyncio as _asyncio

            server = self._ensure_gguf_ready()
            result = _asyncio.run(
                server.chat_complete(
                    {
                        "messages": messages,
                        "max_tokens": max_new_tokens,
                        "temperature": temperature,
                    }
                )
            )
            return str(result.get("content") or "").strip()

        output: queue.Queue[Any] = queue.Queue()
        with self._operation_lock:
            self._generate_locked(
                model_id, messages, max_new_tokens, temperature, top_p, output
            )
        output.put(None)
        chunks: list[str] = []
        while True:
            item = output.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            chunks.append(str(item))
        return "".join(chunks).strip()


local_runtime = LocalModelRuntime()
