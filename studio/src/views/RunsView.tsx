/* 运行记录：工序卡式列表 — 状态灯 / 步骤进度 / token 数码管 / 耗时 / 子运行树展开 */

import { useEffect, useMemo, useState } from "react";
import { useLocation } from "wouter";
import { useRuns, useWorkflows } from "@/stores/workflows";
import { RUN_STATUS_LABELS } from "@/workflow/graph";
import type { Run, RunEvent } from "@/api/types";
import { toast } from "@/stores/ui";

const STATUS_BADGE: Record<string, string> = {
  completed: "badge--success",
  failed: "badge--danger",
  cancelled: "badge--muted",
  interrupted: "badge--muted",
  waiting_approval: "badge--warning",
  running: "badge--info",
  queued: "badge--info",
  cancel_requested: "badge--warning",
};

const STATUS_LAMP: Record<string, string> = {
  completed: "lamp--accent",
  failed: "lamp--danger",
  waiting_approval: "lamp--warning",
  running: "lamp--accent lamp--blink",
  queued: "lamp--warning",
  cancel_requested: "lamp--danger lamp--blink",
};

function fmtDuration(start?: string | null, end?: string | null): string {
  if (!start) return "—";
  const s = new Date(start).getTime();
  const e = end ? new Date(end).getTime() : Date.now();
  const sec = Math.max(0, (e - s) / 1000);
  if (sec < 60) return `${sec.toFixed(0)}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`;
  return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`;
}

/* 从事件流归纳：步骤数、token、模型调用、失败节点 */
function summarize(events?: RunEvent[]) {
  const evs = events || [];
  const nodesDone = new Set<string>();
  const nodesStarted = new Set<string>();
  let failedNode: string | null = null;
  let promptTok = 0;
  let completionTok = 0;
  let modelCalls = 0;
  for (const e of evs) {
    if (e.type === "node_started" && e.node_id) nodesStarted.add(e.node_id);
    if (e.type === "node_completed" && e.node_id) nodesDone.add(e.node_id);
    if ((e.type === "node_attempt_failed" || e.type === "model_call_failed" || e.type === "tool_failed") && e.node_id && !failedNode) {
      failedNode = e.node_id;
    }
    if (e.type === "model_call_completed") {
      modelCalls += 1;
      promptTok += Number(e.payload?.prompt_tokens ?? 0) || 0;
      completionTok += Number(e.payload?.completion_tokens ?? 0) || 0;
    }
  }
  return {
    stepsDone: nodesDone.size,
    stepsStarted: nodesStarted.size,
    failedNode,
    totalTokens: promptTok + completionTok,
    promptTok,
    completionTok,
    modelCalls,
  };
}

const fmtNum = (n: number) => (n >= 10000 ? `${(n / 1000).toFixed(1)}k` : String(n));

/* 单张工序卡 */
function RunCard({
  run,
  name,
  children,
  expanded,
  onToggle,
  onOpen,
}: {
  run: Run;
  name: string;
  children: Run[];
  expanded: boolean;
  onToggle: () => void;
  onOpen: (id: string) => void;
}) {
  const s = summarize(run.events);
  const active = run.status === "running" || run.status === "waiting_approval";
  const inputPreview =
    typeof run.input?.input === "string" ? run.input.input : JSON.stringify(run.input ?? {});

  return (
    <div className={`runcard${run.status === "failed" ? " runcard--failed" : ""}`}>
      <div
        className="runcard__main"
        role="button"
        tabIndex={0}
        onClick={() => onOpen(run.id)}
        onKeyDown={(e) => e.key === "Enter" && onOpen(run.id)}
      >
        <span className={`lamp ${STATUS_LAMP[run.status] || ""}`} />
        <div className="runcard__id">
          <b>{name}</b>
          <code>{run.id}</code>
        </div>
        <div className="runcard__input" title={inputPreview}>
          {inputPreview}
        </div>
        {/* 步骤进度 */}
        <div className="runcard__steps" data-tip={`已完成 ${s.stepsDone} / 启动 ${s.stepsStarted} 节点`}>
          <div className="runcard__stepsbar">
            <span
              style={{
                width: `${s.stepsStarted > 0 ? (s.stepsDone / s.stepsStarted) * 100 : run.status === "completed" ? 100 : 0}%`,
                background: run.status === "failed" ? "var(--danger)" : "var(--copper)",
              }}
            />
          </div>
          <small className="digit">
            {run.status === "completed" ? "完成" : active ? `${s.stepsDone}步` : `${s.stepsDone}步`}
          </small>
        </div>
        {/* token 数码管 */}
        <div className="runcard__tokens" data-tip={`模型调用 ${s.modelCalls} 次 · 输入 ${s.promptTok} / 输出 ${s.completionTok}`}>
          <b className="digit">{s.totalTokens > 0 ? fmtNum(s.totalTokens) : "—"}</b>
          <small>tok</small>
        </div>
        <div className="runcard__dur">
          <b className="digit">{fmtDuration(run.started_at, run.finished_at)}</b>
        </div>
        <div className="runcard__meta">
          <span className={`badge ${STATUS_BADGE[run.status] || "badge--muted"}`}>
            {RUN_STATUS_LABELS[run.status] || run.status}
          </span>
          {s.failedNode && <span className="runcard__failnode">✕ {s.failedNode}</span>}
        </div>
        <time className="runcard__time">{new Date(run.created_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</time>
        {children.length > 0 && (
          <button
            className="runcard__expand"
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
            aria-label={expanded ? "收起子运行" : "展开子运行"}
          >
            {expanded ? "▾" : "▸"} {children.length}
          </button>
        )}
      </div>
      {expanded && children.length > 0 && (
        <div className="runcard__children">
          {children.map((c) => {
            const cs = summarize(c.events);
            return (
              <div
                key={c.id}
                className="runcard__child"
                role="button"
                tabIndex={0}
                onClick={() => onOpen(c.id)}
                onKeyDown={(e) => e.key === "Enter" && onOpen(c.id)}
              >
                <span className={`lamp ${STATUS_LAMP[c.status] || ""}`} />
                <code>{c.id}</code>
                <span className="runcard__childnode">节点 {c.parent_node_id}</span>
                <span className="digit">{cs.totalTokens > 0 ? `${fmtNum(cs.totalTokens)} tok` : "—"}</span>
                <span className="digit">{fmtDuration(c.started_at, c.finished_at)}</span>
                <span className={`badge ${STATUS_BADGE[c.status] || "badge--muted"}`}>
                  {RUN_STATUS_LABELS[c.status] || c.status}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function RunsView() {
  const [, navigate] = useLocation();
  const { runs, fetchRuns } = useRuns();
  const { workflows, fetchWorkflows } = useWorkflows();
  const [filter, setFilter] = useState("all");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  useEffect(() => {
    fetchRuns().catch((e) => toast.error(String(e)));
    if (workflows.length === 0) fetchWorkflows().catch(() => undefined);
    const timer = setInterval(() => fetchRuns().catch(() => undefined), 5000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const nameOf = useMemo(() => {
    const map: Record<string, string> = {};
    for (const w of workflows) map[w.id] = w.name;
    return map;
  }, [workflows]);

  const roots = useMemo(() => runs.filter((r) => !r.parent_run_id), [runs]);
  const childOf = useMemo(() => {
    const map: Record<string, Run[]> = {};
    for (const r of runs) {
      if (r.parent_run_id) (map[r.parent_run_id] ||= []).push(r);
    }
    return map;
  }, [runs]);
  const filtered = filter === "all" ? roots : roots.filter((r) => r.status === filter);

  return (
    <div className="runs">
      <header className="page-head">
        <div>
          <h1>运行记录</h1>
          <p>工序卡 · 每一次 Agent 执行的完整轨迹、审核与输出。</p>
        </div>
        <select className="runs__filter" value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="all">全部状态</option>
          <option value="running">运行中</option>
          <option value="waiting_approval">等待审核</option>
          <option value="completed">已完成</option>
          <option value="failed">失败</option>
          <option value="cancelled">已取消</option>
        </select>
      </header>

      {filtered.length === 0 ? (
        <div className="wf-list__empty">
          <p>{filter === "all" ? "还没有运行记录。在工作流编辑器里运行一次测试。" : "此状态下没有运行。"}</p>
        </div>
      ) : (
        <div className="runs__cards">
          {filtered.map((r) => (
            <RunCard
              key={r.id}
              run={r}
              name={nameOf[r.workflow_id] || r.workflow_id}
              children={childOf[r.id] || []}
              expanded={Boolean(expanded[r.id])}
              onToggle={() => setExpanded((x) => ({ ...x, [r.id]: !x[r.id] }))}
              onOpen={(id) => navigate(`/runs/${id}`)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
