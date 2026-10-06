/* 对话空间：会话栏 + 消息流 + 输入区 + 参数弹层 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useChat, type AgentActivity, type ApprovalMode } from "@/stores/chat";
import { useRuntime } from "@/stores/runtime";
import { api } from "@/api/client";
import type { ChatMessage } from "@/api/types";
import { Markdown } from "@/components/Markdown";
import { FlywheelSvg, GearSvg } from "@/components/Gear";
import { toast } from "@/stores/ui";

/* ---------- 自强化工具活动面板 ---------- */

const TOOL_LABELS: Record<string, string> = {
  read_file: "读取文件",
  write_file: "写入文件",
  apply_patch: "应用补丁",
  run_command: "执行命令",
  restart_service: "重启服务",
  calculator: "计算器",
  current_time: "查询时间",
  search_files: "检索文件",
  semantic_search: "语义检索",
};

function activitySummary(a: AgentActivity): string {
  const args = a.arguments ?? {};
  const path = typeof args.path === "string" ? args.path : "";
  const cmd = typeof args.command === "string" ? args.command : "";
  if (path) return path;
  if (cmd) return cmd;
  return "";
}

function AgentPanel() {
  const activities = useChat((s) => s.agentActivities);
  const resolveApproval = useChat((s) => s.resolveApproval);
  if (activities.length === 0) return null;
  return (
    <div className="agent-panel">
      <div className="agent-panel__head">
        <span className="lamp lamp--accent lamp--blink" />
        自强化操作 · {activities.length}
      </div>
      <div className="agent-panel__list">
        {activities.map((a, i) => (
          <div key={i} className={`agent-row${a.pendingApproval ? " is-pending" : ""}`}>
            <span
              className={`lamp ${
                a.pendingApproval
                  ? "lamp--warning lamp--blink"
                  : a.ok === false
                    ? "lamp--danger"
                    : a.approved === false
                      ? "lamp--danger"
                      : "lamp--accent"
              }`}
            />
            <div className="agent-row__body">
              <b>{TOOL_LABELS[a.tool] ?? a.tool}</b>
              <small className="mono">{activitySummary(a)}</small>
              {a.error && <small className="agent-row__err">{a.error}</small>}
              {a.approved === false && <small className="agent-row__err">已被拒绝</small>}
            </div>
            {a.pendingApproval && a.approvalToken && (
              <div className="agent-row__actions">
                <button
                  className="btn btn--primary btn--sm"
                  onClick={() => resolveApproval(a.approvalToken!, true)}
                >
                  批准
                </button>
                <button
                  className="btn btn--ghost btn--sm"
                  onClick={() => resolveApproval(a.approvalToken!, false)}
                >
                  拒绝
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------- 会话栏 ---------- */

function groupConversations(list: { id: string; title: string; updated_at: string }[]) {
  const now = Date.now();
  const day = 86400_000;
  const groups: Record<string, typeof list> = {};
  for (const c of list) {
    const t = Date.parse(c.updated_at);
    const label =
      now - t < day ? "今天" : now - t < 7 * day ? "最近 7 天" : "更早";
    (groups[label] ??= []).push(c);
  }
  return Object.entries(groups);
}

function ConvSidebar() {
  const conversations = useChat((s) => s.conversations);
  const activeId = useChat((s) => s.activeId);
  const select = useChat((s) => s.selectConversation);
  const create = useChat((s) => s.newConversation);
  const remove = useChat((s) => s.removeConversation);
  const rename = useChat((s) => s.renameConversation);
  const streaming = useChat((s) => s.streaming.handle !== null);

  const [query, setQuery] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");

  const filtered = useMemo(
    () =>
      conversations.filter((c) =>
        c.title.toLowerCase().includes(query.trim().toLowerCase()),
      ),
    [conversations, query],
  );

  const commitRename = (id: string) => {
    const title = editText.trim();
    if (title) rename(id, title);
    setEditingId(null);
  };

  return (
    <aside className="chat__sidebar">
      <div className="chat__sidebar-head">
        <input
          placeholder="搜索会话…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button className="btn btn--ghost" onClick={() => create()} data-tip="新对话">
          +
        </button>
      </div>
      <div className="chat__conv-list">
        {groupConversations(filtered).map(([label, items]) => (
          <div key={label}>
            <div className="chat__conv-group">{label}</div>
            {items.map((c) => (
              <button
                key={c.id}
                className={`chat__conv${c.id === activeId ? " is-active" : ""}`}
                onClick={() => !streaming && select(c.id)}
                onDoubleClick={() => {
                  setEditingId(c.id);
                  setEditText(c.title);
                }}
              >
                {c.id === activeId && <span className="chat__conv-dot" />}
                <span className="chat__conv-title">
                  {editingId === c.id ? (
                    <input
                      autoFocus
                      value={editText}
                      onChange={(e) => setEditText(e.target.value)}
                      onBlur={() => commitRename(c.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") commitRename(c.id);
                        if (e.key === "Escape") setEditingId(null);
                      }}
                      onClick={(e) => e.stopPropagation()}
                    />
                  ) : (
                    c.title
                  )}
                </span>
                <span
                  className="chat__conv-del"
                  role="button"
                  tabIndex={-1}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (!streaming) remove(c.id).catch((err) => toast.error(String(err)));
                  }}
                >
                  ×
                </span>
              </button>
            ))}
          </div>
        ))}
        {filtered.length === 0 && (
          <div className="chat__conv-group">暂无会话，点 + 新建</div>
        )}
      </div>
    </aside>
  );
}

/* ---------- 思考链（借鉴 deepseek-harness 的 reasoning block 呈现） ---------- */

function ThinkingBlock({ text, streaming }: { text: string; streaming: boolean }) {
  // 流式生成时默认展开，结束后默认折叠；用户可手动切换
  const [open, setOpen] = useState(streaming);
  const wasStreaming = useRef(streaming);
  useEffect(() => {
    if (wasStreaming.current && !streaming) setOpen(false); // 思考结束自动收起
    wasStreaming.current = streaming;
  }, [streaming]);
  if (!text) return null;
  const lines = text.split("\n").length;
  return (
    <div className={`think${streaming ? " is-thinking" : ""}`}>
      <button className="think__head" onClick={() => setOpen((v) => !v)}>
        <span className={`think__chev${open ? " is-open" : ""}`}>▸</span>
        <span className="think__lamp" />
        <span className="think__label">
          {streaming ? "思考中…" : "思考过程"}
        </span>
        <span className="think__meta digit">
          {text.length} 字 · {lines} 行
        </span>
      </button>
      {open && <div className="think__body">{text}</div>}
    </div>
  );
}

/* ---------- 消息 ---------- */

const SUGGESTIONS = [
  { title: "总结这个项目的架构", hint: "让它读一遍代码库" },
  { title: "写一个 Python 快速排序并解释", hint: "试试代码生成" },
  { title: "帮我起草一封英文邮件", hint: "写作场景" },
];

function MessageItem({
  msg,
  isStreaming,
  tokenRate,
  isLast,
}: {
  msg: ChatMessage;
  isStreaming: boolean;
  tokenRate: number;
  isLast: boolean;
}) {
  const regenerate = useChat((s) => s.regenerate);
  const setDraft = useChat((s) => s.setDraft);
  const isUser = msg.role === "user";

  const copy = () => {
    navigator.clipboard.writeText(msg.content).then(
      () => toast.success("已复制"),
      () => toast.error("复制失败"),
    );
  };

  // 飞轮转速挂钩 token 速率：1.2s 基准，最快 0.3s/圈
  const duration = Math.max(0.3, Math.min(1.2, tokenRate > 0 ? 24 / tokenRate : 1.2));

  // 机加工单数据（仅 assistant 完成后有）
  const u = msg.usage;
  const tokPerSec =
    !isUser && msg.elapsed_ms && u ? Math.round((u.completion_tokens / (msg.elapsed_ms / 1000)) * 10) / 10 : null;

  return (
    <div className={`msg ${isUser ? "msg--user" : "msg--assistant"}${isStreaming ? " is-streaming" : ""}`}>
      <div className="msg__avatar">{isUser ? "你" : "AI"}</div>
      <div className="msg__body">
        <div className="msg__meta">
          <span>{isUser ? "你" : msg.model || "assistant"}</span>
          <span>·</span>
          <span>{new Date(msg.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</span>
          {isStreaming && tokenRate > 0 && (
            <>
              <span>·</span>
              <span className="mono">{tokenRate} tok/s</span>
            </>
          )}
        </div>
        <div className="msg__content">
          {isStreaming && (
            <span className="msg__fly">
              <span
                className="gear"
                style={{ animationDuration: `${duration}s` }}
              >
                <FlywheelSvg size={16} />
              </span>
            </span>
          )}
          {!isUser && msg.reasoning && (
            <ThinkingBlock text={msg.reasoning} streaming={isStreaming && !msg.content} />
          )}
          {isUser ? msg.content : <Markdown text={msg.content} />}
          {isStreaming && <span style={{ color: "var(--accent)" }}>▍</span>}
        </div>
        {/* 机加工单：耗时 / tok·s / prompt-completion 拆分 */}
        {!isUser && !isStreaming && (u || msg.elapsed_ms != null) && (
          <div className="msg__jobcard">
            {msg.elapsed_ms != null && (
              <span className="msg__jobcell">
                <small>耗时</small>
                <b className="digit">{(msg.elapsed_ms / 1000).toFixed(2)}s</b>
              </span>
            )}
            {tokPerSec != null && (
              <span className="msg__jobcell">
                <small>速率</small>
                <b className="digit">{tokPerSec} tok/s</b>
              </span>
            )}
            {u && (
              <span className="msg__jobcell">
                <small>Token</small>
                <b className="digit">
                  {u.prompt_tokens}→{u.completion_tokens}
                </b>
              </span>
            )}
            {u && (
              <span className="msg__jobbar" data-tip={`输入 ${u.prompt_tokens} / 输出 ${u.completion_tokens} / 共 ${u.total_tokens}`}>
                <span
                  className="msg__jobbar-in"
                  style={{ width: `${u.total_tokens > 0 ? (u.prompt_tokens / u.total_tokens) * 100 : 0}%` }}
                />
                <span
                  className="msg__jobbar-out"
                  style={{ width: `${u.total_tokens > 0 ? (u.completion_tokens / u.total_tokens) * 100 : 0}%` }}
                />
              </span>
            )}
            {msg.finish_reason && msg.finish_reason !== "stop" && (
              <span className="badge badge--warning">{msg.finish_reason}</span>
            )}
          </div>
        )}
        {!isStreaming && msg.content && (
          <div className="msg__actions">
            <button className="btn btn--ghost" onClick={copy}>
              复制
            </button>
            {!isUser && isLast && (
              <button className="btn btn--ghost" onClick={() => regenerate()}>
                重新生成
              </button>
            )}
            {isUser && (
              <button
                className="btn btn--ghost"
                onClick={() => {
                  setDraft(msg.content);
                }}
              >
                再次编辑
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------- 输入区 ---------- */

function Composer() {
  const draft = useChat((s) => s.draft);
  const setDraft = useChat((s) => s.setDraft);
  const send = useChat((s) => s.send);
  const stop = useChat((s) => s.stop);
  const streaming = useChat((s) => s.streaming.handle !== null);
  const activeId = useChat((s) => s.activeId);
  const fileIds = useChat((s) => s.fileIds);
  const setFileIds = useChat((s) => s.setFileIds);
  const params = useChat((s) => s.params);
  const setParams = useChat((s) => s.setParams);
  const agentMode = useChat((s) => s.agentMode);
  const setAgentMode = useChat((s) => s.setAgentMode);
  const approvalMode = useChat((s) => s.approvalMode);
  const setApprovalMode = useChat((s) => s.setApprovalMode);

  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [fileNames, setFileNames] = useState<Record<string, string>>({});
  const [showParams, setShowParams] = useState(false);

  // 自动增高
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(200, ta.scrollHeight)}px`;
  }, [draft]);

  const upload = async (files: FileList | null) => {
    if (!files) return;
    for (const f of Array.from(files)) {
      if (f.size > 15 * 1024 * 1024) {
        toast.warning(`${f.name} 超过 15MB，已跳过`);
        continue;
      }
      try {
        const rec = await api.uploadFile(f);
        setFileIds([...useChat.getState().fileIds, rec.id]);
        setFileNames((m) => ({ ...m, [rec.id]: f.name }));
        toast.success(`已上传 ${f.name}`);
      } catch (err) {
        toast.error(`上传失败：${String(err)}`);
      }
    }
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!streaming) send();
    }
  };

  return (
    <div className="chat__composer">
      <div className="chat__composer-inner">
        {fileIds.length > 0 && (
          <div className="chat__files">
            {fileIds.map((id) => (
              <span key={id} className="file-chip">
                <span className="file-chip__name">{fileNames[id] ?? id.slice(0, 8)}</span>
                <button
                  onClick={() => setFileIds(fileIds.filter((x) => x !== id))}
                  aria-label="移除"
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="chat__row">
          <button
            className="btn btn--ghost"
            onClick={() => fileRef.current?.click()}
            data-tip="附加文件"
            disabled={streaming}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
              <path d="M21 12.5l-8.5 8.5a5.5 5.5 0 01-7.8-7.8l8.5-8.5a3.7 3.7 0 015.2 5.2l-8.5 8.5a1.85 1.85 0 01-2.6-2.6l7.8-7.8" />
            </svg>
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              upload(e.target.files);
              e.target.value = "";
            }}
          />
          <textarea
            ref={taRef}
            className="chat__input"
            placeholder={activeId ? "输入消息，Enter 发送，Shift+Enter 换行" : "先新建一个会话"}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKey}
            disabled={!activeId}
            rows={1}
          />
          <button
            className={`agent-toggle${agentMode ? " is-on" : ""}`}
            onClick={() => setAgentMode(!agentMode)}
            data-tip={agentMode ? "自强化助手已开启：模型可改代码并热更新" : "开启自强化助手"}
            disabled={streaming}
          >
            <span className="agent-toggle__lamp" />
            <span className="agent-toggle__label">助手</span>
          </button>
          {agentMode && (
            <select
              className="approval-mode"
              value={approvalMode}
              onChange={(e) => setApprovalMode(e.target.value as ApprovalMode)}
              data-tip="审批模式：手动=每步确认 / 自动=AI审核安全操作 / 完全=项目内直接执行"
              disabled={streaming}
            >
              <option value="manual">手动审批</option>
              <option value="auto">自动审批</option>
              <option value="full">完全访问</option>
            </select>
          )}
          <div style={{ position: "relative" }}>
            <button
              className="btn btn--ghost"
              onClick={() => setShowParams((v) => !v)}
              data-tip="生成参数"
            >
              <GearSvg size={16} color="currentColor" />
            </button>
            {showParams && (
              <div className="params-pop">
                <div className="params-pop__row">
                  <div className="params-pop__label">
                    <span>temperature</span>
                    <span className="mono">{params.temperature.toFixed(2)}</span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={2}
                    step={0.05}
                    value={params.temperature}
                    onChange={(e) => setParams({ temperature: Number(e.target.value) })}
                  />
                </div>
                <div className="params-pop__row">
                  <div className="params-pop__label">
                    <span>top_p</span>
                    <span className="mono">{params.top_p.toFixed(2)}</span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={params.top_p}
                    onChange={(e) => setParams({ top_p: Number(e.target.value) })}
                  />
                </div>
                <div className="params-pop__row">
                  <div className="params-pop__label">
                    <span>max_tokens</span>
                    <span className="mono">{params.max_tokens}</span>
                  </div>
                  <input
                    type="range"
                    min={64}
                    max={4096}
                    step={64}
                    value={params.max_tokens}
                    onChange={(e) => setParams({ max_tokens: Number(e.target.value) })}
                  />
                </div>
              </div>
            )}
          </div>
          <button
            className={`chat__send${streaming ? " is-stop" : ""}`}
            onClick={() => (streaming ? stop() : send())}
            disabled={!streaming && (!draft.trim() || !activeId)}
            data-tip={streaming ? "停止生成" : "发送"}
          >
            {streaming ? (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                <rect x="5" y="5" width="14" height="14" rx="2" />
              </svg>
            ) : (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                <path d="M7 4l14 8-14 8z" />
              </svg>
            )}
          </button>
        </div>
        <div className="chat__foot">
          <span className="mono">
            temp {params.temperature.toFixed(2)} · top_p {params.top_p.toFixed(2)} · max{" "}
            {params.max_tokens}
          </span>
          <span style={{ flex: 1 }} />
          <span>Enter 发送 · Shift+Enter 换行</span>
        </div>
      </div>
    </div>
  );
}

/* ---------- 主视图 ---------- */

export function ChatView() {
  const messages = useChat((s) => s.messages);
  const activeId = useChat((s) => s.activeId);
  const streaming = useChat((s) => s.streaming);
  const selectedModel = useChat((s) => s.selectedModel);
  const fetchConversations = useChat((s) => s.fetchConversations);
  const setDraft = useChat((s) => s.setDraft);
  const models = useRuntime((s) => s.models);
  const fetchModels = useRuntime((s) => s.fetchModels);
  const scrollRef = useRef<HTMLDivElement>(null);
  const msgRefs = useRef<Record<string, HTMLDivElement | null>>({});

  useEffect(() => {
    fetchConversations().catch((err) => toast.error(String(err)));
    if (models.length === 0) fetchModels().catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 自动滚到底部
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const lastAssistantIdx = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "assistant") return i;
    }
    return -1;
  })();

  /* 上下文油表：用最近一次 usage 的 prompt_tokens 估算占用 */
  const contextLength = useMemo(() => {
    const m = models.find((x) => x.id === selectedModel || `local/${x.id}` === selectedModel || selectedModel.endsWith(`/${x.id}`));
    return m?.context_length ?? null;
  }, [models, selectedModel]);
  const lastUsage = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].usage) return messages[i].usage!;
    }
    return null;
  }, [messages]);
  const ctxPct = contextLength && lastUsage ? Math.min(100, (lastUsage.prompt_tokens / contextLength) * 100) : null;
  const ctxTone = ctxPct == null ? "var(--copper)" : ctxPct > 85 ? "var(--danger)" : ctxPct > 65 ? "var(--warning)" : "var(--copper)";

  const jumpTo = (id: string) => {
    const el = msgRefs.current[id];
    if (el && scrollRef.current) {
      scrollRef.current.scrollTo({ top: el.offsetTop - 12, behavior: "smooth" });
    }
  };

  return (
    <div className="chat">
      <ConvSidebar />
      <div className="chat__main">
        {/* 上下文油表 */}
        {messages.length > 0 && (
          <div className="chat__ctx">
            <span className="chat__ctx-label">上下文</span>
            <div className="chat__ctx-track" data-tip={lastUsage ? `已用 ${lastUsage.prompt_tokens} / ${contextLength ?? "?"} tokens` : "尚无用量采样"}>
              <span
                className="chat__ctx-fill"
                style={{ width: `${ctxPct ?? 0}%`, background: ctxTone }}
              />
              <span className="chat__ctx-tick" style={{ left: "65%" }} />
              <span className="chat__ctx-tick" style={{ left: "85%" }} />
            </div>
            <b className="digit" style={{ color: ctxTone }}>
              {ctxPct != null ? `${ctxPct.toFixed(0)}%` : "—"}
            </b>
            {lastUsage && <small className="chat__ctx-num digit">{lastUsage.prompt_tokens} tok</small>}
          </div>
        )}
        <div className="chat__bodywrap">
          <div className="chat__scroll" ref={scrollRef}>
            <AgentPanel />
            {messages.length === 0 ? (
              <div className="chat__welcome">
                <div>
                  <h1>本地模型工作台</h1>
                  <p>{activeId ? "开始新的对话" : "从左侧选择一个会话，或新建对话"}</p>
                </div>
                {activeId && (
                  <div className="chat__suggest">
                    {SUGGESTIONS.map((s) => (
                      <button
                        key={s.title}
                        className="suggest-card"
                        onClick={() => setDraft(s.title)}
                      >
                        {s.title}
                        <small>{s.hint}</small>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              messages.map((m, i) => (
                <div key={m.id} ref={(el) => { msgRefs.current[m.id] = el; }}>
                  <MessageItem
                    msg={m}
                    isStreaming={streaming.messageId === m.id}
                    tokenRate={streaming.tokenRate}
                    isLast={i === lastAssistantIdx}
                  />
                </div>
              ))
            )}
          </div>
          {/* 右侧消息导航刻度轨 */}
          {messages.length > 1 && (
            <nav className="chat__rail">
              {messages.map((m, i) => (
                <button
                  key={m.id}
                  className={`chat__rail-dot${m.role === "user" ? " is-user" : ""}`}
                  data-tip={`${i + 1}. ${m.role === "user" ? "你" : "AI"} · ${new Date(m.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`}
                  onClick={() => jumpTo(m.id)}
                  aria-label={`跳到第 ${i + 1} 条`}
                />
              ))}
            </nav>
          )}
        </div>
        <Composer />
      </div>
    </div>
  );
}
