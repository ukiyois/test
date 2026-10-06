/* 对话切片：会话列表、消息、流式发送/中止、会话级参数 */

import { create } from "zustand";
import { api, streamChat, type StreamHandle } from "../api/client";
import type { ChatMessage, Conversation } from "../api/types";

export interface ChatParams {
  temperature: number;
  top_p: number;
  max_tokens: number;
}

interface StreamingState {
  messageId: string | null;
  handle: StreamHandle | null;
  /** token 速率（个/秒），供飞轮调速 */
  tokenRate: number;
  interrupted: boolean;
}

/** 审批模式：manual 手动 / auto AI 审核 / full 完全访问 */
export type ApprovalMode = "manual" | "auto" | "full";

/** 自强化工具活动（一次工具调用及其结果/审批） */
export interface AgentActivity {
  tool: string;
  arguments?: Record<string, unknown>;
  ok?: boolean;
  result?: unknown;
  error?: string;
  approvalToken?: string;
  approved?: boolean;
  pendingApproval?: boolean;
}

interface ChatState {
  conversations: Conversation[];
  activeId: string | null;
  messages: ChatMessage[];
  streaming: StreamingState;
  draft: string;
  fileIds: string[];
  params: ChatParams;
  selectedModel: string;
  agentMode: boolean;
  approvalMode: ApprovalMode;
  agentActivities: AgentActivity[];

  fetchConversations: () => Promise<void>;
  selectConversation: (id: string) => Promise<void>;
  newConversation: () => Promise<void>;
  removeConversation: (id: string) => Promise<void>;
  renameConversation: (id: string, title: string) => void;
  setDraft: (text: string) => void;
  setParams: (patch: Partial<ChatParams>) => void;
  setModel: (model: string) => void;
  setFileIds: (ids: string[]) => void;
  setAgentMode: (on: boolean) => void;
  setApprovalMode: (mode: ApprovalMode) => void;
  resolveApproval: (token: string, approved: boolean) => Promise<void>;
  send: () => Promise<void>;
  stop: () => void;
  regenerate: () => Promise<void>;
}

const DEFAULT_PARAMS: ChatParams = {
  temperature: 0.7,
  top_p: 0.9,
  max_tokens: 768,
};

const DEFAULT_MODEL = "local/qwen3.5-0.8b";

