/* React Flow 自定义节点：Clockwork Bench 深色画布节点 */

import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import type { WorkflowNode } from "@/api/types";
import { NODE_META, routeKeys } from "./graph";

export type WfNode = Node<{ node: WorkflowNode; summary: string; runState: string }, "workflow">;

const STATE_LABELS: Record<string, string> = {
  completed: "完成",
  running: "运行",
  waiting: "审核",
  failed: "错误",
};

export function WorkflowNodeView({ data, selected }: NodeProps<WfNode>) {
  const meta = NODE_META[data.node.type] || NODE_META.tool;
  const routes = routeKeys(data.node);
  const isFanout = data.node.type === "fanout";
  return (
    <article
      className={`wfn node-${meta.color}${selected ? " is-selected" : ""}${data.runState ? ` run-${data.runState}` : ""}`}
      style={isFanout ? { height: `${Math.max(150, 80 + routes.length * 22)}px` } : undefined}
    >
      {data.node.type !== "start" && (
        <Handle type="target" position={Position.Left} id="in" className="wfn-handle wfn-handle--in" />
      )}
      <div className="wfn-top">
        <span className="wfn-icon">{meta.icon}</span>
        <span className="wfn-kind">{meta.title}</span>
        <span className={`wfn-state state-${data.runState || "idle"}`}>
          {STATE_LABELS[data.runState] || "草稿"}
        </span>
      </div>
      <strong className="wfn-label">{data.node.label || meta.title}</strong>
      <span className="wfn-summary">{data.summary}</span>
      <span className="wfn-id">{data.node.id}</span>
      {routes.map(([key, label], index) => (
        <span key={key}>
          <span
            className={`wfn-port-label${key === "error_next" ? " is-error" : ""}`}
            style={isFanout ? { top: `${66 + index * 20}px` } : { top: `${72 + index * 20}px` }}
          >
            {label}
          </span>
          <Handle
            type="source"
            position={Position.Right}
            id={key}
            style={{ top: `${(isFanout ? 70 : 76) + index * 20}px` }}
            className={`wfn-handle wfn-handle--out${key === "error_next" ? " is-error" : ""}`}
          />
        </span>
      ))}
    </article>
  );
}

export const nodeTypes = { workflow: WorkflowNodeView };
