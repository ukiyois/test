"""自强化对话端到端实测：agent 模式触发工具 + 成本入账。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:7860"
DEEPSEEK = "remote/provider_1c5ca35385b443aca6c20d810337d94a/deepseek-flash"


def chat_stream(conv_id: str, message: str, agent_mode: bool):
    body = json.dumps({
        "conversation_id": conv_id,
        "model": DEEPSEEK,
        "message": message,
        "max_tokens": 1024,
        "agent_mode": agent_mode,
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/api/chat/stream",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    events = []
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                evt = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            events.append(evt)
    return events


def summarize(events, tag):
    kinds = {}
    tool_calls = []
    approvals = []
    final_text = ""
    for e in events:
        t = e.get("type", "?")
        kinds[t] = kinds.get(t, 0) + 1
        if t == "tool_call":
            tool_calls.append(e.get("tool"))
        if t == "approval_required":
            approvals.append((e.get("tool"), e.get("approval_token")))
        if t == "done":
            final_text = (e.get("message") or {}).get("content", "")[:200]
    print(f"--- {tag} ---")
    print("event types:", kinds)
    print("tool_calls:", tool_calls)
    print("approvals:", approvals)
    print("final:", final_text)
    print()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("all", "agent"):
        # agent 模式：read_file 免审批
        evts = chat_stream("e2e-agent", "请用 read_file 工具读取 llmplatform/app.py 的前 10 行，然后告诉我文件第一行是什么。", True)
        summarize(evts, "AGENT read_file (免审批)")
    if mode in ("all", "plain"):
        # 普通对话：产生远程 token 计成本
        evts = chat_stream("e2e-cost", "用一句话介绍你自己。", False)
        summarize(evts, "PLAIN 对话 (计成本)")
