# LLMmodol 自强化助手 · 工具清单

聊天页开启"助手"拨杆后，模型通过 tool_calls 调用以下工具读写项目代码并热更新。

## 工具一览

| 工具 | 作用 | 危险级别 | 说明 |
|------|------|----------|------|
| `read_file` | 读取项目任意文件（带行号） | 安全 | 免审批，改代码前必用 |
| `write_file` | 整文件写入 | 危险 | 需审批，自动备份到 data/patch_backups |
| `apply_patch` | search/replace 局部修改 | 危险 | 需审批，search 不匹配则拒绝 |
| `run_command` | 在项目根执行任意命令（ls/find/cat/python/npx 等） | 危险 | 需审批，仅拦截删盘/格式化等系统级操作 |
| `restart_service` | 重启后端/前端服务 | 危险 | 需审批，使代码改动热生效 |
| `search_files` | 关键词检索项目文件 | 安全 | 免审批 |
| `semantic_search` | 语义检索（embedding） | 安全 | 免审批 |
| `calculator` | 数学计算 | 安全 | 免审批 |
| `current_time` | 当前时间 | 安全 | 免审批 |

## 工作准则（模型必读）

1. 涉及读取/修改项目代码时，**必须先调用工具**，禁止凭空回答"我不能"。
2. 改代码前先用 `read_file` 或 `search_files` 读懂现状。
3. 优先 `apply_patch` 局部修改；仅整文件重写才用 `write_file`。
4. 后端 Python 改动后 `restart_service`；前端改动后 `run_command` 执行 `npx vite build`。
5. 危险操作由系统自动弹审批，模型正常发起 tool_call 即可。
6. 路径用相对项目根（如 `llmplatform/app.py`、`studio/src/views/ChatView.tsx`）。

## 运行记录与成本

- 每次对话生成一条影子 run（工作流"对话"），模型调用事件 `model_call_completed` 落入 run_events。
- 监控页统计对话 token 与成本：本地模型记 $0，远程模型按 pricing.py 计价。
- 定价可通过 `GET/POST /api/pricing` 查看与调整；预算 `POST /api/budget`（daily_usd / monthly_usd）。
