# CommChatBot

CommChatBot 是一个同时提供 **Chat** 与 **Coding Agent** 两种模式的全栈 AI 工作台。两种模式在界面、上下文和工具权限上相互隔离：Chat 面向问答、文件理解、知识库检索和联网搜索；Coding Agent 面向持久工作区、代码编辑、终端执行和可恢复的 Agent 任务。

项目当前版本为 `0.8.0`，前端使用 Vue 3、Vite、Monaco Editor 与 xterm.js，后端使用 FastAPI、SQLAlchemy、MySQL、Redis 和 Milvus。

完整功能、实现机制、默认容量参数和实测指标见 [功能与指标总览](docs/FUNCTIONS_AND_METRICS.md)。

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
- Redis Streams 实时事件总线与短周期批量数据库落盘；本地 SQLite outbox 在数据库短暂故障或进程重启后自动重放，Redis 故障时客户端回退到数据库事件流。
- 子 Agent 任务拥有持久化父子关系、状态、进度和结果，运行中与重新打开会话后均可查看。
- 取消任务和终端 Ctrl+C 使用不同协议，避免 cancelled 与 500 error 同时出现。

### 上下文与记忆

系统使用分层记忆：

- **短期记忆**：会话消息、Agent checkpoint、任务邮箱和结构化上下文摘要。
- **长期记忆**：MySQL 持久化的用户、项目、会话、任务四种作用域记忆。
- **语义召回**：Milvus 保存可重建的向量索引，MySQL 始终是事实来源；Milvus 暂时不可用时仍可词法召回。
- **自动治理**：回答先返回，长期记忆提取由独立耐久队列和 Worker 异步完成；记录置信度、重要度、TTL、来源、访问次数和版本历史，并合并近似重复项。
- **隐私控制**：用户可以关闭总开关、自动提取、语义召回或单独作用域；密钥、Token、密码和私钥等内容会被拒绝存储。
- **真正删除**：手动删除、清空和 TTL 到期都会物理删除 MySQL 记录，并尽力同步删除 Milvus 向量。

前端账号区域的“记忆管理”面板支持查询、筛选、新建、编辑、删除和清空。

### 安全与可观测性

- 短期访问令牌和一次性轮换 Refresh Token；Refresh Token 只保存在限定路径的 HttpOnly Cookie，旧版 localStorage 凭据会被一次性迁移并删除。
- 注册、登录和密码重置限流，以及登录审计。
- 密码重置会撤销旧 Refresh Session。
- API 返回安全响应头和可追踪的 `X-Request-ID`；认证响应禁止缓存。
- Docker 沙箱以非 root 用户运行，默认无网络、只读根文件系统、仅挂载工作区，并限制 CPU、内存、进程数和输出量。
- Langfuse 记录任务事件；Token、缓存 Token 和成本统计按任务与会话持久化。
- 项目级 `AGENTS.md`、`SKILL.md` 和声明式扩展需要显式信任，不会作为服务端 Python 代码直接加载。

### MCP、Skills 与平台集成

- MCP Client 支持 stdio 与 Streamable HTTP server，发现并调用 tools、resources 和 prompts；连接按工作区复用、空闲回收并在断线后重建，HTTP server 可配置 OAuth 2.0 Client Credentials。项目配置放在 `.commchat/mcp.json`，也可用 `MCP_CONFIG_PATH` 指定全局配置。
- Skill Runtime 采用渐进加载：上下文只注入名称与描述，Agent 按需读取完整 `SKILL.md`、资源文件，并经现有沙箱与审批执行 `scripts/`。
- GitHub 工具支持 PR 列表、创建 PR、Check Runs、整单及行级 Review；签名 Webhook 可把订阅仓库事件写入通知与外部投递队列；仓库自带 GitHub Actions 后端/前端 CI。
- 定时任务复用耐久 Agent 队列，兼容固定间隔和带 IANA 时区的 RFC 5545 RRULE；通知持久化在应用内，并可通过签名 Webhook 或 SMTP 邮件异步重试投递。
- 远程 Runner 使用带租约令牌的心跳、拉取、续租、取消和完成回报协议；Runner 崩溃后过期任务可重新领取，旧租约不能覆盖新执行结果。参考 Runner 默认在无网络且有 CPU/内存/PID 限制的 Docker 容器内执行。

