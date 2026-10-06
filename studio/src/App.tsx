/* 应用骨架：图标轨 + 顶栏 + 路由出口 + 状态条；编辑器路由全屏 */

import { useEffect } from "react";
import { Route, Switch, useLocation } from "wouter";
import { IconRail } from "./components/IconRail";
import { Topbar } from "./components/Topbar";
import { Statusbar } from "./components/Statusbar";
import { ToastStack } from "./components/Toast";
import { CommandPalette } from "./components/CommandPalette";
import { useUi } from "./stores/ui";
import { useChat } from "./stores/chat";
import { ChatView } from "./views/ChatView";
import { WorkflowListView } from "./views/WorkflowListView";
import { WorkflowEditorView } from "./views/WorkflowEditorView";
import { ModelsView } from "./views/ModelsView";
import { RunsView } from "./views/RunsView";
import { RunDetailView } from "./views/RunDetailView";
import { MonitorView } from "./views/MonitorView";

const VIEW_HOTKEYS: Record<string, string> = {
  "1": "/",
  "2": "/workflow",
  "3": "/models",
  "4": "/runs",
  "5": "/monitor",
};

export function App() {
  const [location, navigate] = useLocation();
  const { cmdkOpen, setCmdk } = useUi();
  const fetchConversations = useChat((s) => s.fetchConversations);

  // 编辑器全屏：隐藏图标轨/顶栏/状态条
  const fullscreen = /^\/workflow\/[^/]+$/.test(location);

  useEffect(() => {
    fetchConversations().catch(() => undefined);
  }, [fetchConversations]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCmdk(!useUi.getState().cmdkOpen);
        return;
      }
      if (cmdkOpen) return;
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);
      if (!typing && !e.ctrlKey && !e.metaKey && VIEW_HOTKEYS[e.key]) {
        navigate(VIEW_HOTKEYS[e.key]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate, cmdkOpen, setCmdk]);

  return (
    <div className="shell">
      {!fullscreen && <IconRail />}
      <div className="shell__main">
        {!fullscreen && <Topbar />}
        <main className="shell__view">
          <Switch>
            <Route path="/" component={ChatView} />
            <Route path="/workflow" component={WorkflowListView} />
            <Route path="/workflow/:id" component={WorkflowEditorView} />
            <Route path="/models" component={ModelsView} />
            <Route path="/runs" component={RunsView} />
            <Route path="/runs/:id" component={RunDetailView} />
            <Route path="/monitor" component={MonitorView} />
            <Route>
              <div style={{ padding: 48, color: "var(--text-secondary)" }}>
                <h1>404</h1>
                <p>这个舱室不存在。</p>
              </div>
            </Route>
          </Switch>
        </main>
        {!fullscreen && <Statusbar />}
      </div>
      <ToastStack />
      <CommandPalette />
    </div>
  );
}
