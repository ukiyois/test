/* 工作流图数据模型：节点路由口 ↔ React Flow 边 的映射与校验。
   从旧 workflow-ui 迁入，逻辑保持一致，改为严格类型。 */

import type { WorkflowDef, WorkflowNode } from "@/api/types";

export const NODE_META: Record<string, { title: string; icon: string; color: string }> = {
  start: { title: "任务输入", icon: "↳", color: "start" },
  agent: { title: "Agent", icon: "✳", color: "agent" },
  llm: { title: "模型调用", icon: "◉", color: "llm" },
  tool: { title: "工具", icon: "⌘", color: "tool" },
  condition: { title: "条件分支", icon: "⑂", color: "condition" },
  loop: { title: "循环", icon: "↻", color: "loop" },
  parallel: { title: "并行汇总", icon: "⇉", color: "parallel" },
  fanout: { title: "并行分流", icon: "⑂", color: "fanout" },
  fanin: { title: "并行汇合", icon: "⇶", color: "fanin" },
  subworkflow: { title: "调用 Agent", icon: "↗", color: "subworkflow" },
  approval: { title: "人工审核", icon: "✓", color: "approval" },
  transform: { title: "文本整理", icon: "≋", color: "transform" },
  end: { title: "任务输出", icon: "↗", color: "end" },
};

export const ADDABLE = [
  "agent",
  "llm",
  "tool",
  "condition",
  "loop",
  "fanout",
  "parallel",
  "subworkflow",
  "approval",
  "transform",
] as const;

export const TOOL_LABELS: Record<string, string> = {
  calculator: "安全计算器",
  search_files: "文件关键词检索",
  semantic_search: "文件语义检索",
  current_time: "当前时间",
  http_request: "HTTP API 请求",
};

const ROUTE_KEY_NODE_TYPES = ["agent", "llm", "tool", "transform", "condition", "loop", "parallel", "fanin", "subworkflow"];

export interface Branch {
  id: string;
  label?: string;
  next?: string | null;
  [key: string]: unknown;
}

/** 每个节点类型可拉出的路由口（sourceHandle → 中文标签） */
export function routeKeys(node: WorkflowNode): [string, string][] {
  let routes: [string, string][] = [];
  if (node.type === "condition") routes = [["true_next", "满足"], ["false_next", "不满足"]];
  else if (node.type === "loop") routes = [["body_next", "循环体"], ["done_next", "完成"]];
  else if (node.type === "parallel") routes = [["next", "汇总后继续"]];
  else if (node.type === "fanout")
    routes = ((node.branches as Branch[] | undefined) || []).map((b) => [
      `branch:${b.id}`,
      b.label || b.id || "分支",
    ]);
  else if (node.type === "fanin") routes = [["next", "汇合后继续"]];
  else if (node.type === "subworkflow") routes = [["next", "返回后继续"]];
  else if (node.type === "approval") routes = [["on_approve", "通过"], ["on_reject", "退回"]];
  else if (["start", "agent", "llm", "tool", "transform"].includes(node.type)) routes = [["next", "继续"]];
  if (ROUTE_KEY_NODE_TYPES.includes(node.type)) routes.push(["error_next", "出错"]);
  return routes;
}

export function routeTarget(node: WorkflowNode, key: string): string | null {
  if (key.startsWith("branch:")) {
    const branchId = key.slice("branch:".length);
    return ((node.branches as Branch[] | undefined) || []).find((b) => b.id === branchId)?.next ?? null;
  }
  return (node[key] as string | null | undefined) ?? null;
}

export function withRouteTarget(node: WorkflowNode, key: string, target: string | null): WorkflowNode {
  if (key.startsWith("branch:")) {
    const branchId = key.slice("branch:".length);
    return {
      ...node,
      branches: ((node.branches as Branch[] | undefined) || []).map((b) =>
        b.id === branchId ? { ...b, next: target || null } : b,
      ),
    };
  }
  return { ...node, [key]: target || null };
}

