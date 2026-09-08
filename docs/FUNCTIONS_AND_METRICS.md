# CommChatBot 功能、实现方式与指标总览

更新日期：2026-09-08　适用版本：0.8.0

本文描述当前仓库已经实现并通过测试的能力。配置指标均为默认值；性能指标来自本地隔离 SQLite 全栈实测，不等同于生产 MySQL、Redis、Milvus 和模型服务的容量承诺。

## 1. 产品形态

| 能力 | 实现方式 | 当前指标/边界 |
|---|---|---|
| Chat 模式 | `ProductGateway → ChatbotMode → Chat Profile`，只激活 RAG、Web Search、Chat Memory 扩展 | 与 Coding 工具集合隔离 |
| Coding Agent 模式 | 每个会话绑定独立持久工作区，激活 workspace、diagnostics、git、terminal、project-context、MCP、GitHub、remote、plan、memory、subagents 扩展 | 默认最多 8 个 Agent turn，可配置 |
| 模式切换 | 前端在固定居中位置选择模式；会话、运行状态、工作区和工具权限按模式保存 | 切换会话不会取消后台任务 |
| 流式交互 | HTTP SSE 返回状态、思考、工具、审批、消息 delta、结束和错误事件 | assistant message 从 start 到 end 使用同一 message id |

Chat 的联网搜索和知识库检索只能从输入框显式选择或经保守意图路由触发；Coding Agent 默认不获得这两个工具。Chat 支持附件和代码块运行，但不创建工作区；Coding Agent 使用工作区、编辑器和终端，但不显示 Chat 知识库板块。

## 2. AgentRuntime Core

核心位于 `backend/app/agent_runtime/`，不依赖 FastAPI、数据库、Redis、产品模式或具体扩展。

### 运行循环

1. 将 system、历史消息和新用户输入装配为模型上下文。
2. 在接近 Token 上限时执行结构化压缩。
3. 调用 Provider，并以同一消息身份流式发送 delta。
4. 对工具参数执行 JSON Schema Draft 2020-12 校验。
5. 运行 PreToolUse、PermissionRequest、PostToolUse 等 Hook。
6. 按工具声明串行或并行执行；普通异常转换为 `ToolResult(is_error=True)`。
7. 写入工具观察结果、保存 checkpoint，再进入下一轮模型调用。
8. 生成最终回答，执行 Stop Hook，并记录 Token/缓存 Token/成本。

### 一致性和恢复

- checkpoint 包含消息、下一 turn 和 `before_llm`、`tools_pending`、`after_tools` 阶段。
- assistant tool-call 写入后、工具执行前保存一次；ToolResult 写入后再保存一次，降低写文件、删除、提交和外部写操作被重复执行的风险。
- resume 支持“只恢复 checkpoint”和“恢复后追加新用户消息”两种语义，不重复加入旧 prompt。
- steering/follow-up 使用持久消息邮箱，在安全 turn 边界消费。
- 任务取消与终端 interrupt 是两套协议：前者结束 Agent，后者只中断终端前台程序。
- API/SSE 断开不自动取消任务；Worker 重启可重新领取 durable task。

### 扩展机制

- Extension Runtime v2 校验 manifest、依赖、配置 Schema、模式和权限声明。
- 扩展可贡献工具、上下文、Hook 和事件，并支持逆序 deactivate。
- Chat Profile 默认 3 个扩展：`chat.rag`、`chat.web-search`、`chat.memory`。
- Coding Profile 默认 11 个扩展：workspace、diagnostics、git、terminal、project-context、MCP、GitHub、remote-execution、plan、memory、subagents。
- 所有具体对象由 `backend/app/bootstrap/container.py` 组合；旧模块级导出只是惰性兼容代理，导入模块不会启动连接、PTY 或线程。

## 3. Coding 工作区与编辑器

| 功能 | 实现方式 |
|---|---|
| 工作区隔离 | 每个 Coding 会话映射到独立目录；所有路径先 resolve，再校验不得越出根目录 |
| 文件树 | 递归层级结构，支持创建、删除、恢复、重命名和拖拽移动文件/目录 |
| 删除恢复 | 删除项先进入工作区 trash，可列出和恢复 |
| 编辑器 | Monaco Editor，多标签、脏状态、版本冲突提示、按扩展名选择语言和语法高亮 |
| Patch | 支持 unified diff apply，并校验 patch 中所有路径仍位于工作区 |
| 变更复核 | Git diff 加未跟踪文件 diff；写入和 patch 工具返回结构化 diff |
| Git | status、diff、checkpoint commit、分支列表/切换、单文件恢复和会话 fork worktree |
| 项目导入 | 受允许根目录约束的本地导入，以及 HTTPS Git clone |

