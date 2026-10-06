/* 工作流列表页：卡片网格 + 新建 + 删除 + 发布状态 */

import { useEffect, useState } from "react";
import { useLocation } from "wouter";
import { api } from "@/api/client";
import { useWorkflows } from "@/stores/workflows";
import { toast } from "@/stores/ui";
import { newDefinition, NODE_META } from "@/workflow/graph";

export function WorkflowListView() {
  const [, navigate] = useLocation();
  const { workflows, fetchWorkflows } = useWorkflows();
  const [busy, setBusy] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  useEffect(() => {
    fetchWorkflows().catch((e) => toast.error(String(e)));
  }, [fetchWorkflows]);

  const create = async () => {
    setBusy(true);
    try {
      const workflow = await api.saveWorkflow(newDefinition());
      await fetchWorkflows();
      navigate(`/workflow/${workflow.id}`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    try {
      await api.deleteWorkflow(id);
      await fetchWorkflows();
      setPendingDelete(null);
      toast.success("已删除工作流");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="wf-list">
      <header className="wf-list__head">
        <div>
          <h1>Agent 工作流</h1>
          <p>编排模型、工具与人工判断，构建可发布调用的智能体。</p>
        </div>
        <button className="btn btn--primary" disabled={busy} onClick={create}>
          ＋ 新建工作流
        </button>
      </header>

      {workflows.length === 0 ? (
        <div className="wf-list__empty">
          <p>还没有工作流。点「新建工作流」从四节点模板开始。</p>
        </div>
      ) : (
        <div className="wf-list__grid">
          {workflows.map((w) => {
            const nodes = w.definition?.nodes || [];
            const typeCount = nodes.reduce<Record<string, number>>((acc, n) => {
              acc[n.type] = (acc[n.type] || 0) + 1;
              return acc;
            }, {});
            const dirtyDraft = w.published_version != null && w.version !== w.published_version;
            return (
              <div
                key={w.id}
                className="wf-card"
                role="button"
                tabIndex={0}
                onClick={() => navigate(`/workflow/${w.id}`)}
                onKeyDown={(e) => e.key === "Enter" && navigate(`/workflow/${w.id}`)}
              >
                <div className="wf-card__head">
                  <b>{w.name}</b>
                  <span className="wf-card__badges">
                    <span className="badge badge--muted">草稿 v{w.version}</span>
                    {w.published_version ? (
                      <span className="badge badge--accent">已发布 v{w.published_version}</span>
                    ) : null}
                    {dirtyDraft && <span className="badge badge--warning">有未发布改动</span>}
                  </span>
                </div>
                <p className="wf-card__desc">{w.description || "（未填写用途）"}</p>
                <div className="wf-card__meta">
                  <span>{nodes.length} 个步骤</span>
                  {Object.entries(typeCount)
                    .filter(([t]) => t !== "start" && t !== "end")
                    .slice(0, 5)
                    .map(([t, c]) => (
                      <span key={t} className="wf-card__chip" style={{ borderColor: `var(--node-${NODE_META[t]?.color || "tool"})` }}>
                        {NODE_META[t]?.title || t} ×{c}
                      </span>
                    ))}
                </div>
                <div className="wf-card__foot">
                  {w.updated_at && <small>更新于 {new Date(w.updated_at).toLocaleString("zh-CN")}</small>}
                  <button
                    className="btn btn--danger btn--sm"
                    disabled={busy}
                    onClick={(e) => {
                      e.stopPropagation();
                      setPendingDelete(w.id);
                    }}
                  >
                    删除
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* 删除确认 */}
      {pendingDelete && (
        <div className="modal-mask" onMouseDown={(e) => e.target === e.currentTarget && setPendingDelete(null)}>
          <div className="modal" role="dialog" aria-modal="true">
            <h3>删除工作流</h3>
            <p className="field-help">
              「{workflows.find((w) => w.id === pendingDelete)?.name}」将被永久删除，已发布的调用也会一并停止。此操作不可恢复。
            </p>
            <div className="modal-actions">
              <button className="btn btn--ghost btn--sm" onClick={() => setPendingDelete(null)}>
                取消
              </button>
              <button className="btn btn--danger btn--sm" disabled={busy} onClick={() => remove(pendingDelete)}>
                确认删除
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
