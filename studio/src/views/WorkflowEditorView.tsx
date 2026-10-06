/* 工作流编辑器（全屏）：步骤库 + 画布 + 检查器 + 底部运行台（SSE 实时） */

import { useCallback, useEffect, useState } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  applyNodeChanges,
  type Edge,
  type NodeChange,
  type Connection,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import dagre from "@dagrejs/dagre";
import { useLocation, useParams } from "wouter";
import { api, streamRun } from "@/api/client";
import type { FileRecord, Run, RunEvent, WorkflowDef } from "@/api/types";
import { useWorkflows } from "@/stores/workflows";
import { useRuntime } from "@/stores/runtime";
import { toast } from "@/stores/ui";
import { useWorkflowEditor } from "@/workflow/store";
import {
  ADDABLE,
  NODE_META,
  RUN_ACTIVE,
  RUN_STATUS_LABELS,
  graphPositions,
  nodeRunState,
  routeKeys,
  routeTarget,
  summaryFor as summaryOf,
} from "@/workflow/graph";
import { nodeTypes, type WfNode } from "@/workflow/FlowNode";
import { NodeInspector } from "@/workflow/NodeInspector";
import { TraceEventRow } from "@/workflow/TraceEvent";

/* ---------- definition ↔ React Flow ---------- */

function asFlowNodes(def: WorkflowDef, run: Run | null): WfNode[] {
  const positions = graphPositions(def);
  return def.nodes.map((node) => ({
    id: node.id,
    type: "workflow" as const,
    position: positions[node.id],
    data: {
      node,
      summary: summaryOf(node),
      runState: run ? nodeRunState(run.events, node.id) : "",
    },
  }));
}

function asFlowEdges(def: WorkflowDef): Edge[] {
  return def.nodes.flatMap((node) =>
    routeKeys(node).flatMap(([key, label]) => {
      const target = routeTarget(node, key);
      if (!target || !def.nodes.some((n) => n.id === target)) return [];
      const isError = key === "error_next";
      const isAlt = key.includes("false") || key.includes("reject");
      return [
        {
          id: `${node.id}:${key}:${target}`,
          source: node.id,
          target,
          sourceHandle: key,
          targetHandle: "in",
          label,
          type: "smoothstep",
          style: {
            stroke: isError ? "var(--danger)" : isAlt ? "var(--copper)" : "var(--accent)",
            strokeWidth: isError ? 2 : 1.6,
          },
          labelStyle: { fill: isError ? "var(--danger)" : "var(--text-muted)", fontSize: 10, fontWeight: 600 },
          labelBgStyle: { fill: "var(--bg-deep)", fillOpacity: 0.95 },
          labelBgPadding: [6, 3] as [number, number],
          labelBgBorderRadius: 4,
        },
      ];
    }),
  );
}

/** dagre 自动整理布局 */
function tidyLayout(def: WorkflowDef): WorkflowDef {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 60, ranksep: 120 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of def.nodes) g.setNode(n.id, { width: 250, height: 130 });
  for (const n of def.nodes)
    for (const [key] of routeKeys(n)) {
      const t = routeTarget(n, key);
      if (t && def.nodes.some((x) => x.id === t)) g.setEdge(n.id, t);
    }
  dagre.layout(g);
  return {
    ...def,
    nodes: def.nodes.map((n) => {
      const p = g.node(n.id);
      return p ? { ...n, position: { x: p.x - 125, y: p.y - 65 } } : n;
    }),
  };
}

/* ---------- 主视图 ---------- */

