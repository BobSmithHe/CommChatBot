# CommChatBot

CommChatBot 是一个同时提供 **Chat** 与 **Coding Agent** 两种模式的全栈 AI 工作台。两种模式在界面、上下文和工具权限上相互隔离：Chat 面向问答、文件理解、知识库检索和联网搜索；Coding Agent 面向持久工作区、代码编辑、终端执行和可恢复的 Agent 任务。

项目当前版本为 `0.8.0`，前端使用 Vue 3、Vite、Monaco Editor 与 xterm.js，后端使用 FastAPI、SQLAlchemy、MySQL、Redis 和 Milvus。

## 核心能力

### Chat 模式

- 流式 Markdown、代码高亮、公式和代码块运行。
- 从输入框选择本地 RAG 或 Tavily 网络搜索。
- 支持上传文件；普通问候、创作和实时问题不会误触发本地知识库。
- 运行轨迹按 LLM turn 展示，并将思考、工具动作和结果配对折叠。
- Chat 不暴露工作区、终端和 Coding 专属工具。

### Coding Agent 模式

- 每个会话拥有独立且持久的文件工作区。
- VS Code 风格文件树：创建、删除、重命名、拖拽移动文件或目录。
- Monaco 多标签编辑器和语法高亮。
- Python LSP：补全、诊断、格式化、悬停、跳转定义、引用和重命名。
- xterm.js + PTY/ConPTY 交互终端：多终端、输入、调整尺寸、Ctrl+C、Ctrl+Z 和 shell 异常恢复。
- 文件读写、搜索、unified diff/patch、命令运行、测试、Lint 和变更复核工具。
- Git checkpoint、diff、恢复、工作区快照和会话分支。
- 任务规划、只读子任务委派、并行工具、生命周期 Hook、权限审批和取消。
- API 或浏览器断开不会自动终止任务；Worker 重启后可从 checkpoint 继续。
- Coding Agent 不使用 Chat 的 RAG 和 Web Search 工具。

### AgentRuntime

- 一致的 `message_start → message_update → message_end` 流式消息身份。
- assistant tool-call 和 ToolResult 分阶段 checkpoint，降低副作用工具重复执行风险。
- 恢复 checkpoint 时可选择仅恢复，或追加新的用户消息继续执行。
- 普通工具异常、命令非零退出码、超时和终端丢失都会作为错误结果返回模型，而不是让 Runtime 崩溃。
- 支持串行/并行工具、JSON Schema 参数校验和结构化进度事件。
- 支持 `SessionStart`、`PreToolUse`、`PermissionRequest`、`PostToolUse`、`Stop` Hook。
- durable steering/follow-up 消息队列，可在安全的 turn 边界追加任务指令。
- 取消任务和终端 Ctrl+C 使用不同协议，避免 cancelled 与 500 error 同时出现。

### 上下文与记忆

系统使用分层记忆：

- **短期记忆**：会话消息、Agent checkpoint、任务邮箱和结构化上下文摘要。
- **长期记忆**：MySQL 持久化的用户、项目、会话、任务四种作用域记忆。
- **语义召回**：Milvus 保存可重建的向量索引，MySQL 始终是事实来源；Milvus 暂时不可用时仍可词法召回。
- **自动治理**：回答完成后保守提取长期事实，记录置信度、重要度、TTL、来源、访问次数和版本历史，并合并近似重复项。
- **隐私控制**：用户可以关闭总开关、自动提取、语义召回或单独作用域；密钥、Token、密码和私钥等内容会被拒绝存储。
- **真正删除**：手动删除、清空和 TTL 到期都会物理删除 MySQL 记录，并尽力同步删除 Milvus 向量。

前端账号区域的“记忆管理”面板支持查询、筛选、新建、编辑、删除和清空。

### 安全与可观测性

- 短期访问令牌和一次性轮换 Refresh Token。
- 注册、登录和密码重置限流，以及登录审计。
- 密码重置会撤销旧 Refresh Session。
- Docker 沙箱以非 root 用户运行，默认无网络、只读根文件系统、仅挂载工作区，并限制 CPU、内存、进程数和输出量。
- Langfuse 记录任务事件；Token、缓存 Token 和成本统计按任务与会话持久化。
- 项目级 `AGENTS.md`、`SKILL.md` 和声明式扩展需要显式信任，不会作为服务端 Python 代码直接加载。

## 架构

```text
Vue 3 / Vite / Monaco / xterm.js
                 │ HTTP + SSE + WebSocket
                 ▼
FastAPI ── ProductGateway ── ChatbotMode
   │                         └─ RAG / Web Search
   │
   ├─ Agent task queue ── Redis ── Agent Worker
   │                                  └─ AgentRuntime / Tools / Hooks
   ├─ MySQL: users, conversations, tasks, checkpoints, memory, usage
   ├─ Milvus: knowledge and long-term-memory vector indexes
   ├─ Langfuse: observability
   └─ Docker sandbox: code execution and workspace terminal
```

主要目录：

