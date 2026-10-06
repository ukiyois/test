/* ============================================================
   Clockwork Bench — 统一 API client
   所有组件不直接 fetch，经由此层；错误统一抛 ApiError。
   ============================================================ */

import type {
  ChatMessage,
  Conversation,
  FileRecord,
  GgufStatus,
  ModelInfo,
  MonitorSnapshot,
  Provider,
  Run,
  RuntimeStatus,
  SystemInfo,
  ToolInfo,
  Workflow,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers:
        options.body instanceof FormData
          ? options.headers
          : { "Content-Type": "application/json", ...options.headers },
      ...options,
    });
  } catch {
    throw new ApiError(0, "无法连接服务，请确认后端已启动。");
  }
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* keep default */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const put = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });
const del = <T>(path: string) => request<T>(path, { method: "DELETE" });

/* ---------- 系统 / 运行时 ---------- */

export const api = {
  health: () =>
    get<{ status: string; runtime: RuntimeStatus }>("/api/health"),
  system: () => get<SystemInfo>("/api/system"),
  monitoring: () => get<MonitorSnapshot>("/api/monitoring"),
  saveThresholds: (values: Record<string, unknown> | null, reset = false) =>
    put("/api/monitoring/thresholds", { values, reset }),
  ackAlert: (alertId: string) => post(`/api/monitoring/alerts/${alertId}/ack`),

  /* 平台 API Key */
  createApiKey: () => post<{ api_key: string }>("/api/system/api-key"),
  deleteApiKey: () => del("/api/system/api-key"),

  /* ---------- 模型 ---------- */
  models: () =>
    get<{ data: ModelInfo[]; excluded: string[]; runtime: RuntimeStatus }>(
      "/api/models",
    ),
  loadModel: (modelId: string) =>
    post<RuntimeStatus>(`/api/models/${encodeURIComponent(modelId)}/load`),
  unloadModel: () => post<RuntimeStatus>("/api/models/unload"),

  /* GGUF 引擎 */
  ggufStatus: () => get<GgufStatus>("/api/runtime/gguf"),
  ggufStart: () => post<GgufStatus>("/api/runtime/gguf/start"),
  ggufStop: () => post<GgufStatus>("/api/runtime/gguf/stop"),

  /* ---------- 服务商 ---------- */
  providers: () => get<{ data: Provider[] }>("/api/providers"),
  saveProvider: (body: {
    name: string;
    base_url: string;
    api_key?: string | null;
    model_ids?: string[] | null;
  }) => post<Provider>("/api/providers", body),
  updateProvider: (id: string, body: {
    name: string;
    base_url: string;
    api_key?: string | null;
    model_ids?: string[] | null;
  }) => put<Provider>(`/api/providers/${id}`, body),
  deleteProvider: (id: string) => del(`/api/providers/${id}`),
  discoverProvider: (id: string) => post(`/api/providers/${id}/discover`),
  testProvider: (id: string) => post(`/api/providers/${id}/test`),

  /* ---------- 会话 ---------- */
  conversations: () => get<{ data: Conversation[] }>("/api/conversations"),
  createConversation: (title: string | null, model: string) =>
    post<Conversation>("/api/conversations", { title, model }),
  messages: (conversationId: string) =>
    get<{ data: ChatMessage[] }>(`/api/conversations/${conversationId}/messages`),
  deleteConversation: (id: string) => del(`/api/conversations/${id}`),

  abortChat: (model: string) => post("/api/chat/abort", { model }),

  /* 自强化审批 */
  approveChat: (approval_token: string, approved: boolean) =>
    post("/api/chat/approve", { approval_token, approved }),

  /* 定价 / 预算 */
  pricing: () =>
    get<{ pricing: Record<string, { prompt: number; completion: number }>; budgets: Record<string, number> }>(
      "/api/pricing",
    ),
  setPricing: (model_key: string, prompt: number, completion: number) =>
    post("/api/pricing", { model_key, prompt, completion }),
  setBudget: (period: string, amount_usd: number) =>
    post("/api/budget", { period, amount_usd }),

  /* ---------- 文件 ---------- */
  async uploadFile(file: File): Promise<FileRecord> {
    const form = new FormData();
    form.append("file", file);
    return request<FileRecord>("/api/files", { method: "POST", body: form });
  },
  fileInfo: (id: string) => get<FileRecord>(`/api/files/${id}`),

  /* ---------- 工作流 ---------- */
  workflows: () =>
    get<{ data: Workflow[]; tools: ToolInfo[] }>("/api/workflows"),
  saveWorkflow: (definition: unknown, id?: string) =>
    id
      ? put<Workflow>(`/api/workflows/${id}`, { definition })
      : post<Workflow>("/api/workflows", { definition }),
  validateWorkflow: (definition: unknown) =>
    post<{ valid: boolean; errors?: string[] }>("/api/workflows/validate", {
      definition,
    }),
  publishWorkflow: (id: string) => post<Workflow>(`/api/workflows/${id}/publish`),
  unpublishWorkflow: (id: string) => post<Workflow>(`/api/workflows/${id}/unpublish`),
  deleteWorkflow: (id: string) => del(`/api/workflows/${id}`),
  startRun: (workflowId: string, input: string, fileIds: string[] = []) =>
    post<{ run_id: string; status: string }>(
      `/api/workflows/${workflowId}/runs`,
      { input, file_ids: fileIds },
    ),
  startDeployedRun: (workflowId: string, input: string, fileIds: string[] = []) =>
    post<{ run_id: string; status: string; workflow_version?: number }>(
      `/api/deployed-agents/${workflowId}/runs`,
      { input, file_ids: fileIds },
    ),

  /* ---------- 运行 ---------- */
  runs: (limit = 50) => get<{ data: Run[] }>(`/api/runs?limit=${limit}`),
  run: (id: string) => get<Run>(`/api/runs/${id}`),
  resumeRun: (id: string, approved: boolean, note = "") =>
    post(`/api/runs/${id}/resume`, { approved, note }),
  cancelRun: (id: string) => post(`/api/runs/${id}/cancel`),
};