export function WorkflowEditorView() {
  const params = useParams<{ id: string }>();
  const [, navigate] = useLocation();
  const { workflows, tools, fetchWorkflows } = useWorkflows();
  const models = useRuntime((s) => s.models);
  const fetchModels = useRuntime((s) => s.fetchModels);

  const {
    workflow, definition, selectedId, dirty, past, future,
    load, unload, mutate, undo, redo, select, updateNode, addNode, deleteNodes,
    connect, deleteEdge, duplicateNode, markSaved,
  } = useWorkflowEditor();

  const [nodes, setNodes] = useState<WfNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [flow, setFlow] = useState<ReactFlowInstance<WfNode, Edge> | null>(null);
  const [busy, setBusy] = useState(false);
  const [validation, setValidation] = useState<{ valid: boolean; errors?: string[]; warnings?: string[] } | null>(null);
  const [taskInput, setTaskInput] = useState("");
  const [files, setFiles] = useState<FileRecord[]>([]);
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [approvalNote, setApprovalNote] = useState("");
  const [deployOpen, setDeployOpen] = useState(false);

  /* 载入工作流 */
  useEffect(() => {
    fetchWorkflows().catch((e) => toast.error(String(e)));
    if (models.length === 0) fetchModels().catch(() => undefined);
  }, [fetchWorkflows, fetchModels, models.length]);

  useEffect(() => {
    const wf = workflows.find((w) => w.id === params.id);
    if (wf) load(wf);
    else if (workflows.length > 0) navigate("/workflow");
    return () => unload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.id, workflows]);

  /* definition → flow 同步（只在 definition 引用变化时重建） */
  const canOverlay = Boolean(
    activeRun && activeRun.workflow_id === workflow?.id && !dirty &&
      activeRun.workflow_version === workflow?.version,
  );
  useEffect(() => {
    if (!definition) return;
    setNodes(asFlowNodes(definition, canOverlay ? activeRun : null));
    setEdges(asFlowEdges(definition));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [definition, activeRun, canOverlay]);

  /* SSE 运行推送 */
  useEffect(() => {
    if (!activeRun || !RUN_ACTIVE.includes(activeRun.status) && activeRun.status !== "waiting_approval") return;
    const handle = streamRun(activeRun.id, {
      onSnapshot: (snap) =>
        setActiveRun((r) => (r ? { ...r, status: snap.status as Run["status"], current_node: snap.current_node, pending: snap.pending, error: snap.error, events: snap.events as RunEvent[] } : r)),
      onEvent: (ev) =>
        setActiveRun((r) =>
          r && !r.events?.some((e) => e.id === (ev as RunEvent).id)
            ? { ...r, events: [...(r.events || []), { id: (ev as RunEvent).id ?? Date.now(), ...ev } as RunEvent] }
            : r,
        ),
      onStatus: (st) =>
        setActiveRun((r) => (r ? { ...r, status: st.status as Run["status"], current_node: st.current_node, pending: st.pending, error: st.error } : r)),
      onClose: () => {
        // 终态断开后再拉一次完整状态（拿 result）
        if (activeRun) api.run(activeRun.id).then(setActiveRun).catch(() => undefined);
      },
    });
    return () => handle.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRun?.id, activeRun?.status]);

  /* ---------- 画布交互 ---------- */

  const onNodesChange = useCallback(
    (changes: NodeChange<WfNode>[]) => {
      setNodes((cur) => applyNodeChanges(changes, cur));
      const moved = changes.filter(
        (c): c is Extract<NodeChange<WfNode>, { type: "position" }> =>
          c.type === "position" && Boolean(c.position),
      );
      if (moved.length) {
        // 拖动属于高频操作，不记历史
        mutate(
          (prev) => ({
            ...prev,
            nodes: prev.nodes.map((n) => {
              const m = moved.find((x) => x.id === n.id);
              return m && m.position ? { ...n, position: m.position } : n;
            }),
          }),
          false,
        );
      }
      const removed = changes
        .filter((c): c is Extract<NodeChange<WfNode>, { type: "remove" }> => c.type === "remove")
        .map((c) => c.id);
      if (removed.length) deleteNodes(removed);
    },
    [mutate, deleteNodes],
  );

  const onEdgesChange = useCallback(
    (changes: { type: string; id?: string }[]) => {
      const deleted = changes.filter((c) => c.type === "remove" && c.id);
      for (const d of deleted) deleteEdge(d.id!);
    },
    [deleteEdge],
  );

  const onConnect = useCallback(
    (conn: Connection) => {
      if (!conn.source || !conn.target || !conn.sourceHandle) return;
      const error = connect(conn.source, conn.sourceHandle, conn.target);
      if (error) toast.warning(error);
    },
    [connect],
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const type = e.dataTransfer.getData("application/x-node-type");
      if (!(ADDABLE as readonly string[]).includes(type) || !flow) return;
      const bounds = (e.currentTarget as HTMLElement).getBoundingClientRect();
      const pos = flow.screenToFlowPosition({ x: e.clientX - bounds.left, y: e.clientY - bounds.top });
      addNode(type, { x: pos.x - 125, y: pos.y - 60 });
    },
    [flow, addNode],
  );

  /* ---------- 快捷键 ---------- */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const typing = target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === "z" && !typing) {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
      } else if (mod && e.key.toLowerCase() === "y" && !typing) {
        e.preventDefault();
        redo();
      } else if (mod && e.key.toLowerCase() === "s" && !typing) {
        e.preventDefault();
        save();
      } else if (mod && e.key.toLowerCase() === "d" && !typing && selectedId) {
        e.preventDefault();
        duplicateNode(selectedId);
      } else if (e.key === "Escape") {
        select(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  /* ---------- 操作 ---------- */

  const save = async (silent = false) => {
    if (!definition || !workflow) return false;
    setBusy(true);
    try {
      const saved = await api.saveWorkflow(definition, workflow.id);
      markSaved(saved);
      useWorkflows.setState((s) => ({
        workflows: s.workflows.map((w) => (w.id === saved.id ? saved : w)),
      }));
      if (!silent) toast.success("草稿已保存");
      return true;
    } catch (e) {
      toast.error(String(e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const validate = async () => {
    if (!definition) return;
    try {
      const result = await api.validateWorkflow(definition);
      setValidation(result);
      if (result.valid) toast.success("流程检查通过");
      else toast.warning(`发现 ${result.errors?.length ?? 0} 个问题`);
    } catch (e) {
      toast.error(String(e));
    }
  };

  const publish = async () => {
    if (!workflow) return;
    if (dirty && !(await save(true))) return;
    try {
      const result = await api.publishWorkflow(workflow.id);
      markSaved(result);
      useWorkflows.setState((s) => ({
        workflows: s.workflows.map((w) => (w.id === result.id ? result : w)),
      }));
      toast.success(`已发布 v${result.published_version}`);
      setDeployOpen(true);
    } catch (e) {
      toast.error(String(e));
    }
  };

  const runWorkflow = async () => {
    if (!taskInput.trim() || !workflow) return;
    if (dirty && !(await save(true))) return;
    setBusy(true);
    setActiveRun(null);
    try {
      const run = await api.startRun(workflow.id, taskInput, files.map((f) => f.id));
      const full = await api.run(run.run_id);
      setActiveRun(full);
      toast.success("工作流已开始运行");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const approveRun = async (approved: boolean) => {
    if (!activeRun) return;
    try {
      await api.resumeRun(activeRun.id, approved, approvalNote);
      setApprovalNote("");
      setActiveRun((r) => (r ? { ...r, status: "queued" } : r));
      toast.success(approved ? "已批准，工作流继续" : "已退回，工作流继续");
    } catch (e) {
      toast.error(String(e));
    }
  };

  const cancelRun = async () => {
    if (!activeRun) return;
    try {
      await api.cancelRun(activeRun.id);
      setActiveRun((r) => (r ? { ...r, status: "cancel_requested" } : r));
    } catch (e) {
      toast.error(String(e));
    }
  };

  const focusNode = (nodeId: string) => {
    if (!definition || !flow) return;
    const pos = graphPositions(definition)[nodeId];
    if (!pos) return;
    select(nodeId);
    flow.setCenter(pos.x + 125, pos.y + 65, { zoom: 1.05, duration: 400 });
  };

  const uploadFiles = async (list: FileList | null) => {
    if (!list) return;
    for (const f of Array.from(list)) {
      if (f.size > 15 * 1024 * 1024) {
        toast.warning(`${f.name} 超过 15MB，已跳过`);
        continue;
      }
      try {
        const record = await api.uploadFile(f);
        setFiles((cur) => [...cur, record]);
      } catch (e) {
        toast.error(String(e));
      }
    }
  };

  if (!definition || !workflow) {
    return (
      <div className="wfe wfe--empty">
        <p>正在装载工作台…</p>
      </div>
    );
  }

  const selected = definition.nodes.find((n) => n.id === selectedId) || null;
  const callableModels = models.filter((m) => m.available && ["chat", "text2text"].includes(String(m.kind)));
  const deployUrl = `${window.location.origin}/v1/agents/${encodeURIComponent(workflow.id)}/runs`;
  const runActive = activeRun && RUN_ACTIVE.includes(activeRun.status);

  return (
    <div className="wfe">
      {/* 顶栏 */}
      <header className="wfe-top">
        <button className="btn btn--ghost btn--sm" onClick={() => navigate("/workflow")}>
          ← 工作流
        </button>
        <input
          className="wfe-title"
          value={definition.name}
          onChange={(e) => mutate((p) => ({ ...p, name: e.target.value }))}
        />
        <span className={`badge ${dirty ? "badge--warning" : "badge--muted"}`}>
          {dirty ? "未保存" : `草稿 v${workflow.version}`}
        </span>
        {workflow.published_version ? (
          <span className="badge badge--accent">已发布 v{workflow.published_version}</span>
        ) : null}
        <span style={{ flex: 1 }} />
        <button className="btn btn--ghost btn--sm" disabled={past.length === 0} onClick={undo} data-tip="撤销 Ctrl+Z">
          ↩
        </button>
        <button className="btn btn--ghost btn--sm" disabled={future.length === 0} onClick={redo} data-tip="重做 Ctrl+Shift+Z">
          ↪
        </button>
        <button className="btn btn--ghost btn--sm" onClick={() => definition && mutate(tidyLayout)} data-tip="自动整理布局">
          整理
        </button>
        <button className="btn btn--secondary btn--sm" onClick={validate}>
          检查
        </button>
        <button className="btn btn--secondary btn--sm" disabled={busy || !dirty} onClick={() => save()}>
          保存
        </button>
        <button className="btn btn--primary btn--sm" onClick={publish}>
          发布 ↗
        </button>
      </header>

      <div className="wfe-body">
        {/* 步骤库 */}
        <aside className="wfe-palette">
          <div className="wfe-palette__title">步骤库</div>
          {ADDABLE.map((type) => {
            const meta = NODE_META[type];
            return (
              <button
                key={type}
                className="wfe-step"
                draggable
                onDragStart={(e) => e.dataTransfer.setData("application/x-node-type", type)}
                onClick={() => addNode(type)}
                style={{ borderLeftColor: `var(--node-${meta.color})` }}
              >
                <i>{meta.icon}</i>
                <b>{meta.title}</b>
              </button>
            );
          })}
          <div className="wfe-palette__title" style={{ marginTop: "var(--sp-4)" }}>说明</div>
          <label className="field">
            <span>用途</span>
            <textarea
              rows={3}
              value={definition.description || ""}
              onChange={(e) => mutate((p) => ({ ...p, description: e.target.value }))}
            />
          </label>
          <label className="field">
            <span>默认模型</span>
            <select
              value={definition.model || ""}
              onChange={(e) => mutate((p) => ({ ...p, model: e.target.value }))}
            >
              <option value="">选择模型</option>
              {callableModels.map((m) => (
                <option
                  key={m.id}
                  value={
                    m.source === "remote" ? `remote/${m.provider_id}/${m.remote_model_id}` : `local/${m.id}`
                  }
                >
                  {m.name || m.id}
                </option>
              ))}
            </select>
          </label>
        </aside>

        {/* 画布 */}
        <div className="wfe-canvas" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
          <ReactFlow<WfNode>
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.18 }}
            minZoom={0.25}
            maxZoom={1.5}
            onInit={setFlow}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={(_, node) => select(node.id)}
            onPaneClick={() => select(null)}
            deleteKeyCode={["Backspace", "Delete"]}
            proOptions={{ hideAttribution: true }}
            colorMode="dark"
          >
            <Background color="#1e2530" gap={22} size={1} />
            <Controls position="bottom-left" showInteractive={false} />
            <MiniMap
              position="bottom-right"
              pannable
              zoomable
              nodeColor={(n) =>
                `var(--node-${NODE_META[(n as WfNode).data?.node?.type]?.color || "tool"})`
              }
            />
          </ReactFlow>
          <div className="wfe-canvas__hint">
            滚轮缩放 · 拖空白平移 · Delete 删除 · Ctrl+S 保存 · Ctrl+D 复制节点
          </div>
        </div>

        {/* 检查器 */}
        <aside className="wfe-inspector">
          <div className="wfe-inspector__head">
            <b>步骤设置</b>
            {selected && !["start", "end"].includes(selected.type) && (
              <button className="btn btn--danger btn--sm" onClick={() => deleteNodes([selected.id])}>
                删除
              </button>
            )}
          </div>
          <div className="wfe-inspector__scroll">
            {selected ? (
              <NodeInspector
                node={selected}
                definition={definition}
                models={callableModels}
                tools={tools}
                workflows={workflows.filter((w) => w.id !== workflow.id)}
                onChange={(values) => updateNode(selected.id, values)}
              />
            ) : (
              <div className="wfe-inspector__empty">
                <p>选择画布中的步骤，编辑模型、提示词、工具和分支。</p>
              </div>
            )}
            <div className="wfe-check">
              <div className="wfe-check__head">
                <b>结构检查</b>
                <button className="btn btn--ghost btn--sm" onClick={validate}>
                  运行检查
                </button>
              </div>
              {validation ? (
                <>
                  <p className={validation.valid ? "check-ok" : "check-error"}>
                    {validation.valid ? "流程结构有效" : `${validation.errors?.length ?? 0} 个错误`}
                  </p>
                  {validation.errors?.map((e) => (
                    <div className="check-msg is-error" key={e}>{e}</div>
                  ))}
                  {validation.warnings?.map((w) => (
                    <div className="check-msg is-warning" key={w}>{w}</div>
                  ))}
                </>
              ) : (
                <p className="field-help">检查连接、分支目标和工具配置。</p>
              )}
            </div>
          </div>
        </aside>
      </div>

      {/* 运行台 */}
      <section className="wfe-run">
        <div className="wfe-run__composer">
          <textarea
            value={taskInput}
            onChange={(e) => setTaskInput(e.target.value)}
            placeholder="写下 Agent 本次要完成的事情……"
            rows={2}
          />
          <div className="wfe-run__actions">
            <label className="btn btn--ghost btn--sm">
              ＋ 文件
              <input
                type="file"
                hidden
                multiple
                onChange={(e) => {
                  uploadFiles(e.target.files);
                  e.target.value = "";
                }}
              />
            </label>
            {files.length > 0 && <span className="field-help">{files.length} 个文件</span>}
            <span style={{ flex: 1 }} />
            {activeRun && runActive && (
              <button className="btn btn--ghost btn--sm" onClick={cancelRun}>
                取消
              </button>
            )}
            <button
              className="btn btn--primary btn--sm"
              disabled={busy || !taskInput.trim() || !!runActive}
              onClick={runWorkflow}
            >
              运行测试 ↗
            </button>
          </div>
          {files.length > 0 && (
            <div className="chat__files">
              {files.map((f) => (
                <span key={f.id} className="file-chip">
                  <span className="file-chip__name">{f.filename}</span>
                  <button onClick={() => setFiles((cur) => cur.filter((x) => x.id !== f.id))}>×</button>
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="wfe-run__trace">
          {activeRun ? (
            <>
              <div className="wfe-run__status">
                <span className={`badge badge--${activeRun.status === "completed" ? "success" : activeRun.status === "failed" ? "danger" : activeRun.status === "waiting_approval" ? "warning" : "info"}`}>
                  {RUN_STATUS_LABELS[activeRun.status] || activeRun.status}
                </span>
                <code>{activeRun.id}</code>
                {activeRun.workflow_version && <small>快照 v{activeRun.workflow_version}</small>}
              </div>
              {activeRun.error && <div className="check-msg is-error">{activeRun.error}</div>}
              {activeRun.status === "waiting_approval" && (
                <div className="approval-gate">
                  <div className="approval-gate__stripe" />
                  <div className="approval-gate__body">
                    <b>{String(activeRun.pending?.message || "需要人工审核")}</b>
                    {activeRun.pending?.output != null && (
                      <pre className="trace-row__detail">
                        {typeof activeRun.pending.output === "string"
                          ? activeRun.pending.output
                          : JSON.stringify(activeRun.pending.output, null, 2)}
                      </pre>
                    )}
                    <textarea
                      value={approvalNote}
                      onChange={(e) => setApprovalNote(e.target.value)}
                      placeholder="审核意见（退回时写明修改意见）"
                      rows={2}
                    />
                    <div className="wfe-run__actions">
                      <button className="btn btn--secondary btn--sm" onClick={() => approveRun(false)}>
                        退回
                      </button>
                      <button className="btn btn--primary btn--sm" onClick={() => approveRun(true)}>
                        批准并继续
                      </button>
                    </div>
                  </div>
                </div>
              )}
              {activeRun.state?.result != null && (
                <div className="result-card">
                  <span>最终输出</span>
                  <pre>
                    {typeof activeRun.state.result === "string"
                      ? activeRun.state.result
                      : JSON.stringify(activeRun.state.result, null, 2)}
                  </pre>
                </div>
              )}
              <div className="trace-list">
                {(activeRun.events || []).map((ev) => (
                  <TraceEventRow key={ev.id} event={ev} onLocate={canOverlay ? focusNode : undefined} />
                ))}
              </div>
            </>
          ) : (
            <div className="wfe-run__empty">
              <p>运行轨迹会显示在这里——节点状态、工具调用、重试、审核与输出。</p>
            </div>
          )}
        </div>
      </section>

      {/* 发布弹层 */}
      {deployOpen && (
        <div className="modal-mask" onMouseDown={(e) => e.target === e.currentTarget && setDeployOpen(false)}>
          <div className="modal" role="dialog" aria-modal="true">
            <h3>{workflow.name} · 已发布 v{workflow.published_version}</h3>
            <p className="field-help">所有调用都固定在此版本，草稿修改不会影响线上请求。</p>
            <label className="field">
              <span>Agent Run Endpoint</span>
              <code className="deploy-url">{deployUrl}</code>
            </label>
            <pre className="deploy-code">{`curl -X POST "${deployUrl}" \\
  -H "Authorization: Bearer YOUR_PLATFORM_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"input":"处理这份任务","file_ids":[]}'`}</pre>
            <div className="modal-actions">
              <button
                className="btn btn--secondary btn--sm"
                onClick={() => {
                  navigator.clipboard.writeText(deployUrl);
                  toast.success("已复制 URL");
                }}
              >
                复制 URL
              </button>
              <button
                className="btn btn--ghost btn--sm"
                onClick={async () => {
                  try {
                    const r = await api.unpublishWorkflow(workflow.id);
                    markSaved(r);
                    useWorkflows.setState((s) => ({ workflows: s.workflows.map((w) => (w.id === r.id ? r : w)) }));
                    toast.success("已停止发布");
                    setDeployOpen(false);
                  } catch (e) {
                    toast.error(String(e));
                  }
                }}
              >
                停止发布
              </button>
              <button className="btn btn--primary btn--sm" onClick={() => setDeployOpen(false)}>
                完成
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
