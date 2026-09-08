import { spawn } from "node:child_process";
import { resolve } from "node:path";

const runner = resolve("e2e/run.mjs");
const child = spawn(process.execPath, [runner, "e2e/live.spec.js", ...process.argv.slice(2)], {
  cwd: process.cwd(),
  // The shared runner starts Vite when needed and reuses an existing server.
  env: { ...process.env, LIVE_E2E: "1" },
  stdio: "inherit",
});
child.once("exit", (code) => process.exit(code ?? 1));