/** 分层兜底布局：优先用已存 position，否则从 start 按 BFS 深度排布 */
export function graphPositions(definition: WorkflowDef): Record<string, { x: number; y: number }> {
  const depth: Record<string, number> = {};
  const start = definition.nodes.find((n) => n.type === "start");
  if (start) depth[start.id] = 0;
  const queue = start ? [start.id] : [];
  const nodeMap = Object.fromEntries(definition.nodes.map((n) => [n.id, n]));
  while (queue.length) {
    const id = queue.shift()!;
    const node = nodeMap[id];
    for (const [key] of routeKeys(node)) {
      const target = routeTarget(node, key);
      if (target && depth[target] == null) {
        depth[target] = depth[id] + 1;
        queue.push(target);
      }
    }
  }
  const rowAtDepth: Record<number, number> = {};
  return Object.fromEntries(
    definition.nodes.map((node, index) => {
      const pos = node.position as { x: number; y: number } | undefined;
      if (pos && Number.isFinite(pos.x) && Number.isFinite(pos.y)) return [node.id, pos];
      const layer = depth[node.id] ?? Math.floor(index / 3);
      const row = rowAtDepth[layer] || 0;
      rowAtDepth[layer] = row + 1;
      return [node.id, { x: 80 + layer * 320, y: 90 + row * 194 }];
    }),
  );
}

export function summaryFor(node: WorkflowNode): string {
  const s = (v: unknown) => (v == null ? "" : String(v));
  switch (node.type) {
    case "agent":
      return `${((node.tools as string[] | undefined) || []).length} 个工具 · 最多 ${node.max_iterations || 5} 轮`;
    case "llm":
      return s(node.model) || "继承工作流模型";
    case "tool":
      return TOOL_LABELS[s(node.tool)] || "选择工具";
    case "condition":
      return `${s(node.variable) || "last_output"}${node.variable_path ? `.${s(node.variable_path)}` : ""} · ${s(node.operator) || "contains"} · ${s(node.value) || "设置条件"}`;
    case "loop":
      return `${s(node.variable) || "last_output"} · 最多 ${node.max_iterations || 5} 轮`;
    case "parallel":
      return `${((node.branches as unknown[] | undefined) || []).length} 个并行分支 · 汇总结果`;
    case "fanout":
      return `${((node.branches as unknown[] | undefined) || []).length} 个图内分支 · 独立并发`;
    case "fanin":
      return `${node.failure_policy === "continue" ? "允许部分失败" : "全部成功后继续"}`;
    case "subworkflow":
      return `${s(node.workflow_name) || "选择已发布 Agent"} · 子运行可追踪`;
    case "approval":
      return s(node.message) || "等待人工审核";
    case "transform":
      return "使用变量整理文本";
    default:
      return node.type === "start" ? "文本、文件与任务参数" : "将上一步结果作为输出";
  }
}

export function newDefinition(name = "新建 Agent"): WorkflowDef {
  return {
    name,
    description: "编排模型、工具与人工判断，构建一个可以发布调用的智能体。",
    model: "local/qwen3.5-0.8b",
    nodes: [
      { id: "start", type: "start", label: "接收任务", next: "agent", position: { x: 90, y: 190 } },
      {
        id: "agent",
        type: "agent",
        label: "思考与执行",
        model: "local/qwen3.5-0.8b",
        system_prompt: "你是一个能够使用工具完成任务的助手。遇到计算时调用计算器，回答应简洁并说明依据。",
        tools: ["calculator", "search_files", "current_time"],
        max_iterations: 5,
        max_tokens: 512,
        temperature: 0.2,
        retries: 1,
        timeout: 600,
        next: "approval",
        position: { x: 430, y: 190 },
      },
      {
        id: "approval",
        type: "approval",
        label: "人工检查",
        message: "确认 Agent 的结果是否可以交付。",
        on_approve: "end",
        on_reject: "agent",
        position: { x: 770, y: 190 },
      },
      { id: "end", type: "end", label: "返回结果", position: { x: 1110, y: 190 } },
    ],
  };
}

