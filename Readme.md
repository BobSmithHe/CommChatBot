# CommChatBot

This repo now contains a clean backend implementation under `backend/` and a new frontend under `frontend/`.

The backend does not depend on `wireless_comm_ai/backend`. It uses a MiniCode-PI-inspired architecture:

- `chatbot` mode: optional local knowledge retrieval and web search, then answer generation.
- `coding-agent` mode: durable AgentRuntime loop with workspace file tools, Git checkpoints, approvals, hooks, terminal streaming, cancellation, and task recovery.
- Chat and Coding conversations, prompts, model settings, and tools are isolated.
- Chat owns upload/RAG/web-search controls; Coding owns workspaces and terminals.
- Local SQLite/JSON remain the safe fallback. Redis caches task state, MySQL mirrors task audit records, Milvus mirrors/searches vectors when online, and Langfuse receives runtime events when online.

Run with:

```powershell
conda activate aiagent
cd D:\PyCharm\PythonProject\AIagent\CommChatBot
pip install -r backend/requirements.txt
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8765
```

See `backend/README.md` for details.

Frontend:

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

Open `http://127.0.0.1:5173`.

Docker is intentionally not required. Copy `.env.example` to `.env` and fill secrets locally; `.env` is ignored by Git.
