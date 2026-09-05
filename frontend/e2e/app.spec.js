import { expect, test } from "@playwright/test";


async function mockApplication(page, options = {}) {
  const conversations = [...(options.conversations || [])];
  const chatPayloads = [];
  const terminalMessages = [];
  let nextId = 10;
  let refreshCalls = 0;
  const workspaceFiles = options.workspaceFiles || {};
  const memories = [...(options.memories || [])];
  let memorySettings = {
    enabled: true, auto_capture: true, semantic_recall: true,
    user_scope: true, project_scope: true, conversation_scope: true, task_scope: true,
  };
  await page.routeWebSocket(/\/workspace\/terminals\/[^/]+\/ws/, (socket) => {
    socket.onMessage((raw) => {
      try {
        const message = JSON.parse(String(raw));
        terminalMessages.push(message);
        if (message.type === "auth") {
          socket.send(JSON.stringify({ type: "ready", terminal: { id: "term-1", cursor: 0, backend: "docker" } }));
          if (options.terminalRestart) {
            socket.send(JSON.stringify({
              type: "terminal_restarted",
              terminal: { id: "term-1", cursor: 0, restarted: true, restart_count: 1, restart_reason: "ctrl_c" },
            }));
          }
        }
      } catch {
        // The terminal mock only needs to keep the client connection local.
      }
    });
  });
  await page.route("**/health", (route) => route.fulfill({ json: { status: "ok", version: "e2e" } }));
  await page.route(/^http:\/\/127\.0\.0\.1:\d+\/api\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/auth/refresh") {
      refreshCalls += 1;
      return route.fulfill({ json: { access_token: "fresh-access", refresh_token: "fresh-refresh" } });
    }
    if (path === "/api/auth/me") {
      const authorization = request.headers().authorization || "";
      if (options.expiredSession && authorization.includes("expired-access")) {
        return route.fulfill({ status: 401, json: { detail: "expired" } });
      }
      return route.fulfill({ json: { id: 2, username: options.username || "e2e-user", email: "e2e@example.test" } });
    }
    if (path === "/api/memories/settings" && method === "GET") {
      return route.fulfill({ json: memorySettings });
    }
    if (path === "/api/memories/settings" && method === "PATCH") {
      memorySettings = { ...memorySettings, ...request.postDataJSON() };
      return route.fulfill({ json: memorySettings });
    }
    if (path === "/api/memories" && method === "GET") {
      const scope = url.searchParams.get("scope");
      const query = (url.searchParams.get("query") || "").toLowerCase();
      return route.fulfill({ json: { memories: memories.filter((item) => (
        (!scope || item.scope === scope) && (!query || `${item.key} ${item.value}`.toLowerCase().includes(query))
      )) } });
    }
    if (path === "/api/memories" && method === "POST") {
      const body = request.postDataJSON();
      const item = {
        ...body, id: `memory-${nextId++}`, revision: 1, confidence: body.confidence ?? 1,
        access_count: 0, source_type: "manual",
      };
      memories.unshift(item);
      return route.fulfill({ json: item });
    }
    if (/^\/api\/memories\/[^/]+$/.test(path) && method === "PATCH") {
      const id = path.split("/").pop();
      const index = memories.findIndex((item) => item.id === id);
      memories[index] = { ...memories[index], ...request.postDataJSON(), revision: memories[index].revision + 1 };
      return route.fulfill({ json: memories[index] });
    }
    if (/^\/api\/memories\/[^/]+$/.test(path) && method === "DELETE") {
      const id = path.split("/").pop();
      const index = memories.findIndex((item) => item.id === id);
      if (index >= 0) memories.splice(index, 1);
      return route.fulfill({ json: { status: "deleted" } });
    }
    if (path === "/api/conversations" && method === "GET") {
      const mode = url.searchParams.get("mode");
      return route.fulfill({ json: conversations.filter((item) => item.mode === mode) });
    }
    if (path === "/api/conversations" && method === "POST") {
      const body = request.postDataJSON();
      const item = { id: nextId++, mode: body.mode, title: "New Conversation", permission_mode: "workspace-write" };
      conversations.unshift(item);
      return route.fulfill({ json: item });
    }
    if (/^\/api\/conversations\/\d+$/.test(path)) {
      const id = Number(path.split("/").pop());
      const item = conversations.find((candidate) => candidate.id === id);
      return route.fulfill({ json: { ...item, messages: [] } });
    }
    if (path.endsWith("/workspace/files")) return route.fulfill({
      json: {
        workspace_id: "e2e-workspace",
        entries: Object.entries(workspaceFiles).map(([filePath, content]) => ({
          path: filePath, type: "file", version: `e2e-${content.length}`,
        })),
      },
    });
    if (path.endsWith("/workspace/file") && method === "GET") {
      const filePath = url.searchParams.get("path");
      const content = workspaceFiles[filePath] || "";
      return route.fulfill({ json: { path: filePath, content, version: `e2e-${content.length}` } });
    }
    if (path.includes("/workspace/language/diagnostics")) return route.fulfill({ json: { items: [] } });
    if (path.includes("/workspace/language/completions")) return route.fulfill({ json: { items: [] } });
    if (path.includes("/workspace/language/definition")) return route.fulfill({ json: { items: [] } });
    if (path.includes("/workspace/language/format")) {
      const body = request.postDataJSON();
      return route.fulfill({ json: { content: body.content.replace("value=1", "value = 1") } });
    }
    if (path.endsWith("/workspace/git/status")) return route.fulfill({ json: { is_repo: true, branch: "main", changes: [] } });
    if (path.endsWith("/workspace/trash")) return route.fulfill({ json: { items: [] } });
    if (path.endsWith("/workspace/terminals") && method === "GET") return route.fulfill({ json: { terminals: [] } });
    if (path.endsWith("/workspace/terminals") && method === "POST") {
      return route.fulfill({ json: { id: "term-1", title: "Terminal 1", cwd: ".", output: "" } });
    }
    if (path === "/api/tasks") return route.fulfill({ json: options.tasks || [] });
    if (/^\/api\/tasks\/[^/]+$/.test(path) && method === "GET") {
      const taskId = path.split("/").pop();
      return route.fulfill({ json: options.taskDetails?.[taskId] || {} });
    }
    if (path === "/api/code/execute") return route.fulfill({ json: { stdout: "hello from code\n", stderr: "", exit_code: 0 } });
    if (path === "/api/chat/stream") {
      chatPayloads.push(request.postDataJSON());
      if (options.chatDelayMs) await new Promise((resolve) => setTimeout(resolve, options.chatDelayMs));
      const frames = options.chatFrames || [
        { event: "task", content: { task_id: "a".repeat(32), status: "running" } },
        { event: "turn_start", content: { index: 0, resumed: false } },
        { event: "status", content: "Generating answer" },
        { event: "llm_response", content: { turn: 0, text: "我将给出一个示例。", tool_calls: [], finish_reason: "stop" } },
        { event: "result", content: "Context ready" },
        { event: "answer", content: "结果如下：\n```python\nprint('hello')\n```" },
        { event: "turn_end", content: { index: 0, tool_calls: 0 } },
        { event: "done", content: null },
      ];
      const body = frames.map((item) => `data: ${JSON.stringify(item)}\n\n`).join("");
      return route.fulfill({ status: 200, contentType: "text/event-stream", body });
    }
    return route.fulfill({ json: {} });
  });
  return { chatPayloads, terminalMessages, memories, refreshCalls: () => refreshCalls };
}


