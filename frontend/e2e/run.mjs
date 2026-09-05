import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { createServer } from "vite";

let server;
try {
  const response = await fetch("http://127.0.0.1:5173/", { signal: AbortSignal.timeout(1000) });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
} catch {
  server = await createServer({
    server: { host: "127.0.0.1", port: 5173, strictPort: true },
  });
  await server.listen();
}

const cli = resolve("node_modules/@playwright/test/cli.js");
const child = spawn(process.execPath, [cli, "test", ...process.argv.slice(2)], {
  cwd: process.cwd(),
  env: { ...process.env, E2E_EXTERNAL_SERVER: "1" },
  stdio: "inherit",
});

const exitCode = await new Promise((resolveExit) => {
  child.once("exit", (code) => resolveExit(code ?? 1));
});
if (server) await server.close();
process.exit(exitCode);
