/* UI 切片：toast 通知、命令面板 */

import { create } from "zustand";

export interface Toast {
  id: number;
  kind: "info" | "success" | "warning" | "error";
  title?: string;
  message: string;
  /** 停留毫秒；错误常驻 */
  duration: number;
}

let nextToastId = 1;

interface UiState {
  toasts: Toast[];
  cmdkOpen: boolean;
  setCmdk: (open: boolean) => void;
  toast: (
    kind: Toast["kind"],
    message: string,
    title?: string,
    duration?: number,
  ) => void;
  dismissToast: (id: number) => void;
}

export const useUi = create<UiState>((set) => ({
  toasts: [],
  cmdkOpen: false,
  setCmdk: (open) => set({ cmdkOpen: open }),
  toast: (kind, message, title, duration) => {
    const id = nextToastId++;
    const stay = duration ?? (kind === "error" ? 0 : 3000);
    set((s) => ({
      toasts: [...s.toasts.slice(-4), { id, kind, message, title, duration: stay }],
    }));
    if (stay > 0) {
      setTimeout(() => {
        set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
      }, stay);
    }
  },
  dismissToast: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

/* 便捷全局入口（供非组件代码调用） */
export const toast = {
  info: (msg: string, title?: string) => useUi.getState().toast("info", msg, title),
  success: (msg: string, title?: string) =>
    useUi.getState().toast("success", msg, title),
  warning: (msg: string, title?: string) =>
    useUi.getState().toast("warning", msg, title),
  error: (msg: string, title?: string) =>
    useUi.getState().toast("error", msg, title),
};
