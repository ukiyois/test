/* 运行详情：三栏 — 运行档案+节点瀑布甘特 / 可交互流程图+轨迹 / 事件过滤 */

import { useEffect, useMemo, useState } from "react";
import { useLocation, useParams } from "wouter";
import { api, streamRun } from "@/api/client";
import type { Run, RunEvent } from "@/api/types";
import { toast } from "@/stores/ui";
import {
  NODE_META,
  RUN_ACTIVE,
  RUN_STATUS_LABELS,
  graphPositions,
  nodeRunState,
  routeKeys,
  routeTarget,
} from "@/workflow/graph";
import { TraceEventRow } from "@/workflow/TraceEvent";

/* 节点耗时片段（从事件对归纳 start/end） */
interface NodeSpan {
  nodeId: string;
  label: string;
  start: number; // epoch ms
  end: number | null;
  failed: boolean;
  type: string;
}

function buildSpans(run: Run): NodeSpan[] {
  const evs = [...(run.events || [])].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  );
  const spans: NodeSpan[] = [];
  const open = new Map<string, NodeSpan>();
  const defNodes = run.workflow_snapshot?.nodes || [];
  const labelOf = (id: string) => defNodes.find((n) => n.id === id)?.label || id;
  const typeOf = (id: string) => defNodes.find((n) => n.id === id)?.type || "";
  for (const e of evs) {
    const t = new Date(e.created_at).getTime();
    if (e.type === "node_started" && e.node_id) {
      const span: NodeSpan = {
        nodeId: e.node_id,
        label: labelOf(e.node_id),
        start: t,
        end: null,
        failed: false,
        type: typeOf(e.node_id),
      };
      spans.push(span);
      open.set(e.node_id, span);
    } else if ((e.type === "node_completed" || e.type === "node_attempt_failed" || e.type === "node_error_routed") && e.node_id) {
      const span = open.get(e.node_id);
      if (span) {
        span.end = t;
        if (e.type !== "node_completed") span.failed = true;
        open.delete(e.node_id);
      }
    }
  }
  // 仍未闭合（运行中）的节点结束于 now
  return spans;
}

/* 瀑布甘特图 */
function Waterfall({ run, selected, onSelect }: { run: Run; selected: string | null; onSelect: (id: string | null) => void }) {
  const spans = useMemo(() => buildSpans(run), [run]);
  if (spans.length === 0) return <p className="field-help">暂无节点时序数据。</p>;
  const t0 = Math.min(...spans.map((s) => s.start));
  const t1 = Math.max(...spans.map((s) => s.end ?? Date.now()), run.finished_at ? new Date(run.finished_at).getTime() : 0);
  const total = Math.max(1, t1 - t0);
  return (
    <div className="gantt">
      {spans.map((s, i) => {
        const left = ((s.start - t0) / total) * 100;
        const width = Math.max(1.5, (((s.end ?? Date.now()) - s.start) / total) * 100);
        const dur = ((s.end ?? Date.now()) - s.start) / 1000;
        return (
          <button
            key={`${s.nodeId}-${i}`}
            className={`gantt__row${selected === s.nodeId ? " is-selected" : ""}`}
            onClick={() => onSelect(selected === s.nodeId ? null : s.nodeId)}
            data-tip={`${s.label} · ${dur.toFixed(2)}s${s.failed ? " · 失败" : ""}`}
          >
            <span className="gantt__label">{s.label}</span>
            <span className="gantt__track">
              <span
                className={`gantt__bar${s.failed ? " is-failed" : s.end == null ? " is-running" : ""}`}
                style={{ left: `${left}%`, width: `${width}%` }}
              />
            </span>
            <span className="gantt__val digit">{dur < 1 ? `${(dur * 1000).toFixed(0)}ms` : `${dur.toFixed(1)}s`}</span>
          </button>
        );
      })}
    </div>
  );
}