## 架构

```text
AI Provider
     │
     ▼
AgentRuntime Core (app/agent_runtime)
     ├─ message / stream / checkpoint / context compaction
     ├─ tool execution / permission / cancellation
     ├─ hook lifecycle
     └─ extension host API ───────────────────────────────┐
                                                          ▼
Built-in Extensions (app/extensions)              Product Profiles
     ├─ workspace / git / terminal                ├─ Chat profile
     ├─ plan / memory / subagents                 └─ Coding profile
     ├─ diagnostics / LSP
     └─ RAG / web / MCP / GitHub / remote execution

Platform (app/platform)
     ├─ auth / database
     ├─ task queue / Redis event bus
     ├─ scheduler / notifications
     └─ AgentRuntimeHost adapter

FastAPI ProductGateway selects a profile; the profile activates extensions;
extensions contribute tools, hooks, prompt context and events to AgentRuntime.
All concrete object construction is centralized in `app/bootstrap/container.py`.
```

依赖方向是单向的：`AgentRuntime Core` 不导入 FastAPI、数据库、Redis、产品模式或具体扩展；持久化、审批、Hook、任务邮箱和取消由 `AgentRuntimeHost` 协议注入。旧的 `app/core/` 杂糅目录已经移除。

主要目录：

```text
backend/
  app/api/        FastAPI 路由与请求模型
  app/agent_runtime/ 独立 AgentRuntime Core、Extension Host API 与 SDK
  app/providers/    多 Provider 协议与适配器
  app/bootstrap/       唯一依赖组合入口与服务容器
  app/extensions/builtin/      按功能纵向组织的内置扩展及其实现
  app/extensions/support/      文档、附件、沙箱等共享基础能力
  app/platform/          数据库与 AgentRuntimeHost 平台适配器
  app/platform/services/ auth、任务、事件、调度、通知和会话上下文
  app/infra/      配置、凭据签发与外部可观测性适配
  app/products/   Chat 与 Coding Agent 产品编排
  sandbox/        Coding Docker 沙箱镜像
  tests/          单元与后端集成测试
  scripts/        并发压测、Agent eval 和参考远程 Runner
  evals/          JSONL Agent 评测集
frontend/
  src/            Vue 应用、Agent 轨迹、编辑器和终端组件
  e2e/            Playwright 端到端测试
start.sh          Windows Git Bash 一键启动脚本
```

### 扩展 Profile

AgentRuntime 只维护消息、工具执行、Hook、权限、checkpoint、恢复和取消语义。Chat/Coding 产品通过声明式 Profile 选择扩展，当前目录可通过 `GET /api/extensions` 查询。

Extension Runtime v2 会验证 Manifest、配置 JSON Schema 和工具权限声明，支持可信安装根目录发现、卸载以及反向 `deactivate`。Coding 子代理提供 `spawn_agent`、`send_agent`、`wait_agent`、`interrupt_agent`、`list_agents`，任务树、消息邮箱和 checkpoint 均可持久化。`app.agent_runtime.RemoteAgentClient` 提供 HTTP/SSE 远程 SDK。

全局可使用逗号分隔的 `+id`/`-id` 调整默认 Profile：

```dotenv
CHAT_EXTENSIONS=-chat.web-search
CODING_EXTENSIONS=-coding.github,-coding.remote-execution
```

如果配置中不使用 `+`/`-`，则视为完整替换列表。受信任项目还可在 `.commchat/settings.json` 中覆盖：

```json
{
  "agent_extensions": ["-coding.remote-execution", "-coding.github"]
}
```

项目配置只能选择服务端已注册的扩展；项目自身的可执行能力仍必须通过声明式扩展、Skill 或 MCP 提供，不会直接加载任意 Python 模块。

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

