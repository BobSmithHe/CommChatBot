# CommChatBot Backend

Clean FastAPI backend with two chat modes:

- `rag`: retrieve local knowledge first, then answer.
- `agent`: run a durable MiniCode-style runtime loop: model -> approval/hooks -> tool calls -> tool results -> final answer.

## Architecture

```text
app/
  main.py                 FastAPI entrypoint and HTTP/SSE API
  bootstrap/container.py  the only application composition root
  services.py             compatibility accessors into the bootstrap container
  infra/                  configuration, token primitives, observability adapters
  providers/              low-level model provider contracts/adapters
  agent_runtime/          portable AgentRuntime Core + host protocol + local/remote SDK
  platform/
    database.py           MySQL/SQLite schema and sessions
    services/             auth, task queue/runtime, Redis events, scheduler, notifications
    agent_host.py         platform adapter for AgentRuntimeHost
  extensions/
    builtin/              vertical extension slices: manifest, activation and implementation
    support/              shared attachment, document, execution and sandbox helpers
  products/
    profiles.py           declarative Chat and Coding capability selection
    chatbot.py            thin Chat profile shell
    coding_agent.py       thin Coding profile shell
```

The runtime mirrors MiniCode-PI's package boundary:

```text
AI Provider -> AgentRuntime Core -> Extension Host API -> Built-in Extensions
                               \-> PlatformAgentRuntimeHost -> Platform Services
ProductGateway -> Chat | Coding Profile -> ExtensionRegistry -> AgentRuntime
```

`agent_runtime` has no imports from FastAPI, the database, Redis, product modes,
or concrete extensions. `providers` does not read application configuration, products
depend on ports and immutable profile configuration, and platform services never import
API routes. The previous catch-all `app/core/`, `app/packages/`, and horizontal
`extensions/capabilities/` directories were removed.

## Run

```powershell
conda activate aiagent
cd D:\PyCharm\PythonProject\AIagent\CommChatBot
pip install -r backend/requirements.txt
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8765
# With AGENT_WORKER_MODE=external, start separately:
cd backend
python -m app.worker
```

Optional `.env`:

```env
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-v4-flash
CHAT_MODEL_ID=deepseek-v4-flash
CODING_MODEL_ID=deepseek-v4-flash
TAVILY_API_KEY=...
```

`CHAT_PROVIDER_ID` / `CODING_PROVIDER_ID` and their model ids may be configured independently. Built-in adapters support OpenAI-compatible APIs (DeepSeek by default) and the native Anthropic Messages API. Additional OpenAI-compatible endpoints can be declared with `MODEL_PROVIDERS_JSON`; `GET /api/models` returns the configured catalog without exposing secrets.

Mode boundaries:

- `chatbot`: optional local RAG and Tavily web search.
- `coding-agent`: workspace file listing, reading, searching, creation, exact replacement, atomic unified-diff patches, automatic LSP/Ruff feedback, diff review, bounded project verification commands, task plans, persistent inspectable read-only child-agent trees, durable per-project memory, and temporary-directory Python execution. It never receives RAG or web-search tools.

Runtime safety and recovery:

- Per-conversation `read-only`, `workspace-write`, and `full-access` permission modes.
- Execute/network tools require approval in `workspace-write` mode; configurable hooks can block tool calls.
- MySQL stores tasks, checkpoints, traces, conversations, and auth state; Redis wakes independent workers while the database remains the durable queue source of truth.
- API/browser disconnects do not cancel work. Running tasks are re-queued and continued from their latest checkpoint after worker restart.
- Runtime checkpoints persist the completed message/tool chain and next turn; resume reuses the original task and workspace.
- Steering and follow-up messages use a durable task mailbox. Steering is injected at the next safe turn boundary; follow-up can extend a task after an intermediate answer without losing the current checkpoint.
- Tool inputs are validated with JSON Schema Draft 2020-12, and structured waiting/running/completed progress events remain paired with their tool call.
- Conversations can be forked at a selected message into independent branches; Coding branches receive a filesystem snapshot and can be restored by switching to that branch.
- `SessionStart`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, and `Stop` hooks cover normal, rejected, cancelled, interrupted, failed, and max-turn paths.
- Consecutive tools marked `parallel` execute concurrently while sequential tools remain ordering barriers.
- Project memory is keyed by sanitized import source/Git identity rather than conversation id, so a newly imported workspace for the same project can reuse stable facts and preferences.
- Workspace deletion is recoverable, every workspace is a Git repository, and checkpoints/diffs/restores are available.
- Each workspace terminal is a persistent PTY: Windows uses the same `node-pty`/ConPTY stack used by VS Code, the browser uses a bidirectional authenticated WebSocket, Ctrl+C interrupts without destroying the shell, and viewport resize updates the PTY dimensions.
- Persistent Pyright provides Python completion, diagnostics, hover, definition, references, and rename; Ruff formats and lint-checks. Monaco also supplies JS/TS, JSON, CSS, and HTML workers.
- Allow-listed local import, HTTPS clone, and Git worktrees are supported. File versions refresh clean tabs and surface dirty-tab conflicts before overwrite.
- Docker mode places code, verification commands, and interactive workspace terminals in non-root containers with no network, a read-only root, one workspace bind mount, dropped capabilities, and memory/CPU/PID/output limits. Windows Job Objects remain the `restricted` fallback.
- Long histories are compacted into a durable rolling summary while recent turns remain verbatim. Near the model limit, the active LLM creates a structured goal/decisions/files/errors/next-steps summary; every call still enforces `MODEL_CONTEXT_TOKENS`.
- Trusted Coding projects discover `AGENTS.md`, declarative `SKILL.md` instructions and JSON extension manifests. Trust is explicit; project content is never imported as executable server-side Python, and extension commands still pass through existing permission and sandbox enforcement.
- Per-call token/cache/cost usage is stored with task and conversation ids and aggregated by `GET /api/tasks/{task_id}`. `agent_runtime.sdk` exposes the in-process session API and `agent_runtime.remote_sdk` provides an async HTTP/SSE client for tasks, steering, cancellation and child-agent control.
- Extension Runtime v2 validates manifests, JSON Schema configuration and declared permissions, supports trusted installed entrypoints, unregister and reverse lifecycle deactivation.
- Local RAG uses conservative intent routing, contextual follow-up query rewriting, and separate lexical/dense relevance thresholds. Greetings, assistant-meta prompts, creative tasks, and real-time questions do not query the knowledge base; weak Milvus nearest-neighbor results are discarded before fusion.
- Short-term memory consists of account-scoped conversation messages, Agent checkpoints/task mailbox state, and durable structured context summaries. Governed long-term memory is stored in MySQL at user/project/conversation/task scope, with optional Milvus semantic recall. A conservative post-answer extractor records confidence, importance, TTL, provenance, revision history, and merges near-duplicates; credential-like content is rejected. Per-account privacy settings control capture, recall, and each scope, while explicit delete/clear operations permanently remove database records.

Authentication safety:

- Short-lived access tokens and one-time rotating refresh tokens stored in a path-scoped HttpOnly cookie; logout and password reset revoke refresh sessions.
- Redis-backed login/password-reset rate limiting with an in-process fallback.
- One-time, expiring password reset tokens and per-user authentication audit events.
- Security headers, request correlation ids, and no-store authentication responses.
- `APP_ENVIRONMENT=production` rejects weak JWT secrets, insecure refresh cookies, debug reset tokens, and wildcard credentialed CORS.
- `PASSWORD_RESET_DEBUG=true` returns a reset token only for direct loopback development. Disable it behind a production proxy and use an out-of-band delivery provider.

Docker sandbox:

```powershell
docker build -t commchatbot-sandbox:0.8.0 backend/sandbox
# .env.runtime: SANDBOX_MODE=docker
```

The container never receives the host's model/database credentials or Docker socket. `SANDBOX_NETWORK=deny` maps to Docker's `--network none`.

## API

- `POST /api/chat/stream` with `{ "message": "...", "mode": "chatbot" | "coding-agent", "use_rag": true, "use_web": false }`
- `POST /api/knowledge/upload`
- `GET /api/knowledge/search?query=...`
- `POST /api/code/execute`
- `/api/auth/login`, `/api/auth/refresh`, `/api/auth/logout`, `/api/auth/password/*`, `/api/auth/audit`
- `/api/conversations/*`
- `POST /api/conversations/{id}/fork`, `GET /api/conversations/{id}/branches`, `POST /api/conversations/{id}/branches/{branch_id}/restore`
- `PATCH /api/conversations/{id}/trust`, `GET /api/conversations/{id}/project-context`
- `POST /api/tasks/{task_id}/messages` for persistent `steering` / `follow_up`
- `GET /api/tasks/{task_id}/subtasks/{subtask_id}`, child `/messages`, and `/interrupt`
- `/api/tasks/*`, `/api/models`, `/health/integrations`
- `GET/PATCH /api/memories/settings`, `GET/POST/DELETE /api/memories`, `PATCH/DELETE /api/memories/{id}`, `GET /api/memories/recall`

Memory deployment defaults:

```env
MEMORY_AUTO_CAPTURE=true
MEMORY_SEMANTIC_RECALL=true
MEMORY_RECALL_LIMIT=12
MEMORY_MIN_CONFIDENCE=0.6
MEMORY_MERGE_SIMILARITY=0.9
MEMORY_MAX_RECORDS_PER_USER=5000
MEMORY_COLLECTION_NAME=commchat_memories
```

MySQL is authoritative. Milvus is a recoverable semantic index: if it is temporarily unavailable, lexical recall continues and later writes retry after a cooldown.