/* 迷你流程图：静态 SVG 叠加运行状态，可点击过滤 */
function MiniFlow({ run, selected, onSelect }: { run: Run; selected: string | null; onSelect: (id: string | null) => void }) {
  const def = run.workflow_snapshot;
  if (!def?.nodes?.length) return null;
  const pos = graphPositions(def);
  const xs = def.nodes.map((n) => pos[n.id].x);
  const ys = def.nodes.map((n) => pos[n.id].y);
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  const maxX = Math.max(...xs) + 250;
  const maxY = Math.max(...ys) + 130;
  return (
    <svg className="run-detail__flow" viewBox={`${minX - 20} ${minY - 20} ${maxX - minX + 40} ${maxY - minY + 40}`}>
      {def.nodes.flatMap((n) =>
        routeKeys(n).flatMap(([key]) => {
          const t = routeTarget(n, key);
          if (!t || !pos[t]) return [];
          const a = pos[n.id];
          const b = pos[t];
          return [
            <path
              key={`${n.id}:${key}:${t}`}
              d={`M ${a.x + 250} ${a.y + 65} C ${a.x + 300} ${a.y + 65}, ${b.x - 50} ${b.y + 65}, ${b.x} ${b.y + 65}`}
              fill="none"
              stroke={key === "error_next" ? "var(--danger)" : "var(--line-strong)"}
              strokeWidth="1.5"
            />,
          ];
        }),
      )}
      {def.nodes.map((n) => {
        const p = pos[n.id];
        const state = nodeRunState(run.events, n.id);
        const color = `var(--node-${NODE_META[n.type]?.color || "tool"})`;
        const stateColor =
          state === "completed" ? "var(--success)" :
          state === "failed" ? "var(--danger)" :
          state === "running" ? "var(--info)" :
          state === "waiting" ? "var(--warning)" : "var(--line-subtle)";
        const isSel = selected === n.id;
        return (
          <g
            key={n.id}
            style={{ cursor: "pointer" }}
            onClick={() => onSelect(isSel ? null : n.id)}
            opacity={selected && !isSel ? 0.45 : 1}
          >
            <rect x={p.x} y={p.y} width="250" height="130" rx="10" fill="var(--bg-surface)" stroke={isSel ? "var(--brass)" : stateColor} strokeWidth={isSel ? 3 : state ? 2 : 1} />
            <rect x={p.x} y={p.y} width="4" height="130" fill={color} rx="2" />
            <text x={p.x + 16} y={p.y + 30} fill="var(--text-muted)" fontSize="10">
              {NODE_META[n.type]?.title || n.type}
            </text>
            <text x={p.x + 16} y={p.y + 52} fill="var(--text-primary)" fontSize="13" fontWeight="600">
              {n.label || n.id}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export function RunDetailView() {
  const params = useParams<{ id: string }>();
  const [, navigate] = useLocation();
  const [run, setRun] = useState<Run | null>(null);
  const [note, setNote] = useState("");
  const [selNode, setSelNode] = useState<string | null>(null);

  useEffect(() => {
    if (!params.id) return;
    api.run(params.id).then(setRun).catch((e) => toast.error(String(e)));
  }, [params.id]);

  /* SSE：活跃状态订阅推送 */
  useEffect(() => {
    if (!run || (!RUN_ACTIVE.includes(run.status) && run.status !== "waiting_approval")) return;
    const handle = streamRun(run.id, {
      onSnapshot: (snap) =>
        setRun((r) => (r ? { ...r, status: snap.status as Run["status"], current_node: snap.current_node, pending: snap.pending, error: snap.error, events: snap.events as RunEvent[] } : r)),
      onEvent: (ev) =>
        setRun((r) =>
          r && !r.events?.some((e) => e.id === (ev as RunEvent).id)
            ? { ...r, events: [...(r.events || []), { id: (ev as RunEvent).id ?? Date.now(), ...ev } as RunEvent] }
            : r,
        ),
      onStatus: (st) =>
        setRun((r) => (r ? { ...r, status: st.status as Run["status"], current_node: st.current_node, pending: st.pending, error: st.error } : r)),
      onClose: () => {
        if (run) api.run(run.id).then(setRun).catch(() => undefined);
      },
    });
    return () => handle.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, run?.status]);

  const events = useMemo(() => {
    const evs = run?.events || [];
    return selNode ? evs.filter((e) => e.node_id === selNode) : evs;
  }, [run, selNode]);

  if (!run) {
    return (
      <div className="run-detail">
        <p className="field-help" style={{ padding: 48 }}>正在载入运行详情…</p>
      </div>
    );
  }

  const approve = async (approved: boolean) => {
    try {
      await api.resumeRun(run.id, approved, note);
      setNote("");
      setRun((r) => (r ? { ...r, status: "queued" } : r));
      toast.success(approved ? "已批准" : "已退回");
    } catch (e) {
      toast.error(String(e));
    }
  };

  const cancel = async () => {
    try {
      await api.cancelRun(run.id);
      setRun((r) => (r ? { ...r, status: "cancel_requested" } : r));
    } catch (e) {
      toast.error(String(e));
    }
  };

  return (
    <div className="run-detail">
      <header className="page-head">
        <div>
          <h1>
            运行 <code className="run-detail__id">{run.id}</code>
          </h1>
          <p>
            工作流 {run.workflow_id} · 快照 v{run.workflow_version ?? "?"} ·{" "}
            {new Date(run.created_at).toLocaleString("zh-CN")}
          </p>
        </div>
        <div className="run-detail__actions">
          <span className={`badge ${run.status === "completed" ? "badge--success" : run.status === "failed" ? "badge--danger" : run.status === "waiting_approval" ? "badge--warning" : "badge--info"}`}>
            {RUN_STATUS_LABELS[run.status] || run.status}
          </span>
          {RUN_ACTIVE.includes(run.status) && (
            <button className="btn btn--danger btn--sm" onClick={cancel}>
              取消运行
            </button>
          )}
          <button className="btn btn--ghost btn--sm" onClick={() => navigate("/runs")}>
            ← 返回列表
          </button>
        </div>
      </header>

      {run.error && <div className="check-msg is-error">{run.error}</div>}

      {/* 审核道闸 */}
      {run.status === "waiting_approval" && (
        <div className="approval-gate">
          <div className="approval-gate__stripe" />
          <div className="approval-gate__body">
            <b>{String(run.pending?.message || "需要人工审核")}</b>
            {run.pending?.output != null && (
              <pre className="trace-row__detail">
                {typeof run.pending.output === "string"
                  ? run.pending.output
                  : JSON.stringify(run.pending.output, null, 2)}
              </pre>
            )}
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="审核意见（退回时写明修改意见）"
              rows={2}
            />
            <div className="wfe-run__actions">
              <button className="btn btn--secondary btn--sm" onClick={() => approve(false)}>
                退回
              </button>
              <button className="btn btn--primary btn--sm" onClick={() => approve(true)}>
                批准并继续
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 最终输出 */}
      {run.state?.result != null && (
        <div className="result-card">
          <span>最终输出</span>
          <pre>
            {typeof run.state.result === "string"
              ? run.state.result
              : JSON.stringify(run.state.result, null, 2)}
          </pre>
        </div>
      )}

      {/* 三栏主体 */}
      <div className="run-detail__grid">
        {/* 左栏：运行档案 + 节点瀑布甘特 */}
        <div className="run-detail__col">
          <section className="card run-detail__section">
            <h2>运行档案</h2>
            <dl className="run-ledger">
              <div><dt>运行 ID</dt><dd><code>{run.id}</code></dd></div>
              <div><dt>状态</dt><dd>{RUN_STATUS_LABELS[run.status] || run.status}</dd></div>
              <div><dt>当前节点</dt><dd>{run.current_node || "—"}</dd></div>
              <div><dt>开始</dt><dd>{run.started_at ? new Date(run.started_at).toLocaleTimeString("zh-CN") : "—"}</dd></div>
              <div><dt>结束</dt><dd>{run.finished_at ? new Date(run.finished_at).toLocaleTimeString("zh-CN") : "—"}</dd></div>
              <div><dt>事件数</dt><dd className="digit">{(run.events || []).length}</dd></div>
            </dl>
          </section>

          <section className="card run-detail__section">
            <h2>节点时序瀑布 {selNode && <button className="btn btn--ghost btn--sm" onClick={() => setSelNode(null)}>清除过滤</button>}</h2>
            <Waterfall run={run} selected={selNode} onSelect={setSelNode} />
          </section>

          {run.children && run.children.length > 0 && (
            <section className="card run-detail__section">
              <h2>子 Agent 运行</h2>
              {run.children.map((c) => (
                <div
                  key={c.id}
                  className="run-detail__child"
                  role="button"
                  tabIndex={0}
                  onClick={() => navigate(`/runs/${c.id}`)}
                  onKeyDown={(e) => e.key === "Enter" && navigate(`/runs/${c.id}`)}
                >
                  <span className={`badge ${c.status === "completed" ? "badge--success" : c.status === "failed" ? "badge--danger" : "badge--info"}`}>
                    {RUN_STATUS_LABELS[c.status] || c.status}
                  </span>
                  <code>{c.id}</code>
                  <span className="field-help">节点 {c.parent_node_id}</span>
                </div>
              ))}
            </section>
          )}
        </div>

        {/* 中栏：可交互流程图 */}
        <div className="run-detail__col run-detail__col--flow">
          <section className="card run-detail__section">
            <h2>流程状态 <small className="field-help">点击节点过滤轨迹</small></h2>
            <MiniFlow run={run} selected={selNode} onSelect={setSelNode} />
          </section>
        </div>

        {/* 右栏：轨迹 */}
        <div className="run-detail__col">
          <section className="run-detail__section">
            <h2>
              运行轨迹
              {selNode && <span className="badge badge--info">过滤: {selNode}</span>}
            </h2>
            <div className="trace-list">
              {events.map((ev) => (
                <TraceEventRow key={ev.id} event={ev} />
              ))}
              {events.length === 0 && <p className="field-help">{selNode ? "该节点暂无事件。" : "暂无事件。"}</p>}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
