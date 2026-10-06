/* Toast 堆叠渲染 */

import { useUi } from "../stores/ui";

export function ToastStack() {
  const { toasts, dismissToast } = useUi();
  if (toasts.length === 0) return null;
  return (
    <div className="toast-stack" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast--${t.kind}`}>
          <div className="toast__body">
            {t.title && <div className="toast__title">{t.title}</div>}
            <div className="toast__msg">{t.message}</div>
          </div>
          <button
            className="toast__close"
            onClick={() => dismissToast(t.id)}
            aria-label="关闭"
          >
            ×
          </button>
          {t.duration > 0 && (
            <span
              className="toast__timer"
              style={{ animationDuration: `${t.duration}ms` }}
            />
          )}
        </div>
      ))}
    </div>
  );
}
