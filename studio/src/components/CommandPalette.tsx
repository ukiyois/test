/* Ctrl+K 命令面板：跳转视图 / 新建会话 / 切模型 / 运行工作流 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "wouter";
import { useUi } from "../stores/ui";
import { useChat } from "../stores/chat";
import { useRuntime } from "../stores/runtime";
import { useWorkflows } from "../stores/workflows";

interface Command {
  id: string;
  label: string;
  hint?: string;
  group: string;
  run: () => void;
}

export function CommandPalette() {
  const { cmdkOpen, setCmdk } = useUi();
  const [, navigate] = useLocation();
  const { newConversation, setModel } = useChat();
  const { models } = useRuntime();
  const { workflows, fetchWorkflows } = useWorkflows();
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (cmdkOpen) {
      setQuery("");
      setCursor(0);
      fetchWorkflows().catch(() => undefined);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [cmdkOpen, fetchWorkflows]);

  const commands = useMemo<Command[]>(() => {
    const list: Command[] = [
      { id: "go-chat", label: "对话", hint: "1", group: "跳转", run: () => navigate("/") },
      { id: "go-flow", label: "Agent 工作流", hint: "2", group: "跳转", run: () => navigate("/workflow") },
      { id: "go-models", label: "模型与 API", hint: "3", group: "跳转", run: () => navigate("/models") },
      { id: "go-runs", label: "运行记录", hint: "4", group: "跳转", run: () => navigate("/runs") },
      { id: "go-monitor", label: "运行监控", hint: "5", group: "跳转", run: () => navigate("/monitor") },
      {
        id: "new-chat",
        label: "新建会话",
        group: "操作",
        run: () => {
          newConversation().catch(() => undefined);
          navigate("/");
        },
      },
    ];
    for (const m of models) {
      if (m.available === false) continue;
      list.push({
        id: `model-${m.id}`,
        label: `切换模型：${m.name ?? m.id}`,
        group: "模型",
        run: () => {
          setModel(m.id);
          navigate("/");
        },
      });
    }
    for (const w of workflows.slice(0, 6)) {
      list.push({
        id: `flow-${w.id}`,
        label: `打开工作流：${w.name}`,
        group: "工作流",
        run: () => navigate(`/workflow/${w.id}`),
      });
    }
    return list;
  }, [models, workflows, navigate, newConversation, setModel]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter((c) => c.label.toLowerCase().includes(q));
  }, [commands, query]);

  useEffect(() => {
    setCursor(0);
  }, [query]);

  if (!cmdkOpen) return null;

  const pick = (cmd: Command | undefined) => {
    if (!cmd) return;
    setCmdk(false);
    cmd.run();
  };

  return (
    <div className="modal-mask cmdk-mask" onClick={() => setCmdk(false)}>
      <div className="cmdk" onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          value={query}
          placeholder="输入命令或搜索…"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setCursor((c) => Math.min(filtered.length - 1, c + 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setCursor((c) => Math.max(0, c - 1));
            } else if (e.key === "Enter") {
              e.preventDefault();
              pick(filtered[cursor]);
            } else if (e.key === "Escape") {
              setCmdk(false);
            }
          }}
        />
        <div className="cmdk__list">
          {filtered.length === 0 && (
            <div className="cmdk__empty">没有匹配的命令</div>
          )}
          {filtered.map((cmd, i) => (
            <button
              key={cmd.id}
              className={`cmdk__item${i === cursor ? " is-active" : ""}`}
              onMouseEnter={() => setCursor(i)}
              onClick={() => pick(cmd)}
            >
              <span className="cmdk__group">{cmd.group}</span>
              <span className="cmdk__label">{cmd.label}</span>
              {cmd.hint && <span className="kbd">{cmd.hint}</span>}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
