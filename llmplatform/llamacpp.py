from __future__ import annotations

import json
import subprocess
import threading
import time
import os
from pathlib import Path
from typing import Any

import httpx
import psutil

from .storage import DATA_DIR


ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = DATA_DIR / "engines" / "llama.cpp"
MODEL_PATH = DATA_DIR / "models" / "gguf" / "qwen3.8-27b-q3" / "Qwen3.8-27B-Q3_K_M.gguf"
SERVER_PATH = ENGINE_DIR / "llama-server.exe"
PID_PATH = DATA_DIR / "llama-server.pid"
LOG_PATH = DATA_DIR / "llama-server.log"
SERVER_URL = "http://127.0.0.1:8011"
API_KEY = "llmmod-local-runtime"
MODEL_ALIAS = "Qwen3.8-27B-Q3_K_M"
EXPECTED_MODEL_BYTES = 13_404_051_168
CONTEXT_SIZE = 4096


class LlamaCppRuntime:
    """Manage an optional local GGUF server with llama.cpp's automatic GPU fit."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._mode = "auto-fit"
        self._last_error = ""
        self._starting = False
        self._cancel_startup = False
        self._watcher: threading.Thread | None = None
        self._last_cpu_sample: tuple[int, float, float] | None = None

    def _read_pid(self) -> int | None:
        try:
            return int(PID_PATH.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def _server_process(self) -> psutil.Process | None:
        process = self._process
        if process and process.poll() is None:
            try:
                return psutil.Process(process.pid)
            except psutil.Error:
                return None
        pid = self._read_pid()
        if not pid:
            return None
        try:
            candidate = psutil.Process(pid)
            name = candidate.name().lower()
            command = " ".join(candidate.cmdline()).lower()
            if "llama-server" in name or "llama-server.exe" in command:
                return candidate
        except psutil.Error:
            pass
        PID_PATH.unlink(missing_ok=True)
        return None

    def _health(self) -> bool:
        try:
            response = httpx.get(SERVER_URL + "/health", timeout=0.6)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def _launch(self, gpu_layers: str) -> subprocess.Popen[bytes]:
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        args = [
            str(SERVER_PATH),
            "--model", str(MODEL_PATH),
            "--host", "127.0.0.1",
            "--port", "8011",
            "--alias", MODEL_ALIAS,
            "--api-key", API_KEY,
            "--ctx-size", str(CONTEXT_SIZE),
            "--threads", "16",
            "--n-gpu-layers", gpu_layers,
            "--parallel", "1",
            "--fit", "on",
            "--fit-target", "1024",
            "--fit-ctx", "4096",
            "--metrics",
        ]
        with LOG_PATH.open("ab") as output:
            output.write(("\n\n=== llama.cpp start " + time.strftime("%Y-%m-%d %H:%M:%S") + f" ({gpu_layers}) ===\n").encode())
            output.flush()
            process = subprocess.Popen(
                args,
                cwd=ENGINE_DIR,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
        self._process = process
        PID_PATH.write_text(str(process.pid), encoding="ascii")
        self._mode = "CPU fallback" if gpu_layers == "0" else "automatic CPU/GPU fit"
        self._starting = True
        return process

    def _watch_startup(self, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if self._health():
                with self._lock:
                    self._starting = False
                    self._last_error = ""
                return
            if process.poll() is not None:
                break
            time.sleep(1)

        with self._lock:
            if self._cancel_startup:
                self._starting = False
                PID_PATH.unlink(missing_ok=True)
                return
            if self._health():
                self._starting = False
                return
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            if self._mode != "CPU fallback" and SERVER_PATH.is_file() and MODEL_PATH.is_file():
                try:
                    fallback = self._launch("0")
                    self._last_error = "自动混合装载未就绪，正在回退到 CPU 推理。"
                except OSError as exc:
                    self._starting = False
                    self._last_error = str(exc)
                    return
            else:
                self._starting = False
                self._last_error = self._read_log_tail()
                PID_PATH.unlink(missing_ok=True)
                return

        fallback_deadline = time.monotonic() + 900
        while time.monotonic() < fallback_deadline:
            if self._health():
                with self._lock:
                    self._starting = False
                    self._last_error = ""
                return
            if fallback.poll() is not None:
                break
            time.sleep(1)
        with self._lock:
            if fallback.poll() is None:
                fallback.terminate()
                try:
                    fallback.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    fallback.kill()
            self._starting = False
            self._last_error = self._read_log_tail()
            PID_PATH.unlink(missing_ok=True)

    def _read_log_tail(self) -> str:
        try:
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-24:])[-4000:]
        except OSError:
            return "llama-server 进程已退出。"

    def start(self) -> dict[str, Any]:
        with self._lock:
            current = self._server_process()
            if current:
                return self.status()
            if not SERVER_PATH.is_file():
                raise RuntimeError(f"没有找到 llama-server：{SERVER_PATH}")
            if not MODEL_PATH.is_file() or MODEL_PATH.stat().st_size < EXPECTED_MODEL_BYTES * 0.98:
                raise RuntimeError(f"Qwen3.8-27B Q3_K_M 权重尚未下载完成：{MODEL_PATH}")
            try:
                self._cancel_startup = False
                process = self._launch("auto")
            except OSError as exc:
                raise RuntimeError(f"无法启动 llama-server：{exc}") from exc
            self._watcher = threading.Thread(
                target=self._watch_startup,
                args=(process,),
                daemon=True,
                name="llamacpp-startup-watch",
            )
            self._watcher.start()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._cancel_startup = True
            process = self._server_process()
            if process:
                try:
                    process.terminate()
                    process.wait(timeout=12)
                except (psutil.TimeoutExpired, psutil.Error):
                    try:
                        process.kill()
                    except psutil.Error:
                        pass
                except OSError:
                    pass
            self._process = None
            self._starting = False
            PID_PATH.unlink(missing_ok=True)
            return self.status()

    def status(self) -> dict[str, Any]:
        process = self._server_process()
        running = bool(process)
        ready = self._health() if running else False
        size = MODEL_PATH.stat().st_size if MODEL_PATH.is_file() else 0
        status = (
            "ready" if ready else
            "loading" if running and self._starting else
            "degraded" if running else
            "error" if self._last_error else
            "stopped"
        )
        process_rss = None
        process_cpu = None
        if process:
            try:
                process_rss = process.memory_info().rss
                cpu_times = process.cpu_times()
                cpu_seconds = cpu_times.user + cpu_times.system
                sampled_at = time.monotonic()
                with self._lock:
                    previous = self._last_cpu_sample
                    if previous and previous[0] == process.pid and sampled_at > previous[1]:
                        process_cpu = max(
                            0.0,
                            (cpu_seconds - previous[2])
                            * 100
                            / (sampled_at - previous[1])
                            / max(1, os.cpu_count() or 1),
                        )
                    self._last_cpu_sample = (process.pid, sampled_at, cpu_seconds)
            except psutil.Error:
                pass
        else:
            with self._lock:
                self._last_cpu_sample = None
        metrics = self._metrics() if ready else {}
        return {
            "engine_installed": SERVER_PATH.is_file(),
            "model_installed": size >= EXPECTED_MODEL_BYTES * 0.98,
            "model_path": str(MODEL_PATH),
            "model_size_bytes": size,
            "expected_model_size_bytes": EXPECTED_MODEL_BYTES,
            "model_alias": MODEL_ALIAS,
            "server_url": SERVER_URL + "/v1",
            "status": status,
            "running": running,
            "ready": ready,
            "loading": bool(running and self._starting and not ready),
            "pid": process.pid if process else None,
            "process_rss_bytes": process_rss,
            "process_cpu_percent": process_cpu,
            "metrics": metrics,
            "placement": self._mode if running else None,
            "context_size": CONTEXT_SIZE,
            "gpu_policy": "auto with 1 GiB reserve; CPU fallback on startup failure",
            "last_error": self._last_error,
            "log_path": str(LOG_PATH),
        }

    def _metrics(self) -> dict[str, float | None]:
        """Read llama.cpp's local Prometheus endpoint without blocking the sampler."""
        try:
            response = httpx.get(
                SERVER_URL + "/metrics",
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=0.8,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return {}
        names = {
            "prompt_tokens_seconds": "prompt_tokens_per_second",
            "predicted_tokens_seconds": "generation_tokens_per_second",
            "requests_processing": "requests_processing",
            "requests_deferred": "requests_deferred",
            "prompt_tokens_total": "prompt_tokens_total",
            "tokens_predicted_total": "tokens_predicted_total",
        }
        values: dict[str, float | None] = {label: None for label in names.values()}
        for line in response.text.splitlines():
            if not line or line.startswith("#"):
                continue
            name, _, raw_value = line.partition(" ")
            label = names.get(name.removeprefix("llamacpp:"))
            if not label:
                continue
            try:
                values[label] = float(raw_value.strip())
            except ValueError:
                continue
        return values


async def _chat_stream(payload: dict[str, Any]):
    """Stream chat completions from the local llama.cpp server (OpenAI 兼容)。"""
    body = {
        "model": MODEL_ALIAS,
        "messages": payload["messages"],
        "max_tokens": max(1, min(int(payload.get("max_tokens") or 512), 8192)),
        "temperature": max(0.0, min(float(payload.get("temperature") or 0.7), 2.0)),
        "top_p": max(0.01, min(float(payload.get("top_p") or 0.9), 1.0)),
        "stream": True,
    }
    if payload.get("stop"):
        body["stop"] = payload["stop"]
    async with httpx.AsyncClient(
        base_url=SERVER_URL + "/v1",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=httpx.Timeout(30.0, read=None),
    ) as client:
        async with client.stream("POST", "/chat/completions", json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for choice in chunk.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        yield delta


async def _chat_complete(payload: dict[str, Any]) -> dict[str, Any]:
    """Non-streaming chat completion,返回 OpenAI 风格的 content+usage。"""
    body = {
        "model": MODEL_ALIAS,
        "messages": payload["messages"],
        "max_tokens": max(1, min(int(payload.get("max_tokens") or 512), 8192)),
        "temperature": max(0.0, min(float(payload.get("temperature") or 0.2), 2.0)),
        "stream": False,
    }
    async with httpx.AsyncClient(
        base_url=SERVER_URL + "/v1",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=300,
    ) as client:
        response = await client.post("/chat/completions", json=body)
        response.raise_for_status()
        data = response.json()
    choice = (data.get("choices") or [{}])[0]
    usage = data.get("usage") or {}
    return {
        "content": ((choice.get("message") or {}).get("content")) or "",
        "tool_calls": [],
        "usage": {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
    }


llama_runtime = LlamaCppRuntime()

# 作为 local_runtime 的 external backend 注册：GGUF 引擎启动后，
# local/qwen38-gguf-27b 的请求会被路由到 llama.cpp 服务器。
llama_runtime.chat_stream = _chat_stream  # type: ignore[attr-defined]
llama_runtime.chat_complete = _chat_complete  # type: ignore[attr-defined]
