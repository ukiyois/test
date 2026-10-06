/* 工作流编辑器 zustand store：定义草稿、撤销重做、剪贴板、脏标记 */

import { create } from "zustand";
import { api } from "@/api/client";
import type { Workflow, WorkflowDef } from "@/api/types";
import {
  defaultNode,
  removeNodes,
  routeKeys,
  routeTarget,
  validateConnection,
  withRouteTarget,
} from "./graph";

const HISTORY_LIMIT = 60;

interface EditorState {
  workflow: Workflow | null;
  definition: WorkflowDef | null;
  selectedId: string | null;
  dirty: boolean;
  past: WorkflowDef[];
  future: WorkflowDef[];

  load: (workflow: Workflow) => void;
  unload: () => void;
  mutate: (fn: (prev: WorkflowDef) => WorkflowDef, recordHistory?: boolean) => void;
  undo: () => void;
  redo: () => void;
  select: (id: string | null) => void;
  updateNode: (id: string, values: Partial<WorkflowDef["nodes"][number]>) => void;
  addNode: (type: string, position?: { x: number; y: number }) => string | null;
  deleteNodes: (ids: string[]) => void;
  connect: (source: string, sourceHandle: string, target: string) => string | null;
  deleteEdge: (edgeId: string) => void;
  duplicateNode: (id: string) => string | null;
  markSaved: (workflow: Workflow) => void;
}

export const useWorkflowEditor = create<EditorState>((set, get) => ({
  workflow: null,
  definition: null,
  selectedId: null,
  dirty: false,
  past: [],
  future: [],

  load: (workflow) =>
    set({
      workflow,
      definition: structuredClone(workflow.definition),
      selectedId: null,
      dirty: false,
      past: [],
      future: [],
    }),

  unload: () =>
    set({ workflow: null, definition: null, selectedId: null, dirty: false, past: [], future: [] }),

  mutate: (fn, recordHistory = true) => {
    const { definition, past } = get();
    if (!definition) return;
    const next = fn(definition);
    if (next === definition) return;
    set({
      definition: next,
      dirty: true,
      past: recordHistory ? [...past.slice(-HISTORY_LIMIT + 1), definition] : past,
      future: recordHistory ? [] : get().future,
    });
  },

  undo: () => {
    const { definition, past, future } = get();
    if (!definition || past.length === 0) return;
    set({
      definition: past[past.length - 1],
      past: past.slice(0, -1),
      future: [definition, ...future],
      dirty: true,
    });
  },

  redo: () => {
    const { definition, past, future } = get();
    if (!definition || future.length === 0) return;
    set({
      definition: future[0],
      future: future.slice(1),
      past: [...past, definition],
      dirty: true,
    });
  },

  select: (id) => set({ selectedId: id }),

  updateNode: (id, values) =>
    get().mutate((prev) => ({
      ...prev,
      nodes: prev.nodes.map((n) => (n.id === id ? { ...n, ...values } : n)),
    })),

  addNode: (type, position) => {
    const { definition } = get();
    if (!definition) return null;
    const { node, paired } = defaultNode(type, definition);
    if (position) node.position = position;
    get().mutate((prev) => ({
      ...prev,
      nodes: [...prev.nodes, node, ...(paired ? [paired] : [])],
    }));
    set({ selectedId: node.id });
    return node.id;
  },

  deleteNodes: (ids) => {
    const { definition, selectedId } = get();
    if (!definition || ids.length === 0) return;
    get().mutate((prev) => removeNodes(prev, new Set(ids)));
    if (selectedId && ids.includes(selectedId)) set({ selectedId: null });
  },

  connect: (source, sourceHandle, target) => {
    const { definition } = get();
    if (!definition) return null;
    const error = validateConnection(definition, source, sourceHandle, target);
    if (error) return error;
    get().mutate((prev) => ({
      ...prev,
      nodes: prev.nodes.map((n) => (n.id === source ? withRouteTarget(n, sourceHandle, target) : n)),
    }));
    return null;
  },

  deleteEdge: (edgeId) => {
    // edgeId 形如 source:handle:target
    const [source, handle] = edgeId.split(":");
    if (!source || !handle) return;
    const handleKey = edgeId.startsWith(`${source}:branch:`)
      ? `branch:${edgeId.split(":")[2]}`
      : handle;
    get().mutate((prev) => ({
      ...prev,
      nodes: prev.nodes.map((n) => (n.id === source ? withRouteTarget(n, handleKey, null) : n)),
    }));
  },

  duplicateNode: (id) => {
    const { definition } = get();
    if (!definition) return null;
    const node = definition.nodes.find((n) => n.id === id);
    if (!node || ["start", "end"].includes(node.type)) return null;
    const clone = structuredClone(node);
    clone.id = `${node.type}_${Math.random().toString(36).slice(2, 7)}`;
    clone.label = `${node.label || node.type} 副本`;
    const pos = node.position as { x: number; y: number } | undefined;
    clone.position = { x: (pos?.x ?? 400) + 40, y: (pos?.y ?? 300) + 40 };
    // 断开所有路由口，避免复制连线造成歧义
    let cleaned = clone;
    for (const [key] of routeKeys(cleaned)) {
      if (routeTarget(cleaned, key)) cleaned = withRouteTarget(cleaned, key, null);
    }
    get().mutate((prev) => ({ ...prev, nodes: [...prev.nodes, cleaned] }));
    set({ selectedId: cleaned.id });
    return cleaned.id;
  },

  markSaved: (workflow) => set({ workflow, dirty: false }),
}));

/** 校验当前定义（调后端） */
export async function validateDefinition(definition: WorkflowDef) {
  return api.validateWorkflow(definition);
}
