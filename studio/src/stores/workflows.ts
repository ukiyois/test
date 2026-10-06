/* 工作流与运行切片 */

import { create } from "zustand";
import { api } from "../api/client";
import type { Run, ToolInfo, Workflow } from "../api/types";

interface WorkflowState {
  workflows: Workflow[];
  tools: ToolInfo[];
  fetchWorkflows: () => Promise<void>;
  save: (definition: unknown, id?: string) => Promise<Workflow>;
}

export const useWorkflows = create<WorkflowState>((set, get) => ({
  workflows: [],
  tools: [],
  fetchWorkflows: async () => {
    const { data, tools } = await api.workflows();
    set({ workflows: data, tools });
  },
  save: async (definition, id) => {
    const workflow = await api.saveWorkflow(definition, id);
    await get().fetchWorkflows();
    return workflow;
  },
}));

interface RunsState {
  runs: Run[];
  fetchRuns: () => Promise<void>;
}

export const useRuns = create<RunsState>((set) => ({
  runs: [],
  fetchRuns: async () => {
    const { data } = await api.runs(100);
    set({ runs: data });
  },
}));