基础 Coding 工具为：`list_files`、`read_file`、`search_files`、`write_file`、`replace_in_file`、`apply_patch`、`get_diagnostics`、`review_changes`、`run_command`、`execute_python`。扩展激活后还会追加 Git、终端、MCP、GitHub、记忆、计划、远程执行和子 Agent 工具。

## 4. LSP 与代码质量闭环

- 后端维护按工作区复用的 LSP session；当前 Python 使用 Pyright，并保留 Jedi fallback。
- 前端 Monaco 接入补全、诊断、格式化、定义、悬停、引用和重命名 API。
- Agent 的写入、替换和 patch 工具会收集变更后的诊断并作为反馈返回模型。
- `review_changes`、`run_command`、`execute_python` 支持修改后自检；命令非零退出码作为工具失败观察，而不是 Runtime crash。

## 5. PTY/ConPTY 终端

- Windows 使用 node-pty/ConPTY bridge，Unix 使用 PTY fallback，Docker 模式在已有 sandbox 容器机制内运行。
- 支持多个终端、WebSocket 双向输入、resize、Ctrl+C、Ctrl+Z/EOF、关闭、输出 cursor 和增量读取。
- `execute()` 是一次性 shell command API，返回 output、exit code、cwd、timeout 和 restart 状态；REPL、vim、top 等交互程序使用 write/read/interrupt/resize。
- Ctrl+C 异常杀死 bridge 时可恢复 shell，并以 `restarted=true` 暴露状态；原 shell 的环境变量、虚拟环境和 cwd 不被视为 durable state。
- 输出缓冲上限为 200,000 字符，截断时通过 base offset 保持 cursor 正确。
- 每工作区默认最多 4 个终端；Agent checkpoint 不依赖 terminal id 永久存在。

## 6. RAG、文件理解与 Web Search

### RAG

- 上传文档先解析、分块并记录来源；默认 chunk 900 字符、overlap 150。
- MySQL/本地索引保存文档元数据，Milvus 保存可重建的 dense vector。
- 默认 Embedding 为 `BAAI/bge-large-zh-v1.5`，维度 1024，可使用 CPU/GPU。
- 检索组合词法和 dense 分数，默认 dense 阈值 0.42、词法阈值 0.15、超时 12 秒。
- 意图路由识别问候、创作、助手元问题和实时问题；这些输入不会因弱相似度误触发知识库。
- Milvus 不可用时保留词法回退；MySQL 是事实来源，向量索引可重建。

### Web Search

- 通过 Tavily 扩展提供实时搜索，只存在于 Chat Profile。
- 搜索结果作为带来源的上下文进入模型，不直接混入 Coding 工作区权限。

### Chat 附件

- 上传文件与用户、会话关联，解析后作为本轮上下文。
- 附件存储与 Coding 工作区分离，避免 Chat 请求错误访问 workspace API。

## 7. 长短期记忆与上下文压缩

| 层级 | 持久化与用途 |
|---|---|
| 短期 | 会话消息、当前 Agent checkpoint、任务/子任务邮箱、最近原文消息 |
| 会话摘要 | 长上下文结构化为目标、决策、文件、错误、下一步，并持久化滚动摘要 |
| 长期 | 用户、项目、会话、任务四种 scope 的事实和偏好记录 |
| 语义镜像 | Milvus 向量用于召回，数据库记录保持权威性 |

- 默认模型上下文 32,768 Token，输出预算 4,096 Token；压缩触发比例 0.78。
- 自动记忆提取位于回答关键路径之外，由 durable memory queue 和独立 Worker 处理。
- 记录 confidence、importance、TTL、来源、访问次数和版本历史；相似项按默认 0.9 阈值合并。
- 每用户默认最多 5,000 条长期记忆，单次默认召回 12 条。
- 密钥、Token、密码、私钥等凭据样式拒绝写入；用户可关闭捕获、召回或具体 scope。
- 删除、清空和 TTL 到期执行数据库物理删除，并尽力同步删除向量。

## 8. 多 Agent、计划与项目上下文

