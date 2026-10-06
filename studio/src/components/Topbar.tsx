/* 顶栏：面包屑 + 模型快速切换 + 全局操作 */

import { useEffect } from "react";
import { useLocation } from "wouter";
import { useRuntime } from "../stores/runtime";
import { useChat } from "../stores/chat";
import { useUi } from "../stores/ui";

const TITLES: Array<[RegExp, string]> = [
  [/^\/$/, "对话"],
  [/^\/workflow\/[^/]+$/, "工作流编辑器"],
  [/^\/workflow/, "Agent 工作流"],
  [/^\/models/, "模型与 API"],
  [/^\/runs\/[^/]+$/, "运行详情"],
  [/^\/runs/, "运行记录"],
  [/^\/monitor/, "运行监控"],
];

export function Topbar() {
  const [location] = useLocation();
  const { models, runtime, fetchModels } = useRuntime();
  const { selectedModel, setModel } = useChat();
  const setCmdk = useUi((s) => s.setCmdk);

  useEffect(() => {
    fetchModels().catch(() => undefined);
  }, [fetchModels]);

  const title = TITLES.find(([re]) => re.test(location))?.[1] ?? "LLMmodol";
  const usable = models.filter((m) => m.available !== false);
  const loadedId = runtime?.model_id ? `local/${runtime.model_id}` : null;

  return (
    <header className="topbar">
      <div className="topbar__crumb">
        <strong>{title}</strong>
      </div>
      <div className="topbar__spacer" />
      <label className="model-switch" data-tip="快速切换模型">
        <span className={`dot${loadedId ? " dot--loaded" : ""}`} />
        <select
          value={selectedModel}
          onChange={(e) => setModel(e.target.value)}
          aria-label="当前模型"
        >
          {usable.length === 0 && <option value={selectedModel}>{selectedModel}</option>}
          {usable.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name ?? m.id}
              {loadedId === m.id ? " ●" : ""}
            </option>
          ))}
        </select>
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden>
          <path d="M2 3.5L5 6.5L8 3.5" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </label>
      <button className="btn btn--ghost btn--sm" onClick={() => setCmdk(true)}>
        <span className="kbd">Ctrl K</span>
      </button>
    </header>
  );
}
