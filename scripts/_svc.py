"""start.bat / stop.bat 的辅助脚本。子命令：
  build        打印当前平台构建标识
  health       若服务健康且为当前构建，打印构建标识；否则打印空行
  pid          打印正在运行的平台服务进程 PID；未运行则打印空行
  check <pid>  若该 PID 是平台服务进程则退出码 0，否则 1
  stop         优雅停止 GGUF 引擎后返回（忽略所有错误）
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PORT = 7860


def build_id() -> str:
    from llmplatform.buildinfo import get_build_id
    return get_build_id()


def cmd_build() -> None:
    print(build_id())


def cmd_health() -> None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/api/health", timeout=2
        ) as resp:
            data = json.load(resp)
        if data.get("status") == "ok":
            print(data.get("build_id", ""))
        else:
            print("")
    except Exception:
        print("")


def cmd_stop() -> None:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/runtime/gguf/stop", method="POST"
        )
        urllib.request.urlopen(req, timeout=30)
    except Exception:
        pass


def _find_server_pid():
    import psutil

    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        joined = " ".join(cmdline)
        if "uvicorn" in joined and "llmplatform.app" in joined:
            return proc.info["pid"]
    return None


def cmd_pid() -> None:
    pid = _find_server_pid()
    print(pid if pid else "")


def cmd_check(arg: str) -> int:
    pid = _find_server_pid()
    return 0 if pid and str(pid) == arg.strip() else 1


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "build":
        cmd_build()
    elif action == "health":
        cmd_health()
    elif action == "pid":
        cmd_pid()
    elif action == "check":
        sys.exit(cmd_check(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif action == "stop":
        cmd_stop()
    else:
        print(f"未知子命令: {action}", file=sys.stderr)
        sys.exit(2)