- Plan 扩展维护结构化任务列表、状态和进度事件。
- 子 Agent 支持 spawn、send、wait、interrupt、list；父子关系、请求、checkpoint、结果和错误均持久化。
- 前端运行轨迹显示持久任务树，重新打开会话仍可查看。
- 可信项目发现 `AGENTS.md`、`.commchat/settings.json`、`SKILL.md` 和声明式扩展清单。
- Skills 采用渐进加载：初始只注入名称/描述，模型按需读取完整说明和资源。
- 项目内容不会作为任意服务端 Python 插件直接 import；脚本仍经过权限和沙箱协议。

## 9. Provider、模型与可观测性

- 内置 DeepSeek/OpenAI-compatible Provider 和 Anthropic Messages Provider。
- Chat 与 Coding 可分别配置 Provider 和 model id；额外 OpenAI-compatible 服务可用 `MODEL_PROVIDERS_JSON` 注册。
- 支持 streaming、tools、reasoning content 和 usage metadata。
- 每次调用记录 input/output/cache Token、Provider、模型、任务、会话和估算成本。
- Langfuse 记录任务级 trace/span；观测服务不可用不会阻断主任务。
- `agent_runtime.sdk` 提供进程内 SDK，`RemoteAgentClient` 提供任务、SSE、steering、取消和子 Agent 的异步远程 SDK。

## 10. 账号与安全

- 注册、登录、当前用户、登出、密码重置和登录审计 API。
- Access Token 默认 30 分钟，只保存在前端内存；刷新时自动重试一次并合并并发 refresh 请求。
- Refresh Token 默认 30 天、一次性轮换，只存放在 `Path=/api/auth` 的 HttpOnly Cookie；旧 localStorage Token 会被迁移后删除。
- 登录、注册、密码重置使用 Redis 限流，并有进程内 fallback。默认 60 秒窗口：注册 6 次、登录 8 次、密码重置 4 次。
- 密码重置撤销该用户全部 Refresh Session；登录、刷新、登出和重置写审计记录。
- API 设置 nosniff、DENY frame、no-referrer、Permissions-Policy、认证 no-store 以及 `X-Request-ID`。
- `APP_ENVIRONMENT=production` 会拒绝弱 JWT secret、非 Secure Refresh Cookie、调试重置 Token 和 credentialed wildcard CORS。
- Docker sandbox 默认非 root、只读根文件系统、无网络、drop capabilities，仅挂载工作区，并限制 CPU、内存、PID 和输出。

## 11. 平台服务与外部集成

### Durable task 与事件

- Agent Task、审批、checkpoint、会话、消息、子任务和 mailbox 存入数据库。
- Redis Streams 负责低延迟实时事件；数据库保存 durable history。
- SQLite outbox 在数据库失败前先记录 event key，成功落库后删除；重启自动重放。
- event key 唯一索引保证 API/Worker 多进程重放幂等；失败采用指数退避并限制日志频率。
- Redis 失效时 SSE 客户端回退到数据库事件轮询。

### 审批与 Hook

- permission mode 支持 read-only、workspace-write 和 full-access。
- 需要执行权限的工具可进入 approval_required；前端在输入区上方允许/拒绝。
- SessionStart、PreToolUse、PermissionRequest、PostToolUse 和 Stop Hook 支持管理员配置 argv 命令。

### MCP

- 支持 stdio 与 Streamable HTTP，发现 tools、resources、prompts。
- HTTP 支持 OAuth 2.0 Client Credentials；连接按工作区复用、空闲回收、断线重建。

### GitHub

- 支持 PR 列表/创建、Check Runs、整单 Review、行级 Review。
- Webhook 校验 HMAC 签名；订阅事件进入持久通知与投递队列。

### 定时任务和通知

- 自动任务支持固定间隔和带 IANA 时区的 RFC 5545 RRULE，复用 durable Agent queue。
- 通知支持应用内消息、签名 Webhook 和 SMTP，默认最多重试 6 次并采用退避。

### 远程执行

- Runner 使用共享认证、在线心跳和 pull-based job 协议。
- 每次 claim 签发独立 lease token；默认 45 秒租约、10 秒续租、最多 3 次领取。
- 过期任务可重新领取；旧租约无法 heartbeat 或覆盖新 Runner 的完成结果。
- 取消和 timeout 会设置机器可识别状态；参考 Runner 可停止对应 Docker container。
- 服务端默认拒绝未声明 isolated capability 的 Runner。

## 12. 前端实现

- Vue 3 Composition API，运行轨迹按 turn 组织为 Thought → Action → Observation → Answer。
- 工具参数、结果、超长输出、thinking 和每轮轨迹可折叠；编辑操作直接显示 unified diff。
- Coding 布局由可拖拽 splitter 调整侧栏、对话、编辑器和终端尺寸。
- Monaco、xterm、文件树按需加载；Chat 首屏不会下载 Coding 编辑器和终端主体。
- 会话列表独立滚动，固定行高，长标题省略且保留删除操作。
- 网络请求统一处理认证、Cookie、错误结构、SSE 和并发 refresh。

