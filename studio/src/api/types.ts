/* ============================================================
   Clockwork Bench — 全局类型（与 llmplatform API 对齐）
   ============================================================ */

export interface RuntimeStatus {
  loaded: boolean;
  model_id: string | null;
  placement?: Record<string, unknown>;
  device?: string;
  dtype?: string;
  last_error?: string | null;
  external_backend?: string | null;
  gguf_runtime?: GgufStatus;
  [key: string]: unknown;
}

export interface GgufStatus {
  running: boolean;
  model_path?: string | null;
  port?: number;
  pid?: number | null;
  last_error?: string | null;
  download?: { percent?: number; done?: boolean; total?: number } | null;
  [key: string]: unknown;
}

export interface ModelInfo {
  id: string;
  name?: string;
  kind?: string; // local | remote
  available?: boolean;
  parameter_size?: string | null;
  size_bytes?: number | null;
  context_length?: number | null;
  provider?: string;
  [key: string]: unknown;
}

export interface Provider {
  id: string;
  name: string;
  base_url: string;
  has_key?: boolean;
  model_ids: string[];
  created_at?: string;
  [key: string]: unknown;
}

export interface Conversation {
  id: string;
  title: string;
  model: string;
  created_at: string;
  updated_at: string;
}

export interface ChatUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  /** 推理模型的思考链（reasoning_content），assistant 且模型支持时非空 */
  reasoning?: string;
  model?: string;
  file_ids?: string[];
  created_at: string;
  /** 机加工单数据（assistant 消息完成后回填） */
  usage?: ChatUsage | null;
  elapsed_ms?: number;
  finish_reason?: string;
}

export interface FileRecord {
  id: string;
  filename: string;
  size: number;
  characters?: number;
  media_type?: string;
  preview?: string;
}

export interface WorkflowNode {
  id: string;
  type: string;
  label?: string;
  next?: string | string[] | null;
  [key: string]: unknown;
}

export interface WorkflowDef {
  name: string;
  description?: string;
  model?: string;
  nodes: WorkflowNode[];
  [key: string]: unknown;
}

export interface Workflow {
  id: string;
  name: string;
  description?: string;
  definition: WorkflowDef;
  version: number;
  published_version?: number | null;
  updated_at?: string;
  [key: string]: unknown;
}

export interface ToolInfo {
  id: string;
  name?: string;
  description?: string;
  parameters?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface RunEvent {
  id: number;
  node_id: string | null;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Run {
  id: string;
  workflow_id: string;
  workflow_version?: number;
  status:
    | "queued"
    | "running"
    | "waiting_approval"
    | "cancel_requested"
    | "completed"
    | "failed"
    | "cancelled";
  input: Record<string, unknown>;
  state?: Record<string, unknown>;
  current_node: string | null;
  pending?: Record<string, unknown> | null;
  error?: string | null;
  parent_run_id?: string | null;
  parent_node_id?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
  updated_at?: string;
  events?: RunEvent[];
  children?: Run[];
  workflow_snapshot?: WorkflowDef;
  [key: string]: unknown;
}

export interface MonitorSnapshot {
  system?: Record<string, unknown>;
  harness?: Record<string, unknown>;
  alerts?: MonitorAlert[];
  thresholds?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface MonitorAlert {
  id: string;
  level: "info" | "warning" | "danger";
  metric?: string;
  message: string;
  value?: number;
  threshold?: number;
  acknowledged?: boolean;
  created_at: string;
  [key: string]: unknown;
}

export interface SystemInfo {
  cpu_percent?: number;
  memory_percent?: number;
  memory_used_gb?: number;
  memory_total_gb?: number;
  gpu?: {
    name?: string;
    utilization?: number;
    memory_used_gb?: number;
    memory_total_gb?: number;
    temperature?: number;
  } | null;
  [key: string]: unknown;
}
