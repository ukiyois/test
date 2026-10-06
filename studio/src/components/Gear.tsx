/* 机械元素 SVG：齿轮、飞轮、指针 —— 2px 线稿，金属色 */

interface GearSvgProps {
  size?: number;
  color?: string;
  teeth?: number;
}

/** 线稿齿轮。teeth 默认 8，黄铜色。 */
export function GearSvg({ size = 16, color = "var(--brass)", teeth = 8 }: GearSvgProps) {
  const cx = 12;
  const rOuter = 10;
  const rInner = 7;
  const points: string[] = [];
  for (let i = 0; i < teeth; i++) {
    const a0 = (i / teeth) * Math.PI * 2;
    const a1 = ((i + 0.32) / teeth) * Math.PI * 2;
    const a2 = ((i + 0.5) / teeth) * Math.PI * 2;
    const a3 = ((i + 0.82) / teeth) * Math.PI * 2;
    const p = (a: number, r: number) =>
      `${(cx + Math.cos(a) * r).toFixed(2)},${(cx + Math.sin(a) * r).toFixed(2)}`;
    points.push(`${p(a0, rInner)} ${p(a0, rOuter)} ${p(a1, rOuter)} ${p(a2, rInner)}`);
    if (i === teeth - 1) {
      points.push(`${p(a2, rInner)} ${p(a3, rOuter)}`);
    }
  }
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d={points.map((seg) => `M${seg.split(" ")[0]}L${seg.split(" ").slice(1).join("L")}`).join("")}
        stroke={color}
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <circle cx="12" cy="12" r="7" stroke={color} strokeWidth="1.6" />
      <circle cx="12" cy="12" r="2.6" stroke={color} strokeWidth="1.6" />
    </svg>
  );
}

/** 推理飞轮：带辐条的轮，转速由调用方控制 className */
export function FlywheelSvg({ size = 16, color = "var(--accent)" }: GearSvgProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="9.5" stroke={color} strokeWidth="2" />
      <circle cx="12" cy="12" r="2" stroke={color} strokeWidth="1.6" />
      {[0, 60, 120].map((deg) => (
        <line
          key={deg}
          x1="12"
          y1="12"
          x2={12 + 9 * Math.cos((deg * Math.PI) / 180)}
          y2={12 + 9 * Math.sin((deg * Math.PI) / 180)}
          stroke={color}
          strokeWidth="1.4"
        />
      ))}
      {[180, 240, 300].map((deg) => (
        <line
          key={deg}
          x1="12"
          y1="12"
          x2={12 + 9 * Math.cos((deg * Math.PI) / 180)}
          y2={12 + 9 * Math.sin((deg * Math.PI) / 180)}
          stroke={color}
          strokeWidth="1.4"
        />
      ))}
    </svg>
  );
}

/** 运行中的齿轮指示（自带旋转动画） */
export function SpinningGear({ size = 14, fast = false }: { size?: number; fast?: boolean }) {
  return (
    <span className={`gear${fast ? " gear--fast" : ""}`}>
      <GearSvg size={size} />
    </span>
  );
}
