import { spawn } from "node:child_process";
import { resolve } from "node:path";

const cli = resolve("node_modules/@playwright/test/cli.js");
const child = spawn(process.execPath, [cli, "test", "e2e/live.spec.js", ...process.argv.slice(2)], {
  cwd: process.cwd(),
  env: { ...process.env, LIVE_E2E: "1", E2E_EXTERNAL_SERVER: "1" },
  stdio: "inherit",
});
child.once("exit", (code) => process.exit(code ?? 1));
