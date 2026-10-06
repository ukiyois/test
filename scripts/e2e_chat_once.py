"""发一次对话并打印 done 事件的 usage，验证影子 run + 消耗统计。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:7860"
DEEPSEEK = "remote/provider_1c5ca35385b443aca6c20d810337d94a/deepseek-flash"


def chat(conv_id, message, agent_mode):
    body = json.dumps({
        "conversation_id": conv_id,
        "model": DEEPSEEK,
        "message": message,
        "max_tokens": 512,
        "agent_mode": agent_mode,
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/api/chat/stream", data=body,
        headers={"Content-Type": "application/json"},
    )
    done = None
    tools = []
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                evt = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if evt.get("tool"):
                tools.append(evt.get("tool"))
            if "content" in evt and "usage" in evt:
                done = evt
    return done, tools


if __name__ == "__main__":
    conv_id = sys.argv[1]
    agent = len(sys.argv) > 2 and sys.argv[2] == "agent"
    msg = sys.argv[3] if len(sys.argv) > 3 else "用一句话介绍你自己。"
    done, tools = chat(conv_id, msg, agent)
    print("TOOLS:", tools)
    if done:
        print("USAGE:", json.dumps(done.get("usage")))
        print("AGENT_MODE:", done.get("agent_mode"))
        print("CONTENT:", (done.get("content") or "")[:150])
    else:
        print("NO DONE EVENT")