/* ---------- 流式对话 ---------- */

export interface StreamHandle {
  done: Promise<void>;
  abort: () => void;
}

export interface StreamCallbacks {
  onStart?: (info: { message_id: string; conversation_id: string }) => void;
  onDelta?: (text: string) => void;
  onReasoning?: (text: string) => void;
  onToolCall?: (info: { tool: string; arguments: Record<string, unknown> }) => void;
  onToolResult?: (info: { tool: string; ok: boolean; result?: unknown; error?: string }) => void;
  onApprovalRequired?: (info: {
    approval_token: string;
    tool: string;
    arguments: Record<string, unknown>;
  }) => void;
  onApprovalResolved?: (info: { tool: string; approved: boolean }) => void;
  onDone?: (info: {
    message_id: string;
    content: string;
    reasoning?: string;
    finish_reason?: string;
    elapsed_ms?: number;
    usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number } | null;
    agent_mode?: boolean;
  }) => void;
  onError?: (message: string) => void;
}

/**
 * 发起 SSE 对话流。fetch + ReadableStream 手动解析 SSE 帧，
 * 返回带 abort() 的句柄（同时调用后端 abort 停掉本地推理）。
 */
export function streamChat(
  body: {
    conversation_id: string;
    model: string;
    message: string;
    file_ids?: string[];
    max_tokens?: number;
    temperature?: number;
    top_p?: number;
    agent_mode?: boolean;
    approval_mode?: "manual" | "auto" | "full";
  },
  callbacks: StreamCallbacks,
): StreamHandle {
  const controller = new AbortController();

  const done = (async () => {
    let response: Response;
    try {
      response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (exc) {
      if (!controller.signal.aborted) {
        callbacks.onError?.("无法连接服务，请确认后端已启动。");
      }
      throw exc;
    }
    if (!response.ok || !response.body) {
      let detail = `请求失败（${response.status}）`;
      try {
        const payload = await response.json();
        if (typeof payload?.detail === "string") detail = payload.detail;
      } catch {
        /* keep */
      }
      callbacks.onError?.(detail);
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let eventName = "";
    let dataLines: string[] = [];

    const dispatch = () => {
      if (!eventName || dataLines.length === 0) {
        eventName = "";
        dataLines = [];
        return;
      }
      const raw = dataLines.join("\n");
      let payload: Record<string, unknown> = {};
      try {
        payload = JSON.parse(raw);
      } catch {
        payload = {};
      }
      if (eventName === "start") {
        callbacks.onStart?.(payload as never);
      } else if (eventName === "delta") {
        callbacks.onDelta?.(String(payload.text ?? ""));
      } else if (eventName === "reasoning") {
        callbacks.onReasoning?.(String(payload.text ?? ""));
      } else if (eventName === "tool_call") {
        callbacks.onToolCall?.(payload as never);
      } else if (eventName === "tool_result") {
        callbacks.onToolResult?.(payload as never);
      } else if (eventName === "approval_required") {
        callbacks.onApprovalRequired?.(payload as never);
      } else if (eventName === "approval_resolved") {
        callbacks.onApprovalResolved?.(payload as never);
      } else if (eventName === "done") {
        callbacks.onDone?.(payload as never);
      } else if (eventName === "error") {
        callbacks.onError?.(String(payload.message ?? "未知错误"));
      }
      eventName = "";
      dataLines = [];
    };

    try {
      for (;;) {
        const { value, done: finished } = await reader.read();
        if (finished) break;
        buffer += decoder.decode(value, { stream: true });
        let index: number;
        while ((index = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, index).replace(/\r$/, "");
          buffer = buffer.slice(index + 1);
          if (line === "") {
            dispatch();
          } else if (line.startsWith("event:")) {
            eventName = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).replace(/^ /, ""));
          }
        }
      }
      dispatch();
    } catch (exc) {
      if (!controller.signal.aborted) {
        callbacks.onError?.("连接中断，已保留当前输出。");
      }
      throw exc;
    }
  })().catch(() => undefined);

  return {
    done,
    abort: () => {
      controller.abort();
      // 同时通知后端中止本地推理线程；远程流由连接断开自然终止。
      api.abortChat(body.model).catch(() => undefined);
    },
  };
}

