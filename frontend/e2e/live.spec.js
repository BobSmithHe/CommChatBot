import { expect, test } from "@playwright/test";

test.describe("live full-stack", () => {
  test.skip(!process.env.LIVE_E2E, "Set LIVE_E2E=1 with real API and frontend services running");

  test("register, authenticate, create both conversation modes and render the app", async ({ page, request }) => {
    const suffix = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const account = { username: `live-${suffix}`, email: `live-${suffix}@example.test`, password: "ValidPass123!" };
    const registered = await request.post("http://127.0.0.1:8765/api/auth/register", { data: account });
    expect(registered.ok()).toBeTruthy();
    const login = await request.post("http://127.0.0.1:8765/api/auth/login", { data: { username: account.username, password: account.password } });
    expect(login.ok()).toBeTruthy();
    const session = await login.json();
    const headers = { Authorization: `Bearer ${session.access_token}` };
    for (const mode of ["chatbot", "coding-agent"]) {
      const created = await request.post("http://127.0.0.1:8765/api/conversations", { headers, data: { mode } });
      expect(created.ok()).toBeTruthy();
      expect((await created.json()).mode).toBe(mode);
    }
    await page.addInitScript((tokens) => {
      localStorage.setItem("commchatbot.access_token", tokens.access_token);
    }, session);
    await page.goto("/");
    await expect(page.locator("body")).toContainText("CommChatBot");
  });
});
