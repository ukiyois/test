/* 节点设置面板：按节点类型渲染配置表单（迁入自 workflow-ui） */

import { useEffect, useState } from "react";
import type { ModelInfo, ToolInfo, Workflow, WorkflowDef, WorkflowNode } from "@/api/types";
import { NODE_META, TOOL_LABELS, routeKeys, routeTarget, withRouteTarget } from "./graph";

interface InspectorProps {
  node: WorkflowNode;
  definition: WorkflowDef;
  models: ModelInfo[];
  tools: ToolInfo[];
  workflows: Workflow[];
  onChange: (values: Partial<WorkflowNode>) => void;
}

const OPERATORS: [string, string][] = [
  ["contains", "包含文字"],
  ["not_contains", "不包含文字"],
  ["equals", "完全相同"],
  ["not_equals", "不相同"],
  ["starts_with", "以…开头"],
  ["ends_with", "以…结尾"],
  ["regex", "正则匹配"],
  ["gt", "大于"],
  ["gte", "大于等于"],
  ["lt", "小于"],
  ["lte", "小于等于"],
  ["is_empty", "没有内容"],
  ["is_not_empty", "有内容"],
];

function JsonObjectEditor({
  label,
  value,
  onChange,
  rows = 5,
  placeholder = "{}",
}: {
  label: React.ReactNode;
  value: unknown;
  onChange: (v: Record<string, unknown> | null) => void;
  rows?: number;
  placeholder?: string;
}) {
  const [text, setText] = useState(value == null ? "" : JSON.stringify(value, null, 2));
  const [error, setError] = useState("");
  useEffect(() => setText(value == null ? "" : JSON.stringify(value, null, 2)), [value]);
  return (
    <label className="field">
      <span>{label}</span>
      <textarea
        className="code-area"
        rows={rows}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          if (!text.trim()) {
            onChange(null);
            setError("");
            return;
          }
          try {
            const parsed = JSON.parse(text);
            if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error();
            onChange(parsed);
            setError("");
          } catch {
            setError("JSON 格式有误，需填写对象。");
          }
        }}
        placeholder={placeholder}
      />
      <small className={error ? "field-error" : ""}>{error || "填写 JSON 对象；留空使用默认设置。"}</small>
    </label>
  );
}

