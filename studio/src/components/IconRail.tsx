/* 图标轨：56px 窄轨 + hover 浮层标签 + 黄铜滑块指示器 */

import { useLocation } from "wouter";
import type { ReactNode } from "react";
import { GearSvg } from "./Gear";

interface NavItem {
  path: string;
  label: string;
  icon: ReactNode;
}

const stroke = "currentColor";
const sw = 1.7;

const iconChat = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden>
    <path d="M4 5h16v11H9l-5 4V5z" stroke={stroke} strokeWidth={sw} strokeLinejoin="round" />
    <line x1="8" y1="9" x2="16" y2="9" stroke={stroke} strokeWidth={sw} />
    <line x1="8" y1="12.5" x2="13" y2="12.5" stroke={stroke} strokeWidth={sw} />
  </svg>
);
const iconFlow = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden>
    <rect x="3" y="4" width="6" height="6" rx="1.5" stroke={stroke} strokeWidth={sw} />
    <rect x="15" y="14" width="6" height="6" rx="1.5" stroke={stroke} strokeWidth={sw} />
    <path d="M9 7h5a3 3 0 0 1 3 3v4" stroke={stroke} strokeWidth={sw} />
    <circle cx="18" cy="11" r="1.4" fill={stroke} />
  </svg>
);
const iconModels = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden>
    <circle cx="12" cy="12" r="8.5" stroke={stroke} strokeWidth={sw} />
    <circle cx="12" cy="12" r="3" stroke={stroke} strokeWidth={sw} />
    <line x1="12" y1="3.5" x2="12" y2="9" stroke={stroke} strokeWidth={sw} />
    <line x1="12" y1="15" x2="12" y2="20.5" stroke={stroke} strokeWidth={sw} />
    <line x1="3.5" y1="12" x2="9" y2="12" stroke={stroke} strokeWidth={sw} />
    <line x1="15" y1="12" x2="20.5" y2="12" stroke={stroke} strokeWidth={sw} />
  </svg>
);
const iconRuns = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden>
    <path d="M5 4h14M5 20h14" stroke={stroke} strokeWidth={sw} />
    <path d="M7 4c0 5 3 6 5 8-2 2-5 3-5 8M17 4c0 5-3 6-5 8 2 2 5 3 5 8" stroke={stroke} strokeWidth={sw} strokeLinejoin="round" />
  </svg>
);
const iconMonitor = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden>
    <path d="M4 19a9 9 0 1 1 16 0" stroke={stroke} strokeWidth={sw} />
    <line x1="12" y1="15" x2="16.5" y2="9" stroke={stroke} strokeWidth={2} strokeLinecap="round" />
    <circle cx="12" cy="15" r="1.6" fill={stroke} />
  </svg>
);

const NAV: NavItem[] = [
  { path: "/", label: "对话", icon: iconChat },
  { path: "/workflow", label: "工作流", icon: iconFlow },
  { path: "/models", label: "模型与 API", icon: iconModels },
  { path: "/runs", label: "运行记录", icon: iconRuns },
  { path: "/monitor", label: "监控", icon: iconMonitor },
];

export function IconRail() {
  const [location, navigate] = useLocation();

  const activeIndex = NAV.findIndex((item) =>
    item.path === "/" ? location === "/" : location.startsWith(item.path),
  );

  return (
    <nav className="rail" aria-label="主导航">
      <div className="rail__logo" data-tip="LLMmodol">
        <GearSvg size={22} color="var(--accent)" />
      </div>
      <div className="rail__items">
        {/* 黄铜滑块指示器：沿轨道滑动，带过冲回正 */}
        {activeIndex >= 0 && (
          <span
            className="rail__indicator"
            style={{ transform: `translateY(${activeIndex * 44}px)` }}
          />
        )}
        {NAV.map((item) => {
          const active =
            item.path === "/" ? location === "/" : location.startsWith(item.path);
          return (
            <button
              key={item.path}
              className={`rail__item${active ? " is-active" : ""}`}
              onClick={() => navigate(item.path)}
              aria-label={item.label}
              aria-current={active ? "page" : undefined}
            >
              {item.icon}
              <span className="rail__tip">{item.label}</span>
            </button>
          );
        })}
      </div>
      <div className="rail__foot">
        <button
          className="rail__item"
          data-tip="命令面板 Ctrl+K"
          onClick={() =>
            window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true }))
          }
          aria-label="命令面板"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
            <rect x="4" y="4" width="16" height="16" rx="3" stroke={stroke} strokeWidth={sw} />
            <path d="M9 9.5L7 12l2 2.5M15 9.5L17 12l-2 2.5" stroke={stroke} strokeWidth={sw} strokeLinecap="round" />
          </svg>
        </button>
      </div>
    </nav>
  );
}