```text
backend/
  app/api/        FastAPI 路由与请求模型
  app/core/       任务队列、上下文、记忆、RAG、沙箱、工作区和终端
  app/infra/      配置、数据库、认证与外部集成
  app/packages/   独立的 AgentRuntime、Provider 和 SDK
  app/products/   Chat 与 Coding Agent 产品编排
  sandbox/        Coding Docker 沙箱镜像
  tests/          单元与后端集成测试
frontend/
  src/            Vue 应用、Agent 轨迹、编辑器和终端组件
  e2e/            Playwright 端到端测试
start.sh          Windows Git Bash 一键启动脚本
```

## 环境要求

- Windows 10/11；终端在 Windows 上使用 ConPTY。后端也保留 Unix PTY fallback。
- Conda 环境 `aiagent`，Python 3.11 或更高版本。
- Node.js 18 或更高版本。
- Docker Desktop。
- MySQL 8、Redis、Milvus；Langfuse 为可选但推荐的观测服务。

## 配置

复制示例配置：

```powershell
Copy-Item .env.example .env
```

至少填写数据库、Redis 和模型 Provider 配置：

```env
DEEPSEEK_API_KEY=your-key
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
CHAT_PROVIDER_ID=deepseek
CODING_PROVIDER_ID=deepseek
CHAT_MODEL_ID=deepseek-v4-flash
CODING_MODEL_ID=deepseek-v4-flash

DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your-password
DB_NAME=wireless_comm_ai

REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=your-password

MILVUS_URI=http://localhost:19530
SANDBOX_MODE=docker
```

Chat 和 Coding 可以选择不同 Provider。内置 DeepSeek/OpenAI-compatible 与 Anthropic Messages API 适配器，也可以使用 `MODEL_PROVIDERS_JSON` 声明其他 OpenAI-compatible 服务。完整选项见 [.env.example](.env.example)。

> 不要提交 `.env`。如果密钥曾出现在聊天记录、日志或 Git 历史中，请立即在对应平台轮换。

## 启动

### 一键启动（推荐）

在 **Windows Git Bash** 中运行：

```bash
./start.sh start
./start.sh status
```

其他命令：

```bash
./start.sh restart
./start.sh stop
```

脚本会检查 Docker 和沙箱镜像、安装缺失的前端依赖，并启动 API、Agent Worker 和 Vite。PID 与日志分别保存在 `data/run/` 和 `data/logs/`。

### 手动启动

首次安装：

```powershell
conda activate aiagent
pip install -r backend/requirements.txt
Set-Location frontend
npm.cmd install
Set-Location ..
docker build -t commchatbot-sandbox:0.8.0 backend/sandbox
```

分别在三个终端运行：

```powershell
# API
conda run --no-capture-output -n aiagent python -m uvicorn app.main:app `
  --app-dir backend --host 127.0.0.1 --port 8765

# Agent Worker
Set-Location backend
conda run --no-capture-output -n aiagent python -m app.worker

# Frontend
Set-Location frontend
npm.cmd run dev
```

访问：

- 前端：http://127.0.0.1:5173
- API 健康检查：http://127.0.0.1:8765/health
- 集成状态：http://127.0.0.1:8765/health/integrations

## 测试

```powershell
Set-Location backend
conda run -n aiagent python -m pytest -q

Set-Location ..\frontend
npm.cmd run build
npm.cmd run test:e2e
```

当前基线：

- 后端：`85 passed`
- 前端生产构建：通过
- Playwright E2E：`10 passed`

测试覆盖流式响应、工具调用、并行失败、审批拒绝、取消、checkpoint 恢复、终端退出码/超时/Ctrl+C/Ctrl+Z、输出 cursor、Docker 终端、会话切换、Refresh Token、编辑器标签和记忆管理。

## 常用 API

- `POST /api/chat/stream`：创建 Chat 或 Coding Agent 流式任务。
- `/api/conversations/*`：会话、工作区、分支、项目上下文和终端。
- `/api/tasks/*`：任务状态、事件、取消、审批和 steering/follow-up。
- `/api/knowledge/*`：知识文件上传、搜索、删除和清空。
- `/api/memories/*`：记忆设置、查询、召回、创建、修改、删除和清空。
- `/api/auth/*`：登录、刷新、登出、密码重置和审计。
- `GET /api/models`：可用 Provider/模型目录，不返回密钥。

更多后端细节见 [backend/README.md](backend/README.md)。

## 已知限制

- 首次语义检索需要加载本地 Embedding 模型，会产生一次冷启动延迟。
- 自动长期记忆提取会增加一次较小的模型请求，并可能遗漏表达不明确的信息；用户可在记忆面板校正或关闭该功能。
- Milvus 是可恢复索引，目前没有前端全量重建按钮。
- 生产环境应额外启用数据库磁盘/备份加密、反向代理 TLS，并关闭 `PASSWORD_RESET_DEBUG` 与匿名访问。

## License

仓库目前未声明开源许可证。在添加许可证前，默认保留所有权利。