test("Chat 与 Coding 模式隔离，切换器保持居中", async ({ page }) => {
  await mockApplication(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Chatbot" })).toBeVisible();
  await expect(page.getByText("RAG", { exact: true })).toBeVisible();

  const alignment = await page.locator(".topbar").evaluate((topbar) => {
    const switcher = topbar.querySelector(".segmented").getBoundingClientRect();
    const bar = topbar.getBoundingClientRect();
    return Math.abs((switcher.left + switcher.width / 2) - (bar.left + bar.width / 2));
  });
  expect(alignment).toBeLessThan(2);

  await page.getByRole("button", { name: "Coding" }).click();
  await expect(page.getByRole("heading", { name: "Coding Agent" })).toBeVisible();
  await expect(page.getByText("EXPLORER", { exact: true })).toBeVisible();
  await expect(page.locator(".terminal-host")).toBeVisible();
  await expect(page.getByText("RAG", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Chat" }).click();
  await expect(page.getByText("RAG", { exact: true })).toBeVisible();
  await expect(page.getByText("EXPLORER", { exact: true })).toHaveCount(0);
});


test("回答轨迹自动折叠，Chat 代码可直接运行", async ({ page }) => {
  const state = await mockApplication(page);
  await page.goto("/");
  await page.getByPlaceholder("输入无线通信问题、仿真需求或代码任务...").fill("生成 Python 示例");
  await page.getByRole("button", { name: "发送" }).click();

  const trace = page.getByRole("button", { name: /运行轨迹 · 1 轮/ });
  await expect(trace).toBeVisible();
  await expect(page.locator(".trace-turns")).toBeHidden();
  await trace.click();
  await expect(page.getByRole("button", { name: /Turn 1.*已完成/ })).toBeVisible();
  await page.getByRole("button", { name: /Turn 1.*已完成/ }).click();
  await expect(page.getByText("LLM 响应", { exact: true })).toBeVisible();
  await expect(page.getByText("我将给出一个示例。", { exact: true })).toBeVisible();
  await expect(page.getByText("Context ready", { exact: true })).toBeVisible();
  await page.locator("[data-run-code]").click();
  await expect(page.getByText("hello from code", { exact: false })).toBeVisible();
  expect(state.chatPayloads[0]).toMatchObject({ mode: "chatbot", use_rag: true, use_web: false });
});


test("Agent 轨迹按思考、动作和观察配对并渲染 diff", async ({ page }) => {
  await mockApplication(page, {
    chatFrames: [
      { event: "task", content: { task_id: "d".repeat(32), status: "running" } },
      { event: "turn_start", content: { index: 0 } },
      { event: "thinking", content: { message_id: "message-1", delta: "先检查文件，再修改。" } },
      { event: "tool_use", content: { id: "tool-1", name: "write_file", input: { path: "main.py", content: "print('ok')\n" } } },
      {
        event: "tool_result",
        content: {
          id: "tool-1", name: "write_file", is_error: false,
          output: JSON.stringify({ message: "Wrote main.py", diff: "--- /dev/null\n+++ b/main.py\n@@ -0,0 +1 @@\n+print('ok')\n" }),
        },
      },
      { event: "llm_response", content: { turn: 0, text: "文件已经创建。", tool_calls: [] } },
      { event: "answer", content: "完成。" },
      { event: "turn_end", content: { index: 0, tool_calls: 1 } },
      { event: "done", content: null },
    ],
  });
  await page.goto("/");
  await page.getByPlaceholder("输入无线通信问题、仿真需求或代码任务...").fill("创建文件");
  await page.getByRole("button", { name: "发送" }).click();

  await page.getByRole("button", { name: /运行轨迹 · 1 轮/ }).click();
  await page.getByRole("button", { name: /Turn 1.*已完成/ }).click();
  await expect(page.getByText("思考过程", { exact: true })).toBeVisible();
  await expect(page.locator(".agent-tool-header").getByText("Write", { exact: true })).toBeVisible();
  await expect(page.locator(".agent-diff .added")).toContainText("print('ok')");
  await expect(page.getByText("文件已经创建。", { exact: true })).toBeVisible();
});


test("Coding 审批固定显示在输入框上方", async ({ page }) => {
  const taskId = "b".repeat(32);
  await mockApplication(page, {
    conversations: [{ id: 99, mode: "coding-agent", title: "审批测试", permission_mode: "workspace-write" }],
    tasks: [{ id: taskId, conversation_id: 99, status: "waiting-approval", has_checkpoint: true }],
    taskDetails: {
      [taskId]: {
        approvals: [{
          id: "c".repeat(32),
          tool_name: "run_command",
          input: { argv: ["python", "main.py"] },
          capability: "execute",
          status: "pending",
        }],
      },
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Coding" }).click();

  const approval = page.getByRole("alertdialog", { name: "工具执行审批" });
  await expect(approval).toBeVisible();
  await expect(approval).toContainText("python main.py");
  await expect(page.locator(".trace-events .approval-actions")).toHaveCount(0);
  await approval.getByRole("button", { name: "允许" }).click();
  await expect(approval).toBeHidden();
});


test("终端 shell 自动恢复状态对前端可见", async ({ page }) => {
  await mockApplication(page, { terminalRestart: true });
  await page.goto("/");
  await page.getByRole("button", { name: "Coding" }).click();
  await expect(page.getByText("shell restarted", { exact: true })).toBeVisible();
});


test("切换会话不会中断原任务，任务状态按会话隔离", async ({ page }) => {
  await mockApplication(page, {
    conversations: [
      { id: 1, mode: "chatbot", title: "会话一", permission_mode: "workspace-write" },
      { id: 2, mode: "chatbot", title: "会话二", permission_mode: "workspace-write" },
    ],
    chatDelayMs: 350,
  });
  await page.goto("/");
  await page.getByPlaceholder("输入无线通信问题、仿真需求或代码任务...").fill("后台继续回答");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.locator(".conversation-item", { hasText: "会话一" }).locator(".conversation-running")).toBeVisible();

  await page.locator(".conversation-item", { hasText: "会话二" }).click();
  await expect(page.getByPlaceholder("输入无线通信问题、仿真需求或代码任务...")).toBeEnabled();
  await expect(page.locator(".conversation-item", { hasText: "会话一" }).locator(".conversation-running")).toHaveCount(0);

  await page.locator(".conversation-item", { hasText: "会话一" }).click();
  await expect(page.getByText("结果如下：", { exact: false })).toBeVisible();
});


test("终端 Ctrl+Z 发送平台 EOF 请求", async ({ page }) => {
  const state = await mockApplication(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Coding" }).click();
  await page.locator(".terminal-host").click();
  await page.keyboard.press("Control+z");

  await expect.poll(() => state.terminalMessages.some((message) => message.type === "eof")).toBe(true);
});


test("过期访问令牌只刷新一次并恢复登录", async ({ page }) => {
  const state = await mockApplication(page, { expiredSession: true, username: "renewed-user" });
  await page.addInitScript(() => {
    localStorage.setItem("commchatbot.access_token", "expired-access");
    localStorage.setItem("commchatbot.refresh_token", "valid-refresh");
  });
  await page.goto("/");
  await expect(page.getByText("renewed-user", { exact: true })).toBeVisible();
  expect(state.refreshCalls()).toBe(1);
  const tokens = await page.evaluate(() => ({
    access: localStorage.getItem("commchatbot.access_token"),
    refresh: localStorage.getItem("commchatbot.refresh_token"),
  }));
  expect(tokens).toEqual({ access: "fresh-access", refresh: "fresh-refresh" });
});


test("Coding 编辑器支持多文件标签", async ({ page }) => {
  await mockApplication(page, {
    workspaceFiles: {
      "main.py": "from utils import greet\n\nvalue=1\n",
      "utils.py": "def greet(name):\n    return f'Hi {name}'\n",
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Coding" }).click();
  await expect(page.locator(".file-explorer > .explorer-toolbar")).toBeVisible();
  await expect(page.locator(".editor-header .explorer-toolbar")).toHaveCount(0);
  await page.getByRole("button", { name: "main.py", exact: true }).click();
  await expect.poll(async () => page.locator(".monaco-editor .view-line span").evaluateAll((tokens) => (
    new Set(tokens.flatMap((token) => [...token.classList].filter((name) => name.startsWith("mtk")))).size
  ))).toBeGreaterThan(1);
  await page.getByRole("button", { name: "utils.py", exact: true }).click();
  await expect(page.locator(".editor-tab")).toHaveCount(2);
  await expect(page.locator(".editor-tab.active")).toContainText("utils.py");
  await page.locator(".editor-tab", { hasText: "main.py" }).click();
  await expect(page.locator(".editor-tab.active")).toContainText("main.py");
  await page.getByTitle("格式化文档 (Shift+Alt+F)").click();
});


test("记忆管理支持隐私开关与手动增删改", async ({ page }) => {
  const state = await mockApplication(page);
  await page.goto("/");
  await page.getByTitle("记忆管理").click();
  const dialog = page.getByRole("region", { name: "记忆管理" });
  await expect(dialog).toBeVisible();
  await dialog.getByText("自动提取长期记忆").click();
  await dialog.getByRole("button", { name: "新增" }).click();
  await dialog.getByPlaceholder("记忆名称").fill("回答语言");
  await dialog.getByPlaceholder("记忆内容").fill("使用中文");
  await dialog.getByRole("button", { name: "保存" }).click();
  await expect(dialog.getByText("回答语言", { exact: true })).toBeVisible();
  expect(state.memories).toHaveLength(1);

  await dialog.getByTitle("编辑").click();
  await dialog.getByPlaceholder("记忆内容").fill("优先使用简体中文");
  await dialog.getByRole("button", { name: "保存" }).click();
  await expect(dialog.getByText("优先使用简体中文", { exact: true })).toBeVisible();
  await page.on("dialog", (prompt) => prompt.accept());
  await dialog.getByTitle("删除").click();
  await expect(dialog.getByText("暂无匹配记忆")).toBeVisible();
});
