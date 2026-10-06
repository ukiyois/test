/* 运行监控：机械仪表盘 — 转速表 / 资源液柱 / KPI 计数器 / 产能与瓶颈榜 / 告警挂牌 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import { toast } from "@/stores/ui";

interface Sample {
  sampled_at?: string;
  cpu_percent?: number;
  memory_percent?: number;
  memory_used_bytes?: number;
  memory_total_bytes?: number;
  gpu_percent?: number;
  vram_used_bytes?: number;
  vram_total_bytes?: number;
  gpu_temp_c?: number;
  prompt_tokens_per_second?: number | null;
  generation_tokens_per_second?: number | null;
  [key: string]: unknown;
}

interface AlertItem {
  id?: string;
  level: string;
  title: string;
  detail?: string;
  status?: string;
  acknowledged?: boolean;
  [key: string]: unknown;
}

interface LiveRun {
  run_id: string;
  workflow_name?: string;
  status: string;
  node_label?: string;
  step_count?: number;
  age_seconds?: number | null;
  no_progress_seconds?: number | null;
  [key: string]: unknown;
}

interface SlowNode {
  workflow_name?: string;
  node_id: string;
  samples: number;
  average_ms: number;
  p95_ms: number;
}
interface ModelUsage {
  model: string;
  calls: number;
  failures?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  average_latency_ms?: number | null;
  cost_usd?: number;
  cost_priced?: boolean;
}
interface WorkflowHealth {
  workflow_id: string;
  name: string;
  runs: number;
  completed: number;
  failed: number;
  success_rate_percent?: number | null;
  median_elapsed_ms?: number | null;
}
interface RecentFailure {
  run_id: string;
  workflow_name?: string;
  created_at: string;
  error?: string;
}

interface RunsSummary {
  statuses?: Record<string, number>;
  live_runs?: LiveRun[];
  stalled_runs?: LiveRun[];
  total?: number;
  active?: number;
  last_24h?: number;
  success_rate_percent?: number | null;
  median_elapsed_ms?: number | null;
  p95_elapsed_ms?: number | null;
  retries_last_24h?: number;
  tool_failures_last_24h?: number;
  approvals_requested_last_24h?: number;
  resumes_last_24h?: number;
  model_calls_last_24h?: number;
  model_call_failures_last_24h?: number;
  prompt_tokens_last_24h?: number;
  completion_tokens_last_24h?: number;
  oldest_queued_seconds?: number | null;
  longest_running_seconds?: number | null;
  checkpoint_storage_bytes?: number;
  slow_nodes?: SlowNode[];
  models_usage_last_24h?: ModelUsage[];
  cost_usd_last_24h?: number;
  local_tokens_last_24h?: number;
  remote_tokens_last_24h?: number;
  budgets?: { daily_usd?: number | null; monthly_usd?: number | null };
  by_workflow?: WorkflowHealth[];
  recent_failures?: RecentFailure[];
  [key: string]: unknown;
}

interface Snapshot {
  generated_at?: string;
  latest?: Sample | null;
  history?: Sample[];
  runs?: RunsSummary;
  alerts?: AlertItem[];
  thresholds?: Record<string, number>;
  unacknowledged_alerts?: number;
  [key: string]: unknown;
}

const LEVEL_BADGE: Record<string, string> = {
  ok: "badge--success",
  info: "badge--info",
  warning: "badge--warning",
  critical: "badge--danger",
  danger: "badge--danger",
};

const fmtBytes = (n?: number) =>
  n == null ? "—" : n >= 1 << 30 ? `${(n / (1 << 30)).toFixed(1)} GB` : `${(n / (1 << 20)).toFixed(0)} MB`;
const fmtMs = (ms?: number | null) =>
  ms == null ? "—" : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
const fmtNum = (n?: number | null) => (n == null ? "—" : n.toLocaleString("zh-CN"));

/* 转速表：圆形指针仪表，用于 tok/s 等速率 */
function Tachometer({ value, max, label, unit }: { value: number; max: number; label: string; unit: string }) {
  const ratio = Math.max(0, Math.min(1, value / max));
  const angle = -120 + ratio * 240; // -120° ~ +120°
  const r = 44;
  const cx = 60;
  const cy = 58;
  const rad = (a: number) => (a * Math.PI) / 180;
  const nx = cx + r * 0.72 * Math.cos(rad(angle - 90));
  const ny = cy + r * 0.72 * Math.sin(rad(angle - 90));
  // 刻度弧
  const arc = (a0: number, a1: number, rr: number) => {
    const x0 = cx + rr * Math.cos(rad(a0 - 90));
    const y0 = cy + rr * Math.sin(rad(a0 - 90));
    const x1 = cx + rr * Math.cos(rad(a1 - 90));
    const y1 = cy + rr * Math.sin(rad(a1 - 90));
    return `M ${x0} ${y0} A ${rr} ${rr} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1} ${y1}`;
  };
  const ticks = Array.from({ length: 9 }, (_, i) => -120 + i * 30);
  const hot = ratio > 0.8;
  return (
    <div className="tacho" data-tip={`${label}: ${value.toFixed(1)} ${unit}`}>
      <svg viewBox="0 0 120 78" className="tacho__svg">
        <path d={arc(-120, 120, r)} fill="none" stroke="var(--line)" strokeWidth="5" strokeLinecap="round" />
        <path
          d={arc(-120, angle, r)}
          fill="none"
          stroke={hot ? "var(--danger)" : "var(--copper)"}
          strokeWidth="5"
          strokeLinecap="round"
        />
        {ticks.map((t) => {
          const x0 = cx + (r - 9) * Math.cos(rad(t - 90));
          const y0 = cy + (r - 9) * Math.sin(rad(t - 90));
          const x1 = cx + (r - 4) * Math.cos(rad(t - 90));
          const y1 = cy + (r - 4) * Math.sin(rad(t - 90));
          return <line key={t} x1={x0} y1={y0} x2={x1} y2={y1} stroke="var(--brass-dim)" strokeWidth="1" />;
        })}
        <line x1={cx} y1={cy} x2={nx} y2={ny} stroke={hot ? "var(--danger)" : "var(--brass)"} strokeWidth="2.5" strokeLinecap="round" />
        <circle cx={cx} cy={cy} r="4.5" fill="var(--brass)" stroke="#000" strokeWidth="1" />
      </svg>
      <div className="tacho__read">
        <b className="digit">{value.toFixed(1)}</b>
        <small>{unit}</small>
      </div>
      <div className="tacho__label">{label}</div>
    </div>
  );
}

