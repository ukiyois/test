# LLMmodol · Local AI Studio

Windows 本地模型与 Agent 工作流平台。提供本地模型切换、OpenAI 兼容远程服务商配置、聊天和文件上传、OpenAI 风格 API，以及可保存和恢复的 Harness 工作流运行记录。

## 启动

使用当前已安装的 Python 环境运行：

    .\start.ps1

脚本会在后台启动服务并打开 http://127.0.0.1:7860。停止服务：

    .\stop.ps1

如果缺少 Python 包：

    python -m pip install -r requirements.txt

请保留当前可用的 CUDA 版 PyTorch；不要让普通 pip 安装覆盖成 CPU-only 版本。API 文档位于 http://127.0.0.1:7860/docs。

## 本地模型

模型注册表位于 llmplatform/models.py。当前默认接入：

- Qwen3.5-0.8B、Qwen3.5-2B
- Qwen2.5-1.5B-Instruct
- Gemma-3-1B-it、Gemma-4-E2B-it
- Phi-4-mini-instruct
- FLAN-T5-base（工作流文本生成）
- BGE-M3、Qwen3-Embedding-4B（嵌入模型）

GPT-2-small、GPT-2-medium、MobileLLM-R1-950M、Qwen3-Embedding-0.6B、DeepSeek-Coder-1.3B-Instruct、Xmodel-2.5 和两个 m-a-p 实验模型不进入可调用列表。

聊天生成使用 Transformers 与 Accelerate。加载器按当前可用显存设置 GPU 内存上限，并将剩余层放入系统内存；自动分配失败时再尝试全 CPU 加载。模型首次加载耗时较长。所有模型均使用本地文件和 trust_remote_code=False。

Qwen3.8-27B 另走 GGUF/llama.cpp CUDA runtime，以适配 8 GB 显存和 32 GB 系统内存。运行 `setup_qwen_gguf.ps1` 会安装 CUDA 版 llama.cpp，并下载 Q3_K_M 量化权重（约 13.4 GB）；请预留约 16 GB 以上空间和较长下载时间。随后在“模型与 API”页启动混合推理。运行时先使用 4K 上下文，llama.cpp 按可用显存自动选择 GPU 层并保留 1 GiB 显存余量；装载失败会重启为 CPU 推理。服务绑定 `127.0.0.1:8011`，就绪后自动作为 API 服务商注册并出现在聊天与工作流模型列表。当前 GGUF 包不含视觉 projector，图片理解尚未接入。

## 远程模型服务商

进入“模型与 API”，填写服务商名称、API URL 和 API Key。API URL 接受根地址（例如 https://api.example.com/v1），也会清理以 /chat/completions 结尾的地址。保存后可读取 /models，也可以手工输入模型 ID。

API Key 通过 Windows 凭据管理器保存，不写入 SQLite 数据库。每个服务商可以单独更新或删除。

## 外部调用本平台

平台在本机提供 OpenAI Chat Completions 协议：

- Base URL: http://127.0.0.1:7860/v1
- 模型列表: GET /v1/models
- 对话: POST /v1/chat/completions
- 嵌入: POST /v1/embeddings
- Swagger/OpenAPI: http://127.0.0.1:7860/docs

可在“模型与 API”中生成平台 API Key。未启用时服务仍只绑定本机地址；启用后 /v1/* 需要 Authorization: Bearer <key>。

## Agent Harness

Harness 支持 Agent、模型调用、工具、条件分支、文本转换、人工审核和结束节点。执行记录写入 SQLite，每个节点完成后保存运行状态；支持节点重试、最大步数、调用超时、取消、审核暂停和从中断检查点恢复。

内置工具：

- 计算器（安全表达式解析）
- 本次上传文件搜索
- UTC 时间
- HTTP GET/POST（限制访问内网和本机地址）

内置“文件研究员”工作流可上传文档、选择模型、让 Agent 搜索内容并调用工具。上传支持 PDF、DOCX、TXT、Markdown、CSV、JSON、YAML、LOG、PY 和 HTML；单文件上限 15 MB。扫描件 OCR 尚未接入。

工作流可在“Agent 工作流”中编辑并保存；画布显示节点连接与分支，节点可拖动布局。保存时会递增工作流版本；每次运行固定使用启动时的定义快照，后续编辑不会改变等待审批或恢复中的旧运行。连接缺失、不可达节点和无法到达结束节点会在保存前拦截。运行步骤、工具参数、节点耗时和结果在“运行记录”中查看。

画布支持从分支端口直接选择目标节点，也可在属性面板精确编辑连接。可先运行结构检查；发布会固定当前已保存版本，后续修改需要重新发布才影响线上调用。平台 Agent API：

- `GET /v1/agents`：列出已发布 Agent
- `POST /v1/agents/{workflow_id}/runs`：提交 `{ "input": "...", "file_ids": [] }`，返回运行 ID
- `GET /v1/agent-runs/{run_id}`：读取状态、最终结果和节点事件
- `POST /v1/files`：上传文件并取得可传入 `file_ids` 的文件 ID
- `POST /api/workflows/validate`：预检尚未保存的工作流定义

Agent API 使用“模型与 API”页的同一平台 Key；本机服务默认绑定 `127.0.0.1`。运行定义按发布版本存档，不向 Agent 列表接口暴露系统提示词或工具配置。

“运行监控”每 5 秒采集 CPU、系统内存、平台进程内存、NVIDIA GPU 利用率、温度和显存数据；本机保留最近 7 天采样。监控页同时汇总 Harness 状态、成功率、运行耗时和节点耗时。没有可用的 `nvidia-smi` 时，GPU 指标显示为不可用，不会以 0 冒充真实读数。接口：`GET /api/monitoring?window_minutes=60`。

运行状态、事件、工作流版本和文件保存在 data/。

## 运行注意事项

- 该启动器默认只监听 127.0.0.1，不直接对局域网或公网开放。
- 本地模型一次只运行一个，切换模型时会卸载前一个模型。
- 显存紧张时 Accelerate 使用 CPU/GPU 混合分配；无法自动分配时回退到 CPU，速度会降低。
- 不支持图像 OCR 的文件提取；PDF 只有可选中文本层时能够直接检索。