function ToolArgumentFields({
  toolId,
  tools,
  value,
  onChange,
}: {
  toolId: string;
  tools: ToolInfo[];
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
}) {
  const schema = (tools.find((t) => t.id === toolId)?.parameters as Record<string, unknown>) || {};
  const properties = (schema.properties as Record<string, Record<string, unknown>>) || {};
  const required = new Set((schema.required as string[]) || []);
  const labels: Record<string, string> = {
    expression: "计算表达式",
    query: "搜索内容",
    model: "向量模型",
    url: "请求 URL",
    method: "请求方式",
    headers: "请求头",
    json: "JSON 请求体",
  };
  const values = value || {};
  const setValue = (key: string, nextValue: unknown) => {
    const next = { ...values };
    if (nextValue === "" || nextValue == null) delete next[key];
    else next[key] = nextValue as string;
    onChange(next);
  };
  if (!Object.keys(properties).length) return <small className="field-help">此工具无需输入参数。</small>;
  return (
    <div className="tool-args">
      {Object.entries(properties).map(([key, property]) => {
        const label = (
          <>
            {(property.title as string) || labels[key] || key}
            {required.has(key) ? <em className="req">必填</em> : null}
          </>
        );
        if (property.type === "object")
          return (
            <JsonObjectEditor
              key={key}
              label={label}
              value={values[key] || {}}
              rows={3}
              onChange={(next) => setValue(key, next || {})}
            />
          );
        if (Array.isArray(property.enum))
          return (
            <label className="field" key={key}>
              <span>{label}</span>
              <select value={(values[key] as string) ?? ""} onChange={(e) => setValue(key, e.target.value)}>
                <option value="">使用默认值</option>
                {(property.enum as string[]).map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
              {property.description ? <small>{String(property.description)}</small> : null}
            </label>
          );
        const numeric = ["integer", "number"].includes(property.type as string);
        return (
          <label className="field" key={key}>
            <span>{label}</span>
            <input
              type="text"
              inputMode={numeric ? "decimal" : undefined}
              value={(values[key] as string) ?? ""}
              onChange={(e) => {
                const next = e.target.value;
                const parsed = Number(next);
                setValue(key, numeric && next.trim() !== "" && Number.isFinite(parsed) ? parsed : next);
              }}
              placeholder={numeric ? "数值或 {{节点ID.字段}}" : "可使用 {{input}} 等模板变量"}
            />
            {property.description ? <small>{String(property.description)}</small> : null}
          </label>
        );
      })}
    </div>
  );
}

function ParallelBranchEditor({
  node,
  models,
  tools,
  onChange,
}: {
  node: WorkflowNode;
  models: { id: string; name: string }[];
  tools: ToolInfo[];
  onChange: (v: Partial<WorkflowNode>) => void;
}) {
  const branches = (node.branches as Record<string, unknown>[]) || [];
  const branchTypes: [string, string][] = [
    ["agent", "Agent"],
    ["llm", "模型调用"],
    ["tool", "工具"],
    ["transform", "文本整理"],
  ];
  const branchLabels = Object.fromEntries(branchTypes);
  const update = (index: number, patch: Record<string, unknown>) =>
    onChange({ branches: branches.map((b, i) => (i === index ? { ...b, ...patch } : b)) });
  const setType = (index: number, type: string) => {
    const current = branches[index];
    const defaults =
      type === "tool"
        ? { tool: "calculator", arguments: {} }
        : type === "transform"
          ? { template: "{{last_output}}" }
          : { system_prompt: "", max_tokens: 512, temperature: 0.3, retries: 0 };
    update(index, {
      type,
      ...defaults,
      ...(type === "agent" ? { tools: current.tools || ["search_files", "calculator"] } : {}),
    });
  };
  const add = () => {
    if (branches.length >= 8) return;
    onChange({
      branches: [
        ...branches,
        {
          id: `branch_${Date.now().toString(36)}`,
          label: `分支 ${String.fromCharCode(65 + branches.length)}`,
          type: "transform",
          template: "{{last_output}}",
          retries: 0,
          timeout: 600,
        },
      ],
    });
  };

  return (
    <div className="branch-editor">
      <div className="branch-editor__head">
        <div>
          <b>并行工作分支</b>
          <small>每个分支独立读取上游状态，完成后汇总到同一个对象。</small>
        </div>
        <span>{branches.length} / 8</span>
      </div>
      {branches.map((branch, index) => (
        <article className="branch-card" key={index}>
          <header>
            <span className="branch-card__idx">{String(index + 1).padStart(2, "0")}</span>
            <div>
              <b>{(branch.label as string) || `分支 ${index + 1}`}</b>
              <small>
                {branchLabels[branch.type as string] || "未选择类型"} · {(branch.id as string) || "缺少 ID"}
              </small>
            </div>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              disabled={branches.length <= 2}
              onClick={() => onChange({ branches: branches.filter((_, i) => i !== index) })}
            >
              移除
            </button>
          </header>
          <div className="branch-card__row">
            <label className="field">
              <span>分支 ID</span>
              <input value={(branch.id as string) || ""} onChange={(e) => update(index, { id: e.target.value })} />
            </label>
            <label className="field">
              <span>显示名称</span>
              <input value={(branch.label as string) || ""} onChange={(e) => update(index, { label: e.target.value })} />
            </label>
            <label className="field">
              <span>执行类型</span>
              <select value={(branch.type as string) || "transform"} onChange={(e) => setType(index, e.target.value)}>
                {branchTypes.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {(branch.type === "agent" || branch.type === "llm") && (
            <>
              <label className="field">
                <span>使用模型</span>
                <select value={(branch.model as string) || ""} onChange={(e) => update(index, { model: e.target.value })}>
                  <option value="">继承工作流默认模型</option>
                  {models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>分支指令</span>
                <textarea
                  rows={3}
                  value={(branch.system_prompt as string) || ""}
                  onChange={(e) => update(index, { system_prompt: e.target.value })}
                  placeholder="说明该分支需要独立完成的子任务"
                />
              </label>
              {branch.type === "agent" && (
                <div className="field">
                  <span>允许调用的工具</span>
                  <div className="tool-list">
                    {tools.map((tool) => (
                      <label key={tool.id} className="tool-option">
                        <input
                          type="checkbox"
                          checked={((branch.tools as string[]) || []).includes(tool.id)}
                          onChange={(e) => {
                            const allowed = new Set((branch.tools as string[]) || []);
                            if (e.target.checked) allowed.add(tool.id);
                            else allowed.delete(tool.id);
                            update(index, { tools: [...allowed] });
                          }}
                        />
                        <span>
                          <b>{TOOL_LABELS[tool.id] || tool.id}</b>
                          <small>{tool.description || "注册工具"}</small>
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
              )}
              <div className="branch-card__row">
                <label className="field">
                  <span>最大输出 Token</span>
                  <input
                    type="number"
                    min={32}
                    max={8192}
                    value={(branch.max_tokens as number) ?? 512}
                    onChange={(e) => update(index, { max_tokens: Number(e.target.value) })}
                  />
                </label>
                <label className="field">
                  <span>随机度</span>
                  <input
                    type="number"
                    min={0}
                    max={2}
                    step={0.1}
                    value={(branch.temperature as number) ?? 0.3}
                    onChange={(e) => update(index, { temperature: Number(e.target.value) })}
                  />
                </label>
              </div>
            </>
          )}
          {branch.type === "tool" && (
            <>
              <label className="field">
                <span>执行工具</span>
                <select
                  value={(branch.tool as string) || "calculator"}
                  onChange={(e) => update(index, { tool: e.target.value, arguments: {} })}
                >
                  {tools.map((tool) => (
                    <option key={tool.id} value={tool.id}>
                      {TOOL_LABELS[tool.id] || tool.id}
                    </option>
                  ))}
                </select>
              </label>
              <ToolArgumentFields
                toolId={(branch.tool as string) || "calculator"}
                tools={tools}
                value={(branch.arguments as Record<string, unknown>) || {}}
                onChange={(argumentsValue) => update(index, { arguments: argumentsValue })}
              />
            </>
          )}
          {branch.type === "transform" && (
            <label className="field">
              <span>结果模板</span>
              <textarea
                className="code-area"
                rows={4}
                value={(branch.template as string) || "{{last_output}}"}
                onChange={(e) => update(index, { template: e.target.value })}
              />
            </label>
          )}
        </article>
      ))}
      <button type="button" className="btn btn--secondary btn--sm" disabled={branches.length >= 8} onClick={add}>
        ＋ 添加工作分支
      </button>
      <small className="field-help">分支 2–8 个；执行后汇总所有分支结果，后续节点可按分支 ID 取用单项结果。</small>
    </div>
  );
}

export function NodeInspector({ node, definition, models, tools, workflows, onChange }: InspectorProps) {
  const [schemaText, setSchemaText] = useState(
    node.output_schema == null ? "" : JSON.stringify(node.output_schema, null, 2),
  );
  const [schemaError, setSchemaError] = useState("");
  useEffect(
    () => setSchemaText(node.output_schema == null ? "" : JSON.stringify(node.output_schema, null, 2)),
    [node.id, node.output_schema],
  );
  useEffect(() => setSchemaError(""), [node.id]);

  const modelOptions = models.map((m) => ({
    id:
      m.source === "remote"
        ? `remote/${m.provider_id}/${m.remote_model_id}`
        : `local/${m.id}`,
    name: m.name || m.id,
  }));

  const field = (label: string, key: string, help = "") => (
    <label className="field" key={key}>
      <span>{label}</span>
      <input value={(node[key] as string) ?? ""} onChange={(e) => onChange({ [key]: e.target.value })} />
      {help && <small>{help}</small>}
    </label>
  );
  const numberField = (label: string, key: string, min: number, max: number, step = 1) => (
    <label className="field" key={key}>
      <span>{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={(node[key] as number) ?? 0}
        onChange={(e) => onChange({ [key]: Number(e.target.value) })}
      />
    </label>
  );
  const modelSelect = (
    <label className="field">
      <span>使用模型</span>
      <select
        value={(node.model as string) || definition.model || ""}
        onChange={(e) => onChange({ model: e.target.value })}
      >
        <option value="">继承工作流默认模型</option>
        {modelOptions.map((m) => (
          <option key={m.id} value={m.id}>
            {m.name}
          </option>
        ))}
      </select>
      <small>切换本地或远程模型。远程服务的 URL 和 Key 在模型页管理。</small>
    </label>
  );
  const variableSelect = (
    <label className="field">
      <span>检查输出</span>
      <select value={(node.variable as string) || "last_output"} onChange={(e) => onChange({ variable: e.target.value })}>
        <option value="last_output">上一步输出</option>
        <option value="last_error">最近一次错误</option>
        <option value="input">用户输入</option>
        <option value="result">工作流结果</option>
        {definition.nodes
          .filter((item) => item.id !== node.id)
          .map((item) => (
            <option key={item.id} value={item.id}>
              {item.label || item.id} · 节点输出
            </option>
          ))}
      </select>
    </label>
  );
  const connectionSelect = (key: string, label: string) => (
    <label className={`field route-target${key === "error_next" ? " route-target--error" : ""}`} key={key}>
      <span>{label}</span>
      <select
        value={routeTarget(node, key) || ""}
        onChange={(e) => {
          const target = e.target.value || null;
          onChange(
            key.startsWith("branch:")
              ? { branches: withRouteTarget(node, key, target).branches as WorkflowNode["branches"] }
              : { [key]: target },
          );
        }}
      >
        <option value="">未连接</option>
        {definition.nodes
          .filter((item) => item.id !== node.id)
          .map((item) => (
            <option key={item.id} value={item.id}>
              {item.label || item.id} · {NODE_META[item.type]?.title}
            </option>
          ))}
      </select>
    </label>
  );

  return (
    <div className="inspector-form">
      <div className="inspector-intro">
        <span className="inspector-icon" style={{ color: `var(--node-${NODE_META[node.type]?.color || "tool"})` }}>
          {NODE_META[node.type]?.icon}
        </span>
        <div>
          <b>{node.label || NODE_META[node.type]?.title}</b>
          <small>
            {NODE_META[node.type]?.title} · {node.id}
          </small>
        </div>
      </div>
      {field("显示名称", "label")}

      {(node.type === "agent" || node.type === "llm") && (
        <>
          <div className="inspector-sep">模型设置</div>
          {modelSelect}
          <label className="field">
            <span>系统提示词</span>
            <textarea
              rows={5}
              value={(node.system_prompt as string) || ""}
              onChange={(e) => onChange({ system_prompt: e.target.value })}
              placeholder="角色、目标、规则和回答格式"
            />
          </label>
          <div className="field-row">
            {numberField("最大输出 Token", "max_tokens", 32, 8192)}
            {numberField("随机度", "temperature", 0, 2, 0.1)}
          </div>
          {node.type === "agent" && (
            <>
              <div className="inspector-sep">可用工具</div>
              <div className="tool-list">
                {tools.map((tool) => (
                  <label key={tool.id} className="tool-option">
                    <input
                      type="checkbox"
                      checked={((node.tools as string[]) || []).includes(tool.id)}
                      onChange={(e) => {
                        const current = new Set((node.tools as string[]) || []);
                        if (e.target.checked) current.add(tool.id);
                        else current.delete(tool.id);
                        onChange({ tools: [...current] });
                      }}
                    />
                    <span>
                      <b>{TOOL_LABELS[tool.id] || tool.id}</b>
                      <small>{tool.description || "注册工具"}</small>
                    </span>
                  </label>
                ))}
              </div>
              <div className="field-row">
                {numberField("工具调用轮数", "max_iterations", 1, 8)}
                {numberField("失败重试", "retries", 0, 5)}
              </div>
            </>
          )}
          <div className="field-row">
            {numberField("超时秒数", "timeout", 1, 3600)}
            {node.type === "llm" && numberField("失败重试", "retries", 0, 5)}
          </div>
          <div className="inspector-sep">结构化输出</div>
          <label className="field">
            <span>JSON Schema</span>
            <textarea
              className="code-area"
              rows={7}
              value={schemaText}
              onChange={(e) => setSchemaText(e.target.value)}
              onBlur={() => {
                try {
                  onChange({ output_schema: schemaText.trim() ? JSON.parse(schemaText) : null });
                  setSchemaError("");
                } catch {
                  setSchemaError("JSON 格式有误，请修正后再保存。");
                }
              }}
              placeholder='{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]}'
            />
            <small className={schemaError ? "field-error" : ""}>
              {schemaError || "留空返回普通文本；填写后会解析并校验 JSON。"}
            </small>
          </label>
        </>
      )}

      {node.type === "tool" && (
        <>
          <div className="inspector-sep">工具配置</div>
          <label className="field">
            <span>工具</span>
            <select
              value={(node.tool as string) || "calculator"}
              onChange={(e) => onChange({ tool: e.target.value, arguments: {} })}
            >
              {tools.map((tool) => (
                <option key={tool.id} value={tool.id}>
                  {TOOL_LABELS[tool.id] || tool.id}
                </option>
              ))}
            </select>
          </label>
          <div className="inspector-sep">参数</div>
          <ToolArgumentFields
            toolId={(node.tool as string) || "calculator"}
            tools={tools}
            value={(node.arguments as Record<string, unknown>) || {}}
            onChange={(argumentsValue) => onChange({ arguments: argumentsValue })}
          />
          <div className="field-row">
            {numberField("失败重试", "retries", 0, 5)}
            {numberField("超时秒数", "timeout", 1, 3600)}
          </div>
          <small className="field-help">
            支持 {"{{input}}"}、{"{{last_output}}"}，也可用 `节点ID.字段.子字段` 取值。
          </small>
        </>
      )}

      {node.type === "transform" && (
        <>
          <div className="inspector-sep">结果模板</div>
          <label className="field">
            <span>输出内容</span>
            <textarea
              className="code-area"
              rows={6}
              value={(node.template as string) || "{{last_output}}"}
              onChange={(e) => onChange({ template: e.target.value })}
            />
          </label>
          <small className="field-help">支持内置变量与 {"{{节点ID.字段.子字段}}"}。</small>
        </>
      )}

      {node.type === "condition" && (
        <>
          <div className="inspector-sep">分支判断</div>
          {variableSelect}
          <label className="field">
            <span>字段路径</span>
            <input
              value={(node.variable_path as string) || ""}
              onChange={(e) => onChange({ variable_path: e.target.value })}
              placeholder="score 或 items[0].id"
            />
          </label>
          <label className="field">
            <span>判断方式</span>
            <select value={(node.operator as string) || "contains"} onChange={(e) => onChange({ operator: e.target.value })}>
              {OPERATORS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          {!["is_empty", "is_not_empty"].includes((node.operator as string) || "contains") && (
            <label className="field">
              <span>匹配内容</span>
              <input value={(node.value as string) || ""} onChange={(e) => onChange({ value: e.target.value })} />
            </label>
          )}
        </>
      )}

      {node.type === "loop" && (
        <>
          <div className="inspector-sep">循环停止条件</div>
          {variableSelect}
          <label className="field">
            <span>字段路径</span>
            <input
              value={(node.variable_path as string) || ""}
              onChange={(e) => onChange({ variable_path: e.target.value })}
              placeholder="status 或 items[0].done"
            />
          </label>
          <label className="field">
            <span>继续循环条件</span>
            <select
              value={(node.operator as string) || "is_not_empty"}
              onChange={(e) => onChange({ operator: e.target.value })}
            >
              {OPERATORS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          {!["is_empty", "is_not_empty"].includes((node.operator as string) || "is_not_empty") && (
            <label className="field">
              <span>比较内容</span>
              <input value={(node.value as string) || ""} onChange={(e) => onChange({ value: e.target.value })} />
            </label>
          )}
          {numberField("最多循环轮数", "max_iterations", 1, 50)}
          <small className="field-help">循环体完成后回连到本节点重新判断；达到上限后自动走"完成"出口。</small>
        </>
      )}

      {node.type === "parallel" && (
        <>
          <div className="inspector-sep">并发设置</div>
          {numberField("最大并发分支", "max_concurrency", 1, 8)}
          <ParallelBranchEditor node={node} models={modelOptions} tools={tools} onChange={onChange} />
        </>
      )}

      {node.type === "fanout" && (
        <>
          <div className="inspector-sep">图级并发分流</div>
          <p className="field-help">
            分支在同一张画布中运行完整路径，各自持有状态，全部到达配对汇合节点后主流程继续。
          </p>
          <div className="field-row">
            {numberField("最大并发路径", "max_concurrency", 1, 8)}
            <div className="field">
              <span>配对汇合节点</span>
              <div className="pair-id">{(node.fanin_id as string) || "缺少配对节点"}</div>
            </div>
          </div>
          <div className="branch-editor">
            {((node.branches as Record<string, unknown>[]) || []).map((branch, index) => (
              <div className="branch-card__row" key={(branch.id as string) || index}>
                <span className="branch-card__idx">{String(index + 1).padStart(2, "0")}</span>
                <label className="field">
                  <span>分支 ID</span>
                  <input
                    value={(branch.id as string) || ""}
                    onChange={(e) =>
                      onChange({
                        branches: (node.branches as Record<string, unknown>[]).map((b, i) =>
                          i === index ? { ...b, id: e.target.value } : b,
                        ),
                      })
                    }
                  />
                </label>
                <label className="field">
                  <span>显示名称</span>
                  <input
                    value={(branch.label as string) || ""}
                    onChange={(e) =>
                      onChange({
                        branches: (node.branches as Record<string, unknown>[]).map((b, i) =>
                          i === index ? { ...b, label: e.target.value } : b,
                        ),
                      })
                    }
                  />
                </label>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  disabled={((node.branches as unknown[]) || []).length <= 2}
                  onClick={() =>
                    onChange({ branches: (node.branches as unknown[]).filter((_, i) => i !== index) })
                  }
                >
                  移除
                </button>
              </div>
            ))}
            <button
              type="button"
              className="btn btn--secondary btn--sm"
              disabled={((node.branches as unknown[]) || []).length >= 8}
              onClick={() =>
                onChange({
                  branches: [
                    ...((node.branches as unknown[]) || []),
                    {
                      id: `branch_${Date.now().toString(36)}`,
                      label: `分支 ${((node.branches as unknown[]) || []).length + 1}`,
                      next: null,
                    },
                  ],
                })
              }
            >
              ＋ 添加路径
            </button>
          </div>
        </>
      )}

      {node.type === "fanin" && (
        <>
          <div className="inspector-sep">图级并发汇合</div>
          <p className="field-help">
            对应分流：<code>{(node.fanout_id as string) || "未配对"}</code>
          </p>
          <label className="field">
            <span>分支失败时</span>
            <select
              value={(node.failure_policy as string) || "fail"}
              onChange={(e) => onChange({ failure_policy: e.target.value })}
            >
              <option value="fail">失败即停止主流程</option>
              <option value="continue">保留失败详情并继续</option>
            </select>
          </label>
        </>
      )}

      {node.type === "subworkflow" && (
        <>
          <div className="inspector-sep">调用已发布 Agent</div>
          <label className="field">
            <span>子 Agent</span>
            <select
              value={(node.workflow_id as string) || ""}
              onChange={(e) => {
                const selected = workflows.find((w) => w.id === e.target.value);
                onChange({
                  workflow_id: selected?.id || null,
                  workflow_name: selected?.name || "",
                  workflow_version: selected?.published_version || null,
                });
              }}
            >
              <option value="">选择一个已发布 Agent</option>
              {workflows
                .filter((w) => (w.published_version ?? 0) > 0)
                .map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name} · 已发布 v{w.published_version}
                  </option>
                ))}
            </select>
            <small>执行时固定使用选定的发布版本，子运行可在运行记录中单独打开。</small>
          </label>
          <label className="field">
            <span>传给子 Agent 的任务</span>
            <textarea
              rows={4}
              value={(node.input_template as string) || "{{last_output}}"}
              onChange={(e) => onChange({ input_template: e.target.value })}
            />
          </label>
        </>
      )}

      {node.type === "approval" && (
        <>
          <div className="inspector-sep">人工审核</div>
          <label className="field">
            <span>给审核人的说明</span>
            <textarea
              rows={4}
              value={(node.message as string) || ""}
              onChange={(e) => onChange({ message: e.target.value })}
              placeholder="告诉审核人检查哪些内容"
            />
          </label>
        </>
      )}

      {node.type === "start" && <p className="field-help">接收任务文本与上传文件，放入工作流状态。</p>}
      {node.type === "end" && <p className="field-help">保存前一个节点的输出，作为 Agent 最终结果返回。</p>}

      {routeKeys(node).length > 0 && (
        <>
          <div className="inspector-sep">连接目标</div>
          <div className="route-list">{routeKeys(node).map(([key, label]) => connectionSelect(key, label))}</div>
          {routeKeys(node).some(([key]) => key === "error_next") && (
            <small className="field-help">
              错误出口在节点重试耗尽后触发；目标节点可用 {"{{error}}"} 读取失败信息。
            </small>
          )}
        </>
      )}
      <p className="inspector-footnote">节点设置保存在当前草稿；发布后 API 固定运行已发布版本。</p>
    </div>
  );
}