/* ---------- 运行事件 SSE ---------- */

export interface RunStreamHandle {
  close: () => void;
}

export interface RunStreamCallbacks {
  onSnapshot?: (data: {
    status: string;
    current_node: string | null;
    pending: Record<string, unknown> | null;
    error: string | null;
    events: unknown[];
  }) => void;
  onEvent?: (data: {
    node_id: string | null;
    type: string;
    payload: Record<string, unknown>;
    created_at: string;
  }) => void;
  onStatus?: (data: {
    status: string;
    current_node: string | null;
    pending: Record<string, unknown> | null;
    error: string | null;
  }) => void;
  onClose?: () => void;
}

export function streamRun(
  runId: string,
  callbacks: RunStreamCallbacks,
): RunStreamHandle {
  const source = new EventSource(`/api/runs/${runId}/events`);
  source.addEventListener("snapshot", (ev) => {
    try {
      callbacks.onSnapshot?.(JSON.parse((ev as MessageEvent).data));
    } catch {
      /* ignore */
    }
  });
  source.addEventListener("event", (ev) => {
    try {
      callbacks.onEvent?.(JSON.parse((ev as MessageEvent).data));
    } catch {
      /* ignore */
    }
  });
  source.addEventListener("status", (ev) => {
    try {
      callbacks.onStatus?.(JSON.parse((ev as MessageEvent).data));
    } catch {
      /* ignore */
    }
  });
  source.onerror = () => {
    // 终态后服务端主动断开，EventSource 会触发 error；交给上层决定重连。
    callbacks.onClose?.();
  };
  return {
    close: () => source.close(),
  };
}
