/* 全局状态条：模型 · 显存 · 活跃运行 · API 状态 */

import { useEffect } from "react";
import { useLocation } from "wouter";
import { useRuntime } from "../stores/runtime";
import { useRuns } from "../stores/workflows";
import { SpinningGear } from "./Gear";

export function Statusbar() {
  const { runtime, system, fetchSystem, fetchRuntime } = useRuntime();
  const { runs, fetchRuns } = useRuns();
  const [, navigate] = useLocation();

  useEffect(() => {
    fetchRuntime().catch(() => undefined);
    fetchSystem().catch(() => undefined);
    fetchRuns().catch(() => undefined);
    const timer = setInterval(() => {
      fetchSystem().catch(() => undefined);
      fetchRuns().catch(() => undefined);
    }, 5000);
    return () => clearInterval(timer);
  }, [fetchRuntime, fetchSystem, fetchRuns]);

  const gpu = system?.gpu;
  const vramUsed = gpu?.memory_used_gb ?? null;
  const vramTotal = gpu?.memory_total_gb ?? null;
  const vramRatio =
    vramUsed != null && vramTotal ? Math.min(1, vramUsed / vramTotal) : null;
  const vramClass =
    vramRatio == null ? "" : vramRatio > 0.9 ? " vram-bar--danger" : vramRatio > 0.75 ? " vram-bar--warn" : "";

  const activeRuns = runs.filter((r) =>
    ["queued", "running", "waiting_approval"].includes(r.status),
  ).length;

  const modelLabel = runtime?.model_id ?? "未加载";
  const apiOk = runtime != null;

  return (
    <footer className="statusbar">
      <span className="statusbar__item">
        {runtime?.loaded ? <SpinningGear size={12} /> : <span className="mono">◉</span>}
        <span className="mono">{modelLabel}</span>
      </span>
      <span className="statusbar__sep" />
      <span className="statusbar__item" data-tip="GPU 显存">
        <span>显存</span>
        <span className={`vram-bar${vramClass}`}>
          <span style={{ width: vramRatio != null ? `${vramRatio * 100}%` : "0%" }} />
        </span>
        <span className="mono">
          {vramUsed != null && vramTotal != null
            ? `${vramUsed.toFixed(1)}/${vramTotal.toFixed(0)}G`
            : "--"}
        </span>
      </span>
      <span className="statusbar__sep" />
      <span className="statusbar__item">
        <button onClick={() => navigate("/monitor")} data-tip="查看监控">
          <span>运行</span>
          <span className="mono">{activeRuns}</span>
          {activeRuns > 0 && <SpinningGear size={11} />}
        </button>
      </span>
      <span className="statusbar__sep" />
      <span className="statusbar__item">
        <span style={{ color: apiOk ? "var(--success)" : "var(--danger)" }}>●</span>
        <span>API {apiOk ? "在线" : "离线"}</span>
      </span>
      <span style={{ flex: 1 }} />
      <span className="statusbar__item mono" style={{ color: "var(--text-muted)" }}>
        LLMmodol · Clockwork Bench
      </span>
    </footer>
  );
}
