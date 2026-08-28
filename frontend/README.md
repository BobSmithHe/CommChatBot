# CommChatBot Frontend

Vue 3 + Vite frontend for the clean backend.

## Run

```powershell
cd D:\PyCharm\PythonProject\AIagent\CommChatBot\frontend
npm.cmd install
npm.cmd run dev
```

Open `http://127.0.0.1:5173`.

The dev server proxies `/api` and `/health` to `http://127.0.0.1:8765`.

## UI

- Codex-style three-panel workspace.
- `Chatbot` and `Coding` product-mode switch.
- Mode-specific conversation lists and histories.
- Chat-only RAG and web-search controls.
- Coding Agent workspace file inspection and editing through runtime tools.
- SSE chat streaming with runtime events.
- Knowledge upload/search.
- Python code execution.