/* 液柱/气压表：竖直填充，用于 CPU/内存/显存 */
function ColumnGauge({ pct, label, sub, tone }: { pct: number; label: string; sub?: string; tone: string }) {
  const p = Math.max(0, Math.min(100, pct));
  const hot = p > 85;
  return (
    <div className="cgauge" data-tip={`${label}: ${p.toFixed(0)}%`}>
      <div className="cgauge__tube">
        <div
          className="cgauge__fill"
          style={{ height: `${p}%`, background: hot ? "var(--danger)" : tone }}
        />
        <div className="cgauge__grid" />
      </div>
      <div className="cgauge__read">
        <b className="digit">{p.toFixed(0)}</b>
        <small>%</small>
      </div>
      <div className="cgauge__label">{label}</div>
      {sub && <div className="cgauge__sub">{sub}</div>}
    </div>
  );
}

/* KPI 机械计数器（数码管凹槽） */
function Kpi({ label, value, unit, tone }: { label: string; value: string; unit?: string; tone?: string }) {
  return (
    <div className="kpi">
      <div className="kpi__label">{label}</div>
      <div className="kpi__well">
        <b className="digit" style={tone ? { color: tone } : undefined}>{value}</b>
        {unit && <small>{unit}</small>}
      </div>
    </div>
  );
}