export const useChat = create<ChatState>((set, get) => ({
  conversations: [],
  activeId: null,
  messages: [],
  streaming: { messageId: null, handle: null, tokenRate: 0, interrupted: false },
  draft: "",
  fileIds: [],
  params: DEFAULT_PARAMS,
  selectedModel: DEFAULT_MODEL,
  agentMode: false,
  approvalMode: "manual" as ApprovalMode,
  agentActivities: [],

  fetchConversations: async () => {
    const { data } = await api.conversations();
    data.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    set({ conversations: data });
    const { activeId } = get();
    if (!activeId && data.length > 0) {
      await get().selectConversation(data[0].id);
    }
  },

  selectConversation: async (id) => {
    set({ activeId: id, messages: [], draft: "", fileIds: [] });
    const { data } = await api.messages(id);
    set({ messages: data.filter((m) => m.content || m.role === "user") });
    const conv = get().conversations.find((c) => c.id === id);
    if (conv?.model) set({ selectedModel: conv.model });
  },

  newConversation: async () => {
    const conv = await api.createConversation(null, get().selectedModel);
    set((s) => ({
      conversations: [conv, ...s.conversations],
      activeId: conv.id,
      messages: [],
      draft: "",
      fileIds: [],
    }));
  },

  removeConversation: async (id) => {
    await api.deleteConversation(id);
    const remaining = get().conversations.filter((c) => c.id !== id);
    set({ conversations: remaining });
    if (get().activeId === id) {
      if (remaining.length > 0) {
        await get().selectConversation(remaining[0].id);
      } else {
        set({ activeId: null, messages: [] });
      }
    }
  },

  renameConversation: (id, title) => {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id ? { ...c, title } : c,
      ),
    }));
  },

  setDraft: (text) => set({ draft: text }),
  setParams: (patch) => set((s) => ({ params: { ...s.params, ...patch } })),
  setModel: (model) => set({ selectedModel: model }),
  setFileIds: (ids) => set({ fileIds: ids }),
  setAgentMode: (on) => set({ agentMode: on }),
  setApprovalMode: (mode) => set({ approvalMode: mode }),
  resolveApproval: async (token, approved) => {
    try {
      await api.approveChat(token, approved);
    } catch {
      /* 审批可能已超时 */
    }
    set((s) => ({
      agentActivities: s.agentActivities.map((a) =>
        a.approvalToken === token ? { ...a, pendingApproval: false, approved } : a,
      ),
    }));
  },

  send: async () => {
    const { activeId, draft, streaming, selectedModel, params, fileIds, agentMode, approvalMode } = get();
    const text = draft.trim();
    if (!activeId || !text || streaming.handle) return;

    const now = new Date().toISOString();
    const optimistic: ChatMessage = {
      id: `local-user-${Date.now()}`,
      conversation_id: activeId,
      role: "user",
      content: text,
      model: selectedModel,
      created_at: now,
    };
    set((s) => ({
      draft: "",
      fileIds: [],
      agentActivities: [],
      messages: [...s.messages, optimistic],
      streaming: { messageId: null, handle: null, tokenRate: 0, interrupted: false },
    }));

    let assistantText = "";
    let reasoningText = "";
    let tokenCount = 0;
    const startedAt = performance.now();

    const handle = streamChat(
      {
        conversation_id: activeId,
        model: selectedModel,
        message: text,
        file_ids: fileIds,
        max_tokens: params.max_tokens,
        temperature: params.temperature,
        top_p: params.top_p,
        agent_mode: agentMode,
        approval_mode: approvalMode,
      },
      {
        onStart: ({ message_id }) => {
          set((s) => ({
            streaming: { ...s.streaming, messageId: message_id },
            messages: [
              ...s.messages,
              {
                id: message_id,
                conversation_id: activeId,
                role: "assistant",
                content: "",
                model: selectedModel,
                created_at: new Date().toISOString(),
              },
            ],
          }));
        },
        onDelta: (piece) => {
          assistantText += piece;
          tokenCount += 1;
          const rate = Math.round(
            (tokenCount / Math.max(0.1, (performance.now() - startedAt) / 1000)) * 10,
          ) / 10;
          const messageId = get().streaming.messageId;
          set((s) => ({
            streaming: { ...s.streaming, tokenRate: rate },
            messages: s.messages.map((m) =>
              m.id === messageId ? { ...m, content: assistantText } : m,
            ),
          }));
        },
        onReasoning: (piece) => {
          reasoningText += piece;
          const messageId = get().streaming.messageId;
          set((s) => ({
            messages: s.messages.map((m) =>
              m.id === messageId ? { ...m, reasoning: reasoningText } : m,
            ),
          }));
        },
        onToolCall: ({ tool, arguments: args }) => {
          set((s) => ({
            agentActivities: [...s.agentActivities, { tool, arguments: args }],
          }));
        },
        onToolResult: ({ tool, ok, result, error }) => {
          set((s) => ({
            agentActivities: s.agentActivities.map((a, i) =>
              i === s.agentActivities.map((x) => x.tool).lastIndexOf(tool) &&
              a.ok === undefined
                ? { ...a, ok, result, error }
                : a,
            ),
          }));
        },
        onApprovalRequired: ({ approval_token, tool, arguments: args }) => {
          set((s) => ({
            agentActivities: [
              ...s.agentActivities,
              { tool, arguments: args, approvalToken: approval_token, pendingApproval: true },
            ],
          }));
        },
        onApprovalResolved: ({ tool, approved }) => {
          set((s) => ({
            agentActivities: s.agentActivities.map((a) =>
              a.tool === tool && a.pendingApproval
                ? { ...a, pendingApproval: false, approved }
                : a,
            ),
          }));
        },
        onDone: ({ content, reasoning, usage, elapsed_ms, finish_reason }) => {
          const messageId = get().streaming.messageId;
          set((s) => ({
            streaming: { messageId: null, handle: null, tokenRate: 0, interrupted: false },
            messages: s.messages.map((m) =>
              m.id === messageId
                ? { ...m, content, reasoning: reasoning ?? reasoningText, usage, elapsed_ms, finish_reason }
                : m,
            ),
          }));
          get().fetchConversations().catch(() => undefined);
        },
        onError: (message) => {
          const messageId = get().streaming.messageId;
          set((s) => ({
            streaming: { messageId: null, handle: null, tokenRate: 0, interrupted: true },
            messages: s.messages.map((m) =>
              m.id === messageId
                ? { ...m, content: m.content + `\n\n> ⚠ ${message}` }
                : m,
            ),
          }));
        },
      },
    );
    set((s) => ({ streaming: { ...s.streaming, handle } }));
    await handle.done;
  },

  stop: () => {
    const { streaming } = get();
    streaming.handle?.abort();
    set((s) => ({
      streaming: { ...s.streaming, handle: null, interrupted: true },
    }));
  },

  regenerate: async () => {
    const { messages, streaming } = get();
    if (streaming.handle) return;
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (!lastUser) return;
    // 去掉最后一轮 assistant 回复，重新发送最后一条用户消息
    const trimmed = [...messages];
    while (trimmed.length && trimmed[trimmed.length - 1].role === "assistant") {
      trimmed.pop();
    }
    set({ messages: trimmed, draft: lastUser.content });
    await get().send();
  },
}));
