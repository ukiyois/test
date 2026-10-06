/* 运行轨迹事件条目 + 审核卡 + 结果卡，编辑器运行台与运行详情页共用 */

import type { RunEvent } from "@/api/types";

const EVENT_LABELS: Record<string, string> = {
  run_queued: "任务已排队",
  run_started: "运行开始",
  run_resumed: "从 checkpoint 恢复",
  node_started: "节点开始",
  node_completed: "节点完成",
  node_attempt_failed: "步骤重试",
  node_error_routed: "已转入错误出口",
  model_call_completed: "模型调用完成",
  model_call_failed: "模型调用失败",
  tool_started: "工具开始",
  tool_completed: "工具完成",
  tool_failed: "工具失败",
  parallel_branch_started: "并行分支开始",
  parallel_branch_completed: "并行分支完成",
  parallel_branch_failed: "并行分支失败",
  fanout_started: "图级分流启动",
  fanout_completed: "所有图内路径汇合",
  fanout_branch_started: "工作路径启动",
  fanout_branch_completed: "工作路径完成",
  fanout_branch_failed: "工作路径失败",
  subworkflow_started: "子 Agent 开始",
  subworkflow_reused: "复用子 Agent 结果",
  subworkflow_completed: "子 Agent 完成",
  subworkflow_failed: "子 Agent 失败",
  subworkflow_cancelled: "子 Agent 已取消",
  subworkflow_approval_waiting: "子 Agent 等待审核",
  approval_requested: "等待人工审核",
  approval_resolved: "审核已处理",
  run_completed: "任务完成",
  run_failed: "任务失败",
  run_cancelled: "已取消",
  run_interrupted: "运行中断",
};

const FAILURE_TYPES = new Set([
  "node_attempt_failed",
  "node_error_routed",
  "model_call_failed",
  "tool_failed",
  "parallel_branch_failed",
  "fanout_branch_failed",
  "subworkflow_failed",
  "run_failed",
]);

export function TraceEventRow({
  event,
  onLocate,
}: {
  event: RunEvent;
  onLocate?: (nodeId: string) => void;
}) {
  const payload = event.payload || {};
  const detail =
    payload.error ??
    payload.output ??
    payload.result ??
    payload.message ??
    (payload.model
      ? `${payload.model} · 输入 ${payload.prompt_tokens ?? "?"} / 输出 ${payload.completion_tokens ?? "?"} tokens`
      : payload.tool
        ? `${payload.tool} · ${JSON.stringify(payload.arguments || {})}`
        : payload.branch_id
          ? `分支 ${payload.branch_id}${payload.label ? ` · ${payload.label}` : ""}`
          : "");
  const failed = FAILURE_TYPES.has(event.type);

  return (
    <article className={`trace-row${failed ? " is-failed" : ""}`}>
      <span className="trace-row__pin" />
      <div className="trace-row__body">
        <div className="trace-row__head">
          <b>{EVENT_LABELS[event.type] || event.type}</b>
          <code>{event.node_id || "RUN"}</code>
          {onLocate && event.node_id && (
            <button className="btn btn--ghost btn--sm" onClick={() => onLocate(event.node_id!)}>
              定位
            </button>
          )}
          <time>{new Date(event.created_at).toLocaleTimeString("zh-CN")}</time>
        </div>
        {detail ? (
          <pre className="trace-row__detail">
            {typeof detail === "string" ? detail : JSON.stringify(detail, null, 2)}
          </pre>
        ) : null}
        {payload.duration_ms != null && (
          <small className="trace-row__dur">耗时 {(Number(payload.duration_ms) / 1000).toFixed(2)} 秒</small>
        )}
      </div>
    </article>
  );
}