# Optional integrations
GITHUB_TOKEN=github-token
GITHUB_WEBHOOK_SECRET=strong-webhook-secret
MCP_CONFIG_PATH=/path/to/mcp.json
REMOTE_RUNNER_TOKEN=strong-shared-runner-token
```

HTTP MCP 的 OAuth Client Credentials 可写入 `.commchat/mcp.json`：

```json
{
  "mcpServers": {
    "internal": {
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "token_url": "https://id.example.com/oauth/token",
        "client_id": "${MCP_CLIENT_ID}",
        "client_secret": "${MCP_CLIENT_SECRET}",
        "scope": "mcp.read mcp.write"
      }
    }
  }
}
```

参考远程 Runner 默认使用 Docker 隔离：

```bash
python backend/scripts/remote_runner.py --url http://server:8765 --workdir /srv/project
```

仅受信任的开发机需要直接宿主执行时，才显式使用 `--mode local`，并在服务端设置 `REMOTE_REQUIRE_ISOLATION=false`。

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

运行真实前后端 E2E、并发压测和 Agent eval：

```powershell
Set-Location frontend
npm.cmd run test:e2e:live

Set-Location ..\backend
python scripts/load_test.py --requests 500 --concurrency 40
python scripts/agent_eval.py evals/smoke.jsonl
```

当前基线：

- 后端：`114 passed`
- 前端生产构建：通过
- Playwright E2E：`12 passed`，另有 `1` 个连接真实 API 的测试按需运行

CI 还会实际启动 FastAPI 与 Vite，运行真实浏览器全链路测试，并以失败数、RPS 和 P95 延迟作为健康接口的并发性能门槛。

测试覆盖流式响应、工具调用、并行失败、审批拒绝、取消、checkpoint 恢复、终端退出码/超时/Ctrl+C/Ctrl+Z、输出 cursor、Docker 终端、会话切换、Refresh Token、编辑器标签和记忆管理。

## 常用 API

- `POST /api/chat/stream`：创建 Chat 或 Coding Agent 流式任务。
- `/api/conversations/*`：会话、工作区、分支、项目上下文和终端。
- `/api/tasks/*`：任务状态、事件、取消、审批和 steering/follow-up。
- `/api/knowledge/*`：知识文件上传、搜索、删除和清空。
- `/api/memories/*`：记忆设置、查询、召回、创建、修改、删除和清空。
- `/api/platform/automations/*`：定时任务创建、更新、立即运行和删除。
- `/api/platform/notifications/*`：持久化通知查询与已读状态。
- `/api/platform/notification-endpoints/*`：Webhook/Email 通知目标与投递状态。
- `/api/platform/github/*`：仓库事件订阅、签名 Webhook 与行级 Review。
- `/api/platform/remote/*`：远程 Runner 心跳、任务拉取和结果回报协议。
- `/api/auth/*`：登录、刷新、登出、密码重置和审计。
- `GET /api/models`：可用 Provider/模型目录，不返回密钥。
- `GET /health` / `GET /health/ready`：进程存活与数据库就绪探针。

更多后端细节见 [backend/README.md](backend/README.md)。

## 已知限制

- 首次语义检索需要加载本地 Embedding 模型，会产生一次冷启动延迟。
- 自动长期记忆提取在后台执行，可能短暂延迟或遗漏表达不明确的信息；用户可在记忆面板校正或关闭该功能。
- Milvus 是可恢复索引，目前没有前端全量重建按钮。
- MCP server 进程与远程 Runner 都是外部资源；checkpoint 会保留 Agent/文件状态，但不会把进程内协议会话或 shell 状态持久化，重启后会按配置新建连接。
- 外部通知内置通用 Webhook 与 SMTP Email；移动系统原生推送尚需经 Webhook 接入第三方网关。
- 远程 Runner 保证命令执行隔离，但当前不自动复制本地未提交文件；远端工作目录应由部署系统预置或从 Git 同步。
- 生产环境应额外启用数据库磁盘/备份加密和反向代理 TLS。设置 `APP_ENVIRONMENT=production` 后，后端会拒绝弱 JWT 密钥、不安全 Refresh Cookie、调试密码重置或通配 CORS 配置。

## License

仓库目前未声明开源许可证。在添加许可证前，默认保留所有权利。
