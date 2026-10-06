/* 运行时切片：模型状态、系统指标、轮询 */

import { create } from "zustand";
import { api } from "../api/client";
import type { ModelInfo, RuntimeStatus, SystemInfo } from "../api/types";

interface RuntimeState {
  runtime: RuntimeStatus | null;
  models: ModelInfo[];
  system: SystemInfo | null;
  loadingModel: string | null;
  fetchRuntime: () => Promise<void>;
  fetchModels: () => Promise<void>;
  fetchSystem: () => Promise<void>;
  loadModel: (id: string) => Promise<void>;
  unloadModel: () => Promise<void>;
}

export const useRuntime = create<RuntimeState>((set, get) => ({
  runtime: null,
  models: [],
  system: null,
  loadingModel: null,

  fetchRuntime: async () => {
    try {
      const { runtime } = await api.models();
      set({ runtime });
    } catch {
      /* 静默，状态条显示离线 */
    }
  },
  fetchModels: async () => {
    const { data, runtime } = await api.models();
    set({ models: data, runtime });
  },
  fetchSystem: async () => {
    try {
      set({ system: await api.system() });
    } catch {
      /* 静默 */
    }
  },
  loadModel: async (id) => {
    set({ loadingModel: id });
    try {
      const runtime = await api.loadModel(id);
      set({ runtime });
      await get().fetchModels();
    } finally {
      set({ loadingModel: null });
    }
  },
  unloadModel: async () => {
    const runtime = await api.unloadModel();
    set({ runtime });
  },
}));
