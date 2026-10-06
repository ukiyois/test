/* 占位：随后按设计稿逐个实现 */

export function Placeholder({ title, note }: { title: string; note?: string }) {
  return (
    <div
      className="view-enter"
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 8,
        color: "var(--text-secondary)",
      }}
    >
      <h1>{title}</h1>
      <p>{note ?? "工位装配中…"}</p>
    </div>
  );
}