## 13. API 面

- `/api/auth/*`：注册、登录、刷新、登出、密码重置、审计。
- `/api/chat/stream`、`/api/chat/attachments`：两种产品模式入口和 Chat 附件。
- `/api/conversations/*`：会话、归档、fork、分支恢复、信任、权限、工作区、LSP、Git 和终端。
- `/api/tasks/*`：状态、事件、取消、resume、审批、steering/follow-up 和子任务树。
- `/api/knowledge/*`、`/api/memories/*`：知识库与分层记忆治理。
- `/api/platform/*`：自动任务、通知、通知端点、远程 Runner 和 GitHub。
- `/api/models`、`/api/extensions`：Provider/模型和 Extension/Profile 目录。
- `/health`、`/health/ready`、`/health/integrations`：存活、数据库就绪和外部依赖状态。

## 14. 已验证指标

| 项目 | 结果 |
|---|---:|
| 后端 pytest | 114 passed |
| Ruff CI 门槛 | `F,E9` 全部通过 |
| Mock Playwright | 12 passed，1 个 live case 按需跳过 |
| 真实 FastAPI + Vite + Chromium E2E | 1 passed |
| 300 请求、并发 30 的 `/health` 探测 | 0 failures |
| 实测吞吐 | 771.4 RPS |
| 实测平均延迟 | 35.97 ms |
| 实测 P50 / P95 / P99 | 29.10 / 97.84 / 137.45 ms |
| 前端初始 JS | 89.71 KB，gzip 29.79 KB |
| 延迟加载终端 chunk | 295.01 KB，gzip 74.14 KB |
| 延迟加载 Monaco 主 chunk | 2,728.60 KB，gzip 711.06 KB |
| TypeScript Worker | 7,043.07 KB，仅打开相关编辑能力时加载 |
| 终端输出缓冲 | 200,000 字符 |
| 默认数据库连接池 | 10，max overflow 20 |
| 默认 Agent Worker 并发 | 8 |
| 默认 Memory Worker 并发 | 2 |
| 默认事件 batch / flush | 50 条 / 100 ms |
| 默认 Redis event stream 上限 | 5,000 条/任务 |

性能数据的测试环境是本机回环网络、SQLite、无模型调用的健康接口。真实 Agent 吞吐主要取决于模型延迟、工具耗时、数据库、Redis、Milvus、Embedding 冷启动和 Docker 创建成本。

## 15. 当前已知限制

1. 部分旧数据库升级仍依赖幂等的手写 schema migration，尚未完全迁移到 Alembic 一类版本化迁移框架。
2. 远程执行是 at-least-once 语义。租约显著降低重复，但 Runner 在产生外部副作用后、回报成功前崩溃时，任意外部系统无法获得通用 exactly-once 保证。
3. PTY、MCP 进程和 shell 环境是 ephemeral resource；checkpoint 保留文件与 Agent 状态，不恢复原进程内会话。
4. Milvus 是可重建索引，目前缺少前端一键全量重建入口。
5. Monaco 和 TypeScript Worker 延迟加载后不影响 Chat 首屏，但首次打开对应 Coding 功能仍有下载和初始化成本。
6. 当前 CI 的真实浏览器链路使用 SQLite；MySQL、Redis、Milvus 和 Docker 的组合仍应增加专门的服务矩阵测试。
7. 自动记忆提取是异步启发式过程，可能短暂延迟或漏掉表达不明确的信息，用户需要通过记忆管理面板校正。

## 16. 生产建议

- 立即轮换任何曾出现在聊天、日志或 Git 历史中的 Provider、Tavily、Langfuse 和数据库密钥。
- 设置 `APP_ENVIRONMENT=production`、强随机 JWT secret、Secure Cookie、精确 CORS origin，并在反向代理终止 TLS。
- 使用 MySQL 持久卷和备份、Redis 持久化/高可用、Milvus 持久卷，以及独立 Agent/Memory Worker。
- Agent 自动 shell 使用 Docker sandbox；不要把 cwd 跟踪当作安全边界。
- 对写操作工具设计业务幂等键；远程任务也应尽量运行可重试命令。
- 持续观察模型 Token/成本、任务失败率、审批等待、队列深度、outbox backlog、P95/P99 和 Docker 冷启动时间。