export function MonitorView() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [editingThresholds, setEditingThresholds] = useState(false);
  const [thresholdForm, setThresholdForm] = useState<Record<string, string>>({});

  const refresh = useCallback(() => {
    api.monitoring().then((r) => setSnap(r as Snapshot)).catch(() => undefined);
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [refresh]);

  const ack = async (id?: string) => {
    if (!id) return;
    try {
      await api.ackAlert(id);
      refresh();
    } catch (e) {
      toast.error(String(e));
    }
  };

  const saveThresholds = async (reset = false) => {
    try {
      const values: Record<string, number> = {};
      if (!reset) {
        for (const [k, v] of Object.entries(thresholdForm)) {
          const n = Number(v);
          if (v.trim() !== "" && Number.isFinite(n)) values[k] = n;
        }
      }
      await api.saveThresholds(values, reset);
      setEditingThresholds(false);
      refresh();
      toast.success(reset ? "阈值已恢复默认" : "阈值已保存");
    } catch (e) {
      toast.error(String(e));
    }
  };

  const latest = snap?.latest;
  const runs = snap?.runs;
  const alerts = (snap?.alerts || []).filter((a) => a.level !== "ok");
  const hasGpu = Boolean(latest?.vram_total_bytes || latest?.gpu_percent != null);
  const memPct = latest?.memory_percent ?? (latest?.memory_used_bytes && latest?.memory_total_bytes ? (latest.memory_used_bytes / latest.memory_total_bytes) * 100 : 0);
  const vramPct = latest?.vram_used_bytes && latest?.vram_total_bytes ? (latest.vram_used_bytes / latest.vram_total_bytes) * 100 : 0;
  const genTps = latest?.generation_tokens_per_second ?? 0;
  const promptTps = latest?.prompt_tokens_per_second ?? 0;
  const maxTok = Math.max(50, genTps, promptTps) * 1.25;
  const slowNodes = runs?.slow_nodes || [];
  const modelUsage = runs?.models_usage_last_24h || [];
  const workflows = runs?.by_workflow || [];
  const failures = runs?.recent_failures || [];
  const maxSlow = Math.max(...slowNodes.map((s) => s.p95_ms), 1);
  const maxModelTok = Math.max(...modelUsage.map((m) => m.total_tokens ?? 0), 1);

  return (
    <div className="monitor">
      <header className="page-head">
        <div>
          <h1>运行监控</h1>
          <p>
            机械仪表盘 · 系统资源、推理速率、运行队列与告警
            {snap?.generated_at && ` · 采样于 ${new Date(snap.generated_at).toLocaleTimeString("zh-CN")}`}
          </p>
        </div>
        {snap?.unacknowledged_alerts ? (
          <span className="badge badge--danger">{snap.unacknowledged_alerts} 条未确认告警</span>
        ) : null}
      </header>

      {/* KPI 计数器排 */}
      <div className="monitor__kpis">
        <Kpi label="24h 运行" value={fmtNum(runs?.last_24h)} />
        <Kpi label="成功率" value={runs?.success_rate_percent != null ? runs.success_rate_percent.toFixed(0) : "—"} unit="%" tone={runs?.success_rate_percent != null && runs.success_rate_percent < 80 ? "var(--danger)" : "var(--copper)"} />
        <Kpi label="中位耗时" value={fmtMs(runs?.median_elapsed_ms)} />
        <Kpi label="Token 总量" value={fmtNum((runs?.prompt_tokens_last_24h ?? 0) + (runs?.completion_tokens_last_24h ?? 0))} />
        <Kpi label="远程成本" value={runs?.cost_usd_last_24h != null ? runs.cost_usd_last_24h.toFixed(4) : "0.0000"} unit="$" tone="var(--brass)" />
        <Kpi label="模型调用" value={fmtNum(runs?.model_calls_last_24h)} />
        <Kpi label="重试" value={fmtNum(runs?.retries_last_24h)} tone={runs?.retries_last_24h ? "var(--warning)" : undefined} />
        <Kpi label="Checkpoint" value={fmtBytes(runs?.checkpoint_storage_bytes)} />
      </div>

      {/* 转速表 + 资源液柱 */}
      <div className="monitor__gauges">
        <section className="card gauge-panel">
          <h2>推理速率</h2>
          <div className="gauge-panel__row">
            <Tachometer value={genTps} max={maxTok} label="生成" unit="tok/s" />
            <Tachometer value={promptTps} max={maxTok} label="预填充" unit="tok/s" />
          </div>
          {genTps === 0 && promptTps === 0 && <p className="field-help">GGUF 引擎未运行或无实时速率采样。</p>}
        </section>

        <section className="card gauge-panel">
          <h2>系统资源</h2>
          <div className="gauge-panel__row">
            <ColumnGauge pct={latest?.cpu_percent ?? 0} label="CPU" tone="var(--info)" />
            <ColumnGauge pct={memPct} label="内存" sub={latest?.memory_used_bytes != null ? fmtBytes(latest.memory_used_bytes) : undefined} tone="var(--accent)" />
            {hasGpu && (
              <>
                <ColumnGauge pct={latest?.gpu_percent ?? 0} label="GPU" tone="var(--violet)" />
                <ColumnGauge pct={vramPct} label="显存" sub={latest?.vram_used_bytes != null ? fmtBytes(latest.vram_used_bytes) : undefined} tone="var(--brass)" />
                {latest?.gpu_temp_c != null && (
                  <ColumnGauge pct={Math.min(100, (latest.gpu_temp_c / 100) * 100)} label="温度" sub={`${latest.gpu_temp_c.toFixed(0)}°C`} tone="var(--warning)" />
                )}
              </>
            )}
          </div>
        </section>
      </div>

      <div className="monitor__cols">
        {/* 活跃运行 + 队列健康 */}
        <section className="card monitor__section">
          <h2>活跃运行 {runs?.live_runs ? `（${runs.live_runs.length}）` : ""}</h2>
          {(runs?.live_runs || []).length === 0 ? (
            <p className="field-help">当前没有活跃的运行。</p>
          ) : (
            runs!.live_runs!.map((r) => {
              const stalled = (r.no_progress_seconds ?? 0) > 60 || (r.age_seconds ?? 0) > 300;
              return (
                <div key={r.run_id} className="monitor-run">
                  <span className={`lamp ${stalled ? "lamp--danger lamp--blink" : r.status === "waiting_approval" ? "lamp--warning" : "lamp--accent"}`} />
                  <div className="monitor-run__info">
                    <b>{r.workflow_name || r.run_id}</b>
                    <small>
                      {r.node_label || "等待调度"} · 第 {r.step_count ?? 0} 步
                      {r.age_seconds != null && ` · ${Math.round(r.age_seconds)}s`}
                    </small>
                  </div>
                  <span className={`badge ${r.status === "waiting_approval" ? "badge--warning" : "badge--info"}`}>{r.status}</span>
                </div>
              );
            })
          )}
          <div className="monitor-queue">
            {runs?.oldest_queued_seconds != null && <span className="badge badge--muted">最长排队 {Math.round(runs.oldest_queued_seconds)}s</span>}
            {runs?.longest_running_seconds != null && <span className="badge badge--muted">最长运行 {Math.round(runs.longest_running_seconds)}s</span>}
            {(runs?.stalled_runs?.length ?? 0) > 0 && <span className="badge badge--warning">{runs!.stalled_runs!.length} 停滞</span>}
            {(runs?.approvals_requested_last_24h ?? 0) > 0 && <span className="badge badge--warning">{runs!.approvals_requested_last_24h} 待审批</span>}
          </div>
          {runs?.statuses && (
            <>
              <div className="inspector-sep">24 小时状态分布</div>
              <div className="monitor-statuses">
                {Object.entries(runs.statuses).map(([k, v]) => (
                  <span key={k} className="badge badge--muted">{k} ×{v}</span>
                ))}
              </div>
            </>
          )}
        </section>

        {/* 告警挂牌 */}
        <section className="card monitor__section">
          <div className="models__section-head">
            <h2>告警挂牌</h2>
            <button className="btn btn--ghost btn--sm" onClick={() => {
              setThresholdForm(Object.fromEntries(Object.entries(snap?.thresholds || {}).map(([k, v]) => [k, String(v)])));
              setEditingThresholds(true);
            }}>
              阈值设置
            </button>
          </div>
          {alerts.length === 0 ? (
            <p className="check-ok">一切正常，机械运转平稳。</p>
          ) : (
            alerts.map((a, i) => (
              <div key={a.id || i} className={`alert-row alert-row--${a.level}`}>
                <span className={`badge ${LEVEL_BADGE[a.level] || "badge--muted"}`}>{a.level}</span>
                <div className="alert-row__body">
                  <b>{a.title}</b>
                  {a.detail && <small>{a.detail}</small>}
                </div>
                {a.id && a.status === "active" && (
                  <button className="btn btn--ghost btn--sm" onClick={() => ack(a.id)}>确认</button>
                )}
              </div>
            ))
          )}
          {failures.length > 0 && (
            <>
              <div className="inspector-sep">最近失败</div>
              {failures.map((f) => (
                <div key={f.run_id} className="alert-row alert-row--critical">
                  <span className="lamp lamp--danger" />
                  <div className="alert-row__body">
                    <b>{f.workflow_name || f.run_id}</b>
                    {f.error && <small>{f.error}</small>}
                  </div>
                </div>
              ))}
            </>
          )}
        </section>
      </div>

      <div className="monitor__cols">
        {/* 瓶颈节点榜 */}
        <section className="card monitor__section">
          <h2>瓶颈节点榜 <small>按 p95 耗时</small></h2>
          {slowNodes.length === 0 ? (
            <p className="field-help">暂无节点耗时采样。</p>
          ) : (
            slowNodes.map((n) => (
              <div key={`${n.workflow_name}:${n.node_id}`} className="rank-row">
                <div className="rank-row__info">
                  <b>{n.node_id}</b>
                  <small>{n.workflow_name} · {n.samples} 样本</small>
                </div>
                <div className="rank-row__bar">
                  <span style={{ width: `${(n.p95_ms / maxSlow) * 100}%` }} />
                </div>
                <span className="rank-row__val digit">{fmtMs(n.p95_ms)}</span>
              </div>
            ))
          )}
        </section>

        {/* 模型产能榜 */}
        <section className="card monitor__section">
          <h2>模型产能榜 <small>24h token 产出 · 成本</small></h2>
          {modelUsage.length === 0 ? (
            <p className="field-help">24 小时内没有模型调用。</p>
          ) : (
            modelUsage.map((m) => (
              <div key={m.model} className="rank-row">
                <div className="rank-row__info">
                  <b>{m.model}</b>
                  <small>
                    {m.calls} 次{m.failures ? ` · ${m.failures} 失败` : ""} · 延迟 {fmtMs(m.average_latency_ms)}
                  </small>
                </div>
                <div className="rank-row__bar">
                  <span style={{ width: `${((m.total_tokens ?? 0) / maxModelTok) * 100}%` }} />
                </div>
                <span className="rank-row__val digit">{fmtNum(m.total_tokens)}</span>
                <span className="rank-row__cost digit">
                  {m.cost_priced ? `$${(m.cost_usd ?? 0).toFixed(4)}` : "—"}
                </span>
              </div>
            ))
          )}
          {(runs?.local_tokens_last_24h != null || runs?.remote_tokens_last_24h != null) && (
            <div className="cost-share">
              <div className="cost-share__bar">
                <span
                  className="cost-share__seg cost-share__seg--local"
                  style={{
                    width: `${(((runs?.local_tokens_last_24h ?? 0) / Math.max(1, (runs?.local_tokens_last_24h ?? 0) + (runs?.remote_tokens_last_24h ?? 0))) * 100).toFixed(1)}%`,
                  }}
                />
                <span
                  className="cost-share__seg cost-share__seg--remote"
                  style={{
                    width: `${(((runs?.remote_tokens_last_24h ?? 0) / Math.max(1, (runs?.local_tokens_last_24h ?? 0) + (runs?.remote_tokens_last_24h ?? 0))) * 100).toFixed(1)}%`,
                  }}
                />
              </div>
              <div className="cost-share__legend">
                <span><i className="cost-share__dot cost-share__dot--local" />本地 {fmtNum(runs?.local_tokens_last_24h ?? 0)} tok · $0</span>
                <span><i className="cost-share__dot cost-share__dot--remote" />远程 {fmtNum(runs?.remote_tokens_last_24h ?? 0)} tok · ${(runs?.cost_usd_last_24h ?? 0).toFixed(4)}</span>
                {runs?.budgets?.daily_usd != null && (
                  <span className="cost-share__budget">日预算 ${runs.budgets.daily_usd.toFixed(2)}</span>
                )}
                {runs?.budgets?.monthly_usd != null && (
                  <span className="cost-share__budget">月预算 ${runs.budgets.monthly_usd.toFixed(2)}</span>
                )}
              </div>
            </div>
          )}
        </section>
      </div>

      {/* 工作流健康榜 */}
      {workflows.length > 0 && (
        <section className="card monitor__section">
          <h2>工作流健康榜</h2>
          <div className="monitor-wf">
            {workflows.map((w) => (
              <div key={w.workflow_id} className="monitor-wf__row">
                <div className="monitor-wf__info">
                  <b>{w.name}</b>
                  <small>{w.runs} 次 · 完成 {w.completed} · 失败 {w.failed} · 中位 {fmtMs(w.median_elapsed_ms)}</small>
                </div>
                <div className="monitor-wf__rate">
                  <div className="monitor-wf__ratebar">
                    <span
                      style={{
                        width: `${w.success_rate_percent ?? 0}%`,
                        background: (w.success_rate_percent ?? 0) >= 80 ? "var(--copper)" : (w.success_rate_percent ?? 0) >= 50 ? "var(--warning)" : "var(--danger)",
                      }}
                    />
                  </div>
                  <b className="digit">{w.success_rate_percent != null ? `${w.success_rate_percent.toFixed(0)}%` : "—"}</b>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* 阈值编辑弹层 */}
      {editingThresholds && (
        <div className="modal-mask" onMouseDown={(e) => e.target === e.currentTarget && setEditingThresholds(false)}>
          <div className="modal" role="dialog" aria-modal="true">
            <h3>告警阈值</h3>
            <p className="field-help">留空的项保持不变。</p>
            <div className="threshold-grid">
              {Object.entries(thresholdForm).map(([k, v]) => (
                <label className="field" key={k}>
                  <span>{k}</span>
                  <input value={v} onChange={(e) => setThresholdForm({ ...thresholdForm, [k]: e.target.value })} inputMode="numeric" />
                </label>
              ))}
            </div>
            <div className="modal-actions">
              <button className="btn btn--danger btn--sm" onClick={() => saveThresholds(true)}>恢复默认</button>
              <button className="btn btn--ghost btn--sm" onClick={() => setEditingThresholds(false)}>取消</button>
              <button className="btn btn--primary btn--sm" onClick={() => saveThresholds(false)}>保存</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
