# CommChatBot Backend

Clean FastAPI backend with two chat modes:

- `rag`: retrieve local knowledge first, then answer.
- `agent`: run a durable MiniCode-style runtime loop: model -> approval/hooks -> tool calls -> tool results -> final answer.

## Architecture

```text
app/
  main.py                 FastAPI entrypoint and HTTP/SSE API
  services.py             ProductGateway for API-facing orchestration
  infra/                  config, SQLite, auth
  packages/
    ai/                   low-level model provider contracts/adapters
    agent/                reusable MiniCode-style AgentRuntime
  products/
    chatbot.py            chatbot workflow: direct RAG and chat-agent mode
    coding_agent.py       coding-agent workflow: tools + runtime prompt
  core/
    rag/                  local hybrid RAG + optional Milvus mirror
    code/                 Python execution capability
```

The runtime mirrors MiniCode-PI's package boundary:

```text
API -> ProductGateway -> ChatbotMode | CodingAgentMode
                     -> packages.agent.AgentRuntime
                     -> packages.ai.ModelProvider
                     -> Product tools (RAG / Web / Python)
```

## Run

```powershell
conda activate aiagent
cd D:\PyCharm\PythonProject\AIagent\CommChatBot
pip install -r backend/requirements.txt
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8765
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

`CHAT_MODEL_ID` and `CODING_MODEL_ID` may be configured independently. Both fall back to `DEEPSEEK_MODEL`.

Mode boundaries:

- `chatbot`: optional local RAG and Tavily web search.
- `coding-agent`: workspace file listing, reading, searching, creation, exact replacement, bounded project verification commands, and temporary-directory Python execution. It never receives RAG or web-search tools.

Runtime safety and recovery:

- Per-conversation `read-only`, `workspace-write`, and `full-access` permission modes.
- Execute/network tools require approval in `workspace-write` mode; configurable hooks can block tool calls.
- Tasks and traces survive process restarts; interrupted tasks can be resumed from the UI.
- Workspace deletion is recoverable, every workspace is a Git repository, and checkpoints/diffs/restores are available.
- Redis task cache, MySQL audit, Milvus vectors, and Langfuse telemetry are optional adapters with local fallback.

## API

- `POST /api/chat/stream` with `{ "message": "...", "mode": "chatbot" | "coding-agent", "use_rag": true, "use_web": false }`
- `POST /api/knowledge/upload`
- `GET /api/knowledge/search?query=...`
- `POST /api/code/execute`
- `/api/auth/*`, `/api/conversations/*`
- `/api/tasks/*`, `/health/integrations`
