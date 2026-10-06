/* 模型与 API：本地模型、远程服务商、平台 API 端点与密钥 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import type { Provider } from "@/api/types";
import { useRuntime } from "@/stores/runtime";
import { toast } from "@/stores/ui";

interface SystemPayload {
  title?: string;
  api_base_url?: string;
  openapi_url?: string;
  api_key_configured?: boolean;
  [key: string]: unknown;
}

export function ModelsView() {
  const { models, runtime, loadingModel, fetchModels, loadModel, unloadModel } = useRuntime();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [system, setSystem] = useState<SystemPayload | null>(null);
  const [editing, setEditing] = useState<Provider | "new" | null>(null);
  const [form, setForm] = useState({ name: "", base_url: "", api_key: "", model_ids: "" });
  const [busy, setBusy] = useState(false);
  const [ggufBusy, setGgufBusy] = useState(false);

  const refresh = useCallback(() => {
    fetchModels().catch((e) => toast.error(String(e)));
    api.providers().then((r) => setProviders(r.data)).catch(() => undefined);
    api.system().then(setSystem).catch(() => undefined);
  }, [fetchModels]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const localModels = models.filter((m) => m.source !== "remote");
  const remoteModels = models.filter((m) => m.source === "remote");
  const gguf = runtime?.gguf_runtime;

  const openEdit = (p: Provider | "new") => {
    setEditing(p);
    setForm(
      p === "new"
        ? { name: "", base_url: "", api_key: "", model_ids: "" }
        : {
            name: p.name,
            base_url: p.base_url,
            api_key: "",
            model_ids: (p.model_ids || []).join("\n"),
          },
    );
  };

  const saveProvider = async () => {
    if (!form.name.trim() || !form.base_url.trim()) {
      toast.warning("名称与 Base URL 必填");
      return;
    }
    setBusy(true);
    try {
      const body = {
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        api_key: form.api_key.trim() || null,
        model_ids: form.model_ids.split("\n").map((s) => s.trim()).filter(Boolean),
      };
      if (editing === "new") await api.saveProvider(body);
      else if (editing) await api.updateProvider(editing.id, body);
      setEditing(null);
      refresh();
      toast.success("服务商已保存");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const discover = async (id: string) => {
    setBusy(true);
    try {
      const r = await api.discoverProvider(id);
      const found = (r as { model_ids?: string[] }).model_ids || [];
      toast.success(`发现 ${found.length} 个模型`);
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const removeProvider = async (id: string) => {
    if (!window.confirm("删除此服务商？其下远程模型将不可调用。")) return;
    try {
      await api.deleteProvider(id);
      refresh();
      toast.success("已删除服务商");
    } catch (e) {
      toast.error(String(e));
    }
  };

  const toggleGguf = async () => {
    setGgufBusy(true);
    try {
      if (gguf?.running) {
        await api.ggufStop();
        toast.success("GGUF 引擎已停止");
      } else {
        await api.ggufStart();
        toast.success("GGUF 引擎已启动");
      }
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setGgufBusy(false);
    }
  };

  const rotateKey = async () => {
    if (!window.confirm("生成新密钥后旧密钥立即失效，继续？")) return;
    try {
      const r = await api.createApiKey();
      await navigator.clipboard.writeText(r.api_key);
      toast.success("新密钥已生成并复制到剪贴板（仅此一次显示）");
      refresh();
    } catch (e) {
      toast.error(String(e));
    }
  };

  return (
    <div className="models">
      <header className="page-head">
        <div>
          <h1>模型与 API</h1>
          <p>管理本地模型加载、远程服务商接入，以及对外 OpenAI 兼容端点。</p>
        </div>
      </header>

      <div className="models__grid">
        {/* 本地模型 */}
        <section className="card models__section">
          <div className="models__section-head">
            <h2>本地模型</h2>
            {runtime?.loaded && (
              <button className="btn btn--ghost btn--sm" onClick={() => unloadModel().catch((e) => toast.error(String(e)))}>
                卸载当前
              </button>
            )}
          </div>
          <div className="models__list">
            {localModels.map((m) => {
              const isGguf = m.id.startsWith("gguf/");
              const loaded = isGguf
                ? Boolean(gguf?.running)
                : runtime?.loaded && runtime.model_id === m.id;
              return (
                <div key={m.id} className={`model-row${loaded ? " is-loaded" : ""}`}>
                  <div className="model-row__info">
                    <b>{m.name || m.id}</b>
                    <small>
                      {isGguf ? "llama.cpp 引擎" : m.kind || "chat"}
                      {m.parameter_size ? ` · ${m.parameter_size}` : ""}
                      {m.context_length ? ` · ctx ${m.context_length}` : ""}
                    </small>
                  </div>
                  <span className={`badge ${m.available ? "badge--success" : "badge--muted"}`}>
                    {m.available ? "就绪" : "缺权重"}
                  </span>
                  {isGguf ? (
                    <button
                      className={`btn btn--sm ${gguf?.running ? "btn--danger" : "btn--secondary"}`}
                      disabled={!m.available || ggufBusy}
                      onClick={toggleGguf}
                    >
                      {ggufBusy ? "…" : gguf?.running ? "停止引擎" : "启动引擎"}
                    </button>
                  ) : loaded ? (
                    <span className="badge badge--accent">运行中</span>
                  ) : (
                    <button
                      className="btn btn--secondary btn--sm"
                      disabled={!m.available || loadingModel !== null}
                      onClick={() => loadModel(m.id).catch((e) => toast.error(String(e)))}
                    >
                      {loadingModel === m.id ? "装载中…" : "装载"}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
          {/* 动力分配表：显示已装载模型在 GPU/CPU 上的层分布 */}
          {runtime?.loaded && runtime.placement && <PlacementTable placement={runtime.placement} />}
        </section>

        {/* 远程服务商 */}
        <section className="card models__section">
          <div className="models__section-head">
            <h2>远程服务商</h2>
            <button className="btn btn--secondary btn--sm" onClick={() => openEdit("new")}>
              ＋ 添加
            </button>
          </div>
          <div className="models__list">
            {providers.length === 0 && <p className="field-help">还没有服务商，添加 OpenAI 兼容端点即可调用远程模型。</p>}
            {providers.map((p) => (
              <div key={p.id} className="model-row">
                <div className="model-row__info">
                  <b>{p.name}</b>
                  <small>{p.base_url} · {(p.model_ids || []).length} 个模型</small>
                </div>
                <span className={`badge ${p.has_key || (p as Record<string, unknown>).api_key_set ? "badge--success" : "badge--warning"}`}>
                  {p.has_key || (p as Record<string, unknown>).api_key_set ? "已配置密钥" : "缺密钥"}
                </span>
                <button className="btn btn--ghost btn--sm" disabled={busy} onClick={() => discover(p.id)} data-tip="发现模型">
                  发现
                </button>
                <button className="btn btn--ghost btn--sm" onClick={() => openEdit(p)}>
                  编辑
                </button>
                <button className="btn btn--danger btn--sm" onClick={() => removeProvider(p.id)}>
                  删除
                </button>
              </div>
            ))}
          </div>
          {remoteModels.length > 0 && (
            <>
              <div className="inspector-sep">可调用远程模型</div>
              <div className="models__list">
                {remoteModels.map((m) => (
                  <div key={m.id} className="model-row">
                    <div className="model-row__info">
                      <b>{m.name}</b>
                      <small>{String(m.base_url || "")}</small>
                    </div>
                    <span className={`badge ${m.available ? "badge--success" : "badge--muted"}`}>
                      {m.available ? "可用" : "不可用"}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </section>

        {/* API 端点 */}
        <section className="card models__section models__section--wide">
          <div className="models__section-head">
            <h2>平台 API</h2>
            <span className={`badge ${system?.api_key_configured ? "badge--success" : "badge--warning"}`}>
              {system?.api_key_configured ? "密钥已配置" : "未配置密钥"}
            </span>
          </div>
          <p className="field-help">
            OpenAI 兼容端点，所有 /v1 请求需携带 <code>Authorization: Bearer &lt;密钥&gt;</code>。
          </p>
          <div className="models__endpoints">
            <div className="endpoint-row">
              <code>{system?.api_base_url || "http://127.0.0.1:7860/v1"}/chat/completions</code>
              <span>对话补全（SSE 流式）</span>
            </div>
            <div className="endpoint-row">
              <code>{system?.api_base_url || "http://127.0.0.1:7860/v1"}/agents/{"{id}"}/runs</code>
              <span>已发布 Agent 运行</span>
            </div>
            <div className="endpoint-row">
              <code>{system?.api_base_url || "http://127.0.0.1:7860/v1"}/models</code>
              <span>模型清单</span>
            </div>
            <div className="endpoint-row">
              <code>{system?.api_base_url || "http://127.0.0.1:7860/v1"}/embeddings</code>
              <span>向量嵌入</span>
            </div>
            <div className="endpoint-row">
              <code>{system?.openapi_url || "http://127.0.0.1:7860/docs"}</code>
              <span>交互式接口文档（Swagger）</span>
            </div>
          </div>
          <div className="models__key-actions">
            <button className="btn btn--secondary btn--sm" onClick={rotateKey}>
              生成新密钥
            </button>
            {system?.api_key_configured && (
              <button
                className="btn btn--danger btn--sm"
                onClick={() =>
                  window.confirm("删除密钥后所有 API 调用将被拒绝，继续？") &&
                  api.deleteApiKey().then(refresh).then(() => toast.success("密钥已删除")).catch((e) => toast.error(String(e)))
                }
              >
                删除密钥
              </button>
            )}
          </div>
        </section>
      </div>

      {/* 服务商编辑弹层 */}
      {editing && (
        <div className="modal-mask" onMouseDown={(e) => e.target === e.currentTarget && setEditing(null)}>
          <div className="modal" role="dialog" aria-modal="true">
            <h3>{editing === "new" ? "添加服务商" : `编辑 · ${editing.name}`}</h3>
            <label className="field">
              <span>名称</span>
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="如：DeepSeek 官方" />
            </label>
            <label className="field">
              <span>Base URL</span>
              <input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
            </label>
            <label className="field">
              <span>API Key{editing !== "new" ? "（留空保持不变）" : ""}</span>
              <input type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} placeholder="sk-…" />
            </label>
            <label className="field">
              <span>模型 ID（每行一个，留空用「发现」自动获取）</span>
              <textarea rows={4} value={form.model_ids} onChange={(e) => setForm({ ...form, model_ids: e.target.value })} />
            </label>
            <div className="modal-actions">
              <button className="btn btn--ghost btn--sm" onClick={() => setEditing(null)}>
                取消
              </button>
              <button className="btn btn--primary btn--sm" disabled={busy} onClick={saveProvider}>
                保存
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* 动力分配表：机械面板上的一排层分布槽 + 关键指标数码管 */
function PlacementTable({ placement }: { placement: Record<string, unknown> }) {
  const layers = (placement.layers as Record<string, number> | undefined) || null;
  const mode = String(placement.mode || "—");
  const dtype = String(placement.dtype || placement.model || "—");
  const engine = placement.engine ? String(placement.engine) : null;
  const gpu = layers?.gpu ?? 0;
  const cpu = layers?.cpu ?? 0;
  const disk = layers?.disk ?? 0;
  const total = layers?.total ?? 0;
  const pct = (n: number) => (total ? Math.round((n / total) * 100) : 0);
  return (
    <div className="placement">
      <div className="placement__head">
        <span className="placement__title">动力分配</span>
        <span className="placement__mode">{engine ? `${engine} · ` : ""}{mode}{dtype !== "—" ? ` · ${dtype}` : ""}</span>
      </div>
      {layers && total > 0 ? (
        <>
          <div className="placement__bar" role="img" aria-label={`GPU ${gpu} 层, CPU ${cpu} 层, 磁盘 ${disk} 层`}>
            {gpu > 0 && <span className="placement__seg placement__seg--gpu" style={{ width: `${pct(gpu)}%` }} data-tip={`GPU · ${gpu} 层`} />}
            {cpu > 0 && <span className="placement__seg placement__seg--cpu" style={{ width: `${pct(cpu)}%` }} data-tip={`CPU · ${cpu} 层`} />}
            {disk > 0 && <span className="placement__seg placement__seg--disk" style={{ width: `${pct(disk)}%` }} data-tip={`磁盘 · ${disk} 层`} />}
          </div>
          <div className="placement__legend">
            <span><i className="placement__dot placement__dot--gpu" />GPU <b className="digit">{gpu}</b> 层</span>
            <span><i className="placement__dot placement__dot--cpu" />CPU <b className="digit">{cpu}</b> 层</span>
            {disk > 0 && <span><i className="placement__dot placement__dot--disk" />磁盘 <b className="digit">{disk}</b> 层</span>}
            <span className="placement__total">共 <b className="digit">{total}</b> 层</span>
          </div>
        </>
      ) : (
        <p className="field-help" style={{ margin: 0 }}>
          {engine === "llama.cpp"
            ? `llama.cpp 自动 GPU 装配${placement.context_size ? ` · 上下文 ${String(placement.context_size)}` : ""}`
            : "单设备运行，无跨层分配。"}
        </p>
      )}
    </div>
  );
}