/** 新节点默认配置（fanout 会额外返回配对 fanin） */
export function defaultNode(type: string, definition: WorkflowDef): { node: WorkflowNode; paired: WorkflowNode | null } {
  const id = `${type}_${Math.random().toString(36).slice(2, 7)}`;
  const idx = Math.max(0, definition.nodes.length - 2);
  const base: WorkflowNode = {
    id,
    type,
    label: NODE_META[type]?.title || type,
    position: { x: 430 + (idx % 3) * 310, y: 410 + Math.floor(idx / 3) * 190 },
  };
  const configs: Record<string, WorkflowNode> = {
    agent: { ...base, model: definition.model, system_prompt: "", tools: ["calculator"], max_iterations: 5, max_tokens: 512, temperature: 0.2, retries: 0, timeout: 600, next: null },
    llm: { ...base, model: definition.model, system_prompt: "", max_tokens: 512, temperature: 0.2, retries: 0, timeout: 600, next: null },
    tool: { ...base, tool: "calculator", arguments: { expression: "{{input}}" }, retries: 0, timeout: 30, next: null },
    condition: { ...base, variable: "last_output", operator: "contains", value: "", true_next: null, false_next: null },
    loop: { ...base, variable: "last_output", operator: "is_not_empty", value: "", max_iterations: 5, body_next: null, done_next: null },
    parallel: {
      ...base,
      branches: [
        { id: "branch_a", label: "分支 A", type: "transform", template: "{{input}}" },
        { id: "branch_b", label: "分支 B", type: "transform", template: "{{last_output}}" },
      ],
      max_concurrency: 2,
      next: null,
    },
    fanout: {
      ...base,
      branches: [
        { id: `${id}_a`, label: "分析分支", next: null },
        { id: `${id}_b`, label: "复核分支", next: null },
      ],
      max_concurrency: 2,
      fanin_id: `${id}_join`,
    },
    subworkflow: { ...base, workflow_id: null, workflow_name: "", workflow_version: null, workflow_snapshot: null, input_template: "{{last_output}}", next: null },
    approval: { ...base, message: "请检查本步骤的输出。", on_approve: null, on_reject: null },
    transform: { ...base, template: "{{last_output}}", retries: 0, timeout: 30, next: null },
  };
  const node = configs[type] ?? base;
  const pos = base.position as { x: number; y: number };
  const paired =
    type === "fanout"
      ? ({
          id: `${id}_join`,
          type: "fanin",
          label: "汇合分支结果",
          fanout_id: id,
          failure_policy: "fail",
          next: null,
          position: { x: pos.x + 360, y: pos.y + 70 },
        } as WorkflowNode)
      : null;
  return { node, paired };
}

/** 删除节点集合（含 fanout/fanin 配对），并清空指向它们的路由 */
export function removeNodes(definition: WorkflowDef, ids: Set<string>): WorkflowDef {
  const all = new Set(ids);
  definition.nodes.forEach((n) => {
    if (!all.has(n.id)) return;
    if (n.type === "fanout") all.add(n.fanin_id as string);
    if (n.type === "fanin") all.add(n.fanout_id as string);
  });
  return {
    ...definition,
    nodes: definition.nodes
      .filter((n) => !all.has(n.id))
      .map((n) => {
        let updated = n;
        for (const [key] of routeKeys(n)) {
          const t = routeTarget(updated, key);
          if (t && all.has(t)) updated = withRouteTarget(updated, key, null);
        }
        return updated;
      }),
  };
}

/** 连线合法性校验：返回错误消息或 null */
export function validateConnection(
  definition: WorkflowDef,
  sourceId: string,
  sourceHandle: string,
  targetId: string,
): string | null {
  const source = definition.nodes.find((n) => n.id === sourceId);
  const target = definition.nodes.find((n) => n.id === targetId);
  if (!source || !target) return "节点不存在";
  if (sourceId === targetId) return "不能连接自身";
  if (target.type === "start") return "任务输入节点不接受连线";
  if (source.type === "end") return "任务输出节点不能再连出";
  if (!routeKeys(source).some(([k]) => k === sourceHandle)) return "无效的路由口";
  // 成环检测：从 target 出发能回到 source 则不允许
  const seen = new Set<string>();
  const stack = [targetId];
  while (stack.length) {
    const id = stack.pop()!;
    if (id === sourceId) return "会形成环路（循环节点请使用循环体出口）";
    if (seen.has(id)) continue;
    seen.add(id);
    const n = definition.nodes.find((x) => x.id === id);
    if (!n) continue;
    for (const [key] of routeKeys(n)) {
      const t = routeTarget(n, key);
      if (t) stack.push(t);
    }
  }
  return null;
}

/** 由运行事件推导节点状态 */
export function nodeRunState(
  events: { node_id: string | null; type: string }[] | undefined,
  nodeId: string,
): string {
  if (!events) return "";
  let result = "";
  for (const ev of events) {
    if (ev.node_id !== nodeId) continue;
    if (ev.type === "node_started") result = "running";
    if (ev.type === "node_completed") result = "completed";
    if (ev.type === "node_attempt_failed" || ev.type === "tool_failed") result = "failed";
    if (ev.type === "approval_requested") result = "waiting";
  }
  return result;
}

export const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  waiting_approval: "等待审核",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  cancel_requested: "正在取消",
};

export const RUN_ACTIVE = ["queued", "running", "cancel_requested"];
