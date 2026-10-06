"""自强化工具集：让模型在沙箱约束下读写项目代码并热更新。

借鉴 Codex 的 patch/apply 与审批机制，按正式标准实现：
  - 路径沙箱：所有文件操作限制在项目根内，解析符号链接/相对路径防逃逸，
    并拒绝敏感路径（.git、密钥、虚拟环境、数据目录）。
  - apply_patch：结构化 diff 应用，写前自动备份到 data/patch_backups/，
    支持回滚（restore_backup）。原子写入（临时文件 + os.replace）。
  - run_command：工作目录锁定项目根 + 超时；不做白名单，仅拦截系统级危险
    命令（删盘/格式化/fork 炸弹等），其余交给审批门裁决。
  - restart_service：调用项目 stop.ps1/start.ps1 热重启后端。
  - 危险操作（写/删/执行/重启）需经审批门，由调用方（对话/工作流）裁决。

所有文件/命令操作以 asyncio.to_thread 运行，避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# 项目根（llmplatform 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 沙箱内禁止触碰的相对路径（大小写不敏感，匹配路径任一段）
_FORBIDDEN_SEGMENTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".env",
}
# 禁止写入/删除的具体相对路径前缀（数据与密钥目录）
_FORBIDDEN_PREFIXES = (
    "data/secrets",
    "data/server.log",
)
# 允许写入的扩展名（代码/配置/文档/样式）；为空表示不限制扩展名
_ALLOWED_WRITE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".json", ".md",
    ".html", ".txt", ".toml", ".cfg", ".ps1", ".yml", ".yaml",
}
# run_command 拒绝的系统级危险命令关键词（小写子串匹配）；
# 不做白名单——项目内自我迭代需要自由执行 ls/find/cat/python/npm 等任意命令。
# 只拦真正会破坏系统/项目的操作，其余交给审批门（manual/auto/full）裁决。
_BLOCKED_COMMAND_KEYWORDS = (
    "rm -rf /", "rm -rf ~", "rm -rf *", "del /f/s/q", "rmdir /s /q c:",
    "format c:", "format d:", "mkfs", ":(){:|:&};:",  # fork 炸弹
    "dd if=", "shutdown", "reboot", "takeown", "icacls c:\\",
    "rd /s /q c:\\", "del /s /q c:\\", "erase /s /q c:\\",
)
_RUN_COMMAND_TIMEOUT = 180
_BACKUP_DIR = PROJECT_ROOT / "data" / "patch_backups"
_MAX_FILE_BYTES = 2_000_000  # 2MB，防止读/写超大文件
_MAX_OUTPUT_CHARS = 8000
_MAX_LIST_ENTRIES = 400
_MAX_SEARCH_HITS = 120
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}


class SandboxError(ValueError):
    """沙箱约束被违反。"""


def _resolve_safe_path(path: str, *, for_write: bool = False) -> Path:
    """把用户给定路径解析为项目根内的绝对路径，拒绝逃逸与敏感目标。"""
    if not path or not str(path).strip():
        raise SandboxError("路径不能为空。")
    candidate = Path(str(path).replace("\\", "/").lstrip("/"))
    if candidate.is_absolute():
        # 允许绝对路径，但必须落在项目根内
        resolved = Path(os.path.realpath(str(candidate)))
    else:
        resolved = Path(os.path.realpath(str(PROJECT_ROOT / candidate)))
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise SandboxError(f"路径越出项目根，已拒绝：{path}") from exc
    rel = resolved.relative_to(PROJECT_ROOT)
    parts = {p.lower() for p in rel.parts}
    if parts & _FORBIDDEN_SEGMENTS:
        raise SandboxError(f"路径包含受保护目录，已拒绝：{path}")
    rel_str = rel.as_posix().lower()
    for prefix in _FORBIDDEN_PREFIXES:
        if rel_str == prefix or rel_str.startswith(prefix + "/") or rel_str.startswith(prefix):
            raise SandboxError(f"路径受保护，已拒绝：{path}")
    if for_write and resolved.suffix.lower() not in _ALLOWED_WRITE_SUFFIXES:
        raise SandboxError(f"不允许写入该类型文件：{resolved.suffix or '(无扩展名)'}")
    return resolved


def _to_project_rel(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def list_files(path: str = ".", pattern: str = "*", max_results: int = 200) -> dict[str, Any]:
    """列出项目内目录内容（默认跳过 node_modules/.git 等噪音目录）。"""
    base = _resolve_safe_path(path or ".")
    if not base.exists():
        raise SandboxError(f"路径不存在：{path}")
    max_results = max(1, min(int(max_results or 200), _MAX_LIST_ENTRIES))
    entries: list[dict[str, Any]] = []
    if base.is_file():
        return {"root": _to_project_rel(base), "entries": [{"path": _to_project_rel(base), "type": "file", "size": base.stat().st_size}]}
    for root, dirs, files in os.walk(str(base)):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        root_path = Path(root)
        for d in sorted(dirs):
            if len(entries) >= max_results:
                break
            p = root_path / d
            if fnmatch.fnmatch(d, pattern or "*"):
                entries.append({"path": _to_project_rel(p), "type": "dir"})
        for f in sorted(files):
            if len(entries) >= max_results:
                break
            p = root_path / f
            if fnmatch.fnmatch(f, pattern or "*"):
                entries.append({"path": _to_project_rel(p), "type": "file", "size": p.stat().st_size})
        if len(entries) >= max_results:
            break
    return {"root": _to_project_rel(base), "pattern": pattern, "count": len(entries), "entries": entries}


def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
    """读取项目内文件内容（可按行区间），返回带行号的文本。"""
    resolved = _resolve_safe_path(path)
    if not resolved.is_file():
        raise SandboxError(f"文件不存在：{path}")
    if resolved.stat().st_size > _MAX_FILE_BYTES:
        raise SandboxError(f"文件过大（>{_MAX_FILE_BYTES}B），拒绝读取：{path}")
    text = resolved.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(start_line or 1))
    end = total if end_line in (None, 0) else min(total, int(end_line))
    if start > total:
        return {"path": _to_project_rel(resolved), "total_lines": total, "content": ""}
    body = "\n".join(f"{i + 1}: {lines[i]}" for i in range(start - 1, end))
    if len(body) > _MAX_OUTPUT_CHARS:
        body = body[:_MAX_OUTPUT_CHARS] + "\n…[输出已截断]"
    return {
        "path": _to_project_rel(resolved),
        "total_lines": total,
        "start_line": start,
        "end_line": end,
        "content": body,
    }


def grep_code(query: str, path: str = ".", file_pattern: str = "*", max_results: int = 80) -> dict[str, Any]:
    """在项目源码中做大小写不敏感文本搜索，返回命中文件/行号/片段。"""
    q = str(query or "").strip()
    if not q:
        raise SandboxError("搜索关键词不能为空。")
    base = _resolve_safe_path(path or ".")
    if not base.exists():
        raise SandboxError(f"路径不存在：{path}")
    max_results = max(1, min(int(max_results or 80), _MAX_SEARCH_HITS))
    hits: list[dict[str, Any]] = []
    if base.is_file():
        targets = [base]
    else:
        targets = []
        for root, dirs, files in os.walk(str(base)):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for f in files:
                if fnmatch.fnmatch(f, file_pattern or "*"):
                    targets.append(Path(root) / f)
    ql = q.lower()
    for fp in targets:
        if len(hits) >= max_results:
            break
        try:
            if fp.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if ql in line.lower():
                hits.append({"path": _to_project_rel(fp), "line": i, "text": line.strip()[:220]})
                if len(hits) >= max_results:
                    break
    return {"query": q, "count": len(hits), "hits": hits}


def list_backups(max_results: int = 100) -> dict[str, Any]:
    """列出 data/patch_backups 中最近的补丁备份，便于回滚选择。"""
    max_results = max(1, min(int(max_results or 100), _MAX_LIST_ENTRIES))
    if not _BACKUP_DIR.is_dir():
        return {"backup_dir": str(_BACKUP_DIR), "count": 0, "backups": []}
    backups = sorted(_BACKUP_DIR.glob("*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "backup_dir": str(_BACKUP_DIR),
        "count": min(len(backups), max_results),
        "backups": [
            {"path": str(p), "name": p.name, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)}
            for p in backups[:max_results]
        ],
    }


def agent_plan(task: str = "") -> dict[str, Any]:
    """返回自强化任务的推荐工作流（探查→最小修改→验证→生效），不修改任何文件。"""
    return {
        "task": task or "(未指定)",
        "steps": [
            {"step": 1, "action": "探查", "tools": ["list_files", "grep_code", "read_file"], "note": "读懂目标文件/相关代码，不要猜结构"},
            {"step": 2, "action": "最小修改", "tools": ["apply_patch"], "note": "优先局部补丁；仅整文件重写才用 write_file"},
            {"step": 3, "action": "验证", "tools": ["run_command"], "note": "前端改完 npx vite build；改逻辑可跑 python -c 或 pytest"},
            {"step": 4, "action": "生效", "tools": ["restart_service"], "note": "后端 Python 改动需重启才生效"},
            {"step": 5, "action": "汇报", "tools": [], "note": "总结改了哪些文件、如何验证"},
        ],
        "reminders": [
            "危险操作（write_file/apply_patch/run_command/restart_service）会自动弹审批",
            "路径用相对项目根，如 llmplatform/app.py",
            "改坏了可用 list_backups 找到备份再 restore_backup 回滚",
        ],
    }


def write_file(path: str, content: str) -> dict[str, Any]:
    """整文件覆盖写入（自动备份原文件），返回变更摘要。"""
    resolved = _resolve_safe_path(path, for_write=True)
    if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
        raise SandboxError(f"内容过大（>{_MAX_FILE_BYTES}B），拒绝写入。")
    backup = _backup_file(resolved)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(resolved, content)
    return {
        "path": _to_project_rel(resolved),
        "bytes": len(content.encode("utf-8")),
        "backup": backup,
        "created": backup is None,
    }


def apply_patch(path: str, search: str, replace: str, occurrence: str = "first") -> dict[str, Any]:
    """结构化补丁：把文件中匹配的 search 文本替换为 replace。

    occurrence: "first" 只替换第一处；"all" 替换全部。
    写前自动备份，失败不修改文件。
    """
    resolved = _resolve_safe_path(path, for_write=True)
    if not resolved.is_file():
        raise SandboxError(f"文件不存在：{path}")
    original = resolved.read_text(encoding="utf-8", errors="replace")
    if search not in original:
        raise SandboxError("未找到匹配的 search 文本，补丁未应用。")
    count = original.count(search)
    if occurrence == "all":
        new_text = original.replace(search, replace)
        applied = count
    else:
        new_text = original.replace(search, replace, 1)
        applied = 1
    backup = _backup_file(resolved)
    _atomic_write(resolved, new_text)
    return {
        "path": _to_project_rel(resolved),
        "replacements": applied,
        "matched_occurrences": count,
        "backup": backup,
    }


def restore_backup(backup_path: str) -> dict[str, Any]:
    """从备份回滚一个文件。"""
    backup = Path(os.path.realpath(str(backup_path)))
    try:
        backup.relative_to(_BACKUP_DIR)
    except ValueError as exc:
        raise SandboxError("备份路径非法。") from exc
    if not backup.is_file():
        raise SandboxError(f"备份不存在：{backup_path}")
    # 备份文件名编码了原始相对路径：rel__path__to__file.TIMESTAMP.bak
    name = backup.name
    rel_encoded = name.split(".")[0]
    rel = rel_encoded.replace("__", "/")
    target = _resolve_safe_path(rel, for_write=True)
    shutil.copyfile(str(backup), str(target))
    return {"restored": _to_project_rel(target), "from": backup.name}


def run_command(command: str) -> dict[str, Any]:
    """在项目根执行命令，工作目录锁定项目根；仅拦截系统级危险操作。"""
    cmd = str(command).strip()
    if not cmd:
        raise SandboxError("命令不能为空。")
    lowered = cmd.lower()
    if any(k in lowered for k in _BLOCKED_COMMAND_KEYWORDS):
        raise SandboxError(f"系统级危险命令，已拒绝：{cmd[:40]}")
    started = time.monotonic()
    # Windows cmd 默认 GBK，子进程中文输出常乱码；先切 UTF-8 再执行命令。
    if os.name == "nt" and not lowered.startswith("chcp "):
        cmd = "chcp 65001 >nul && " + cmd
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            shell=True,
            capture_output=True,
            text=True,
            timeout=_RUN_COMMAND_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    except subprocess.TimeoutExpired:
        return {
            "command": cmd,
            "ok": False,
            "error": f"命令超时（>{_RUN_COMMAND_TIMEOUT}s）",
        }
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + "\n…[输出已截断]"
    return {
        "command": cmd,
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "elapsed_s": round(time.monotonic() - started, 2),
        "output": output.strip(),
    }


def restart_service() -> dict[str, Any]:
    """热重启后端服务（stop.ps1 → start.ps1 -NoBrowser）。"""
    stop = PROJECT_ROOT / "stop.ps1"
    start = PROJECT_ROOT / "start.ps1"
    if not stop.is_file() or not start.is_file():
        raise SandboxError("未找到 stop.ps1 / start.ps1。")
    # 后台异步拉起新进程，避免阻塞当前请求导致重启失败
    ps = (
        f"Start-Sleep -Seconds 1; "
        f"& '{stop}' | Out-Null; Start-Sleep -Seconds 2; "
        f"& '{start}' -NoBrowser | Out-Null"
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        cwd=str(PROJECT_ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {"restarting": True, "note": "后端将在数秒内重启，前端构建需先执行 npx vite build。"}


def _atomic_write(path: Path, content: str) -> None:
    """原子写入：先写临时文件再 os.replace，避免半截文件。"""
    tmp = path.with_name(path.name + ".tmp_write")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _backup_file(path: Path) -> str | None:
    """把现有文件备份到 data/patch_backups/，返回备份路径；文件不存在返回 None。"""
    if not path.is_file():
        return None
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = _BACKUP_DIR / f"{rel.replace('/', '__')}.{stamp}.bak"
    shutil.copyfile(str(path), str(backup))
    return str(backup)


# ---- 工具 schema（供 OpenAI tools / 本地 JSON 工具调用）----

SELF_IMPROVE_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {
        "description": "列出项目内目录/文件（自动跳过 .git/node_modules 等噪音目录）。用于低成本探查结构。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根的目录，默认 ."},
                "pattern": {"type": "string", "description": "文件名匹配模式，如 *.py、*.tsx，默认 *"},
                "max_results": {"type": "integer", "description": "最大返回条数，默认 200"},
            },
        },
    },
    "grep_code": {
        "description": "在项目源码中做大小写不敏感文本搜索，返回命中文件/行号/片段。比 search_files 更适合查代码。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要搜索的关键词"},
                "path": {"type": "string", "description": "搜索范围目录，默认项目根"},
                "file_pattern": {"type": "string", "description": "文件名匹配，如 *.py，默认 *"},
                "max_results": {"type": "integer", "description": "最大命中数，默认 80"},
            },
            "required": ["query"],
        },
    },
    "list_backups": {
        "description": "列出 data/patch_backups 中最近的补丁备份，便于选择回滚点。",
        "parameters": {
            "type": "object",
            "properties": {"max_results": {"type": "integer", "description": "最大返回条数，默认 100"}},
        },
    },
    "agent_plan": {
        "description": "返回当前任务的推荐自强化工作流（探查→最小修改→验证→生效），不修改任何文件。",
        "parameters": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "任务描述，可选"}},
        },
    },
    "read_file": {
        "description": "读取项目内代码/配置文件内容（带行号，可按行区间）。用于修改前理解现状。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根的路径，如 llmplatform/app.py 或 studio/src/views/ChatView.tsx"},
                "start_line": {"type": "integer", "description": "起始行（默认 1）"},
                "end_line": {"type": "integer", "description": "结束行（默认到文件末尾）"},
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "description": "整文件覆盖写入（自动备份原文件，可回滚）。适合新建或整体重写。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根的目标路径"},
                "content": {"type": "string", "description": "完整文件内容"},
            },
            "required": ["path", "content"],
        },
    },
    "apply_patch": {
        "description": "结构化局部修改：把文件中的 search 文本替换为 replace。优先于 write_file，改动更小更安全。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根的目标路径"},
                "search": {"type": "string", "description": "要被替换的原文（须与文件内容完全一致，含缩进）"},
                "replace": {"type": "string", "description": "替换后的新文本"},
                "occurrence": {"type": "string", "enum": ["first", "all"], "description": "替换第一处还是全部（默认 first）"},
            },
            "required": ["path", "search", "replace"],
        },
    },
    "run_command": {
        "description": "在项目根执行任意命令（如 ls、find、cat、python、npx vite build、git status）。用于探查/构建/验证。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令（工作目录锁定项目根）"},
            },
            "required": ["command"],
        },
    },
    "restart_service": {
        "description": "热重启后端服务，使 Python 代码改动生效。前端改动需先 run_command 执行 npx vite build。",
        "parameters": {"type": "object", "properties": {}},
    },
}

# 危险操作集合：调用方需先经审批门裁决
DANGEROUS_TOOLS = {"write_file", "apply_patch", "run_command", "restart_service"}


async def execute_self_improve_tool(tool_id: str, arguments: dict[str, Any]) -> Any:
    """在线程池执行自强化工具，避免阻塞事件循环。"""
    if tool_id == "list_files":
        return await asyncio.to_thread(
            list_files,
            str(arguments.get("path", ".")),
            str(arguments.get("pattern", "*")),
            int(arguments.get("max_results") or 200),
        )
    if tool_id == "grep_code":
        return await asyncio.to_thread(
            grep_code,
            str(arguments.get("query", "")),
            str(arguments.get("path", ".")),
            str(arguments.get("file_pattern", "*")),
            int(arguments.get("max_results") or 80),
        )
    if tool_id == "list_backups":
        return await asyncio.to_thread(
            list_backups, int(arguments.get("max_results") or 100)
        )
    if tool_id == "agent_plan":
        return await asyncio.to_thread(agent_plan, str(arguments.get("task", "")))
    if tool_id == "read_file":
        return await asyncio.to_thread(
            read_file,
            str(arguments.get("path", "")),
            int(arguments.get("start_line") or 1),
            arguments.get("end_line"),
        )
    if tool_id == "write_file":
        return await asyncio.to_thread(
            write_file, str(arguments.get("path", "")), str(arguments.get("content", ""))
        )
    if tool_id == "apply_patch":
        return await asyncio.to_thread(
            apply_patch,
            str(arguments.get("path", "")),
            str(arguments.get("search", "")),
            str(arguments.get("replace", "")),
            str(arguments.get("occurrence", "first")),
        )
    if tool_id == "run_command":
        return await asyncio.to_thread(run_command, str(arguments.get("command", "")))
    if tool_id == "restart_service":
        return await asyncio.to_thread(restart_service)
    raise SandboxError(f"未注册的自强化工具：{tool_id}")
